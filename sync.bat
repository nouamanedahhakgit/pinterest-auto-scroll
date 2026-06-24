@echo off
REM ====================================================================
REM  sync.bat - one-click GitHub sync for the Pinterest Scan project
REM  Safe for use across MULTIPLE computers:
REM    1) clears any stale git lock files
REM    2) stages everything (your new CSV exports, code, etc.)
REM    3) commits with a timestamp + this computer's name
REM    4) PULLS other computers' changes first (rebase) so nothing is lost
REM    5) pushes everything back up
REM  Just double-click this file (or run:  sync.bat)
REM ====================================================================
cd /d "%~dp0"
echo.
echo ===== Pinterest Scan - GitHub sync =====
echo.

REM 1) remove leftover lock files (harmless if none exist)
if exist ".git\index.lock" del /q ".git\index.lock"
if exist ".git\HEAD.lock"  del /q ".git\HEAD.lock"
del /q ".git\*.lock" >nul 2>&1

REM 2) show what changed before staging
echo -- status before sync --
git status -sb
echo.

REM 3) stage
echo -- staging changes --
git add -A

REM 4) commit (may say "nothing to commit" — that is normal if already synced)
echo -- committing --
git commit -m "auto-sync %DATE% %TIME% (%COMPUTERNAME%)"
if errorlevel 1 (
    echo   ^(no new changes to commit — files may already be on GitHub^)
) else (
    echo -- files in this commit --
    git show --stat --oneline -1
)
echo.

REM 5) pull other computers' work, replaying our commit on top
echo -- pulling other computers' changes (rebase) --
git pull --rebase --autostash

REM 6) push
echo -- pushing --
git push

echo.
echo -- key data files tracked in git --
git ls-files progress.json keywords.txt keywords_google_sheet.tsv sortpin.db sortpin_data.json 2>nul
echo.
echo -- ignored by .gitignore (not synced) --
git status --ignored -s 2>nul | findstr /I "^!!"
echo.
echo ===== Done =====
echo If push showed a small size ^(e.g. 344 bytes^), only .gitignore or tiny
echo edits changed — progress.json etc. were committed in an earlier sync.
echo.
echo If you see a "CONFLICT" message above, open the file it names,
echo fix it, then run:  git add .  ^&  git rebase --continue  ^&  git push
echo.
pause