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

# ── main ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
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
