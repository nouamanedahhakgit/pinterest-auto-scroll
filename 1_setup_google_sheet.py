"""
STEP 1 — Upload keywords to Google Sheet (background)
======================================================
Writes keywords + Pinterest URLs + Trends URLs + Status into columns A–D.

HOW TO USE:
  python 1_setup_google_sheet.py           # tries web app, then Brave auto-paste
  python 1_setup_google_sheet.py --brave # force Brave paste (no Apps Script)
  python 1_setup_google_sheet.py --check # test web app deployment
"""

import json
import os
import sys

from google_sheets_client import (
    BASE,
    SPREADSHEET_ID,
    SETUP_MSG,
    APPS_SCRIPT,
    WEBAPP_FILE,
    choose_backend,
    get_gspread_client,
    post_webapp,
    probe_webapp,
    resolve_webapp,
    get_existing_sheet_statuses,
)

KEYWORDS_FILE = "keywords.txt"
PROGRESS_FILE = "progress.json"
STATUS_DONE = "Done"
STATUS_NOT_YET = "Not Yet"
CHUNK_SIZE = 400
FORCE_BRAVE = "--brave" in sys.argv


def load_keywords():
    path = os.path.join(BASE, KEYWORDS_FILE)
    if not os.path.exists(path):
        print(f"ERROR: {KEYWORDS_FILE} not found.")
        sys.exit(1)
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


def make_pinterest_url(kw):
    return "https://www.pinterest.com/search/pins/?q=" + kw.replace(" ", "+") + "&rs=typed"


def make_trends_url(kw):
    return "https://trends.pinterest.com/search?country=US&query=" + kw.replace(" ", "+")



def build_rows(keywords, progress, sheet_statuses):
    rows = []
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in sheet_statuses:
            st = sheet_statuses[kw_lower]
        else:
            st = STATUS_DONE if progress.get(kw, {}).get("status") == "done" else STATUS_NOT_YET
        rows.append([kw, make_pinterest_url(kw), make_trends_url(kw), st])
    return rows


def build_tsv(rows):
    lines = ["Keyword\tPinterest Search URL\tPinterest Trends URL\tStatus"]
    for row in rows:
        lines.append("\t".join(row))
    return "\n".join(lines)


def upload_via_webapp_setup(rows, cfg):
    data = post_webapp(cfg, {"action": "setup", "rows": rows})
    if data.get("action") != "setup":
        raise RuntimeError("setup not supported")
    return data.get("count", len(rows))


def upload_via_webapp_columns(rows, cfg):
    keywords, pin_urls, trends_urls, statuses = zip(*rows) if rows else ([], [], [], [])
    post_webapp(cfg, {"column": 1, "set_header": True, "statuses": list(keywords)})
    post_webapp(cfg, {"column": 2, "statuses": list(pin_urls)})
    post_webapp(cfg, {"column": 3, "statuses": list(trends_urls)})
    post_webapp(cfg, {"column": 4, "statuses": list(statuses)})
    return len(rows)


def upload_via_webapp(rows, cfg):
    mode = probe_webapp(cfg)
    if mode == "sync-only":
        raise RuntimeError("sync-only")
    if mode == "setup":
        print("  Mode: full upload (setup)")
        return upload_via_webapp_setup(rows, cfg)
    print("  Mode: column upload (A → B → C → D)")
    return upload_via_webapp_columns(rows, cfg)


def upload_via_api(rows):
    gc, auth_mode = get_gspread_client()
    if gc is None:
        raise RuntimeError("no api credentials")

    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.sheet1
    header = [["Keyword", "Pinterest Search URL", "Pinterest Trends URL", "Status"]]
    ws.clear()
    ws.update("A1:D1", header, value_input_option="RAW")

    total = 0
    for i in range(0, len(rows), CHUNK_SIZE):
        chunk = rows[i : i + CHUNK_SIZE]
        start_row = i + 2
        end_row = start_row + len(chunk) - 1
        ws.update(f"A{start_row}:D{end_row}", chunk, value_input_option="RAW")
        total += len(chunk)
        print(f"  Uploaded rows {start_row}–{end_row}...")

    print(f"  Auth: {auth_mode} | Sheet: {sh.title}")
    return total


def upload_via_brave(rows):
    from google_sheets_brave import upload_tsv_full

    print("  Apps Script not working — using Brave auto-paste (logged-in Google required)")
    print("  A sheet tab will open briefly and paste all data at A1...")
    tsv = build_tsv(rows)
    upload_tsv_full(tsv)
    return len(rows)


def cmd_check():
    webapp = resolve_webapp()
    if not webapp:
        print(f"  ✗ Missing {WEBAPP_FILE}\n")
        sys.exit(1)

    print(f"\n  Web app URL: {webapp['url'][:72]}...")
    try:
        mode = probe_webapp(webapp)
        print(f"  Apps Script: ✅ OK ({mode}) — background upload works\n")
        sys.exit(0)
    except RuntimeError as e:
        print(f"  Apps Script: ✗ {e}\n")
        print("  You can still upload without fixing Apps Script:")
        print("    python 1_setup_google_sheet.py --brave\n")
        sys.exit(1)


def main():
    if "--check" in sys.argv:
        cmd_check()

    keywords = load_keywords()
    progress = load_progress()
    
    webapp = resolve_webapp()
    print("Fetching existing keyword statuses from Google Sheets...")
    sheet_statuses = get_existing_sheet_statuses(webapp)
    
    rows = build_rows(keywords, progress, sheet_statuses)
    total = len(rows)

    print(f"\n{'═'*55}")
    print(f"  Pinterest → Google Sheet Setup")
    print(f"{'─'*55}")
    print(f"  Keywords  : {total}")
    print(f"{'═'*55}\n")

    written = None

    if FORCE_BRAVE:
        written = upload_via_brave(rows)
    else:
        webapp = resolve_webapp()
        backend = choose_backend(webapp)

        if not backend:
            print(f"  No {WEBAPP_FILE} — using Brave auto-paste...\n")
            written = upload_via_brave(rows)
        else:
            try:
                if backend == "webapp":
                    print("  Uploading via Apps Script web app...")
                    written = upload_via_webapp(rows, webapp)
                else:
                    print("  Uploading via Google Sheets API...")
                    written = upload_via_api(rows)
            except Exception as e:
                err = str(e)
                print(f"  Web app failed: {err}")
                print("  Falling back to Brave auto-paste...\n")
                written = upload_via_brave(rows)

    print(f"\n  ✅ Done! {written} keywords uploaded.")
    print(f"  Columns: A=Keyword | B=Pinterest URL | C=Trends URL | D=Status\n")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit\n")
    print(f"  Next: python 2_pinterest_auto_scroll.py --5m\n")


if __name__ == "__main__":
    main()