"""
magic_scroll.py — multi-computer Pinterest auto-scrape orchestrator
====================================================================
Run this on AS MANY COMPUTERS AS YOU LIKE at the same time. Each one loops:

  1. CLAIM up to 5 keywords from the Google Sheet  → status "pending"
       (atomic via the Apps Script LockService — two PCs never grab the same one)
  2. SCROLL each keyword in Brave (SortPin saves the pins)
  3. BUILD the database  → python 4_build_database.py
       (saves ALL scraped data to sortpin.db + IMPORTANT_DATABASE/sortpin_mysql.sql)
  4. MARK those keywords "Done" on the Sheet
  5. CLEAR SortPin → python 6_clear_sortpin.py --yes
       (archives a backup into _SORTPIN_ARCHIVE/ first, then empties the extension)
  6. Repeat until no keywords are left.

REQUIREMENTS (one-time):
  • Re-deploy the Apps Script (google_sheets_apps_script.js, version 3 with
    claim/mark) — Extensions → Apps Script → paste → Deploy → Manage deployments
    → Edit → New version. google_sheets_webapp.json must hold the /exec URL.
  • The Sheet's column A = keywords, column D = Status (run step 1 once).
  • pip install selenium

Run:
  python magic_scroll.py             # 5 minutes per keyword (default)
  python magic_scroll.py --2m        # 2 minutes per keyword
  python magic_scroll.py --batch 5   # keywords claimed per cycle (default 5)
  python magic_scroll.py --disk      # build DB from disk (needs ccl_chromium_reader)
"""

import os, sys, time, socket, subprocess, re

BASE       = os.path.dirname(os.path.abspath(__file__))
CDP_PORT   = 9222
BRAVE_PATH = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
PY         = sys.executable

# ── CLI options ───────────────────────────────────────────────────────────────
def _minutes():
    for a in sys.argv[1:]:
        m = re.match(r"^--(\d+(?:\.\d+)?)m$", a)
        if m:
            return float(m.group(1))
    return 5.0

def _batch():
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a == "--batch" and i + 1 < len(args):
            try: return int(args[i + 1])
            except ValueError: pass
    return 5

MINUTES   = _minutes()
BATCH     = _batch()
BUILD_ARGS = ["4_build_database.py"] + (["--disk"] if "--disk" in sys.argv[1:] else [])

# ── Google Sheet (web app: claim / mark) ──────────────────────────────────────
def load_sheet_client():
    try:
        import google_sheets_client as gsc
        return gsc
    except Exception as e:
        print(f"  Could not load google_sheets_client.py — {e}")
        return None

def claim_keywords(gsc, cfg, n):
    data = gsc.post_webapp(cfg, {"action": "claim", "count": n})
    return data.get("claimed", [])

def mark_done(gsc, cfg, keywords):
    if not keywords:
        return
    gsc.post_webapp(cfg, {"action": "mark", "keywords": keywords, "status": "Done"})

# ── Brave + Selenium (self-contained scroll) ──────────────────────────────────
def _cdp_up():
    try:
        s = socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1); s.close()
        return True
    except OSError:
        return False

def ensure_brave():
    if _cdp_up():
        return True
    subprocess.run(["taskkill", "/F", "/IM", "brave.exe"], capture_output=True)
    time.sleep(2)
    subprocess.Popen([BRAVE_PATH, f"--remote-debugging-port={CDP_PORT}",
                      "--no-first-run", "--no-default-browser-check"])
    for _ in range(15):
        if _cdp_up():
            time.sleep(2); return True
        time.sleep(1)
    return False

def connect():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    o = Options()
    o.add_experimental_option("debuggerAddress", f"127.0.0.1:{CDP_PORT}")
    d = webdriver.Chrome(options=o); d.implicitly_wait(3)
    return d

def open_keyword_tab(driver, kw):
    url = "https://www.pinterest.com/search/pins/?q=" + kw.replace(" ", "+") + "&rs=typed"
    existing = set(driver.window_handles)
    subprocess.Popen([BRAVE_PATH, url])
    for _ in range(15):
        time.sleep(1)
        new = set(driver.window_handles) - existing
        if new:
            driver.switch_to.window(next(iter(new))); return
    if driver.window_handles:
        driver.switch_to.window(driver.window_handles[-1])

def click_start(driver):
    """Native (trusted) click on SortPin's 'Start Scroll' for the current tab."""
    from selenium.webdriver.common.by import By
    for _ in range(12):
        btns = driver.find_elements(By.TAG_NAME, "button")
        if any("stop scroll" in (b.text or "").lower() for b in btns):
            return True                                    # already scrolling
        for b in btns:
            try: t = (b.text or "").lower()
            except Exception: continue
            if "start scroll" in t:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", b)
                    time.sleep(0.2); b.click(); time.sleep(1.5)
                    if any("stop scroll" in (x.text or "").lower()
                           for x in driver.find_elements(By.TAG_NAME, "button")):
                        return True
                except Exception:
                    pass
        time.sleep(2)
    return False

def close_pinterest_tabs(driver, base_tab):
    for h in list(driver.window_handles):
        if h == base_tab:
            continue
        if len(driver.window_handles) <= 1:
            break
        try:
            driver.switch_to.window(h)
            try: driver.execute_script("window.onbeforeunload=null;")
            except Exception: pass
            if "pinterest.com" in (driver.current_url or ""):
                driver.close()
        except Exception:
            continue
    try:
        if base_tab in driver.window_handles:
            driver.switch_to.window(base_tab)
        elif driver.window_handles:
            driver.switch_to.window(driver.window_handles[-1])
    except Exception:
        pass

def scroll_keyword(driver, kw, base_tab, minutes):
    print(f"    ▶ '{kw}' — {minutes:g} min")
    open_keyword_tab(driver, kw)
    time.sleep(6)
    print("      " + ("SortPin scrolling" if click_start(driver)
                      else "⚠ could not start SortPin (continuing)"))
    time.sleep(int(minutes * 60))
    close_pinterest_tabs(driver, base_tab)

# ── run the existing steps as subprocesses ────────────────────────────────────
def run_step(label, args):
    print(f"    → {label}")
    subprocess.run([PY] + args, cwd=BASE)

# ── main loop ─────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*62}\n  MAGIC SCROLL — {BATCH} keywords/cycle · {MINUTES:g} min each\n{'='*62}")
    gsc = load_sheet_client()
    cfg = gsc.resolve_webapp() if gsc else None
    if not cfg:
        print("  No Google Sheet web app (google_sheets_webapp.json) — cannot claim.\n")
        sys.exit(1)

    cycle = 0
    while True:
        cycle += 1
        print(f"\n── cycle {cycle}: claiming up to {BATCH} keywords ──")
        try:
            claimed = claim_keywords(gsc, cfg, BATCH)
        except Exception as e:
            print(f"  claim failed — {e}\n  (Re-deploy the Apps Script v3 with claim/mark.)")
            break
        if not claimed:
            print("  No keywords left to claim (all Done/pending). Finished. 🎉")
            break
        kws = [c["keyword"] for c in claimed]
        print("  claimed (pending):", ", ".join(kws))

        # scroll every claimed keyword
        if not ensure_brave():
            print("  Could not start Brave — releasing keywords is not automatic; "
                  "set them back to 'Not Yet' on the sheet if needed.")
            break
        driver = connect()
        base_tab = driver.current_window_handle
        for kw in kws:
            scroll_keyword(driver, kw, base_tab, MINUTES)

        # save scraped data BEFORE clearing the extension
        run_step("build database", BUILD_ARGS)

        # mark done on the sheet
        try:
            mark_done(gsc, cfg, kws)
            print("  marked Done:", ", ".join(kws))
        except Exception as e:
            print(f"  ⚠ could not mark Done — {e}")

        # clear SortPin (archives a backup first); this closes Brave
        run_step("clear SortPin", ["6_clear_sortpin.py", "--yes"])
        try: driver.quit()
        except Exception: pass

    print(f"\n  Magic scroll finished after {cycle - 1} full cycle(s).\n")

if __name__ == "__main__":
    main()
