# ─────────────────────────────────────────────────────────────────────────────
# src/utils.py
# ─────────────────────────────────────────────────────────────────────────────

import os
import time
import logging
from typing import Dict, Any, Optional

# Reference the named logger established in this configuration
LOGGER = logging.getLogger("simbli_minutes")


# ═════════════════════════════════════════════════════════════════════════════
# 1. LOGGING CONFIGURATION ENGINE
# ═════════════════════════════════════════════════════════════════════════════

def setup_logger(log_level: str = "INFO", log_file: str = None) -> logging.Logger:
    """
    Builds and registers a named rotating logger with stdout console handlers.

    Args:
        log_level: Severity threshold (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        log_file: Optional filepath to persist physical logging diagnostics.

    Returns:
        A pre-configured logging.Logger instance.
    """
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    fmt = "%(asctime)s [%(levelname)-8s] %(funcName)s:%(lineno)d — %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"
    
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    
    if log_file:
        from logging.handlers import RotatingFileHandler
        # Handle file rotation safely to prevent logs from eating disk space
        handlers.append(RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=3))
        
    logging.basicConfig(level=numeric_level, format=fmt, datefmt=datefmt, handlers=handlers)
    
    logger = logging.getLogger("simbli_minutes")
    logger.setLevel(numeric_level)
    return logger


# ═════════════════════════════════════════════════════════════════════════════
# 2. DIAGNOSTIC & DEBUG UTILITIES
# ═════════════════════════════════════════════════════════════════════════════

def debug_screenshot(driver, label: str = "screenshot") -> None:
    """
    Captures a physical PNG screenshot of the current browser state.
    Used during exceptions to debug off-screen modal obstructions or dead frames.

    Args:
        driver: Active browser driver instance.
        label: Context prefix tag for the output screenshot filename.
    """
    filename = f"/tmp/screenshot_{label}_{int(time.time())}.png"
    try:
        driver.save_screenshot(filename)
        LOGGER.debug(f"📸 Screen diagnostic capture successfully saved to local temp: {filename}")
    except Exception as e:
        LOGGER.warning(f"Failed to capture browser diagnostic screenshot: {e}")


def debug_summarize_run_stats(stats: Dict[str, Any]) -> None:
    """
    Prints a clean, formatted diagnostic table of the current batch metrics.

    Args:
        stats: Structured dictionary tracking scraper execution parameters.
    """
    border = "=" * 60
    LOGGER.info(border)
    LOGGER.info("BOARDBCRAWLER — SCRAPE EXECUTION BATCH RUN COMPLETE SUMMARY")
    LOGGER.info(border)
    for k, v in stats.items():
        # Cleanly indent keys and dynamic statistical output lists
        if isinstance(v, list):
            LOGGER.info(f"  {k}:")
            for item in v:
                LOGGER.info(f"    • {item}")
        else:
            LOGGER.info(f"  {k}: {v}")
    LOGGER.info(border)