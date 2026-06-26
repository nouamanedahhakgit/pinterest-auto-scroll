"""
STEP 2 — Pinterest Auto-Scroll for SortPin  (mouse-free edition)
=================================================================
Uses Selenium + Chrome DevTools Protocol (CDP) to control Brave
entirely via JavaScript and HTTP — zero mouse, zero screen focus.
You can keep working on your PC normally while this runs.

HOW IT WORKS:
  1. Launches Brave with --remote-debugging-port=9222
  2. Selenium connects to Brave over CDP (like a remote control)
  3. driver.get(url)         → navigates without keyboard or mouse
  4. driver.execute_script() → clicks SortPin button via JavaScript
  5. Counts down, then auto-advances to next keyword

CLI FLAGS:
  python 2_pinterest_auto_scroll.py --2m
  python 2_pinterest_auto_scroll.py --5m     ← default
  python 2_pinterest_auto_scroll.py --15m
  python 2_pinterest_auto_scroll.py           ← asks interactively

KEYBOARD CONTROLS (global — work even while using other apps):
  N / SPACE  →  Mark Done + next keyword
  S          →  Skip, keep Not Yet
  ESC / Q    →  Quit
  + / -      →  ±1 min duration
"""

import subprocess, time, sys, os, json, re, socket
import keyboard
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from datetime import datetime

# ═══════════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════════
BRAVE_PATH     = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
KEYWORDS_FILE  = "keywords.txt"
PROGRESS_FILE  = "progress.json"
CDP_PORT       = 9222          # Chrome DevTools Protocol port

SCROLL_MINUTES = 5             # default minutes per keyword
PAGE_LOAD_WAIT = 6             # seconds to wait after driver.get()
BTN_FIND_TRIES = 12            # retries to find SortPin button via JS
BTN_RETRY_WAIT = 2             # seconds between retries
STALL_SECS     = 40            # if page height stops growing this long → no more pins → next keyword
STALL_SAMPLE   = 6             # seconds between page-height samples
# ═══════════════════════════════════════════════════════════════════

BASE = os.path.dirname(os.path.abspath(__file__))

# ── CLI flag parser ───────────────────────────────────────────────────────────
def _parse_cli_minutes():
    for arg in sys.argv[1:]:
        m = re.match(r'^--(\d+(?:\.\d+)?)m(?:in)?$', arg)
        if m:
            return float(m.group(1))
    return None

# ── Progress helpers ──────────────────────────────────────────────────────────
def load_progress():
    path = os.path.join(BASE, PROGRESS_FILE)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_progress(p):
    with open(os.path.join(BASE, PROGRESS_FILE), "w", encoding="utf-8") as f:
        json.dump(p, f, indent=2, ensure_ascii=False)

def mark_done(progress, kw):
    progress[kw] = {"status": "done", "done_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
    save_progress(progress)

def is_done(progress, kw):
    return progress.get(kw, {}).get("status") == "done"

# ── Keywords ──────────────────────────────────────────────────────────────────
def load_keywords():
    path = os.path.join(BASE, KEYWORDS_FILE)
    if not os.path.exists(path):
        print(f"\n  ERROR: {KEYWORDS_FILE} not found.\n"); sys.exit(1)
    kws = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                kws.append(line)
    return kws

def make_url(kw):
    return "https://www.pinterest.com/search/pins/?q=" + kw.replace(" ", "+") + "&rs=typed"

# ── Brave / CDP helpers ───────────────────────────────────────────────────────
def is_cdp_available():
    """Check if Brave is already listening on the CDP port."""
    try:
        s = socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1)
        s.close()
        return True
    except OSError:
        return False

def launch_brave():
    """
    Launch Brave with --remote-debugging-port.
    If Brave is already running WITHOUT the debug port, close it first.
    """
    if is_cdp_available():
        print(f"  Brave already running with CDP on port {CDP_PORT} — connecting...")
        return

    print(f"  Closing any existing Brave processes...")
    subprocess.run(["taskkill", "/F", "/IM", "brave.exe"],
                   capture_output=True)
    time.sleep(2)

    print(f"  Launching Brave with CDP on port {CDP_PORT}...")
    subprocess.Popen([
        BRAVE_PATH,
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
    ])

    # Wait up to 15 s for CDP to become available
    for i in range(15):
        if is_cdp_available():
            print(f"  Brave CDP ready.")
            return
        time.sleep(1)
    print(f"  ⚠  CDP port not confirmed — will try to connect anyway.")

def connect_selenium():
    """
    Connect Selenium to the already-running Brave via CDP.
    selenium-manager (built into selenium 4.6+) auto-downloads
    the matching ChromeDriver — no manual setup needed.
    """
    options = Options()
    options.binary_location = BRAVE_PATH
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{CDP_PORT}")
    driver = webdriver.Chrome(options=options)
    driver.implicitly_wait(3)
    return driver

def navigate_new_tab(driver, url):
    """
    Open URL via subprocess so Brave creates the tab naturally.
    Tabs opened this way are NOT flagged as WebDriver-controlled,
    so navigator.webdriver stays false → Plasmo/SortPin injects.
    Then we switch Selenium's focus to the new tab via CDP.

    Returns the new tab's window handle (or None) so the caller can close
    exactly that tab later instead of guessing.
    """
    existing = set(driver.window_handles)
    subprocess.Popen([BRAVE_PATH, url])
    # Wait up to 15 s for the new tab to appear in CDP handles
    for _ in range(15):
        time.sleep(1)
        new_handles = set(driver.window_handles) - existing
        if new_handles:
            h = next(iter(new_handles))
            driver.switch_to.window(h)
            return h
    # Fallback: use whatever handle is available
    if driver.window_handles:
        h = driver.window_handles[-1]
        driver.switch_to.window(h)
        return h
    return None

def close_keyword_tabs(driver, base_tab, verbose=False):
    """
    Close EVERY tab except `base_tab` so tabs never pile up. We close by
    handle (not by URL) so it works even for subprocess-opened tabs, and we
    null out onbeforeunload first so Pinterest's "Leave site?" prompt can't
    block driver.close(). Always leaves the base tab alive (CDP stays connected).
    """
    closed = 0
    for h in list(driver.window_handles):
        if h == base_tab:
            continue
        if len(driver.window_handles) <= 1:
            break                      # never close the very last tab
        try:
            driver.switch_to.window(h)
            try:
                driver.execute_script("window.onbeforeunload=null;")
            except Exception:
                pass
            driver.close()
            closed += 1
        except Exception:
            continue
    # Re-focus the base tab (or any survivor)
    try:
        if base_tab in driver.window_handles:
            driver.switch_to.window(base_tab)
        elif driver.window_handles:
            driver.switch_to.window(driver.window_handles[-1])
    except Exception:
        pass
    if verbose and closed:
        print(f"     🗑  Closed {closed} old tab(s) — only the new search stays")
    return closed

# ── SortPin button — native (trusted) clicks on the CURRENT tab only ──────────
# IMPORTANT: SortPin only reacts to TRUSTED clicks (event.isTrusted===true).
# A scripted click via driver.execute_script('el.click()') is untrusted and is
# silently ignored. Selenium's WebElement.click() goes through the browser's
# real input pipeline (CDP) → isTrusted=true → SortPin activates.
#
# Also: SortPin's run state can carry over to a new keyword's tab (it shows
# 'Stop Scroll' even on a freshly opened page). If we just see 'Stop Scroll'
# and assume "already running", the scroll stays bound to the OLD keyword and
# the new keyword never actually scrolls. So for every keyword we STOP any
# existing scroll first, then START fresh — binding the scroll to THIS tab.

def _sortpin_state(driver):
    """Read SortPin state in the CURRENT tab: 'running', 'idle', or 'none'."""
    try:
        labels = [(b.text or "").lower()
                  for b in driver.find_elements(By.TAG_NAME, "button")]
    except Exception:
        return "none"
    if any("stop scroll"  in t for t in labels):  return "running"
    if any("start scroll" in t for t in labels):  return "idle"
    return "none"

def _click_button_by_text(driver, needle):
    """Native trusted-click the first CURRENT-tab button whose text contains
    `needle` (lowercase). Returns True if a click was issued."""
    for b in driver.find_elements(By.TAG_NAME, "button"):
        try:
            txt = (b.text or "").lower()
        except Exception:
            continue
        if needle in txt:
            try:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", b)
                time.sleep(0.2)
                b.click()                      # ← TRUSTED native click
                return True
            except Exception:
                return False
    return False

def click_sortpin_button(driver):
    """
    Start a FRESH SortPin scroll on the CURRENT (new-keyword) tab.

    Only the focused tab is touched — navigate_new_tab() already switched focus
    to the new keyword's tab. If SortPin is still 'running' (leftover from the
    previous keyword), we STOP it first so the scroll re-binds to this keyword,
    then START and verify it flipped to 'Stop Scroll'.
    """
    for attempt in range(1, BTN_FIND_TRIES + 1):
        st = _sortpin_state(driver)

        # Leftover scroll from previous keyword → stop it so we can start fresh.
        if st == "running":
            _click_button_by_text(driver, "stop scroll")
            time.sleep(1.0)
            st = _sortpin_state(driver)

        # Now idle → start fresh on THIS keyword's tab.
        if st == "idle":
            if _click_button_by_text(driver, "start scroll"):
                time.sleep(1.5)               # let SortPin flip state
                if _sortpin_state(driver) == "running":
                    print(f"\n  ✅ SortPin scrolling started (fresh, this keyword)")
                    return True

        print(f"\r  🔍 [{attempt}/{BTN_FIND_TRIES}] SortPin: {st}"
              f"  (retry in {BTN_RETRY_WAIT}s)          ", end="", flush=True)
        time.sleep(BTN_RETRY_WAIT)

    print(f"\n  ⚠  Could not start SortPin on this keyword "
          f"after {BTN_FIND_TRIES} tries — continuing anyway")
    return False

# ── Shared state & keyboard listener ─────────────────────────────────────────
state = {
    "running":   True,
    "action":    None,
    "duration":  SCROLL_MINUTES * 60,
}

def on_key(event):
    s = state
    n = event.name
    if   n in ("n", "space"):  s["action"] = "next_done"
    elif n == "s":              s["action"] = "next_skip"
    elif n in ("esc", "q"):    s["action"] = "quit"; s["running"] = False
    elif n == "=":
        s["duration"] = s["duration"] + 60
        mins = s["duration"] / 60
        print(f"\n  +1 min → now {mins:.0f} min per keyword", flush=True)
    elif n == "-":
        s["duration"] = max(30, s["duration"] - 60)
        mins = s["duration"] / 60
        print(f"\n  -1 min → now {mins:.1f} min per keyword", flush=True)

keyboard.on_press(on_key)

# JS: total scrollable page height — grows as Pinterest loads more pins,
# plateaus when pagination is exhausted (no more pins to load).
_PAGE_HEIGHT_JS = ("return document.documentElement.scrollHeight"
                   " || document.body.scrollHeight || 0;")

def _page_height(driver):
    try:
        h = driver.execute_script(_PAGE_HEIGHT_JS)
        return int(h) if h else 0
    except Exception:
        return 0

# ── Countdown ─────────────────────────────────────────────────────────────────
def countdown(driver, kw, pos, total, duration_secs):
    state["action"] = None
    end_at = time.time() + duration_secs

    # pagination-exhaustion tracking
    last_h      = 0
    last_grow   = time.time()
    next_sample = time.time() + STALL_SAMPLE   # first sample after a short delay

    while time.time() < end_at:
        if not state["running"]:    return "quit"
        if state["action"] == "next_done": return "next"
        if state["action"] == "next_skip": return "skip"

        # ── Detect "no more pins" (page height stopped growing) ───────────────
        now = time.time()
        if now >= next_sample:
            next_sample = now + STALL_SAMPLE
            h = _page_height(driver)
            if h > last_h + 50:            # grew meaningfully → still loading pins
                last_h = h
                last_grow = now
            elif last_h > 0 and (now - last_grow) >= STALL_SECS:
                print()                    # finish the progress line
                return "exhausted"

        remaining = int(end_at - time.time())
        elapsed   = duration_secs - remaining
        pct       = elapsed / duration_secs
        bar       = "█" * int(pct * 30) + "░" * (30 - int(pct * 30))
        em, es    = divmod(elapsed, 60)
        rm, rs    = divmod(remaining, 60)
        idle      = int(time.time() - last_grow)
        print(
            f"\r  [{bar}]  {em:02d}:{es:02d} elapsed  |  "
            f"{rm:02d}:{rs:02d} left  |  "
            f"[{pos}/{total}] {kw[:26]:<26}  "
            f"newpins:{max(0, STALL_SECS - idle):>2}s  "
            f"[N=next S=skip ESC=quit]",
            end="", flush=True
        )
        time.sleep(1)
    return "done"

# ── Startup prompt ────────────────────────────────────────────────────────────
def ask_duration():
    print(f"\n{'═'*55}")
    print(f"  Pinterest Auto-Scroll  ✦  SortPin  (mouse-free)")
    print(f"{'─'*55}")
    print(f"  How many minutes per keyword?")
    print(f"  Common choices: 2 / 5 / 10 / 15")
    print(f"  (default = {SCROLL_MINUTES} min — press ENTER to use it)")
    print(f"{'─'*55}")
    while True:
        raw = input(f"  >> Minutes [{SCROLL_MINUTES}]: ").strip()
        if raw == "":
            return SCROLL_MINUTES * 60
        try:
            mins = float(raw)
            if mins > 0:
                return int(mins * 60)
            print("  Enter a positive number.")
        except ValueError:
            print("  Enter a number e.g. 2 or 5 or 0.5")

# ── Banner ────────────────────────────────────────────────────────────────────
def print_banner(all_kws, remaining, done_count, chosen_mins):
    total    = len(all_kws)
    pct      = int((done_count / total) * 100) if total else 0
    bar      = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
    mins_str = f"{chosen_mins:.0f}" if chosen_mins == int(chosen_mins) else f"{chosen_mins:.1f}"
    print(f"\n{'═'*65}")
    print(f"  Pinterest Auto-Scroll  ✦  SortPin  (mouse-free via JS)")
    print(f"  [{bar}] {pct}%  —  {done_count} done / {total} total")
    print(f"{'─'*65}")
    if remaining:
        print(f"  ⏳ {len(remaining)} remaining  |  ⏱ {mins_str} min per keyword")
        print(f"  Starting: \"{remaining[0]}\"")
    else:
        print(f"  ✅ All done!")
    print(f"{'─'*65}")
    print(f"  N/SPACE = done+next  S = skip  ESC = quit  +/- = ±1min")
    print(f"  You can use your PC normally — no mouse needed")
    print(f"{'═'*65}\n")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    s        = state
    all_kws  = load_keywords()
    progress = load_progress()

    # Duration: CLI flag or interactive prompt
    cli_mins = _parse_cli_minutes()
    if cli_mins is not None:
        s["duration"] = int(cli_mins * 60)
        chosen_mins   = cli_mins
        mins_str = f"{chosen_mins:.0f}" if chosen_mins == int(chosen_mins) else f"{chosen_mins:.1f}"
        print(f"\n  Duration: {mins_str} min per keyword (from command line)")
    else:
        s["duration"] = ask_duration()
        chosen_mins   = s["duration"] / 60

    # Seed new keywords
    changed = False
    for kw in all_kws:
        if kw not in progress:
            progress[kw] = {"status": "not_yet"}
            changed = True
    if changed:
        save_progress(progress)

    done_count = sum(1 for kw in all_kws if is_done(progress, kw))
    remaining  = [kw for kw in all_kws if not is_done(progress, kw)]

    print_banner(all_kws, remaining, done_count, chosen_mins)

    if not remaining:
        print("  Nothing left — delete progress.json to start over.\n")
        return

    # ── Launch Brave & connect Selenium ──────────────────────────────────────
    launch_brave()
    time.sleep(2)
    print(f"  Connecting Selenium to Brave...")
    try:
        driver = connect_selenium()
        print(f"  ✅ Connected — browser under JS control\n")
    except Exception as e:
        print(f"\n  ERROR connecting to Brave: {e}")
        print(f"  Make sure Brave is NOT already open, then re-run.\n")
        sys.exit(1)

    # Anchor tab: we keep exactly this one alive and close everything else
    # after each keyword, so tabs never pile up.
    try:
        base_tab = driver.current_window_handle
    except Exception:
        base_tab = driver.window_handles[0] if driver.window_handles else None

    total_start = time.time()
    pos         = 0

    try:
        while s["running"] and pos < len(remaining):
            kw  = remaining[pos]
            url = make_url(kw)

            # ── Open in a new Brave tab (subprocess, not driver.get) ─────
            # This avoids navigator.webdriver=true so Plasmo/SortPin injects.
            print(f"  ▶  [{pos+1}/{len(remaining)}]  {kw}")
            print(f"     Opening new tab via Brave (SortPin-friendly)...")
            # Close any leftover tabs from before opening this one,
            # so we never accumulate tabs.
            close_keyword_tabs(driver, base_tab, verbose=True)
            kw_tab = navigate_new_tab(driver, url)
            print(f"     Waiting {PAGE_LOAD_WAIT}s for page + SortPin...")
            time.sleep(PAGE_LOAD_WAIT)

            # ── Click SortPin button via JavaScript ───────────────────────
            click_sortpin_button(driver)

            # ── Countdown ─────────────────────────────────────────────────
            print(f"\n  ⏱  Scrolling {s['duration']//60}m {s['duration']%60:02d}s"
                  f"  —  SortPin saving pins (you can use your PC now)")
            result = countdown(driver, kw, pos + 1, len(remaining), s["duration"])

            # ── Close this keyword's tab before moving on (no tab pile-up) ─
            try:
                close_keyword_tabs(driver, base_tab, verbose=True)
            except Exception:
                pass

            # ── Result ────────────────────────────────────────────────────
            if result in ("done", "next", "exhausted"):
                # Close current Selenium session safely before database builder closes Brave
                try:
                    driver.quit()
                except Exception:
                    pass
                
                # Run database build for this keyword immediately
                print(f"\n     Running database build and sync for keyword '{kw}'...")
                subprocess.run([sys.executable, "4_build_database.py", "--no-csv", "--keyword", kw], cwd=BASE)
                
                mark_done(progress, kw)
                done_count += 1
                label = {"done":      "⏰ Time up",
                         "next":      "⏭  Early done",
                         "exhausted": "📄 Page end — no more pins"}[result]
                print(f"\n  ✅ {label}: \"{kw}\"  [{done_count}/{len(all_kws)} done]")
                pos += 1
                
                # If there are more keywords, relaunch Brave & connect Selenium
                if pos < len(remaining) and s["running"]:
                    print("\n     Relaunching Brave and reconnecting Selenium for next keyword...")
                    launch_brave()
                    time.sleep(2)
                    try:
                        driver = connect_selenium()
                        base_tab = driver.current_window_handle
                    except Exception as e:
                        print(f"\n  ERROR reconnecting to Brave: {e}")
                        break
            elif result == "skip":
                print(f"\n  ⏭  Skipped (Not Yet): \"{kw}\"")
                pos += 1
            elif result == "quit":
                print(f"\n  ⚠  Quit — \"{kw}\" stays Not Yet")
                break

    finally:
        # ── Session summary ───────────────────────────────────────────────
        elapsed = time.time() - total_start
        m, sec  = divmod(int(elapsed), 60)
        done_now   = sum(1 for kw in all_kws if is_done(progress, kw))
        still_left = len(all_kws) - done_now
        print(f"\n\n{'═'*65}")
        print(f"  Session finished  |  {m}m {sec}s")
        print(f"  ✅ Done    : {done_now} / {len(all_kws)}")
        print(f"  ⏳ Not Yet : {still_left}")
        if still_left:
            print(f"  → Re-run anytime to continue the remaining {still_left}.")
        print(f"  → Run 3_sync_to_sheet.py to update your Google Sheet.")
        print(f"{'═'*65}\n")

if __name__ == "__main__":
    main()
