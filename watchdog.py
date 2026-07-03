#!/usr/bin/env python3
"""
watchdog.py — run any pinterest-scan script with auto-restart, auto git pull/push,
and problem reporting.

HOW IT WORKS (two-machine setup):
  leno  = where Claude runs and edits code (this machine, git push after each fix)
  hp    = where scripts run (this watchdog auto git pull before each restart)

  1. Script crashes on hp → watchdog writes watchdog_report.txt
  2. User pastes report to Claude on leno → Claude fixes code + git push
  3. Watchdog on hp detects new git commit (polls every 3 min) → git pull + restart
  4. Fixed version runs automatically — no manual intervention needed

Usage:
    python watchdog.py python 13_scan-website-interface-by-ia.py --slow --retry-failed
    python watchdog.py python 14_download_blog_pin_links.py
    python watchdog.py python magic_scroll.py --2m
    python watchdog.py python 10_domain_quick_scrape_api.py

Watchdog flags (before the script command):
    --max-restarts N     give up after N crashes (default 20)
    --stuck-minutes N    kill if no output for N min (default 10)
    --git-poll N         check for new git commits every N minutes (default 3)
    --no-git             disable all git operations
"""

import subprocess, sys, os, re, time, threading
from datetime import datetime
from collections import defaultdict

BASE        = os.path.dirname(os.path.abspath(__file__))
REPORT_PATH = os.path.join(BASE, "watchdog_report.txt")
LOG_PATH    = os.path.join(BASE, "watchdog_run.log")

# ─── Known error patterns ─────────────────────────────────────────────────────
PATTERNS = [
    ("unreachable",  r"Failed \(unreachable\)",
     "warn",     "sites unreachable — internet issue; run --retry-failed after connection improves"),
    ("timeout",      r"Failed \(timeout\)",
     "warn",     "fetch timeouts — slow connection or slow servers"),
    ("stuck_task",   r"\[([5-9]\d{2,}|\d{4,})\.\d+s\]",
     "warn",     "task(s) took 500s+ — redirect-loop bug; update to latest code"),
    ("sheet_fail",   r"Sheet batch write failed",
     "warn",     "Google Sheets unreachable — data saved to DB; Sheet catches up when internet recovers"),
    ("ai_error",     r"Failed \(AI error:",
     "warn",     "OpenRouter unreachable — internet blip; retry later"),
    ("db_fail",      r"write failed for|db_local.*fail|db_mysql.*fail",
     "error",    "DB write error — check disk space and .env MySQL credentials"),
    ("mysql_gone",   r"MySQL server has gone away|Lost connection to MySQL",
     "error",    "MySQL disconnected — script reconnects; if repeated, check MySQL server"),
    ("python_crash", r"Traceback \(most recent call last\)",
     "critical", "Python exception — script crashed, watchdog will restart"),
    ("memory",       r"MemoryError",
     "critical", "out of memory — reduce --workers"),
    ("keyboard",     r"KeyboardInterrupt",
     "info",     "stopped manually (Ctrl+C)"),
]
_COMPILED = [(k, re.compile(p, re.I), sev, tip) for k, p, sev, tip in PATTERNS]
_BACKOFF  = [30, 60, 120, 300, 600]   # restart delays (last repeats)


# ─── Git helpers ──────────────────────────────────────────────────────────────

def _git(args, timeout=30):
    """Run a git command, return (stdout, stderr, returncode). Never raises."""
    try:
        r = subprocess.run(
            ["git"] + args, cwd=BASE,
            capture_output=True, text=True, timeout=timeout,
        )
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except Exception as e:
        return "", str(e), 1


def _current_commit():
    out, _, rc = _git(["rev-parse", "HEAD"])
    return out if rc == 0 else None


def _fetch_and_check():
    """Returns True if there are new commits on the remote branch."""
    _git(["fetch", "--quiet"], timeout=20)
    local,  _, rc1 = _git(["rev-parse", "HEAD"])
    remote, _, rc2 = _git(["rev-parse", "@{u}"])
    if rc1 != 0 or rc2 != 0:
        return False
    return local != remote


def _pull():
    """git pull --ff-only. Returns (changed: bool, summary: str)."""
    before = _current_commit()
    out, err, rc = _git(["pull", "--ff-only", "--quiet"])
    after = _current_commit()
    if rc != 0:
        return False, f"git pull failed: {err or out}"
    changed = before != after and after is not None
    if changed:
        log_out, _, _ = _git(["log", "--oneline", f"{before}..{after}"])
        return True, log_out or "(no log)"
    return False, "already up to date"


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _fmt(seconds):
    h, r = divmod(int(seconds), 3600)
    m, s = divmod(r, 60)
    if h: return f"{h}h {m}m"
    if m: return f"{m}m {s}s"
    return f"{s}s"


def _write_report(cmd, n_runs, counts, tail, start_ts):
    elapsed = (datetime.now() - start_ts).total_seconds()
    out = [
        "=" * 62,
        "  WATCHDOG REPORT — paste to Claude on leno to diagnose",
        "=" * 62,
        f"Script   : {' '.join(cmd)}",
        f"Started  : {start_ts:%Y-%m-%d %H:%M:%S}",
        f"Duration : {_fmt(elapsed)}",
        f"Runs     : {n_runs}",
        "",
        "--- Detected problems ---",
    ]
    any_found = False
    for key, _, sev, tip in PATTERNS:
        n = counts.get(key, 0)
        if n:
            any_found = True
            out.append(f"  {key:<16} {n:>6}x  [{sev}]")
            out.append(f"              → {tip}")
    if not any_found:
        out.append("  (none — crash had no matching pattern)")

    if tail:
        out += ["", "--- Last 40 output lines before crash ---"]
        out += [f"  {ln}" for ln in tail[-40:]]

    out += ["", "=" * 62, ""]
    text = "\n".join(out)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"\n  📋 Report saved → {REPORT_PATH}")


# ─── Core loop ────────────────────────────────────────────────────────────────

def run(cmd, max_restarts=20, stuck_minutes=10, git_poll_minutes=3, use_git=True):
    start_ts       = datetime.now()
    counts         = defaultdict(int)
    tail           = []
    n_runs         = 0
    # Shared flag: git poller sets this when new commits are detected so the
    # main loop can restart the script even without a crash.
    git_update_evt = threading.Event()
    proc_ref       = [None]   # mutable cell so threads can access current proc

    print(f"  🐕 watchdog | {' '.join(cmd)}")
    print(f"     max-restarts={max_restarts}  stuck={stuck_minutes}min  "
          f"git-poll={git_poll_minutes}min  git={'on' if use_git else 'off'}")
    print(f"     log    → {LOG_PATH}")
    print(f"     report → {REPORT_PATH}")
    print()

    # ── Git poller thread ──────────────────────────────────────────────────────
    def _git_poller():
        while True:
            time.sleep(git_poll_minutes * 60)
            if not use_git:
                continue
            try:
                if _fetch_and_check():
                    print(f"\n  🔄 New git commits detected — will restart script with latest code.")
                    git_update_evt.set()
                    # Kill the running process so the main loop picks up the event
                    p = proc_ref[0]
                    if p and p.poll() is None:
                        p.terminate()
            except Exception:
                pass

    threading.Thread(target=_git_poller, daemon=True).start()

    with open(LOG_PATH, "a", encoding="utf-8") as logf:
        logf.write(f"\n{'='*60}\n{datetime.now():%Y-%m-%d %H:%M:%S}  {' '.join(cmd)}\n{'='*60}\n")

        for attempt in range(max_restarts + 1):
            n_runs += 1
            git_update_evt.clear()

            # ── Wait / git pull before restart ────────────────────────────────
            if attempt > 0:
                wait = _BACKOFF[min(attempt - 1, len(_BACKOFF) - 1)]
                print(f"\n  ♻️  Restart {attempt}/{max_restarts} in {wait}s …")
                try:
                    time.sleep(wait)
                except KeyboardInterrupt:
                    print("\n  Stopped.")
                    break

            if use_git:
                print("  ⬇️  git pull …", end=" ", flush=True)
                changed, summary = _pull()
                if changed:
                    print(f"✅ new commits:\n     {summary}")
                else:
                    print(summary)

            # ── Launch script ─────────────────────────────────────────────────
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                cwd=BASE,
            )
            proc_ref[0] = proc
            last_output = [time.time()]

            # Stuck-detector
            def _stuck(p=proc, limit=stuck_minutes * 60):
                while p.poll() is None:
                    if time.time() - last_output[0] > limit:
                        print(f"\n  ⚠️  No output for {stuck_minutes} min — killing stuck process.")
                        try: p.kill()
                        except Exception: pass
                        return
                    time.sleep(15)
            threading.Thread(target=_stuck, daemon=True).start()

            # Progress summary every 5 min
            last_summary = [time.time()]

            try:
                for raw in proc.stdout:
                    line = raw.rstrip()
                    last_output[0] = time.time()

                    print(line)
                    logf.write(line + "\n"); logf.flush()
                    tail.append(line)
                    if len(tail) > 200: tail.pop(0)

                    for key, regex, sev, tip in _COMPILED:
                        if regex.search(line):
                            counts[key] += 1
                            if sev == "critical" and counts[key] == 1:
                                print(f"  ‼️  CRITICAL — {tip}")

                    now = time.time()
                    if now - last_summary[0] > 300:
                        last_summary[0] = now
                        issues = {k: v for k, v in counts.items() if k != "keyboard" and v}
                        print(f"\n  📊 {_fmt((datetime.now()-start_ts).total_seconds())} elapsed | "
                              f"run #{n_runs} | issues: {issues or 'none'}\n")

            except KeyboardInterrupt:
                proc.terminate()
                counts["keyboard"] += 1
                print("\n  Stopped by user.")
                break

            proc.wait()
            rc = proc.returncode

            # Git update triggered the kill → restart immediately (no crash count)
            if git_update_evt.is_set():
                print(f"  🔄 Restarting with new code from git …")
                attempt -= 1   # don't count this as a crash restart
                continue

            if rc == 0:
                print(f"\n  ✅ Script finished cleanly "
                      f"({_fmt((datetime.now()-start_ts).total_seconds())} total).")
                break

            if counts.get("keyboard"):
                break

            print(f"\n  💥 Exit code {rc} (run #{n_runs}, "
                  f"{_fmt((datetime.now()-start_ts).total_seconds())} elapsed).")

            if attempt >= max_restarts:
                print(f"  Max restarts ({max_restarts}) reached — giving up.")
                break

            _write_report(cmd, n_runs, counts, tail, start_ts)

    _write_report(cmd, n_runs, counts, tail, start_ts)


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    args          = list(sys.argv[1:])
    max_restarts  = 20
    stuck_minutes = 10
    git_poll      = 3
    use_git       = True

    # Consume watchdog flags (everything before the actual command)
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--max-restarts"  and i + 1 < len(args): max_restarts  = int(args[i+1]); i += 2
        elif a == "--stuck-minutes" and i + 1 < len(args): stuck_minutes = int(args[i+1]); i += 2
        elif a == "--git-poll"    and i + 1 < len(args): git_poll      = int(args[i+1]); i += 2
        elif a == "--no-git":  use_git = False; i += 1
        elif a.startswith("--max-restarts="):  max_restarts  = int(a.split("=",1)[1]); i += 1
        elif a.startswith("--stuck-minutes="): stuck_minutes = int(a.split("=",1)[1]); i += 1
        elif a.startswith("--git-poll="):      git_poll      = int(a.split("=",1)[1]); i += 1
        else: break   # first non-watchdog arg = start of the wrapped command

    cmd = args[i:]
    if not cmd:
        print(__doc__)
        sys.exit(0)

    run(cmd, max_restarts=max_restarts, stuck_minutes=stuck_minutes,
        git_poll_minutes=git_poll, use_git=use_git)


if __name__ == "__main__":
    main()
