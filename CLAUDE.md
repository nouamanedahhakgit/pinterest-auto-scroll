# Pinterest Scan — Project Context

## What this project does
Automates Pinterest keyword scrolling so the **SortPin** browser extension (installed in Brave) saves pins automatically while the program scrolls each search page.

## Folder
`C:\Users\leno\Documents\GitHub\remove in next\pinterest scan\`

## Files
| File | Purpose |
|---|---|
| `keywords.txt` | 193 keywords, one per line. Lines starting `#` are skipped. |
| `progress.json` | Auto-created. Tracks Done / Not Yet per keyword. Delete to reset. |
| `1_setup_google_sheet.py` | Pastes keywords + URLs into Google Sheet column A/B/C. Run once. |
| `2_pinterest_auto_scroll.py` | **Main script.** Opens Brave, clicks SortPin button, counts down, auto-advances. |
| `3_sync_to_sheet.py` | Writes Done/Not Yet from progress.json into column D of the sheet. |
| `4_build_database.py` | Builds a local **relational** DB from SortPin data: Pinner→Boards→Pins. Outputs `sortpin.db` + `sortpin_data.json`. |
| `5_view_data.py` | Prints statistics + builds/opens `sortpin_viewer.html` to browse Pinner→Boards→Pins. |
| `sortpin.db` | Auto-created (step 4). SQLite: tables `pinners`, `boards`, `pins` with foreign keys. |
| `sortpin_data.json` | Auto-created (step 4). Nested Pinner→Boards→Pins (feeds the viewer). |
| `sortpin_viewer.html` | Auto-created (step 5). Self-contained offline browser of the data. |
| `Pinterest_Trends_Analysis_June2026.xlsx` | Full trend data (5 sheets). |

## Run commands
```
python 1_setup_google_sheet.py          # fill Google Sheet once
python 2_pinterest_auto_scroll.py --5m  # 5 min per keyword
python 2_pinterest_auto_scroll.py --2m  # 2 min per keyword
python 2_pinterest_auto_scroll.py       # interactive prompt
python 3_sync_to_sheet.py               # push statuses to sheet
python 4_build_database.py              # SortPin CSV exports → relational DB + JSON
python 4_build_database.py --live       # try pulling live from the extension first
python 5_view_data.py                   # stats + open the visual data browser
```

## Steps 4 & 5 — SortPin data → relational DB + viewer
- **Data model:** `leads` CSV = master pinners; `boards` link to pinner via `owner_username`;
  `pins` link to pinner via `pinner_username` and to a board via `board_url`.
- **Step 4 input:** export Pins / Boards / Pinners from the SortPin popup (the 3
  `SortPin.com_all_*.csv` files) into this folder, then run step 4. `--live` tries
  to read the extension's storage directly via CDP first, falling back to the CSVs.
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
