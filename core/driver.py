# ─────────────────────────────────────────────────────────────────────────────
# core/driver.py
# Shared Chrome WebDriver factory and browser utility helpers.
# Used by all crawler scrapers — no platform-specific logic lives here.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import random
import time
from typing import Optional

import undetected_chromedriver as uc
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    InvalidSessionIdException,
    TimeoutException,
    WebDriverException,
)

LOGGER = logging.getLogger("simbli_minutes")


def create_undetected_driver() -> uc.Chrome:
    """
    Creates and configures an undetected Chrome driver instance running
    inside an Xvfb virtual frame buffer display environment.
    """
    time.sleep(random.uniform(2.5, 6.0))

    LOGGER.info("Creating undetected Chrome driver...")
    os.environ['DISPLAY'] = ':99'

    options = uc.ChromeOptions()
    pid = os.getpid()
    options.add_argument(f'--user-data-dir=/tmp/chrome_profile_{pid}')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-blink-features=AutomationControlled')
    options.add_argument('--window-size=1920,1080')
    options.add_argument('--start-maximized')
    options.add_argument('--no-first-run')
    options.add_argument('--no-default-browser-check')
    options.add_argument(
        'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    )

    download_dir = f"/tmp/dl_{pid}"
    os.makedirs(download_dir, exist_ok=True)

    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "download.default_directory": download_dir,
        "download.prompt_for_download": False,
        "download.directory_upgrade": True,
        "plugins.always_open_pdf_externally": True,
    }
    options.add_experimental_option("prefs", prefs)

    try:
        driver = uc.Chrome(
            options=options,
            use_subprocess=True,
            driver_executable_path='/usr/local/bin/chromedriver'
        )
        driver.set_page_load_timeout(60)
        driver.implicitly_wait(5)
        LOGGER.info("✅ Successfully created undetected Chrome driver.")
        return driver
    except Exception as e:
        LOGGER.error(f"Failed to create Chrome driver: {e}")
        raise


def safe_click(driver: uc.Chrome, element) -> None:
    """Clicks an element, falling back to a JavaScript click if obstructed."""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)
        WebDriverWait(driver, 5).until(EC.element_to_be_clickable(element))
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def _is_session_alive(driver: uc.Chrome) -> bool:
    """Verifies if the browser automation process is still responsive."""
    try:
        _ = driver.current_url
        return True
    except (InvalidSessionIdException, WebDriverException):
        return False


def wait_for_new_tab(driver: uc.Chrome, original_handles: list, timeout: int = 15) -> Optional[str]:
    """Blocks until a new browser tab or window handle is allocated."""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: len(d.window_handles) > len(original_handles)
        )
        new_handles = [h for h in driver.window_handles if h not in original_handles]
        return new_handles[0] if new_handles else None
    except TimeoutException:
        LOGGER.debug("  No new tab appeared within timeout.")
        return None


def close_extra_tabs(driver: uc.Chrome, keep_handle: str) -> None:
    """Closes all browser tabs except the given handle."""
    try:
        for handle in driver.window_handles:
            if handle != keep_handle:
                driver.switch_to.window(handle)
                driver.close()
        driver.switch_to.window(keep_handle)
    except Exception as e:
        LOGGER.warning(f"Failed to cleanly close lingering tabs: {e}")


def wait_for_download(download_dir: str, timeout: int = 40) -> Optional[str]:
    """Blocks until a non-partial PDF appears in download_dir."""
    LOGGER.debug(f"[DOWNLOAD] Watching directory: {download_dir}")
    start = time.time()
    seen = set(os.listdir(download_dir))

    while time.time() - start < timeout:
        current = set(os.listdir(download_dir))
        new_files = current - seen

        for f in new_files:
            if f.endswith(".pdf") and not f.endswith(".crdownload"):
                full_path = os.path.join(download_dir, f)
                if not os.path.exists(full_path + ".crdownload"):
                    LOGGER.debug(f"[DOWNLOAD] Found completed file: {full_path}")
                    return full_path
        time.sleep(1)
    LOGGER.warning("[DOWNLOAD] Timeout waiting for file download.")
    return None
