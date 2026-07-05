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
    python 15_post_planner.py --from 2025-07-01 --to 2025-12-12 --created-only --once
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
    content_type_generated_by_ia_scan TEXT,
    category_generated_by_ia_scan TEXT,
    description_generated_by_ia_scan TEXT,
    post_score_generated_by_ia_scan INTEGER DEFAULT 0,
    seasonal_context_generated_by_ia_scan TEXT,
    best_posting_days_generated_by_ia_scan TEXT,
    posting_window_generated_by_ia_scan TEXT,
    keywords_generated_by_ia_scan TEXT,
    hook_generated_by_ia_scan TEXT,
    caption_generated_by_ia_scan TEXT,
    target_audience_generated_by_ia_scan TEXT,
    monetization_angle_generated_by_ia_scan TEXT,
    content_json_generated_by_ia_scan TEXT,
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
    content_type_generated_by_ia_scan VARCHAR(50),
    category_generated_by_ia_scan VARCHAR(100),
    description_generated_by_ia_scan TEXT,
    post_score_generated_by_ia_scan TINYINT DEFAULT 0,
    seasonal_context_generated_by_ia_scan VARCHAR(200),
    best_posting_days_generated_by_ia_scan VARCHAR(100),
    posting_window_generated_by_ia_scan VARCHAR(255),
    keywords_generated_by_ia_scan TEXT,
    hook_generated_by_ia_scan VARCHAR(255),
    caption_generated_by_ia_scan TEXT,
    target_audience_generated_by_ia_scan VARCHAR(255),
    monetization_angle_generated_by_ia_scan VARCHAR(100),
    content_json_generated_by_ia_scan JSON,
    scanned_at        DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
    INDEX idx_type    (content_type),
    INDEX idx_score   (post_score)
) CHARACTER SET utf8mb4
"""

EXTRA_SQLITE_COLUMNS = {
    "content_type_generated_by_ia_scan": "TEXT",
    "category_generated_by_ia_scan": "TEXT",
    "description_generated_by_ia_scan": "TEXT",
    "post_score_generated_by_ia_scan": "INTEGER DEFAULT 0",
    "seasonal_context_generated_by_ia_scan": "TEXT",
    "best_posting_days_generated_by_ia_scan": "TEXT",
    "posting_window_generated_by_ia_scan": "TEXT",
    "keywords_generated_by_ia_scan": "TEXT",
    "hook_generated_by_ia_scan": "TEXT",
    "caption_generated_by_ia_scan": "TEXT",
    "target_audience_generated_by_ia_scan": "TEXT",
    "monetization_angle_generated_by_ia_scan": "TEXT",
    "content_json_generated_by_ia_scan": "TEXT",
}

EXTRA_MYSQL_COLUMNS = {
    "content_type_generated_by_ia_scan": "VARCHAR(50)",
    "category_generated_by_ia_scan": "VARCHAR(100)",
    "description_generated_by_ia_scan": "TEXT",
    "post_score_generated_by_ia_scan": "TINYINT DEFAULT 0",
    "seasonal_context_generated_by_ia_scan": "VARCHAR(200)",
    "best_posting_days_generated_by_ia_scan": "VARCHAR(100)",
    "posting_window_generated_by_ia_scan": "VARCHAR(255)",
    "keywords_generated_by_ia_scan": "TEXT",
    "hook_generated_by_ia_scan": "VARCHAR(255)",
    "caption_generated_by_ia_scan": "TEXT",
    "target_audience_generated_by_ia_scan": "VARCHAR(255)",
    "monetization_angle_generated_by_ia_scan": "VARCHAR(100)",
    "content_json_generated_by_ia_scan": "JSON",
}

def _ensure_table_sqlite(con):
    con.execute(CREATE_SQLITE)
    cols = {r[1] for r in con.execute("PRAGMA table_info(pin_content_analysis)").fetchall()}
    for name, sql_type in EXTRA_SQLITE_COLUMNS.items():
        if name not in cols:
            con.execute(f"ALTER TABLE pin_content_analysis ADD COLUMN {name} {sql_type}")
    con.commit()

def _ensure_table_mysql(conn):
    cur = conn.cursor()
    cur.execute(CREATE_MYSQL)
    cur.execute("""
        SELECT COLUMN_NAME
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
          AND TABLE_NAME = 'pin_content_analysis'
    """)
    cols = {r[0] for r in cur.fetchall()}
    for name, sql_type in EXTRA_MYSQL_COLUMNS.items():
        if name not in cols:
            cur.execute(f"ALTER TABLE pin_content_analysis ADD COLUMN {name} {sql_type}")

def _mysql_pin_created_expr(alias: str = "p") -> str:
    return (
        f"COALESCE("
        f"STR_TO_DATE({alias}.created_at, '%%a, %%d %%b %%Y %%H:%%i:%%s +0000'),"
        f"STR_TO_DATE({alias}.created_at, '%%Y-%%m-%%dT%%H:%%i:%%s.%%fZ'),"
        f"STR_TO_DATE({alias}.created_at, '%%Y-%%m-%%d %%H:%%i:%%s'),"
        f"STR_TO_DATE({alias}.created_at, '%%Y-%%m-%%d')"
        f")"
    )

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

def _as_text(value, max_len: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)
    return str(value)[:max_len]

def _as_csv(value, max_len: int = 1000) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        value = ", ".join(str(v) for v in value if str(v).strip())
    return str(value)[:max_len]

def _prior_json_examples_text(pin: dict, limit: int = 3) -> str:
    examples = pin.get("prior_json_examples") or []
    if not examples:
        return ""
    compact = []
    for ex in examples[:limit]:
        try:
            if isinstance(ex, str):
                obj = json.loads(ex)
            else:
                obj = ex
            compact.append(obj)
        except Exception:
            compact.append(str(ex)[:1000])
    return json.dumps(compact, ensure_ascii=False)[:6000]

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
        '  "seasonal_context": "season/holiday/occasion, or evergreen if not seasonal",\n'
        '  "best_posting_days": [<day numbers 1-15 best suited, up to 5>]\n'
        "}\n\n"
        f"Posting window: {schedule_window}. "
        "Score 9-10 = must post now, very timely. "
        "Score 7-8 = good fit for this window. "
        "Score 5-6 = neutral/evergreen. "
        "Score 1-4 = not relevant for this window."
    )

    system += (
        "\n\nAdditional required fields for the same JSON object:\n"
        '  "posting_window": {"start": "YYYY-MM-DD or empty", "end": "YYYY-MM-DD or empty", "peak_start": "YYYY-MM-DD or empty", "peak_end": "YYYY-MM-DD or empty", "reason": ""},\n'
        '  "keywords": ["Pinterest SEO keyword"],\n'
        '  "hook": "short high-click hook",\n'
        '  "caption": "optimized Pinterest caption",\n'
        '  "target_audience": "who should see this",\n'
        '  "monetization_angle": "recipe traffic|affiliate|email signup|product|ad traffic|none",\n'
        '  "content_angle": "beginner tutorial|holiday idea|quick dinner|gift idea|etc",\n'
        '  "evergreen_score": <int 1-10>,\n'
        '  "seasonal_score": <int 1-10>,\n'
        '  "competition_level": "low|medium|high",\n'
        '  "source_content_type": "recipe|travel|job|career|how_to|listicle|product|fashion|beauty|home|finance|health|fitness|parenting|education|business|technology|diy|craft|news|entertainment|other",\n'
        '  "structured_content": {\n'
        '    "detected_niche": "",\n'
        '    "content_format": "recipe|guide|listicle|review|comparison|tutorial|itinerary|job_post|career_advice|product_page|outfit|news|story|other",\n'
        '    "title": "", "summary": "",\n'
        '    "main_entities": [],\n'
        '    "key_points": [],\n'
        '    "facts_to_preserve": [],\n'
        '    "warnings_or_constraints": [],\n'
        '    "source_sections": [{"heading": "", "points": []}],\n'
        '    "type_specific": {},\n'
        '    "rewrite_brief": {"suggested_title": "", "meta_description": "", "slug": "", "tone": "", "unique_angle": "", "search_intent": "", "recommended_word_count": ""}\n'
        "  }\n"
        "Dynamic type_specific examples: "
        "Recipe: ingredients, instructions, prep_time, cook_time, servings, equipment, nutrition, tips, substitutions, storage. "
        "Travel: destination, itinerary, attractions, costs, best_time_to_visit, transportation, lodging, map_points, safety_tips, packing_list. "
        "Job/Career: job_title, company, location, salary, requirements, responsibilities, skills, application_steps, deadlines, remote_policy. "
        "Product/Shopping: product_name, brand, price, features, pros, cons, use_cases, alternatives, buying_guide, affiliate_angle. "
        "Fashion/Beauty: outfit_items, colors, occasion, body_fit_notes, styling_steps, products_used, seasonal_fit. "
        "Finance/Business: topic, strategy_steps, risks, numbers_to_preserve, tools, examples, compliance_notes. "
        "DIY/Craft/Home: materials, tools, steps, measurements, difficulty, time_required, safety_notes. "
        "Never leave seasonal_context empty: use a season/holiday/occasion when relevant, otherwise use evergreen. "
        "For seasonal content, always fill posting_window with the best period to publish before or around the event. "
        "The structured_content.type_specific object must adapt to the actual post. Do not force recipe fields onto non-recipe posts. "
        "Extract structured_content from the destination page, not only from the Pinterest caption."
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
    prior_examples = _prior_json_examples_text(pin)
    if prior_examples:
        user_parts.append(
            "Previous similar generated JSON examples from this database:\n"
            f"{prior_examples}\n"
            "Use these examples only as schema/style guidance when relevant. "
            "If this post is a different niche or needs a better structure, create the best dynamic structure for this post."
        )

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
                "max_tokens": 1800,
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
        post_score = max(1, min(10, int(data.get("post_score", 5))))
        posting_window = data.get("posting_window") if isinstance(data.get("posting_window"), dict) else {}
        structured_content = data.get("structured_content") if isinstance(data.get("structured_content"), dict) else {}
        seasonal_context = _as_text(data.get("seasonal_context", ""), 200).strip() or "evergreen"
        full_generated = {
            "schema_version": "dynamic_ia_scan_v2",
            "schema_mode": "dynamic_by_detected_niche",
            "content_type": ct,
            "category": data.get("category", ""),
            "description": data.get("description", ""),
            "post_score": post_score,
            "seasonal_context": seasonal_context,
            "best_posting_days": sorted(set(days)),
            "posting_window": posting_window,
            "keywords": data.get("keywords", []),
            "hook": data.get("hook", ""),
            "caption": data.get("caption", ""),
            "target_audience": data.get("target_audience", ""),
            "monetization_angle": data.get("monetization_angle", ""),
            "content_angle": data.get("content_angle", ""),
            "evergreen_score": data.get("evergreen_score", ""),
            "seasonal_score": data.get("seasonal_score", ""),
            "competition_level": data.get("competition_level", ""),
            "source_content_type": data.get("source_content_type", ""),
            "structured_content": structured_content,
        }
        return {
            "content_type":     ct,
            "category":         str(data.get("category", ""))[:100],
            "description":      str(data.get("description", ""))[:500],
            "post_score":       post_score,
            "seasonal_context": seasonal_context,
            "best_posting_days": ",".join(str(d) for d in sorted(set(days))),
            "posting_window":    _as_text(posting_window, 255),
            "keywords":          _as_csv(data.get("keywords", []), 2000),
            "hook":              _as_text(data.get("hook", ""), 255),
            "caption":           _as_text(data.get("caption", ""), 2000),
            "target_audience":   _as_text(data.get("target_audience", ""), 255),
            "monetization_angle": _as_text(data.get("monetization_angle", ""), 100),
            "content_json":      json.dumps(full_generated, ensure_ascii=False),
        }
    except Exception as e:
        print(f"[ai] error: {e}")
        return None

# ─── SQLite path ──────────────────────────────────────────────────────────────
def _fetch_eligible_sqlite(limit: int) -> list[dict]:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    _ensure_table_sqlite(con)
    # Check step-14 columns exist (they're added lazily by 14_download_blog_pin_links.py)
    cols = {r[1] for r in con.execute("PRAGMA table_info(pins)").fetchall()}
    if "link_html" not in cols:
        con.close()
        print("[planner] ℹ  No link_html column in local sortpin.db — "
              "step 14 (14_download_blog_pin_links.py) hasn't run on this machine yet.\n"
              "         Run step 14 first to download blog post HTML, "
              "or use --source mysql if another PC has already run it.")
        return []
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
         seasonal_context, best_posting_days,
         content_type_generated_by_ia_scan, category_generated_by_ia_scan,
         description_generated_by_ia_scan, post_score_generated_by_ia_scan,
         seasonal_context_generated_by_ia_scan, best_posting_days_generated_by_ia_scan,
         posting_window_generated_by_ia_scan, keywords_generated_by_ia_scan,
         hook_generated_by_ia_scan, caption_generated_by_ia_scan,
         target_audience_generated_by_ia_scan, monetization_angle_generated_by_ia_scan,
         content_json_generated_by_ia_scan)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
        result["content_type"],
        result["category"],
        result["description"],
        result["post_score"],
        result["seasonal_context"],
        result["best_posting_days"],
        result.get("posting_window", ""),
        result.get("keywords", ""),
        result.get("hook", ""),
        result.get("caption", ""),
        result.get("target_audience", ""),
        result.get("monetization_angle", ""),
        result.get("content_json", ""),
    ))
    con.commit()
    con.close()

# ─── MySQL path ────────────────────────────────────────────────────────────────
def _fetch_eligible_mysql(
    limit: int,
    from_dt: str = "",
    to_dt: str = "",
    created_only: bool = False,
    site_type: str = "",
    pin_id: str = "",
    force: bool = False,
) -> list[dict]:
    conn = _get_mysql()
    if not conn:
        return []
    _ensure_table_mysql(conn)
    created_expr = _mysql_pin_created_expr("p")
    where = [
        "p.link_download_status = 'Done'",
        "p.link_html IS NOT NULL AND p.link_html != ''",
    ]
    if not force:
        where.append("p.id NOT IN (SELECT pin_id FROM pin_content_analysis)")
    params: list = []
    if created_only:
        where.append("p.pin_type = 'created'")
    if pin_id:
        where.append("p.id = %s")
        params.append(pin_id)
    if from_dt:
        where.append(f"{created_expr} >= %s")
        params.append(from_dt)
    if to_dt:
        where.append(f"{created_expr} <= %s")
        params.append(to_dt + " 23:59:59")
    join_sql = ""
    if site_type:
        join_sql = "LEFT JOIN pinners pi ON pi.username = p.pinner_username"
        if site_type == "blank":
            where.append("(pi.site_type IS NULL OR pi.site_type = '')")
        else:
            where.append("pi.site_type = %s")
            params.append(site_type)
    where_sql = " AND ".join(where)
    cur = conn.cursor()
    cur.execute(f"""
        SELECT p.id, p.pin_url, p.title, p.description, p.pinner_username,
               p.board_name, p.link, p.link_html, p.link_css, p.link_js
        FROM   pins p
        {join_sql}
        WHERE  {where_sql}
        ORDER  BY {created_expr} DESC, p.id DESC
        LIMIT  %s
    """, params + [int(limit)])
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
         seasonal_context, best_posting_days,
         content_type_generated_by_ia_scan, category_generated_by_ia_scan,
         description_generated_by_ia_scan, post_score_generated_by_ia_scan,
         seasonal_context_generated_by_ia_scan, best_posting_days_generated_by_ia_scan,
         posting_window_generated_by_ia_scan, keywords_generated_by_ia_scan,
         hook_generated_by_ia_scan, caption_generated_by_ia_scan,
         target_audience_generated_by_ia_scan, monetization_angle_generated_by_ia_scan,
         content_json_generated_by_ia_scan)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON DUPLICATE KEY UPDATE
            content_type=VALUES(content_type),
            category=VALUES(category),
            description=VALUES(description),
            post_score=VALUES(post_score),
            seasonal_context=VALUES(seasonal_context),
            best_posting_days=VALUES(best_posting_days),
            content_type_generated_by_ia_scan=VALUES(content_type_generated_by_ia_scan),
            category_generated_by_ia_scan=VALUES(category_generated_by_ia_scan),
            description_generated_by_ia_scan=VALUES(description_generated_by_ia_scan),
            post_score_generated_by_ia_scan=VALUES(post_score_generated_by_ia_scan),
            seasonal_context_generated_by_ia_scan=VALUES(seasonal_context_generated_by_ia_scan),
            best_posting_days_generated_by_ia_scan=VALUES(best_posting_days_generated_by_ia_scan),
            posting_window_generated_by_ia_scan=VALUES(posting_window_generated_by_ia_scan),
            keywords_generated_by_ia_scan=VALUES(keywords_generated_by_ia_scan),
            hook_generated_by_ia_scan=VALUES(hook_generated_by_ia_scan),
            caption_generated_by_ia_scan=VALUES(caption_generated_by_ia_scan),
            target_audience_generated_by_ia_scan=VALUES(target_audience_generated_by_ia_scan),
            monetization_angle_generated_by_ia_scan=VALUES(monetization_angle_generated_by_ia_scan),
            content_json_generated_by_ia_scan=VALUES(content_json_generated_by_ia_scan),
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
        result["content_type"],
        result["category"],
        result["description"],
        result["post_score"],
        result["seasonal_context"],
        result["best_posting_days"],
        result.get("posting_window", ""),
        result.get("keywords", ""),
        result.get("hook", ""),
        result.get("caption", ""),
        result.get("target_audience", ""),
        result.get("monetization_angle", ""),
        result.get("content_json", ""),
    ))

# ─── Schedule builder ─────────────────────────────────────────────────────────
def _attach_prior_json_examples_mysql(pins: list[dict], limit_per_pin: int = 3) -> None:
    if not pins:
        return
    conn = _get_mysql()
    if not conn:
        return
    _ensure_table_mysql(conn)
    cur = conn.cursor()
    for pin in pins:
        pinner = (pin.get("pinner_username") or "").strip()
        board = (pin.get("board_name") or "").strip()
        pin_id = str(pin.get("id", "") or "")
        where = [
            "content_json_generated_by_ia_scan IS NOT NULL",
            "content_json_generated_by_ia_scan <> ''",
        ]
        params: list = []
        if pin_id:
            where.append("pin_id <> %s")
            params.append(pin_id)
        similar = []
        if pinner:
            similar.append("pinner_username = %s")
            params.append(pinner)
        if board:
            similar.append("board_name = %s")
            params.append(board)
        if not similar:
            pin["prior_json_examples"] = []
            continue
        where.append("(" + " OR ".join(similar) + ")")
        cur.execute(f"""
            SELECT content_json_generated_by_ia_scan
            FROM pin_content_analysis
            WHERE {" AND ".join(where)}
            ORDER BY scanned_at DESC
            LIMIT %s
        """, params + [limit_per_pin])
        pin["prior_json_examples"] = [r[0] for r in cur.fetchall() if r and r[0]]

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

def _run_pass(
    source: str,
    limit: int,
    workers: int,
    dry_run: bool,
    from_dt: str = "",
    to_dt: str = "",
    created_only: bool = False,
    site_type: str = "",
    pin_id: str = "",
    force: bool = False,
) -> int:
    if source == "mysql":
        pins = _fetch_eligible_mysql(limit, from_dt, to_dt, created_only, site_type, pin_id, force)
        _attach_prior_json_examples_mysql(pins)
    else:
        if from_dt or to_dt or created_only:
            print("[planner] date range / --created-only filters are only supported for --source mysql")
        pins = _fetch_eligible_sqlite(limit)

    if not pins:
        return 0

    extra = ""
    if from_dt or to_dt:
        extra += f", date={from_dt or '...'} to {to_dt or '...'}"
    if created_only:
        extra += ", created-only"
    if site_type:
        extra += f", site={site_type}"
    if pin_id:
        extra += f", pin_id={pin_id}"
    if force:
        extra += ", force"
    print(f"[planner] {len(pins)} pins to classify (source={source}{extra})")
    for pin in pins:
        html_len = len(pin.get("link_html") or "")
        css_len = len(pin.get("link_css") or "")
        js_len = len(pin.get("link_js") or "")
        print(
            f"[planner] pin {pin.get('id')} Step 14 content: "
            f"html={html_len:,} chars, css={css_len:,} chars, js={js_len:,} chars"
        )
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
            prior_count = len(pin.get("prior_json_examples") or [])
            print(f"  {emoji} {pin.get('pinner_username','?')} | {label} | prior_json_examples={prior_count} | {elapsed:.1f}s"
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
    ap.add_argument("--from", dest="from_dt", default="", help="Only classify pins created on/after YYYY-MM-DD (MySQL)")
    ap.add_argument("--to", dest="to_dt", default="", help="Only classify pins created on/before YYYY-MM-DD (MySQL)")
    ap.add_argument("--created-only", action="store_true", help="Only classify pins with pin_type='created' (MySQL)")
    ap.add_argument("--site-type", default="", help="Only classify pinners with this pinners.site_type value; use blank for empty site_type (MySQL)")
    ap.add_argument("--pin-id", default="", help="Only classify one pin id (MySQL)")
    ap.add_argument("--force", action="store_true", help="Re-scan pins even if they already exist in pin_content_analysis")
    ap.add_argument("--no-calendar", action="store_true", help="Do not print the global 15-day schedule after scanning")
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
        classified = _run_pass(
            source,
            args.limit,
            args.workers,
            args.dry_run,
            args.from_dt,
            args.to_dt,
            args.created_only,
            args.site_type,
            args.pin_id,
            args.force,
        )
        print(f"[planner] Pass #{pass_num} done — {classified} pins classified")

        if classified > 0 and not args.no_calendar:
            _print_schedule(source)

        if args.once:
            break
        print(f"[planner] Idle {args.poll_minutes} min — waiting for new downloaded pins…")
        time.sleep(args.poll_minutes * 60)

if __name__ == "__main__":
    main()
