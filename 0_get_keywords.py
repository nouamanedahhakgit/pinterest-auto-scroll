"""
STEP 0 — Auto-fetch trending keywords for Pinterest Auto-Scroll
===============================================================
Two sources (tries #1 first, falls back to #2):

  1. trends.pinterest.com — real Pinterest data, opens Brave
     No token needed. Uses the same Brave + Selenium setup as step 2.
     Run: python 0_get_keywords.py --pinterest

  2. Google Trends (pytrends) — no key, no browser needed
     pip install pytrends
     Run: python 0_get_keywords.py --google

  Default (no flag): tries Pinterest Trends first, falls back to Google.

OUTPUT:
  Appends new trending keywords to keywords.txt (no duplicates).
  Then run step 2 as usual.
"""

import os, sys, time, socket, subprocess, re, requests
from datetime import datetime

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
KEYWORDS_FILE = "keywords.txt"
BRAVE_PATH    = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
CDP_PORT      = 9222

# Seeds used for Google Trends fallback
GOOGLE_SEEDS = [
    "summer outfit ideas",
    "beach vacation outfit",
    "beach aesthetic outfit",
    "vacation outfit ideas",
    "aesthetic outfit 2026",
    "y2k outfit ideas",
    "coquette aesthetic outfit",
    "coastal grandmother outfit",
    "ballet core outfit",
    "sport luxe outfit",
    "hair ideas summer 2026",
    "nail art ideas 2026",
    "makeup trends 2026",
    "accessories trend",
    "sunglasses trend women",
    "sneaker trend 2026",
    "tracksuit outfit women",
    "jersey outfit ideas",
    "denim outfit ideas",
    "pinterest fashion 2026",
]
# ═══════════════════════════════════════════════════════════════

BASE = os.path.dirname(os.path.abspath(__file__))


# ── Existing keywords ──────────────────────────────────────────
def load_existing_keywords():
    path = os.path.join(BASE, KEYWORDS_FILE)
    if not os.path.exists(path):
        return set()
    kws = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                kws.add(line.lower())
    return kws


# ── CDP / Brave helpers (same as step 2) ──────────────────────
def is_cdp_available():
    try:
        s = socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def launch_brave_cdp():
    if is_cdp_available():
        print(f"  Brave already running with CDP — connecting...")
        return
    subprocess.run(["taskkill", "/F", "/IM", "brave.exe"], capture_output=True)
    time.sleep(2)
    subprocess.Popen([
        BRAVE_PATH,
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run", "--no-default-browser-check",
    ])
    for _ in range(15):
        if is_cdp_available():
            return
        time.sleep(1)


def connect_selenium():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    opts.binary_location = BRAVE_PATH
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{CDP_PORT}")
    driver = webdriver.Chrome(options=opts)
    driver.implicitly_wait(3)
    return driver


# ── Source 1: trends.pinterest.com via Selenium ───────────────
EXTRACT_JS = """
(function() {
    var kws = [];
    var seen = {};

    function add(text) {
        var t = text.trim().toLowerCase().replace(/\\s+/g, ' ');
        // Keep only keyword-shaped strings: 2-7 words, 5-60 chars
        if (!t || seen[t]) return;
        var words = t.split(' ').length;
        if (t.length < 5 || t.length > 60 || words < 1 || words > 7) return;
        // Skip obvious non-keyword strings (nav items, dates, numbers)
        if (/^(home|about|log in|sign up|english|privacy|terms|\\d+)$/i.test(t)) return;
        seen[t] = true;
        kws.push(t);
    }

    // Strategy 1: links pointing to Pinterest explore/trends pages
    document.querySelectorAll('a[href*="/explore/"], a[href*="trends.pinterest"]').forEach(function(a) {
        add(a.innerText);
    });

    // Strategy 2: elements with 'trend' or 'keyword' in their class/id
    document.querySelectorAll('[class*="trend"], [class*="keyword"], [class*="Trend"], [class*="Keyword"]').forEach(function(el) {
        add(el.innerText);
    });

    // Strategy 3: heading elements inside main content
    document.querySelectorAll('main h2, main h3, main h4, [role="main"] h2, [role="main"] h3').forEach(function(el) {
        add(el.innerText);
    });

    // Strategy 4: list items / cards that look like keyword pills
    document.querySelectorAll('li, [role="listitem"]').forEach(function(el) {
        // Only pick short text (a keyword, not a paragraph)
        var t = el.innerText.trim();
        if (t.split(' ').length <= 6) add(t);
    });

    return kws.slice(0, 200);
})();
"""

def fetch_pinterest_trends_browser():
    """
    Opens trends.pinterest.com in a real Brave tab (subprocess, so extensions
    inject and the page loads normally), then extracts trending keywords via JS.
    """
    print("  Launching Brave + Selenium...")
    launch_brave_cdp()
    time.sleep(2)

    try:
        driver = connect_selenium()
    except Exception as e:
        print(f"  ✗ Could not connect to Brave: {e}")
        return []

    url  = "https://trends.pinterest.com/"
    results = []

    try:
        existing_handles = set(driver.window_handles)
        subprocess.Popen([BRAVE_PATH, url])

        # Wait for tab to open
        new_handle = None
        for _ in range(15):
            time.sleep(1)
            new = set(driver.window_handles) - existing_handles
            if new:
                new_handle = next(iter(new))
                driver.switch_to.window(new_handle)
                break

        if not new_handle:
            print("  ⚠ New tab did not appear — trying direct navigation")
            driver.get(url)

        print("  Waiting 8s for Pinterest Trends to load...")
        time.sleep(8)

        keywords = driver.execute_script(EXTRACT_JS)
        print(f"  ✅ Pinterest Trends: {len(keywords)} keywords extracted")

        for kw in keywords:
            results.append((kw.lower(), 80, "pinterest-trends"))

    except Exception as e:
        print(f"  ⚠ Pinterest Trends scrape error: {e}")
    finally:
        # Close the trends tab
        try:
            if new_handle and len(driver.window_handles) > 1:
                driver.switch_to.window(new_handle)
                driver.execute_script("window.onbeforeunload=null;")
                driver.close()
                driver.switch_to.window(driver.window_handles[-1])
        except Exception:
            pass

    return results


# ── Source 2: Google Trends (pytrends) ────────────────────────
def fetch_google_trends(seeds):
    try:
        from pytrends.request import TrendReq
    except ImportError:
        print("  ✗  pytrends not installed.")
        print("     Run:  pip install pytrends   then retry")
        return []

    pytrends = TrendReq(hl='en-US', tz=300, timeout=(10, 30))
    results  = []
    errors   = 0

    for i, seed in enumerate(seeds):
        print(f"\r  [{i+1:2d}/{len(seeds)}] \"{seed[:45]}\"          ",
              end="", flush=True)
        try:
            pytrends.build_payload([seed], timeframe='now 30-d', geo='US')
            related = pytrends.related_queries()
            info    = related.get(seed, {})

            rising_df = info.get("rising")
            if rising_df is not None and not rising_df.empty:
                for _, row in rising_df.iterrows():
                    kw  = str(row["query"]).lower().strip()
                    val = float(row["value"])
                    if 3 < len(kw) <= 60 and len(kw.split()) <= 7:
                        results.append((kw, val, "rising"))

            top_df = info.get("top")
            if top_df is not None and not top_df.empty:
                for _, row in top_df.iterrows():
                    kw  = str(row["query"]).lower().strip()
                    val = float(row["value"]) * 0.4
                    if 3 < len(kw) <= 60 and len(kw.split()) <= 7:
                        results.append((kw, val, "top"))

            time.sleep(1.5)

        except Exception as e:
            msg = str(e)
            if "429" in msg or "Too Many" in msg:
                print(f"\n  ⚠  Rate-limited — waiting 45s...")
                time.sleep(45)
                errors += 1
                if errors >= 3:
                    print("  ⚠  Too many rate-limit errors. Try again later.")
                    break
            else:
                print(f"\n  ⚠  \"{seed}\": {e}")

    print(f"\r  ✅ Google Trends: {len(results)} raw results from {len(seeds)} seeds"
          + " " * 20)
    return results


# ── Pinterest autocomplete (bonus, no auth) ────────────────────
def fetch_autocomplete(terms):
    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36"),
        "Accept": "application/json, */*",
        "Referer": "https://www.pinterest.com/",
    })
    results = []
    for term in terms[:8]:
        try:
            r = session.get("https://www.pinterest.com/search/suggest/",
                            params={"q": term}, timeout=8)
            if r.status_code == 200:
                data  = r.json()
                items = data if isinstance(data, list) else data.get("items", [])
                for item in items:
                    kw = (item if isinstance(item, str)
                          else item.get("term", item.get("keyword", ""))).strip()
                    if kw:
                        results.append((kw.lower(), 10, "suggest"))
            time.sleep(0.7)
        except Exception:
            pass
    if results:
        print(f"  ✅ Pinterest autocomplete: {len(results)} suggestions")
    return results


# ── Dedup + rank ───────────────────────────────────────────────
def deduplicate_and_rank(raw):
    seen = {}
    for kw, score, source in raw:
        kw = kw.strip().lower()
        if not kw:
            continue
        if kw in seen:
            seen[kw]["score"] += score
        else:
            seen[kw] = {"score": score, "source": source}
    ranked = [(k, v["score"], v["source"]) for k, v in seen.items()]
    ranked.sort(key=lambda x: x[1], reverse=True)
    return ranked


def append_to_keywords_file(new_kws, label):
    path = os.path.join(BASE, KEYWORDS_FILE)
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n")
        f.write(f"# ── {label} {'─' * max(1, 52 - len(label))}\n")
        for kw, _, _ in new_kws:
            f.write(f"{kw}\n")


# ── Main ───────────────────────────────────────────────────────
def main():
    force_google    = "--google"    in sys.argv
    force_pinterest = "--pinterest" in sys.argv

    print(f"\n{'═'*62}")
    print(f"  Pinterest Keyword Fetcher  ✦  Step 0")
    print(f"{'─'*62}")

    existing = load_existing_keywords()
    print(f"  Existing in keywords.txt: {len(existing)}")

    raw = []
    source_used = "none"

    # ── Source 1: trends.pinterest.com via browser ─────────────
    if not force_google:
        print(f"\n  [1] Pinterest Trends (Brave browser)...")
        result = fetch_pinterest_trends_browser()
        if result:
            raw        += result
            source_used = "trends.pinterest.com"
        else:
            print("  → No data from Pinterest Trends browser — trying Google Trends")

    # ── Source 2: Google Trends ────────────────────────────────
    if not raw and not force_pinterest:
        print(f"\n  [2] Google Trends (pytrends)...")
        raw        += fetch_google_trends(GOOGLE_SEEDS)
        source_used = "Google Trends"
    elif raw and not force_pinterest:
        # Augment Pinterest results with autocomplete suggestions
        top_terms = [kw for kw, _, _ in raw[:10]]
        raw += fetch_autocomplete(top_terms)

    if not raw:
        print("\n  ✗ No keywords fetched.")
        print("  Try:  pip install pytrends")
        print("  Then: python 0_get_keywords.py --google\n")
        sys.exit(1)

    ranked   = deduplicate_and_rank(raw)
    new_only = [(kw, sc, src) for kw, sc, src in ranked
                if kw not in existing]

    print(f"\n{'─'*62}")
    print(f"  Source      : {source_used}")
    print(f"  Total found : {len(ranked)}")
    print(f"  Already had : {len(ranked) - len(new_only)}")
    print(f"  NEW         : {len(new_only)}")
    print(f"{'─'*62}")

    if not new_only:
        print("\n  Nothing new — keywords.txt is already up to date.\n")
        return

    show = min(40, len(new_only))
    print(f"\n  Top {show} new trending keywords:")
    print(f"  {'#':<4} {'Keyword':<42} {'Score':>6}  Source")
    print(f"  {'─'*4} {'─'*42} {'─'*6}  {'─'*12}")
    for i, (kw, score, src) in enumerate(new_only[:show], 1):
        print(f"  {i:<4} {kw:<42} {score:>6.0f}  {src}")
    if len(new_only) > show:
        print(f"  ... and {len(new_only)-show} more")

    print(f"\n{'─'*62}")
    print(f"  How many to add to keywords.txt?")
    print(f"  Enter number, ENTER = all {len(new_only)}, 0 = cancel")
    while True:
        raw_input = input(f"  >> [{len(new_only)}]: ").strip()
        if raw_input == "":
            to_add = new_only; break
        if raw_input == "0":
            print("  Cancelled.\n"); return
        try:
            n = int(raw_input)
            if 1 <= n <= len(new_only):
                to_add = new_only[:n]; break
            print(f"  Enter 0–{len(new_only)}.")
        except ValueError:
            print("  Enter a number.")

    date_str = datetime.now().strftime("%Y-%m-%d")
    label    = f"Trending {date_str} — {source_used}"
    append_to_keywords_file(to_add, label)

    print(f"\n  ✅ {len(to_add)} keywords added to keywords.txt")
    print(f"\n  Next: python 2_pinterest_auto_scroll.py --5m\n")


if __name__ == "__main__":
    main()
