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
  A new `site_type` column is added to the local `pinners` table. At the
  start of every pass this script bulk-syncs it straight from the Google
  Sheet's "websites" tab (`get_websites` action — same data step 10/13 write),
  matching purely by username (the Sheet's `id` column = pinner username, no
  domain-guessing needed). After that one sync call, the blog filter runs
  entirely off the local DB — no per-pin Sheet round-trips. A pinner counts
  as "blog" when `site_type` contains "blog" (case-insensitive) — same
  substring check used by magic_scroll.py's --blog-only.

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
  python 14_download_blog_pin_links.py                       # bot mode: download all, then poll every 10 min
  python 14_download_blog_pin_links.py --once                # single pass, then exit
  python 14_download_blog_pin_links.py --workers 120          # more parallel threads (default 80)
  python 14_download_blog_pin_links.py --poll-minutes 5       # check more often than 10 min
  python 14_download_blog_pin_links.py --retry-failed         # also re-attempt Failed/Blocked pins, not just untried ones
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

def run_pass(units: list, conn, workers: int, dry_run: bool) -> int:
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
        write_unit_result(conn, res["pin_ids"], res["status"], res["html"], res["css"], res["js"])

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
    ap.add_argument("--retry-failed", action="store_true", help="also re-attempt pins with a Failed/Blocked status, not just untried ones")
    ap.add_argument("--dry-run", action="store_true", help="download + print only, write nothing")
    args = ap.parse_args()

    gsc = load_sheet_client()
    cfg = gsc.resolve_webapp() if gsc else None
    conn = connect_db()

    print(f"  14_download_blog_pin_links.py — workers={args.workers}")
    while True:
        sync_site_types(gsc, cfg, conn)
        apply_local_site_type_fallback(conn)
        units = fetch_eligible_units(conn, args.limit, args.retry_failed)
        if units:
            run_pass(units, conn, args.workers, args.dry_run)
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
