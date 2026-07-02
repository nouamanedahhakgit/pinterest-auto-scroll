@echo off
REM ============================================================
REM  run_fast.bat — launch multiple parallel workers
REM  Each step10 instance: 20 workers, claims 20 sites at a time
REM  Each step14 instance: 80 workers downloading blog pin links
REM  MySQL pool per instance = 8 connections
REM  3 x step10 = 60 concurrent site scans, 24 MySQL connections
REM  Edit STEP10_INSTANCES / STEP14_INSTANCES to taste.
REM  Run from the pinterest-auto-scroll folder.
REM ============================================================

set STEP10_INSTANCES=3
set STEP14_INSTANCES=2

echo Starting %STEP10_INSTANCES% instance(s) of step 10...
for /L %%i in (1,1,%STEP10_INSTANCES%) do (
    start "Step10-%%i" cmd /k "python 10_domain_quick_scrape_api.py && pause"
)

echo Starting %STEP14_INSTANCES% instance(s) of step 14...
for /L %%i in (1,1,%STEP14_INSTANCES%) do (
    start "Step14-%%i" cmd /k "python 14_download_blog_pin_links.py && pause"
)

echo All instances launched. Close any window to stop that instance.
