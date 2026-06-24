"""
STEP 0 — Auto-fetch trending keywords for Pinterest Auto-Scroll
===============================================================
Two sources (tries #1 first, falls back to #2):

  1. Pinterest Trends API v5  — real Pinterest data, free token
     Setup (one time, ~2 min):
       a. Go to https://developers.pinterest.com
       b. Sign in with your Pinterest business account
       c. My Apps → Create app → Generate access token
          (check scope: user_accounts:read + catalogs:read)
       d. Paste the token below as PINTEREST_TOKEN = "..."

  2. Google Trends (pytrends)  — no key, no setup, auto-fallback
     pip install pytrends requests

RUN:
  python 0_get_keywords.py              # auto (Pinterest first)
  python 0_get_keywords.py --google     # force Google Trends
  python 0_get_keywords.py --pinterest  # force Pinterest API only

OUTPUT:
  Appends new trending keywords to keywords.txt (no duplicates).
  Then run step 2 as usual.
"""

import os, sys, time, json, re, requests
from datetime import datetime

# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════
PINTEREST_TOKEN = os.environ.get("PINTEREST_TOKEN", "")
# ↑ Paste your token here OR set env var:  set PINTEREST_TOKEN=your_token

KEYWORDS_FILE = "keywords.txt"

# Seeds used when falling back to Google Trends
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

# Pinterest Trends API — trend types to pull
PINTEREST_TREND_TYPES = ["growing", "monthly", "yearly"]
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


# ── Pinterest Trends API v5 ────────────────────────────────────
def fetch_pinterest_trends(token):
    """
    Returns list of (keyword, score, trend_type) or None on auth failure.
    Tries growing / monthly / yearly endpoints.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    results = []

    for trend_type in PINTEREST_TREND_TYPES:
        url = (f"https://api.pinterest.com/v5/trends/keywords"
               f"/{trend_type}/trending/")
        params = {
            "region": "US",
            "trend_window_days": 90,
            "limit": 50,
        }
        try:
            r = requests.get(url, headers=headers, params=params, timeout=15)
        except Exception as e:
            print(f"  ⚠  Network error ({trend_type}): {e}")
            continue

        if r.status_code == 401:
            print("  ✗  Pinterest API: token invalid or expired (401)")
            print("     Get a new token at https://developers.pinterest.com")
            return None
        if r.status_code == 403:
            print("  ✗  Pinterest API: access denied (403)")
            print("     Make sure your app has 'user_accounts:read' scope")
            return None
        if r.status_code != 200:
            print(f"  ⚠  Pinterest API {trend_type}: HTTP {r.status_code}")
            continue

        data = r.json()
        # API may return 'trends' or 'items' depending on version
        items = data.get("trends") or data.get("items") or []
        for item in items:
            kw    = (item.get("keyword") or item.get("keyword_name") or "").strip()
            score = float(item.get("normalized_score_in_trend_window")
                          or item.get("trend_score") or 50)
            if kw:
                results.append((kw.lower(), score, trend_type))
        print(f"  ✅ Pinterest {trend_type:8s}: {len(items):3d} keywords")
        time.sleep(0.5)

    return results if results else []


# ── Google Trends (pytrends fallback) ─────────────────────────
def fetch_google_trends(seeds):
    try:
        from pytrends.request import TrendReq
    except ImportError:
        print("  ✗  pytrends not installed.")
        print("     Run:  pip install pytrends")
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

            info = related.get(seed, {})

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
                    val = float(row["value"]) * 0.4   # lower weight than rising
                    if 3 < len(kw) <= 60 and len(kw.split()) <= 7:
                        results.append((kw, val, "top"))

            time.sleep(1.5)

        except Exception as e:
            msg = str(e)
            if "429" in msg or "Too Many" in msg:
                print(f"\n  ⚠  Rate-limited by Google — waiting 45s...")
                time.sleep(45)
                errors += 1
                if errors >= 3:
                    print("  ⚠  Too many rate-limit errors. Try again in a few minutes.")
                    break
            else:
                print(f"\n  ⚠  Error for \"{seed}\": {e}")

    print(f"\r  ✅ Google Trends: {len(results)} raw results from {len(seeds)} seeds"
          + " " * 20)
    return results


# ── Pinterest autocomplete (no token, bonus keywords) ─────────
def fetch_pinterest_autocomplete(terms):
    """
    Hits Pinterest's internal search-suggest endpoint for each term.
    Requires no auth — just a session UA. Returns (kw, score=10, 'suggest') list.
    """
    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/125.0.0.0 Safari/537.36"),
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": "https://www.pinterest.com/",
    })
    results = []
    for term in terms[:10]:   # limit to avoid bans
        try:
            url = "https://www.pinterest.com/search/suggest/"
            r   = session.get(url, params={"q": term}, timeout=10)
            if r.status_code == 200:
                data = r.json()
                # Response may be a list of strings or dicts
                items = data if isinstance(data, list) else data.get("items", [])
                for item in items:
                    kw = (item if isinstance(item, str)
                          else item.get("term", item.get("keyword", ""))).strip()
                    if kw:
                        results.append((kw.lower(), 10, "suggest"))
            time.sleep(0.8)
        except Exception:
            pass
    return results


# ── Helpers ────────────────────────────────────────────────────
def deduplicate_and_rank(raw):
    """
    Merge duplicates (sum scores), return sorted list of
    (keyword, total_score, source) highest first.
    """
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

    # ── Source 1: Pinterest API ────────────────────────────────
    if PINTEREST_TOKEN and not force_google:
        print(f"\n  [1/2] Pinterest Trends API...")
        result = fetch_pinterest_trends(PINTEREST_TOKEN)
        if result is not None and len(result) > 0:
            raw        += result
            source_used = "Pinterest API v5"
        elif result is None:
            print("  → Falling back to Google Trends")

    # ── Source 2: Google Trends ────────────────────────────────
    if not raw and not force_pinterest:
        print(f"\n  [2/2] Google Trends (pytrends)...")
        raw        += fetch_google_trends(GOOGLE_SEEDS)
        source_used = "Google Trends"

    # ── Bonus: Pinterest autocomplete ──────────────────────────
    if raw and not force_pinterest:
        print(f"\n  [+]   Pinterest autocomplete (bonus suggestions)...")
        seeds_for_suggest = [kw for kw, _, src in raw[:8] if src in ("growing","rising")]
        raw += fetch_pinterest_autocomplete(seeds_for_suggest)

    if not raw:
        print("\n  ✗ No keywords fetched.")
        if not PINTEREST_TOKEN:
            print("  Tip: set PINTEREST_TOKEN or make sure pytrends is installed.")
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

    # Preview top 40
    show = min(40, len(new_only))
    print(f"\n  Top {show} new trending keywords:")
    print(f"  {'#':<4} {'Keyword':<42} {'Score':>6}  Source")
    print(f"  {'─'*4} {'─'*42} {'─'*6}  {'─'*10}")
    for i, (kw, score, src) in enumerate(new_only[:show], 1):
        print(f"  {i:<4} {kw:<42} {score:>6.0f}  {src}")
    if len(new_only) > show:
        print(f"  ... and {len(new_only)-show} more")

    # Ask how many to add
    print(f"\n{'─'*62}")
    print(f"  How many to add to keywords.txt?")
    print(f"  Enter a number, ENTER = all {len(new_only)}, 0 = cancel")
    while True:
        raw_input = input(f"  >> [{len(new_only)}]: ").strip()
        if raw_input == "":
            to_add = new_only
            break
        if raw_input == "0":
            print("  Cancelled — nothing written.\n")
            return
        try:
            n = int(raw_input)
            if 1 <= n <= len(new_only):
                to_add = new_only[:n]
                break
            print(f"  Enter a number from 0 to {len(new_only)}.")
        except ValueError:
            print("  Enter a number.")

    date_str = datetime.now().strftime("%Y-%m-%d")
    label    = f"Trending {date_str} — {source_used}"
    append_to_keywords_file(to_add, label)

    print(f"\n  ✅ {len(to_add)} keywords added to keywords.txt")
    print(f"\n  Next:")
    print(f"    python 2_pinterest_auto_scroll.py --5m")
    print(f"    (new keywords are at the bottom of keywords.txt)\n")


if __name__ == "__main__":
    main()
