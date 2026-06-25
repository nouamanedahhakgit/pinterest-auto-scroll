"""
STEP 7 — Deep-scrape each pinner: profile → boards → pins
=========================================================
For every pinner already in sortpin.db this:
  1. opens the pinner's PROFILE in Brave and scrolls it (SortPin captures the
     pinner's boards + profile statistics),
  2. opens EACH of that pinner's boards and scrolls it TILL THE END
     (until the page stops loading new pins),
so afterwards you have, for each pinner: all their boards, all the pins in
those boards, and the pinner's statistics.

It saves to the database periodically (runs step 4), and is RESUMABLE — it
records finished boards in profiles_progress.json and skips them next time.
Runs alongside magic_scroll / on many computers (each does different pinners).

Run:
  python 7_scrape_profiles.py                 # process 20 pinners, 10 min cap/board
  python 7_scrape_profiles.py --limit 50      # process 50 pinners this run
  python 7_scrape_profiles.py --max-min 5     # cap each board at 5 minutes
  python 7_scrape_profiles.py --disk          # build DB from disk when saving
"""

import os, sys, time, socket, subprocess, json, sqlite3, re

BASE       = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE, "sortpin.db")
PROG_PATH  = os.path.join(BASE, "profiles_progress.json")
CDP_PORT   = 9222
BRAVE_PATH = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
PY         = sys.executable

PROFILE_SECS = 45          # seconds to scroll a profile (load its boards)
STALL_SECS   = 40          # board "finished" when height stops growing this long
SAMPLE_SECS  = 6           # how often to sample page height
SAVE_EVERY   = 5           # build the DB after this many pinners

def _opt_int(flag, default):
    a = sys.argv[1:]
    for i, x in enumerate(a):
        if x == flag and i + 1 < len(a):
            try: return int(a[i + 1])
            except ValueError: pass
    return default

LIMIT    = _opt_int("--limit", 20)
MAX_MIN  = _opt_int("--max-min", 10)
BUILD_ARGS = ["4_build_database.py"] + (["--disk"] if "--disk" in sys.argv[1:] else [])

# ── progress ──────────────────────────────────────────────────────────────────
def load_prog():
    if os.path.exists(PROG_PATH):
        try:
            with open(PROG_PATH, encoding="utf-8") as f:
                d = json.load(f)
            return set(d.get("boards", [])), set(d.get("pinners", []))
        except Exception:
            pass
    return set(), set()

def save_prog(boards_done, pinners_done):
    with open(PROG_PATH, "w", encoding="utf-8") as f:
        json.dump({"boards": sorted(boards_done), "pinners": sorted(pinners_done)}, f)

# ── read pinners + their boards from the DB ───────────────────────────────────
def load_targets():
    if not os.path.exists(DB_PATH):
        print("  sortpin.db not found — run step 4 first."); sys.exit(1)
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    pinners = [dict(r) for r in con.execute(
        "SELECT username, full_name, follower_count FROM pinners "
        "WHERE username IN (SELECT DISTINCT owner_username FROM boards WHERE owner_username IS NOT NULL) "
        "ORDER BY follower_count DESC")]
    boards_by = {}
    for r in con.execute("SELECT id, url, owner_username, name FROM boards "
                         "WHERE url IS NOT NULL AND url<>''"):
        boards_by.setdefault(r["owner_username"], []).append(dict(r))
    con.close()
    return pinners, boards_by

# ── Brave + Selenium ──────────────────────────────────────────────────────────
def _cdp_up():
    try:
        s = socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1); s.close(); return True
    except OSError:
        return False

def ensure_brave():
    if _cdp_up(): return True
    subprocess.run(["taskkill", "/F", "/IM", "brave.exe"], capture_output=True); time.sleep(2)
    subprocess.Popen([BRAVE_PATH, f"--remote-debugging-port={CDP_PORT}",
                      "--no-first-run", "--no-default-browser-check"])
    for _ in range(15):
        if _cdp_up(): time.sleep(2); return True
        time.sleep(1)
    return False

def connect():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    o = Options(); o.add_experimental_option("debuggerAddress", f"127.0.0.1:{CDP_PORT}")
    d = webdriver.Chrome(options=o); d.implicitly_wait(3); return d

def open_tab(driver, url):
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
    from selenium.webdriver.common.by import By
    for _ in range(10):
        btns = driver.find_elements(By.TAG_NAME, "button")
        if any("stop scroll" in (b.text or "").lower() for b in btns):
            return True
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
                except Exception: pass
        time.sleep(2)
    return False

_HEIGHT_JS = "return document.documentElement.scrollHeight || document.body.scrollHeight || 0;"

def scroll_till_finish(driver, max_secs):
    """Scroll until the page stops growing for STALL_SECS, or max_secs elapses."""
    end_at = time.time() + max_secs
    last_h, last_grow, next_sample = 0, time.time(), time.time() + SAMPLE_SECS
    while time.time() < end_at:
        now = time.time()
        if now >= next_sample:
            next_sample = now + SAMPLE_SECS
            try: h = int(driver.execute_script(_HEIGHT_JS) or 0)
            except Exception: h = last_h
            if h > last_h + 50:
                last_h, last_grow = h, now
            elif last_h > 0 and now - last_grow >= STALL_SECS:
                return "end"
        time.sleep(1)
    return "timeout"

def close_tabs(driver, base):
    for h in list(driver.window_handles):
        if h == base or len(driver.window_handles) <= 1:
            continue
        try:
            driver.switch_to.window(h)
            try: driver.execute_script("window.onbeforeunload=null;")
            except Exception: pass
            if "pinterest.com" in (driver.current_url or ""):
                driver.close()
        except Exception:
            continue
    try:
        driver.switch_to.window(base if base in driver.window_handles else driver.window_handles[-1])
    except Exception:
        pass

def run_step(label, args):
    print(f"    → {label}")
    subprocess.run([PY] + args, cwd=BASE)

# ── main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n{'='*62}\n  STEP 7 — deep-scrape profiles → boards → pins\n"
          f"  {LIMIT} pinners this run · {MAX_MIN} min cap/board\n{'='*62}")
    pinners, boards_by = load_targets()
    boards_done, pinners_done = load_prog()
    todo = [p for p in pinners if p["username"] not in pinners_done][:LIMIT]
    if not todo:
        print("  Nothing left — all known pinners processed. (delete profiles_progress.json to redo)\n")
        return
    if not ensure_brave():
        print("  Could not start Brave."); sys.exit(1)
    driver = connect(); base = driver.current_window_handle
    processed = 0
    try:
        for p in todo:
            u = p["username"]; brds = boards_by.get(u, [])
            print(f"\n▶ @{u}  ({p.get('full_name') or ''}) — {len(brds)} boards")
            # 1) profile (captures boards + stats)
            open_tab(driver, f"https://www.pinterest.com/{u}/")
            time.sleep(5); click_start(driver); time.sleep(PROFILE_SECS)
            close_tabs(driver, base)
            # 2) each board till the end
            for b in brds:
                if b["id"] in boards_done:
                    continue
                print(f"    • board '{(b.get('name') or '')[:34]}' …", end="", flush=True)
                open_tab(driver, b["url"]); time.sleep(5)
                click_start(driver)
                why = scroll_till_finish(driver, MAX_MIN * 60)
                close_tabs(driver, base)
                boards_done.add(b["id"]); save_prog(boards_done, pinners_done)
                print(f" {why}")
            pinners_done.add(u); save_prog(boards_done, pinners_done)
            processed += 1
            if processed % SAVE_EVERY == 0:
                run_step("save to database", BUILD_ARGS)
        run_step("final save to database", BUILD_ARGS)
    finally:
        save_prog(boards_done, pinners_done)
        print(f"\n  Done — processed {processed} pinner(s) this run. "
              f"Re-run to continue the rest.\n")

if __name__ == "__main__":
    main()
