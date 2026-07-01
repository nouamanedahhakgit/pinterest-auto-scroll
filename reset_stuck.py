"""
reset_stuck.py — reset all stuck/crashed statuses in Sheet + MySQL at once.
Run from HP:  python reset_stuck.py

What it resets:
  Sheet websites tab:
    - "Running"                        → "not yet"   (crashed mid-scan)
    - "Failed (database is locked)"    → "not yet"   (retry)
    - "Failed (MySQL Connection...)"   → "not yet"   (retry)
  Sheet keywords tab:
    - "pending"                        → "Not Yet"   (claimed but never finished)
  MySQL scraped_websites:
    - status = 'running'               → NULL        (crashed mid-scan)
"""

import json
import os
import sys
from pathlib import Path

BASE = Path(__file__).parent

# ── load config ────────────────────────────────────────────────────────────────
def load_env():
    env = {}
    for line in (BASE / ".env").read_text(errors="replace").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip()
    return env

def load_webapp():
    return json.loads((BASE / "google_sheets_webapp.json").read_text())

# ── Sheet resets ───────────────────────────────────────────────────────────────
def reset_sheet_websites():
    """Call Apps Script reset_running_websites — resets Running → not yet."""
    try:
        import requests
        cfg = load_webapp()
        print("Sheet: resetting Running websites...")
        r = requests.post(cfg["url"],
                          json={"action": "reset_running_websites", "secret": cfg.get("secret","")},
                          timeout=60)
        data = r.json()
        print(f"  → {data}")
    except Exception as e:
        print(f"  Sheet websites reset failed: {e}")

def reset_sheet_pending_keywords():
    """Call Apps Script reset_pending_keywords — resets pending → Not Yet."""
    try:
        import requests
        cfg = load_webapp()
        print("Sheet: resetting pending keywords...")
        r = requests.post(cfg["url"],
                          json={"action": "reset_pending_keywords", "secret": cfg.get("secret","")},
                          timeout=60)
        data = r.json()
        print(f"  → {data}")
    except Exception as e:
        print(f"  Sheet keywords reset failed: {e}")

def reset_sheet_failed_retryable():
    """Directly update Sheet rows whose scrapped = retryable failure → not yet."""
    RETRYABLE = [
        "Failed (database is locked)",
        "Failed (MySQL Connection not available.)",
    ]
    try:
        import requests
        cfg = load_webapp()
        print("Sheet: loading all websites to find retryable failures...")
        r = requests.post(cfg["url"],
                          json={"action": "get_websites", "secret": cfg.get("secret","")},
                          timeout=60)
        websites = r.json().get("websites", [])
        to_reset = [w for w in websites if (w.get("scrapped") or "").strip() in RETRYABLE]
        print(f"  Found {len(to_reset)} retryable failed rows")
        if not to_reset:
            return

        # batch update them to "not yet"
        updates = [{"website": w["website"], "updates": {"scrapped": "not yet"}}
                   for w in to_reset if w.get("website")]
        # send in chunks of 50
        chunk = 50
        for i in range(0, len(updates), chunk):
            batch = updates[i:i+chunk]
            r2 = requests.post(cfg["url"],
                               json={"action": "batch_update_websites",
                                     "secret": cfg.get("secret",""),
                                     "updates": batch},
                               timeout=60)
            print(f"  batch {i//chunk+1}: {r2.json()}")
    except Exception as e:
        print(f"  Sheet failed-retry reset failed: {e}")

# ── MySQL reset ────────────────────────────────────────────────────────────────
def reset_mysql():
    try:
        import pymysql
        env = load_env()
        conn = pymysql.connect(
            host=env["MYSQL_HOST"],
            port=int(env.get("MYSQL_PORT", 3306)),
            db=env["MYSQL_DB"],
            user=env["MYSQL_USER"],
            password=env["MYSQL_PASSWORD"],
            charset="utf8mb4",
            connect_timeout=10,
            autocommit=True,
        )
        c = conn.cursor()

        # reset running websites
        c.execute("UPDATE scraped_websites SET status=NULL WHERE status='running'")
        print(f"MySQL: reset {c.rowcount} running websites → NULL")


        # reset running keywords (if keywords table exists)
        try:
            c.execute("UPDATE keywords SET status='Not Yet' WHERE status='pending'")
            print(f"MySQL: reset {c.rowcount} pending keywords → Not Yet")
        except Exception:
            pass  # keywords table may not exist

        conn.close()
    except Exception as e:
        print(f"MySQL reset failed: {e}")

def reset_mysql_blocked():
    """Reset blocked websites in MySQL → NULL so step 10 retries them."""
    try:
        import pymysql
        env = load_env()
        conn = pymysql.connect(
            host=env["MYSQL_HOST"], port=int(env.get("MYSQL_PORT", 3306)),
            db=env["MYSQL_DB"], user=env["MYSQL_USER"], password=env["MYSQL_PASSWORD"],
            charset="utf8mb4", connect_timeout=10, autocommit=True,
        )
        c = conn.cursor()
        c.execute("UPDATE scraped_websites SET status=NULL WHERE status='blocked'")
        print(f"MySQL: reset {c.rowcount} blocked websites → NULL")
        conn.close()
    except Exception as e:
        print(f"MySQL blocked reset failed: {e}")

def reset_sheet_blocked():
    """Reset Blocked (...) rows in Sheet websites tab → not yet."""
    try:
        import requests
        cfg = load_webapp()
        print("Sheet: loading all websites to find blocked rows...")
        r = requests.post(cfg["url"],
                          json={"action": "get_websites", "secret": cfg.get("secret", "")},
                          timeout=60)
        websites = r.json().get("websites", [])
        to_reset = [w for w in websites
                    if (w.get("scrapped") or "").strip().startswith("Blocked")]
        print(f"  Found {len(to_reset)} blocked rows in Sheet")
        if not to_reset:
            return
        updates = [{"website": w["website"], "updates": {"scrapped": "not yet"}}
                   for w in to_reset if w.get("website")]
        for i in range(0, len(updates), 50):
            batch = updates[i:i+50]
            r2 = requests.post(cfg["url"],
                               json={"action": "batch_update_websites",
                                     "secret": cfg.get("secret", ""),
                                     "updates": batch},
                               timeout=60)
            print(f"  batch {i//50+1}: {r2.json()}")
    except Exception as e:
        print(f"  Sheet blocked reset failed: {e}")

# ── captcha reset ──────────────────────────────────────────────────────────────
def reset_captcha():
    """Resolve active captcha warnings in MySQL + reset in-memory pause state."""
    try:
        import pymysql
        env = load_env()
        conn = pymysql.connect(
            host=env["MYSQL_HOST"], port=int(env.get("MYSQL_PORT", 3306)),
            db=env["MYSQL_DB"], user=env["MYSQL_USER"], password=env["MYSQL_PASSWORD"],
            charset="utf8mb4", connect_timeout=10, autocommit=True,
        )
        c = conn.cursor()
        c.execute("UPDATE captcha_warnings SET resolved=1 WHERE resolved=0")
        print(f"MySQL: resolved {c.rowcount} captcha warning(s)")
        conn.close()
    except Exception as ex:
        print(f"  captcha warning reset failed: {ex}")
    try:
        import captcha_solver
        captcha_solver.reset_pause()
    except Exception:
        print("  captcha_solver not imported (restart step 10 to apply in-process)")

# ── main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys

    if "--retry-blocked" in sys.argv:
        print("=" * 55)
        print("  Reset blocked websites → retry with captcha solver")
        print("=" * 55)
        reset_mysql_blocked()
        print()
        reset_sheet_blocked()
        print()
        print("Done. Step 10 will retry all previously-blocked sites.")
        sys.exit(0)

    if "--reset-captcha" in sys.argv:
        print("=" * 55)
        print("  Reset captcha solver pause + warnings")
        print("=" * 55)
        reset_captcha()
        print("\nDone. Captcha solving resumes on next blocked site.")
        sys.exit(0)

    print("=" * 55)
    print("  Reset stuck/crashed statuses — Sheet + MySQL")
    print("=" * 55)
    print()

    reset_mysql()
    print()
    reset_sheet_websites()
    print()
    reset_sheet_pending_keywords()
    print()
    reset_sheet_failed_retryable()
    print()
    print("Done. You can now restart the bots.")
