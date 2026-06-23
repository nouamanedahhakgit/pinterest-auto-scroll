"""
STEP 2 — Pinterest Auto-Scroll for SortPin
============================================
For each keyword:
  1. Opens Pinterest search in Brave
  2. Finds the SortPin "Start Scroll" button on screen and clicks it
  3. Waits 15 minutes (SortPin scrolls automatically every 5 s)
  4. Moves to next keyword automatically — no key needed
  5. Marks each keyword Done in progress.json

MANUAL CONTROLS (work even when Brave is focused):
  N  /  SPACE   →  Skip to Next keyword right now (marks Done)
  S             →  Skip without marking Done  (stays Not Yet)
  ESC  /  Q     →  Quit session
  +  /  -       →  Increase / decrease scroll duration by 1 min
  Mouse to TOP-LEFT corner → Emergency stop
"""

import subprocess, time, sys, os, json, re
import numpy as np
import pyautogui, pyperclip, keyboard
from datetime import datetime

# ── CLI argument parser ───────────────────────────────────────────────────────
# Usage examples:
#   python 2_pinterest_auto_scroll.py --2m
#   python 2_pinterest_auto_scroll.py --5m
#   python 2_pinterest_auto_scroll.py --15m
#   python 2_pinterest_auto_scroll.py --0.5m   (30 seconds, for testing)
#   python 2_pinterest_auto_scroll.py           (shows interactive prompt)
def _parse_cli_minutes():
    for arg in sys.argv[1:]:
        m = re.match(r'^--(\d+(?:\.\d+)?)m(?:in)?$', arg)
        if m:
            return float(m.group(1))
    return None   # no flag found → show prompt

# ═══════════════════════════════════════════════════════════════════
#  CONFIG  — edit these
# ═══════════════════════════════════════════════════════════════════
BRAVE_PATH      = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
KEYWORDS_FILE   = "keywords.txt"
PROGRESS_FILE   = "progress.json"

SCROLL_MINUTES  = 5       # default minutes per keyword (can change at startup prompt)
PAGE_LOAD_WAIT  = 6       # seconds to wait after page opens before clicking button
BTN_FIND_TRIES  = 10      # how many times to retry finding SortPin button
BTN_RETRY_WAIT  = 2       # seconds between each button-find retry

# SortPin "Start Scroll" button colour: bg-blue-500 = #3B82F6 = RGB(59, 130, 246)
BTN_R, BTN_G, BTN_B = 59, 130, 246
BTN_TOLERANCE       = 30   # colour match tolerance
BTN_MIN_PIXELS      = 40   # minimum matching pixels to confirm button found
# ═══════════════════════════════════════════════════════════════════

pyautogui.FAILSAFE = True
BASE = os.path.dirname(os.path.abspath(__file__))

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

# ── Keyword loader ────────────────────────────────────────────────────────────
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

# ── Browser helpers ───────────────────────────────────────────────────────────
def navigate(url):
    pyperclip.copy(url)
    pyautogui.hotkey("ctrl", "l")
    time.sleep(0.4)
    pyautogui.hotkey("ctrl", "a")
    time.sleep(0.1)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.1)
    pyautogui.press("enter")

def focus_center():
    w, h = pyautogui.size()
    pyautogui.click(w // 2, int(h * 0.55))
    time.sleep(0.3)

# ── SortPin button finder (numpy pixel scan) ──────────────────────────────────
def find_sortpin_button():
    """
    Scans the screen for SortPin's blue-500 'Start Scroll' button.
    Returns (cx, cy) of the button center, or None if not found.
    """
    img  = pyautogui.screenshot()
    arr  = np.array(img)                    # shape: (H, W, 3)  RGB uint8

    r, g, b = arr[:, :, 0].astype(int), arr[:, :, 1].astype(int), arr[:, :, 2].astype(int)

    # Euclidean colour distance to blue-500
    dist = ((r - BTN_R)**2 + (g - BTN_G)**2 + (b - BTN_B)**2) ** 0.5
    mask = dist < BTN_TOLERANCE

    ys, xs = np.where(mask)
    if len(ys) < BTN_MIN_PIXELS:
        return None

    # Centre of mass of matching pixels
    cx = int(xs.mean())
    cy = int(ys.mean())
    return cx, cy

def click_sortpin_button():
    """
    Retries BTN_FIND_TRIES times to find and click the SortPin button.
    Returns True if clicked, False if never found.
    """
    for attempt in range(1, BTN_FIND_TRIES + 1):
        pos = find_sortpin_button()
        if pos:
            cx, cy = pos
            print(f"\n  🎯 SortPin button found at ({cx}, {cy}) — clicking")
            pyautogui.click(cx, cy)
            time.sleep(0.5)
            return True
        print(f"\r  🔍 Looking for SortPin button... attempt {attempt}/{BTN_FIND_TRIES}", end="", flush=True)
        time.sleep(BTN_RETRY_WAIT)

    print(f"\n  ⚠  SortPin button not found after {BTN_FIND_TRIES} tries.")
    print(f"     Make sure SortPin extension is active on this page.")
    return False

# ── Shared state ──────────────────────────────────────────────────────────────
state = {
    "running":  True,
    "action":   None,           # "next_done" | "next_skip" | "quit"
    "duration": SCROLL_MINUTES * 60,
}

def on_key(event):
    s = state
    n = event.name
    if   n in ("n", "space"):   s["action"] = "next_done"
    elif n == "s":               s["action"] = "next_skip"
    elif n in ("esc", "q"):      s["action"] = "quit";   s["running"] = False
    elif n == "=":
        s["duration"] = s["duration"] + 60
        print(f"\n  +1 min → {s['duration']//60} min per keyword", flush=True)
    elif n == "-":
        s["duration"] = max(60, s["duration"] - 60)
        print(f"\n  -1 min → {s['duration']//60} min per keyword", flush=True)

keyboard.on_press(on_key)

# ── Countdown display ─────────────────────────────────────────────────────────
def countdown(kw, pos, total, duration_secs):
    """
    Counts down duration_secs, updating the console every second.
    Returns the action that ended the countdown:
      "done"  — timer finished naturally
      "next"  — user pressed N/SPACE early
      "skip"  — user pressed S
      "quit"  — user pressed ESC
    """
    state["action"] = None
    end_at = time.time() + duration_secs

    while time.time() < end_at:
        if not state["running"]:
            return "quit"
        if state["action"] == "next_done":
            return "next"
        if state["action"] == "next_skip":
            return "skip"

        remaining   = int(end_at - time.time())
        elapsed     = duration_secs - remaining
        pct         = elapsed / duration_secs
        bar_done    = int(pct * 30)
        bar         = "█" * bar_done + "░" * (30 - bar_done)
        rm, rs      = divmod(remaining, 60)
        em, es      = divmod(elapsed, 60)

        print(
            f"\r  [{bar}]  {em:02d}:{es:02d} elapsed  |  "
            f"{rm:02d}:{rs:02d} left  |  "
            f"[{pos}/{total}] {kw[:32]:<32}  "
            f"[N=done+next  S=skip  ESC=quit]",
            end="", flush=True
        )
        time.sleep(1)

    return "done"

# ── Banner / summary ──────────────────────────────────────────────────────────
def print_banner(all_kws, remaining, done_count, chosen_mins):
    total = len(all_kws)
    pct   = int((done_count / total) * 100) if total else 0
    bar   = "█" * int(pct / 4) + "░" * (25 - int(pct / 4))
    # show as whole number or 1-decimal
    mins_str = f"{chosen_mins:.0f}" if chosen_mins == int(chosen_mins) else f"{chosen_mins:.1f}"
    print(f"\n{'═'*65}")
    print(f"  Pinterest Auto-Scroll  ✦  SortPin Click Mode")
    print(f"  [{bar}] {pct}%  —  {done_count} done / {total} total")
    print(f"{'─'*65}")
    if remaining:
        print(f"  ⏳ {len(remaining)} remaining  |  ⏱ {mins_str} min per keyword")
        print(f"  Starting from: \"{remaining[0]}\"")
    else:
        print(f"  ✅ All done!")
    print(f"{'─'*65}")
    print(f"  N/SPACE = done+next   S = skip   ESC = quit   +/- = ±1 min")
    print(f"  Mouse to TOP-LEFT corner = emergency stop")
    print(f"{'═'*65}\n")

# ── Startup time prompt ───────────────────────────────────────────────────────
def ask_duration():
    print(f"\n{'═'*55}")
    print(f"  Pinterest Auto-Scroll  ✦  SortPin")
    print(f"{'─'*55}")
    print(f"  How many minutes per keyword?")
    print(f"  Common choices:  2  /  5  /  10  /  15")
    print(f"  (default = {SCROLL_MINUTES} min — just press ENTER to use it)")
    print(f"{'─'*55}")
    while True:
        raw = input(f"  >> Minutes per keyword [{SCROLL_MINUTES}]: ").strip()
        if raw == "":
            return SCROLL_MINUTES * 60
        try:
            mins = float(raw)
            if mins <= 0:
                print("  Please enter a positive number.")
                continue
            return int(mins * 60)
        except ValueError:
            print("  Please enter a number (e.g. 2  or  5  or  0.5)")

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    s        = state
    all_kws  = load_keywords()
    progress = load_progress()

    # Duration: from CLI flag or interactive prompt
    cli_mins = _parse_cli_minutes()
    if cli_mins is not None:
        s["duration"] = int(cli_mins * 60)
        chosen_mins   = cli_mins
        mins_str = f"{chosen_mins:.0f}" if chosen_mins == int(chosen_mins) else f"{chosen_mins:.1f}"
        print(f"\n  Duration set from command line: {mins_str} min per keyword")
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
        print("  Nothing left — all keywords are Done.")
        print("  Delete progress.json to start over.\n")
        return

    # Open Brave with first keyword
    first_url = make_url(remaining[0])
    print(f"  Opening Brave → {first_url}")
    print(f"  Waiting {PAGE_LOAD_WAIT}s for page + SortPin to load...\n")
    subprocess.Popen([BRAVE_PATH, first_url])
    time.sleep(PAGE_LOAD_WAIT)
    focus_center()

    total_start  = time.time()
    pos          = 0
    first_opened = True

    while s["running"] and pos < len(remaining):
        kw  = remaining[pos]
        url = make_url(kw)

        # ── Navigate ──────────────────────────────────────────────────────
        if first_opened:
            first_opened = False
        else:
            print(f"\n\n  ▶  [{pos+1}/{len(remaining)}]  {kw}")
            print(f"     {url}")
            navigate(url)
            print(f"     Waiting {PAGE_LOAD_WAIT}s for page + SortPin...")
            time.sleep(PAGE_LOAD_WAIT)
            focus_center()

        # ── Click SortPin button ──────────────────────────────────────────
        print(f"\n  [{pos+1}/{len(remaining)}]  \"{kw}\"")
        clicked = click_sortpin_button()
        if not clicked:
            print(f"  ↳ Continuing anyway (SortPin may need a moment)")

        # ── 15-minute countdown ───────────────────────────────────────────
        print(f"\n  ⏱  Scrolling for {s['duration']//60} min "
              f"({s['duration']} s)  —  SortPin is saving pins...")
        result = countdown(kw, pos + 1, len(remaining), s["duration"])

        # ── Handle result ─────────────────────────────────────────────────
        if result in ("done", "next"):
            mark_done(progress, kw)
            done_count += 1
            label = "⏰ Time up" if result == "done" else "⏭  Skipped early"
            print(f"\n  ✅ {label}: \"{kw}\"   "
                  f"[{done_count}/{len(all_kws)} total done]")
            pos += 1

        elif result == "skip":
            print(f"\n  ⏭  Skipped (Not Yet kept): \"{kw}\"")
            pos += 1

        elif result == "quit":
            print(f"\n  ⚠  Quit — \"{kw}\" stays Not Yet")
            break

    # ── Session summary ────────────────────────────────────────────────────────
    elapsed = time.time() - total_start
    m, sec  = divmod(int(elapsed), 60)
    done_now   = sum(1 for kw in all_kws if is_done(progress, kw))
    still_left = len(all_kws) - done_now

    print(f"\n\n{'═'*65}")
    print(f"  Session finished  |  Time: {m}m {sec}s")
    print(f"  ✅ Done      : {done_now} / {len(all_kws)}")
    print(f"  ⏳ Not Yet   : {still_left}")
    if still_left:
        print(f"  → Re-run this script anytime to continue.")
    print(f"  → Run 3_sync_to_sheet.py to update your Google Sheet.")
    print(f"{'═'*65}\n")


if __name__ == "__main__":
    main()
