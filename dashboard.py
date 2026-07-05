#!/usr/bin/env python3
"""
Pinterest Scan — Live Bot Dashboard
Run:  python dashboard.py
Open: http://localhost:8765   (auto-refreshes every 15 s)

Logs are read from MySQL bot_logs table (written by db_logger.py).
No log files, no restart needed — bots log to DB automatically.
"""

import html as _html
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ── paths ──────────────────────────────────────────────────────────────────────
BASE          = Path(__file__).parent
PROGRESS_JSON = BASE / "progress.json"
MAGIC_LOG     = BASE / "magic_log.jsonl"
ENV_FILE      = BASE / ".env"

BOT_SCRIPTS = {
    "magic_scroll": "magic_scroll.py",
    "bot10":        "10_domain_quick_scrape_api.py",
    "bot13":        "13_scan-website-interface-by-ia.py",
    "bot14":        "14_download_blog_pin_links.py",
    "bot15":        "15_post_planner.py",
}

_scan_jobs = {}

# ── load .env ──────────────────────────────────────────────────────────────────
def load_env():
    env = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

# ── process detection ──────────────────────────────────────────────────────────
def get_running_bots_local():
    """Check LOCAL processes on this machine via psutil."""
    result = {k: (False, None) for k in BOT_SCRIPTS}
    try:
        import psutil
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                cmd = " ".join(proc.info["cmdline"] or [])
                for key, script in BOT_SCRIPTS.items():
                    if script in cmd:
                        result[key] = (True, proc.info["pid"])
            except Exception:
                pass
    except ImportError:
        pass
    return result

def get_running_bots_from_logs(conn):
    """Detect RUNNING bots from MySQL bot_logs recency (works across machines).
    If last log entry < 3 min ago → RUNNING. Between 3-10 min → IDLE. Older → STOPPED."""
    result = {}
    now = time.time()
    for key in BOT_SCRIPTS:
        try:
            with conn.cursor() as c:
                c.execute(
                    "SELECT ts FROM bot_logs WHERE bot=%s ORDER BY id DESC LIMIT 1",
                    (key,)
                )
                row = c.fetchone()
            if row and row[0]:
                diff = now - row[0].timestamp()
                if diff < 180:
                    result[key] = ("running", None)
                elif diff < 600:
                    result[key] = ("idle", int(diff // 60))
                else:
                    result[key] = ("stopped", None)
            else:
                result[key] = ("no_logs", None)
        except Exception:
            result[key] = ("unknown", None)
    return result

# ── MySQL log reader ───────────────────────────────────────────────────────────
def get_bot_logs_from_mysql(conn, bot_key, n=150):
    """Return last n log rows for a bot from the bot_logs MySQL table.
    Returns list of (ts, level, message) or None if table doesn't exist yet."""
    try:
        with conn.cursor() as c:
            c.execute(
                """SELECT ts, level, message FROM bot_logs
                   WHERE bot = %s ORDER BY id DESC LIMIT %s""",
                (bot_key, n),
            )
            rows = c.fetchall()
        return list(reversed(rows))  # oldest first for display
    except Exception:
        return None  # table not yet created (bots haven't started with db_logger)

def log_stats_from_rows(rows):
    """Count errors/warnings in log rows."""
    if not rows:
        return 0, 0
    errors   = sum(1 for _, lvl, msg in rows if lvl == "ERROR" or
                   re.search(r'\berror\b|\bexception\b|\btraceback\b', str(msg), re.I))
    warnings = sum(1 for _, lvl, msg in rows if lvl == "WARN" or
                   re.search(r'\bwarn\b', str(msg), re.I))
    return errors, warnings

# ── keyword stats — reads from Google Sheet (source of truth for magic_scroll) ─
def get_keyword_stats():
    """Primary: Google Sheet webapp get_keywords action.
    Fallback: keywords.txt (total) + progress.json (done count)."""
    try:
        webapp = json.loads((BASE / "google_sheets_webapp.json").read_text())
        url    = webapp["url"]
        secret = webapp.get("secret", "")
        import requests as _req
        resp = _req.post(
            url,
            json={"action": "get_keywords", "secret": secret},
            timeout=20,
        )
        data = resp.json()
        if not data.get("ok"):
            raise ValueError(data.get("error", "sheet error"))

        keywords = data.get("keywords", [])
        done, not_yet, pending, other = 0, 0, 0, 0
        recent = []
        for item in keywords:
            s = (item.get("status") or "").strip().lower()
            if s == "done":
                done += 1
                recent.append(item["keyword"])
            elif s in ("not yet", "not_yet", ""):
                not_yet += 1
            elif s == "pending":
                pending += 1
            else:
                other += 1

        total = len(keywords)
        return {
            "total":   total,
            "done":    done,
            "not_yet": not_yet,
            "pending": pending,
            "other":   other,
            "pct":     round(done / total * 100, 1) if total else 0,
            "recent":  [{"ts": "—", "kw": k} for k in recent[-6:]],
            "source":  "Google Sheet",
        }
    except Exception as sheet_err:
        # fallback: keywords.txt count + progress.json done
        try:
            kws  = [l.strip() for l in open(BASE / "keywords.txt", errors="replace")
                    if l.strip() and not l.startswith("#")]
            prog = json.loads(PROGRESS_JSON.read_text(errors="replace"))
            done = sum(1 for v in prog.values()
                       if (v.get("status") if isinstance(v, dict) else v) == "done")
            total = len(kws)
            return {
                "total": total, "done": done,
                "not_yet": total - done, "pending": 0, "other": 0,
                "pct": round(done / total * 100, 1) if total else 0,
                "recent": [],
                "source": f"keywords.txt (Sheet unavailable: {sheet_err})",
            }
        except Exception as e2:
            return {"error": str(e2)}

def get_magic_log():
    try:
        lines = MAGIC_LOG.read_text(errors="replace").strip().splitlines()
        return [json.loads(l) for l in reversed(lines[-15:]) if l.strip()][:10]
    except Exception:
        return []

# ── mysql stats + logs ─────────────────────────────────────────────────────────
def get_mysql_stats(env):
    try:
        import pymysql
        conn = pymysql.connect(
            host=env.get("MYSQL_HOST", "72.61.197.144"),
            port=int(env.get("MYSQL_PORT", 3306)),
            db=env.get("MYSQL_DB", "data_pint"),
            user=env.get("MYSQL_USER", "data_pint_user"),
            password=env.get("MYSQL_PASSWORD", ""),
            charset="utf8mb4", connect_timeout=8,
        )
        c = conn.cursor()

        totals = {}
        for t in ("pinners", "boards", "pins"):
            try:
                c.execute(f"SELECT COUNT(*) FROM `{t}`"); totals[t] = c.fetchone()[0]
            except Exception: totals[t] = "?"

        c.execute("SELECT status, COUNT(*) FROM scraped_websites GROUP BY status")
        sw_status = dict(c.fetchall())

        c.execute("""SELECT site_type, COUNT(*) FROM scraped_websites
                     WHERE status='done' GROUP BY site_type ORDER BY COUNT(*) DESC""")
        sw_types = c.fetchall()

        try:
            c.execute("""SELECT ia_confirmed_blog, COUNT(*) FROM scraped_websites
                         WHERE status='done' GROUP BY ia_confirmed_blog""")
            ia_rows = {str(k): v for k, v in c.fetchall()}
        except Exception: ia_rows = {}

        try:
            c.execute("""SELECT status_website_scaned_by_ia, COUNT(*) FROM scraped_websites
                         GROUP BY status_website_scaned_by_ia""")
            ai_scan = {str(k): v for k, v in c.fetchall()}
        except Exception: ai_scan = {}

        try:
            c.execute("""SELECT domain, site_type, last_scraped_at FROM scraped_websites
                         WHERE status='done' ORDER BY last_scraped_at DESC LIMIT 6""")
            sw_recent = c.fetchall()
        except Exception: sw_recent = []

        try:
            c.execute("""SELECT domain, status, last_scraped_at FROM scraped_websites
                         WHERE status IN ('failed','blocked','error')
                         ORDER BY last_scraped_at DESC LIMIT 6""")
            sw_failed = c.fetchall()
        except Exception: sw_failed = []

        # step 14
        try:
            c.execute("""SELECT link_download_status, COUNT(*) FROM pins
                         WHERE pinner_username IN
                           (SELECT username FROM pinners WHERE site_type LIKE '%blog%')
                         GROUP BY link_download_status ORDER BY COUNT(*) DESC""")
            dl_status = c.fetchall()
        except Exception: dl_status = []

        try:
            c.execute("SELECT COUNT(*) FROM pinners WHERE site_type LIKE '%blog%'")
            blog_pinners = c.fetchone()[0]
        except Exception: blog_pinners = "?"

        try:
            c.execute("""SELECT COUNT(*) FROM pins WHERE pinner_username IN
                           (SELECT username FROM pinners WHERE site_type LIKE '%blog%')""")
            blog_pins_total = c.fetchone()[0]
        except Exception: blog_pins_total = "?"

        try:
            c.execute("""SELECT p.link, p.link_download_status, p.link_downloaded_at
                         FROM pins p WHERE p.link_download_status IS NOT NULL
                           AND p.link_download_status != ''
                         ORDER BY p.link_downloaded_at DESC LIMIT 8""")
            dl_recent = c.fetchall()
        except Exception: dl_recent = []

        # ── step 13: AI scanner stats ──────────────────────────────────────────
        try:
            c.execute("""SELECT status_website_scaned_by_ia, COUNT(*) FROM scraped_websites
                         GROUP BY status_website_scaned_by_ia""")
            ai13_status = {str(k): v for k, v in c.fetchall()}
        except Exception: ai13_status = {}

        try:
            c.execute("""SELECT type_website_scaned_by_ia, COUNT(*) FROM scraped_websites
                         WHERE status_website_scaned_by_ia LIKE 'Done%'
                         GROUP BY type_website_scaned_by_ia ORDER BY COUNT(*) DESC LIMIT 12""")
            ai13_types = c.fetchall()
        except Exception: ai13_types = []

        try:
            c.execute("""SELECT domain, type_website_scaned_by_ia, category_website_scaned_by_ia,
                                status_website_scaned_by_ia
                         FROM scraped_websites
                         WHERE status_website_scaned_by_ia IS NOT NULL
                           AND status_website_scaned_by_ia != ''
                         ORDER BY id DESC LIMIT 8""")
            ai13_recent = c.fetchall()
        except Exception: ai13_recent = []

        try:
            c.execute("""SELECT domain, status_website_scaned_by_ia FROM scraped_websites
                         WHERE status_website_scaned_by_ia LIKE 'Failed%'
                            OR status_website_scaned_by_ia LIKE 'Blocked%'
                         ORDER BY id DESC LIMIT 6""")
            ai13_failed = c.fetchall()
        except Exception: ai13_failed = []

        # eligible for step 13 (not yet scanned by AI)
        try:
            c.execute("""SELECT COUNT(*) FROM scraped_websites
                         WHERE (status_website_scaned_by_ia IS NULL OR status_website_scaned_by_ia = '')
                           AND status = 'done'
                           AND site_type NOT IN ('Store','Social Media','Link-in-Bio')""")
            ai13_pending = c.fetchone()[0]
        except Exception: ai13_pending = "?"

        # ── captcha solver stats ───────────────────────────────────────────────
        try:
            c.execute("""
                SELECT service,
                       COUNT(*)             AS total,
                       SUM(success)         AS successes,
                       COUNT(*)-SUM(success) AS failures,
                       ROUND(AVG(success)*100,1) AS success_rate,
                       ROUND(AVG(solve_time_ms)/1000,1) AS avg_solve_s
                FROM captcha_stats
                GROUP BY service
            """)
            cap_by_service = c.fetchall()
        except Exception: cap_by_service = []

        try:
            c.execute("""
                SELECT COUNT(*) AS total, SUM(success) AS ok
                FROM captcha_stats
                WHERE ts >= CURDATE()
            """)
            row = c.fetchone()
            cap_today = {"total": int(row[0] or 0), "ok": int(row[1] or 0)}
        except Exception: cap_today = {"total": 0, "ok": 0}

        try:
            c.execute("""
                SELECT service, captcha_type, url, success, solve_time_ms, error
                FROM captcha_stats ORDER BY ts DESC LIMIT 12
            """)
            cap_recent = c.fetchall()
        except Exception: cap_recent = []

        try:
            c.execute("""
                SELECT message, ts FROM captcha_warnings
                WHERE resolved=0 ORDER BY ts DESC LIMIT 5
            """)
            cap_warnings = c.fetchall()
        except Exception: cap_warnings = []

        # ── step 15: post planner stats ────────────────────────────────────────
        try:
            c.execute("SELECT content_type, COUNT(*) FROM pin_content_analysis GROUP BY content_type ORDER BY COUNT(*) DESC")
            plan_by_type = c.fetchall()
        except Exception: plan_by_type = []

        try:
            c.execute("SELECT COUNT(*) FROM pin_content_analysis")
            plan_total = c.fetchone()[0]
        except Exception: plan_total = 0

        try:
            c.execute("""SELECT COUNT(*) FROM pins
                         WHERE link_download_status='Done'
                           AND link_html IS NOT NULL AND link_html != ''
                           AND id NOT IN (SELECT pin_id FROM pin_content_analysis)""")
            plan_pending = c.fetchone()[0]
        except Exception: plan_pending = "?"

        try:
            c.execute("""SELECT pinner_username, content_type, category, post_score,
                                best_posting_days, scanned_at, pin_url
                         FROM pin_content_analysis
                         ORDER BY scanned_at DESC LIMIT 10""")
            plan_recent = c.fetchall()
        except Exception: plan_recent = []

        try:
            c.execute("""SELECT best_posting_days, content_type, category, description, post_score, pin_url
                         FROM pin_content_analysis WHERE post_score >= 5
                         ORDER BY post_score DESC LIMIT 300""")
            plan_schedule_rows = c.fetchall()
        except Exception: plan_schedule_rows = []

        # ── bot logs from MySQL bot_logs table ─────────────────────────────────
        bot_logs = {}
        for key in ("magic_scroll", "bot10", "bot13", "bot14"):
            bot_logs[key] = get_bot_logs_from_mysql(conn, key, n=150)

        # ── bot running status from log recency ────────────────────────────────
        bot_status = get_running_bots_from_logs(conn)

        conn.close()
        return {"ok": True, "totals": totals, "sw_status": sw_status,
                "sw_types": sw_types, "ia_rows": ia_rows, "ai_scan": ai_scan,
                "sw_recent": sw_recent, "sw_failed": sw_failed,
                "dl_status": dl_status, "blog_pinners": blog_pinners,
                "blog_pins_total": blog_pins_total, "dl_recent": dl_recent,
                "bot_logs": bot_logs,
                "bot_status": bot_status,
                "ai13_status": ai13_status,
                "ai13_types": ai13_types,
                "ai13_recent": ai13_recent,
                "ai13_failed": ai13_failed,
                "ai13_pending": ai13_pending,
                "cap_by_service": cap_by_service,
                "cap_today": cap_today,
                "cap_recent": cap_recent,
                "cap_warnings": cap_warnings,
                "plan_by_type": plan_by_type,
                "plan_total": plan_total,
                "plan_pending": plan_pending,
                "plan_recent": plan_recent,
                "plan_schedule_rows": plan_schedule_rows}
    except ImportError:
        return {"ok": False, "error": "pymysql not installed — pip install pymysql"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ── cache ──────────────────────────────────────────────────────────────────────
_cache = {}
_lock  = threading.Lock()

def refresh_cache():
    env        = load_env()
    local_bots = get_running_bots_local()
    kw         = get_keyword_stats()
    ml         = get_magic_log()
    mysql      = get_mysql_stats(env)
    with _lock:
        _cache.update({"local_bots": local_bots, "kw": kw,
                        "ml": ml, "mysql": mysql,
                        "ts": datetime.now().strftime("%H:%M:%S")})

def bg_refresh():
    while True:
        try: refresh_cache()
        except Exception: pass
        time.sleep(15)

# ── HTML helpers ───────────────────────────────────────────────────────────────
def e(s): return _html.escape(str(s or ""))

def status_pill(running, pid=None):
    if running:
        return f'<span class="pill pill-run">● RUNNING  pid {pid}</span>'
    return '<span class="pill pill-stop">○ STOPPED</span>'

def pct_bar(val, total, color="#22c55e"):
    p = round(val / total * 100, 1) if total else 0
    return (f'<div class="bar-wrap"><div class="bar-fill" style="width:{p}%;background:{color}"></div></div>'
            f'<small class="muted">{int(val):,} / {int(total):,} &nbsp;({p} %)</small>')

def render_log(rows, bot_key):
    """Render log rows (ts, level, message) from MySQL with colour coding."""
    if rows is None:
        # bot_logs table not yet created — bots haven't been restarted with db_logger yet
        cmds = {
            "magic_scroll": "python magic_scroll.py --15m",
            "bot10":        "python 10_domain_quick_scrape_api.py",
            "bot13":        "python 13_scan-website-interface-by-ia.py",
            "bot14":        "python 14_download_blog_pin_links.py",
        }
        return (f'<div class="no-log">'
                f'<b>No DB logs yet.</b> Restart the bot once — logs save to MySQL automatically from then on:<br><br>'
                f'<code>{e(cmds.get(bot_key, ""))}</code>'
                f'</div>')

    if not rows:
        return '<div class="no-log muted">No log entries in database yet.</div>'

    out = ['<div class="log-box">']
    for ts_val, level, message in rows:
        msg = str(message or "")
        lw  = msg.lower()
        lvl = (level or "INFO").upper()
        if lvl == "ERROR" or re.search(r'traceback|exception|error\b', lw):
            cls = "ll-err"
        elif lvl == "WARN" or re.search(r'\bwarn\b', lw):
            cls = "ll-warn"
        elif lvl == "OK" or re.search(r'\bdone\b|✓|✅|success|complete', lw):
            cls = "ll-ok"
        elif re.search(r'running|starting|pass #|cycle|batch|worker', lw):
            cls = "ll-info"
        else:
            cls = "ll-dim"
        ts_str = str(ts_val)[:19] if ts_val else ""
        out.append(f'<div class="{cls}"><span class="ll-ts">{e(ts_str)}</span>  {e(msg)}</div>')
    out.append('</div>')
    return "".join(out)

def _ai13_pill(st):
    st = str(st or "")
    if st.startswith("Done"):   return '<span class="pill pill-run" style="font-size:10px">Done</span>'
    if "Fail" in st:            return '<span class="pill pill-err" style="font-size:10px">Failed</span>'
    if "Block" in st:           return '<span class="pill pill-warn" style="font-size:10px">Blocked</span>'
    return f'<span class="pill pill-info" style="font-size:10px">{e(st[:20])}</span>'

def stat_box(n, label, color="#e2e8f0"):
    return (f'<div class="stat-box">'
            f'<div class="stat-n" style="color:{color}">{n}</div>'
            f'<div class="stat-l">{label}</div></div>')

# ── full page ──────────────────────────────────────────────────────────────────
def build_page():
    with _lock:
        local_bots = dict(_cache.get("local_bots", {}))
        kw         = dict(_cache.get("kw",   {}))
        ml         = list(_cache.get("ml",   []))
        mysql      = dict(_cache.get("mysql",{}))
        ts         = _cache.get("ts", "…")
    bot_logs   = mysql.get("bot_logs",  {}) if mysql.get("ok") else {}
    bot_status = mysql.get("bot_status",{}) if mysql.get("ok") else {}

    # ── process status bar ─────────────────────────────────────────────────────
    def bot_pill(key, label):
        """Show running status: MySQL log recency first, local psutil as fallback."""
        db_st, db_extra = bot_status.get(key, ("unknown", None))
        local_running, local_pid = local_bots.get(key, (False, None))

        if db_st == "running":
            pill = '<span class="pill pill-run">● RUNNING</span>'
        elif db_st == "idle":
            pill = f'<span class="pill pill-idle">◑ IDLE ({db_extra}m ago)</span>'
        elif db_st == "no_logs":
            if local_running:
                pill = f'<span class="pill pill-run">● RUNNING (local pid {local_pid})</span>'
            else:
                pill = '<span class="pill pill-stop">○ STOPPED <small style="font-weight:400">(restart to enable DB logs)</small></span>'
        else:
            pill = '<span class="pill pill-stop">○ STOPPED</span>'
        return (f'<div class="sb-item" id="bot-{key}"><span class="sb-name">{label}</span>{pill}</div>')

    status_bar = f'''
    <div class="status-bar">
      {bot_pill("magic_scroll", "magic_scroll (hp)")}
      {bot_pill("bot10",        "Step 10 — classifier")}
      {bot_pill("bot13",        "Step 13 — AI scanner")}
      {bot_pill("bot14",        "Step 14 — downloader")}
    </div>'''

    # ── keyword card ───────────────────────────────────────────────────────────
    if "error" in kw:
        kw_body = f'<p class="err">Error: {e(kw["error"])}</p>'
    else:
        done    = kw.get("done", 0)
        total   = kw.get("total", 1)
        not_yet = kw.get("not_yet", 0)
        pending = kw.get("pending", 0)
        source  = kw.get("source", "")
        recent_rows = "".join(
            f'<tr><td class="muted">{e(r["ts"])}</td><td>{e(r["kw"])}</td></tr>'
            for r in kw.get("recent", [])
        )
        kw_body = f'''
        <div class="stat-row">
          {stat_box(f'{done:,}',    "Done ✓",    "#22c55e")}
          {stat_box(f'{not_yet:,}', "Not Yet",    "#94a3b8")}
          {stat_box(f'{pending:,}', "Pending/Running", "#3b82f6")}
          {stat_box(f'{total:,}',   "Total kw",   "#a78bfa")}
        </div>
        {pct_bar(done, total)}
        <div class="muted" style="font-size:11px;margin-top:6px">Source: {e(source)}</div>
        {'<div class="sub-title" style="margin-top:12px">Recently done</div><table class="mini-table">' + recent_rows + '</table>' if recent_rows else ''}'''

    # ── magic_log card ─────────────────────────────────────────────────────────
    if ml:
        ml_rows = "".join(
            f'<tr><td class="muted">{e(x.get("ts",""))}</td>'
            f'<td class="purple">{e(x.get("event","?"))}</td>'
            f'<td class="blue">{e(x.get("computer",""))}</td>'
            f'<td class="muted">{x.get("pinners","?")} pinners · {x.get("pins","?")} pins</td></tr>'
            for x in ml
        )
        ml_body = f'<table class="mini-table">{ml_rows}</table>'
    else:
        ml_body = '<p class="muted">No cycle log yet.</p>'

    # ── mysql / step10 / step14 ────────────────────────────────────────────────
    if not mysql.get("ok"):
        err = e(mysql.get("error", "unknown"))
        mysql_body = step10_body = step13_body = step14_body = f'<p class="err">⚠ MySQL: {err}</p>'
        cap_card = plan_card = f'<div class="card" style="border:1px dashed #334155"><p class="err">⚠ MySQL unavailable: {err}</p></div>'
    else:
        t = mysql["totals"]
        mysql_body = f'''
        <div class="stat-row">
          {stat_box(f'{t.get("pinners","?"):,}', "Pinners",  "#a78bfa")}
          {stat_box(f'{t.get("boards","?"):,}',  "Boards",   "#60a5fa")}
          {stat_box(f'{t.get("pins","?"):,}',    "Pins",     "#34d399")}
        </div>'''

        sw = mysql["sw_status"]
        sw_done = sw.get("done", 0); sw_run = sw.get("running", 0)
        sw_ny   = sw.get("not yet", sw.get("not_yet", 0))
        sw_fail = sw.get("failed", 0) + sw.get("error", 0)
        sw_blk  = sw.get("blocked", 0)
        sw_tot  = sum(sw.values()) if sw else 1

        type_rows = "".join(
            f'<tr><td>{e(t2 or "—")}</td><td class="blue"><b>{n:,}</b></td></tr>'
            for t2, n in mysql.get("sw_types", [])
        )
        ia = mysql.get("ia_rows", {})
        ai = mysql.get("ai_scan", {})
        ai_done = ai.get("Done", 0)
        ai_fail = sum(v for k, v in ai.items() if k and "fail" in k.lower())
        ai_blk  = sum(v for k, v in ai.items() if k and "block" in k.lower())
        ai_none = ai.get("None", 0)

        recent_sw = "".join(
            f'<tr><td class="blue">{e(d)}</td><td class="green">{e(tp or "—")}</td>'
            f'<td class="muted">{e(str(ts2 or "")[:16])}</td></tr>'
            for d, tp, ts2 in mysql.get("sw_recent", [])
        )
        failed_sw = "".join(
            f'<tr><td class="err">{e(d)}</td>'
            f'<td><span class="pill pill-err">{e(st)}</span></td>'
            f'<td class="muted">{e(str(ts2 or "")[:16])}</td></tr>'
            for d, st, ts2 in mysql.get("sw_failed", [])
        )

        step10_body = f'''
        <div class="stat-row">
          {stat_box(f'{sw_done:,}', "Done",    "#22c55e")}
          {stat_box(f'{sw_run:,}',  "Running", "#3b82f6")}
          {stat_box(f'{sw_ny:,}',   "Not Yet", "#94a3b8")}
          {stat_box(f'{sw_fail:,}', "Failed",  "#ef4444")}
          {stat_box(f'{sw_blk:,}',  "Blocked", "#f97316")}
        </div>
        {pct_bar(sw_done, sw_tot)}
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px">
          <div>
            <div class="sub-title">Site types (done)</div>
            <table class="mini-table">{type_rows}</table>
          </div>
          <div>
            <div class="sub-title">AI confirmation</div>
            <span class="green">✅ Blog confirmed: <b>{ia.get("1",0):,}</b></span><br>
            <span class="err">❌ Not blog: <b>{ia.get("0",0):,}</b></span><br>
            <span class="muted">⏳ Pending: <b>{ia.get("None",0):,}</b></span>
            <div class="sub-title" style="margin-top:10px">AI scanner (step 13)</div>
            <span class="green">Done: <b>{ai_done:,}</b></span> &nbsp;
            <span class="err">Failed: <b>{ai_fail:,}</b></span> &nbsp;
            <span style="color:#f97316">Blocked: <b>{ai_blk:,}</b></span> &nbsp;
            <span class="muted">Pending: <b>{ai_none:,}</b></span>
          </div>
        </div>
        {'<div class="sub-title" style="margin-top:12px">Recently classified</div><table class="mini-table">' + recent_sw + '</table>' if recent_sw else ''}
        {'<div class="sub-title err" style="margin-top:12px">Recent failures / blocked</div><table class="mini-table">' + failed_sw + '</table>' if failed_sw else ''}
        '''

        dl = mysql.get("dl_status", [])
        dl_d = {str(k or ""): v for k, v in dl}
        dl_done = dl_d.get("Done", 0)
        dl_none = dl_d.get("", 0) + dl_d.get("None", 0)
        dl_run  = dl_d.get("Running", 0)
        dl_fail = sum(v for k, v in dl_d.items() if "failed" in k.lower())
        dl_blk  = sum(v for k, v in dl_d.items() if "blocked" in k.lower())
        dl_tot  = mysql.get("blog_pins_total", 1) or 1

        dl_status_rows = "".join(
            f'<tr><td><span class="pill pill-auto">{e(str(k or "null"))}</span></td>'
            f'<td class="blue"><b>{v:,}</b></td></tr>'
            for k, v in dl
        )
        dl_recent_rows = "".join(
            f'<tr><td class="blue" style="max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{e((lnk or "")[:70])}</td>'
            f'<td><span class="pill pill-auto">{e(str(st or ""))}</span></td>'
            f'<td class="muted">{e(str(ts3 or "")[:16])}</td></tr>'
            for lnk, st, ts3 in mysql.get("dl_recent", [])
        )

        step14_body = f'''
        <div class="stat-row">
          {stat_box(f'{dl_done:,}', "Done ✓",  "#22c55e")}
          {stat_box(f'{dl_none:,}', "Not Yet",  "#94a3b8")}
          {stat_box(f'{dl_run:,}',  "Running",  "#3b82f6")}
          {stat_box(f'{dl_fail:,}', "Failed",   "#ef4444")}
          {stat_box(f'{dl_blk:,}',  "Blocked",  "#f97316")}
        </div>
        <div class="muted" style="margin:4px 0 6px">
          Blog pinners: <b class="green">{mysql.get("blog_pinners","?"):,}</b> &nbsp;·&nbsp;
          Total blog pins: <b class="blue">{mysql.get("blog_pins_total","?"):,}</b>
        </div>
        {pct_bar(dl_done, dl_tot)}
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px">
          <div><div class="sub-title">Status breakdown</div><table class="mini-table">{dl_status_rows}</table></div>
          {'<div><div class="sub-title">Recently downloaded</div><table class="mini-table">' + dl_recent_rows + '</table></div>' if dl_recent_rows else '<div></div>'}
        </div>'''

        # ── step 13 card body ──────────────────────────────────────────────────
        ai13  = mysql.get("ai13_status", {})
        ai13_done    = ai13.get("Done", 0)
        ai13_failed  = sum(v for k,v in ai13.items() if k and "fail"  in k.lower())
        ai13_blocked = sum(v for k,v in ai13.items() if k and "block" in k.lower())
        ai13_null    = ai13.get("None", 0) + ai13.get("", 0)
        ai13_tot     = ai13_done + ai13_failed + ai13_blocked + ai13_null or 1
        ai13_pend    = mysql.get("ai13_pending", "?")

        ai13_type_rows = "".join(
            f'<tr><td>{e(t or "—")}</td><td class="blue"><b>{n:,}</b></td></tr>'
            for t, n in mysql.get("ai13_types", [])
        )
        ai13_recent_rows = "".join(
            f'<tr><td class="blue" style="font-size:12px">{e(d)}</td>'
            f'<td style="font-size:12px;color:#a3e635">{e(tp or "—")}</td>'
            f'<td class="muted" style="font-size:11px">{e(cat or "")}</td>'
            f'<td>{_ai13_pill(st)}</td></tr>'
            for d, tp, cat, st in mysql.get("ai13_recent", [])
        )
        ai13_failed_rows = "".join(
            f'<tr><td class="err" style="font-size:12px">{e(d)}</td>'
            f'<td style="font-size:11px;color:#f97316">{e((st or "")[:60])}</td></tr>'
            for d, st in mysql.get("ai13_failed", [])
        )

        # "never run" only if bot13 has no DB logs AND no MySQL results
        bot13_db_status, _ = bot_status.get("bot13", ("unknown", None))
        not_started = (ai13_done == 0 and ai13_failed == 0 and ai13_blocked == 0
                       and bot13_db_status in ("no_logs", "unknown", "stopped"))
        step13_body = f'''
        {"<div class='no-log' style='margin-bottom:10px'>⚠ Step 13 has never run. Start it: <code>python 13_scan-website-interface-by-ia.py</code></div>" if not_started else ""}
        <div class="stat-row">
          {stat_box(f'{ai13_done:,}',    "Done ✓",        "#22c55e")}
          {stat_box(str(ai13_pend),      "Eligible/Pending","#94a3b8")}
          {stat_box(f'{ai13_failed:,}',  "Failed",         "#ef4444")}
          {stat_box(f'{ai13_blocked:,}', "Blocked",        "#f97316")}
        </div>
        {pct_bar(ai13_done, int(ai13_pend) + ai13_done if str(ai13_pend).isdigit() else ai13_done or 1)}
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px">
          <div>
            <div class="sub-title">AI-detected types (done)</div>
            <table class="mini-table">{ai13_type_rows if ai13_type_rows else "<tr><td class='muted'>none yet</td></tr>"}</table>
          </div>
          <div>
            {"<div class='sub-title'>Recently scanned</div><table class='mini-table'>" + ai13_recent_rows + "</table>" if ai13_recent_rows else ""}
          </div>
        </div>
        {"<div class='sub-title err' style='margin-top:10px'>Recent failures</div><table class='mini-table'>" + ai13_failed_rows + "</table>" if ai13_failed_rows else ""}
        '''

    # ── captcha solver card ────────────────────────────────────────────────────
    cap_by_service = mysql.get("cap_by_service", []) if mysql.get("ok") else []
    cap_today      = mysql.get("cap_today", {})      if mysql.get("ok") else {}
    cap_recent     = mysql.get("cap_recent", [])     if mysql.get("ok") else []
    cap_warnings   = mysql.get("cap_warnings", [])   if mysql.get("ok") else []

    # warning banner (20 consecutive failures)
    cap_warn_html = ""
    for msg, wts in cap_warnings:
        cap_warn_html += (
            f'<div class="no-log" style="background:#450a0a;border-left:4px solid #ef4444;'
            f'margin-bottom:8px;font-size:12px">'
            f'⚠ <b>CAPTCHA WARNING</b> [{e(str(wts)[:16])}]: {e(str(msg))}'
            f'<br><small style="color:#94a3b8">Run <code>python reset_stuck.py --reset-captcha</code> on HP to resume.</small>'
            f'</div>'
        )

    # per-service table
    svc_rows = ""
    for row in cap_by_service:
        svc, total, ok, fail, rate, avg_s = row
        rate_color = "#22c55e" if (rate or 0) >= 70 else "#f97316" if (rate or 0) >= 40 else "#ef4444"
        svc_rows += (
            f'<tr>'
            f'<td class="blue"><b>{e(svc)}</b></td>'
            f'<td>{int(total or 0):,}</td>'
            f'<td class="green">{int(ok or 0):,}</td>'
            f'<td class="err">{int(fail or 0):,}</td>'
            f'<td style="color:{rate_color}"><b>{rate or 0}%</b></td>'
            f'<td class="muted">{avg_s or "—"}s</td>'
            f'</tr>'
        )

    # recent attempts
    recent_cap_rows = ""
    for svc, ctype, url, success, ms, err in cap_recent:
        icon = "✓" if success else "✗"
        color = "#22c55e" if success else "#ef4444"
        recent_cap_rows += (
            f'<tr>'
            f'<td style="color:{color}"><b>{icon}</b></td>'
            f'<td class="blue" style="font-size:11px">{e(svc)}</td>'
            f'<td class="muted" style="font-size:11px">{e(ctype or "—")}</td>'
            f'<td style="font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'
            f'{e((url or "")[:60])}</td>'
            f'<td class="muted" style="font-size:11px">{int(ms or 0)/1000:.1f}s</td>'
            f'<td class="err" style="font-size:11px">{e((err or "")[:40])}</td>'
            f'</tr>'
        )

    today_total = cap_today.get("total", 0)
    today_ok    = cap_today.get("ok", 0)
    today_fail  = today_total - today_ok

    if cap_by_service or cap_warnings:
        cap_card = f'''
        <div class="card" style="margin-bottom:20px">
          <div class="card-title">
            <div class="dot" style="background:#f59e0b;box-shadow:0 0 6px #f59e0b"></div>
            🔓 Captcha Solver — AntiCaptcha / CapSolver / 2captcha
            <span style="margin-left:auto;font-size:12px;font-weight:400;color:#94a3b8">
              Today: <b class="{'green' if today_ok else 'muted'}">{today_ok} solved</b>
              / {today_total} total
            </span>
          </div>
          {cap_warn_html}
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
            <div>
              <div class="sub-title">Per-service stats (all time)</div>
              <table class="mini-table">
                <tr><th>Service</th><th>Total</th><th>OK</th><th>Fail</th><th>Rate</th><th>Avg</th></tr>
                {svc_rows if svc_rows else "<tr><td colspan='6' class='muted'>No attempts yet</td></tr>"}
              </table>
            </div>
            <div>
              <div class="sub-title">Recent attempts</div>
              <table class="mini-table">
                <tr><th></th><th>Service</th><th>Type</th><th>URL</th><th>Time</th><th>Error</th></tr>
                {recent_cap_rows if recent_cap_rows else "<tr><td colspan='6' class='muted'>None yet</td></tr>"}
              </table>
            </div>
          </div>
        </div>'''
    else:
        cap_card = f'''
        <div class="card" style="margin-bottom:20px;border:1px dashed #334155">
          <div class="card-title">
            <div class="dot" style="background:#475569"></div>
            🔓 Captcha Solver — AntiCaptcha / CapSolver / 2captcha
          </div>
          <p class="muted" style="font-size:12px">
            No captcha attempts yet. Solver activates automatically when step 10 hits a captcha/Cloudflare block.
            Services will rotate: AntiCaptcha → CapSolver → 2captcha (best success rate preferred after 15 attempts).
          </p>
        </div>'''


    # ── Post Planner card ──────────────────────────────────────────────────────
    _TYPE_EMOJI = {"Seasonal":"🌸","Trend":"🔥","Shopping":"🛍️","Lifestyle":"✨",
                   "Recipe":"🍴","DIY":"🔨","Other":"📌"}
    _TYPE_COLOR = {"Seasonal":"#22d3ee","Trend":"#f97316","Shopping":"#a78bfa",
                   "Lifestyle":"#4ade80","Recipe":"#fb923c","DIY":"#60a5fa","Other":"#94a3b8"}

    plan_total   = int(mysql.get("plan_total", 0) or 0)
    plan_pending = mysql.get("plan_pending", "?")

    plan_type_rows = "".join(
        f'<tr><td>{_TYPE_EMOJI.get(t,"📌")} <span style="color:{_TYPE_COLOR.get(t,"#94a3b8")}">' +
        f'{e(t or "Other")}</span></td><td class="blue"><b>{n:,}</b></td></tr>'
        for t, n in mysql.get("plan_by_type", [])
    ) or "<tr><td colspan=\'2\' class=\'muted\'>No pins classified yet</td></tr>"

    plan_recent_rows = "".join(
        f'<tr>' +
        f'<td class="blue" style="font-size:12px">{e(str(u or ""))}</td>' +
        f'<td style="font-size:12px;color:{_TYPE_COLOR.get(ct,"#94a3b8")}">{_TYPE_EMOJI.get(ct,"📌")} {e(ct or "")}</td>' +
        f'<td class="muted" style="font-size:11px">{e(cat or "")}</td>' +
        f'<td style="font-size:12px;color:{"#4ade80" if (sc or 0)>=8 else "#f97316" if (sc or 0)>=5 else "#94a3b8"}">{sc}/10</td>' +
        f'<td class="muted" style="font-size:11px">{e(str(days or "")[:30])}</td>' +
        f'<td class="muted" style="font-size:10px">{e(str(ts4 or "")[:16])}</td>' +
        (f'<td><a href="{e(str(pu or ""))}" target="_blank" style="color:#60a5fa;font-size:11px">🔗 open</a></td>' if pu else '<td class="muted">—</td>') +
        '</tr>'
        for u, ct, cat, sc, days, ts4, pu in mysql.get("plan_recent", [])
    ) or "<tr><td colspan=\'7\' class=\'muted\'>No classifications yet — run python 15_post_planner.py</td></tr>"

    from datetime import date as _date, timedelta as _td
    _today2 = _date.today()
    _DAYS15 = [_today2 + _td(days=i) for i in range(15)]
    _cal: dict = {d: [] for d in range(1, 16)}
    for days_str, ct, cat, desc, sc, pu in mysql.get("plan_schedule_rows", []):
        days_list = [int(x) for x in str(days_str or "").split(",") if x.strip().isdigit()]
        if not days_list:
            days_list = list(range(1, 16, 2))
        for d in days_list:
            if 1 <= d <= 15 and len(_cal[d]) < 4:
                _cal[d].append((ct, cat, sc, pu))

    cal_cells = ""
    for i, dt in enumerate(_DAYS15, 1):
        pins_here = _cal[i]
        is_today2 = (dt == _today2)
        day_label = dt.strftime("%a %d")
        badge_style = "background:#1e3a5f;border:1px solid #3b82f6;" if is_today2 else "background:#0f172a;border:1px solid #1e293b;"
        pin_lines = ""
        for ct, cat, sc, pu in pins_here:
            em  = _TYPE_EMOJI.get(ct, "📌")
            col = _TYPE_COLOR.get(ct, "#94a3b8")
            link_open = f'href="{e(pu)}" target="_blank" ' if pu else ""
            pin_lines += (f'<div style="font-size:10px;margin:1px 0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' +
                          f'{em} <a {link_open}style="color:{col};text-decoration:none">{e(cat or ct)}</a> ' +
                          f'<span style="color:#475569">{sc}/10</span></div>')
        if not pin_lines:
            pin_lines = '<div style="font-size:10px;color:#1e293b">·</div>'
        cal_cells += f'<div style="{badge_style}border-radius:6px;padding:6px 8px;min-height:70px">' + \
                     f'<div style="font-size:11px;font-weight:700;color:{"#60a5fa" if is_today2 else "#475569"};margin-bottom:4px">' + \
                     f'{"★ " if is_today2 else ""}{day_label}</div>' + pin_lines + "</div>"

    if mysql.get("ok"):
        plan_card = f'''
        <div class="card" style="margin-bottom:20px">
          <div class="card-title">
            <div class="dot" style="background:#22d3ee;box-shadow:0 0 6px #22d3ee"></div>
            📅 Post Planner — Distribute &amp; Schedule (Step 15)
            <span style="margin-left:auto;font-size:12px;font-weight:400;color:#94a3b8">
              <b class="blue" id="plan-total">{plan_total:,}</b> classified &nbsp;·&nbsp;
              <span class="muted"><span id="plan-pending">{plan_pending}</span> pending</span>
            </span>
          </div>
          {'<p class="muted" style="font-size:12px">No pins classified yet — run <code style="color:#a78bfa">python 15_post_planner.py</code> to start.</p>' if not plan_total else ''}
          <div style="display:grid;grid-template-columns:200px 1fr;gap:16px;margin-bottom:14px">
            <div>
              <div class="sub-title">Content types</div>
              <table class="mini-table">{plan_type_rows}</table>
            </div>
            <div>
              <div class="sub-title">Auto-preview ({_today2.strftime("%b %d")} → {(_today2+_td(days=14)).strftime("%b %d")})</div>
              <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:6px">{cal_cells}</div>
            </div>
          </div>

          <!-- ── Distribute form ── -->
          <div style="background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:14px 18px;margin-bottom:14px">
            <div class="sub-title" style="margin-bottom:10px">📤 Generate posting schedule</div>
            <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
              <label style="font-size:12px;color:#94a3b8">Total posts:
                <input id="sched-total" type="number" value="45" min="1" max="500"
                  style="width:70px;margin-left:6px;background:#1e293b;border:1px solid #334155;
                         border-radius:4px;color:#e2e8f0;padding:4px 8px;font-size:12px">
              </label>
              <label style="font-size:12px;color:#94a3b8">Over:
                <input id="sched-days" type="number" value="15" min="1" max="60"
                  style="width:60px;margin-left:6px;background:#1e293b;border:1px solid #334155;
                         border-radius:4px;color:#e2e8f0;padding:4px 8px;font-size:12px">
                <span style="margin-left:4px;color:#64748b">days</span>
              </label>
              <button onclick="generateSchedule()"
                style="background:#22d3ee;color:#0f172a;font-weight:700;border:none;
                       border-radius:6px;padding:6px 18px;cursor:pointer;font-size:13px">
                ▶ Distribute
              </button>
              <button onclick="copySchedule()"
                style="background:#1e293b;color:#94a3b8;border:1px solid #334155;
                       border-radius:6px;padding:6px 14px;cursor:pointer;font-size:12px">
                📋 Copy
              </button>
              <span id="sched-status" style="font-size:12px;color:#64748b"></span>
            </div>
          </div>

          <!-- ── Schedule output ── -->
          <div id="sched-out" style="display:none;margin-bottom:14px"></div>

          <!-- ── Browse / filter pins ── -->
          <div style="background:#0f172a;border:1px solid #1e293b;border-radius:10px;padding:14px 18px;margin-bottom:14px">
            <div class="sub-title" style="margin-bottom:10px">🔍 Browse classified pins</div>
            <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px">
              <label style="font-size:12px;color:#94a3b8" title="Filter by original pin creation date (pins.created_at)">Pin created from:
                <input id="pins-from" type="date" style="margin-left:4px;background:#1e293b;border:1px solid #334155;border-radius:4px;color:#e2e8f0;padding:3px 7px;font-size:12px">
              </label>
              <label style="font-size:12px;color:#94a3b8" title="Filter by original pin creation date (pins.created_at)">to:
                <input id="pins-to" type="date" style="margin-left:4px;background:#1e293b;border:1px solid #334155;border-radius:4px;color:#e2e8f0;padding:3px 7px;font-size:12px">
              </label>
              <label style="font-size:12px;color:#94a3b8">Type:
                <select id="pins-type" style="margin-left:4px;background:#1e293b;border:1px solid #334155;border-radius:4px;color:#e2e8f0;padding:3px 7px;font-size:12px">
                  <option value="">All</option>
                  <option>Seasonal</option><option>Trend</option><option>Shopping</option>
                  <option>Lifestyle</option><option>Recipe</option><option>DIY</option><option>Other</option>
                </select>
              </label>
              <label style="font-size:12px;color:#94a3b8">Min score:
                <input id="pins-minscore" type="number" value="0" min="0" max="10"
                  style="width:52px;margin-left:4px;background:#1e293b;border:1px solid #334155;border-radius:4px;color:#e2e8f0;padding:3px 7px;font-size:12px">
              </label>
              <label style="font-size:12px;color:#94a3b8">Status:
                <select id="pins-status-filter" style="margin-left:4px;background:#1e293b;border:1px solid #334155;border-radius:4px;color:#e2e8f0;padding:3px 7px;font-size:12px">
                  <option value="all">All created</option>
                  <option value="with_link">With link</option>
                  <option value="classified">Classified</option>
                  <option value="unclassified">Unclassified ready</option>
                  <option value="ready">All ready</option>
                  <option value="not_downloaded">Not downloaded</option>
                  <option value="failed">Download failed</option>
                </select>
              </label>
              <label style="font-size:12px;color:#94a3b8">Link:
                <select id="pins-link-filter" style="margin-left:4px;background:#1e293b;border:1px solid #334155;border-radius:4px;color:#e2e8f0;padding:3px 7px;font-size:12px">
                  <option value="">All</option>
                  <option value="with">Has link</option>
                  <option value="without">No link</option>
                </select>
              </label>
              <label style="font-size:12px;color:#94a3b8">Site:
                <select id="pins-site-filter" style="margin-left:4px;background:#1e293b;border:1px solid #334155;border-radius:4px;color:#e2e8f0;padding:3px 7px;font-size:12px">
                  <option value="">All</option>
                  <option value="Blog">Blog</option>
                  <option value="Store">Store</option>
                  <option value="Link-in-Bio">Link-in-Bio</option>
                  <option value="Social Media">Social Media</option>
                  <option value="General Website">General Website</option>
                  <option value="Portfolio">Portfolio</option>
                  <option value="News/Media">News/Media</option>
                  <option value="SaaS/Tool">SaaS/Tool</option>
                  <option value="blank">Not set</option>
                </select>
              </label>
              <label style="font-size:12px;color:#94a3b8">Sort:
                <select id="pins-sort" style="margin-left:4px;background:#1e293b;border:1px solid #334155;border-radius:4px;color:#e2e8f0;padding:3px 7px;font-size:12px">
                  <option value="score">Score ↓</option>
                  <option value="followers">Followers ↓</option>
                  <option value="repins">Repins ↓</option>
                  <option value="date">Date ↓</option>
                  <option value="type">Type</option>
                </select>
              </label>
              <button onclick="loadPins(1)"
                style="background:#22d3ee;color:#0f172a;font-weight:700;border:none;border-radius:6px;padding:6px 16px;cursor:pointer;font-size:13px">
                🔍 Load
              </button>
              <button onclick="scanSelectedPins()"
                style="background:#1e293b;color:#e2e8f0;font-weight:700;border:1px solid #334155;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:13px">
                Scan selected
              </button>
            </div>
            <div id="pins-status" style="font-size:12px;color:#64748b;margin-bottom:6px"></div>
            <div id="scan-log-panel" style="display:none;background:#020617;border:1px solid #1e293b;border-radius:8px;margin-bottom:10px;padding:10px 12px">
              <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:6px">
                <div id="scan-log-title" style="font-size:12px;font-weight:700;color:#e2e8f0">Step 15 scan</div>
                <div id="scan-log-state" style="font-size:11px;color:#94a3b8"></div>
              </div>
              <pre id="scan-log-lines" style="margin:0;max-height:180px;overflow:auto;white-space:pre-wrap;font-size:11px;line-height:1.4;color:#94a3b8"></pre>
            </div>
            <div id="pins-table" style="max-height:480px;overflow-y:auto;border:1px solid #1e293b;border-radius:8px;display:none"></div>
            <div id="pins-pager" style="display:flex;gap:10px;align-items:center;margin-top:8px;display:none">
              <button id="pins-prev" onclick="loadPins(_pinsPage-1)"
                style="background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:12px">◀ Prev</button>
              <span id="pins-pageinfo" style="font-size:12px;color:#64748b"></span>
              <button id="pins-next" onclick="loadPins(_pinsPage+1)"
                style="background:#1e293b;color:#94a3b8;border:1px solid #334155;border-radius:4px;padding:4px 12px;cursor:pointer;font-size:12px">Next ▶</button>
            </div>
          </div>

        </div>

        <script>
        const TYPE_EMOJI = {{Seasonal:"🌸",Trend:"🔥",Shopping:"🛍️",Lifestyle:"✨",Recipe:"🍴",DIY:"🔨",Other:"📌"}};
        const TYPE_COLOR = {{Seasonal:"#22d3ee",Trend:"#f97316",Shopping:"#a78bfa",Lifestyle:"#4ade80",Recipe:"#fb923c",DIY:"#60a5fa",Other:"#94a3b8"}};
        let _lastSchedule = null;
        let _pinsPage = 1, _pinsTotal = 0, _pinsPerPage = 50;
        let _scanPollTimer = null;

        function _fmt(n) {{
          if (!n) return "—";
          if (n >= 1000000) return (n/1000000).toFixed(1)+"M";
          if (n >= 1000) return (n/1000).toFixed(1)+"k";
          return n;
        }}

        // ── persist filter state in URL hash so meta-refresh doesn't wipe it ──
        function _saveFilters(page) {{
          const f = {{
            from:     document.getElementById("pins-from").value,
            to:       document.getElementById("pins-to").value,
            type:     document.getElementById("pins-type").value,
            status:   document.getElementById("pins-status-filter").value,
            link:     document.getElementById("pins-link-filter").value,
            site:     document.getElementById("pins-site-filter").value,
            sort:     document.getElementById("pins-sort").value,
            minscore: document.getElementById("pins-minscore").value,
            page:     page || _pinsPage
          }};
          history.replaceState(null, "", "#pins=" + encodeURIComponent(JSON.stringify(f)));
        }}

        function _restoreFilters() {{
          try {{
            const hash = location.hash;
            if (!hash.startsWith("#pins=")) return null;
            const f = JSON.parse(decodeURIComponent(hash.slice(6)));
            if (f.from)     document.getElementById("pins-from").value     = f.from;
            if (f.to)       document.getElementById("pins-to").value       = f.to;
            if (f.type)     document.getElementById("pins-type").value     = f.type;
            if (f.status)   document.getElementById("pins-status-filter").value = f.status;
            if (f.link)     document.getElementById("pins-link-filter").value = f.link;
            if (f.site)     document.getElementById("pins-site-filter").value = f.site;
            if (f.sort)     document.getElementById("pins-sort").value     = f.sort;
            if (f.minscore) document.getElementById("pins-minscore").value = f.minscore;
            return f.page || 1;
          }} catch(e) {{ return null; }}
        }}

        document.addEventListener("DOMContentLoaded", function() {{
          const page = _restoreFilters();
          if (page !== null) loadPins(page);
        }});

        async function loadPins(page) {{
          page = Math.max(1, page || 1);
          _pinsPage = page;
          const from  = document.getElementById("pins-from").value;
          const to    = document.getElementById("pins-to").value;
          const type  = document.getElementById("pins-type").value;
          const filterStatus = document.getElementById("pins-status-filter").value;
          const linkFilter = document.getElementById("pins-link-filter").value;
          const siteFilter = document.getElementById("pins-site-filter").value;
          const sort  = document.getElementById("pins-sort").value;
          const minscore = document.getElementById("pins-minscore").value || 0;
          const status = document.getElementById("pins-status");
          status.textContent = "⏳ Loading…";
          _saveFilters(page);
          let url = `/api/pins?page=${{page}}&sort=${{sort}}&minscore=${{minscore}}&status=${{filterStatus}}`;
          if (from) url += `&from=${{from}}`;
          if (to)   url += `&to=${{to}}`;
          if (type) url += `&type=${{encodeURIComponent(type)}}`;
          if (linkFilter) url += `&link=${{encodeURIComponent(linkFilter)}}`;
          if (siteFilter) url += `&site=${{encodeURIComponent(siteFilter)}}`;
          try {{
            const r = await fetch(url);
            const data = await r.json();
            if (!data.ok) {{ status.textContent = "⚠ " + (data.error || "error"); return; }}
            _pinsTotal = data.total;
            const pages = Math.ceil(data.total / _pinsPerPage) || 1;
            if (data.total === 0) {{
              status.textContent = "0 pins found — run python 15_post_planner.py --source mysql to classify pins";
              document.getElementById("pins-table").style.display = "none";
              document.getElementById("pins-pager").style.display = "none";
              return;
            }}
            status.textContent = `${{data.total.toLocaleString()}} pins — showing ${{(page-1)*_pinsPerPage+1}}–${{Math.min(page*_pinsPerPage, data.total)}} · "Pin created" = original pin date`;
            renderPinsTable(data.pins);
            // pager
            const pager = document.getElementById("pins-pager");
            pager.style.display = data.total > _pinsPerPage ? "flex" : "none";
            document.getElementById("pins-prev").disabled = page <= 1;
            document.getElementById("pins-next").disabled = page >= pages;
            document.getElementById("pins-pageinfo").textContent = `Page ${{page}} / ${{pages}}`;
          }} catch(ex) {{
            status.textContent = "⚠ " + ex;
          }}
        }}

        async function scanSelectedPins() {{
          const from = document.getElementById("pins-from").value;
          const to = document.getElementById("pins-to").value;
          const linkFilter = document.getElementById("pins-link-filter").value;
          const siteFilter = document.getElementById("pins-site-filter").value;
          const status = document.getElementById("pins-status");
          if (!from || !to) {{
            status.textContent = "Choose Pin created from/to before scanning selected pins.";
            return;
          }}
          status.textContent = "Starting Step 15 scan for this date range...";
          try {{
            let url = `/api/scan-selected?from=${{from}}&to=${{to}}&limit=500&workers=10`;
            if (linkFilter) url += `&link=${{encodeURIComponent(linkFilter)}}`;
            if (siteFilter) url += `&site=${{encodeURIComponent(siteFilter)}}`;
            const r = await fetch(url);
            const data = await r.json();
            if (!data.ok) {{
              status.textContent = "Scan failed: " + (data.error || "unknown error");
              return;
            }}
            status.textContent = `Step 15 scan started (pid ${{data.pid}}). It will classify ready pins only.`;
            startScanPolling(data.job_id);
            setTimeout(() => loadPins(_pinsPage), 5000);
          }} catch(ex) {{
            status.textContent = "Scan failed: " + ex;
          }}
        }}

        function startScanPolling(jobId, onDone=null) {{
          const panel = document.getElementById("scan-log-panel");
          const title = document.getElementById("scan-log-title");
          const state = document.getElementById("scan-log-state");
          const lines = document.getElementById("scan-log-lines");
          panel.style.display = "";
          title.textContent = `Step 15 scan job ${{jobId}}`;
          lines.textContent = "Waiting for logs...";
          if (_scanPollTimer) clearInterval(_scanPollTimer);
          async function poll() {{
            try {{
              const r = await fetch(`/api/scan-status?job_id=${{encodeURIComponent(jobId)}}`);
              const data = await r.json();
              if (!data.ok) {{
                state.textContent = data.error || "status error";
                return;
              }}
              state.textContent = data.running ? `running · pid ${{data.pid}}` : `finished · exit ${{data.returncode}}`;
              lines.textContent = data.log || "(no log output yet)";
              lines.scrollTop = lines.scrollHeight;
              if (!data.running) {{
                clearInterval(_scanPollTimer);
                _scanPollTimer = null;
                loadPins(_pinsPage);
                if (onDone) onDone(data);
              }}
            }} catch(ex) {{
              state.textContent = "status error: " + ex;
            }}
          }}
          poll();
          _scanPollTimer = setInterval(poll, 3000);
        }}

        async function scanOnePin(pinId, force=false) {{
          const status = document.getElementById("pins-status");
          status.textContent = `Starting Step 15 scan for pin ${{pinId}}...`;
          try {{
            const r = await fetch(`/api/scan-pin?pin_id=${{encodeURIComponent(pinId)}}${{force ? "&force=1" : ""}}`);
            const data = await r.json();
            if (!data.ok) {{
              status.textContent = "Pin scan failed: " + (data.error || "unknown error");
              return;
            }}
            status.textContent = `Step 15 pin scan started (pid ${{data.pid}}).`;
            startScanPolling(data.job_id, () => {{
              const filterStatus = document.getElementById("pins-status-filter").value;
              if (filterStatus === "unclassified") {{
                status.textContent = `Pin ${{pinId}} was classified. Current filter shows remaining unclassified-ready pins only.`;
              }} else {{
                status.textContent = `Pin ${{pinId}} scan finished.`;
              }}
            }});
          }} catch(ex) {{
            status.textContent = "Pin scan failed: " + ex;
          }}
        }}

        async function downloadOnePin(pinId, thenAi=false) {{
          const status = document.getElementById("pins-status");
          status.textContent = `Starting Step 14 download for pin ${{pinId}}...`;
          try {{
            const r = await fetch(`/api/download-pin?pin_id=${{encodeURIComponent(pinId)}}`);
            const data = await r.json();
            if (!data.ok) {{
              status.textContent = "Step 14 failed to start: " + (data.error || "unknown error");
              return;
            }}
            status.textContent = `Step 14 download started (pid ${{data.pid}}).`;
            startScanPolling(data.job_id, thenAi ? (() => {{
              status.textContent = `Step 14 finished. Starting Step 15 AI for pin ${{pinId}}...`;
              setTimeout(() => scanOnePin(pinId, true), 800);
            }}) : null);
          }} catch(ex) {{
            status.textContent = "Step 14 failed to start: " + ex;
          }}
        }}

        function renderPinsTable(pins) {{
          const wrap = document.getElementById("pins-table");
          wrap.style.display = "";
          if (!pins.length) {{ wrap.innerHTML = '<p class="muted" style="padding:12px">No pins match.</p>'; return; }}
          const cellTitle = (v) => String(v || "").replaceAll("&", "&amp;").replaceAll('"', "&quot;").replaceAll("<", "&lt;").replaceAll(">", "&gt;");
          const shortText = (v, n=90) => {{
            v = String(v || "");
            return v.length > n ? v.slice(0, n - 1) + "…" : v;
          }};
          const detailValue = (label, value) => `
            <div style="min-width:220px">
              <div style="font-size:10px;text-transform:uppercase;color:#64748b;margin-bottom:2px">${{label}}</div>
              <div style="font-size:12px;color:#e2e8f0;white-space:pre-wrap;word-break:break-word">${{cellTitle(value || "—")}}</div>
            </div>`;
          const prettyJson = (value) => {{
            if (!value) return "—";
            try {{ return JSON.stringify(JSON.parse(value), null, 2); }}
            catch(e) {{ return value; }}
          }};
          const detailHtml = (p) => `
            <div style="padding:12px;background:#020617;border-top:1px solid #1e293b">
              <div style="display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:10px">
                <div style="font-size:12px;font-weight:800;color:#e2e8f0">AI generated data for pin ${{p.pin_id}}</div>
                <div style="font-size:11px;color:#64748b">generated_by_ia_scan fields</div>
              </div>
              <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px 14px;margin-bottom:12px">
                ${{detailValue("content_type_generated_by_ia_scan", p.type)}}
                ${{detailValue("category_generated_by_ia_scan", p.category)}}
                ${{detailValue("post_score_generated_by_ia_scan", p.score ? p.score + "/10" : "")}}
                ${{detailValue("seasonal_context_generated_by_ia_scan", p.seasonal)}}
                ${{detailValue("best_posting_days_generated_by_ia_scan", p.days)}}
                ${{detailValue("posting_window_generated_by_ia_scan", p.ia_window)}}
                ${{detailValue("keywords_generated_by_ia_scan", p.ia_keywords)}}
                ${{detailValue("hook_generated_by_ia_scan", p.ia_hook)}}
                ${{detailValue("caption_generated_by_ia_scan", p.ia_caption)}}
                ${{detailValue("target_audience_generated_by_ia_scan", p.ia_audience)}}
                ${{detailValue("monetization_angle_generated_by_ia_scan", p.ia_monetization)}}
                ${{detailValue("description_generated_by_ia_scan", p.desc)}}
              </div>
              <div style="font-size:10px;text-transform:uppercase;color:#64748b;margin-bottom:4px">content_json_generated_by_ia_scan</div>
              <pre style="margin:0;max-height:320px;overflow:auto;background:#0f172a;border:1px solid #1e293b;border-radius:6px;padding:10px;color:#cbd5e1;font-size:11px;line-height:1.45;white-space:pre-wrap;word-break:break-word">${{cellTitle(prettyJson(p.ia_json))}}</pre>
            </div>`;
          let html = `<table class="mini-table" style="width:100%">
            <tr>
              <th>Pin ID</th><th>State</th><th>Type</th><th>Category</th><th>Score</th><th>Seasonal</th>
              <th>Pinner</th><th>Site</th><th>Followers</th><th>Reach</th><th>Repins</th>
              <th>Pin created</th><th>AI scanned</th><th>Best days</th><th>Caption</th>
              <th>Action</th><th>Pin</th><th>Blog</th>
            </tr>`;
          for (const p of pins) {{
            const em  = TYPE_EMOJI[p.type] || "📌";
            const col = TYPE_COLOR[p.type]  || "#94a3b8";
            const sc  = p.score >= 8 ? "#4ade80" : p.score >= 5 ? "#f97316" : "#94a3b8";
            let scanState = "Need Step 14";
            let stateColor = "#64748b";
            const readyForAi = !!p.ready_for_ai;
            if (p.classified) {{ scanState = "AI scanned"; stateColor = "#4ade80"; }}
            else if (readyForAi) {{ scanState = "Ready for AI"; stateColor = "#22d3ee"; }}
            else if (p.download_status === "Done") {{ scanState = "Step 14 empty"; stateColor = "#f97316"; }}
            else if ((p.download_status || "").startsWith("Failed")) {{ scanState = p.download_status; stateColor = "#f97316"; }}
            else if (!p.blog_url) {{ scanState = "No blog link"; }}
            const canScan = !p.classified && readyForAi;
            let actionHtml = "";
            actionHtml = `<div style="display:flex;gap:4px;align-items:center;white-space:nowrap">`;
            if (p.blog_url) {{
              const step14Label = (p.download_status || "").startsWith("Failed") ? "Retry 14" : (p.download_status === "Done" ? "Re-14" : "Step 14");
              const step14AiLabel = p.download_status === "Done" ? "Re-14+AI" : "14+AI";
              actionHtml += `<button onclick="downloadOnePin('${{p.pin_id}}')" title="Download/retry this pin destination with Step 14" style="background:#1e293b;color:#e2e8f0;border:1px solid #334155;border-radius:4px;padding:3px 7px;font-size:11px;cursor:pointer">${{step14Label}}</button>`;
              actionHtml += `<button onclick="downloadOnePin('${{p.pin_id}}', true)" title="Run Step 14, then automatically run Step 15 AI" style="background:#172554;color:#93c5fd;border:1px solid #1d4ed8;border-radius:4px;padding:3px 7px;font-size:11px;cursor:pointer">${{step14AiLabel}}</button>`;
            }} else {{
              actionHtml += `<button disabled title="No outbound blog link to download" style="background:#111827;color:#64748b;border:1px solid #1f2937;border-radius:4px;padding:3px 7px;font-size:11px">No link</button>`;
            }}
            if (readyForAi && !p.classified) {{
              actionHtml += `<button onclick="scanOnePin('${{p.pin_id}}')" title="Classify this downloaded pin with Step 15 AI" style="background:#0f172a;color:#22d3ee;border:1px solid #164e63;border-radius:4px;padding:3px 7px;font-size:11px;cursor:pointer">AI</button>`;
              actionHtml += `<button onclick="scanOnePin('${{p.pin_id}}', true)" title="Force Step 15 using prior similar JSON examples when available" style="background:#312e81;color:#c4b5fd;border:1px solid #6d28d9;border-radius:4px;padding:3px 7px;font-size:11px;cursor:pointer">Smart AI</button>`;
            }} else if (readyForAi && p.classified) {{
              actionHtml += `<button onclick="scanOnePin('${{p.pin_id}}', true)" title="Force re-run Step 15 AI for this pin" style="background:#052e16;color:#4ade80;border:1px solid #166534;border-radius:4px;padding:3px 7px;font-size:11px;cursor:pointer">Re-AI</button>`;
              actionHtml += `<button onclick="scanOnePin('${{p.pin_id}}', true)" title="Force Step 15 using prior similar JSON examples when available" style="background:#312e81;color:#c4b5fd;border:1px solid #6d28d9;border-radius:4px;padding:3px 7px;font-size:11px;cursor:pointer">Smart AI</button>`;
            }} else {{
              actionHtml += `<button disabled title="Step 15 needs Step 14 downloaded content first" style="background:#111827;color:#64748b;border:1px solid #1f2937;border-radius:4px;padding:3px 7px;font-size:11px">AI</button>`;
              actionHtml += `<button disabled title="Smart AI needs Step 14 downloaded content first" style="background:#111827;color:#64748b;border:1px solid #1f2937;border-radius:4px;padding:3px 7px;font-size:11px">Smart AI</button>`;
            }}
            actionHtml += `<button onclick="togglePinDetails('${{p.pin_id}}')" title="Expand AI generated data" style="background:#312e81;color:#ddd6fe;border:1px solid #6d28d9;border-radius:4px;padding:3px 7px;font-size:11px;cursor:pointer">Expand</button>`;
            actionHtml += `</div>`;
            html += `<tr>
              <td style="font-size:11px;color:#93c5fd;white-space:nowrap" title="${{p.pin_id}}">${{p.pin_id}}</td>
              <td style="font-size:11px;color:${{stateColor}};white-space:nowrap">${{scanState}}</td>
              <td style="color:${{col}};white-space:nowrap">${{em}} ${{p.type}}</td>
              <td style="font-size:11px">${{p.category||"—"}}</td>
              <td style="color:${{sc}};font-weight:700">${{p.classified ? p.score + "/10" : (p.download_status || "not downloaded")}}</td>
              <td style="font-size:11px;color:#94a3b8">${{p.seasonal||"—"}}</td>
              <td style="font-size:11px"><a href="https://pinterest.com/${{p.pinner}}/" target="_blank" style="color:#60a5fa">${{p.pinner}}</a></td>
              <td style="font-size:11px;color:#94a3b8;white-space:nowrap">${{p.site_type||"—"}}</td>
              <td style="font-size:11px;color:#4ade80">${{_fmt(p.followers)}}</td>
              <td style="font-size:11px;color:#22d3ee">${{_fmt(p.reach)}}</td>
              <td style="font-size:11px">${{_fmt(p.repins)}}</td>
              <td style="font-size:11px;color:#64748b;white-space:nowrap">${{p.created_at||"—"}}</td>
              <td style="font-size:11px;color:#64748b;white-space:nowrap">${{p.scanned||"—"}}</td>
              <td style="font-size:11px;color:#64748b">${{p.days||"—"}}</td>
              <td style="font-size:11px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{p.desc||""}}">${{p.desc||"—"}}</td>
              <td>${{actionHtml}}</td>
              <td>${{p.pin_url ? `<a href="${{p.pin_url}}" target="_blank" style="color:#a78bfa;font-size:11px">🔗 pin</a>` : "—"}}</td>
              <td>${{p.blog_url ? `<a href="${{p.blog_url}}" target="_blank" style="color:#f97316;font-size:11px">🌐 blog</a>` : "—"}}</td>
            </tr>
            <tr id="pin-detail-${{p.pin_id}}" style="display:none">
              <td colspan="18" style="padding:0">${{detailHtml(p)}}</td>
            </tr>`;
          }}
          html += `</table>`;
          wrap.innerHTML = html;
        }}

        function togglePinDetails(pinId) {{
          const row = document.getElementById(`pin-detail-${{pinId}}`);
          if (!row) return;
          row.style.display = row.style.display === "none" ? "table-row" : "none";
        }}

        // ── Schedule ──────────────────────────────────────────────────────────
        async function generateSchedule() {{
          const total = document.getElementById("sched-total").value;
          const days  = document.getElementById("sched-days").value;
          const status = document.getElementById("sched-status");
          status.textContent = "⏳ Loading…";
          try {{
            const r = await fetch(`/api/schedule?total=${{total}}&days=${{days}}`);
            const data = await r.json();
            if (!data.ok) {{ status.textContent = "⚠ " + data.error; return; }}
            _lastSchedule = data;
            status.textContent = `✓ ${{data.total}} posts across ${{data.days}} days`;
            renderSchedule(data);
          }} catch(ex) {{
            status.textContent = "⚠ " + ex;
          }}
        }}

        function renderSchedule(data) {{
          const out = document.getElementById("sched-out");
          out.style.display = "";
          let html = `<div class="sub-title" style="margin-bottom:8px">Generated schedule — ${{data.total}} posts over ${{data.days}} days</div>`;
          html += `<div style="max-height:420px;overflow-y:auto;border:1px solid #1e293b;border-radius:8px">`;
          html += `<table class="mini-table" style="width:100%">`;
          html += `<tr><th>Day</th><th>Date</th><th>Type</th><th>Category</th><th>Score</th><th>Pinner</th><th>Caption</th><th>Pin</th></tr>`;
          for (const day of data.schedule) {{
            for (let i=0; i < day.pins.length; i++) {{
              const p = day.pins[i];
              const em = TYPE_EMOJI[p.type] || "📌";
              const col = TYPE_COLOR[p.type] || "#94a3b8";
              html += `<tr>`;
              if (i === 0) html += `<td rowspan="${{day.pins.length}}" style="font-weight:700;color:#60a5fa;white-space:nowrap;vertical-align:top">#${{day.day}}</td><td rowspan="${{day.pins.length}}" style="font-size:11px;color:#94a3b8;white-space:nowrap;vertical-align:top">${{day.date}}</td>`;
              html += `<td style="color:${{col}}">${{em}} ${{p.type}}</td>`;
              html += `<td style="font-size:11px">${{p.category}}</td>`;
              html += `<td style="color:${{p.score>=8?"#4ade80":p.score>=5?"#f97316":"#94a3b8"}}">${{p.score}}/10</td>`;
              html += `<td style="font-size:11px"><a href="https://pinterest.com/${{p.pinner}}/" target="_blank" style="color:#60a5fa">${{p.pinner}}</a></td>`;
              html += `<td style="font-size:11px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{p.desc}}">${{p.desc}}</td>`;
              html += `<td>${{p.url ? `<a href="${{p.url}}" target="_blank" style="color:#a78bfa;font-size:11px">🔗 open</a>` : "—"}}</td>`;
              html += `</tr>`;
            }}
          }}
          html += `</table></div>`;
          out.innerHTML = html;
        }}

        function copySchedule() {{
          if (!_lastSchedule) {{ alert("Generate a schedule first."); return; }}
          let txt = "";
          for (const day of _lastSchedule.schedule) {{
            txt += `\\n=== Day ${{day.day}} — ${{day.date}} ===\\n`;
            for (const p of day.pins) {{
              txt += `[${{p.type}}] ${{p.category}} (${{p.score}}/10) — ${{p.pinner}}\\n`;
              if (p.desc) txt += `  Caption: ${{p.desc}}\\n`;
              if (p.url)  txt += `  Pin: ${{p.url}}\\n`;
            }}
          }}
          navigator.clipboard.writeText(txt.trim()).then(() => {{
            document.getElementById("sched-status").textContent = "✓ Copied to clipboard!";
          }});
        }}
        </script>'''

    # ── log panels ─────────────────────────────────────────────────────────────
    def log_panel(key, title, dot_color):
        rows = bot_logs.get(key)  # list of (ts, level, message) or None
        badges = ""
        if rows is not None:
            errs, warns = log_stats_from_rows(rows)
            # find age from last row
            last_ts = rows[-1][0] if rows else None
            age_str = ""
            if last_ts:
                try:
                    diff = int(time.time() - last_ts.timestamp())
                    age_str = f"{diff}s ago" if diff < 120 else f"{diff//60}m ago"
                except Exception:
                    age_str = str(last_ts)[:16]
            badges = f'<span class="pill pill-info">{len(rows)} lines</span> '
            if age_str:
                badges += f'<span class="pill pill-info">last: {age_str}</span> '
            if errs:
                badges += f'<span class="pill pill-err">⚠ {errs} errors</span> '
            if warns:
                badges += f'<span class="pill pill-warn">{warns} warnings</span>'
        return f'''
        <div class="card" style="margin-bottom:20px">
          <div class="card-title">
            <div class="dot" style="background:{dot_color};box-shadow:0 0 6px {dot_color}"></div>
            {title} — live log (MySQL)
            <span style="margin-left:auto;font-weight:400;font-size:12px">{badges}</span>
          </div>
          {render_log(rows, key)}
        </div>'''

    log_panels = (
        log_panel("magic_scroll", "Bot 1 · magic_scroll",           "#22c55e") +
        log_panel("bot10",        "Bot 2 · Step 10 (classifier)",   "#f97316") +
        log_panel("bot13",        "Bot 3 · Step 13 (AI scanner)",   "#a78bfa") +
        log_panel("bot14",        "Bot 4 · Step 14 (link downloader)", "#3b82f6")
    )

    # ── assemble ───────────────────────────────────────────────────────────────
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <!-- no meta-refresh: live updates via /api/live JS polling -->
  <title>Pinterest Scan — Dashboard</title>
  <style>
    *, *::before, *::after {{ box-sizing:border-box; margin:0; padding:0 }}
    body {{ background:#0f172a; color:#e2e8f0; font-family:'Segoe UI',system-ui,sans-serif; padding:18px; }}
    h1 {{ font-size:20px; font-weight:800; color:#f8fafc; margin-bottom:3px }}
    .subtitle {{ color:#475569; font-size:12px; margin-bottom:16px }}

    /* status bar */
    .status-bar {{ display:flex; gap:20px; flex-wrap:wrap; background:#1e293b; border-radius:12px;
                   padding:14px 18px; margin-bottom:18px; border:1px solid #334155 }}
    .sb-item {{ display:flex; align-items:center; gap:10px }}
    .sb-name {{ font-size:13px; color:#94a3b8; font-weight:600 }}

    /* pills */
    .pill {{ display:inline-block; padding:2px 10px; border-radius:20px; font-size:11px; font-weight:700 }}
    .pill-run  {{ background:#14532d; color:#4ade80; border:1px solid #166534 }}
    .pill-stop {{ background:#1e293b; color:#64748b; border:1px solid #334155 }}
    .pill-idle {{ background:#1c1917; color:#fb923c; border:1px solid #7c2d12 }}
    .pill-err  {{ background:#450a0a; color:#f87171; border:1px solid #7f1d1d }}
    .pill-warn {{ background:#422006; color:#fb923c; border:1px solid #7c2d12 }}
    .pill-info {{ background:#0f172a; color:#64748b; border:1px solid #1e293b }}
    .pill-auto {{ background:#1e293b; color:#a78bfa; border:1px solid #334155 }}

    /* refresh bar */
    .rbar {{ background:#1e293b; border-radius:8px; padding:8px 14px; display:flex;
             justify-content:space-between; align-items:center; font-size:12px;
             color:#475569; margin-bottom:18px; border:1px solid #1e293b }}

    /* cards */
    .card {{ background:#1e293b; border-radius:14px; padding:18px; border:1px solid #334155; margin-bottom:18px }}
    .card-title {{ font-size:13px; font-weight:700; text-transform:uppercase; letter-spacing:.07em;
                   color:#94a3b8; margin-bottom:14px; display:flex; align-items:center; gap:8px }}
    .dot {{ width:9px; height:9px; border-radius:50%; flex-shrink:0 }}

    /* stat row */
    .stat-row {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:12px }}
    .stat-box {{ background:#0f172a; border-radius:10px; padding:10px 14px; text-align:center; flex:1; min-width:80px }}
    .stat-n {{ font-size:24px; font-weight:800; line-height:1 }}
    .stat-l {{ font-size:11px; color:#64748b; margin-top:3px; text-transform:uppercase; letter-spacing:.04em }}

    /* progress bar */
    .bar-wrap {{ background:#0f172a; border-radius:6px; height:12px; overflow:hidden; margin:4px 0 }}
    .bar-fill  {{ height:100%; transition:width .4s }}

    /* tables */
    .mini-table {{ width:100%; border-collapse:collapse; font-size:12px }}
    .mini-table td {{ padding:3px 6px }}
    .mini-table tr:hover {{ background:rgba(255,255,255,.03) }}

    /* log box */
    .log-box {{ background:#020617; border-radius:8px; padding:10px 12px; font-family:monospace;
                font-size:11.5px; line-height:1.55; max-height:420px; overflow-y:auto;
                border:1px solid #0f172a; white-space:pre-wrap; word-break:break-all }}
    .ll-err  {{ color:#f87171 }}
    .ll-warn {{ color:#fb923c }}
    .ll-ok   {{ color:#4ade80 }}
    .ll-info {{ color:#60a5fa }}
    .ll-dim  {{ color:#475569 }}
    .ll-ts   {{ color:#334155; font-size:10px; user-select:none }}
    .no-log  {{ background:#020617; border-radius:8px; padding:14px; font-size:12px; color:#64748b;
                border:1px solid #0f172a }}
    .no-log code {{ display:block; margin-top:8px; color:#a78bfa; font-family:monospace; font-size:12px;
                    background:#0f172a; padding:8px 12px; border-radius:6px }}

    /* helpers */
    .sub-title {{ font-size:11px; text-transform:uppercase; letter-spacing:.06em; color:#475569; margin-bottom:5px }}
    .muted {{ color:#64748b }}
    .green {{ color:#4ade80 }}
    .blue  {{ color:#60a5fa }}
    .purple{{ color:#a78bfa }}
    .err   {{ color:#f87171 }}

    /* two-column grid */
    .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:18px; margin-bottom:18px }}
    @media(max-width:720px) {{ .grid2 {{ grid-template-columns:1fr }} .status-bar {{ flex-direction:column }} }}
  </style>
</head>
<body>
  <h1>🚀 Pinterest Scan — Bot Dashboard</h1>
  <div class="subtitle">Live updates every 15 s · <a href="/" style="color:#3b82f6">Force reload</a></div>

  {status_bar}

  <div class="rbar">
    <span>Last updated: <b id="last-updated" style="color:#60a5fa">{ts}</b></span>
    <span class="muted">Data from MySQL + local progress.json + log files · <span id="live-ticker" style="color:#4ade80"></span></span>
  </div>

  <div class="grid2">
    <div class="card" style="margin-bottom:0">
      <div class="card-title"><div class="dot" style="background:#22c55e;box-shadow:0 0 6px #22c55e"></div>Bot 1 — magic_scroll · keyword progress</div>
      {kw_body}
    </div>
    <div class="card" style="margin-bottom:0">
      <div class="card-title"><div class="dot" style="background:#a78bfa;box-shadow:0 0 6px #a78bfa"></div>MySQL totals (all PCs)</div>
      {mysql_body}
      <div style="margin-top:14px">
        <div class="card-title" style="margin-bottom:8px"><div class="dot" style="background:#60a5fa;box-shadow:0 0 6px #60a5fa"></div>magic_scroll cycle log</div>
        {ml_body}
      </div>
    </div>
  </div>

  <div class="card">
    <div class="card-title"><div class="dot" style="background:#f97316;box-shadow:0 0 6px #f97316"></div>Bot 2 — Step 10 — classifier — MySQL stats</div>
    {step10_body}
  </div>

  <div class="card">
    <div class="card-title"><div class="dot" style="background:#a78bfa;box-shadow:0 0 6px #a78bfa"></div>Bot 3 — Step 13 — AI website scanner — MySQL stats</div>
    {step13_body}
  </div>

  <div class="card">
    <div class="card-title"><div class="dot" style="background:#3b82f6;box-shadow:0 0 6px #3b82f6"></div>Bot 4 — Step 14 — blog link downloader — MySQL stats</div>
    {step14_body}
  </div>

  {cap_card}

  {plan_card}

  <hr style="border:none;border-top:1px solid #1e293b;margin:4px 0 20px">
  <h2 style="font-size:14px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.07em;margin-bottom:16px">
    📄 Live Bot Logs
  </h2>

  {log_panels}

  <div style="text-align:center;color:#1e293b;font-size:11px;margin-top:4px">dashboard.py</div>

<script>
// ── Live polling — updates bot pills + stats without reloading the page ────
const BOT_KEYS = ["magic_scroll","bot10","bot13","bot14","bot15"];
const PILL_CLS = {{run:"pill-run", idle:"pill-idle", stop:"pill-stop"}};

let _liveCountdown = 15;

async function _livePoll() {{
  try {{
    const r = await fetch("/api/live");
    const d = await r.json();
    if (!d.ok) return;

    // timestamp
    const lu = document.getElementById("last-updated");
    if (lu) lu.textContent = d.ts || "";

    // bot pills
    for (const key of BOT_KEYS) {{
      const el = document.getElementById("bot-" + key);
      if (!el || !d.bots[key]) continue;
      const b = d.bots[key];
      const pill = el.querySelector("span.pill, span[class^=pill], span[class*=' pill']")
                || el.querySelector("span:not(.sb-name)");
      if (pill) {{
        pill.className = "pill " + (PILL_CLS[b.cls] || "pill-stop");
        pill.textContent = b.txt;
      }}
    }}

    // plan counters
    const pt = document.getElementById("plan-total");
    if (pt) pt.textContent = (d.plan_total || 0).toLocaleString();
    const pp = document.getElementById("plan-pending");
    if (pp) pp.textContent = d.plan_pending ?? "?";

  }} catch(e) {{ /* ignore network errors during poll */ }}
}}

function _startLiveTicker() {{
  const tick = document.getElementById("live-ticker");
  setInterval(async () => {{
    _liveCountdown--;
    if (tick) tick.textContent = `next update in ${{_liveCountdown}}s`;
    if (_liveCountdown <= 0) {{
      _liveCountdown = 15;
      if (tick) tick.textContent = "updating…";
      await _livePoll();
      if (tick) tick.textContent = "✓ updated";
      setTimeout(() => {{ if (tick) tick.textContent = ""; }}, 2000);
    }}
  }}, 1000);
}}

document.addEventListener("DOMContentLoaded", _startLiveTicker);
</script>

</body>
</html>'''

# ── server ─────────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)

        if parsed.path == "/favicon.ico":
            self.send_response(204); self.end_headers(); return

        # ── /api/schedule?total=N&days=M ──────────────────────────────────────
        if parsed.path == "/api/schedule":
            import json as _json
            qs = parse_qs(parsed.query)
            total = max(1, min(500, int(qs.get("total", ["45"])[0])))
            days  = max(1, min(60,  int(qs.get("days",  ["15"])[0])))
            try:
                env2 = load_env()
                import pymysql as _pm
                conn2 = _pm.connect(
                    host=env2.get("MYSQL_HOST","72.61.197.144"),
                    port=int(env2.get("MYSQL_PORT",3306)),
                    db=env2.get("MYSQL_DB","data_pint"),
                    user=env2.get("MYSQL_USER","data_pint_user"),
                    password=env2.get("MYSQL_PASSWORD",""),
                    charset="utf8mb4", connect_timeout=6,
                )
                cur2 = conn2.cursor()
                cur2.execute("""
                    SELECT pin_id, pin_url, pinner_username, content_type, category,
                           description, post_score, seasonal_context, best_posting_days
                    FROM pin_content_analysis
                    WHERE post_score >= 5
                    ORDER BY post_score DESC, content_type
                    LIMIT %s
                """, (total * 3,))
                cols2 = [d[0] for d in cur2.description]
                all_pins = [dict(zip(cols2, r)) for r in cur2.fetchall()]
                conn2.close()
            except Exception as ex:
                body2 = _json.dumps({"error": str(ex)}).encode()
                self.send_response(500)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length", str(len(body2)))
                self.end_headers(); self.wfile.write(body2); return

            # distribute: round-robin content types, capped at total
            from collections import defaultdict
            by_type = defaultdict(list)
            for p in all_pins:
                by_type[p["content_type"]].append(p)
            type_order = sorted(by_type.keys(), key=lambda t: -len(by_type[t]))
            schedule = []   # list of lists (one per day)
            per_day  = max(1, -(-total // days))  # ceil division
            pool = []
            idx_map = {t: 0 for t in type_order}
            while len(pool) < total:
                added = 0
                for t in type_order:
                    i = idx_map[t]
                    if i < len(by_type[t]):
                        pool.append(by_type[t][i])
                        idx_map[t] += 1
                        added += 1
                    if len(pool) >= total:
                        break
                if added == 0:
                    break
            pool = pool[:total]
            from datetime import date as _d2, timedelta as _td2
            today2 = _d2.today()
            for day_i in range(days):
                start = day_i * per_day
                day_pins = pool[start:start + per_day]
                if not day_pins:
                    break
                schedule.append({
                    "day": day_i + 1,
                    "date": (today2 + _td2(days=day_i)).strftime("%A, %b %d"),
                    "pins": [
                        {"type": p["content_type"], "category": p["category"],
                         "score": p["post_score"], "url": p["pin_url"] or "",
                         "pinner": p["pinner_username"] or "",
                         "desc": (p["description"] or "")[:120]}
                        for p in day_pins
                    ]
                })
            body2 = _json.dumps({"ok": True, "schedule": schedule,
                                 "total": len(pool), "days": days}).encode()
            self.send_response(200)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body2)))
            self.end_headers(); self.wfile.write(body2); return

        # ── /api/pins?from=&to=&type=&sort=&minscore=&page= ──────────────────
        if parsed.path == "/api/pins":
            import json as _json
            qs = parse_qs(parsed.query)
            page      = max(1, int(qs.get("page",     ["1"])[0]))
            per_pg    = 50
            sort_key  = qs.get("sort",     ["score"])[0]
            from_dt   = qs.get("from",     [""])[0]
            to_dt     = qs.get("to",       [""])[0]
            ftype     = qs.get("type",     [""])[0]
            link_filter = qs.get("link", [""])[0]
            site_filter = qs.get("site", [""])[0]
            status_filter = qs.get("status", ["classified"])[0]
            if status_filter not in ("all", "with_link", "classified", "unclassified", "ready", "not_downloaded", "failed"):
                status_filter = "classified"
            minscore  = max(0, min(10, int(qs.get("minscore", ["0"])[0])))
            created_expr = (
                "COALESCE("
                "STR_TO_DATE(p.created_at, '%%a, %%d %%b %%Y %%H:%%i:%%s +0000'),"
                "STR_TO_DATE(p.created_at, '%%Y-%%m-%%dT%%H:%%i:%%s.%%fZ'),"
                "STR_TO_DATE(p.created_at, '%%Y-%%m-%%d %%H:%%i:%%s'),"
                "STR_TO_DATE(p.created_at, '%%Y-%%m-%%d')"
                ")"
            )
            order_sql = {
                "score":     "COALESCE(pca.post_score,0) DESC, pca.scanned_at DESC",
                "followers": "pi.follower_count DESC, COALESCE(pca.post_score,0) DESC",
                "repins":    "p.repin_count DESC, COALESCE(pca.post_score,0) DESC",
                "date":      f"{created_expr} DESC, COALESCE(pca.post_score,0) DESC",
                "type":      "pca.content_type ASC, COALESCE(pca.post_score,0) DESC",
            }.get(sort_key, "COALESCE(pca.post_score,0) DESC")
            where, params = ["p.pin_type = 'created'"], []
            if status_filter == "classified":
                where.extend([
                    "p.link_download_status = 'Done'",
                    "p.link_html IS NOT NULL",
                    "p.link_html != ''",
                ])
                where.append("pca.pin_id IS NOT NULL")
                where.append("pca.post_score >= %s")
                params.append(minscore)
            elif status_filter == "unclassified":
                where.extend([
                    "p.link_download_status = 'Done'",
                    "p.link_html IS NOT NULL",
                    "p.link_html != ''",
                ])
                where.append("pca.pin_id IS NULL")
            elif status_filter == "ready":
                where.extend([
                    "p.link_download_status = 'Done'",
                    "p.link_html IS NOT NULL",
                    "p.link_html != ''",
                ])
                where.append("(pca.pin_id IS NULL OR pca.post_score >= %s)")
                params.append(minscore)
            elif status_filter == "with_link":
                where.append("COALESCE(p.link,'') != ''")
            elif status_filter == "not_downloaded":
                where.append("(p.link_download_status IS NULL OR p.link_download_status = '')")
            elif status_filter == "failed":
                where.append("p.link_download_status LIKE 'Failed%%'")
            if from_dt:  where.append(f"{created_expr} >= %s");       params.append(from_dt)
            if to_dt:    where.append(f"{created_expr} <= %s");       params.append(to_dt + " 23:59:59")
            if link_filter == "with":
                where.append("COALESCE(p.link,'') != ''")
            elif link_filter == "without":
                where.append("COALESCE(p.link,'') = ''")
            if site_filter == "blank":
                where.append("(pi.site_type IS NULL OR pi.site_type = '')")
            elif site_filter:
                where.append("pi.site_type = %s")
                params.append(site_filter)
            if ftype:
                where.append("pca.pin_id IS NOT NULL")
                where.append("pca.content_type = %s")
                params.append(ftype)
            where_str = " AND ".join(where)
            try:
                env3 = load_env()
                import pymysql as _pm3
                conn3 = _pm3.connect(
                    host=env3.get("MYSQL_HOST","72.61.197.144"),
                    port=int(env3.get("MYSQL_PORT",3306)),
                    db=env3.get("MYSQL_DB","data_pint"),
                    user=env3.get("MYSQL_USER","data_pint_user"),
                    password=env3.get("MYSQL_PASSWORD",""),
                    charset="utf8mb4", connect_timeout=6,
                )
                cur3 = conn3.cursor()
                cur3.execute(f"""
                    SELECT COUNT(*)
                    FROM pins p
                    LEFT JOIN pin_content_analysis pca ON pca.pin_id = p.id
                    LEFT JOIN pinners pi ON pi.username = COALESCE(pca.pinner_username, p.pinner_username)
                    WHERE {where_str}
                """, params)
                total3 = cur3.fetchone()[0]
                cur3.execute(f"""
                    SELECT COALESCE(pca.pin_id, p.id),
                           COALESCE(pca.pin_url, p.pin_url),
                           COALESCE(pca.pinner_username, p.pinner_username),
                           pca.content_type,
                           COALESCE(pca.category, 'Unclassified'),
                           COALESCE(pca.description, p.description, p.title),
                           pca.post_score, pca.seasonal_context, pca.best_posting_days,
                           pca.scanned_at,
                           COALESCE(pi.follower_count,0), COALESCE(pi.profile_reach,0),
                           COALESCE(p.repin_count,0),     COALESCE(p.link,''),
                           COALESCE(DATE_FORMAT({created_expr}, '%%Y-%%m-%%d'), ''),
                           COALESCE(p.created_at,''),
                           pca.pin_id IS NOT NULL,
                           COALESCE(p.link_download_status, ''),
                           COALESCE(pi.site_type, ''),
                           (COALESCE(p.link_download_status, '') = 'Done' AND COALESCE(p.link_html, '') != ''),
                           COALESCE(pca.hook_generated_by_ia_scan, ''),
                           COALESCE(pca.keywords_generated_by_ia_scan, ''),
                           COALESCE(pca.posting_window_generated_by_ia_scan, ''),
                           COALESCE(pca.target_audience_generated_by_ia_scan, ''),
                           COALESCE(pca.monetization_angle_generated_by_ia_scan, ''),
                           COALESCE(pca.caption_generated_by_ia_scan, ''),
                           COALESCE(CAST(pca.content_json_generated_by_ia_scan AS CHAR), '')
                    FROM pins p
                    LEFT JOIN pin_content_analysis pca ON pca.pin_id = p.id
                    LEFT JOIN pinners pi ON pi.username = COALESCE(pca.pinner_username, p.pinner_username)
                    WHERE {where_str}
                    ORDER BY {order_sql}
                    LIMIT %s OFFSET %s
                """, params + [per_pg, (page-1)*per_pg])
                rows3 = cur3.fetchall()
                conn3.close()
                pins3 = [
                    {"pin_id": r[0], "pin_url": r[1] or "", "pinner": r[2] or "",
                     "type": r[3] or "Other", "category": r[4] or "",
                     "desc": (r[5] or "")[:160], "score": r[6] or 0,
                     "seasonal": r[7] or "", "days": r[8] or "",
                     "scanned": str(r[9] or "")[:16],
                     "followers": r[10] or 0, "reach": r[11] or 0,
                     "repins": r[12] or 0, "blog_url": r[13] or "",
                     "created_at": str(r[14] or r[15] or "")[:10],
                     "classified": bool(r[16]),
                      "download_status": r[17] or "",
                      "site_type": r[18] or "",
                      "ready_for_ai": bool(r[19]),
                      "ia_hook": (r[20] or "")[:180],
                      "ia_keywords": (r[21] or "")[:220],
                      "ia_window": (r[22] or "")[:220],
                      "ia_audience": (r[23] or "")[:180],
                      "ia_monetization": (r[24] or "")[:120],
                      "ia_caption": (r[25] or "")[:220],
                      "ia_json": r[26] or ""}
                    for r in rows3
                ]
                body3 = _json.dumps({"ok": True, "total": total3,
                                     "page": page, "per_page": per_pg,
                                     "pins": pins3}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body3)))
                self.end_headers(); self.wfile.write(body3); return
            except Exception as ex3:
                body3 = _json.dumps({"ok": False, "error": str(ex3)}).encode()
                self.send_response(500)
                self.send_header("Content-Type","application/json")
                self.send_header("Content-Length", str(len(body3)))
                self.end_headers(); self.wfile.write(body3); return

        # ── /api/live — lightweight status poll (no new DB call, reads cache) ──
        if parsed.path == "/api/scan-selected":
            import json as _json
            import subprocess as _subprocess
            qs = parse_qs(parsed.query)
            from_dt = qs.get("from", [""])[0]
            to_dt = qs.get("to", [""])[0]
            link_filter = qs.get("link", [""])[0]
            site_filter = qs.get("site", [""])[0]
            try:
                limit = max(1, min(2000, int(qs.get("limit", ["500"])[0])))
                workers = max(1, min(30, int(qs.get("workers", ["10"])[0])))
            except Exception:
                limit, workers = 500, 10
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", from_dt or "") or not re.match(r"^\d{4}-\d{2}-\d{2}$", to_dt or ""):
                body_scan = _json.dumps({"ok": False, "error": "from/to must be YYYY-MM-DD"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_scan)))
                self.end_headers(); self.wfile.write(body_scan); return
            if link_filter == "without":
                body_scan = _json.dumps({"ok": False, "error": "Step 15 can only scan pins with downloaded blog content. Choose Link=Has link or All."}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_scan)))
                self.end_headers(); self.wfile.write(body_scan); return
            env_scan = load_env()
            if not env_scan.get("MYSQL_PASSWORD"):
                body_scan = _json.dumps({"ok": False, "error": "MYSQL_PASSWORD is not configured"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_scan)))
                self.end_headers(); self.wfile.write(body_scan); return
            script = BASE / "15_post_planner.py"
            if not script.exists():
                body_scan = _json.dumps({"ok": False, "error": "15_post_planner.py not found"}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_scan)))
                self.end_headers(); self.wfile.write(body_scan); return
            try:
                proc_env = os.environ.copy()
                proc_env["PYTHONIOENCODING"] = "utf-8"
                job_id = f"{int(time.time())}-{from_dt}-{to_dt}"
                log_dir = BASE / "dashboard_scan_logs"
                log_dir.mkdir(exist_ok=True)
                log_path = log_dir / f"step15-{job_id}.log"
                log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
                cmd_scan = [
                        sys.executable, "-u", str(script),
                        "--source", "mysql",
                        "--once",
                        "--limit", str(limit),
                        "--workers", str(workers),
                        "--from", from_dt,
                        "--to", to_dt,
                        "--created-only",
                    ]
                if site_filter:
                    cmd_scan += ["--site-type", site_filter]
                proc = _subprocess.Popen(
                    cmd_scan,
                    cwd=str(BASE),
                    env=proc_env,
                    stdout=log_fh,
                    stderr=_subprocess.STDOUT,
                    creationflags=getattr(_subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
                log_fh.close()
                _scan_jobs[job_id] = {
                    "proc": proc,
                    "pid": proc.pid,
                    "log_path": str(log_path),
                    "from": from_dt,
                    "to": to_dt,
                    "site": site_filter,
                    "link": link_filter,
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                }
                body_scan = _json.dumps({"ok": True, "pid": proc.pid, "job_id": job_id, "limit": limit}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_scan)))
                self.end_headers(); self.wfile.write(body_scan); return
            except Exception as ex_scan:
                body_scan = _json.dumps({"ok": False, "error": str(ex_scan)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_scan)))
                self.end_headers(); self.wfile.write(body_scan); return

        if parsed.path == "/api/download-pin":
            import json as _json
            import subprocess as _subprocess
            qs = parse_qs(parsed.query)
            pin_id = qs.get("pin_id", [""])[0].strip()
            if not re.match(r"^\d{5,}$", pin_id):
                body_pin = _json.dumps({"ok": False, "error": "pin_id is required"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_pin)))
                self.end_headers(); self.wfile.write(body_pin); return
            env_scan = load_env()
            if not env_scan.get("MYSQL_PASSWORD"):
                body_pin = _json.dumps({"ok": False, "error": "MYSQL_PASSWORD is not configured"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_pin)))
                self.end_headers(); self.wfile.write(body_pin); return
            script = BASE / "14_download_blog_pin_links.py"
            if not script.exists():
                body_pin = _json.dumps({"ok": False, "error": "14_download_blog_pin_links.py not found"}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_pin)))
                self.end_headers(); self.wfile.write(body_pin); return
            try:
                proc_env = os.environ.copy()
                proc_env["PYTHONIOENCODING"] = "utf-8"
                job_id = f"{int(time.time())}-step14-pin-{pin_id}"
                log_dir = BASE / "dashboard_scan_logs"
                log_dir.mkdir(exist_ok=True)
                log_path = log_dir / f"{job_id}.log"
                log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
                proc = _subprocess.Popen(
                    [
                        sys.executable, "-u", str(script),
                        "--source", "mysql",
                        "--once",
                        "--workers", "1",
                        "--limit", "1",
                        "--retry-failed",
                        "--pin-id", pin_id,
                    ],
                    cwd=str(BASE),
                    env=proc_env,
                    stdout=log_fh,
                    stderr=_subprocess.STDOUT,
                    creationflags=getattr(_subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
                log_fh.close()
                _scan_jobs[job_id] = {
                    "proc": proc,
                    "pid": proc.pid,
                    "log_path": str(log_path),
                    "pin_id": pin_id,
                    "step": "14",
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                }
                body_pin = _json.dumps({"ok": True, "pid": proc.pid, "job_id": job_id}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_pin)))
                self.end_headers(); self.wfile.write(body_pin); return
            except Exception as ex_pin:
                body_pin = _json.dumps({"ok": False, "error": str(ex_pin)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_pin)))
                self.end_headers(); self.wfile.write(body_pin); return

        if parsed.path == "/api/scan-pin":
            import json as _json
            import subprocess as _subprocess
            qs = parse_qs(parsed.query)
            pin_id = qs.get("pin_id", [""])[0].strip()
            force_scan = qs.get("force", ["0"])[0] == "1"
            if not re.match(r"^\d{5,}$", pin_id):
                body_pin = _json.dumps({"ok": False, "error": "pin_id is required"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_pin)))
                self.end_headers(); self.wfile.write(body_pin); return
            env_scan = load_env()
            if not env_scan.get("MYSQL_PASSWORD"):
                body_pin = _json.dumps({"ok": False, "error": "MYSQL_PASSWORD is not configured"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_pin)))
                self.end_headers(); self.wfile.write(body_pin); return
            script = BASE / "15_post_planner.py"
            if not script.exists():
                body_pin = _json.dumps({"ok": False, "error": "15_post_planner.py not found"}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_pin)))
                self.end_headers(); self.wfile.write(body_pin); return
            try:
                proc_env = os.environ.copy()
                proc_env["PYTHONIOENCODING"] = "utf-8"
                job_id = f"{int(time.time())}-pin-{pin_id}"
                log_dir = BASE / "dashboard_scan_logs"
                log_dir.mkdir(exist_ok=True)
                log_path = log_dir / f"step15-{job_id}.log"
                log_fh = open(log_path, "w", encoding="utf-8", errors="replace")
                cmd_pin = [
                    sys.executable, "-u", str(script),
                    "--source", "mysql",
                    "--once",
                    "--limit", "1",
                    "--workers", "1",
                    "--pin-id", pin_id,
                    "--no-calendar",
                ]
                if force_scan:
                    cmd_pin.append("--force")
                proc = _subprocess.Popen(
                    cmd_pin,
                    cwd=str(BASE),
                    env=proc_env,
                    stdout=log_fh,
                    stderr=_subprocess.STDOUT,
                    creationflags=getattr(_subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                )
                log_fh.close()
                _scan_jobs[job_id] = {
                    "proc": proc,
                    "pid": proc.pid,
                    "log_path": str(log_path),
                    "pin_id": pin_id,
                    "force": force_scan,
                    "started_at": datetime.now().isoformat(timespec="seconds"),
                }
                body_pin = _json.dumps({"ok": True, "pid": proc.pid, "job_id": job_id}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_pin)))
                self.end_headers(); self.wfile.write(body_pin); return
            except Exception as ex_pin:
                body_pin = _json.dumps({"ok": False, "error": str(ex_pin)}).encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_pin)))
                self.end_headers(); self.wfile.write(body_pin); return

        if parsed.path == "/api/scan-status":
            import json as _json
            qs = parse_qs(parsed.query)
            job_id = qs.get("job_id", [""])[0]
            job = _scan_jobs.get(job_id)
            if not job:
                body_status = _json.dumps({"ok": False, "error": "scan job not found"}).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type","application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body_status)))
                self.end_headers(); self.wfile.write(body_status); return
            proc = job.get("proc")
            returncode = proc.poll() if proc else None
            log_text = ""
            try:
                log_path = Path(job["log_path"])
                if log_path.exists():
                    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
                    log_text = "\n".join(lines[-80:])
            except Exception as log_ex:
                log_text = f"(could not read log: {log_ex})"
            body_status = _json.dumps({
                "ok": True,
                "job_id": job_id,
                "pid": job.get("pid"),
                "running": returncode is None,
                "returncode": returncode,
                "started_at": job.get("started_at"),
                "log": log_text,
            }).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body_status)))
            self.end_headers(); self.wfile.write(body_status); return

        if parsed.path == "/api/live":
            import json as _json
            with _lock:
                mysql2  = dict(_cache.get("mysql", {}))
                local2  = dict(_cache.get("local_bots", {}))
                ts2     = _cache.get("ts", "")
            bot_st2 = mysql2.get("bot_status", {}) if mysql2.get("ok") else {}
            def _pill_data(key):
                db_st, db_ex = bot_st2.get(key, ("unknown", None))
                local_r, local_p = local2.get(key, (False, None))
                if db_st == "running":   return {"cls": "run",  "txt": "● RUNNING"}
                if db_st == "idle":      return {"cls": "idle", "txt": f"◑ IDLE ({db_ex}m ago)"}
                if db_st == "no_logs":
                    if local_r:          return {"cls": "run",  "txt": f"● RUNNING (pid {local_p})"}
                    return {"cls": "stop", "txt": "○ STOPPED (restart for DB logs)"}
                return {"cls": "stop", "txt": "○ STOPPED"}
            bots2 = {k: _pill_data(k) for k in
                     ("magic_scroll","bot10","bot13","bot14","bot15")}
            plan_t2 = mysql2.get("plan_total",   0)  if mysql2.get("ok") else 0
            plan_p2 = mysql2.get("plan_pending","?") if mysql2.get("ok") else "?"
            totals2 = mysql2.get("totals", {})        if mysql2.get("ok") else {}
            body_live = _json.dumps({"ok": True, "ts": ts2, "bots": bots2,
                                     "plan_total": plan_t2, "plan_pending": plan_p2,
                                     "totals": totals2}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type","application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body_live)))
            self.end_headers(); self.wfile.write(body_live); return

        if parsed.path not in ("/", ""):
            self.send_response(404); self.end_headers(); return
        body = build_page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

def _start_planner():
    """Launch 15_post_planner.py --source mysql in a background subprocess.
    Only starts if not already running and MySQL password is configured."""
    import subprocess, sys as _sys, os as _os
    env = load_env()
    if not env.get("MYSQL_PASSWORD"):
        print("   ⚠ Planner not started — MYSQL_PASSWORD not set in .env")
        return
    script = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                           "15_post_planner.py")
    if not _os.path.exists(script):
        print("   ⚠ 15_post_planner.py not found — skipping auto-start")
        return
    running, pid = get_running_bots_local().get("bot15", (False, None))
    if running:
        print(f"   Step 15 planner already running (pid {pid})")
        return
    try:
        proc = subprocess.Popen(
            [_sys.executable, script, "--source", "mysql"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
        print(f"   🤖 Step 15 planner started in background (pid {proc.pid})")
    except Exception as ex:
        print(f"   ⚠ Could not start planner: {ex}")


def _watch_self_reload():
    """Restart dashboard.py when this file changes."""
    this_file = Path(__file__).resolve()
    try:
        last_mtime = this_file.stat().st_mtime
    except OSError:
        return

    while True:
        time.sleep(1.0)
        try:
            current_mtime = this_file.stat().st_mtime
        except OSError:
            continue
        if current_mtime != last_mtime:
            print("\n   dashboard.py changed - restarting server...\n", flush=True)
            os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    PORT = 8765
    print("⏳ Loading data…")
    refresh_cache()
    threading.Thread(target=bg_refresh, daemon=True).start()
    threading.Thread(target=_watch_self_reload, daemon=True).start()
    _start_planner()
    print(f"\n✅  http://localhost:{PORT}\n")
    print("   Logs are read from MySQL bot_logs table (via db_logger.py).")
    print("   Restart each bot once — after that logs save to MySQL automatically forever.\n")
    print("   Ctrl+C to stop.\n")
    try:
        HTTPServer(("", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
