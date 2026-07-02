"""
15_post_planner.py — AI pin classifier + 15-day post schedule builder.

For every pin whose blog post was downloaded by step 14 (link_download_status='Done'),
extracts text from the downloaded HTML/CSS/JS, asks an AI model to classify it
(Seasonal / Trend / Shopping / Lifestyle / Recipe / DIY / Other), and suggests which
of the next 15 days are best to post it. Results are saved to pin_content_analysis
(never re-scans the same pin). Prints or exports a full 15-day calendar.

--source mysql  (default) — shared cloud MySQL DB (all PCs' pins)
--source sqlite             — this PC's local sortpin.db only
--source auto               — mysql if MYSQL_PASSWORD in .env, else sqlite

Run:
    python 15_post_planner.py                # scan all eligible, then poll forever
    python 15_post_planner.py --once         # single pass then exit
    python 15_post_planner.py --workers 10   # parallel AI threads (default 10)
    python 15_post_planner.py --limit 100    # cap this pass to N pins
    python 15_post_planner.py --dry-run      # classify+print, no DB writes
    python 15_post_planner.py --schedule     # print 15-day calendar, no new scanning
    python 15_post_planner.py --source mysql
    python 15_post_planner.py --source sqlite
"""
from __future__ import annotations
import argparse
import html as _html
import json
import os
import re
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path
from textwrap import shorten

import requests

# ─── Config ───────────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "sortpin.db"
POLL_MINUTES = 10
BATCH_SIZE   = 50        # pins per DB fetch
MAX_HTML_SEND = 3_000    # chars of blog body text to send to AI
TODAY        = date.today()
DAYS_AHEAD   = 15
SCHEDULE_DATES = [TODAY + timedelta(days=i) for i in range(DAYS_AHEAD)]

# ─── .env ─────────────────────────────────────────────────────────────────────
def _load_env() -> dict:
    env = {}
    p = Path(__file__).parent / ".env"
    if p.exists():
        for line in p.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

_ENV = _load_env()
OPENROUTER_KEY = _ENV.get("OPENROUTER_API_KEY", "")
AI_MODEL       = _ENV.get("QUICK_SCRAPE_OPENROUTER_MODEL", "openai/gpt-4.1-nano")

# ─── MySQL helper (mirrors step 14) ───────────────────────────────────────────
_mysql_conn = None
_mysql_lock = threading.Lock()

def _get_mysql():
    global _mysql_conn
    with _mysql_lock:
        try:
            if _mysql_conn and _mysql_conn.open:
                _mysql_conn.ping(reconnect=True)
                return _mysql_conn
        except Exception:
            _mysql_conn = None
        try:
            import pymysql
            _mysql_conn = pymysql.connect(
                host=_ENV["MYSQL_HOST"], port=int(_ENV.get("MYSQL_PORT", 3306)),
                db=_ENV["MYSQL_DB"], user=_ENV["MYSQL_USER"],
                password=_ENV["MYSQL_PASSWORD"],
                charset="utf8mb4", connect_timeout=6, autocommit=True,
            )
            return _mysql_conn
        except Exception as e:
            print(f"[mysql] connection failed: {e}")
            return None

def _mysql_available() -> bool:
    return bool(_ENV.get("MYSQL_PASSWORD")) and _get_mysql() is not None

# ─── Table auto-create ─────────────────────────────────────────────────────────
CREATE_SQLITE = """
CREATE TABLE IF NOT EXISTS pin_content_analysis (
    pin_id            TEXT PRIMARY KEY,
    pin_url           TEXT,
    pinner_username   TEXT,
    board_name        TEXT,
    content_type      TEXT,
    category          TEXT,
    description       TEXT,
    post_score        INTEGER DEFAULT 0,
    seasonal_context  TEXT,
    best_posting_days TEXT,
    scanned_at        DATETIME DEFAULT CURRENT_TIMESTAMP
)
"""
CREATE_MYSQL = """
CREATE TABLE IF NOT EXISTS pin_content_analysis (
    pin_id            VARCHAR(64) PRIMARY KEY,
    pin_url           VARCHAR(500),
    pinner_username   VARCHAR(200),
    board_name        VARCHAR(500),
    content_type      VARCHAR(50),
    category          VARCHAR(100),
    description       TEXT,
    post_score        TINYINT DEFAULT 0,
    seasonal_context  VARCHAR(200),
    best_posting_days VARCHAR(100),
    scanned_at        DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_type    (content_type),
    INDEX idx_score   (post_score)
) CHARACTER SET utf8mb4
"""

def _ensure_table_sqlite(con):
    con.execute(CREATE_SQLITE)
    con.commit()

def _ensure_table_mysql(conn):
    conn.cursor().execute(CREATE_MYSQL)

# ─── HTML → clean text ────────────────────────────────────────────────────────
def _extract_text_from_html(html: str) -> tuple[str, str, str]:
    """Returns (title, meta_description, body_snippet)."""
    if not html:
        return "", "", ""
    # Title
    tm = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = _html.unescape(re.sub(r"\s+", " ", tm.group(1))).strip()[:200] if tm else ""
    # Meta description
    dm = re.search(
        r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]*content=["\'](.*?)["\']',
        html, re.I | re.S,
    ) or re.search(
        r'<meta[^>]+content=["\'](.*?)["\'][^>]*(?:name|property)=["\'](?:description|og:description)["\']',
        html, re.I | re.S,
    )
    meta_desc = _html.unescape(dm.group(1)).strip()[:300] if dm else ""
    # Body text
    body = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav|aside)[^>]*>.*?</\1>", " ", html)
    # H1/H2 headings — keep them as they carry the most signal
    headings = " | ".join(
        _html.unescape(re.sub(r"\s+", " ", h)).strip()
        for h in re.findall(r"<h[12][^>]*>(.*?)</h[12]>", body, re.I | re.S)[:5]
    )
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    snippet = shorten(body, MAX_HTML_SEND, placeholder="…")
    combined_body = (f"Headings: {headings}\n\n" if headings else "") + snippet
    return title, meta_desc, combined_body

# ─── AI classification ────────────────────────────────────────────────────────
_CONTENT_TYPES = ["Seasonal", "Trend", "Shopping", "Lifestyle", "Recipe", "DIY", "Other"]

def _ask_ai(pin: dict) -> dict | None:
    """
    Sends pin metadata + blog post content to OpenRouter.
    Returns dict with keys: content_type, category, description, post_score,
                            seasonal_context, best_posting_days (list[int])
    or None on error.
    """
    if not OPENROUTER_KEY:
        print("[ai] ⚠ OPENROUTER_API_KEY not set in .env")
        return None

    html_text  = pin.get("link_html", "") or ""
    css_text   = pin.get("link_css",  "") or ""
    js_text    = pin.get("link_js",   "") or ""

    blog_title, blog_meta, blog_body = _extract_text_from_html(html_text)

    # Also peek at CSS/JS for extra class/text signals (brief snippet)
    css_snippet = shorten(re.sub(r"\s+", " ", css_text), 300, placeholder="…") if css_text else ""
    js_snippet  = shorten(re.sub(r"\s+", " ", js_text), 300, placeholder="…") if js_text else ""

    schedule_window = (
        f"{SCHEDULE_DATES[0].strftime('%B %d')} – {SCHEDULE_DATES[-1].strftime('%B %d, %Y')} "
        f"(day 1 = {SCHEDULE_DATES[0].strftime('%A %B %d')})"
    )

    system = (
        "You are a Pinterest content strategist. "
        "Analyze the provided pin + blog post data and respond with a JSON object only — "
        "no markdown, no explanation. Schema:\n"
        "{\n"
        '  "content_type": "Seasonal|Trend|Shopping|Lifestyle|Recipe|DIY|Other",\n'
        '  "category": "1-3 word theme (e.g. Summer Fashion, Home Decor)",\n'
        '  "description": "1-2 sentence social media caption for this pin",\n'
        '  "post_score": <int 1-10, relevance for posting in the schedule window>,\n'
        '  "seasonal_context": "season/holiday/occasion or empty string",\n'
        '  "best_posting_days": [<day numbers 1-15 best suited, up to 5>]\n'
        "}\n\n"
        f"Posting window: {schedule_window}. "
        "Score 9-10 = must post now, very timely. "
        "Score 7-8 = good fit for this window. "
        "Score 5-6 = neutral/evergreen. "
        "Score 1-4 = not relevant for this window."
    )

    user_parts = [
        f"Pin title: {pin.get('title', '')[:200]}",
        f"Pin description: {pin.get('description', '')[:300]}",
        f"Board: {pin.get('board_name', '')[:100]}",
        f"Pinner: {pin.get('pinner_username', '')[:80]}",
    ]
    if blog_title:   user_parts.append(f"Blog post title: {blog_title}")
    if blog_meta:    user_parts.append(f"Blog meta description: {blog_meta}")
    if blog_body:    user_parts.append(f"Blog post content:\n{blog_body}")
    if css_snippet:  user_parts.append(f"CSS snippet: {css_snippet}")
    if js_snippet:   user_parts.append(f"JS snippet: {js_snippet}")

    try:
        resp = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENROUTER_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user",   "content": "\n".join(user_parts)},
                ],
                "max_tokens": 300,
                "temperature": 0.2,
            },
            timeout=30,
        )
        resp.raise_for_status()
        raw = resp.json()["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences if present
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.S).strip()
        data = json.loads(raw)
        # Validate / normalise
        ct = data.get("content_type", "Other")
        if ct not in _CONTENT_TYPES:
            ct = "Other"
        days = data.get("best_posting_days", [])
        if isinstance(days, list):
            days = [int(d) for d in days if isinstance(d, (int, float)) and 1 <= int(d) <= DAYS_AHEAD]
        else:
            days = []
        return {
            "content_type":     ct,
            "category":         str(data.get("category", ""))[:100],
            "description":      str(data.get("description", ""))[:500],
            "post_score":       max(1, min(10, int(data.get("post_score", 5)))),
            "seasonal_context": str(data.get("seasonal_context", ""))[:200],
            "best_posting_days": ",".join(str(d) for d in sorted(set(days))),
        }
    except Exception as e:
        print(f"[ai] error: {e}")
        return None

# ─── SQLite path ──────────────────────────────────────────────────────────────
def _fetch_eligible_sqlite(limit: int) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    _ensure_table_sqlite(con)
    rows = con.execute(f"""
        SELECT p.id, p.pin_url, p.title, p.description, p.pinner_username,
               p.board_name, p.link, p.link_html, p.link_css, p.link_js
        FROM   pins p
        WHERE  p.link_download_status = 'Done'
          AND  p.link_html IS NOT NULL AND p.link_html != ''
          AND  p.id NOT IN (SELECT pin_id FROM pin_content_analysis)
        ORDER  BY p.rowid DESC
        LIMIT  {int(limit)}
    """).fetchall()
    con.close()
    return [dict(r) for r in rows]

def _save_result_sqlite(pin_id: str, pin: dict, result: dict, dry_run: bool):
    if dry_run:
        return
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT OR REPLACE INTO pin_content_analysis
        (pin_id, pin_url, pinner_username, board_name,
         content_type, category, description, post_score,
         seasonal_context, best_posting_days)
        VALUES (?,?,?,?,?,?,?,?,?,?)
    """, (
        pin_id,
        pin.get("pin_url", ""),
        pin.get("pinner_username", ""),
        pin.get("board_name", ""),
        result["content_type"],
        result["category"],
        result["description"],
        result["post_score"],
        result["seasonal_context"],
        result["best_posting_days"],
    ))
    con.commit()
    con.close()

# ─── MySQL path ────────────────────────────────────────────────────────────────
def _fetch_eligible_mysql(limit: int) -> list[dict]:
    conn = _get_mysql()
    if not conn:
        return []
    _ensure_table_mysql(conn)
    cur = conn.cursor()
    cur.execute(f"""
        SELECT p.id, p.pin_url, p.title, p.description, p.pinner_username,
               p.board_name, p.link, p.link_html, p.link_css, p.link_js
        FROM   pins p
        WHERE  p.link_download_status = 'Done'
          AND  p.link_html IS NOT NULL AND p.link_html != ''
          AND  p.id NOT IN (SELECT pin_id FROM pin_content_analysis)
        ORDER  BY p.id DESC
        LIMIT  %s
    """, (int(limit),))
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]

def _save_result_mysql(pin_id: str, pin: dict, result: dict, dry_run: bool):
    if dry_run:
        return
    conn = _get_mysql()
    if not conn:
        return
    conn.cursor().execute("""
        INSERT INTO pin_content_analysis
        (pin_id, pin_url, pinner_username, board_name,
         content_type, category, description, post_score,
         seasonal_context, best_posting_days)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            content_type=VALUES(content_type),
            category=VALUES(category),
            description=VALUES(description),
            post_score=VALUES(post_score),
            seasonal_context=VALUES(seasonal_context),
            best_posting_days=VALUES(best_posting_days),
            scanned_at=CURRENT_TIMESTAMP(3)
    """, (
        pin_id,
        pin.get("pin_url", ""),
        pin.get("pinner_username", ""),
        pin.get("board_name", ""),
        result["content_type"],
        result["category"],
        result["description"],
        result["post_score"],
        result["seasonal_context"],
        result["best_posting_days"],
    ))

# ─── Schedule builder ─────────────────────────────────────────────────────────
_TYPE_EMOJI = {
    "Seasonal":  "🌸",
    "Trend":     "🔥",
    "Shopping":  "🛍️",
    "Lifestyle": "✨",
    "Recipe":    "🍴",
    "DIY":       "🔨",
    "Other":     "📌",
}

def _build_schedule(source: str) -> dict[int, list[dict]]:
    """Load classified pins and distribute across 15 days."""
    if source == "mysql":
        conn = _get_mysql()
        if conn:
            cur = conn.cursor()
            cur.execute("""
                SELECT pin_id, pin_url, pinner_username, board_name,
                       content_type, category, description, post_score,
                       seasonal_context, best_posting_days
                FROM   pin_content_analysis
                WHERE  post_score >= 5
                ORDER  BY post_score DESC
                LIMIT  500
            """)
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        else:
            rows = []
    else:
        con = sqlite3.connect(DB_PATH)
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute("""
            SELECT * FROM pin_content_analysis WHERE post_score >= 5
            ORDER BY post_score DESC LIMIT 500
        """).fetchall()]
        con.close()

    # Build day→pins mapping
    calendar: dict[int, list[dict]] = {d: [] for d in range(1, DAYS_AHEAD + 1)}
    used_per_day: dict[int, set] = {d: set() for d in range(1, DAYS_AHEAD + 1)}

    for row in rows:
        days_str = row.get("best_posting_days") or ""
        days = [int(x) for x in days_str.split(",") if x.strip().isdigit()]
        if not days:
            # Spread evergreen across odd days
            days = [d for d in range(1, DAYS_AHEAD + 1, 2)]
        for day in days:
            if day in calendar and len(calendar[day]) < 5 and row["pin_id"] not in used_per_day[day]:
                calendar[day].append(row)
                used_per_day[day].add(row["pin_id"])
    return calendar

def _print_schedule(source: str):
    calendar = _build_schedule(source)
    print(f"\n{'═'*70}")
    print(f"  📅  15-DAY POST SCHEDULE  ({SCHEDULE_DATES[0]} → {SCHEDULE_DATES[-1]})")
    print(f"{'═'*70}")
    for i, dt in enumerate(SCHEDULE_DATES, 1):
        pins = calendar.get(i, [])
        print(f"\n  Day {i:>2} — {dt.strftime('%A, %B %d')}")
        if not pins:
            print("         (no suggestions yet — run scanner to classify more pins)")
        for p in pins:
            emoji = _TYPE_EMOJI.get(p["content_type"], "📌")
            score = p["post_score"]
            cat   = p["category"] or p["content_type"]
            desc  = shorten(p["description"] or "(no description)", 70, placeholder="…")
            print(f"    {emoji} [{score}/10] [{cat}] {desc}")
    print(f"\n{'═'*70}\n")

# ─── Main scan pass ────────────────────────────────────────────────────────────
_write_lock = threading.Lock()

def _run_pass(source: str, limit: int, workers: int, dry_run: bool) -> int:
    if source == "mysql":
        pins = _fetch_eligible_mysql(limit)
    else:
        pins = _fetch_eligible_sqlite(limit)

    if not pins:
        return 0

    print(f"[planner] {len(pins)} pins to classify (source={source})")
    done = 0

    def handle(pin: dict):
        nonlocal done
        pin_id = str(pin.get("id", ""))
        t0 = time.time()
        result = _ask_ai(pin)
        elapsed = time.time() - t0
        if result:
            with _write_lock:
                if source == "mysql":
                    _save_result_mysql(pin_id, pin, result, dry_run)
                else:
                    _save_result_sqlite(pin_id, pin, result, dry_run)
                done += 1
            emoji = _TYPE_EMOJI.get(result["content_type"], "📌")
            label = f"[{result['content_type']}] [{result['category']}] score={result['post_score']}"
            print(f"  {emoji} {pin.get('pinner_username','?')} | {label} | {elapsed:.1f}s"
                  + (" (dry)" if dry_run else ""))
        else:
            print(f"  ✗ {pin.get('pinner_username','?')} — AI failed ({elapsed:.1f}s)")

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(handle, p) for p in pins]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                print(f"[planner] thread error: {e}")

    return done

# ─── CLI ──────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="AI pin classifier + 15-day post planner")
    ap.add_argument("--source",   choices=["mysql", "sqlite", "auto"], default="auto")
    ap.add_argument("--workers",  type=int, default=10)
    ap.add_argument("--limit",    type=int, default=200)
    ap.add_argument("--once",     action="store_true")
    ap.add_argument("--dry-run",  action="store_true")
    ap.add_argument("--schedule", action="store_true", help="Print 15-day calendar and exit")
    ap.add_argument("--poll-minutes", type=int, default=POLL_MINUTES)
    args = ap.parse_args()

    # Resolve source
    source = args.source
    if source == "auto":
        source = "mysql" if _mysql_available() else "sqlite"
    elif source == "mysql" and not _mysql_available():
        print("[planner] ✗ MySQL not reachable — check .env MYSQL_PASSWORD. Use --source sqlite.")
        sys.exit(1)
    print(f"[planner] source={source}, model={AI_MODEL}")

    if args.schedule:
        _print_schedule(source)
        return

    pass_num = 0
    while True:
        pass_num += 1
        print(f"\n[planner] ── Pass #{pass_num} ── {time.strftime('%Y-%m-%d %H:%M:%S')}")
        classified = _run_pass(source, args.limit, args.workers, args.dry_run)
        print(f"[planner] Pass #{pass_num} done — {classified} pins classified")

        if classified > 0:
            _print_schedule(source)

        if args.once:
            break
        print(f"[planner] Idle {args.poll_minutes} min — waiting for new downloaded pins…")
        time.sleep(args.poll_minutes * 60)

if __name__ == "__main__":
    main()
