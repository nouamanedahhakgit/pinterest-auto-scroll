"""
magic_scroll.py — multi-computer Pinterest auto-scrape orchestrator
====================================================================
Run this on AS MANY COMPUTERS AS YOU LIKE at the same time. Each one loops:

  1. CLAIM up to 10 keywords from the Google Sheet  → status "pending"
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
  python magic_scroll.py             # 15 minutes per keyword, batch size 10 (default)
  python magic_scroll.py --2m        # 2 minutes per keyword
  python magic_scroll.py --10m       # 10 minutes per keyword
  python magic_scroll.py --batch 10  # 10 keywords claimed per cycle
  python magic_scroll.py --kw 10     # 10 keywords claimed per cycle (alternative)
  python magic_scroll.py --10kw      # 10 keywords claimed per cycle (alternative)
  python magic_scroll.py --10        # 10 keywords claimed per cycle (alternative)
  python magic_scroll.py --disk      # build DB from disk (needs ccl_chromium_reader)
"""

import os, sys, time, socket, subprocess, re, json, datetime, platform

BASE            = os.path.dirname(os.path.abspath(__file__))
CDP_PORT        = 9222
BRAVE_PATH      = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
BRAVE_PROFILE   = "MagicScroll"   # separate profile — install SortPin + login to Pinterest here once
PY              = sys.executable
LOG_PATH        = os.path.join(BASE, "magic_log.jsonl")
ENV_PATH        = os.path.join(BASE, ".env")

_brave_proc = None   # track our Brave PID — only kill OUR instance, not all Brave windows

def load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env

ENV_VARS = load_env()

def log_event(**ev):
    """Append one job-log line (JSONL). The step-5 server shows these live."""
    ev["ts"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ev["computer"] = platform.node()
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass

# ── CLI options ───────────────────────────────────────────────────────────────
def _minutes():
    for a in sys.argv[1:]:
        m = re.match(r"^--(\d+(?:\.\d+)?)m$", a)
        if m:
            return float(m.group(1))
    try:
        return float(ENV_VARS.get("DEFAULT_SCROLL_MINUTES", "15.0"))
    except ValueError:
        return 15.0

def _batch():
    args = sys.argv[1:]
    # Check for --batch <N>
    for i, a in enumerate(args):
        if a == "--batch" and i + 1 < len(args):
            try: return int(args[i + 1])
            except ValueError: pass
    # Check for --kw <N>
    for i, a in enumerate(args):
        if a == "--kw" and i + 1 < len(args):
            try: return int(args[i + 1])
            except ValueError: pass
    # Check for --<N>kw (e.g. --10kw)
    for a in args:
        m = re.match(r"^--(\d+)kw$", a)
        if m:
            return int(m.group(1))
    # Check for --<N> where N is an integer (e.g. --10)
    for a in args:
        m = re.match(r"^--(\d+)$", a)
        if m:
            return int(m.group(1))
    try:
        return int(ENV_VARS.get("DEFAULT_BATCH_SIZE", "10"))
    except ValueError:
        return 10


MINUTES   = _minutes()
BATCH     = _batch()
BUILD_ARGS = ["4_build_database.py", "--no-csv"] + (["--disk"] if "--disk" in sys.argv[1:] else [])

# pagination-end detection: stop a keyword early when no new pins load
STALL_SECS  = 40           # "no more pins" when page height stops growing this long
SAMPLE_SECS = 6            # how often to check the page height
_HEIGHT_JS  = "return document.documentElement.scrollHeight || document.body.scrollHeight || 0;"

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

def _kill_our_brave():
    global _brave_proc
    if _brave_proc is not None:
        try:
            _brave_proc.kill()
        except Exception:
            pass
        _brave_proc = None
        time.sleep(2)

def _find_brave():
    candidates = [
        BRAVE_PATH,
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe"),
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

def ensure_brave():
    global _brave_proc
    if _cdp_up():
        return True
    _kill_our_brave()   # kill only OUR Brave — regular Brave windows are untouched

    brave = _find_brave()
    if not brave:
        print(f"  ERROR: brave.exe not found. Checked: {BRAVE_PATH}")
        print("  Set BRAVE_PATH at the top of magic_scroll.py to the correct location.")
        return False

    _brave_proc = subprocess.Popen([
        brave,
        f"--remote-debugging-port={CDP_PORT}",
        f"--profile-directory={BRAVE_PROFILE}",
        "--no-first-run", "--no-default-browser-check",
        "--disable-features=Translate",
        "--disable-default-apps",
        "--disable-sync",
    ])
    # Wait up to 60s — first launch of MagicScroll profile can be slow
    for i in range(60):
        if _cdp_up():
            time.sleep(2); return True
        if i == 10:
            print("  (waiting for Brave... if first launch of MagicScroll profile, takes ~20s)")
        time.sleep(1)
    print(f"  ERROR: Brave started (PID {_brave_proc.pid}) but CDP port {CDP_PORT} never opened.")
    print(f"  • Make sure MagicScroll profile exists in Brave (Profiles menu → Add profile)")
    print(f"  • Install SortPin + log into Pinterest in that profile")
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
    driver.execute_script(f"window.open('{url}', '_blank')")  # open inside our Brave, not the user's
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
        try:
            btns = driver.find_elements(By.TAG_NAME, "button")
        except Exception:
            time.sleep(2)
            continue

        # Check if already scrolling
        has_stop = False
        for b in btns:
            try:
                if "stop scroll" in (b.text or "").lower():
                    has_stop = True
                    break
            except Exception:
                pass

        if has_stop:
            return True                                    # already scrolling

        for b in btns:
            try:
                t = (b.text or "").lower()
            except Exception:
                continue
            if "start scroll" in t:
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", b)
                    time.sleep(0.2)
                    b.click()
                    time.sleep(1.5)
                    
                    # Verify it flipped to stop scroll
                    has_stop_after = False
                    for x in driver.find_elements(By.TAG_NAME, "button"):
                        try:
                            if "stop scroll" in (x.text or "").lower():
                                has_stop_after = True
                                break
                        except Exception:
                            pass
                    if has_stop_after:
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

def wait_pins(driver, max_secs):
    """Like step 2: keep scrolling, but STOP EARLY when the page stops loading
    new pins (height hasn't grown for STALL_SECS). `max_secs` is just a cap so a
    fast/endless page still moves on eventually. Returns why it stopped."""
    end_at = time.time() + max_secs
    last_h, last_grow, next_sample = 0, time.time(), time.time() + SAMPLE_SECS
    while time.time() < end_at:
        now = time.time()
        if now >= next_sample:
            next_sample = now + SAMPLE_SECS
            try: h = int(driver.execute_script(_HEIGHT_JS) or 0)
            except Exception: h = last_h
            if h > last_h + 50:                 # new pins loaded → keep going
                last_h, last_grow = h, now
            elif last_h > 0 and now - last_grow >= STALL_SECS:
                return "no more pins"           # exhausted → next keyword
        time.sleep(1)
    return "time cap"

_PINCOUNT_JS = ("return (document.querySelectorAll('div[data-test-id=\"pin\"]').length"
                " || document.querySelectorAll('div[role=\"listitem\"]').length || 0);")

def _count_pins(driver):
    try: return int(driver.execute_script(_PINCOUNT_JS) or 0)
    except Exception: return 0

def scroll_keyword(driver, kw, base_tab, minutes, cycle=0):
    print(f"    ▶ '{kw}' — up to {minutes:g} min")
    t0 = time.time()
    open_keyword_tab(driver, kw)
    time.sleep(6)
    started = click_start(driver)
    print("      " + ("SortPin scrolling" if started else "⚠ could not start SortPin (continuing)"))
    why = wait_pins(driver, int(minutes * 60))   # ends early when no new pins
    pins = _count_pins(driver)
    secs = int(time.time() - t0)
    print(f"      → moving on ({why}) — {secs}s, ~{pins} pins on page")
    close_pinterest_tabs(driver, base_tab)
    log_event(event="keyword", cycle=cycle, keyword=kw, seconds=secs,
              minutes=round(secs / 60, 1), pins=pins, why=why,
              started=bool(started))
    return {"keyword": kw, "seconds": secs, "pins": pins, "why": why}

# ── run the existing steps as subprocesses ────────────────────────────────────
def run_step(label, args):
    print(f"    → {label}")
    subprocess.run([PY] + args, cwd=BASE)

# ── main loop ─────────────────────────────────────────────────────────────────
def reset_pending_keywords(gsc, cfg):
    print("\n=== Reset Pending Keywords → Not Yet ===")
    try:
        data = gsc.post_webapp(cfg, {"action": "reset_pending_keywords"})
        n = data.get("reset", 0)
        print(f"  Reset {n} keyword(s) from 'pending' → 'Not Yet'.")
    except Exception as e:
        print(f"  Failed: {e}")

def main():
    if "--reset-keywords" in sys.argv:
        gsc = load_sheet_client()
        cfg = gsc.resolve_webapp() if gsc else None
        if cfg:
            reset_pending_keywords(gsc, cfg)
        else:
            print("  No google_sheets_webapp.json found.")
        sys.exit(0)

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
        log_event(event="claim", cycle=cycle, keywords=kws, count=len(kws))

        # scroll every claimed keyword — keep Brave open the whole cycle
        if not ensure_brave():
            print("  Could not start Brave — releasing keywords is not automatic; "
                  "set them back to 'Not Yet' on the sheet if needed.")
            break
        driver = connect()
        base_tab = driver.current_window_handle
        cyc_start = time.time()
        scrolled_kws = []
        for idx, kw in enumerate(kws):
            # Reconnect only if Brave died mid-cycle
            if not _cdp_up():
                print(f"  Brave lost — reconnecting before '{kw}'…")
                if not ensure_brave():
                    print("  Could not restart Brave. Stopping cycle early.")
                    break
                driver = connect()
                base_tab = driver.current_window_handle

            scroll_keyword(driver, kw, base_tab, MINUTES, cycle=cycle)
            scrolled_kws.append(kw)
            # Just close Pinterest tab — keep Brave alive for next keyword
            close_pinterest_tabs(driver, base_tab)

        # ── Build DB once for the whole cycle (not once per keyword) ──────────
        try:
            driver.quit()
        except Exception:
            pass
        _kill_our_brave()   # close only the MagicScroll Brave, not the user's windows
        if scrolled_kws:
            run_step(f"build database ({len(scrolled_kws)} keywords)", BUILD_ARGS)

            # ── Sync to MySQL then clear local SQLite ─────────────────────────
            db_path = os.path.join(BASE, "sortpin.db")
            synced = False
            if os.path.exists(db_path):
                try:
                    import sqlite3
                    con = sqlite3.connect(db_path)
                    db_pins_after    = con.execute("SELECT COUNT(*) FROM pins").fetchone()[0]
                    db_created_after = con.execute("SELECT COUNT(*) FROM pins WHERE pin_type='created'").fetchone()[0]
                    db_saved_after   = con.execute("SELECT COUNT(*) FROM pins WHERE pin_type='saved'").fetchone()[0]
                    con.close()
                except Exception:
                    db_pins_after = db_created_after = db_saved_after = 0

                print(f"  [Magic Status] Pins in local DB: {db_pins_after} "
                      f"(created: {db_created_after}, saved: {db_saved_after})")

                # Push to MySQL
                sync_result = subprocess.run(
                    [PY, "8_sync_to_mysql.py"], cwd=BASE, capture_output=False
                )
                synced = sync_result.returncode == 0

                if synced:
                    # Clear local SQLite — MySQL is now the source of truth
                    try:
                        os.remove(db_path)
                        print("  Local sortpin.db cleared — MySQL holds all data.")
                    except Exception as e:
                        print(f"  Warning: could not delete sortpin.db: {e}")
                else:
                    print("  MySQL sync failed — keeping sortpin.db locally (data safe).")
            else:
                db_pins_after = db_created_after = db_saved_after = 0

            run_step("clear SortPin", ["6_clear_sortpin.py", "--yes"])

        print(f"  [Magic Status] Total Pins this cycle: {db_pins_after} (created: {db_created_after}, saved: {db_saved_after})")

        log_event(event="cycle_done", cycle=cycle, keywords=kws,
                  seconds=int(time.time() - cyc_start),
                  total_pins=db_pins_after,
                  created_pins=db_created_after,
                  saved_pins=db_saved_after,
                  new_pins=0)

    print(f"\n  Magic scroll finished after {cycle - 1} full cycle(s).\n")

if __name__ == "__main__":
    main()
