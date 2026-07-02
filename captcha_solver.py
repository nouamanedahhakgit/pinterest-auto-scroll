"""
captcha_solver.py — Multi-service captcha solver with rotation + statistics.

Services (rotate automatically):
  1. AntiCaptcha  — api.anti-captcha.com
  2. CapSolver    — api.capsolver.com
  3. 2captcha     — 2captcha.com

Rotation strategy:
  - First 15 attempts: round-robin (5 each)
  - After 15: prefer best success-rate service (70% picks),
    rotate to 2nd-best every 10th attempt to keep stats fresh

Stop condition:
  - 20 consecutive failures across all services → raises CaptchaGiveUp
  - Writes warning to MySQL captcha_warnings table (dashboard shows it)

Stats: every attempt logged to MySQL captcha_stats table (dashboard reads it)

Usage in step 10:
    import captcha_solver
    try:
        token = captcha_solver.solve(url, html)  # None = unsolvable, str = token
    except captcha_solver.CaptchaGiveUp:
        # treat as permanently blocked

Playwright stealth (separate helper):
    ok, html = captcha_solver.try_playwright(url)
"""
from __future__ import annotations
import os
import re
import time
import threading
from pathlib import Path

import requests as _req

# ── API keys ───────────────────────────────────────────────────────────────────
SERVICES = [
    {"name": "anticaptcha", "key": "324ca205943a17b86d899adec13e75f9"},
    {"name": "capsolver",   "key": "CAP-41A58D1D093B3654C6A8AD9C3C637D349C14228308845CE1EB37FC85ADD27086"},
    {"name": "2captcha",    "key": "15cfbb19ea5532cdc39d1200d7287730"},
]

STOP_THRESHOLD = 20   # consecutive failures → CaptchaGiveUp

# ── In-memory stats (thread-safe) ──────────────────────────────────────────────
_lock = threading.Lock()
_stats: dict = {s["name"]: {"attempts": 0, "successes": 0, "failures": 0}
                for s in SERVICES}
_total_attempts = 0
_consecutive_failures = 0
_paused = False          # set True after STOP_THRESHOLD; reset by reset_pause()


class CaptchaGiveUp(Exception):
    """Raised when STOP_THRESHOLD consecutive failures reached. Treat site as blocked."""


# ── MySQL ──────────────────────────────────────────────────────────────────────
_mysql_conn = None
_mysql_lock = threading.Lock()

def _load_env() -> dict:
    env = {}
    p = Path(__file__).parent / ".env"
    if p.exists():
        for line in p.read_text(errors="replace").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

def _get_mysql():
    global _mysql_conn
    with _mysql_lock:
        try:
            if _mysql_conn and _mysql_conn.open:
                _mysql_conn.ping(reconnect=True)
                return _mysql_conn
        except Exception:
            _mysql_conn = None
        try:
            import pymysql
            env = _load_env()
            _mysql_conn = pymysql.connect(
                host=env["MYSQL_HOST"], port=int(env.get("MYSQL_PORT", 3306)),
                db=env["MYSQL_DB"], user=env["MYSQL_USER"],
                password=env["MYSQL_PASSWORD"],
                charset="utf8mb4", connect_timeout=5, autocommit=True,
            )
            c = _mysql_conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS captcha_stats (
                    id            INT AUTO_INCREMENT PRIMARY KEY,
                    ts            DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
                    service       VARCHAR(50),
                    captcha_type  VARCHAR(50),
                    url           VARCHAR(500),
                    success       TINYINT(1),
                    solve_time_ms INT,
                    error         TEXT,
                    INDEX idx_ts      (ts),
                    INDEX idx_service (service)
                ) CHARACTER SET utf8mb4
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS captcha_warnings (
                    id       INT AUTO_INCREMENT PRIMARY KEY,
                    ts       DATETIME(3) DEFAULT CURRENT_TIMESTAMP(3),
                    message  TEXT,
                    resolved TINYINT(1) DEFAULT 0,
                    INDEX idx_resolved (resolved)
                ) CHARACTER SET utf8mb4
            """)
        except Exception:
            _mysql_conn = None
        return _mysql_conn

def _log_attempt(service: str, captcha_type: str, url: str,
                 success: bool, solve_time_ms: int, error: str = "") -> None:
    try:
        conn = _get_mysql()
        if conn:
            conn.cursor().execute(
                "INSERT INTO captcha_stats "
                "(service, captcha_type, url, success, solve_time_ms, error) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (service, captcha_type, url[:500], int(success), solve_time_ms, (error or "")[:1000]),
            )
    except Exception:
        pass

def _write_warning(msg: str) -> None:
    print(f"[captcha_solver] ⚠ WARNING: {msg}")
    try:
        conn = _get_mysql()
        if conn:
            conn.cursor().execute(
                "INSERT INTO captcha_warnings (message) VALUES (%s)", (msg,)
            )
    except Exception:
        pass

def resolve_warning() -> None:
    """Call this to mark all active captcha warnings as resolved (e.g. after manual reset)."""
    try:
        conn = _get_mysql()
        if conn:
            conn.cursor().execute("UPDATE captcha_warnings SET resolved=1 WHERE resolved=0")
    except Exception:
        pass

def reset_pause() -> None:
    """Reset the pause state after manual intervention (run on HP to resume captcha solving)."""
    global _consecutive_failures, _paused
    with _lock:
        _consecutive_failures = 0
        _paused = False
    resolve_warning()
    print("[captcha_solver] Pause reset — captcha solving resumed.")

def get_stats() -> dict:
    """Return current in-memory stats (dashboard / debug use)."""
    with _lock:
        return {
            "services": {s["name"]: dict(_stats[s["name"]]) for s in SERVICES},
            "total_attempts":       _total_attempts,
            "consecutive_failures": _consecutive_failures,
            "stop_threshold":       STOP_THRESHOLD,
            "paused":               _paused,
        }

def get_balances() -> dict[str, float | None]:
    """Query current account balance from each service."""
    out = {}
    for svc in SERVICES:
        name, key = svc["name"], svc["key"]
        try:
            if name == "2captcha":
                r = _req.get(f"http://2captcha.com/res.php?key={key}&action=getbalance", timeout=8)
                out[name] = float(r.text.strip())
            elif name == "anticaptcha":
                r = _req.post("https://api.anti-captcha.com/getBalance",
                              json={"clientKey": key}, timeout=8)
                out[name] = r.json().get("balance")
            elif name == "capsolver":
                r = _req.post("https://api.capsolver.com/getBalance",
                              json={"clientKey": key}, timeout=8)
                out[name] = r.json().get("balance")
        except Exception:
            out[name] = None
    return out

# ── Service rotation ───────────────────────────────────────────────────────────
def _pick_service() -> dict:
    with _lock:
        if _total_attempts < 15:
            return SERVICES[_total_attempts % len(SERVICES)]
        rates = {s["name"]: _stats[s["name"]]["successes"] / max(_stats[s["name"]]["attempts"], 1)
                 for s in SERVICES}
        ranked = sorted(SERVICES, key=lambda s: rates[s["name"]], reverse=True)
        # every 10th attempt rotate to 2nd-best to refresh stats
        if _total_attempts % 10 == 0 and len(ranked) > 1:
            return ranked[1]
        return ranked[0]

# ── Captcha type + site-key detection ─────────────────────────────────────────
def detect_captcha(html: str) -> tuple[str, str]:
    """
    Returns (captcha_type, site_key).
    captcha_type: 'turnstile' | 'hcaptcha' | 'recaptcha_v2' | 'unknown'
    site_key: empty string when unknown
    """
    # Cloudflare Turnstile
    for pat in [
        r'cf-turnstile[^>]*data-sitekey=["\']([^"\']+)',
        r'data-sitekey=["\']([^"\']+)["\'][^>]*cf-turnstile',
        r'"sitekey"\s*:\s*"([^"]+)"[^}]*"turnstile"',
    ]:
        m = re.search(pat, html, re.I | re.S)
        if m:
            return "turnstile", m.group(1)

    # hCaptcha
    for pat in [
        r'h-captcha[^>]*data-sitekey=["\']([^"\']+)',
        r'data-sitekey=["\']([^"\']+)["\'][^>]*h-captcha',
        r'hcaptcha\.com/captcha[^"\']*["\'][^>]*data-sitekey=["\']([^"\']+)',
    ]:
        m = re.search(pat, html, re.I | re.S)
        if m:
            return "hcaptcha", m.group(1)

    # reCAPTCHA v2
    for pat in [
        r'g-recaptcha[^>]*data-sitekey=["\']([^"\']+)',
        r'grecaptcha\.render\([^,]+,\s*\{[^}]*["\']sitekey["\']\s*:\s*["\']([^"\']+)',
        r'data-sitekey=["\']([^"\']+)',
    ]:
        m = re.search(pat, html, re.I | re.S)
        if m:
            return "recaptcha_v2", m.group(1)

    return "unknown", ""

# ── Per-service solvers ────────────────────────────────────────────────────────
def _solve_anticaptcha(site_key: str, page_url: str, captcha_type: str, key: str) -> tuple[str | None, str]:
    type_map = {
        "recaptcha_v2": "NoCaptchaTaskProxyless",
        "hcaptcha":     "HCaptchaTaskProxyless",
        "turnstile":    "TurnstileTaskProxyless",
    }
    task = {
        "type":       type_map.get(captcha_type, "NoCaptchaTaskProxyless"),
        "websiteURL": page_url,
        "websiteKey": site_key,
    }
    try:
        r = _req.post("https://api.anti-captcha.com/createTask",
                      json={"clientKey": key, "task": task}, timeout=30)
        data = r.json()
        if data.get("errorId", 0) != 0:
            return None, data.get("errorDescription", "createTask error")
        task_id = data["taskId"]
    except Exception as e:
        return None, f"createTask exception: {e}"

    for _ in range(36):  # poll up to 3 min
        time.sleep(5)
        try:
            r2 = _req.post("https://api.anti-captcha.com/getTaskResult",
                           json={"clientKey": key, "taskId": task_id}, timeout=15)
            d2 = r2.json()
            if d2.get("errorId", 0) != 0:
                return None, d2.get("errorDescription", "getTaskResult error")
            if d2.get("status") == "ready":
                sol = d2.get("solution", {})
                token = sol.get("gRecaptchaResponse") or sol.get("token") or sol.get("cf_clearance")
                return token, ""
        except Exception:
            pass
    return None, "timeout (3 min)"

def _solve_capsolver(site_key: str, page_url: str, captcha_type: str, key: str) -> tuple[str | None, str]:
    type_map = {
        "recaptcha_v2": "ReCaptchaV2TaskProxyLess",
        "hcaptcha":     "HCaptchaTaskProxyLess",
        "turnstile":    "AntiTurnstileTaskProxyless",
    }
    task = {
        "type":       type_map.get(captcha_type, "ReCaptchaV2TaskProxyLess"),
        "websiteURL": page_url,
        "websiteKey": site_key,
    }
    try:
        r = _req.post("https://api.capsolver.com/createTask",
                      json={"clientKey": key, "task": task}, timeout=30)
        data = r.json()
        if data.get("errorId", 0) != 0:
            return None, data.get("errorDescription", "createTask error")
        task_id = data["taskId"]
    except Exception as e:
        return None, f"createTask exception: {e}"

    for _ in range(40):
        time.sleep(3)
        try:
            r2 = _req.post("https://api.capsolver.com/getTaskResult",
                           json={"clientKey": key, "taskId": task_id}, timeout=15)
            d2 = r2.json()
            if d2.get("errorId", 0) != 0:
                return None, d2.get("errorDescription", "getTaskResult error")
            if d2.get("status") == "ready":
                sol = d2.get("solution", {})
                token = sol.get("gRecaptchaResponse") or sol.get("token") or sol.get("cf_clearance")
                return token, ""
        except Exception:
            pass
    return None, "timeout (3 min)"

def _solve_2captcha(site_key: str, page_url: str, captcha_type: str, key: str) -> tuple[str | None, str]:
    method_map = {
        "recaptcha_v2": "userrecaptcha",
        "hcaptcha":     "hcaptcha",
        "turnstile":    "turnstile",
    }
    method = method_map.get(captcha_type, "userrecaptcha")
    payload: dict = {"key": key, "method": method, "pageurl": page_url, "json": 1}
    if captcha_type in ("recaptcha_v2", "turnstile"):
        payload["googlekey"] = site_key
    else:
        payload["sitekey"] = site_key

    try:
        r = _req.post("http://2captcha.com/in.php", data=payload, timeout=30)
        data = r.json()
        if data.get("status") != 1:
            return None, data.get("request", "submit error")
        task_id = data["request"]
    except Exception as e:
        return None, f"submit exception: {e}"

    for _ in range(36):
        time.sleep(5)
        try:
            r2 = _req.get(
                f"http://2captcha.com/res.php?key={key}&action=get&id={task_id}&json=1",
                timeout=15,
            )
            d2 = r2.json()
            if d2.get("status") == 1:
                return d2["request"], ""
            req_val = d2.get("request", "")
            if req_val not in ("CAPCHA_NOT_READY", "CAPTCHA_NOT_READY"):
                return None, req_val or "unknown poll error"
        except Exception:
            pass
    return None, "timeout (3 min)"

_SOLVERS = {
    "anticaptcha": _solve_anticaptcha,
    "capsolver":   _solve_capsolver,
    "2captcha":    _solve_2captcha,
}

# ── Playwright stealth (Cloudflare JS challenges, no API needed) ───────────────
_playwright_sem = threading.Semaphore(3)   # max 3 concurrent headless browsers

def try_playwright(url: str, inject_token: str | None = None,
                   captcha_type: str | None = None) -> tuple[bool, str]:
    """
    Try to fetch page using Playwright + playwright-stealth.
    If inject_token is given, injects it into the captcha form before getting HTML.
    Returns (success, html).
    success=False if page is still a challenge page after render.

    Requires:  pip install playwright playwright-stealth
               playwright install chromium
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        return False, ""

    try:
        from playwright_stealth import stealth_sync  # type: ignore
        _has_stealth = True
    except ImportError:
        _has_stealth = False

    with _playwright_sem:
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-setuid-sandbox",
                          "--disable-blink-features=AutomationControlled"],
                )
                ctx = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 720},
                )
                page = ctx.new_page()
                if _has_stealth:
                    stealth_sync(page)

                page.goto(url, timeout=30_000, wait_until="networkidle")
                page.wait_for_timeout(3_000)   # let Cloudflare JS run

                if inject_token and captcha_type:
                    _inject_token(page, inject_token, captcha_type)
                    page.wait_for_timeout(3_000)

                html = page.content()
                browser.close()

            still_blocked = _is_challenge_page(html)
            return not still_blocked, html
        except Exception:
            return False, ""

def _inject_token(page, token: str, captcha_type: str) -> None:
    """Inject solved token into the challenge page form."""
    try:
        if captcha_type == "recaptcha_v2":
            page.evaluate(
                f'document.getElementById("g-recaptcha-response").innerHTML = "{token}";'
            )
        elif captcha_type == "hcaptcha":
            page.evaluate(
                f'document.querySelector("[name=h-captcha-response]").value = "{token}";'
            )
        elif captcha_type == "turnstile":
            page.evaluate(
                f'document.querySelector("[name=cf-turnstile-response]").value = "{token}";'
            )
        page.evaluate("document.querySelector('form') && document.querySelector('form').submit();")
    except Exception:
        pass

def _is_challenge_page(html: str) -> bool:
    if not html:
        return True
    lw = html.lower()
    if len(html) > 50_000:
        return False   # real page
    return (
        ("captcha" in lw or "cloudflare" in lw) and
        any(p in lw for p in [
            "verify you are human", "checking your browser",
            "just a moment", "are you a robot", "security check",
        ])
    )

# ── Main public solve() ────────────────────────────────────────────────────────
def solve(url: str, html: str) -> str | None:
    """
    Attempt to solve the captcha on the page at `url`.
    Returns token string on success, None if no site-key found or unsolvable.
    Raises CaptchaGiveUp after STOP_THRESHOLD consecutive failures.
    """
    global _total_attempts, _consecutive_failures, _paused

    if _paused:
        raise CaptchaGiveUp(f"Captcha solving paused after {STOP_THRESHOLD} consecutive failures.")

    captcha_type, site_key = detect_captcha(html)
    if not site_key:
        print(f"[captcha_solver] No site-key in HTML for {url} — skipping API solve")
        return None

    svc   = _pick_service()
    fn    = _SOLVERS[svc["name"]]
    print(f"[captcha_solver] {svc['name']} ▶ solving {captcha_type} @ {url}")
    t0    = time.time()
    token, error = fn(site_key, url, captcha_type, svc["key"])
    elapsed_ms = int((time.time() - t0) * 1000)
    success = token is not None

    _log_attempt(svc["name"], captcha_type, url, success, elapsed_ms, error)

    with _lock:
        _total_attempts += 1
        _stats[svc["name"]]["attempts"] += 1
        if success:
            _stats[svc["name"]]["successes"] += 1
            _consecutive_failures = 0
            print(f"[captcha_solver] ✓ {svc['name']} solved in {elapsed_ms}ms")
        else:
            _stats[svc["name"]]["failures"] += 1
            _consecutive_failures += 1
            print(f"[captcha_solver] ✗ {svc['name']} failed: {error} "
                  f"({elapsed_ms}ms) — consec={_consecutive_failures}/{STOP_THRESHOLD}")
            if _consecutive_failures >= STOP_THRESHOLD:
                _paused = True
                msg = (
                    f"{STOP_THRESHOLD} consecutive captcha failures — captcha solving PAUSED. "
                    f"Last service: {svc['name']}, last error: {error}. "
                    f"Run captcha_solver.reset_pause() to resume."
                )
                _write_warning(msg)
                raise CaptchaGiveUp(msg)

    return token
