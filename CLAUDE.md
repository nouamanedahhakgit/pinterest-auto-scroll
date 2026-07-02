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
| `10_domain_quick_scrape_api.py` | Flask app + bulk job: heuristically classifies each pinner's `website` (Store / Link-in-Bio / Social Media / Blog / General Website) via `STORE_DOMAINS`/`LINK_IN_BIO_DOMAINS` lists + WordPress/post-count signals. Writes `scrapped`/`site_type`/`categories` on the Sheet's `websites` tab + local `scraped_websites`. |
| `13_scan-website-interface-by-ia.py` | **AI website scanner bot.** Standalone, multi-threaded, separate from step 10. For every General-Website/Blog/not-yet-classified pinner site (skips Store/Link-in-Bio/Social Media — step 10 already nailed those), fetches the homepage and asks an OpenRouter model for the site's real type, a short filterable category/theme, and a description — catches step 10's false "Blog" calls on sites that just have a marketing blog bolted onto a SaaS/store/agency. Writes 4 new columns ending in `_website_scaned_by_ia` (local `sortpin.db` + the Sheet, auto-created on first run). Scans the whole backlog once, then polls every 10 min forever for newly-eligible sites — leave it running, Ctrl+C to stop. |
| `14_download_blog_pin_links.py` | **Blog pin destination-link downloader bot.** Standalone, multi-threaded. Syncs the Sheet's `site_type` column into `pinners.site_type` (local sortpin.db, or the cloud MySQL `pinners` table — see `--source` below), then for every pin whose pinner's `site_type` contains "blog", downloads the pin's outbound `link` (not `pin_url`) — page HTML + every linked CSS/JS file + inline `<style>`/`<script>` blocks — into 5 new `pins` columns. De-dupes by unique link before downloading (a link pinned many times is fetched once, written to every matching pin). Writes to the DB as each link completes, not batched. Detects Cloudflare blocks specifically (vs a generic HTTP block) and tries a curl_cffi Chrome-impersonation bypass first. **`--source mysql\|sqlite\|auto`** (default `mysql`): `mysql` reads/writes the shared cloud MySQL DB (every PC's pins+pinners — see `8_sync_to_mysql.py`) and exits if `.env`'s `MYSQL_PASSWORD` isn't configured/reachable, `sqlite` is this PC's local `sortpin.db` only, `auto` picks `mysql` when `.env` has a working `MYSQL_PASSWORD` else falls back to `sqlite` (this was the old default). Downloads the whole backlog once, then polls every 10 min forever for newly-eligible pins — leave it running, Ctrl+C to stop. |
| `magic_scroll.py` | **All-in-one multi-computer loop:** claim 5 keywords from sheet (pending) → scroll → build DB → mark Done → clear SortPin → repeat. Also has a **pinner mode** (`--pinner N`) that deep-scrapes pinners instead — see below. |
| `sortpin.db` | Auto-created (step 4). SQLite: tables `pinners`, `boards`, `pins` with foreign keys. |
| `sortpin_data.json` | Auto-created (step 4). Flat pinners/boards/pins arrays (feeds the viewer). |
| `sortpin_viewer.html` | Auto-created (step 5). Offline browser with Pinners / Boards / Pins tabs. |
| `websites_sheet_cache.json` | Auto-created (step 13). Last successful `get_websites` Sheet pull — fallback when that read flakes. |
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
python 10_domain_quick_scrape_api.py             # DEFAULT = bulk scan pending sites (same as --run), exits when sheet queue is empty
python 10_domain_quick_scrape_api.py --run       # explicit, identical to the no-flags default
python 10_domain_quick_scrape_api.py --runjobforall  # scan every site in the sheet, ignoring site_type/done filters
python 10_domain_quick_scrape_api.py --serve     # start the Flask API server instead (must be requested explicitly)
python magic_scroll.py                  # multi-PC: claim→scroll→build→done→clear→repeat (5 min/kw)
python magic_scroll.py --2m --batch 5   # 2 min per keyword, 5 keywords per cycle
python magic_scroll.py --disk           # build DB from disk each cycle (needs ccl_chromium_reader)
python magic_scroll.py --pinner 10      # PINNER MODE: claim 10 pinners from sortpin.db, deep-scrape each (5 min/board default)
python magic_scroll.py --pinner 10 --15m       # 15 min cap per board/profile instead of the 5 min default
python magic_scroll.py --pinner 10 --blog-only # only pinners step 7 classified as "blog" sites
python magic_scroll.py --pinner 10 --min-reach 100000              # only pinners with profile_reach >= 100k
python magic_scroll.py --pinner 10 --blog-only --min-reach 100000  # blog sites AND Reach > 100k — never stops, idles 5 min between checks for new ones
python 13_scan-website-interface-by-ia.py                  # AI scanner bot: scan all eligible sites, then poll every 10 min forever
python 13_scan-website-interface-by-ia.py --once            # single pass then exit (no polling)
python 13_scan-website-interface-by-ia.py --workers 100     # more parallel threads (default 60)
python 13_scan-website-interface-by-ia.py --poll-minutes 5  # check more often than 10 min
python 13_scan-website-interface-by-ia.py --limit 20 --once --dry-run  # quick test, no writes
python 14_download_blog_pin_links.py                       # download bot: mysql by default, sync site_type, download all eligible pin links, then poll every 10 min forever
python 14_download_blog_pin_links.py --once                # single pass then exit (no polling)
python 14_download_blog_pin_links.py --workers 120         # more parallel threads (default 80)
python 14_download_blog_pin_links.py --poll-minutes 5      # check more often than 10 min
python 14_download_blog_pin_links.py --retry-failed        # also re-attempt Failed/Blocked pins, not just untried ones
python 14_download_blog_pin_links.py --limit 20 --once --dry-run  # quick test, no writes
python 14_download_blog_pin_links.py --source mysql         # explicit (same as default) — cloud MySQL, every PC's pins+pinners
python 14_download_blog_pin_links.py --source sqlite        # force this PC's local sortpin.db only
python 14_download_blog_pin_links.py --source auto          # mysql if .env has MYSQL_PASSWORD, else sqlite (old default)
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
  **clear** SortPin → repeat.
- `--blog-only` requires a **confirmed** "blog" classification — checked against the
  Google Sheet's `websites` tab first (`site_type` column, only trusted when `scrapped`
  starts with "Yes"), since `10_domain_quick_scrape_api.py` classification usually runs
  on a different PC than the one doing `--pinner` mode, so the local `scraped_websites`
  table here is often empty/stale. Falls back to local `scraped_websites` (status='done'
  rows only) for any pinner not yet on the Sheet, or if the Sheet/web app is unreachable.
  An unscanned, failed, or blocked site is never assumed to be a blog — it's excluded.
  (Fixed a bug where an empty local classification table made this silently match
  *everyone* instead of no one.)
- `--min-reach N` requires reach `>= N` (bare `--min-reach` with no number defaults to
  100000) — uses the Sheet's `reach` column when present and nonzero, else falls back to
  local `pinners.profile_reach`. Combine with `--blog-only` to get exactly "blog sites
  with Reach > 100k" — both filters AND together.
- Per-board/profile time cap defaults to 5 minutes; override with `--Nm` (e.g. `--15m`).
- **Never stops.** When a cycle finds zero eligible pinners (all done, or none yet
  match the filters), it idles 5 minutes and checks again — so pinners added or
  newly classified later (new keyword scans, new site-type classifications from
  `10_domain_quick_scrape_api.py`) get picked up automatically without restarting.
  Leave it running in the background; Ctrl+C to stop.

## Step 13 — AI website scanner bot (`13_scan-website-interface-by-ia.py`)
- **Why it exists:** step 10's classifier is a fast heuristic (domain lists + WordPress/
  post-count signals) and over-fires "Blog" for any site with an RSS feed or `/blog/`
  folder anywhere on the page — even SaaS tools, agencies, and stores that just publish a
  marketing blog as one feature (e.g. postermywall.com). This script asks an actual AI
  model to read the homepage and judge the site by its real primary purpose.
- **Eligibility:** skips any pinner website whose existing `site_type` is `Store`,
  `Link-in-Bio`, or `Social Media` — step 10 already classified those correctly and
  confidently. Scans everything else: `General Website`, `Blog` (re-verifies it), and
  blank/not-yet-classified. Also skips any row that already has a value in
  `status_website_scaned_by_ia` (so re-running only picks up new/unscanned sites).
- **Standalone, separate from step 10:** its own `.env`/DB/Sheet-client code, no shared
  state or imports from `10_domain_quick_scrape_api.py`. Uses OpenRouter directly via
  `requests` (model from `.env`'s `QUICK_SCRAPE_OPENROUTER_MODEL`, default
  `openai/gpt-4.1-nano`) — needs `OPENROUTER_API_KEY` in `.env`.
- **Output — 4 new columns**, written to both local `sortpin.db` (`scraped_websites`
  table, auto-migrated) and the Sheet's `websites` tab (auto-created there on first run
  via one `update_website` warm-up call, since the Sheet's `batch_update_websites` action
  can't create new columns itself — only `update_website` can):
  - `status_website_scaned_by_ia` — `Done` / `Failed (...)` / `Blocked (...)`
  - `type_website_scaned_by_ia` — Blog / Store / SaaS/Tool / Portfolio / News/Media / etc.
  - `category_website_scaned_by_ia` — short 1-3 word theme (e.g. "Fashion & Beauty"),
    meant for Sheet filtering
  - `description_website_scaned_by_ia` — 1-2 sentence plain description
- **Speed:** high thread count (`--workers`, default 60) fetches+classifies sites in
  parallel; token cost is a non-issue by design (nano model, short responses). Each site
  gets a fast TCP ping (port 443 then 80, hostname matched exactly to what the real
  fetch will use — not the www-stripped `domain` field, since some sites only have a
  DNS record for one of bare/`www.`) before anything else — dead/unreachable domains
  fail in ~6s (hard-capped) instead of stalling on a slow DNS lookup or the full request
  timeout. The script also forces IPv4-only DNS resolution process-wide (monkeypatches
  `socket.getaddrinfo`): some sites publish an IPv6 record that's dead/blackholed on a
  given network, and unlike browsers (real Happy Eyeballs, RFC 6555), Python's
  socket/requests stack tries addresses sequentially and burns the full timeout on the
  dead IPv6 address before falling back to the working IPv4 one — explains sites that
  load fine in a browser but were slow/"unreachable" here. The homepage GET also bypasses
  any system/VPN/antivirus HTTP proxy explicitly (`proxies={"http": None, "https": None}`)
  — `requests` honors such a proxy by default while the raw-socket ping never does, so a
  local proxy can make ping pass fast while the real fetch silently stalls behind it — and
  streams the body itself under a hard 15s wall-clock deadline, since `requests`' own
  read-timeout only bounds gaps *between* chunks, not total transfer time (a server
  trickling data without ever going silent for the full read-timeout window can otherwise
  hang far longer than the nominal timeout suggests). Every result line also prints its own
  elapsed seconds plus a pass-total/average at the end; a background watchdog also prints a
  heartbeat for any single site stuck on the same phase (pinging/fetching/asking AI) for
  5s+, naming the domain and phase directly instead of going silent until it times out.
  The Sheet's `get_websites` read (69k+ rows) is retried automatically (3 attempts, 3s/6s
  backoff) if it errors or comes back with a valid-but-empty list — a heavy read like that
  occasionally has a transient Apps Script hiccup. If still empty after retries, it falls
  back to `websites_sheet_cache.json` (the last successful pull, auto-saved every time one
  succeeds) instead of just the thin local `scraped_websites` table — so one flaky read
  doesn't make the whole 37k-site backlog look like it vanished.
- **Bot behaviour:** does one fast full pass over the current backlog, then — once
  nothing eligible is left — idles and re-checks every `--poll-minutes` (default 10) for
  newly-synced or newly-eligible sites. Never stops on its own; `--once` for a single pass,
  Ctrl+C to quit the loop. `--dry-run` scans and prints without writing anything;
  `--limit N` caps how many rows a pass scans (handy for testing).

## Step 14 — Blog pin destination-link downloader (`14_download_blog_pin_links.py`)
- **Why it exists:** download the actual content (HTML/CSS/JS) behind every pin that
  belongs to a pinner whose website is a confirmed blog — pins from non-blog pinners are
  never touched.
- **"Is this pinner a blog?" — local, not per-pin Sheet calls:** a `site_type` column
  on the `pinners` table (local `sortpin.db`, or MySQL's `pinners` table in `--source
  mysql` mode — see below). At the start of every pass the script bulk-syncs it straight
  from the Sheet's `websites` tab `site_type` column, matched purely by `id` (= pinner
  `username`) — no domain-guessing needed. After that one sync call, the blog filter
  (`site_type` contains "blog", case-insensitive — same substring check as magic_scroll's
  `--blog-only`) runs entirely off the DB. Sync uses the same 3-attempt retry +
  `websites_sheet_cache.json` fallback as step 13 (shared cache file — either script's
  last successful pull benefits the other); if the Sheet's unreachable, it just keeps
  whatever `site_type` is already stored rather than blocking.
- **Two pin sources — `--source {mysql,sqlite,auto}` (default `mysql`):**
  - `mysql` — (default) the shared cloud MySQL database `8_sync_to_mysql.py` pushes every PC's data
    into. Local `sortpin.db` only has what *this* machine has scraped; MySQL is the union
    across every PC, since `8_sync_to_mysql.py` additively `INSERT IGNORE`s each PC's rows
    into the same shared tables — this is why MySQL holds far more pins/pinners than any
    one PC's local DB. **Site_type is written straight into MySQL's `pinners` table** via
    a plain `UPDATE ... WHERE username=...` (not `INSERT IGNORE`) — `8_sync_to_mysql.py`'s
    own pinners sync is insert-only and never updates a row already in MySQL, so a
    classification made locally *after* that pinner's row was already synced would
    otherwise never reach MySQL. Eligible pins are then found with a direct SQL
    `INNER JOIN` (`pins.pinner_username = pinners.username AND pinners.site_type LIKE
    '%blog%'`), and the same 5 download columns are added to MySQL's `pins` table and
    written there too, so progress is shared across every PC running the script. A
    selected batch is immediately marked `link_download_status='Running'` as best-effort
    dedup against another PC running concurrently — not a hard lock; a crashed run can
    leave pins stuck at `'Running'`, cleared by rerunning with `--retry-failed` (which
    bypasses the status filter entirely). Exits instead of falling back if MySQL isn't
    reachable/configured (check `.env`'s `MYSQL_PASSWORD`).
  - `sqlite` — this PC's local `sortpin.db` only, the script's original mode. If the Sheet
    is unreachable, falls back to the local `scraped_websites` table (status='done' rows,
    matched by domain — mirrors magic_scroll's `--blog-only` fallback).
  - `auto` — `mysql` when `.env` has a working `MYSQL_PASSWORD` (same credential keys as
    `8_sync_to_mysql.py`: `MYSQL_HOST`/`MYSQL_PORT`/`MYSQL_DB`/`MYSQL_USER`/
    `MYSQL_PASSWORD`), else falls back to `sqlite`. This was the default before `mysql`
    became the default.
- **What gets downloaded:** the pin's `link` column — the real outbound URL the pin
  points to, NOT `pin_url` (that's just the Pinterest pin page itself). De-duplicates by
  unique `link` before downloading (the same URL is often pinned many times across boards/
  repins) — each unique link is fetched once, and that one result is written to every pin
  row sharing it.
- **Output — 5 new columns on `pins`:**
  - `link_download_status` — `Done` / `Blocked (Cloudflare)` / `Blocked (HTTP ...)` /
    `Failed (...)` / `Failed (no link)`
  - `link_downloaded_at` — timestamp of the attempt
  - `link_html` — destination page's raw HTML (capped ~3MB)
  - `link_css` — inline `<style>` blocks + every linked external `.css` file's contents,
    concatenated (capped ~4MB total)
  - `link_js` — inline `<script>` blocks + every linked external `.js` file's contents,
    concatenated (capped ~6MB total)
- **Cloudflare detected specifically** (not lumped into a generic block reason): on a
  403/429/503, first retries once via curl_cffi's Chrome-impersonation bypass (same
  technique step 13 uses); if that also fails, checks response headers (`cf-ray`,
  `Server: cloudflare`) and known challenge-page text ("Checking your browser...",
  "Just a moment...", etc.) to label it `Blocked (Cloudflare)` specifically rather than
  just `Blocked (HTTP 403)`.
- **Speed / concurrency:** same proven primitives as step 13 — process-wide IPv4-only DNS
  monkeypatch, a dedicated hard-ceiling TCP ping pre-check (`quick_ping`), proxy-bypassed
  streaming GETs with a real wall-clock deadline (not just requests' between-chunk
  read-timeout), and a watchdog heartbeat thread that prints anything stuck 5s+ in the
  same phase. High-thread-count `ThreadPoolExecutor` (`--workers`, default 80) downloads
  many destination pages + their assets in parallel; each completed unique link is
  written to the DB IMMEDIATELY inside `handle_result` (lock-guarded UPDATE, not buffered
  to the end), so progress survives an interruption.
- **Standalone, separate from steps 10/13:** its own DB/Sheet-client code, no shared
  state or imports (those scripts' filenames aren't even valid Python module names; the
  MySQL connection helper is its own copy too, not imported from `8_sync_to_mysql.py`).
  No AI classification involved — pure download job. `.env`/`MYSQL_PASSWORD` is only
  needed for `--source mysql`/`auto`'s MySQL path; `--source sqlite` needs no `.env`.
- **Bot behaviour:** downloads the whole eligible backlog, then — once nothing eligible is
  left — idles and re-checks every `--poll-minutes` (default 10) for newly-scraped pins or
  newly-confirmed-blog pinners. Never stops on its own; `--once` for a single pass, Ctrl+C
  to quit. `--dry-run` downloads and prints without writing anything; `--limit N` caps how
  many unique links a pass downloads (testing); `--retry-failed` also re-attempts pins
  already marked Failed/Blocked instead of only untried ones.

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
- **Site-scan status column:** every pinner row (in `--server` table view, card view badge,
  and the static viewer's JSON) carries a live-computed `site_scan_status`: `done` /
  `running` / `blocked` / `failed` / `not_yet` (from `scraped_websites.status`, matched
  by domain), `unscanned` (has a website but `10_domain_quick_scrape_api.py` hasn't
  reached that domain yet), or `no_website`. It's computed on the fly from `pinners` +
  `scraped_websites`, not a stored column, so it's never stale or wiped by a rebuild.
  On the **Sheet** side, the `websites` tab's `scrapped` column already serves the same
  purpose — `10_domain_quick_scrape_api.py` writes real values there as it works
  (`Not Yet` → `Running` → `Yes`/`Failed (...)`/`Blocked (...)`); the value `run_websites_sync`
  writes at first-sync time (`"not yet"`) is just a placeholder until step 10 visits it.

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
