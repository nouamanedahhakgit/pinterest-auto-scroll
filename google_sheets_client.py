"""Shared Google Sheets background upload/sync helpers."""

import json
import os
import sys

try:
    import requests
except ImportError:
    requests = None

BASE = os.path.dirname(os.path.abspath(__file__))

SPREADSHEET_ID = "1ZaIcgG7E2ChZYtUr9UZP78bfO-YNMArlbWZk_71E_VE"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
SA_FILE = "google_service_account.json"
OAUTH_FILE = "google_credentials.json"
TOKEN_FILE = "google_token.json"
WEBAPP_FILE = "google_sheets_webapp.json"
APPS_SCRIPT = "google_sheets_apps_script.js"

SETUP_MSG = f"""
{'═'*62}
  ONE-TIME SETUP — Google Sheets (background, no browser)
{'─'*62}

  Apps Script web app (recommended — you already use this for step 3):

    1. Open Google Sheet → Extensions → Apps Script
    2. Replace ALL code with:  {os.path.join(BASE, APPS_SCRIPT)}
    3. Deploy → Manage deployments → Edit (pencil) → New version → Deploy
         Execute as: Me  |  Who has access: Anyone
    4. Ensure {WEBAPP_FILE} contains your Web App URL
    5. Run again

  Service account alternative:

    1. Google Cloud → enable Sheets API → service account JSON key
    2. Save as {os.path.join(BASE, SA_FILE)}
    3. Share sheet with service account email (Editor)

{'═'*62}
"""


def load_webapp_config():
    path = os.path.join(BASE, WEBAPP_FILE)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg if cfg.get("url") else None


def webapp_from_argv():
    args = sys.argv[1:]
    for i, arg in enumerate(args):
        if arg == "--webapp-url" and i + 1 < len(args):
            return {"url": args[i + 1], "secret": "pinterest-scan-2026"}
        if arg.startswith("--webapp-url="):
            return {"url": arg.split("=", 1)[1], "secret": "pinterest-scan-2026"}
    return None


def resolve_webapp():
    return load_webapp_config() or webapp_from_argv()


def has_api_credentials():
    return (
        os.path.exists(os.path.join(BASE, SA_FILE))
        or os.path.exists(os.path.join(BASE, OAUTH_FILE))
    )


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


def _parse_webapp_response(response):
    """Return JSON dict or raise with a readable Apps Script error."""
    if requests is None:
        raise RuntimeError("requests package required")

    text = response.text or ""
    ctype = (response.headers.get("content-type") or "").lower()

    if "application/json" in ctype or text.strip().startswith("{"):
        try:
            return response.json()
        except ValueError:
            pass

    if "doPost" in text or "doGet" in text:
        raise RuntimeError(
            "Apps Script error: doPost not found. Paste the FULL "
            f"{APPS_SCRIPT} (must include function doPost), Save, redeploy Web app."
        )
    if response.status_code == 404:
        raise RuntimeError("Web app URL not found (404). Update google_sheets_webapp.json with the /exec URL from Deploy.")

    snippet = text[:160].replace("\n", " ")
    raise RuntimeError(f"Web app returned HTML, not JSON: {snippet}")


def post_webapp_raw(cfg, payload, timeout=None):
    if requests is None:
        print("  requests package required.")
        sys.exit(1)

    # (connect_timeout, read_timeout): 10s to connect, 60s to receive the response.
    # Apps Script can be slow on large payloads but should never take >60s to respond.
    # Old default was 180s flat — that blocked every failed write for 3 full minutes.
    if timeout is None:
        timeout = (10, 60)

    payload = dict(payload)
    payload.setdefault("secret", cfg.get("secret", "pinterest-scan-2026"))
    r = requests.post(cfg["url"], json=payload, timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return _parse_webapp_response(r)


def probe_webapp(cfg):
    """Return 'setup', 'columns', 'sync-only', or raise on broken deployment."""
    if requests is None:
        return "sync-only"

    try:
        r = requests.get(cfg["url"], timeout=30, allow_redirects=True)
        data = _parse_webapp_response(r)
        if data.get("version", 0) >= 2:
            return "columns"
    except Exception:
        pass

    try:
        data = post_webapp_raw(
            cfg,
            {"action": "setup", "rows": [["__setup_probe__", "a", "b", "c"]]},
            timeout=30,
        )
        if data.get("action") == "setup":
            return "setup"
    except Exception:
        pass

    try:
        data = post_webapp_raw(
            cfg,
            {"column": 1, "statuses": ["__col_probe__"]},
            timeout=30,
        )
        if data.get("column") == 1:
            return "columns"
    except Exception:
        pass

    try:
        data = post_webapp_raw(cfg, {"statuses": ["Done"]}, timeout=30)
        if data.get("ok") and "column" not in data and data.get("action") != "column":
            return "sync-only"
    except Exception as exc:
        raise RuntimeError(str(exc)) from exc

    return "sync-only"


def post_webapp(cfg, payload, timeout=None):
    # timeout=None → post_webapp_raw uses its own default (10s connect, 60s read).
    # Pass an explicit value (e.g. timeout=180) only for known-slow calls like get_websites.
    data = post_webapp_raw(cfg, payload, timeout=timeout)
    if not data.get("ok"):
        raise RuntimeError(data.get("error", "web app error"))
    return data


def choose_backend(webapp):
    if has_api_credentials():
        return "api"
    if webapp:
        return "webapp"
    return None


def get_existing_sheet_statuses(webapp):
    # Try webapp first
    if webapp:
        try:
            data = post_webapp(webapp, {"action": "get_keywords"})
            if data.get("ok"):
                return {item["keyword"].lower(): item["status"] for item in data.get("keywords", []) if item.get("keyword")}
        except Exception:
            pass
            
    # Try gspread API fallback
    try:
        gc, _ = get_gspread_client()
        if gc:
            sh = gc.open_by_key(SPREADSHEET_ID)
            ws = sh.sheet1
            # Fetch all values
            vals = ws.get_all_values()
            if len(vals) > 1:
                return {row[0].lower().strip(): row[3].strip() for row in vals[1:] if len(row) >= 4 and row[0]}
    except Exception:
        pass
        
    return {}