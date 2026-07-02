"""Upload/sync Google Sheet via Brave + Selenium (no Apps Script needed)."""

import os
import subprocess
import socket
import time

import pyperclip
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains

BRAVE_PATH = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
CDP_PORT = 9222
SHEET_URL = (
    "https://docs.google.com/spreadsheets/d/"
    "1ZaIcgG7E2ChZYtUr9UZP78bfO-YNMArlbWZk_71E_VE/edit"
)


def is_cdp_available():
    try:
        s = socket.create_connection(("127.0.0.1", CDP_PORT), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def launch_brave():
    if is_cdp_available():
        return
    subprocess.run(["taskkill", "/F", "/IM", "brave.exe"], capture_output=True)
    time.sleep(2)
    subprocess.Popen([
        BRAVE_PATH,
        f"--remote-debugging-port={CDP_PORT}",
        "--no-first-run",
        "--no-default-browser-check",
    ])
    for _ in range(15):
        if is_cdp_available():
            return
        time.sleep(1)


def connect_selenium():
    opts = Options()
    opts.binary_location = BRAVE_PATH
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{CDP_PORT}")
    driver = webdriver.Chrome(options=opts)
    driver.implicitly_wait(2)
    return driver


def _open_sheet_tab(driver):
    handles_before = set(driver.window_handles)
    driver.execute_script(f"window.open({SHEET_URL!r}, '_blank');")
    time.sleep(1)
    new_handles = [h for h in driver.window_handles if h not in handles_before]
    driver.switch_to.window(new_handles[-1] if new_handles else driver.window_handles[-1])
    driver.get(SHEET_URL)
    print("  Waiting for Google Sheet to load (12s)...")
    time.sleep(12)


def _focus_and_paste(driver, clipboard_text, navigate_d2=False):
    pyperclip.copy(clipboard_text)
    body = driver.find_element(By.TAG_NAME, "body")
    body.click()
    time.sleep(0.4)

    actions = ActionChains(driver)
    actions.key_down(Keys.CONTROL).send_keys(Keys.HOME).key_up(Keys.CONTROL).perform()
    time.sleep(0.3)

    if navigate_d2:
        for _ in range(3):
            actions = ActionChains(driver)
            actions.send_keys(Keys.ARROW_RIGHT).perform()
            time.sleep(0.1)
        actions = ActionChains(driver)
        actions.send_keys(Keys.ARROW_DOWN).perform()
        time.sleep(0.2)

    actions = ActionChains(driver)
    actions.key_down(Keys.CONTROL).send_keys("v").key_up(Keys.CONTROL).perform()
    time.sleep(4)


def upload_tsv_full(tsv_text):
    """Paste full TSV grid starting at A1 (Keyword | URLs | Status)."""
    launch_brave()
    driver = connect_selenium()
    try:
        _open_sheet_tab(driver)
        _focus_and_paste(driver, tsv_text, navigate_d2=False)
    finally:
        pass  # keep Brave open for user
    return True


def upload_status_column(status_lines):
    """Paste one status per row into column D starting at D2."""
    launch_brave()
    driver = connect_selenium()
    try:
        _open_sheet_tab(driver)
        _focus_and_paste(driver, "\n".join(status_lines), navigate_d2=True)
    finally:
        pass
    return True