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
  python 7_scrape_profiles.py                 # process 20 pinners, 5 min cap/board
  python 7_scrape_profiles.py --limit 50      # process 50 pinners this run
  python 7_scrape_profiles.py --minutes 10    # cap each board at 10 minutes
  python 7_scrape_profiles.py --disk          # build DB from disk when saving
"""

import os, sys, time, socket, subprocess, json, sqlite3, re

BASE       = os.path.dirname(os.path.abspath(__file__))
DB_PATH    = os.path.join(BASE, "sortpin.db")
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

def _minutes_opt():
    args = sys.argv[1:]
    for i, x in enumerate(args):
        if x in ("--minutes", "--max-min") and i + 1 < len(args):
            try: return int(args[i + 1])
            except ValueError: pass
    return 5  # default 5 minutes

LIMIT    = _opt_int("--limit", 20)
MAX_MIN  = _minutes_opt()
BUILD_ARGS = ["4_build_database.py", "--no-clear", "--no-csv"] + (["--disk"] if "--disk" in sys.argv[1:] else [])



# ── read pinners + their boards from the DB ───────────────────────────────────
def _extract_domain(url):
    if not url:
        return ""
    u = url.strip().lower()
    if not u.startswith("http"):
        u = "https://" + u
    try:
        from urllib.parse import urlparse
        h = urlparse(u).hostname or ""
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""

def load_targets():
    if not os.path.exists(DB_PATH):
        print("  sortpin.db not found — run step 4 first."); sys.exit(1)
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row

    # Check if 'status' column exists in pinners table, add it if not
    cursor = con.cursor()
    try:
        cursor.execute("SELECT status FROM pinners LIMIT 1")
    except sqlite3.OperationalError:
        try:
            con.execute("ALTER TABLE pinners ADD COLUMN status TEXT DEFAULT 'not yet'")
            con.commit()
        except Exception:
            pass

    # Check if 'status' column exists in boards table, add it if not
    try:
        cursor.execute("SELECT status FROM boards LIMIT 1")
    except sqlite3.OperationalError:
        try:
            con.execute("ALTER TABLE boards ADD COLUMN status TEXT DEFAULT 'not yet'")
            con.commit()
        except Exception:
            pass

    # Build domain→site_type map from scraped_websites (if table exists)
    site_types = {}
    try:
        for r in con.execute("SELECT domain, site_type FROM scraped_websites WHERE domain IS NOT NULL"):
            if r[0] and r[1]:
                site_types[r[0].lower().strip()] = r[1]
    except sqlite3.OperationalError:
        pass  # table doesn't exist yet — no filtering applied

    all_pinners = [dict(r) for r in con.execute(
        "SELECT username, full_name, follower_count, website_url, status FROM pinners "
        "ORDER BY follower_count DESC")]

    # Filter: only process pinners whose scraped site_type contains "blog"
    if site_types:
        filtered, skipped = [], 0
        for p in all_pinners:
            domain = _extract_domain(p.get("website_url", ""))
            st = site_types.get(domain, "")
            if "blog" in st.lower():
                filtered.append(p)
            else:
                skipped += 1
        if skipped:
            print(f"  [site_type filter] skipping {skipped} pinners (not blog) — "
                  f"{len(filtered)} blog pinners queued")
        pinners = filtered
    else:
        pinners = all_pinners

    boards_by = {}
    for r in con.execute("SELECT id, url, owner_username, name, status FROM boards "
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
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        except AttributeError:
            pass

    pinners, boards_by = load_targets()
    
    total_pinners = len(pinners)
    done_count = sum(1 for p in pinners if p.get("status") == "done")
    running_count = sum(1 for p in pinners if p.get("status") == "running")
    not_yet_count = total_pinners - done_count - running_count
    pct = (done_count / total_pinners * 100) if total_pinners else 0.0

    print(f"\n{'='*62}\n  STEP 7 — deep-scrape profiles → boards → pins\n"
          f"  {LIMIT} pinners this run · {MAX_MIN} min cap/board\n"
          f"  Progress: {done_count}/{total_pinners} completed successfully ({pct:.1f}%) · "
          f"{running_count} running/interrupted · {not_yet_count} not yet processed\n{'='*62}")

    todo = [p for p in pinners if p.get("status") != "done"][:LIMIT]
    if not todo:
        print("  Nothing left — all known pinners processed.\n")
        return
    if not ensure_brave():
        print("  Could not start Brave."); sys.exit(1)
    driver = connect(); base = driver.current_window_handle
    processed = 0
    try:
        for p in todo:
            u = p["username"]
            print(f"\n▶ @{u}  ({p.get('full_name') or ''}) — loading profile...")
            
            # Set pinner status to 'running'
            con = sqlite3.connect(DB_PATH)
            con.execute("UPDATE pinners SET status='running' WHERE username=?", (u,))
            con.commit()
            con.close()
            
            # 1a) SAVED tab profile (captures boards + stats)
            print("    → loading saved profile (_saved/)...")
            open_tab(driver, f"https://www.pinterest.com/{u}/_saved/")
            time.sleep(5); click_start(driver)
            print("      scrolling saved boards (capturing all boards)...")
            why_saved = scroll_till_finish(driver, 120)
            close_tabs(driver, base)
            
            # Run step 4 to import newly captured boards and pinner info
            print("    → updating profile boards...")
            run_step("import profile boards", BUILD_ARGS)

            # 1b) CREATED tab profile (captures created pins like magic)
            print("    → loading created profile (_created/)...")
            open_tab(driver, f"https://www.pinterest.com/{u}/_created/")
            time.sleep(5); click_start(driver)
            print("      scrolling created pins (like magic)...")
            why_profile = scroll_till_finish(driver, MAX_MIN * 60)
            close_tabs(driver, base)
            print(f"      → finished created pins ({why_profile})")

            # Run step 4 to import newly captured profile pins
            print("    → updating profile pins...")
            run_step("import profile pins", BUILD_ARGS)
            
            # Fetch all boards currently in the DB for this pinner (pre-existing + newly discovered)
            con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
            brds = [dict(r) for r in con.execute(
                "SELECT id, url, owner_username, name, status FROM boards "
                "WHERE owner_username = ? AND url IS NOT NULL AND url<>''", (u,))]
            con.close()
            
            print(f"    found {len(brds)} boards to process")

            # 2) each board till the end
            for b in brds:
                if b.get("status") == "done":
                    continue
                
                print(f"    • board '{(b.get('name') or '')[:34]}' …", end="", flush=True)
                
                # Set board status to 'running'
                con = sqlite3.connect(DB_PATH)
                con.execute("UPDATE boards SET status='running' WHERE id=?", (b["id"],))
                con.commit()
                con.close()
                
                board_url = b["url"]
                if board_url.startswith("/"):
                    board_url = "https://www.pinterest.com" + board_url
                elif not board_url.startswith("http"):
                    board_url = "https://www.pinterest.com/" + board_url
                
                open_tab(driver, board_url); time.sleep(5)
                click_start(driver)
                why = scroll_till_finish(driver, MAX_MIN * 60)
                close_tabs(driver, base)
                
                # Set board status to 'done'
                con = sqlite3.connect(DB_PATH)
                con.execute("UPDATE boards SET status='done' WHERE id=?", (b["id"],))
                con.commit()
                con.close()
                
                print(f" {why}")
            
            # Check if all boards for this pinner are done
            con = sqlite3.connect(DB_PATH)
            cursor = con.cursor()
            cursor.execute("SELECT COUNT(*) FROM boards WHERE owner_username=? AND status<>'done'", (u,))
            not_done_count = cursor.fetchone()[0]
            if not_done_count == 0:
                con.execute("UPDATE pinners SET status='done' WHERE username=?", (u,))
                print(f"  Processed pinner @{u} completely.")
            else:
                con.execute("UPDATE pinners SET status='not yet' WHERE username=?", (u,))
                print(f"  Pinner @{u} has remaining boards.")
            con.commit()
            con.close()
            
            processed += 1
            if processed % SAVE_EVERY == 0:
                run_step("save to database", BUILD_ARGS)
        final_args = ["4_build_database.py", "--no-csv"] + (["--disk"] if "--disk" in sys.argv[1:] else [])
        run_step("final save & clear extension", final_args)
    finally:
        print(f"\n  Done — processed {processed} pinner(s) this run. "
              f"Re-run to continue the rest.\n")

if __name__ == "__main__":
    main()
