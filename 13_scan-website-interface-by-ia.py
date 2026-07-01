"""
STEP 13 - Fast AI Website Type / Category / Description Scanner
=================================================================

Standalone bot. Scans every pinner website that doesn't have an AI opinion
yet, PLUS any already tagged "General Website" or "Blog" by step 10 (that
heuristic classifier is known to mislabel SaaS/tool sites that merely have a
marketing blog bolted on — e.g. postermywall.com). Skips anything step 10
already confidently tagged Store / Link-in-Bio / Social Media — those don't
need an AI opinion.

For each eligible website it first does a fast TCP ping (no full HTTP request)
to skip dead/unreachable domains in seconds instead of stalling, then fetches
the homepage and sends the title / meta
description / visible text to an OpenRouter model (QUICK_SCRAPE_OPENROUTER_MODEL
in .env, default openai/gpt-4.1-nano), and asks for the site's REAL type, a
short filterable theme/category, and a 1-2 sentence description — judged by
the site's PRIMARY purpose, not by incidental signals like an RSS feed or a
/blog/ subfolder.

Results are written to FOUR new columns — local sortpin.db's
`scraped_websites` table AND the Google Sheet's `websites` tab (columns are
auto-created on the Sheet on first run, no redeploy needed):

    status_website_scaned_by_ia        Done / Failed (...) / Blocked (...)
    type_website_scaned_by_ia          Blog / Store / SaaS/Tool / Portfolio / ...
    category_website_scaned_by_ia      short theme, e.g. "Fashion & Beauty" — for filtering
    description_website_scaned_by_ia   1-2 sentence plain description

Runs as a bot: scans the whole backlog as fast as possible (high thread
count, parallel fetch+AI calls), then — once nothing eligible is left —
idles and re-checks every --poll-minutes (default 10) for newly added or
newly eligible websites. Never stops on its own; Ctrl+C to quit.

Run:
  python 13_scan-website-interface-by-ia.py                      # bot mode: scan all, then poll every 10 min
  python 13_scan-website-interface-by-ia.py --once               # single pass, then exit
  python 13_scan-website-interface-by-ia.py --workers 100        # more parallel threads (default 60)
  python 13_scan-website-interface-by-ia.py --poll-minutes 5     # check more often than 10 min
  python 13_scan-website-interface-by-ia.py --limit 20 --once --dry-run   # quick test, no writes

.env:
  OPENROUTER_API_KEY=...                              (required)
  QUICK_SCRAPE_OPENROUTER_MODEL=openai/gpt-4.1-nano    (optional — this is the default if unset)
"""
from __future__ import annotations
import db_logger  # logs every print() to MySQL bot_logs table — dashboard reads from there

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
from urllib.parse import urlparse

import requests

# Force IPv4-only DNS resolution process-wide. Root cause of sites being slow/
# "unreachable" here while loading fine in a real browser: some hosts publish an
# IPv6 (AAAA) record whose route is dead/blackholed on this network. Browsers use
# real Happy Eyeballs (RFC 6555) — race v4/v6, fall back almost instantly. Python's
# socket.create_connection() and requests/urllib3 do NOT race; they try addresses
# sequentially and burn the *full* timeout on the dead IPv6 address before ever
# trying the working IPv4 one. Stripping AAAA records at the getaddrinfo() level
# fixes every caller (raw sockets, requests, urllib3) with one change.
_orig_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
socket.getaddrinfo = _ipv4_only_getaddrinfo

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE, ".env")
DB_PATH = os.path.join(BASE, "sortpin.db")
SHEET_CACHE_PATH = os.path.join(BASE, "websites_sheet_cache.json")

NEW_COLUMNS = [
    "status_website_scaned_by_ia",
    "type_website_scaned_by_ia",
    "category_website_scaned_by_ia",
    "description_website_scaned_by_ia",
]

# Already confidently classified by step 10's domain lists — no AI needed.
SKIP_SITE_TYPES = {"store", "link-in-bio", "social media"}

DEFAULT_MODEL = "openai/gpt-4.1-nano"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """You classify websites for a Pinterest lead database. You will be given a \
homepage's title, meta description, and visible text. Return ONLY a JSON object with exactly \
these keys:

"type": the site's REAL primary type — judge by what the business/person actually DOES, not by \
incidental signals. Use one of: Blog, Store, SaaS/Tool, Portfolio, News/Media, Personal/Lifestyle, \
Business/Corporate, Nonprofit/Community, Education, Forum/Community, Other. \
IMPORTANT: a company that runs a SaaS product, design tool, agency, or store but also happens to \
publish a marketing/content blog is NOT a "Blog" — classify it by its actual product/service. Only \
use "Blog" when publishing articles/posts IS the primary purpose of the site.

"category": a short 1-3 word theme/niche, useful for spreadsheet filtering, e.g. "Fashion & Beauty", \
"Marketing Tools", "Food & Recipes", "Travel", "Home & DIY", "Health & Wellness", "Design Tools", \
"Photography & Art", "Parenting & Family", "Finance", "Tech & SaaS", "Education", "Entertainment", \
"Nonprofit", "Other".

"description": one or two plain sentences describing what the website/business actually offers.

Respond with raw JSON only, no markdown fences, no extra text."""


def load_env() -> dict:
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env


def load_sheet_client():
    try:
        import google_sheets_client as gsc
        return gsc
    except Exception as e:
        print(f"  (Sheet sync unavailable: {e})")
        return None


def _extract_domain(url: str) -> str:
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


# A 69k-row getWebsites() Apps Script read occasionally times out or comes back
# with a valid-but-empty {"ok": true, "websites": []} (no exception at all) on a
# transient hiccup. Caching the last successful pull means a flaky read falls
# back to "yesterday's real backlog" instead of silently looking like there's
# nothing left to scan.
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
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scraped_websites (
            domain TEXT PRIMARY KEY,
            url TEXT,
            title TEXT,
            description TEXT,
            cms TEXT,
            tech_stack TEXT,
            category_id INTEGER,
            status TEXT,
            post_count INTEGER DEFAULT 0,
            last_scraped_at TEXT,
            site_type TEXT
        );
        """
    )
    existing = {r[1] for r in conn.execute("PRAGMA table_info(scraped_websites)").fetchall()}
    for col in NEW_COLUMNS:
        if col not in existing:
            conn.execute(f"ALTER TABLE scraped_websites ADD COLUMN {col} TEXT")
    conn.commit()
    return conn


def local_rows(conn) -> list[dict]:
    cur = conn.execute(
        "SELECT domain, url, site_type, status_website_scaned_by_ia FROM scraped_websites"
    )
    return [dict(r) for r in cur.fetchall()]


def write_local(conn, domain: str, url: str, status: str, type_: str, category: str, description: str):
    with _db_lock:
        try:
            conn.execute(
                """
                INSERT INTO scraped_websites (domain, url, status_website_scaned_by_ia,
                    type_website_scaned_by_ia, category_website_scaned_by_ia, description_website_scaned_by_ia)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                    url=excluded.url,
                    status_website_scaned_by_ia=excluded.status_website_scaned_by_ia,
                    type_website_scaned_by_ia=excluded.type_website_scaned_by_ia,
                    category_website_scaned_by_ia=excluded.category_website_scaned_by_ia,
                    description_website_scaned_by_ia=excluded.description_website_scaned_by_ia
                """,
                (domain, url, status, type_, category, description),
            )
            conn.commit()
        except Exception as _sqlite_err:
            print(f"  [db_local] write failed for {domain}: {_sqlite_err}")
    # Also write to MySQL so dashboard + other PCs see results even if sortpin.db is broken
    _write_mysql(domain, url, status, type_, category, description)


# ── MySQL writer for step-13 results ──────────────────────────────────────────
_mysql_conn_13 = None
_mysql_lock_13 = __import__("threading").Lock()

def _get_mysql_conn_13():
    global _mysql_conn_13
    try:
        import pymysql
        env = {}
        if os.path.exists(ENV_PATH):
            for line in open(ENV_PATH, errors="replace"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    env[k.strip()] = v.strip()
        pw = env.get("MYSQL_PASSWORD", "")
        if not pw:
            return None
        if _mysql_conn_13:
            try:
                _mysql_conn_13.ping(reconnect=True)
                return _mysql_conn_13
            except Exception:
                pass
        _mysql_conn_13 = pymysql.connect(
            host=env.get("MYSQL_HOST", "72.61.197.144"),
            port=int(env.get("MYSQL_PORT", 3306)),
            db=env.get("MYSQL_DB", "data_pint"),
            user=env.get("MYSQL_USER", "data_pint_user"),
            password=pw,
            charset="utf8mb4",
            connect_timeout=6,
            autocommit=True,
        )
        # Ensure columns exist in MySQL scraped_websites
        with _mysql_conn_13.cursor() as _c:
            for col, coltype in [
                ("status_website_scaned_by_ia",      "VARCHAR(100)"),
                ("type_website_scaned_by_ia",         "VARCHAR(100)"),
                ("category_website_scaned_by_ia",     "VARCHAR(200)"),
                ("description_website_scaned_by_ia",  "TEXT"),
            ]:
                try:
                    _c.execute(f"ALTER TABLE scraped_websites ADD COLUMN {col} {coltype}")
                except Exception:
                    pass  # column already exists
        return _mysql_conn_13
    except Exception:
        return None

def _write_mysql(domain: str, url: str, status: str, type_: str, category: str, description: str):
    with _mysql_lock_13:
        try:
            mc = _get_mysql_conn_13()
            if not mc:
                return
            with mc.cursor() as c:
                c.execute(
                    """INSERT INTO scraped_websites
                           (domain, url, status_website_scaned_by_ia,
                            type_website_scaned_by_ia, category_website_scaned_by_ia,
                            description_website_scaned_by_ia)
                       VALUES (%s, %s, %s, %s, %s, %s)
                       ON DUPLICATE KEY UPDATE
                           status_website_scaned_by_ia      = VALUES(status_website_scaned_by_ia),
                           type_website_scaned_by_ia         = VALUES(type_website_scaned_by_ia),
                           category_website_scaned_by_ia     = VALUES(category_website_scaned_by_ia),
                           description_website_scaned_by_ia  = VALUES(description_website_scaned_by_ia)
                    """,
                    (domain, url or "", status or "", type_ or "", category or "", description or ""),
                )
        except Exception as _me:
            print(f"  [db_mysql] write failed for {domain}: {_me}")


# ─── Eligibility ─────────────────────────────────────────────────────────────

def fetch_eligible_rows(gsc, cfg, conn, limit) -> list:
    """Sheet first (cross-PC source of truth), local DB as fallback/merge."""
    rows = []
    seen_domains = set()
    # Diagnostic counters — printed every run so "0 eligible" is never a mystery.
    stats = {"total": 0, "no_website": 0, "already_scanned": 0, "skip_site_type": 0, "eligible": 0}

    def _check(website: str, site_type: str, already_scanned: str) -> bool:
        stats["total"] += 1
        if not (website or "").strip():
            stats["no_website"] += 1
            return False
        if (already_scanned or "").strip():
            stats["already_scanned"] += 1
            return False
        if (site_type or "").strip().lower() in SKIP_SITE_TYPES:
            stats["skip_site_type"] += 1
            return False
        stats["eligible"] += 1
        return True

    sheet_count = 0
    used_cache = False
    if gsc and cfg:
        # The Sheet's "websites" tab has 69k+ rows, so this single getWebsites()
        # call is a heavy Apps Script read — it occasionally times out or comes
        # back with a valid-but-empty {"ok": true, "websites": []} (no exception
        # at all) on a transient hiccup. Retry with backoff before trusting an
        # empty result; if it's still empty, fall back to the LAST SUCCESSFUL
        # Sheet snapshot on disk (not just the thin local scraped_websites table)
        # so one flaky read doesn't make the whole 37k-site backlog disappear.
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
                used_cache = True
                if last_err:
                    print(f"  (Could not reach Sheet — {last_err}. Using last cached Sheet snapshot ({len(cached)} sites).)")
                else:
                    print(f"  (Sheet returned 0 websites after retries — using last cached Sheet snapshot ({len(cached)} sites).)")
            elif last_err:
                print(f"  (Could not reach Sheet — {last_err}. Falling back to local DB.)")
            elif data is not None and not data.get("websites"):
                print("  (Sheet returned 0 websites after retries, no cache available — falling back to local DB only.)")

        if data:
            for w in data.get("websites", []):
                sheet_count += 1
                website = (w.get("website") or "").strip()
                domain = _extract_domain(website)
                if not domain or domain in seen_domains:
                    continue
                seen_domains.add(domain)
                if _check(website, w.get("site_type"), w.get("status_website_scaned_by_ia")):
                    rows.append({
                        "domain": domain,
                        "website": website,
                        "name": w.get("name") or "",
                    })

    local_count = 0
    for r in local_rows(conn):
        local_count += 1
        domain = r.get("domain") or _extract_domain(r.get("url") or "")
        if not domain or domain in seen_domains:
            continue
        seen_domains.add(domain)
        if _check(r.get("url"), r.get("site_type"), r.get("status_website_scaned_by_ia")):
            rows.append({
                "domain": domain,
                "website": r.get("url") or "",
                "name": "",
            })

    print(f"  (Sheet rows: {sheet_count}{' [from cache]' if used_cache else ''}, local rows: {local_count}, "
          f"{stats['total']} unique checked — eligible: {stats['eligible']}, "
          f"already scanned: {stats['already_scanned']}, "
          f"skipped Store/Link-in-Bio/Social: {stats['skip_site_type']}, "
          f"no website: {stats['no_website']}.)")

    if limit:
        rows = rows[:limit]
    return rows


# ─── Fetch + extract ─────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Dead/unresolvable domains can make the OS's own DNS resolver hang for a long
# time — far longer than any socket/requests timeout we set, since that hang
# happens *before* the socket even exists. Running the probe in its own tiny
# pool and hard-capping how long we wait on the future (not the socket call
# itself) is what actually bounds it. A bare TCP connect (no HTTP) is the
# fastest possible "is this even reachable" check, tried on 443 then 80.
_PING_POOL = ThreadPoolExecutor(max_workers=300)
_PING_TIMEOUT = 2.5  # seconds per port attempt


def _tcp_probe(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def quick_ping(domain: str, timeout: float = _PING_TIMEOUT) -> bool:
    """Fast reachability pre-check. Dead domains fail in ~timeout*2 seconds instead
    of stalling on a slow/non-responsive DNS lookup or a full HTTP request+timeout."""
    if not domain:
        return False
    fut = _PING_POOL.submit(lambda: _tcp_probe(domain, 443, timeout) or _tcp_probe(domain, 80, timeout))
    try:
        return fut.result(timeout=timeout * 2 + 1)
    except Exception:
        return False


def fetch_homepage(url: str, timeout=(5, 10), hard_deadline: float = 15.0):
    """Returns (ok, html_or_empty, status_label). status_label is only set on failure
    and is already a fully-formed 'Failed (...)' / 'Blocked (...)' string.
    timeout is a (connect, read) tuple so a slow/dead server fails fast on connect
    instead of waiting the full read timeout.

    IMPORTANT gotcha this also fixes: requests' read-timeout only bounds the gap
    BETWEEN chunks, not total transfer time — a server that trickles a few bytes
    every few seconds (never going fully silent for `timeout[1]` seconds straight)
    can stretch a "10s read timeout" into minutes. We stream the body ourselves and
    enforce a real wall-clock deadline across the whole download."""
    target = url if url.startswith("http") else "https://" + url
    t0 = time.time()
    try:
        # requests respects system/env proxy settings by default; the ping
        # pre-check is a raw socket and never goes through one. If Windows or
        # an antivirus/VPN has a local proxy configured, ping passes (fast,
        # no proxy) while this GET silently routes through that proxy and
        # stalls — bypass it explicitly so both checks see the same path.
        r = requests.get(target, headers=HEADERS, timeout=timeout, allow_redirects=True,
                          proxies={"http": None, "https": None}, stream=True)
    except requests.exceptions.Timeout:
        return False, "", "Failed (timeout)"
    except requests.exceptions.RequestException as e:
        return False, "", f"Failed (connection error: {e})"

    if r.status_code in (403, 429, 503):
        r.close()
        # 1) curl_cffi Chrome impersonation
        curl_html = ""
        try:
            from curl_cffi import requests as curl_cffi_requests
            r2 = curl_cffi_requests.get(target, impersonate="chrome120", headers=HEADERS, timeout=timeout[1])
            if r2.status_code < 400:
                curl_html = r2.text
        except Exception:
            pass
        if curl_html:
            return True, curl_html, ""
        # 2) Playwright stealth + captcha API solver
        try:
            import captcha_solver as _cs
            pw_ok, pw_html = _cs.try_playwright(target)
            if pw_ok and pw_html:
                return True, pw_html, ""
            # still blocked — try API solver
            if pw_html:
                try:
                    token = _cs.solve(target, pw_html)
                    if token:
                        ctype, _ = _cs.detect_captcha(pw_html)
                        pw_ok2, pw_html2 = _cs.try_playwright(target, inject_token=token, captcha_type=ctype)
                        if pw_ok2 and pw_html2:
                            return True, pw_html2, ""
                except _cs.CaptchaGiveUp:
                    pass
        except ImportError:
            pass
        return False, "", f"Blocked (HTTP {r.status_code})"

    if r.status_code >= 400:
        r.close()
        return False, "", f"Failed (HTTP {r.status_code})"

    try:
        chunks = []
        total_bytes = 0
        for chunk in r.iter_content(chunk_size=16384):
            if time.time() - t0 > hard_deadline:
                r.close()
                return False, "", "Failed (timeout)"
            if not chunk:
                continue
            chunks.append(chunk)
            total_bytes += len(chunk)
            if total_bytes > 2_000_000:  # we only need the homepage text, not a huge asset-laden page
                break
    except requests.exceptions.RequestException as e:
        return False, "", f"Failed (connection error: {e})"
    finally:
        r.close()

    content = b"".join(chunks)
    try:
        text = content.decode(r.encoding or "utf-8", errors="replace")
    except (LookupError, TypeError):
        text = content.decode("utf-8", errors="replace")

    # Check if 200 response is actually a Cloudflare/captcha challenge page
    lw = text.lower()
    is_challenge = (
        len(text) < 30_000 and
        ("cloudflare" in lw or "captcha" in lw or "hcaptcha" in lw) and
        any(p in lw for p in ["verify you are human", "checking your browser",
                               "just a moment", "are you a robot", "security check",
                               "challenge-platform"])
    )
    if is_challenge:
        try:
            import captcha_solver as _cs
            pw_ok, pw_html = _cs.try_playwright(target)
            if pw_ok and pw_html:
                return True, pw_html, ""
            if pw_html:
                try:
                    token = _cs.solve(target, pw_html)
                    if token:
                        ctype, _ = _cs.detect_captcha(pw_html)
                        pw_ok2, pw_html2 = _cs.try_playwright(target, inject_token=token, captcha_type=ctype)
                        if pw_ok2 and pw_html2:
                            return True, pw_html2, ""
                except _cs.CaptchaGiveUp:
                    pass
        except ImportError:
            pass
        return False, "", "Blocked (challenge page)"

    return True, text, ""


def extract_text(raw_html: str):
    title_m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, re.I | re.S)
    title = html.unescape(re.sub(r"\s+", " ", title_m.group(1))).strip() if title_m else ""

    desc_m = re.search(
        r'<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']',
        raw_html, re.I | re.S,
    )
    if not desc_m:
        desc_m = re.search(
            r'<meta[^>]+content=["\'](.*?)["\'][^>]+(?:name|property)=["\'](?:description|og:description)["\']',
            raw_html, re.I | re.S,
        )
    desc = html.unescape(desc_m.group(1)).strip() if desc_m else ""

    body = re.sub(r"(?is)<(script|style|noscript|svg|header|footer|nav)[^>]*>.*?</\1>", " ", raw_html)
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = html.unescape(body)
    body = re.sub(r"\s+", " ", body).strip()
    return title, desc, body[:3000]


# ─── AI classification ────────────────────────────────────────────────────────

def ai_classify(api_key: str, model: str, url: str, title: str, desc: str, body: str, pinner_name: str) -> dict:
    user_msg = (
        f"URL: {url}\n"
        f"Pinner: {pinner_name or '(unknown)'}\n"
        f"Title: {title}\n"
        f"Meta description: {desc}\n"
        f"Visible text (truncated): {body}"
    )
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_msg},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 250,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/pinterest-scan",
        "X-Title": "pinterest-scan-website-classifier",
    }
    r = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=(5, 25))
    r.raise_for_status()
    content = r.json()["choices"][0]["message"]["content"]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", content, re.S)
        if m:
            return json.loads(m.group(0))
        raise


# ─── Worker ───────────────────────────────────────────────────────────────────

def scan_one(row: dict, api_key: str, model: str, progress: dict = None, progress_lock=None) -> dict:
    t0 = time.time()

    def mark(phase: str):
        # Logs the phase transition immediately AND records it in the shared
        # `progress` dict so the watchdog thread (in run_pass) can print a
        # heartbeat ("still fetching homepage, 12s so far") if this phase
        # runs long — answers "where is it stuck" without guessing.
        print(f"        ...{domain}: {phase} ({time.time()-t0:.1f}s in)")
        if progress is not None:
            with progress_lock:
                progress[domain] = (phase, time.time())

    def done(status, type_="", category="", description=""):
        if progress is not None:
            with progress_lock:
                progress.pop(domain, None)
        return {**row, "status": status, "type": type_, "category": category,
                 "description": description, "elapsed": round(time.time() - t0, 1)}

    url, name = row["website"], row.get("name", "")
    domain = row.get("domain") or url
    target = url if url.startswith("http") else "https://" + url
    # Ping the EXACT hostname fetch_homepage will hit (not the www-stripped
    # `domain` field) — some sites only have a DNS record for one of
    # bare-domain / www-subdomain, so pinging the wrong variant gives a false
    # "reachable" and the real fetch still hangs resolving the other one.
    ping_host = urlparse(target).hostname or row["domain"]

    mark("pinging")
    if not quick_ping(ping_host):
        return done("Failed (unreachable)")

    mark("fetching homepage")
    ok, raw_html, status_label = fetch_homepage(url)
    if not ok:
        return done(status_label)

    title, desc, body = extract_text(raw_html)
    if not (title or desc or body):
        return done("Failed (empty page)")

    mark("asking AI")
    try:
        result = ai_classify(api_key, model, url, title, desc, body, name)
        return done(
            "Done",
            str(result.get("type", "")).strip(),
            str(result.get("category", "")).strip(),
            str(result.get("description", "")).strip(),
        )
    except Exception as e:
        return done(f"Failed (AI error: {e})")


# ─── Sheet write-back ─────────────────────────────────────────────────────────

def ensure_sheet_columns(gsc, cfg, anchor_url: str):
    """One update_website call so all 4 columns exist before batch writes —
    batch_update_websites silently drops fields whose column doesn't exist yet."""
    try:
        gsc.post_webapp(cfg, {
            "action": "update_website",
            "website": anchor_url,
            "updates": {c: "" for c in NEW_COLUMNS},
        })
    except Exception as e:
        print(f"  (Could not pre-create Sheet columns — {e})")


def flush_sheet_batch(gsc, cfg, buffer: list):
    if not buffer:
        return
    try:
        gsc.post_webapp(cfg, {
            "action": "batch_update_websites",
            "updates": [
                {
                    "website": item["website"],
                    "fields": {
                        "status_website_scaned_by_ia": item["status"],
                        "type_website_scaned_by_ia": item["type"],
                        "category_website_scaned_by_ia": item["category"],
                        "description_website_scaned_by_ia": item["description"],
                    },
                }
                for item in buffer
            ],
        })
    except Exception as e:
        print(f"  (Sheet batch write failed — {e})")


# ─── Main pass ────────────────────────────────────────────────────────────────

def run_pass(rows: list, conn, gsc, cfg, api_key: str, model: str, workers: int, dry_run: bool) -> int:
    total = len(rows)
    pass_t0 = time.time()
    print(f"  Scanning {total} website(s) with {workers} threads (model={model})...")

    if gsc and cfg and not dry_run:
        ensure_sheet_columns(gsc, cfg, rows[0]["website"])

    done = 0
    sheet_buffer = []
    buffer_lock = threading.Lock()

    # Heartbeat watchdog: every 4s, print any site that's been sitting in the
    # same phase (pinging/fetching homepage/asking AI) for 5s+, so a stuck
    # site is visible immediately instead of just silence until it times out.
    progress = {}
    progress_lock = threading.Lock()
    stop_watchdog = threading.Event()

    def watchdog():
        while not stop_watchdog.wait(4):
            now = time.time()
            with progress_lock:
                snapshot = list(progress.items())
            for d, (phase, started) in snapshot:
                elapsed = now - started
                if elapsed >= 5:
                    print(f"        ...{d}: still {phase} ({elapsed:.0f}s so far, watchdog)")

    wd_thread = threading.Thread(target=watchdog, daemon=True)
    wd_thread.start()

    def handle_result(res: dict):
        nonlocal done
        done += 1
        tag = res["type"] or "-"
        secs = res.get("elapsed", 0)
        print(f"  [{done}/{total}] {res['domain']} -> {res['status']} ({tag}) [{secs}s]")
        if res["status"] == "Done":
            print(f"        category: {res['category']}")
            print(f"        description: {res['description']}")
        if dry_run:
            return
        write_local(conn, res["domain"], res["website"], res["status"], res["type"], res["category"], res["description"])
        if gsc and cfg:
            flush_now = None
            with buffer_lock:
                sheet_buffer.append(res)
                if len(sheet_buffer) >= 25:
                    flush_now, sheet_buffer[:] = sheet_buffer[:], []
            if flush_now:
                flush_sheet_batch(gsc, cfg, flush_now)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(scan_one, row, api_key, model, progress, progress_lock): row for row in rows}
        for fut in as_completed(futures):
            try:
                handle_result(fut.result())
            except Exception as e:
                row = futures[fut]
                print(f"  [!] {row.get('domain')} crashed: {e}")

    stop_watchdog.set()

    if not dry_run and gsc and cfg and sheet_buffer:
        flush_sheet_batch(gsc, cfg, sheet_buffer)

    wall = time.time() - pass_t0
    avg = wall / total if total else 0
    print(f"  Pass done: {total} site(s) in {wall:.1f}s (avg {avg:.1f}s/site, {workers} threads in parallel).")
    return done


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Fast AI website type/category/description scanner (bot).")
    ap.add_argument("--workers", type=int, default=60, help="parallel threads (default 60)")
    ap.add_argument("--poll-minutes", type=int, default=10, help="idle check interval after backlog clears (default 10)")
    ap.add_argument("--once", action="store_true", help="single pass then exit (no polling loop)")
    ap.add_argument("--limit", type=int, default=None, help="cap rows scanned this run (testing)")
    ap.add_argument("--dry-run", action="store_true", help="scan + print only, write nothing")
    args = ap.parse_args()

    env = load_env()
    api_key = env.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        print("  OPENROUTER_API_KEY not set in .env — add it and re-run.")
        sys.exit(1)
    model = (env.get("QUICK_SCRAPE_OPENROUTER_MODEL", "") or DEFAULT_MODEL).strip() or DEFAULT_MODEL

    gsc = load_sheet_client()
    cfg = gsc.resolve_webapp() if gsc else None
    conn = connect_db()

    print(f"  13_scan-website-interface-by-ia.py — model={model}, workers={args.workers}")
    while True:
        rows = fetch_eligible_rows(gsc, cfg, conn, args.limit)
        if rows:
            run_pass(rows, conn, gsc, cfg, api_key, model, args.workers, args.dry_run)
        else:
            print("  No eligible websites right now (all scanned, or none synced yet).")

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
