"""
STEP 3 — Sync progress.json statuses to Google Sheet (column D)
================================================================
Updates column D in the background — no browser, no clipboard, no clicking.

HOW TO USE:
  python 3_sync_to_sheet.py

ONE-TIME SETUP (pick one):
  A) google_service_account.json  — Google Cloud service account (silent)
  B) google_credentials.json    — OAuth (browser once, then silent)
  C) google_sheets_webapp.json    — Apps Script web app (easiest, see SETUP_MSG)


  .
"""

import os
import sys
import json

try:
    import requests
except ImportError:
    requests = None

# ═══ CONFIG ══════════════════════════════════════════════════════════════════
KEYWORDS_FILE  = "keywords.txt"
PROGRESS_FILE  = "progress.json"
SPREADSHEET_ID = "1ZaIcgG7E2ChZYtUr9UZP78bfO-YNMArlbWZk_71E_VE"
STATUS_DONE    = "Done"
STATUS_NOT_YET = "Not Yet"

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SA_FILE      = "google_service_account.json"
OAUTH_FILE   = "google_credentials.json"
TOKEN_FILE   = "google_token.json"
WEBAPP_FILE  = "google_sheets_webapp.json"
APPS_SCRIPT  = "google_sheets_apps_script.js"
# ═════════════════════════════════════════════════════════════════════════════

BASE = os.path.dirname(os.path.abspath(__file__))

SETUP_MSG = f"""
{'═'*62}
  ONE-TIME SETUP — Google Sheets API (background sync)
{'─'*62}

  Service account (recommended — fully silent after setup):

    1. https://console.cloud.google.com/ → create/select a project
    2. APIs & Services → Library → enable "Google Sheets API"
    3. Credentials → Create → Service account → Create key → JSON
    4. Save the JSON file here as:
         {os.path.join(BASE, SA_FILE)}
    5. Open that JSON → copy "client_email"
    6. Share your Google Sheet with that email → Editor
    7. Run again:  python 3_sync_to_sheet.py

  OAuth alternative (browser opens once on first run only):

    1. Same GCP project → Credentials → OAuth client ID → Desktop app
    2. Download JSON → save as {OAUTH_FILE} in this folder
    3. Run:  python 3_sync_to_sheet.py

  Apps Script web app (easiest — no Google Cloud):

    1. Open your Google Sheet → Extensions → Apps Script
    2. Paste the code from:  {os.path.join(BASE, APPS_SCRIPT)}
    3. Deploy → New deployment → Web app
         Execute as: Me  |  Who has access: Anyone
    4. Copy the Web App URL into {WEBAPP_FILE}:
         {{"url": "https://script.google.com/macros/s/AKfycbx2Qck5m_eckYLuo-CM6N1PdA1MsZgxFjHCI8UROrp2_U5WqMeEUGlsNjTzyE12Svpg/exec", "secret": "pinterest-scan-2026"}}
    5. Run:  python 3_sync_to_sheet.py

{'═'*62}
"""


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


def status_for(progress, kw):
    if progress.get(kw, {}).get("status") == "done":
        return STATUS_DONE
    return STATUS_NOT_YET


def get_gspread_client():
    try:
        import gspread
        from google.oauth2.service_account import Credentials as SA_Creds
    except ImportError:
        print("  Missing packages. Run:")
        print("  pip install gspread google-api-python-client google-auth-oauthlib")
        sys.exit(1)

    sa_path = os.path.join(BASE, SA_FILE)
    if os.path.exists(sa_path):
        creds = SA_Creds.from_service_account_file(sa_path, scopes=SCOPES)
        return gspread.authorize(creds), "service account"

    oauth_path = os.path.join(BASE, OAUTH_FILE)
    token_path = os.path.join(BASE, TOKEN_FILE)
    if os.path.exists(oauth_path):
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials as UserCreds
        from google_auth_oauthlib.flow import InstalledAppFlow

        creds = None
        if os.path.exists(token_path):
            creds = UserCreds.from_authorized_user_file(token_path, SCOPES)
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                print("  First OAuth login — browser will open once...")
                flow = InstalledAppFlow.from_client_secrets_file(oauth_path, SCOPES)
                creds = flow.run_local_server(port=0, prompt="consent")
            with open(token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())
        return gspread.authorize(creds), "oauth"

    return None, None


def load_webapp_config():
    path = os.path.join(BASE, WEBAPP_FILE)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if cfg.get("url"):
        return cfg
    return None


def sync_via_webapp(statuses, cfg):
    if requests is None:
        print("  requests package required for web app sync.")
        sys.exit(1)

    flat = [row[0] for row in statuses]
    payload = {
        "secret": cfg.get("secret", "pinterest-scan-2026"),
        "statuses": flat,
    }
    url = cfg["url"]
    r = requests.post(url, json=payload, timeout=120, allow_redirects=True)
    r.raise_for_status()

    try:
        data = r.json()
    except ValueError:
        snippet = (r.text or "")[:200].replace("\n", " ")
        raise RuntimeError(
            "Web app did not return JSON. Check deployment is Web app (not API), "
            f"access = Anyone, and URL ends with /exec. Response: {snippet}"
        ) from None

    if not data.get("ok"):
        raise RuntimeError(data.get("error", "web app returned error"))
    return data.get("count", len(flat))


def webapp_from_argv():
    """Allow: python 3_sync_to_sheet.py --webapp-url https://script.google.com/.../exec"""
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--webapp-url" and i + 1 < len(args):
            return {"url": args[i + 1], "secret": "pinterest-scan-2026"}
        if arg.startswith("--webapp-url="):
            return {"url": arg.split("=", 1)[1], "secret": "pinterest-scan-2026"}
    return None


def build_status_column(keywords, progress, sheet_keywords):
    """Align statuses to sheet rows (column A), fall back to keywords.txt order."""
    if sheet_keywords:
        statuses = []
        progress_lc = {k.lower(): v for k, v in progress.items()}
        keywords_lc = {k.lower(): k for k in keywords}
        for raw in sheet_keywords:
            kw = (raw or "").strip()
            if not kw:
                statuses.append([""])
                continue
            key = keywords_lc.get(kw.lower(), kw)
            if progress.get(key, progress_lc.get(kw.lower(), {})).get("status") == "done":
                statuses.append([STATUS_DONE])
            else:
                statuses.append([STATUS_NOT_YET])
        return statuses

    return [[status_for(progress, kw)] for kw in keywords]


def sync_to_sheet(ws, statuses):
    if not statuses:
        return 0
    end_row = len(statuses) + 1
    ws.update(f"D2:D{end_row}", statuses, value_input_option="RAW")
    return len(statuses)


def main():
    keywords = load_keywords()
    progress = load_progress()
    total    = len(keywords)

    done_count = sum(1 for kw in keywords if progress.get(kw, {}).get("status") == "done")
    not_yet    = total - done_count

    print(f"\n{'═'*55}")
    print(f"  Google Sheet Status Sync  (background API)")
    print(f"{'─'*55}")
    print(f"  Keywords  : {total}")
    print(f"  Done      : {done_count}")
    print(f"  Not Yet   : {not_yet}")
    print(f"{'═'*55}\n")

    webapp       = load_webapp_config() or webapp_from_argv()
    sa_exists    = os.path.exists(os.path.join(BASE, SA_FILE))
    oauth_exists = os.path.exists(os.path.join(BASE, OAUTH_FILE))
    webapp_path  = os.path.join(BASE, WEBAPP_FILE)

    if not sa_exists and not oauth_exists and not webapp:
        print(f"  ✗ Missing {WEBAPP_FILE} in this folder.\n")
        print(SETUP_MSG)
        sys.exit(1)

    if webapp and not os.path.exists(webapp_path) and not (sa_exists or oauth_exists):
        print(f"  Using web app URL (save to {WEBAPP_FILE} to skip --webapp-url next time)")

    if webapp and not sa_exists and not oauth_exists:
        statuses = build_status_column(keywords, progress, sheet_keywords=None)
        print("  Syncing via Apps Script web app (background)...")
        try:
            written = sync_via_webapp(statuses, webapp)
        except Exception as e:
            print(f"  Web app failed: {e}")
            print("  Falling back to Brave auto-paste (column D)...\n")
            from google_sheets_brave import upload_status_column
            flat = [row[0] for row in statuses]
            upload_status_column(flat)
            written = len(flat)
    else:
        print("  Connecting to Google Sheets API...")
        try:
            client = get_gspread_client()
            if client[0] is None:
                print(SETUP_MSG)
                sys.exit(1)
            gc, auth_mode = client
            sh = gc.open_by_key(SPREADSHEET_ID)
            ws = sh.sheet1
        except Exception as e:
            err = str(e)
            print(f"\n  ✗ Could not access sheet: {err}\n")
            if "403" in err or "permission" in err.lower():
                print("  → Share the sheet with your service account email (Editor).")
            elif "404" in err:
                print("  → Check SPREADSHEET_ID in this script.")
            sys.exit(1)

        print(f"  Auth: {auth_mode}")
        print(f"  Sheet: {sh.title} / tab: {ws.title}")

        if (ws.acell("D1").value or "").strip().lower() != "status":
            ws.update("D1", [["Status"]], value_input_option="RAW")

        col_a = ws.col_values(1)
        if col_a and col_a[0].strip().lower() in ("keyword", "keywords"):
            sheet_keywords = col_a[1:]
        else:
            sheet_keywords = col_a[1:] if len(col_a) > 1 else []

        statuses = build_status_column(keywords, progress, sheet_keywords)
        if len(sheet_keywords) < len(keywords) and not sheet_keywords:
            print(f"  Sheet column A empty — writing {len(keywords)} rows by keywords.txt order")
        elif len(sheet_keywords) != len(keywords):
            print(f"  Sheet rows: {len(sheet_keywords)} | keywords.txt: {len(keywords)}")
            print(f"  Matching by keyword text in column A")

        print(f"  Updating column D ({len(statuses)} rows)...")
        try:
            written = sync_to_sheet(ws, statuses)
        except Exception as e:
            print(f"\n  ✗ Update failed: {e}\n")
            sys.exit(1)

    print(f"\n  ✅ Done! {written} statuses written to column D.")
    print(f"     Done      = {done_count}")
    print(f"     Not Yet   = {not_yet}\n")
    print(f"  Sheet: https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit\n")


if __name__ == "__main__":
    main()