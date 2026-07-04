#!/usr/bin/env python3
"""
auto_fixer.py — runs on leno, watches watchdog_report.txt for new crashes,
sends the crash + code to Claude API, applies the fix, git pushes.

The hp watchdog detects the push within 3 min and restarts with fixed code.
Fully automatic — no human needed once this is running.

SETUP (one time):
    1. Add to .env:  ANTHROPIC_API_KEY=sk-ant-...
    2. pip install anthropic --break-system-packages
    3. python auto_fixer.py

FLOW:
    hp crashes → watchdog_report.txt updated
    → auto_fixer detects change (polls every 20s)
    → if internet-only issue: skip (watchdog restarts by itself)
    → if real code bug: ask Claude API for fix
    → apply fix to file + git commit + git push
    → hp watchdog detects new commit → git pull → restart ✅
"""

import os, sys, time, subprocess, re, shutil
from datetime import datetime

BASE        = os.path.dirname(os.path.abspath(__file__))
REPORT_PATH = os.path.join(BASE, "watchdog_report.txt")
ENV_PATH    = os.path.join(BASE, ".env")
LOG_PATH    = os.path.join(BASE, "auto_fixer.log")

# Map script filename → path relative to BASE
KNOWN_SCRIPTS = {
    "13_scan-website-interface-by-ia.py",
    "14_download_blog_pin_links.py",
    "magic_scroll.py",
    "10_domain_quick_scrape_api.py",
    "dashboard.py",
    "google_sheets_client.py",
    "watchdog.py",
    "7_scrape_profiles.py",
    "4_build_database.py",
}

# Patterns that mean "internet blip, not a code bug" → no API call needed
INTERNET_PATTERNS = {"unreachable", "timeout", "sheet_fail", "ai_error"}
# Patterns that mean there IS a code bug
BUG_PATTERNS      = {"python_crash", "memory", "db_fail", "mysql_gone", "stuck_killed"}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _log(msg):
    ts  = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_env():
    env = {}
    if not os.path.exists(ENV_PATH):
        return env
    with open(ENV_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _git(args):
    exe = shutil.which("git") or "git"
    r   = subprocess.run([exe] + args, cwd=BASE, capture_output=True, text=True)
    return r.returncode == 0, (r.stdout + r.stderr).strip()


# ─── Report parsing ───────────────────────────────────────────────────────────

def parse_report(text):
    """Return (script_name, pattern_counts_dict, last_output_lines)."""
    script  = None
    counts  = {}
    output  = []

    m = re.search(r"Script\s*:\s*python\s+(\S+\.py)", text)
    if m:
        script = m.group(1)

    for m in re.finditer(r"(\w+)\s+(\d+)x\s+\[(\w+)\]", text):
        counts[m.group(1)] = int(m.group(2))

    in_out = False
    for line in text.splitlines():
        if "Last 40 output lines" in line:
            in_out = True
            continue
        if in_out:
            if line.startswith("==="):
                break
            output.append(line[2:] if line.startswith("  ") else line)

    return script, counts, output


def needs_code_fix(counts):
    """True only when a real code bug is present (not just internet noise)."""
    has_bug      = bool(set(counts) & BUG_PATTERNS)
    only_internet = set(counts).issubset(INTERNET_PATTERNS | {"keyboard"})
    return has_bug and not only_internet


# ─── Claude API call ──────────────────────────────────────────────────────────

PATCH_FORMAT = """
Return your fix as one or more PATCH blocks (enough surrounding lines to be unique):

<<<OLD
exact lines from the current file to replace
>>>OLD
<<<NEW
replacement lines
>>>NEW

If there are multiple changes, use multiple PATCH blocks.
If no code fix is needed (internet/network issue only), reply: NO_FIX_NEEDED
"""

FULL_FILE_FORMAT = """
Return the COMPLETE fixed file wrapped in triple backticks:
```python
... entire file ...
```
If no code fix is needed (internet/network issue only), reply: NO_FIX_NEEDED
"""

def call_claude(api_key, model, report_text, script_name, file_content):
    """
    Returns (fixed_content_or_None, status)
    status: 'fixed' | 'no_fix_needed' | 'parse_failed' | 'api_error'
    """
    import anthropic

    # For long files use patch format (saves tokens); short files → full file
    long_file    = len(file_content.splitlines()) > 400
    return_format = PATCH_FORMAT if long_file else FULL_FILE_FORMAT

    prompt = f"""You are an expert Python developer. A script in a Pinterest automation
project just crashed. Fix the bug.

CRASH REPORT:
{report_text}

CURRENT FILE ({script_name}):
```python
{file_content}
```

{return_format}
Only fix the actual bug. Do not refactor unrelated code.
Add a short comment on the changed line(s) explaining the fix."""

    try:
        client  = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=16000,
            messages=[{"role": "user", "content": prompt}],
        )
        response = message.content[0].text.strip()
    except Exception as e:
        return None, f"api_error: {e}"

    if re.search(r"\bNO_FIX_NEEDED\b", response) and "```" not in response:
        return None, "no_fix_needed"

    if long_file:
        return _apply_patches(file_content, response), "fixed"
    else:
        m = re.search(r"```(?:python)?\s*(.*?)```", response, re.S)
        if m:
            return m.group(1).strip(), "fixed"
        return None, "parse_failed"


def _apply_patches(original, response):
    """Apply <<<OLD / >>>OLD / <<<NEW / >>>NEW patch blocks to original text."""
    result  = original
    patches = re.findall(r"<<<OLD\s*(.*?)>>>OLD\s*<<<NEW\s*(.*?)>>>NEW", response, re.S)
    if not patches:
        return None
    for old, new in patches:
        old = old.strip("\n")
        new = new.strip("\n")
        if old not in result:
            _log(f"  ⚠️  Patch OLD block not found in file — skipping this patch.")
            continue
        result = result.replace(old, new, 1)
    return result if result != original else None


# ─── Apply fix + git push ─────────────────────────────────────────────────────

def apply_and_push(script_name, fixed_content, patterns):
    file_path   = os.path.join(BASE, script_name)
    backup_path = file_path + ".bak"

    # Backup original
    shutil.copy2(file_path, backup_path)
    _log(f"  💾 Backup → {script_name}.bak")

    # Write fix
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(fixed_content)
    _log(f"  📝 {script_name} updated.")

    # Git
    ok, out = _git(["add", file_path])
    if not ok:
        _log(f"  ❌ git add failed: {out}")
        return False

    bug_keys = [k for k in patterns if k in BUG_PATTERNS]
    msg = f"auto-fix({script_name}): {', '.join(bug_keys) or 'crash'}"
    ok, out = _git(["commit", "-m", msg])
    if not ok:
        if "nothing to commit" in out:
            _log("  (nothing new to commit)")
            return True
        _log(f"  ❌ git commit failed: {out}")
        return False

    ok, out = _git(["push"])
    if not ok:
        _log(f"  ❌ git push failed — run 'git push' manually on leno: {out}")
        return False

    _log(f"  🚀 Pushed! hp watchdog will detect within 3 min and restart.")
    return True


# ─── Main loop ────────────────────────────────────────────────────────────────

def main():
    env     = load_env()
    api_key = env.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("❌  ANTHROPIC_API_KEY not set in .env")
        print("    Add this line to .env:")
        print("    ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    try:
        import anthropic
    except ImportError:
        print("  Installing anthropic…")
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "anthropic",
             "--break-system-packages", "-q"],
            check=True,
        )
        import anthropic  # noqa

    model = env.get("CLAUDE_FIXER_MODEL", "claude-sonnet-4-6")
    _log(f"🤖 auto_fixer started | model={model}")
    _log(f"   watching {REPORT_PATH}")
    _log(f"   Ctrl+C to stop\n")

    last_mtime     = None
    last_processed = None   # mtime of last report we acted on

    while True:
        try:
            if not os.path.exists(REPORT_PATH):
                time.sleep(20)
                continue

            mtime = os.path.getmtime(REPORT_PATH)

            if mtime == last_mtime or mtime == last_processed:
                time.sleep(20)
                continue

            last_mtime = mtime
            ts = datetime.fromtimestamp(mtime).strftime("%H:%M:%S")
            _log(f"\n📋 New watchdog report @ {ts}")

            # Wait 10s — watchdog may still be writing
            time.sleep(10)

            with open(REPORT_PATH, encoding="utf-8") as f:
                report_text = f.read()

            script_name, counts, last_output = parse_report(report_text)
            _log(f"   Script  : {script_name}")
            _log(f"   Patterns: {counts}")

            last_processed = mtime

            if not script_name or script_name not in KNOWN_SCRIPTS:
                _log(f"   ⚠️  Unknown script '{script_name}' — skipping auto-fix.")
                continue

            if not needs_code_fix(counts):
                _log("   → Internet/network issue only. No code fix needed.")
                _log("     Watchdog will restart the script automatically.")
                continue

            _log("   → Code bug detected! Calling Claude API…")

            file_path = os.path.join(BASE, script_name)
            if not os.path.exists(file_path):
                _log(f"   ❌ File not found: {file_path}")
                continue

            with open(file_path, encoding="utf-8") as f:
                file_content = f.read()

            fixed_content, status = call_claude(
                api_key, model, report_text, script_name, file_content
            )

            if status == "no_fix_needed":
                _log("   → Claude: no code fix needed (network issue).")
            elif status.startswith("api_error"):
                _log(f"   ❌ Claude API error: {status}")
            elif status == "parse_failed" or fixed_content is None:
                _log("   ❌ Could not parse Claude's fix — check report manually.")
            else:
                _log("   ✅ Claude returned a fix — applying…")
                apply_and_push(script_name, fixed_content, counts)

        except KeyboardInterrupt:
            _log("\nStopped.")
            break
        except Exception as e:
            _log(f"[error] {e}")
            time.sleep(30)

        time.sleep(20)


if __name__ == "__main__":
    main()
