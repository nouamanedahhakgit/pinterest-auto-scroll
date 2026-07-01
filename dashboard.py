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
}

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
                "cap_warnings": cap_warnings}
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
            return (f'<div class="sb-item"><span class="sb-name">{label}</span>'
                    f'<span class="pill pill-run">● RUNNING</span></div>')
        elif db_st == "idle":
            return (f'<div class="sb-item"><span class="sb-name">{label}</span>'
                    f'<span class="pill pill-idle">◑ IDLE ({db_extra}m ago)</span></div>')
        elif db_st == "no_logs":
            # no DB logs yet — fall back to local psutil
            if local_running:
                return (f'<div class="sb-item"><span class="sb-name">{label}</span>'
                        f'<span class="pill pill-run">● RUNNING (local pid {local_pid})</span></div>')
            return (f'<div class="sb-item"><span class="sb-name">{label}</span>'
                    f'<span class="pill pill-stop">○ STOPPED <small style="font-weight:400">(restart to enable DB logs)</small></span></div>')
        else:
            return (f'<div class="sb-item"><span class="sb-name">{label}</span>'
                    f'<span class="pill pill-stop">○ STOPPED</span></div>')

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
        mysql_body = step10_body = step14_body = f'<p class="err">⚠ MySQL: {err}</p>'
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
  <meta http-equiv="refresh" content="15">
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
  <div class="subtitle">Auto-refreshes every 15 s · <a href="/" style="color:#3b82f6">Refresh now</a></div>

  {status_bar}

  <div class="rbar">
    <span>Last updated: <b style="color:#60a5fa">{ts}</b></span>
    <span class="muted">Data from MySQL + local progress.json + log files</span>
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

  <hr style="border:none;border-top:1px solid #1e293b;margin:4px 0 20px">
  <h2 style="font-size:14px;font-weight:700;color:#94a3b8;text-transform:uppercase;letter-spacing:.07em;margin-bottom:16px">
    📄 Live Bot Logs
  </h2>

  {log_panels}

  <div style="text-align:center;color:#1e293b;font-size:11px;margin-top:4px">dashboard.py</div>
</body>
</html>'''

# ── server ─────────────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_response(204); self.end_headers(); return
        if self.path not in ("/", ""):
            self.send_response(404); self.end_headers(); return
        body = build_page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass

if __name__ == "__main__":
    PORT = 8765
    print("⏳ Loading data…")
    refresh_cache()
    threading.Thread(target=bg_refresh, daemon=True).start()
    print(f"\n✅  http://localhost:{PORT}\n")
    print("   Logs are read from MySQL bot_logs table (via db_logger.py).")
    print("   Restart each bot once — after that logs save to MySQL automatically forever.\n")
    print("   Ctrl+C to stop.\n")
    try:
        HTTPServer(("", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
