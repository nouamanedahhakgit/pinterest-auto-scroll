"""
STEP 3 — Sync progress.json statuses to Google Sheet (column D)
================================================================
Reads your local progress.json and updates column D in the sheet
with "Done" or "Not Yet" for every keyword.

HOW TO USE:
  1. Run: python 3_sync_to_sheet.py
  2. Brave opens your Google Sheet automatically
  3. Make sure you are LOGGED IN to Google
  4. Press ENTER in this terminal when the sheet is ready
  5. Statuses paste into column D automatically

You can run this any time after a scrolling session.
"""

import os, sys, json, time
import subprocess, pyperclip, pyautogui

# ═══ CONFIG ══════════════════════════════════════════════════════════════════
BRAVE_PATH     = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
KEYWORDS_FILE  = "keywords.txt"
PROGRESS_FILE  = "progress.json"
SHEET_URL      = "https://docs.google.com/spreadsheets/d/1ZaIcgG7E2ChZYtUr9UZP78bfO-YNMArlbWZk_71E_VE/edit"
STATUS_DONE    = "Done"
STATUS_NOT_YET = "Not Yet"
# ═════════════════════════════════════════════════════════════════════════════

BASE = os.path.dirname(os.path.abspath(__file__))

def load_keywords():
    path = os.path.join(BASE, KEYWORDS_FILE)
    if not os.path.exists(path):
        print(f"ERROR: {KEYWORDS_FILE} not found."); sys.exit(1)
    kws = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                kws.append(line)
    return kws

def load_progress():
    path = os.path.join(BASE, PROGRESS_FILE)
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def main():
    keywords = load_keywords()
    progress = load_progress()
    total    = len(keywords)

    done_count = sum(1 for kw in keywords
                     if progress.get(kw, {}).get("status") == "done")
    not_yet    = total - done_count

    print(f"\n{'═'*55}")
    print(f"  Google Sheet Status Sync")
    print(f"{'─'*55}")
    print(f"  Keywords  : {total}")
    print(f"  Done      : {done_count}")
    print(f"  Not Yet   : {not_yet}")
    print(f"{'═'*55}\n")

    # Preview first 10
    print("  Preview (first 10):")
    print(f"  {'Keyword':<38} Status")
    print(f"  {'─'*38} ──────────")
    for kw in keywords[:10]:
        st = STATUS_DONE if progress.get(kw, {}).get("status") == "done" else STATUS_NOT_YET
        mark = "✅" if st == STATUS_DONE else "⏳"
        print(f"  {kw:<38} {mark} {st}")
    if total > 10:
        print(f"  ... and {total - 10} more\n")
    else:
        print()

    # Build status column (one value per line = one row in Sheets)
    statuses = []
    for kw in keywords:
        st = STATUS_DONE if progress.get(kw, {}).get("status") == "done" else STATUS_NOT_YET
        statuses.append(st)

    pyperclip.copy("\n".join(statuses))
    print(f"  ✅ {total} statuses copied to clipboard\n")

    # Open sheet in Brave
    print(f"  Opening Google Sheet in Brave...")
    subprocess.Popen([BRAVE_PATH, SHEET_URL])

    print(f"\n{'═'*55}")
    print(f"  WAITING FOR YOU:")
    print(f"  1. Make sure you are LOGGED IN to Google in Brave")
    print(f"  2. Wait for the sheet to fully load")
    print(f"  3. Press ENTER here (do NOT click the sheet yet)")
    print(f"{'═'*55}")
    input("\n  >> Press ENTER when the sheet is loaded: ")

    print(f"\n  Navigating to cell D2 in 3 seconds...")
    print(f"  Keep Brave as the focused window!\n")
    time.sleep(3)

    # Navigate to D2:
    # Ctrl+Home → A1, then 3x Right → D1, then 1x Down → D2
    pyautogui.hotkey("ctrl", "Home")
    time.sleep(0.4)
    pyautogui.press("right", presses=3, interval=0.12)   # A1 → D1
    time.sleep(0.2)
    pyautogui.press("down",  presses=1)                   # D1 → D2
    time.sleep(0.2)

    # Paste (each line goes to the next row automatically in Sheets)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(2)

    print(f"  ✅ Done! {total} statuses written to column D.")
    print(f"     Done      = {done_count} rows")
    print(f"     Not Yet   = {not_yet} rows\n")
    print(f"  Tip: In the sheet, use Filter → column D → 'Not Yet'")
    print(f"  to see exactly which keywords still need scrolling.\n")

if __name__ == "__main__":
    main()
