#!/usr/bin/env python3
"""
Pinterest Scan — Live Bot Dashboard
Run:  python dashboard.py
Open: http://localhost:8765   (auto-refreshes every 15 s)

Start your bots WITH logging so this dashboard can tail them:
  python magic_scroll.py --15m     2>&1 | tee logs/magic_scroll.log
  python 10_domain_quick_scrape_api.py  2>&1 | tee logs/bot10.log
  python 14_download_blog_pin_links.py  2>&1 | tee logs/bot14.log
"""

import html as _html
import json
import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# ── paths ──────────────────────────────────────────────────────────────────────
BASE         = Path(__file__).parent
LOGS_DIR     = BASE / "logs"
PROGRESS_JSON = BASE / "progress.json"
MAGIC_LOG    = BASE / "magic_log.jsonl"
SORTPIN_DB   = BASE / "sortpin.db"
ENV_FILE     = BASE / ".env"

LOG_FILES = {
    "magic_scroll": LOGS_DIR / "magic_scroll.log",
    "bot10":        LOGS_DIR / "bot10.log",
    "bot14":        LOGS_DIR / "bot14.log",
}

BOT_SCRIPTS = {
    "magic_scroll": "magic_scroll.py",
    "bot10":        "10_domain_quick_scrape_api.py",
    "bot14":        "14_download_blog_pin_links.py",
}

LOGS_DIR.mkdir(exist_ok=True)

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
def get_running_bots():
    """Returns dict of bot_key -> (True/False, pid or None)"""
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

# ── log tail ───────────────────────────────────────────────────────────────────
def tail_log(path: Path, n=120):
    """Return last n lines of a log file, or None if missing."""
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = min(size, 80_000)
            f.seek(-chunk, 2)
            raw = f.read().decode("utf-8", errors="replace")
        lines = raw.splitlines()
        return lines[-n:]
    except Exception:
        return []

def log_stats(lines):
    """Count errors/warnings in log lines."""
    if not lines:
        return 0, 0, 0
    errors   = sum(1 for l in lines if re.search(r'\berror\b|\bexception\b|\btraceback\b', l, re.I))
    warnings = sum(1 for l in lines if re.search(r'\bwarn\b|\bwarning\b', l, re.I))
    recent_errors = [l for l in lines[-300:] if re.search(r'\berror\b|\bexception\b|\btraceback\b|\bfailed\b', l, re.I)]
    return errors, warnings, recent_errors

# ── keyword stats ──────────────────────────────────────────────────────────────
def get_keyword_stats():
    try:
        data = json.loads(PROGRESS_JSON.read_text(errors="replace"))
    except Exception as e:
        return {"error": str(e)}
    done, not_yet, running = 0, 0, 0
    recent = []
    for kw, v in data.items():
        s  = v.get("status", "") if isinstance(v, dict) else str(v)
        ts = v.get("done_at", "") if isinstance(v, dict) else ""
        if s == "done":
            done += 1
            if ts: recent.append((ts, kw))
        elif s in ("not_yet", "Not Yet"): not_yet += 1
        elif s == "running": running += 1
    recent.sort(reverse=True)
    total = len(data)
    return {"total": total, "done": done, "not_yet": not_yet, "running": running,
            "pct": round(done/total*100,1) if total else 0,
            "recent": [{"ts": t, "kw": k} for t, k in recent[:6]]}

def get_magic_log():
    try:
        lines = MAGIC_LOG.read_text(errors="replace").strip().splitlines()
        return [json.loads(l) for l in reversed(lines[-15:]) if l.strip()][:10]
    except Exception:
        return []

# ── mysql stats ────────────────────────────────────────────────────────────────
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

        conn.close()
        return {"ok": True, "totals": totals, "sw_status": sw_status,
                "sw_types": sw_types, "ia_rows": ia_rows, "ai_scan": ai_scan,
                "sw_recent": sw_recent, "sw_failed": sw_failed,
                "dl_status": dl_status, "blog_pinners": blog_pinners,
                "blog_pins_total": blog_pins_total, "dl_recent": dl_recent}
    except ImportError:
        return {"ok": False, "error": "pymysql not installed — pip install pymysql"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ── cache ──────────────────────────────────────────────────────────────────────
_cache = {}
_lock  = threading.Lock()

def refresh_cache():
    env   = load_env()
    bots  = get_running_bots()
    logs  = {k: tail_log(p) for k, p in LOG_FILES.items()}
    kw    = get_keyword_stats()
    ml    = get_magic_log()
    mysql = get_mysql_stats(env)
    with _lock:
        _cache.update({"bots": bots, "logs": logs, "kw": kw,
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

def render_log(lines, bot_key):
    """Render log lines with colour coding + error highlighting."""
    if lines is None:
        # log file doesn't exist yet — show start command
        cmds = {
            "magic_scroll": "python magic_scroll.py --15m 2>&1 | tee logs/magic_scroll.log",
            "bot10":        "python 10_domain_quick_scrape_api.py 2>&1 | tee logs/bot10.log",
            "bot14":        "python 14_download_blog_pin_links.py 2>&1 | tee logs/bot14.log",
        }
        return (f'<div class="no-log">'
                f'<b>No log file yet.</b> Start the bot like this so logs are captured:<br><br>'
                f'<code>{e(cmds.get(bot_key, ""))}</code>'
                f'</div>')

    if not lines:
        return '<div class="no-log muted">Log file is empty.</div>'

    out = ['<div class="log-box">']
    for line in lines:
        lw = line.lower()
        if re.search(r'traceback|exception|error\b', lw):
            cls = "ll-err"
        elif re.search(r'\bwarn\b|\bwarning\b', lw):
            cls = "ll-warn"
        elif re.search(r'\bdone\b|✓|✅|success|complete', lw):
            cls = "ll-ok"
        elif re.search(r'running|starting|pass #|cycle|batch|worker', lw):
            cls = "ll-info"
        else:
            cls = "ll-dim"
        out.append(f'<div class="{cls}">{e(line)}</div>')
    out.append('</div>')
    return "".join(out)

def stat_box(n, label, color="#e2e8f0"):
    return (f'<div class="stat-box">'
            f'<div class="stat-n" style="color:{color}">{n}</div>'
            f'<div class="stat-l">{label}</div></div>')

# ── full page ──────────────────────────────────────────────────────────────────
def build_page():
    with _lock:
        bots  = dict(_cache.get("bots", {}))
        logs  = dict(_cache.get("logs", {}))
        kw    = dict(_cache.get("kw",   {}))
        ml    = list(_cache.get("ml",   []))
        mysql = dict(_cache.get("mysql",{}))
        ts    = _cache.get("ts", "…")

    # ── process status bar ─────────────────────────────────────────────────────
    b1r, b1p = bots.get("magic_scroll", (False, None))
    b2r, b2p = bots.get("bot10", (False, None))
    b3r, b3p = bots.get("bot14", (False, None))

    status_bar = f'''
    <div class="status-bar">
      <div class="sb-item">
        <span class="sb-name">magic_scroll</span>
        {status_pill(b1r, b1p)}
      </div>
      <div class="sb-item">
        <span class="sb-name">Step 10 (classifier)</span>
        {status_pill(b2r, b2p)}
      </div>
      <div class="sb-item">
        <span class="sb-name">Step 14 (downloader)</span>
        {status_pill(b3r, b3p)}
      </div>
    </div>'''

    # ── keyword card ───────────────────────────────────────────────────────────
    if "error" in kw:
        kw_body = f'<p class="err">Error: {e(kw["error"])}</p>'
    else:
        done = kw.get("done", 0); total = kw.get("total", 1)
        not_yet = kw.get("not_yet", 0); running = kw.get("running", 0)
        recent_rows = "".join(
            f'<tr><td class="muted">{e(r["ts"])}</td><td>{e(r["kw"])}</td></tr>'
            for r in kw.get("recent", [])
        )
        kw_body = f'''
        <div class="stat-row">
          {stat_box(f'{done:,}',    "Done ✓",   "#22c55e")}
          {stat_box(f'{not_yet:,}', "Not Yet",   "#94a3b8")}
          {stat_box(running,        "Running",   "#3b82f6")}
          {stat_box(f'{total:,}',   "Total kw",  "#a78bfa")}
        </div>
        {pct_bar(done, total)}
        <div class="sub-title" style="margin-top:14px">Recently done</div>
        <table class="mini-table">{recent_rows}</table>'''

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

    # ── log panels ─────────────────────────────────────────────────────────────
    def log_panel(key, title, dot_color):
        lines = logs.get(key)
        errs, warns, _ = log_stats(lines or [])
        badges = ""
        if lines is not None:
            sz = LOG_FILES[key].stat().st_size if LOG_FILES[key].exists() else 0
            sz_kb = sz // 1024
            age_s = int(time.time() - LOG_FILES[key].stat().st_mtime) if LOG_FILES[key].exists() else 0
            age = f"{age_s}s ago" if age_s < 120 else f"{age_s//60}m ago"
            badges = (f'<span class="pill pill-info">{len(lines or [])} lines shown</span> '
                      f'<span class="pill pill-info">{sz_kb} KB</span> '
                      f'<span class="pill pill-info">updated {age}</span> ')
            if errs:
                badges += f'<span class="pill pill-err">{errs} errors</span> '
            if warns:
                badges += f'<span class="pill pill-warn">{warns} warnings</span>'
        return f'''
        <div class="card" style="margin-bottom:20px">
          <div class="card-title">
            <div class="dot" style="background:{dot_color};box-shadow:0 0 6px {dot_color}"></div>
            {title} — live log
            <span style="margin-left:auto;font-weight:400;font-size:12px">{badges}</span>
          </div>
          {render_log(lines, key)}
        </div>'''

    log_panels = (
        log_panel("magic_scroll", "Bot 1 · magic_scroll", "#22c55e") +
        log_panel("bot10",        "Bot 2 · Step 10 (domain classifier)", "#f97316") +
        log_panel("bot14",        "Bot 3 · Step 14 (link downloader)",   "#3b82f6")
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
    <div class="card-title"><div class="dot" style="background:#f97316;box-shadow:0 0 6px #f97316"></div>Bot 2 — Step 10 (classifier) + Step 13 (AI scanner) — MySQL stats</div>
    {step10_body}
  </div>

  <div class="card">
    <div class="card-title"><div class="dot" style="background:#3b82f6;box-shadow:0 0 6px #3b82f6"></div>Bot 3 — Step 14 (blog link downloader) — MySQL stats</div>
    {step14_body}
  </div>

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
    print("   Logs are tailed from:  logs/magic_scroll.log  /  logs/bot10.log  /  logs/bot14.log")
    print("   Start bots like this to capture logs:\n")
    print("     python magic_scroll.py --15m 2>&1 | tee logs/magic_scroll.log")
    print("     python 10_domain_quick_scrape_api.py 2>&1 | tee logs/bot10.log")
    print("     python 14_download_blog_pin_links.py 2>&1 | tee logs/bot14.log\n")
    print("   Ctrl+C to stop.\n")
    try:
        HTTPServer(("", PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")
