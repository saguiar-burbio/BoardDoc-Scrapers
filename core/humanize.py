# ─────────────────────────────────────────────────────────────────────────────
# src/humanize.py
# ─────────────────────────────────────────────────────────────────────────────

import random
import time
import logging
from selenium.webdriver.common.action_chains import ActionChains

# Use the established system logging named context
LOGGER = logging.getLogger("simbli_minutes")


def human_like_mouse_move(driver, element) -> None:
    """
    Moves the mouse cursor to a target web element with randomized 
    pixel offsets to simulate natural human motor movement.
    """
    try:
        actions = ActionChains(driver)
        size = element.size
        # Add random pixel padding within the boundaries of the target element
        offset_x = random.randint(5, max(6, size['width'] - 6))
        offset_y = random.randint(5, max(6, size['height'] - 6))
        actions.move_to_element_with_offset(element, offset_x, offset_y).perform()
        time.sleep(random.uniform(0.2, 0.6))
    except Exception as e:
        LOGGER.warning(f"Simulated mouse move failed: {e}")


def human_click(driver, element) -> None:
    """
    Simulates a natural user click by hovering over the element, pausing briefly 
    as if targeting, performing the click, and introducing a post-click delay.
    """
    try:
        actions = ActionChains(driver)
        actions.move_to_element(element).pause(random.uniform(0.2, 0.7)).click().perform()
        time.sleep(random.uniform(1.0, 2.0))
    except Exception as e:
        LOGGER.warning(f"Simulated human click failed: {e}")


def slow_scroll(driver, step: int = 200, pause: float = 0.2) -> None:
    """
    Gradually scrolls the active page downward to simulate a user reading 
    the viewport contents using a scroll wheel or keyboard arrows.
    """
    try:
        last_height = driver.execute_script("return document.body.scrollHeight")
        for pos in range(0, last_height, step):
            driver.execute_script(f"window.scrollTo(0, {pos});")
            time.sleep(pause + random.uniform(0.05, 0.2))
    except Exception as e:
        LOGGER.warning(f"Simulated page scroll failed: {e}")


def random_idle() -> None:
    """
    Introduces an occasional random thinking pause (30% probability) 
    to break regular automated scraping cadence and stay undetected.
    """
    if random.random() < 0.3:  # 30% chance to trigger an idle delay
        delay = random.uniform(3.0, 7.0)
        LOGGER.info(f"🕒 Human simulation delay: Idling for {delay:.2f} seconds...")
        time.sleep(delay)