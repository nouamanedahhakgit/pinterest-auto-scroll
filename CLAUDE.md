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
| `6_clear_sortpin.py` | Archives extension data into `_SORTPIN_ARCHIVE/` then clears it from Brave. Run step 4 first to save. |
| `7_scrape_profiles.py` | **Deep scrape:** for each pinner → open profile → open every board → scroll till end. Resumable (`profiles_progress.json`). |
| `magic_scroll.py` | **All-in-one multi-computer loop:** claim 5 keywords from sheet (pending) → scroll → build DB → mark Done → clear SortPin → repeat. Also has a **pinner mode** (`--pinner N`) that deep-scrapes pinners instead — see below. |
| `sortpin.db` | Auto-created (step 4). SQLite: tables `pinners`, `boards`, `pins` with foreign keys. |
| `sortpin_data.json` | Auto-created (step 4). Flat pinners/boards/pins arrays (feeds the viewer). |
| `sortpin_viewer.html` | Auto-created (step 5). Offline browser with Pinners / Boards / Pins tabs. |
| `IMPORTANT_DATABASE/` | Auto-created (step 4). `sortpin_mysql.sql` (import into MySQL) + a copy of `sortpin.db`. |
| `_SORTPIN_ARCHIVE/` | Auto-created (step 6). Timestamped backups of cleared extension data. Gitignored. |
| `google_sheets_apps_script.js` | Apps Script web app (v3): setup/column/**claim**/**mark** (LockService = multi-PC safe). Redeploy after edits. |
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
python 4_build_database.py --disk       # read IndexedDB files directly, NO browser (pip install ccl_chromium_reader)
python 4_build_database.py --disk --archive  # rebuild from _SORTPIN_ARCHIVE backups (all cleared history)
python 4_build_database.py --csv        # skip live; build only from CSV exports in folder
python 4_build_database.py              # also writes IMPORTANT_DATABASE/sortpin_mysql.sql
python 5_view_data.py                   # static viewer (reads sortpin.db → Pinners/Boards/Pins tabs)
python 5_view_data.py --server          # LIVE local server: card+TABLE views, full pin detail, images
python 6_clear_sortpin.py               # archives backup → _SORTPIN_ARCHIVE/ then clears
python 6_clear_sortpin.py --yes         # no confirmation prompt
python 7_scrape_profiles.py             # deep-scrape pinners → boards → pins (20 pinners/run)
python 7_scrape_profiles.py --limit 50  # process 50 pinners this run (resumable)
python magic_scroll.py                  # multi-PC: claim→scroll→build→done→clear→repeat (5 min/kw)
python magic_scroll.py --2m --batch 5   # 2 min per keyword, 5 keywords per cycle
python magic_scroll.py --disk           # build DB from disk each cycle (needs ccl_chromium_reader)
python magic_scroll.py --pinner 10      # PINNER MODE: claim 10 pinners from sortpin.db, deep-scrape each (5 min/board default)
python magic_scroll.py --pinner 10 --15m       # 15 min cap per board/profile instead of the 5 min default
python magic_scroll.py --pinner 10 --blog-only # only pinners step 7 classified as "blog" sites
```

## magic_scroll — multi-computer workflow
- Each computer loops: **claim** up to N "Not Yet" keywords from the sheet (→ "pending",
  atomic via Apps Script `LockService` so no two PCs take the same), **scroll** them in
  Brave, run step 4 to **save** all data (sortpin.db + sortpin_mysql.sql), **mark Done**
  on the sheet, run step 6 to **clear** SortPin (archived first), then repeat.
- One-time: redeploy `google_sheets_apps_script.js` (v3, has claim/mark) as the web app;
  `google_sheets_webapp.json` holds the `/exec` URL. Sheet col A = keyword, col D = Status.

## magic_scroll — pinner mode (`--pinner N`)
- Same script, totally different loop: deep-scrapes **pinners** instead of keywords. No
  Google Sheet involved — pinners + progress live in `sortpin.db`'s `status` column
  (`pinners`/`boards` tables, same as step 7), so it's resumable and multi-PC safe just by
  re-running (claimed rows are marked `'running'` immediately).
- Each cycle: **claim** up to N not-`done` pinners (highest `follower_count` first) →
  **scan** each one (saved profile → boards, created profile → created pins, then every
  board scrolled till it stops loading new pins — step 7's logic) → **build** the DB
  (`sortpin.db` is *never* deleted in this mode, since it's what tracks progress) →
  **clear** SortPin → repeat until no pinners are left.
- `--blog-only` adds step 7's "only pinners whose site is classified as a blog" filter.
  It's off by default because `scraped_websites` currently has very few classified
  domains — with it on by default the filter would silently match nothing.
- Per-board/profile time cap defaults to 5 minutes; override with `--Nm` (e.g. `--15m`).

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
