"""
STEP 14 - Blog Pin Destination-Link Downloader
================================================

Standalone bot. For every pin in `pins` whose PINNER's website is a confirmed
"blog" (see below), downloads the pin's outbound destination link (the
`link` column — the real external URL the pin points to, NOT `pin_url` which
is just the Pinterest pin page) and saves a full snapshot — page HTML, every
linked external stylesheet's CSS, and every linked external script's JS
(plus any inline <style>/<script> blocks already in the markup) — into new
columns on the `pins` table. Pins belonging to a non-blog (or not-yet-known)
pinner are skipped entirely; nothing is written for them.

"Is this pinner's website a blog?" — per-pinner, not per-domain:
  A `site_type` column holds the Google Sheet's "websites" tab classification
  (`get_websites` action — same data step 10/13 write), matched purely by
  username (the Sheet's `id` column = pinner username, no domain-guessing
  needed). A pinner counts as "blog" when `site_type` contains "blog"
  (case-insensitive) — same substring check used by magic_scroll.py's
  --blog-only.

Two pin sources (--source, default mysql):
  --source mysql     (default) The shared cloud MySQL database 8_sync_to_mysql.py pushes
                     every PC's local data into — pins/pinners from ALL PCs,
                     not just this one, since local sortpin.db only has what
                     THIS machine has scraped. `site_type` (the real string —
                     Blog/Store/Link-in-Bio/General Website/etc., not just a
                     blog flag) is written straight into MySQL's `pinners`
                     table every pass via a plain UPDATE keyed by username —
                     unlike 8_sync_to_mysql.py's own pinners sync, which uses
                     INSERT IGNORE (insert-only, never updates an existing
                     row), so a later classification would otherwise never
                     reach MySQL. Eligible pins are then found there directly
                     with a SQL JOIN (pins.pinner_username = pinners.username
                     AND pinners.site_type LIKE '%blog%'), and the 5 download
                     columns below are added to MySQL's `pins` table and
                     written there too — so progress is shared across every
                     PC that runs this script, not siloed per machine. Claims
                     a batch (`link_download_status='Running'`) the moment
                     it's selected, as best-effort dedup against another PC
                     running the same pass concurrently — not a hard lock; a
                     crashed run can leave some pins stuck at 'Running',
                     cleared by rerunning with --retry-failed. Exits if
                     .env's MYSQL_PASSWORD isn't configured/reachable.
  --source sqlite    This PC's local sortpin.db only. `site_type` is synced
                     into the local `pinners` table every pass; if the Sheet
                     is unreachable, falls back to the local `scraped_websites`
                     table (status='done' rows, matched by domain — mirrors
                     magic_scroll.py's --blog-only fallback).
  --source auto      mysql if .env has a working MYSQL_PASSWORD, else sqlite.

Same unique LINK is often pinned many times (repins, multiple boards) — this
script de-duplicates by `link` before downloading, fetches each unique link
once, then writes that one result to every pin row sharing it. Saves real
bandwidth/CPU on popular posts instead of re-downloading identical pages.

Threaded for max throughput: a ThreadPoolExecutor (--workers, default 80)
downloads many destination pages + their assets in parallel; each completed
unique link is written to the DB IMMEDIATELY (not buffered/batched at the
end) via a lock-guarded UPSERT, so progress is durable even if interrupted.

New columns written to `pins` (mirrors step 13's status-column pattern):
    link_download_status     Done / Blocked (Cloudflare) / Blocked (HTTP ..) /
                              Failed (...) / Failed (no link)
    link_downloaded_at       timestamp of the attempt
    link_html                destination page's raw HTML (capped)
    link_css                 inline <style> blocks + every linked .css file's
                              contents, concatenated
    link_js                  inline <script> blocks + every linked .js file's
                              contents, concatenated

Cloudflare is detected specifically (not lumped into a generic block reason):
response headers (cf-ray / Server: cloudflare) or known challenge-page text
("Checking your browser...", "Just a moment...", etc.) on a 403/429/503,
after first retrying once via curl_cffi's Chrome-impersonation (same bypass
step 13 uses) in case that alone gets past it.

Runs as a bot: downloads the whole eligible backlog, then — once nothing
eligible is left — idles and re-checks every --poll-minutes (default 10) for
newly-scraped pins or newly-confirmed-blog pinners. Never stops on its own;
Ctrl+C to quit.

Run:
  python 14_download_blog_pin_links.py                       # bot mode: mysql by default; poll every 10 min
  python 14_download_blog_pin_links.py --source mysql         # explicit (same as default) — shared cloud DB (all PCs' pins)
  python 14_download_blog_pin_links.py --source sqlite        # force this PC's local sortpin.db only
  python 14_download_blog_pin_links.py --source auto          # mysql if .env configured, else sqlite (old default)
  python 14_download_blog_pin_links.py --once                # single pass, then exit
  python 14_download_blog_pin_links.py --workers 120          # more parallel threads (default 80)
  python 14_download_blog_pin_links.py --poll-minutes 5       # check more often than 10 min
  python 14_download_blog_pin_links.py --retry-failed         # also re-attempt Failed/Blocked/Running pins, not just untried ones
  python 14_download_blog_pin_links.py --limit 20 --once --dry-run   # quick test, no writes
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import socket
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin

import requests

# Force IPv4-only DNS resolution process-wide — same fix as step 13. Some hosts
# publish a dead/blackholed IPv6 (AAAA) record; Python doesn't race v4/v6 like a
# real browser does (RFC 6555 Happy Eyeballs), so it burns the full timeout on
# the dead address before trying the working one. Stripping AAAA at the
# getaddrinfo() level fixes every caller (raw sockets, requests, urllib3).
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "sortpin.db")
# Same cache file step 13 maintains — it's the exact same get_websites payload,
# so either script's last successful pull benefits the other.
SHEET_CACHE_PATH = os.path.join(BASE, "websites_sheet_cache.json")

PIN_COLUMNS = [
    "link_download_status",
    "link_downloaded_at",
    "link_html",
    "link_css",
    "link_js",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

CLOUDFLARE_BODY_MARKERS = [
    "checking your browser before accessing",
    "cf-browser-verification",
    "attention required! | cloudflare",
    "ddos protection by cloudflare",
    "just a moment...",
    "cf-chl-",
    "cf_chl_opt",
]

# Size/time caps — keep sortpin.db sane and bound how long one worker thread
# can be stuck on a single asset-heavy page.
PAGE_HARD_DEADLINE = 20.0      # wall-clock seconds for the destination page itself
ASSET_HARD_DEADLINE = 12.0     # wall-clock seconds per individual css/js file
UNIT_HARD_DEADLINE = 45.0      # wall-clock seconds for the whole assets phase combined
MAX_PAGE_BYTES = 3_000_000
MAX_ASSET_BYTES = 1_500_000
MAX_CSS_FILES = 15
MAX_JS_FILES = 20
MAX_CSS_TOTAL_CHARS = 4_000_000
MAX_JS_TOTAL_CHARS = 6_000_000

LINK_CSS_RE = re.compile(r'<link[^>]+rel=["\']?stylesheet["\']?[^>]*href=["\']([^"\']+)["\']', re.I)
LINK_CSS_RE_ALT = re.compile(r'<link[^>]+href=["\']([^"\']+)["\'][^>]*rel=["\']?stylesheet["\']?', re.I)
SCRIPT_SRC_RE = re.compile(r'<script[^>]+src=["\']([^"\']+)["\']', re.I)
INLINE_STYLE_RE = re.compile(r"<style[^>]*>(.*?)</style>", re.I | re.S)
INLINE_SCRIPT_RE = re.compile(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.I | re.S)


def load_sheet_client():
    try:
        import google_sheets_client as gsc
        return gsc
    except Exception as e:
        print(f"  (Sheet sync unavailable: {e})")
        return None


def _load_sheet_cache():
    try:
        with open(SHEET_CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_sheet_cache(websites: list):
    try:
        with open(SHEET_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(websites, f)
    except Exception:
        pass


# ─── Local DB ────────────────────────────────────────────────────────────────

_db_lock = threading.Lock()


def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    pinner_cols = {r[1] for r in conn.execute("PRAGMA table_info(pinners)").fetchall()}
    if "site_type" not in pinner_cols:
        conn.execute("ALTER TABLE pinners ADD COLUMN site_type TEXT")

    pin_cols = {r[1] for r in conn.execute("PRAGMA table_info(pins)").fetchall()}
    for col in PIN_COLUMNS:
        if col not in pin_cols:
            conn.execute(f"ALTER TABLE pins ADD COLUMN {col} TEXT")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_pins_pinner_username ON pins(pinner_username)")
    conn.commit()
    return conn


def sync_site_types(gsc, cfg, conn) -> int:
    """Pull the Sheet's websites tab `site_type` column (keyed by `id` =
    pinner username) into the local pinners.site_type column, so every pass's
    blog filter runs entirely off the local DB. Same retry+cache resilience
    as step 13 (the 69k-row read occasionally comes back empty with no error
    on a transient hiccup)."""
    if not (gsc and cfg):
        print("  (No Sheet client/config — using whatever site_type is already in pinners locally.)")
        return 0

    data = None
    last_err = None
    for attempt in range(3):
        try:
            data = gsc.post_webapp(cfg, {"action": "get_websites"})
            last_err = None
            if data.get("websites"):
                break
        except Exception as e:
            last_err = e
            data = None
        if attempt < 2:
            time.sleep(3 * (attempt + 1))  # 3s, then 6s

    if data and data.get("websites"):
        _save_sheet_cache(data["websites"])
    else:
        cached = _load_sheet_cache()
        if cached:
            data = {"websites": cached}
            print(f"  (Sheet unreachable/empty for site_type sync — using last cached snapshot ({len(cached)} sites).)")
        else:
            print(f"  (Could not sync site_type from Sheet — {last_err or 'empty response, no cache available'}. "
                  f"Using whatever is already in pinners.site_type.)")
            return 0

    rows = [
        (str(w.get("site_type") or "").strip(), str(w.get("id") or "").strip().lower())
        for w in data.get("websites", [])
        if str(w.get("id") or "").strip()
    ]
    with _db_lock:
        conn.executemany("UPDATE pinners SET site_type=? WHERE lower(username)=?", rows)
        conn.commit()
    print(f"  Synced site_type for {len(rows)} pinner row(s) from the Sheet.")
    return len(rows)


def _extract_domain(url: str) -> str:
    """Same normalization magic_scroll.py's --blog-only fallback uses, so domain
    matching against the local scraped_websites table is consistent project-wide."""
    if not url:
        return ""
    u = url.strip().lower()
    if not u.startswith("http"):
        u = "https://" + u
    try:
        h = urlparse(u).hostname or ""
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


def apply_local_site_type_fallback(conn) -> int:
    """For any pinner whose site_type is still empty after the Sheet sync
    (Sheet unreachable/stale-deployment/not-yet-classified-there), fill it in
    from this PC's own local `scraped_websites` table (written by
    10_domain_quick_scrape_api.py when IT runs here), matched by domain —
    same fallback magic_scroll.py's --blog-only already relies on. This is a
    last resort: the Sheet is the real cross-machine source of truth, but a
    pinner scanned locally on THIS PC shouldn't sit ineligible just because
    the Sheet round-trip failed."""
    try:
        local_rows = conn.execute(
            "SELECT domain, site_type FROM scraped_websites "
            "WHERE domain IS NOT NULL AND status='done' AND site_type IS NOT NULL AND site_type <> ''"
        ).fetchall()
    except sqlite3.OperationalError:
        return 0  # table doesn't exist on this PC yet — nothing to fall back to

    if not local_rows:
        return 0
    local_by_domain = {r[0].strip().lower(): r[1] for r in local_rows if r[0]}

    pending = conn.execute(
        "SELECT username, website_url FROM pinners WHERE (site_type IS NULL OR site_type='') "
        "AND website_url IS NOT NULL AND website_url <> ''"
    ).fetchall()

    updates = []
    for username, website_url in pending:
        dom = _extract_domain(website_url)
        st = local_by_domain.get(dom)
        if st:
            updates.append((st, username))

    if updates:
        with _db_lock:
            conn.executemany("UPDATE pinners SET site_type=? WHERE username=?", updates)
            conn.commit()
    print(f"  (Local fallback: filled site_type for {len(updates)} pinner(s) from this PC's own scraped_websites table.)")
    return len(updates)


def fetch_eligible_units(conn, limit, retry_failed: bool) -> list:
    """Returns a list of {'link': ..., 'pin_ids': [...]} units — one per
    UNIQUE destination link among pins whose pinner is a confirmed blog.
    Prints a diagnostic breakdown every call so '0 eligible' is never a
    mystery (same idiom as step 13's fetch_eligible_rows)."""
    total_pins = conn.execute("SELECT COUNT(*) FROM pins").fetchone()[0]
    blog_pinners = conn.execute(
        "SELECT COUNT(*) FROM pinners WHERE site_type IS NOT NULL AND lower(site_type) LIKE '%blog%'"
    ).fetchone()[0]

    status_clause = "" if retry_failed else "AND (p.link_download_status IS NULL OR p.link_download_status = '')"
    base_where = """
        FROM pins p
        JOIN pinners pn ON pn.username = p.pinner_username
        WHERE pn.site_type IS NOT NULL AND lower(pn.site_type) LIKE '%blog%'
    """
    pins_under_blog = conn.execute(f"SELECT COUNT(*) {base_where}").fetchone()[0]
    pins_under_blog_with_link = conn.execute(
        f"SELECT COUNT(*) {base_where} AND p.link IS NOT NULL AND TRIM(p.link) <> ''"
    ).fetchone()[0]

    sql = f"""
        SELECT p.id, p.link
        {base_where}
        AND p.link IS NOT NULL AND TRIM(p.link) <> ''
        {status_clause}
    """
    rows = [dict(r) for r in conn.execute(sql).fetchall()]

    units = {}
    for r in rows:
        link = r["link"].strip()
        units.setdefault(link, []).append(r["id"])
    unit_list = [{"link": link, "pin_ids": pin_ids} for link, pin_ids in units.items()]

    print(f"  (pins total: {total_pins}, blog pinners: {blog_pinners}, pins under blog pinners: {pins_under_blog}, "
          f"with a link: {pins_under_blog_with_link}, eligible (not yet attempted): {len(rows)}, "
          f"unique links to download: {len(unit_list)}.)")

    if limit:
        unit_list = unit_list[:limit]
    return unit_list


def write_unit_result(conn, pin_ids: list, status: str, html_: str, css_: str, js_: str):
    with _db_lock:
        placeholders = ",".join("?" for _ in pin_ids)
        conn.execute(
            f"""UPDATE pins SET link_download_status=?, link_downloaded_at=?,
                link_html=?, link_css=?, link_js=?
                WHERE id IN ({placeholders})""",
            (status, time.strftime("%Y-%m-%d %H:%M:%S"), html_, css_, js_, *pin_ids),
        )
        conn.commit()


def fetch_website_classifications(gsc, cfg) -> dict:
    """Same Sheet read + retry + cache-fallback as sync_site_types(), but
    returns the full {username_lower: site_type} mapping instead of writing
    it anywhere — shared by both backends below: the sqlite backend still
    writes it into local pinners.site_type (sync_site_types), the MySQL
    backend writes the *real* site_type string into MySQL's pinners.site_type
    column too (sync_site_types_mysql) so it's visible/queryable there, not
    just used internally for filtering."""
    if not (gsc and cfg):
        print("  (No Sheet client/config for site_type.)")
        return {}

    data = None
    last_err = None
    for attempt in range(3):
        try:
            data = gsc.post_webapp(cfg, {"action": "get_websites"})
            last_err = None
            if data.get("websites"):
                break
        except Exception as e:
            last_err = e
            data = None
        if attempt < 2:
            time.sleep(3 * (attempt + 1))

    if data and data.get("websites"):
        _save_sheet_cache(data["websites"])
    else:
        cached = _load_sheet_cache()
        if cached:
            data = {"websites": cached}
            print(f"  (Sheet unreachable/empty — using last cached snapshot ({len(cached)} sites) for site_type.)")
        else:
            print(f"  (Could not reach Sheet for site_type — {last_err or 'empty response, no cache available'}.)")
            return {}

    classifications = {}
    for w in data.get("websites", []):
        uid = str(w.get("id") or "").strip().lower()
        st = str(w.get("site_type") or "").strip()
        if uid and st:
            classifications[uid] = st
    blog_count = sum(1 for st in classifications.values() if "blog" in st.lower())
    print(f"  {len(classifications)} pinner(s) classified on the Sheet ({blog_count} 'blog').")
    return classifications


# ─── MySQL backend ────────────────────────────────────────────────────────────
# Optional alternate pin source: the cloud MySQL database 8_sync_to_mysql.py
# pushes every PC's local sortpin.db into. Local sortpin.db only has whatever
# THIS PC has scraped; MySQL has the union across every PC, so it's a much
# bigger eligible-pin pool. Selected via --source mysql (or --source auto,
# the default, when a working .env MySQL password is present); falls back to
# the local-sqlite backend above when MySQL isn't configured or unreachable.
# Write-back goes straight to MySQL's `pins` table so progress is shared
# across every PC that runs this script, instead of siloed per-machine.

def load_env() -> dict:
    env = {}
    env_path = os.path.join(BASE, ".env")
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


def mysql_configured(env: dict) -> bool:
    pw = env.get("MYSQL_PASSWORD", "")
    return bool(pw) and pw != "YOUR_PASSWORD_HERE"


def get_mysql_connection(env: dict):
    """Same connection routine 8_sync_to_mysql.py uses (same host/db/user
    defaults, same stale-sleeping-connection cleanup) but never sys.exit()s
    on failure — returns None so the caller can fall back to sqlite mode."""
    if not mysql_configured(env):
        return None
    try:
        import mysql.connector
    except ImportError:
        print("  (mysql-connector-python not installed — pip install mysql-connector-python cryptography. Falling back to sqlite mode.)")
        return None

    host = env.get("MYSQL_HOST", "72.61.197.144")
    port = int(env.get("MYSQL_PORT", "3306"))
    db = env.get("MYSQL_DB", "data_pint")
    user = env.get("MYSQL_USER", "data_pint_user")
    password = env.get("MYSQL_PASSWORD", "")

    try:
        con = mysql.connector.connect(
            host=host, port=port, database=db, user=user, password=password,
            charset="utf8mb4", collation="utf8mb4_general_ci",
        )
        cur = con.cursor()
        try:
            cur.execute("SET SESSION TRANSACTION ISOLATION LEVEL READ COMMITTED")
            cur.execute("SET SESSION innodb_lock_wait_timeout = 120")
        finally:
            cur.close()
        print(f"  Connected to cloud MySQL ({host}:{port}, db={db}) — using it as the pin source (all PCs' data).")
        return con
    except Exception as e:
        print(f"  (Could not connect to MySQL — {e}. Falling back to local sqlite mode.)")
        return None


def ensure_mysql_pin_columns(mysql_conn):
    cur = mysql_conn.cursor()
    cur.execute("SHOW COLUMNS FROM pins")
    existing = {row[0] for row in cur.fetchall()}
    type_map = {
        "link_download_status": "VARCHAR(64)",
        "link_downloaded_at": "VARCHAR(32)",
        "link_html": "LONGTEXT",
        "link_css": "LONGTEXT",
        "link_js": "LONGTEXT",
    }
    for col, mysql_type in type_map.items():
        if col not in existing:
            cur.execute(f"ALTER TABLE pins ADD COLUMN `{col}` {mysql_type}")
            mysql_conn.commit()
            print(f"  (MySQL: added column `{col}` to pins.)")
    cur.close()


def ensure_mysql_pinner_columns(mysql_conn):
    cur = mysql_conn.cursor()
    cur.execute("SHOW COLUMNS FROM pinners")
    existing = {row[0] for row in cur.fetchall()}
    if "site_type" not in existing:
        cur.execute("ALTER TABLE pinners ADD COLUMN `site_type` VARCHAR(64)")
        mysql_conn.commit()
        print("  (MySQL: added column `site_type` to pinners.)")
    cur.close()


def _pinners_username_collation(mysql_conn) -> str:
    """Looks up the ACTUAL collation of pinners.username on this server,
    rather than assuming one. 8_sync_to_mysql.py's CREATE TABLE only sets a
    table-level DEFAULT CHARSET=utf8mb4 (no explicit COLLATE), so the column's
    real collation is whatever the schema's default happened to be at the
    moment that table was first created -- which can silently differ from a
    bare CREATE TEMPORARY TABLE's default collation (e.g. utf8mb4_unicode_ci
    vs utf8mb4_0900_ai_ci), and MySQL refuses to compare two columns with
    different *implicit* collations ('Illegal mix of collations'). Matching
    the temp table's column to the real one exactly avoids that, instead of
    hardcoding a guess that only works on some servers."""
    cur = mysql_conn.cursor()
    try:
        cur.execute(
            "SELECT COLLATION_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'pinners' AND COLUMN_NAME = 'username'"
        )
        row = cur.fetchone()
        return row[0] if row and row[0] else "utf8mb4_unicode_ci"
    except Exception:
        return "utf8mb4_unicode_ci"
    finally:
        cur.close()


def sync_site_types_from_db(mysql_conn) -> int:
    """Sync site_type from scraped_websites directly into pinners.site_type — no Google Sheet needed.
    Joins pinners.domain_url = scraped_websites.domain, copies site_type where it differs."""
    cur = mysql_conn.cursor()
    try:
        cur.execute(
            "UPDATE pinners p "
            "INNER JOIN scraped_websites sw ON sw.domain = p.domain_url "
            "SET p.site_type = sw.site_type "
            "WHERE sw.site_type IS NOT NULL AND sw.site_type != '' "
            "  AND sw.status = 'done' "
            "  AND (p.site_type IS NULL OR p.site_type != sw.site_type)"
        )
        updated = cur.rowcount
        mysql_conn.commit()
        cur.close()
        return updated
    except Exception as e:
        mysql_conn.rollback()
        cur.close()
        print(f"  (warning: sync_site_types_from_db failed — {e})")
        return 0


def sync_site_types_mysql(mysql_conn, classifications: dict) -> int:
    """Writes the Sheet's real site_type STRING (Blog / Store / Link-in-Bio /
    General Website / etc., not just a blog/not-blog flag) straight into
    MySQL's pinners.site_type column, by username. Unlike 8_sync_to_mysql.py's
    pinners sync (INSERT IGNORE — insert-only, never touches an existing row),
    this is a plain UPDATE, so it overwrites whatever's there every pass —
    site_type stays current on MySQL itself, not just inferred internally by
    this script. Only updates pinners that already exist in MySQL (a website
    classified on the Sheet for a pinner no PC has ever scraped into MySQL
    yet has nothing to attach to).

    Bulk-loads via a temporary table + a single UPDATE...JOIN instead of one
    UPDATE per username. mysql-connector-python's executemany() only rewrites
    INSERT statements into a single multi-row round trip -- for UPDATE (the
    old approach here) it just loops and sends one network round trip per
    row, so 40k+ classified pinners meant 40k+ round trips to the remote DB
    (the actual cause of this step taking minutes). INSERT batches fine, so
    we stage all (username, site_type) pairs into a temp table with batched
    INSERTs, then do the whole update as one JOIN statement. The temp table's
    username column is created with the SAME collation as pinners.username
    (looked up via _pinners_username_collation) so the JOIN never hits
    MySQL's 'Illegal mix of collations' error."""
    if not classifications:
        return 0
    items = list(classifications.items())
    collation = _pinners_username_collation(mysql_conn)
    cur = mysql_conn.cursor()
    try:
        cur.execute("DROP TEMPORARY TABLE IF EXISTS tmp_site_types")
        cur.execute(
            "CREATE TEMPORARY TABLE tmp_site_types ("
            f"username VARCHAR(190) COLLATE {collation} PRIMARY KEY, site_type VARCHAR(64))"
        )
        insert_sql = "INSERT INTO tmp_site_types (username, site_type) VALUES (%s, %s)"
        batch_size = 5000
        for i in range(0, len(items), batch_size):
            batch = items[i:i + batch_size]
            cur.executemany(insert_sql, [(username, site_type) for username, site_type in batch])
        mysql_conn.commit()

        updated = 0
        for attempt in range(1, 4):
            try:
                cur.execute(
                    "UPDATE pinners p INNER JOIN tmp_site_types t ON t.username = p.username "
                    "SET p.site_type = t.site_type"
                )
                updated = cur.rowcount
                mysql_conn.commit()
                break
            except Exception as err:
                mysql_conn.rollback()
                is_lock = "1205" in str(err) or getattr(err, "errno", None) == 1205
                if is_lock and attempt < 3:
                    time.sleep(5 * attempt)
                    continue
                print(f"  (warning: site_type bulk sync failed — {err})")
                break

        cur.execute("DROP TEMPORARY TABLE IF EXISTS tmp_site_types")
        mysql_conn.commit()
    except Exception as err:
        mysql_conn.rollback()
        print(f"  (warning: site_type sync setup failed — {err})")
        cur.close()
        return 0
    cur.close()
    print(f"  Synced site_type for {updated} pinner(s) into MySQL ({len(items)} classified on the Sheet).")
    return updated


def _mysql_execute_retry(mysql_conn, sql, params=None, retries=3, delay=5):
    cur = mysql_conn.cursor()
    for attempt in range(1, retries + 1):
        try:
            cur.execute(sql, params or ())
            mysql_conn.commit()
            cur.close()
            return
        except Exception as err:
            mysql_conn.rollback()
            is_lock = "1205" in str(err) or getattr(err, "errno", None) == 1205
            if is_lock and attempt < retries:
                time.sleep(delay * attempt)
                continue
            cur.close()
            raise


def fetch_eligible_units_mysql(mysql_conn, limit, retry_failed: bool) -> list:
    """Same shape/diagnostics as fetch_eligible_units() but reads from MySQL
    directly — a plain JOIN against pinners.site_type (kept fresh every pass
    by sync_site_types_mysql, called right before this in main()), so no
    Python-side username set/chunking needed; MySQL does the filtering.
    Also immediately marks claimed pins 'Running' so a second PC's concurrent
    pass mostly avoids re-claiming the same backlog (best-effort, not a hard
    lock — a crashed run can leave some pins stuck at 'Running'; rerun with
    --retry-failed to sweep those back up)."""
    cur = mysql_conn.cursor()
    cur.execute("SELECT COUNT(*) FROM pins")
    total_pins = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM pinners WHERE site_type LIKE '%blog%'")
    blog_pinner_count = cur.fetchone()[0]

    if not blog_pinner_count:
        print(f"  (pins total in MySQL: {total_pins}, blog pinners (site_type on MySQL): 0 — nothing eligible.)")
        cur.close()
        return []

    status_clause = "" if retry_failed else "AND (p.link_download_status IS NULL OR p.link_download_status='')"

    cur.execute(
        "SELECT COUNT(*) FROM pins p INNER JOIN pinners pn ON pn.username = p.pinner_username "
        "WHERE pn.site_type LIKE '%blog%'"
    )
    pins_under_blog = cur.fetchone()[0]

    cur.execute(
        "SELECT COUNT(*) FROM pins p INNER JOIN pinners pn ON pn.username = p.pinner_username "
        "WHERE pn.site_type LIKE '%blog%' AND p.link IS NOT NULL AND p.link <> ''"
    )
    pins_under_blog_with_link = cur.fetchone()[0]

    cur.execute(
        f"SELECT p.id, p.link FROM pins p INNER JOIN pinners pn ON pn.username = p.pinner_username "
        f"WHERE pn.site_type LIKE '%blog%' AND p.link IS NOT NULL AND p.link <> '' {status_clause}"
    )
    rows = cur.fetchall()
    cur.close()

    units_map = {}
    for pin_id, link in rows:
        link = (link or "").strip()
        if not link:
            continue
        units_map.setdefault(link, []).append(pin_id)
    unit_list = [{"link": link, "pin_ids": pin_ids} for link, pin_ids in units_map.items()]

    print(f"  (pins total in MySQL: {total_pins}, blog pinners (site_type on MySQL): {blog_pinner_count}, "
          f"pins under blog pinners: {pins_under_blog}, with a link: {pins_under_blog_with_link}, "
          f"eligible (not yet attempted): {len(rows)}, unique links to download: {len(unit_list)}.)")

    if limit:
        unit_list = unit_list[:limit]

    all_ids = [pid for u in unit_list for pid in u["pin_ids"]]
    for i in range(0, len(all_ids), 500):
        chunk = all_ids[i:i + 500]
        placeholders = ", ".join(["%s"] * len(chunk))
        try:
            _mysql_execute_retry(
                mysql_conn,
                f"UPDATE pins SET link_download_status='Running' WHERE id IN ({placeholders})",
                chunk,
            )
        except Exception as e:
            print(f"  (warning: couldn't claim a batch — {e})")

    return unit_list


def write_unit_result_mysql(mysql_conn, pin_ids: list, status: str, html_: str, css_: str, js_: str):
    placeholders = ",".join(["%s"] * len(pin_ids))
    sql = (f"UPDATE pins SET link_download_status=%s, link_downloaded_at=%s, "
           f"link_html=%s, link_css=%s, link_js=%s WHERE id IN ({placeholders})")
    params = [status, time.strftime("%Y-%m-%d %H:%M:%S"), html_, css_, js_] + pin_ids
    _mysql_execute_retry(mysql_conn, sql, params)


# ─── Fetch + extract ─────────────────────────────────────────────────────────

# Dead/unresolvable domains can make DNS resolution hang far longer than any
# socket/requests timeout (that hang happens BEFORE the socket exists). Same
# dedicated-pool hard-ceiling probe as step 13.
_PING_POOL = ThreadPoolExecutor(max_workers=300)
_PING_TIMEOUT = 2.5


def _tcp_probe(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def quick_ping(domain: str, timeout: float = _PING_TIMEOUT) -> bool:
    if not domain:
        return False
    fut = _PING_POOL.submit(lambda: _tcp_probe(domain, 443, timeout) or _tcp_probe(domain, 80, timeout))
    try:
        return fut.result(timeout=timeout * 2 + 1)
    except Exception:
        return False


def _looks_like_cloudflare(status_code: int, headers, body_sample: str) -> bool:
    # Self-contained on purpose: only ever a "block" if the status itself is a
    # block-shaped one. A 200 page that merely mentions "checking your browser"
    # in its body text (rare, but possible) must never be flagged.
    if status_code not in (403, 429, 503):
        return False
    server = (headers.get("Server") or "").lower() if headers else ""
    if "cloudflare" in server:
        return True
    if headers and (headers.get("cf-ray") or headers.get("CF-RAY")):
        return True
    low = (body_sample or "")[:5000].lower()
    return any(marker in low for marker in CLOUDFLARE_BODY_MARKERS)


def _stream_get(url: str, hard_deadline: float, max_bytes: int, timeout=(5, 10)):
    """Shared streaming GET with a real wall-clock deadline (requests' own
    read-timeout only bounds gaps BETWEEN chunks, not total transfer time —
    same gotcha step 13's fetch_homepage fixes) and a byte cap. Bypasses any
    system/VPN/antivirus HTTP proxy explicitly, since the raw-socket ping
    pre-check never goes through one and would otherwise pass while this GET
    silently stalls behind it. Returns (ok, text, status_code, headers, error)."""
    t0 = time.time()
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True,
                          proxies={"http": None, "https": None}, stream=True)
    except requests.exceptions.Timeout:
        return False, "", 0, {}, "timeout"
    except requests.exceptions.RequestException as e:
        return False, "", 0, {}, f"connection error: {e}"

    status = r.status_code
    resp_headers = r.headers

    if status >= 400:
        sample = ""
        try:
            chunk = next(r.iter_content(chunk_size=4096), b"")
            sample = chunk.decode("utf-8", errors="replace")
        except Exception:
            pass
        r.close()
        return False, sample, status, resp_headers, f"HTTP {status}"

    try:
        chunks = []
        total = 0
        for chunk in r.iter_content(chunk_size=16384):
            if time.time() - t0 > hard_deadline:
                r.close()
                return False, "", status, resp_headers, "timeout"
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                break
    except requests.exceptions.RequestException as e:
        return False, "", status, resp_headers, f"connection error: {e}"
    finally:
        r.close()

    content = b"".join(chunks)
    try:
        text = content.decode(r.encoding or "utf-8", errors="replace")
    except (LookupError, TypeError):
        text = content.decode("utf-8", errors="replace")
    return True, text, status, resp_headers, ""


def fetch_page(url: str):
    """Fetch the destination page HTML. Returns (ok, html, status_label).
    On 403/429/503, tries curl_cffi's Chrome-impersonation bypass once (same
    as step 13) before deciding it's genuinely blocked. Distinguishes a
    Cloudflare-specific block from a generic HTTP block, per request."""
    target = url if url.startswith("http") else "https://" + url
    ok, text, status, headers, err = _stream_get(target, PAGE_HARD_DEADLINE, MAX_PAGE_BYTES)
    if ok:
        return True, text, ""

    if status in (403, 429, 503):
        is_cf = _looks_like_cloudflare(status, headers, text)
        try:
            from curl_cffi import requests as curl_cffi_requests
            r2 = curl_cffi_requests.get(target, impersonate="chrome120", headers=HEADERS, timeout=10)
            if r2.status_code < 400 and not _looks_like_cloudflare(r2.status_code, r2.headers, r2.text):
                return True, r2.text, ""
            if r2.status_code in (403, 429, 503) and _looks_like_cloudflare(r2.status_code, r2.headers, r2.text):
                is_cf = True
        except Exception:
            pass
        return False, "", ("Blocked (Cloudflare)" if is_cf else f"Blocked (HTTP {status})")

    if err == "timeout":
        return False, "", "Failed (timeout)"
    if err.startswith("connection error"):
        return False, "", f"Failed ({err})"
    if status and status >= 400:
        return False, "", f"Failed (HTTP {status})"
    return False, "", "Failed (unknown error)"


def extract_asset_urls(raw_html: str, base_url: str):
    css_urls, js_urls = [], []
    seen_css, seen_js = set(), set()
    for pattern in (LINK_CSS_RE, LINK_CSS_RE_ALT):
        for m in pattern.finditer(raw_html):
            u = urljoin(base_url, m.group(1))
            if u not in seen_css:
                seen_css.add(u)
                css_urls.append(u)
    for m in SCRIPT_SRC_RE.finditer(raw_html):
        u = urljoin(base_url, m.group(1))
        if u not in seen_js:
            seen_js.add(u)
            js_urls.append(u)
    return css_urls[:MAX_CSS_FILES], js_urls[:MAX_JS_FILES]


def extract_inline_assets(raw_html: str):
    css_inline = "\n\n".join(m.group(1) for m in INLINE_STYLE_RE.finditer(raw_html))
    js_inline = "\n\n".join(m.group(1) for m in INLINE_SCRIPT_RE.finditer(raw_html))
    return css_inline, js_inline


def fetch_assets(urls: list, max_total_chars: int, unit_t0: float):
    parts = []
    total = 0
    for u in urls:
        if total >= max_total_chars:
            break
        if time.time() - unit_t0 > UNIT_HARD_DEADLINE:
            break
        ok, text, status, headers, err = _stream_get(u, ASSET_HARD_DEADLINE, MAX_ASSET_BYTES)
        if ok and text:
            snippet = f"/* === {u} === */\n{text}"
            parts.append(snippet)
            total += len(snippet)
    return "\n\n".join(parts)[:max_total_chars]


# ─── Worker ───────────────────────────────────────────────────────────────────

def download_one(unit: dict, progress: dict = None, progress_lock=None) -> dict:
    t0 = time.time()
    link = unit["link"]

    def mark(phase: str):
        print(f"        ...{link[:60]}: {phase} ({time.time()-t0:.1f}s in)")
        if progress is not None:
            with progress_lock:
                progress[link] = (phase, time.time())

    def done(status, html_="", css_="", js_=""):
        if progress is not None:
            with progress_lock:
                progress.pop(link, None)
        return {**unit, "status": status, "html": html_, "css": css_, "js": js_,
                 "elapsed": round(time.time() - t0, 1)}

    if not link:
        return done("Failed (no link)")

    target = link if link.startswith("http") else "https://" + link
    host = urlparse(target).hostname or ""

    mark("pinging")
    if not quick_ping(host):
        return done("Failed (unreachable)")

    mark("fetching page")
    ok, raw_html, status_label = fetch_page(target)
    if not ok:
        return done(status_label)

    mark("fetching assets")
    css_urls, js_urls = extract_asset_urls(raw_html, target)
    css_inline, js_inline = extract_inline_assets(raw_html)
    css_external = fetch_assets(css_urls, MAX_CSS_TOTAL_CHARS, t0)
    js_external = fetch_assets(js_urls, MAX_JS_TOTAL_CHARS, t0)

    css_combined = "\n\n".join(x for x in (css_inline, css_external) if x)
    js_combined = "\n\n".join(x for x in (js_inline, js_external) if x)

    return done("Done", raw_html[:MAX_PAGE_BYTES], css_combined, js_combined)


# ─── Main pass ────────────────────────────────────────────────────────────────

def run_pass(units: list, write_fn, workers: int, dry_run: bool) -> int:
    total = len(units)
    total_pins = sum(len(u["pin_ids"]) for u in units)
    pass_t0 = time.time()
    print(f"  Downloading {total} unique link(s) ({total_pins} pin(s)) with {workers} threads...")

    done = 0
    progress = {}
    progress_lock = threading.Lock()
    stop_watchdog = threading.Event()

    def watchdog():
        while not stop_watchdog.wait(4):
            now = time.time()
            with progress_lock:
                snapshot = list(progress.items())
            for link, (phase, started) in snapshot:
                elapsed = now - started
                if elapsed >= 5:
                    print(f"        ...{link[:60]}: still {phase} ({elapsed:.0f}s so far, watchdog)")

    wd_thread = threading.Thread(target=watchdog, daemon=True)
    wd_thread.start()

    def handle_result(res: dict):
        nonlocal done
        done += 1
        secs = res.get("elapsed", 0)
        n_pins = len(res["pin_ids"])
        print(f"  [{done}/{total}] {res['link'][:80]} -> {res['status']} ({n_pins} pin(s)) [{secs}s]")
        if dry_run:
            return
        write_fn(res["pin_ids"], res["status"], res["html"], res["css"], res["js"])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(download_one, u, progress, progress_lock): u for u in units}
        for fut in as_completed(futures):
            try:
                handle_result(fut.result())
            except Exception as e:
                u = futures[fut]
                print(f"  [!] {u.get('link', '')[:80]} crashed: {e}")

    stop_watchdog.set()

    wall = time.time() - pass_t0
    avg = wall / total if total else 0
    print(f"  Pass done: {total} link(s) / {total_pins} pin(s) in {wall:.1f}s (avg {avg:.1f}s/link, {workers} threads in parallel).")
    return done


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Downloads HTML/CSS/JS of blog pinners' pin destination links (bot).")
    ap.add_argument("--workers", type=int, default=80, help="parallel threads (default 80)")
    ap.add_argument("--poll-minutes", type=int, default=10, help="idle check interval after backlog clears (default 10)")
    ap.add_argument("--once", action="store_true", help="single pass then exit (no polling loop)")
    ap.add_argument("--limit", type=int, default=None, help="cap unique links downloaded this run (testing)")
    ap.add_argument("--retry-failed", action="store_true", help="also re-attempt pins with a Failed/Blocked/Running status, not just untried ones")
    ap.add_argument("--dry-run", action="store_true", help="download + print only, write nothing")
    ap.add_argument("--source", choices=["auto", "mysql", "sqlite"], default="mysql",
                     help="pin source: 'mysql' (default) = shared cloud DB (every PC's pins+pinners, "
                          "see 8_sync_to_mysql.py) — exits if .env's MYSQL_PASSWORD isn't configured/reachable, "
                          "'sqlite' = this PC's local sortpin.db only, "
                          "'auto' = mysql when .env has a working MYSQL_PASSWORD, else falls back to sqlite")
    args = ap.parse_args()

    gsc = load_sheet_client()
    cfg = gsc.resolve_webapp() if gsc else None

    mysql_conn = None
    if args.source in ("auto", "mysql"):
        mysql_conn = get_mysql_connection(load_env())
        if args.source == "mysql" and mysql_conn is None:
            print("  --source mysql was requested but MySQL isn't reachable/configured (check .env). Exiting.")
            sys.exit(1)

    if mysql_conn is not None:
        env = load_env()
        ensure_mysql_pin_columns(mysql_conn)
        ensure_mysql_pinner_columns(mysql_conn)
        print(f"  14_download_blog_pin_links.py — workers={args.workers}, source=mysql (all PCs' data)")
        while True:
            # Reconnect if the connection dropped (idle timeout after 10+ min polls)
            try:
                mysql_conn.ping(reconnect=True, attempts=3, delay=2)
            except Exception:
                print("  MySQL connection lost — reconnecting...")
                mysql_conn = get_mysql_connection(env)
                if mysql_conn is None:
                    print("  Reconnect failed. Retrying next poll...")
                    time.sleep(60)
                    continue

            # Sync site_type directly from scraped_websites (written by step 10) — no Sheet needed
            updated = sync_site_types_from_db(mysql_conn)
            if updated > 0:
                print(f"  Synced site_type for {updated} pinner(s) from scraped_websites (DB-direct, no Sheet).")
            else:
                print(f"  site_type sync from DB: 0 new updates (all pinners already up to date).")
            units = fetch_eligible_units_mysql(mysql_conn, args.limit, args.retry_failed)
            if units:
                write_fn = lambda pin_ids, status, h, c, j: write_unit_result_mysql(mysql_conn, pin_ids, status, h, c, j)
                run_pass(units, write_fn, args.workers, args.dry_run)
            else:
                print("  No eligible pins right now in MySQL (no confirmed-blog pinners yet, or all already downloaded/running).")

            if args.once:
                break
            print(f"  Idling {args.poll_minutes} min before next check... (Ctrl+C to stop)")
            try:
                time.sleep(args.poll_minutes * 60)
            except KeyboardInterrupt:
                print("\n  Stopped.")
                break
        return

    conn = connect_db()
    print(f"  14_download_blog_pin_links.py — workers={args.workers}, source=sqlite (this PC only)")
    while True:
        sync_site_types(gsc, cfg, conn)
        apply_local_site_type_fallback(conn)
        units = fetch_eligible_units(conn, args.limit, args.retry_failed)
        if units:
            write_fn = lambda pin_ids, status, h, c, j: write_unit_result(conn, pin_ids, status, h, c, j)
            run_pass(units, write_fn, args.workers, args.dry_run)
        else:
            print("  No eligible pins right now (no confirmed-blog pinners yet, or all already downloaded).")

        if args.once:
            break
        print(f"  Idling {args.poll_minutes} min before next check... (Ctrl+C to stop)")
        try:
            time.sleep(args.poll_minutes * 60)
        except KeyboardInterrupt:
            print("\n  Stopped.")
            break


if __name__ == "__main__":
    main()
