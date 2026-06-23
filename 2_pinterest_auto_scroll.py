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
    """
    existing = set(driver.window_handles)
    subprocess.Popen([BRAVE_PATH, url])
    # Wait up to 15 s for the new tab to appear in CDP handles
    for _ in range(15):
        time.sleep(1)
        new_handles = set(driver.window_handles) - existing
        if new_handles:
            driver.switch_to.window(next(iter(new_handles)))
            return True
    # Fallback: use whatever handle is available
    if driver.window_handles:
        driver.switch_to.window(driver.window_handles[-1])
    return False

def close_current_tab(driver):
    """Close current keyword tab; switch Selenium to a remaining tab."""
    if len(driver.window_handles) > 1:
        driver.close()
        driver.switch_to.window(driver.window_handles[-1])

# ── SortPin button — pure JavaScript, zero mouse ──────────────────────────────
SORTPIN_JS = """
    // SortPin injects its UI as <div id="pinterest-one-root"> in the main document.
    var root = document.querySelector('#pinterest-one-root');
    if (!root) return 'no_sortpin_root';
    var btns = Array.from(root.querySelectorAll('button'));
    var btn  = btns.find(function(b) {
        return b.innerText && b.innerText.includes('Start Scroll');
    });
    if (btn) {
        btn.click();
        return 'clicked';
    }
    return 'not_found:' + btns.map(function(b){ return b.innerText.trim().slice(0,30); }).join('|');
"""

def click_sortpin_button(driver):
    """Click SortPin 'Start Scroll' button via JS inside Plasmo Shadow DOM."""
    for attempt in range(1, BTN_FIND_TRIES + 1):
        try:
            result = driver.execute_script(SORTPIN_JS)
            if result == "clicked":
                print(f"\n  ✅ SortPin button clicked (Shadow DOM)")
                return True
            # Show debug info so we know what's happening
            print(f"\r  🔍 [{attempt}/{BTN_FIND_TRIES}] SortPin: {result}"
                  f"  (retry in {BTN_RETRY_WAIT}s)          ", end="", flush=True)
        except Exception as e:
            print(f"\r  🔍 [{attempt}/{BTN_FIND_TRIES}] JS error: {e}", end="", flush=True)
        time.sleep(BTN_RETRY_WAIT)
    print(f"\n  ⚠  SortPin button not found after {BTN_FIND_TRIES} tries — continuing anyway")
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

# ── Countdown ─────────────────────────────────────────────────────────────────
def countdown(kw, pos, total, duration_secs):
    state["action"] = None
    end_at = time.time() + duration_secs
    while time.time() < end_at:
        if not state["running"]:    return "quit"
        if state["action"] == "next_done": return "next"
        if state["action"] == "next_skip": return "skip"
        remaining = int(end_at - time.time())
        elapsed   = duration_secs - remaining
        pct       = elapsed / duration_secs
        bar       = "█" * int(pct * 30) + "░" * (30 - int(pct * 30))
        em, es    = divmod(elapsed, 60)
        rm, rs    = divmod(remaining, 60)
        print(
            f"\r  [{bar}]  {em:02d}:{es:02d} elapsed  |  "
            f"{rm:02d}:{rs:02d} left  |  "
            f"[{pos}/{total}] {kw[:30]:<30}  "
            f"[N=done+next  S=skip  ESC=quit]",
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
            navigate_new_tab(driver, url)
            print(f"     Waiting {PAGE_LOAD_WAIT}s for page + SortPin...")
            time.sleep(PAGE_LOAD_WAIT)

            # ── Click SortPin button via JavaScript ───────────────────────
            click_sortpin_button(driver)

            # ── Countdown ─────────────────────────────────────────────────
            print(f"\n  ⏱  Scrolling {s['duration']//60}m {s['duration']%60:02d}s"
                  f"  —  SortPin saving pins (you can use your PC now)")
            result = countdown(kw, pos + 1, len(remaining), s["duration"])

            # ── Close this keyword's tab before moving on ─────────────────
            try:
                close_current_tab(driver)
            except Exception:
                pass

            # ── Result ────────────────────────────────────────────────────
            if result in ("done", "next"):
                mark_done(progress, kw)
                done_count += 1
                label = "⏰ Time up" if result == "done" else "⏭  Early done"
                print(f"\n  ✅ {label}: \"{kw}\"  [{done_count}/{len(all_kws)} done]")
                pos += 1
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
