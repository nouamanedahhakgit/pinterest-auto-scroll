"""
STEP 1 — Upload keywords to Google Sheet
=========================================
Opens your Google Sheet in Brave and pastes all keywords
with their Pinterest URLs into columns A and B.

HOW TO USE:
  1. Run this script: python 1_setup_google_sheet.py
  2. Wait for the sheet to open in Brave
  3. Make sure you are LOGGED IN to Google
  4. Click once on cell A1 in the sheet
  5. Come back to this terminal and press ENTER
  6. Keywords will be pasted automatically
"""

import subprocess
import time
import sys
import os
import pyperclip
import pyautogui

# ═══ CONFIG ══════════════════════════════════════════════════════════════════
BRAVE_PATH    = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
KEYWORDS_FILE = "keywords.txt"
SHEET_URL     = "https://docs.google.com/spreadsheets/d/1ZaIcgG7E2ChZYtUr9UZP78bfO-YNMArlbWZk_71E_VE/edit"
# ═════════════════════════════════════════════════════════════════════════════

def load_keywords():
    if not os.path.exists(KEYWORDS_FILE):
        print(f"ERROR: {KEYWORDS_FILE} not found. Run this from the pinterest scan folder.")
        sys.exit(1)
    kws = []
    with open(KEYWORDS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                kws.append(line)
    return kws

def make_pinterest_url(kw):
    return "https://www.pinterest.com/search/pins/?q=" + kw.replace(" ", "+") + "&rs=typed"

def make_trends_url(kw):
    return "https://trends.pinterest.com/search?country=US&query=" + kw.replace(" ", "+")

def main():
    keywords = load_keywords()
    total    = len(keywords)

    print(f"\n{'='*55}")
    print(f"  Pinterest → Google Sheet Setup")
    print(f"  {total} keywords found in {KEYWORDS_FILE}")
    print(f"{'='*55}\n")

    # Build tab-separated data: Keyword | Pinterest Search URL | Trends URL | #
    header = "Keyword\tPinterest Search URL\tPinterest Trends URL\tStatus"
    rows   = [header]
    for i, kw in enumerate(keywords, 1):
        pin_url    = make_pinterest_url(kw)
        trends_url = make_trends_url(kw)
        rows.append(f"{kw}\t{pin_url}\t{trends_url}\t")

    tsv_data = "\n".join(rows)
    pyperclip.copy(tsv_data)
    print(f"  ✅ {total} keywords copied to clipboard (Tab-Separated)\n")

    # Open sheet in Brave
    print(f"  Opening Google Sheet in Brave...")
    subprocess.Popen([BRAVE_PATH, SHEET_URL])

    print(f"\n{'='*55}")
    print(f"  WAITING FOR YOU:")
    print(f"  1. Make sure you are LOGGED IN to Google in Brave")
    print(f"  2. Click on cell A1 in the spreadsheet")
    print(f"  3. Come back here and press ENTER")
    print(f"{'='*55}")
    input("\n  >> Press ENTER when you have clicked on cell A1: ")

    print(f"\n  Pasting in 3 seconds... keep Brave focused!")
    time.sleep(3)

    # Paste (Ctrl+V)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(2)

    print(f"\n  ✅ Done! {total} keywords + URLs pasted into your Google Sheet.")
    print(f"  Columns: A=Keyword | B=Pinterest URL | C=Trends URL | D=Status\n")
    print(f"  Next step: run  python 2_pinterest_auto_scroll.py\n")

if __name__ == "__main__":
    main()
