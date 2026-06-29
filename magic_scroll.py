"""
magic_scroll.py — multi-computer Pinterest auto-scrape orchestrator
====================================================================
Run this on AS MANY COMPUTERS AS YOU LIKE at the same time. Each one loops:

  1. CLAIM up to 10 keywords from the Google Sheet  → status "pending"
       (atomic via the Apps Script LockService — two PCs never grab the same one)
  2. SCROLL each keyword in Brave (SortPin saves the pins)
  3. BUILD the database  → python 4_build_database.py
       (saves ALL scraped data to sortpin.db + IMPORTANT_DATABASE/sortpin_mysql.sql)
  4. MARK those keywords "Done" on the Sheet
  5. CLEAR SortPin → python 6_clear_sortpin.py --yes
       (archives a backup into _SORTPIN_ARCHIVE/ first, then empties the extension)
  6. Repeat until no keywords are left.

REQUIREMENTS (one-time):
  • Re-deploy the Apps Script (google_sheets_apps_script.js, version 3 with
    claim/mark) — Extensions → Apps Script → paste → Deploy → Manage deployments
    → Edit → New version. google_sheets_webapp.json must hold the /exec URL.
  • The Sheet's column A = keywords, column D = Status (run step 1 once).
  • pip install selenium

Run:
  python magic_scroll.py             # 15 minutes per keyword, batch size 10 (default)
  python magic_scroll.py --2m        # 2 minutes per keyword
  python magic_scroll.py --10m       # 10 minutes per keyword
  python magic_scroll.py --batch 10  # 10 keywords claimed per cycle
  python magic_scroll.py --kw 10     # 10 keywords claimed per cycle (alternative)
  python magic_scroll.py --10kw      # 10 keywords claimed per cycle (alternative)
  python magic_scroll.py --10        # 10 keywords claimed per cycle (alternative)
  python magic_scroll.py --disk      # build DB from disk (needs ccl_chromium_reader)

PINNER MODE — same "magic" loop, but deep-scrapes PINNERS instead of keywords:
  Pass --pinner N and it switches entirely to this mode (no Google Sheet used —
  pinners + their status live in your local sortpin.db, same as step 7). Each
  cycle:
    1. CLAIM up to N pinners from sortpin.db (status != 'done', highest
       followers first) → marks them 'running' so an interrupted run
       resumes correctly. Add --blog-only to also apply step 7's
       "only scraped_websites.site_type contains 'blog'" filter.
    2. SCAN each pinner: open their saved-profile (captures boards), their
       created-profile (captures created pins), then every one of their
       boards, scrolling each till it stops loading new pins (step 7's logic).
    3. BUILD the database  → python 4_build_database.py
       (status columns are preserved across rebuilds — sortpin.db is NOT
       deleted in this mode, since it's what tracks pinner/board progress)
    4. CLEAR SortPin → python 6_clear_sortpin.py --yes
    5. Repeat until no pinners are left.

  python magic_scroll.py --pinner 10        # 10 pinners/cycle, deep-scrape each
  python magic_scroll.py --pinner=10        # same, alternative syntax
  python magic_scroll.py --10pinners        # same, alternative syntax
  python magic_scroll.py --pinner 10 --5m   # 5 min cap per board/profile (default 5)
  python magic_scroll.py --pinner 10 --blog-only   # only pinners step 7 classified as blogs
"""

import os, sys, time, socket, subprocess, re, json, datetime, platform, sqlite3

BASE       = os.path.dirname(os.path.abspath(__file__))
CDP_PORT   = 9222
BRAVE_PATH = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
PY         = sys.executable
LOG_PATH   = os.path.join(BASE, "magic_log.jsonl")
ENV_PATH   = os.path.join(BASE, ".env")

def load_env():
    env = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env

ENV_VARS = load_env()

def log_event(**ev):
    """Append one job-log line (JSONL). The step-5 server shows these live."""
    ev["ts"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ev["computer"] = platform.node()
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    except Exception:
        pass

# ── CLI options ───────────────────────────────────────────────────────────────
def _explicit_minutes():
    """Returns the --Nm value if the user passed one, else None (no fallback)."""
    for a in sys.argv[1:]:
        m = re.match(r"^--(\d+(?:\.\d+)?)m$", a)
        if m:
            return float(m.group(1))
    return None

def _minutes():
    explicit = _explicit_minutes()
    if explicit is not None:
        return explicit
    try:
        return float(ENV_VARS.get("DEFAULT_SCROLL_MINUTES", "15.0"))
    except ValueError:
        return 15.0

def _pinner_batch():
    """Returns the pinner-mode batch size if --pinner/--pinners was passed,
    else None (meaning: stay in keyword mode). Accepts:
      --pinner 10   --pinners 10   --pinner=10   --pinners=10
      --10pinner    --10pinners"""
    args = sys.argv[1:]
    for i, a in enumerate(args):
        if a in ("--pinner", "--pinners") and i + 1 < len(args):
            try:
                return int(args[i + 1].lstrip("="))
            except ValueError:
                pass
        m = re.match(r"^--pinners?=(\d+)$", a)
        if m:
            return int(m.group(1))
        m = re.match(r"^--(\d+)pinners?$", a)
        if m:
            return int(m.group(1))
    return None

def _batch():
    args = sys.argv[1:]
    # Check for --batch <N>
    for i, a in enumerate(args):
        if a == "--batch" and i + 1 < len(args):
            try: return int(args[i + 1])
            except ValueError: pass
    # Check for --kw <N>
    for i, a in enumerate(args):
        if a == "--kw" and i + 1 < len(args):
            try: return int(args[i + 1])
            except ValueError: pass
    # Check for --<N>kw (e.g. --10kw)
    for a in args:
        m = re.match(r"^--(\d+)kw$", a)
        if m:
            return int(m.group(1))
    # Check for --<N> where N is an integer (e.g. --10)
    for a in args:
        m = re.match(r"^--(\d+)$", a)
        if m:
            return int(m.group(1))
    try:
        return int(ENV_VARS.get("DEFAULT_BATCH_SIZE", "10"))
    except ValueError:
        return 10


MINUTES   = _minutes()
BATCH     = _batch()
PINNER_BATCH   = _pinner_batch()
PINNER_MAX_MIN = _explicit_minutes() if _explicit_minutes() is not None else 5.0  # step 7's default
BUILD_ARGS = ["4_build_database.py", "--no-csv"] + (["--disk"] if "--disk" in sys.argv[1:] else [])
PINNER_BUILD_ARGS_LIVE = ["4_build_database.py", "--no-clear", "--no-csv"] + (["--disk"] if "--disk" in sys.argv[1:] else [])

DB_PATH      = os.path.join(BASE, "sortpin.db")
PROFILE_SECS = 45   # seconds to scroll a saved-profile page (load its boards) — mirrors step 7
SAVE_EVERY   = 5    # build the DB after this many pinners within a cycle — mirrors step 7

# pagination-end detection: stop a keyword early when no new pins load
STALL_SECS  = 40           # "no more pins" when page height stops growing this long
SAMPLE_SECS = 6            # how often to check the page height
_HEIGHT_JS  = "return document.documentElement.scrollHeight || document.body.scrollHeight || 0;"

# ── Google Sheet (web app: claim / mark) ──────────────────────────────────────
def load_sheet_client():
    try:
        import google_sheets_client as gsc
        return gsc
    except Exception as e:
        print(f"  Could not load google_sheets_client.py — {e}")
        return None

def claim_keywords(gsc, cfg, n):
    data = gsc.post_webapp(cfg, {"action": "claim", "count": n})
    return data.get("claimed", [])

def mark_done(gsc, cfg, keywords):
    if not keywords:
        return
    gsc.post_webapp(cfg, {"action": "mark", "keywords": keywords, "status": "Done"})

# ── Brave + Selenium (self-contained scroll) ──────────────────────────────────
def _cdp_up():
    try:
        s = socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1); s.close()
        return True
    except OSError:
        return False

def ensure_brave():
    if _cdp_up():
        return True
    subprocess.run(["taskkill", "/F", "/IM", "brave.exe"], capture_output=True)
    time.sleep(2)
    subprocess.Popen([BRAVE_PATH, f"--remote-debugging-port={CDP_PORT}",
                      "--no-first-run", "--no-default-browser-check"])
    for _ in range(15):
        if _cdp_up():
            time.sleep(2); return True
        time.sleep(1)
    return False

def connect():
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    o = Options()
    o.add_experimental_option("debuggerAddress", f"127.0.0.1:{CDP_PORT}")
    d = webdriver.Chrome(options=o); d.implicitly_wait(3)
    return d

def open_url_tab(driver, url):
    existing = set(driver.window_handles)
    subprocess.Popen([BRAVE_PATH, url])
    for _ in range(15):
        time.sleep(1)
        new = set(driver.window_handles) - existing
        if new:
            driver.switch_to.window(next(iter(new))); return
    if driver.window_handles:
        driver.switch_to.window(driver.window_handles[-1])

def open_keyword_tab(driver, kw):
    url = "https://www.pinterest.com/search/pins/?q=" + kw.replace(" ", "+") + "&rs=typed"
    open_url_tab(driver, url)

def click_start(driver):
    """Click SortPin's Start Scroll button on the current Pinterest page."""
    from selenium.webdriver.common.by import By
    for _ in range(12):
        try:
            btns = driver.find_elements(By.TAG_NAME, "button")
        except Exception:
            time.sleep(2)
            continue

        for b in btns:
            try:
                if "stop scroll" in (b.text or "").lower():
                    return True   # already scrolling
            except Exception:
                pass

        for b in btns:
            try:
                if "start scroll" in (b.text or "").lower():
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", b)
                    time.sleep(0.2)
                    b.click()
                    time.sleep(1.5)
                    for x in driver.find_elements(By.TAG_NAME, "button"):
                        try:
                            if "stop scroll" in (x.text or "").lower():
                                return True
                        except Exception:
                            pass
            except Exception:
                pass
        time.sleep(2)
    return False

def close_pinterest_tabs(driver, base_tab):
    for h in list(driver.window_handles):
        if h == base_tab:
            continue
        if len(driver.window_handles) <= 1:
            break
        try:
            driver.switch_to.window(h)
            try: driver.execute_script("window.onbeforeunload=null;")
            except Exception: pass
            if "pinterest.com" in (driver.current_url or ""):
                driver.close()
        except Exception:
            continue
    try:
        if base_tab in driver.window_handles:
            driver.switch_to.window(base_tab)
        elif driver.window_handles:
            driver.switch_to.window(driver.window_handles[-1])
    except Exception:
        pass

def wait_pins(driver, max_secs):
    """Like step 2: keep scrolling, but STOP EARLY when the page stops loading
    new pins (height hasn't grown for STALL_SECS). `max_secs` is just a cap so a
    fast/endless page still moves on eventually. Returns why it stopped."""
    end_at = time.time() + max_secs
    last_h, last_grow, next_sample = 0, time.time(), time.time() + SAMPLE_SECS
    while time.time() < end_at:
        now = time.time()
        if now >= next_sample:
            next_sample = now + SAMPLE_SECS
            try: h = int(driver.execute_script(_HEIGHT_JS) or 0)
            except Exception: h = last_h
            if h > last_h + 50:                 # new pins loaded → keep going
                last_h, last_grow = h, now
            elif last_h > 0 and now - last_grow >= STALL_SECS:
                return "no more pins"           # exhausted → next keyword
        time.sleep(1)
    return "time cap"

_PINCOUNT_JS = ("return (document.querySelectorAll('div[data-test-id=\"pin\"]').length"
                " || document.querySelectorAll('div[role=\"listitem\"]').length || 0);")

def _count_pins(driver):
    try: return int(driver.execute_script(_PINCOUNT_JS) or 0)
    except Exception: return 0

def scroll_keyword(driver, kw, base_tab, minutes, cycle=0):
    print(f"    ▶ '{kw}' — up to {minutes:g} min")
    t0 = time.time()
    open_keyword_tab(driver, kw)
    time.sleep(6)
    started = click_start(driver)
    print("      " + ("SortPin scrolling" if started else "⚠ could not start SortPin (continuing)"))
    why = wait_pins(driver, int(minutes * 60))   # ends early when no new pins
    pins = _count_pins(driver)
    secs = int(time.time() - t0)
    print(f"      → moving on ({why}) — {secs}s, ~{pins} pins on page")
    close_pinterest_tabs(driver, base_tab)
    log_event(event="keyword", cycle=cycle, keyword=kw, seconds=secs,
              minutes=round(secs / 60, 1), pins=pins, why=why,
              started=bool(started))
    return {"keyword": kw, "seconds": secs, "pins": pins, "why": why}

# ── run the existing steps as subprocesses ────────────────────────────────────
def run_step(label, args):
    print(f"    → {label}")
    subprocess.run([PY] + args, cwd=BASE)

# ── pinner mode (deep-scrape profiles → boards → pins, looped like magic) ─────
def _extract_domain(url):
    if not url:
        return ""
    u = url.strip().lower()
    if not u.startswith("http"):
        u = "https://" + u
    try:
        from urllib.parse import urlparse
        h = urlparse(u).hostname or ""
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""

def _ensure_status_columns():
    """Make sure pinners/boards have a status column (mirrors step 7)."""
    if not os.path.exists(DB_PATH):
        return False
    con = sqlite3.connect(DB_PATH)
    for table in ("pinners", "boards"):
        try:
            con.execute(f"SELECT status FROM {table} LIMIT 1")
        except sqlite3.OperationalError:
            try:
                con.execute(f"ALTER TABLE {table} ADD COLUMN status TEXT DEFAULT 'not yet'")
                con.commit()
            except Exception:
                pass
    con.close()
    return True

def _set_pinner_status(username, status):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE pinners SET status=? WHERE username=?", (status, username))
    con.commit(); con.close()

def _set_board_status(board_id, status):
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE boards SET status=? WHERE id=?", (status, board_id))
    con.commit(); con.close()

def claim_pinner_batch(n, blog_only=False):
    """Pick up to n not-done pinners from sortpin.db (highest followers first),
    and mark them 'running' immediately so an interrupted run resumes them.
    With blog_only=True, applies step 7's "only scraped_websites.site_type
    contains 'blog'" filter — off by default here, since right now almost no
    pinner has been domain-classified yet, which would make this silently
    claim nothing. Pass --blog-only to opt into that stricter behavior."""
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    all_pinners = [dict(r) for r in con.execute(
        "SELECT username, full_name, follower_count, website_url, status FROM pinners "
        "WHERE status IS NULL OR status<>'done' ORDER BY follower_count DESC")]

    pinners = all_pinners
    if blog_only:
        site_types = {}
        try:
            for r in con.execute("SELECT domain, site_type FROM scraped_websites WHERE domain IS NOT NULL"):
                if r[0] and r[1]:
                    site_types[r[0].lower().strip()] = r[1]
        except sqlite3.OperationalError:
            pass
        if site_types:
            pinners = [p for p in all_pinners
                       if "blog" in site_types.get(_extract_domain(p.get("website_url", "")), "").lower()]

    batch = pinners[:n]
    for p in batch:
        con.execute("UPDATE pinners SET status='running' WHERE username=?", (p["username"],))
    con.commit(); con.close()
    return batch

def scrape_pinner(driver, base_tab, pinner_row, max_min, cycle=0):
    """Full deep-scrape of one pinner: saved profile (→ boards) → created
    profile (→ created pins) → every board, scrolled till it stops loading
    new pins. Mirrors 7_scrape_profiles.py's per-pinner logic."""
    u = pinner_row["username"]
    print(f"\n  ▶ @{u}  ({pinner_row.get('full_name') or ''}) — loading profile...")
    t0 = time.time()

    # 1a) saved profile → captures boards + stats
    print("    → loading saved profile (_saved/)...")
    open_url_tab(driver, f"https://www.pinterest.com/{u}/_saved/")
    time.sleep(5); click_start(driver)
    why_saved = wait_pins(driver, PROFILE_SECS * 2)   # generous cap to load boards
    close_pinterest_tabs(driver, base_tab)
    run_step("import profile boards", PINNER_BUILD_ARGS_LIVE)

    # 1b) created profile → captures created pins (like magic)
    print("    → loading created profile (_created/)...")
    open_url_tab(driver, f"https://www.pinterest.com/{u}/_created/")
    time.sleep(5); click_start(driver)
    why_created = wait_pins(driver, int(max_min * 60))
    close_pinterest_tabs(driver, base_tab)
    print(f"      → finished created pins ({why_created})")
    run_step("import profile pins", PINNER_BUILD_ARGS_LIVE)

    # boards known for this pinner now (pre-existing + newly discovered)
    con = sqlite3.connect(DB_PATH); con.row_factory = sqlite3.Row
    boards = [dict(r) for r in con.execute(
        "SELECT id, url, name, status FROM boards "
        "WHERE owner_username=? AND url IS NOT NULL AND url<>''", (u,))]
    con.close()
    print(f"    found {len(boards)} board(s) to process")

    # 2) each board till the end
    board_results = []
    for b in boards:
        if b.get("status") == "done":
            continue
        print(f"    • board '{(b.get('name') or '')[:34]}' …", end="", flush=True)
        _set_board_status(b["id"], "running")

        board_url = b["url"]
        if board_url.startswith("/"):
            board_url = "https://www.pinterest.com" + board_url
        elif not board_url.startswith("http"):
            board_url = "https://www.pinterest.com/" + board_url

        open_url_tab(driver, board_url); time.sleep(5)
        click_start(driver)
        why = wait_pins(driver, int(max_min * 60))
        close_pinterest_tabs(driver, base_tab)

        _set_board_status(b["id"], "done")
        board_results.append({"board": b.get("name"), "why": why})
        print(f" {why}")

    # finalize pinner status: done only if every board is done
    con = sqlite3.connect(DB_PATH)
    remaining = con.execute(
        "SELECT COUNT(*) FROM boards WHERE owner_username=? AND status<>'done'", (u,)
    ).fetchone()[0]
    con.execute("UPDATE pinners SET status=? WHERE username=?",
                ("done" if remaining == 0 else "not yet", u))
    con.commit(); con.close()

    secs = int(time.time() - t0)
    print(f"  → @{u} finished in {secs}s "
          f"({'done' if remaining == 0 else f'{remaining} board(s) remaining'})")
    log_event(event="pinner", cycle=cycle, pinner=u, seconds=secs,
              boards=len(board_results), why_saved=why_saved, why_created=why_created,
              remaining_boards=remaining)
    return {"pinner": u, "seconds": secs, "boards": len(board_results), "remaining": remaining}

def pinner_mode(batch_size, max_min, blog_only=False):
    print(f"\n{'='*62}\n  MAGIC SCROLL (pinners) — {batch_size} pinners/cycle · "
          f"{max_min:g} min/board" + (" · blog-only" if blog_only else "") + f"\n{'='*62}")
    if not os.path.exists(DB_PATH):
        print("  sortpin.db not found — run step 2 + step 4 first.\n")
        sys.exit(1)
    _ensure_status_columns()

    cycle = 0
    while True:
        cycle += 1
        print(f"\n── cycle {cycle}: claiming up to {batch_size} pinners ──")
        batch = claim_pinner_batch(batch_size, blog_only=blog_only)
        if not batch:
            print("  No pinners left to claim (all Done). Finished. 🎉")
            break
        names = ", ".join("@" + p["username"] for p in batch)
        print("  claimed (running):", names)
        log_event(event="claim_pinners", cycle=cycle,
                  pinners=[p["username"] for p in batch], count=len(batch))

        if not ensure_brave():
            print("  Could not start Brave — claimed pinners stay 'running'; re-run to retry.")
            break
        driver = connect()
        base_tab = driver.current_window_handle
        cyc_start = time.time()
        done_in_cycle = []
        for idx, p in enumerate(batch):
            if not _cdp_up():
                print(f"  Brave lost — reconnecting before @{p['username']}…")
                if not ensure_brave():
                    print("  Could not restart Brave. Stopping cycle early.")
                    break
                driver = connect()
                base_tab = driver.current_window_handle

            scrape_pinner(driver, base_tab, p, max_min, cycle=cycle)
            done_in_cycle.append(p["username"])

            if (idx + 1) % SAVE_EVERY == 0:
                run_step("periodic save", PINNER_BUILD_ARGS_LIVE)

        try:
            driver.quit()
        except Exception:
            pass

        if done_in_cycle:
            # Build + auto-clear the extension (default mode clears unless --no-clear).
            # sortpin.db itself is NEVER deleted here — it's what tracks pinner/board
            # progress, and 4_build_database.py already preserves status across rebuilds.
            run_step(f"build database ({len(done_in_cycle)} pinner(s))", BUILD_ARGS)
            run_step("clear SortPin", ["6_clear_sortpin.py", "--yes"])

        con = sqlite3.connect(DB_PATH)
        done_total  = con.execute("SELECT COUNT(*) FROM pinners WHERE status='done'").fetchone()[0]
        total       = con.execute("SELECT COUNT(*) FROM pinners").fetchone()[0]
        con.close()
        print(f"  [Magic Status] Pinners done: {done_total}/{total}")
        log_event(event="pinner_cycle_done", cycle=cycle, pinners=done_in_cycle,
                  seconds=int(time.time() - cyc_start), done_total=done_total, total=total)

    print(f"\n  Magic scroll (pinners) finished after {cycle - 1} full cycle(s).\n")

# ── main loop ─────────────────────────────────────────────────────────────────
def reset_pending_keywords(gsc, cfg):
    print("\n=== Reset Pending Keywords → Not Yet ===")
    try:
        data = gsc.post_webapp(cfg, {"action": "reset_pending_keywords"})
        n = data.get("reset", 0)
        print(f"  Reset {n} keyword(s) from 'pending' → 'Not Yet'.")
    except Exception as e:
        print(f"  Failed: {e}")

def main():
    if PINNER_BATCH:
        pinner_mode(PINNER_BATCH, PINNER_MAX_MIN, blog_only=("--blog-only" in sys.argv[1:]))
        return

    if "--reset-keywords" in sys.argv:
        gsc = load_sheet_client()
        cfg = gsc.resolve_webapp() if gsc else None
        if cfg:
            reset_pending_keywords(gsc, cfg)
        else:
            print("  No google_sheets_webapp.json found.")
        sys.exit(0)

    print(f"\n{'='*62}\n  MAGIC SCROLL — {BATCH} keywords/cycle · {MINUTES:g} min each\n{'='*62}")
    gsc = load_sheet_client()
    cfg = gsc.resolve_webapp() if gsc else None
    if not cfg:
        print("  No Google Sheet web app (google_sheets_webapp.json) — cannot claim.\n")
        sys.exit(1)

    cycle = 0
    while True:
        cycle += 1
        print(f"\n── cycle {cycle}: claiming up to {BATCH} keywords ──")
        try:
            claimed = claim_keywords(gsc, cfg, BATCH)
        except Exception as e:
            print(f"  claim failed — {e}\n  (Re-deploy the Apps Script v3 with claim/mark.)")
            break
        if not claimed:
            print("  No keywords left to claim (all Done/pending). Finished. 🎉")
            break
        kws = [c["keyword"] for c in claimed]
        print("  claimed (pending):", ", ".join(kws))
        log_event(event="claim", cycle=cycle, keywords=kws, count=len(kws))

        # scroll every claimed keyword — keep Brave open the whole cycle
        if not ensure_brave():
            print("  Could not start Brave — releasing keywords is not automatic; "
                  "set them back to 'Not Yet' on the sheet if needed.")
            break
        driver = connect()
        base_tab = driver.current_window_handle
        cyc_start = time.time()
        scrolled_kws = []
        for idx, kw in enumerate(kws):
            # Reconnect only if Brave died mid-cycle
            if not _cdp_up():
                print(f"  Brave lost — reconnecting before '{kw}'…")
                if not ensure_brave():
                    print("  Could not restart Brave. Stopping cycle early.")
                    break
                driver = connect()
                base_tab = driver.current_window_handle

            scroll_keyword(driver, kw, base_tab, MINUTES, cycle=cycle)
            scrolled_kws.append(kw)
            # Just close Pinterest tab — keep Brave alive for next keyword
            close_pinterest_tabs(driver, base_tab)

        # ── Build DB once for the whole cycle (not once per keyword) ──────────
        try:
            driver.quit()
        except Exception:
            pass

        if scrolled_kws:
            # step 4 handles: read disk → build DB → sync to MySQL → clear SortPin extension
            run_step(f"build database ({len(scrolled_kws)} keywords)", BUILD_ARGS)

            # read stats from local DB after build
            db_path = os.path.join(BASE, "sortpin.db")
            db_pins_after = db_created_after = db_saved_after = 0
            if os.path.exists(db_path):
                try:
                    con = sqlite3.connect(db_path)
                    db_pins_after    = con.execute("SELECT COUNT(*) FROM pins").fetchone()[0]
                    db_created_after = con.execute("SELECT COUNT(*) FROM pins WHERE pin_type='created'").fetchone()[0]
                    db_saved_after   = con.execute("SELECT COUNT(*) FROM pins WHERE pin_type='saved'").fetchone()[0]
                    con.close()
                except Exception:
                    pass

        print(f"  [Magic Status] Total Pins this cycle: {db_pins_after} (created: {db_created_after}, saved: {db_saved_after})")

        log_event(event="cycle_done", cycle=cycle, keywords=kws,
                  seconds=int(time.time() - cyc_start),
                  total_pins=db_pins_after,
                  created_pins=db_created_after,
                  saved_pins=db_saved_after,
                  new_pins=0)

    print(f"\n  Magic scroll finished after {cycle - 1} full cycle(s).\n")

if __name__ == "__main__":
    main()
