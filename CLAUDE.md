# Pinterest Scan — Project Context

## What this project does
Automates Pinterest keyword scrolling so the **SortPin** browser extension (installed in Brave) saves pins automatically while the program scrolls each search page.

## Folder
`C:\Users\leno\Documents\GitHub\remove in next\pinterest scan\`

## Files
| File | Purpose |
|---|---|
| `keywords.txt` | 193+ keywords, one per line. Lines starting `#` are skipped. |
| `progress.json` | Auto-created. Tracks Done / Not Yet per keyword. Delete to reset. |
| `0_get_keywords.py` | **Step 0.** Fetches trending keywords via Pinterest API or Google Trends → appends to keywords.txt. |
| `1_setup_google_sheet.py` | Pastes keywords + URLs into Google Sheet column A/B/C. Run once. |
| `2_pinterest_auto_scroll.py` | **Main script.** Opens Brave, clicks SortPin button, counts down, auto-advances. |
| `3_sync_to_sheet.py` | Writes Done/Not Yet from progress.json into column D of the sheet. |
| `4_build_database.py` | Builds a local **relational** DB from SortPin data: Pinner→Boards→Pins. Outputs `sortpin.db` + `sortpin_data.json`. |
| `5_view_data.py` | Prints statistics + builds/opens `sortpin_viewer.html` to browse Pinner→Boards→Pins. |
| `6_clear_sortpin.py` | Clears the SortPin extension's stored data (IndexedDB + big storage arrays) so it stops growing. Run step 4 first to save. |
| `sortpin.db` | Auto-created (step 4). SQLite: tables `pinners`, `boards`, `pins` with foreign keys. |
| `sortpin_data.json` | Auto-created (step 4). Nested Pinner→Boards→Pins (feeds the viewer). |
| `sortpin_viewer.html` | Auto-created (step 5). Self-contained offline browser of the data. |
| `Pinterest_Trends_Analysis_June2026.xlsx` | Full trend data (5 sheets). |

## Run commands
```
python 0_get_keywords.py                # fetch trending keywords → append to keywords.txt
python 0_get_keywords.py --google       # force Google Trends (no token needed)
python 0_get_keywords.py --pinterest    # force Pinterest API only
python 1_setup_google_sheet.py          # fill Google Sheet once
python 2_pinterest_auto_scroll.py --5m  # 5 min per keyword
python 2_pinterest_auto_scroll.py --2m  # 2 min per keyword
python 2_pinterest_auto_scroll.py       # interactive prompt
python 3_sync_to_sheet.py               # push statuses to sheet
python 4_build_database.py              # LIVE pull from SortPin extension via Brave (CDP)
python 4_build_database.py --disk       # read IndexedDB files directly, NO browser (pip install dfindexeddb)
python 4_build_database.py --csv        # skip live; build only from CSV exports in folder
python 5_view_data.py                   # stats + open the visual data browser
python 6_clear_sortpin.py               # clear extension data (run step 4 first!)
```

## Steps 4 & 5 — SortPin data → relational DB + viewer
- **Data model:** `leads` CSV = master pinners; `boards` link to pinner via `owner_username`;
  `pins` link to pinner via `pinner_username` and to a board via `board_url`.
- **Step 4 input:** by DEFAULT step 4 pulls live from the SortPin extension —
  it launches/attaches Brave on the debug port (closing any open Brave, like
  step 2), opens the extension popup pages via Selenium, and reads the
  extension's `chrome.storage.local` + IndexedDB. Data arrays are auto-detected
  by field signature (`pin_url`→pins, `owner_username`→boards, `contact_email`→
  pinners), so storage key names don't matter. If the live read fails it falls
  back to merging the `SortPin.com_all_*.csv` exports in the folder. Use `--csv`
  to force the CSV path only.
- **Note:** pins CSV `id` column is Excel-mangled (`1.00001E+18`); real pin id is
  recovered from `pin_url` (`/pin/<id>`). Empty phantom CSV rows are skipped.
- **Step 5:** `sortpin_viewer.html` is one offline file (data embedded) — searchable,
  sortable pinner list → click a pinner → their boards → click a board → its pins.

## Script 2 — how it works
1. Reads `progress.json`, skips already-Done keywords
2. Opens `https://www.pinterest.com/search/pins/?q=KEYWORD&rs=typed` in Brave
3. Scans screen for SortPin's blue button (`#3B82F6`) using numpy pixel detection → clicks it
4. Counts down chosen minutes (SortPin scrolls every 5 s on its own)
5. Auto-advances; marks keyword Done in `progress.json`

## Script 2 — controls (work while Brave is focused)
| Key | Action |
|---|---|
| `N` / `SPACE` | Mark Done + next keyword |
| `S` | Skip, keep Not Yet |
| `+` / `-` | ±1 minute duration |
| `ESC` | Quit (current keyword stays Not Yet) |
| Mouse → top-left | Emergency stop (pyautogui FAILSAFE) |

## Google Sheet
`https://docs.google.com/spreadsheets/d/1ZaIcgG7E2ChZYtUr9UZP78bfO-YNMArlbWZk_71E_VE/edit`
Columns: A=Keyword | B=Pinterest URL | C=Trends URL | D=Status (Done / Not Yet)

## Environment
- OS: Windows 11, Brave at `C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe`
- Python 3.9, packages: pyautogui, numpy, keyboard, pyperclip, openpyxl, requests, selenium

## SortPin button detection
- Colour: `bg-blue-500` = `#3B82F6` = RGB(59, 130, 246), tolerance 30, min 40 pixels
- Retries 10× with 2 s gap before giving up
- If not found: prints warning, continues anyway

## Keywords source
Pinterest Summer 2026 Trend Report + Pinterest Predicts 2026 (21 trends).
Categories in keywords.txt: Beach/Vacation, Sport-Luxe, Jersey/Varsity, Dockside/Sailorcore,
Sneakers, Sunglasses, Hair, Makeup, Accessories, Athlete Aesthetic, Pinterest Predicts 2026.
