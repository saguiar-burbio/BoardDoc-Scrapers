# ─────────────────────────────────────────────────────────────────────────────
# crawlers/boarddocs/scraper.py
# BoardDocs — platform-specific scraping layer.
#
# Shared infrastructure is imported from core/.
# Only BoardDocs-specific DOM logic lives here.
# ─────────────────────────────────────────────────────────────────────────────

import base64
import json
import logging
import os
import random
import re
import tempfile
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Set, Tuple

import pandas as pd
import requests
from requests import Session
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config.settings import MINUTES_FOLDER_ID, MODEL_NAME
from core.analyzer import analyze_pdf_with_gemini_with_retry
from core.database import (
    db_check_minhash_dupe,
    db_check_sha256_dupe,
    get_prompt_info,
    log_ai_usage,
    log_crawl_attachment,
)
from core.document_utils import (
    _cover_page_is_policy,
    build_unique_filename,
    classify_doc_type,
    combine_and_upload_documents,
    debug_check_pdf_file,
    is_pdf_corrupted,
)
from core.driver import _is_session_alive
from core.google_functions import upload_file_to_folder
from core.hashing import build_minhash, compute_sha256_from_file, serialize_minhash
from core.humanize import random_idle, slow_scroll
from core.models import AttachmentRecord, MeetingRecord
from core.pdf_functions import (
    extract_first_date_from_pdf,
    extract_text_with_ocr,
    parse_to_mmddyy,
)

LOGGER = logging.getLogger("simbli_minutes")

# drive_service is injected by crawlers/boarddocs/main.py at startup:
#   import crawlers.boarddocs.scraper as bd_scraper
#   bd_scraper.drive_service = drive_service
drive_service = None

# ─── BoardDocs CSS/XPath selectors ───────────────────────────────────────────
BD_VIEW_AGENDA_BTN_ID     = "btn-view-agenda"
BD_VIEW_MINUTES_BTN_ID    = "btn-view-minutes-id"
BD_DIALOG_CLOSE_BTN_XPATH = (
    "//div[contains(@class,'ui-dialog-buttonset')]"
    "//button[normalize-space()='Close']"
)


# =============================================================================
# SECTION 1 — SELENIUM INTERACTION HELPERS
# =============================================================================

def safe_click(driver, element) -> None:
    try:
        WebDriverWait(driver, 5).until(EC.element_to_be_clickable(element))
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def wait_for_new_tab(driver, original_handles: list, timeout: int = 15) -> Optional[str]:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: len(d.window_handles) > len(original_handles)
        )
        new_handles = [h for h in driver.window_handles if h not in original_handles]
        return new_handles[0] if new_handles else None
    except TimeoutException:
        LOGGER.debug("  No new tab appeared within timeout.")
        return None


def close_extra_tabs(driver, keep_handle: str) -> None:
    for handle in driver.window_handles:
        if handle != keep_handle:
            driver.switch_to.window(handle)
            driver.close()
    driver.switch_to.window(keep_handle)


# =============================================================================
# SECTION 2 — PDF DOWNLOAD
# =============================================================================

def download_pdf_with_selenium_cookies(driver, pdf_url: str, filepath: str) -> bool:
    """Download a PDF using the browser's current session cookies."""
    LOGGER.debug(f"  Downloading PDF via requests+cookies: {pdf_url[:80]}...")
    session = Session()
    for cookie in driver.get_cookies():
        session.cookies.set(cookie["name"], cookie["value"])
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/117.0.5938.92 Safari/537.36"
        )
    }
    try:
        response = session.get(pdf_url, headers=headers, timeout=60)
        if response.status_code == 200:
            with open(filepath, "wb") as f:
                f.write(response.content)
            LOGGER.info(f"  ✅ PDF saved: {filepath} ({os.path.getsize(filepath):,} bytes)")
            return True
        LOGGER.error(f"  ❌ PDF download failed. Status: {response.status_code}")
        return False
    except Exception as e:
        LOGGER.error(f"  ❌ PDF download exception: {e}")
        return False


# =============================================================================
# SECTION 3 — MEETING LIST  (JSON-LD)
# =============================================================================

def get_boarddocs_meetings(driver, link: str) -> List[Dict]:
    """
    Navigate to the BoardDocs district page and collect meeting records from
    JSON-LD <script type="application/ld+json"> blocks embedded in the page.

    Returns list of dicts with keys: title, start_date, url.
    """
    LOGGER.info(f"  Loading BoardDocs meeting list: {link}")
    driver.get(link)
    time.sleep(15)
    slow_scroll(driver)

    meetings: List[Dict] = []
    unique_urls: set = set()

    script_elements = driver.find_elements(
        By.XPATH, '//script[@type="application/ld+json"]'
    )
    LOGGER.debug(f"  Found {len(script_elements)} JSON-LD script block(s).")

    for script in script_elements:
        try:
            raw_json = script.get_attribute("innerHTML")
            data = json.loads(raw_json)
            items = data if isinstance(data, list) else [data]
            for event in items:
                if event.get("@type") != "Event":
                    continue
                title      = event.get("name", "").strip()
                start_date = event.get("startDate", "").strip()
                url        = event.get("url", "").strip()
                if not url or url in unique_urls:
                    continue
                unique_urls.add(url)
                meetings.append({"title": title, "start_date": start_date, "url": url})
        except (json.JSONDecodeError, Exception) as e:
            LOGGER.warning(f"  JSON-LD parse error in script block: {e}")
            continue

    LOGGER.info(f"  Collected {len(meetings)} unique meeting(s) from JSON-LD.")
    return meetings


def _parse_boarddocs_date(date_str: str) -> Optional[datetime]:
    """Parse a BoardDocs ISO datetime string (e.g. '2024-05-12T00:00:00.000Z')."""
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(date_str, fmt)
        except ValueError:
            continue
    LOGGER.warning(f"  Could not parse BoardDocs date string: '{date_str}'")
    return None


# =============================================================================
# SECTION 4 — MEETING NAVIGATION
# =============================================================================

def navigate_to_meeting_and_open_agenda(
    driver,
    meeting_url: str,
    meeting_title: str,
) -> bool:
    """Navigate to a BoardDocs meeting page and click the 'View Agenda' button."""
    LOGGER.info(f"  Navigating to meeting: {meeting_url}")
    driver.get(meeting_url)
    time.sleep(10)
    slow_scroll(driver)
    random_idle()

    try:
        view_agenda_btn = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, BD_VIEW_AGENDA_BTN_ID))
        )
        LOGGER.debug(f"  'View Agenda' button found for: {meeting_title}")
        time.sleep(1)
        safe_click(driver, view_agenda_btn)
        LOGGER.info("  'View Agenda' button clicked — agenda pane loading.")
        time.sleep(3)
        return True
    except TimeoutException:
        LOGGER.warning(f"  ⚠️ 'View Agenda' button not found for: {meeting_title}")
        return False
    except Exception as e:
        LOGGER.error(f"  ❌ Error clicking 'View Agenda' for '{meeting_title}': {e}")
        return False


# =============================================================================
# SECTION 5 — COVER PAGE  (Print dialog → CDP printToPDF)
# =============================================================================

def _dismiss_boarddocs_dialog(driver) -> None:
    """Click the 'Close' button in a BoardDocs print dialog if present."""
    try:
        close_btn = WebDriverWait(driver, 3).until(
            EC.element_to_be_clickable((By.XPATH, BD_DIALOG_CLOSE_BTN_XPATH))
        )
        safe_click(driver, close_btn)
        LOGGER.debug("  [BD Dialog] Close button clicked.")
    except (TimeoutException, NoSuchElementException):
        pass
    except Exception as e:
        LOGGER.debug(f"  [BD Dialog] Could not dismiss dialog: {e}")


def _get_boarddocs_cover_page_pdf(
    driver,
    nces: str,
    district: str,
    term_for_naming: str,
    row_date: datetime,
) -> Optional[str]:
    """
    Capture the current agenda item as a PDF via the BoardDocs Print dialog flow:
      1. Close any leftover dialog.
      2. Click the fa-print icon via JS.
      3. Switch to the 'Current Agenda Item' tab.
      4. CDP Page.printToPDF.
    """
    output_path = os.path.join(
        tempfile.gettempdir(),
        f"{nces}_{district.upper()}_{term_for_naming}_{row_date.strftime('%m%d%y')}_cover.pdf",
    )

    try:
        # 1. Close any lingering dialog
        try:
            close_btn = driver.find_element(
                By.XPATH,
                "//div[contains(@class,'ui-dialog')]"
                "//button[contains(@class,'ui-dialog-titlebar-close')]",
            )
            if close_btn.is_displayed():
                driver.execute_script("arguments[0].click();", close_btn)
                LOGGER.debug("[BD Cover] Closed leftover print dialog.")
                time.sleep(1)
        except Exception:
            pass

        # 2. Click the print icon
        print_icon = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.XPATH, "//i[contains(@class,'fa-print')]"))
        )
        driver.execute_script("arguments[0].click();", print_icon)
        LOGGER.debug("[BD Cover] Print icon clicked via JS.")
        time.sleep(3)

        # 3. Switch to 'Current Agenda Item' tab
        try:
            current_tab = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable((
                    By.XPATH,
                    "//div[contains(@class,'ui-dialog')]"
                    "//li[contains(@class,'print-agenda')]"
                    "//span[contains(text(),'Current Agenda Item')]",
                ))
            )
            driver.execute_script("arguments[0].click();", current_tab)
            LOGGER.debug("[BD Cover] Switched to 'Current Agenda Item' tab.")
            time.sleep(2)
        except Exception as e:
            LOGGER.warning(f"[BD Cover] Could not switch tab: {e}")

        # 4. Wait for tab-3 content
        try:
            WebDriverWait(driver, 10).until(
                EC.visibility_of_element_located((
                    By.XPATH,
                    "//div[@id='tab-3' and not(contains(@style,'display: none'))]",
                ))
            )
            LOGGER.debug("[BD Cover] 'Current Agenda Item' content is visible.")
        except Exception as e:
            LOGGER.warning(f"[BD Cover] Tab-3 visibility check timed out: {e}")

        # 5. CDP print to PDF
        pdf_data = driver.execute_cdp_cmd(
            "Page.printToPDF",
            {"printBackground": True, "preferCSSPageSize": True},
        )
        with open(output_path, "wb") as f:
            f.write(base64.b64decode(pdf_data["data"]))

        LOGGER.info(f"[BD Cover] Saved cover PDF: {output_path}")
        return output_path

    except Exception as e:
        LOGGER.error(f"[BD Cover] Failed: {e}", exc_info=True)
        return None


# =============================================================================
# SECTION 6 — MINUTES ICON  (standalone minutes PDF)
# =============================================================================

def _download_boarddocs_minutes_icon(
    driver,
    main_window: str,
    nces: str,
    district: str,
    meeting_type: str,
    row_date: datetime,
    link: str,
    prompt_df: pd.DataFrame,
    meeting_record: MeetingRecord,
    meeting_id: Optional[int] = None,
) -> None:
    """
    If the page exposes a minutes icon button (id='btn-view-minutes-id'), click it,
    download the resulting PDF, classify with Gemini, and upload to MINUTES_FOLDER_ID.
    Mutates meeting_record in-place.
    """
    file_date      = row_date.strftime("%m-%d-%y")
    district_upper = district.upper()
    handles_before = list(driver.window_handles)
    icon_tmp_path  = ""

    try:
        minutes_btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.ID, BD_VIEW_MINUTES_BTN_ID))
        )
        LOGGER.info("  [BD Minutes Icon] Minute icon button found — downloading.")
    except TimeoutException:
        LOGGER.debug("  [BD Minutes Icon] No minute icon on this meeting page.")
        return

    try:
        safe_click(driver, minutes_btn)
        time.sleep(3)

        try:
            print_btns = driver.find_elements(By.XPATH, "//button")
            print_btn  = next(
                (b for b in print_btns if b.text.strip().lower() == "print"), None
            )
            if print_btn:
                safe_click(driver, print_btn)
                LOGGER.debug("  [BD Minutes Icon] Print button clicked inside minutes view.")
                time.sleep(3)
                close_btn = next(
                    (b for b in driver.find_elements(By.XPATH, "//button")
                     if b.text.strip().lower() == "close"),
                    None,
                )
                if close_btn:
                    safe_click(driver, close_btn)
                    LOGGER.debug("  [BD Minutes Icon] Close button clicked.")
        except Exception as e:
            LOGGER.warning(f"  [BD Minutes Icon] Could not click Print inside minutes: {e}")

        wait_secs = random.uniform(20, 35)
        LOGGER.info(f"  [BD Minutes Icon] Waiting {wait_secs:.1f}s for PDF to resolve...")
        time.sleep(wait_secs)

        new_tab = wait_for_new_tab(driver, handles_before, timeout=10)
        if new_tab:
            driver.switch_to.window(new_tab)
            time.sleep(5)

        pdf_url = driver.current_url
        LOGGER.info(f"  [BD Minutes Icon] PDF URL: {pdf_url}")

        icon_tmp_path = os.path.join(
            tempfile.gettempdir(),
            f"{nces}_{district_upper}_MINUTES_ICON_{file_date}.pdf",
        )

        if not download_pdf_with_selenium_cookies(driver, pdf_url, icon_tmp_path):
            LOGGER.error("  [BD Minutes Icon] ❌ Download failed.")
            meeting_record.errors += 1
            return

        if not debug_check_pdf_file(icon_tmp_path):
            LOGGER.error("  [BD Minutes Icon] ❌ Downloaded file is not a valid PDF.")
            meeting_record.errors += 1
            return

        file_hash = compute_sha256_from_file(icon_tmp_path)
        if db_check_sha256_dupe(file_hash):
            LOGGER.info("  [BD Minutes Icon] 🔁 SHA-256 dupe — skipping.")
            meeting_record.dupes += 1
            return

        text, _ = extract_text_with_ocr(icon_tmp_path)
        minhash_obj = build_minhash(text)
        minhash_is_dupe, _ = db_check_minhash_dupe(minhash_obj)
        if minhash_is_dupe:
            LOGGER.info("  [BD Minutes Icon] 🔁 MinHash dupe — skipping.")
            meeting_record.dupes += 1
            return

        prompt_text, prompt_id = get_prompt_info(prompt_df, "minutes_and_agendas")
        analysis = analyze_pdf_with_gemini_with_retry(icon_tmp_path, prompt_text, MODEL_NAME)
        res       = analysis.get("result", {})

        log_ai_usage(
            prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
            tokens=analysis.get("tokens", {}),
            status="SUCCESS" if "error" not in res else "FAILED",
            meeting_id=meeting_id,
        )

        MAPPING = {
            "REGULAR": "BOE-REG", "COMMITTEE": "BOE-COM", "WORK":      "BOE-WS",
            "FINANCE": "BOE-FIN", "PUBLIC":    "BOE-PUB", "EXECUTIVE": "BOE-EXE",
            "SPECIAL": "BOE-SP",  "AGENDA":    "BOE-AGENDA",
        }
        raw_type       = res.get("meeting_type", "").upper()
        doc_type       = MAPPING.get(raw_type, classify_doc_type(meeting_type))
        meeting_date_r = res.get("meeting_date")
        ai_date        = parse_to_mmddyy(meeting_date_r) if meeting_date_r else None
        if not ai_date:
            ai_date = parse_to_mmddyy(extract_first_date_from_pdf(icon_tmp_path))
        final_date_str = ai_date or row_date.strftime("%m%d%y")

        base_name       = f"{nces}_{district_upper}_{doc_type}_{final_date_str}.pdf"
        file_name_final = build_unique_filename(base_name)

        uploaded_file_id = upload_file_to_folder(
            drive_service, MINUTES_FOLDER_ID, icon_tmp_path, file_name_final
        )
        LOGGER.info(
            f"  [BD Minutes Icon] ✅ Uploaded: "
            f"https://drive.google.com/file/d/{uploaded_file_id}/view?usp=sharing"
        )

        att_record = AttachmentRecord(index=0, name="[Minutes Icon]", pdf_url=pdf_url)
        meeting_record.attachments.append(att_record)
        meeting_record.downloaded += 1

    except Exception as e:
        LOGGER.error(f"  [BD Minutes Icon] Unexpected error: {e}")
        LOGGER.debug(traceback.format_exc())
        meeting_record.errors += 1

    finally:
        for handle in driver.window_handles:
            if handle != main_window:
                try:
                    driver.switch_to.window(handle)
                    driver.close()
                except Exception:
                    pass
        try:
            driver.switch_to.window(main_window)
        except Exception:
            pass
        if icon_tmp_path and os.path.exists(icon_tmp_path):
            try:
                os.remove(icon_tmp_path)
            except OSError:
                pass


# =============================================================================
# SECTION 7 — SINGLE ATTACHMENT PROCESSOR
# =============================================================================

def _process_single_attachment(
    driver,
    a_tag,
    attachment_index: int,
    main_window: str,
    nces: str,
    district: str,
    meeting_type: str,
    term_for_naming: str,
    span_text: str,
    row_date: datetime,
    link: str,
    prompt_df: pd.DataFrame,
    att_record: AttachmentRecord,
    meeting_record: MeetingRecord,
    meeting_id: Optional[int] = None,
    specialist_mode: bool = False,
    category: str = "GENERAL_SUPPORTING",
) -> Optional[str]:
    """
    Download and hash-check a single BoardDocs public attachment.

    BoardDocs public-file links are direct URLs — download via session cookies.
    specialist_mode=True + category='BOARD_MINUTES' → classify and upload directly.
    Otherwise returns the temp file path for the caller to merge.
    """
    href            = a_tag.get_attribute("href")
    attachment_name = a_tag.text.strip() or a_tag.get_attribute("title") or ""
    file_date       = row_date.strftime("%m-%d-%y")
    district_upper  = district.upper()

    LOGGER.debug(
        f"  Attachment #{attachment_index}: name='{attachment_name}'  href='{href}'"
    )
    att_record.name    = attachment_name
    att_record.pdf_url = href or ""

    if not href:
        LOGGER.warning(f"  ⚠️ Attachment #{attachment_index} has no href — skipping.")
        att_record.downloaded = "⚠️ No href"
        log_crawl_attachment(
            meeting_id=meeting_id, nces_id=nces, pdf_url="",
            attachment_name=attachment_name, outcome="⚠️ No href", is_duplicate=False,
        )
        meeting_record.errors += 1
        return None

    file_name = (
        f"{nces}_{district_upper}_{term_for_naming}_{file_date}_{attachment_index}.pdf"
    )
    filepath     = os.path.join(tempfile.gettempdir(), file_name)
    return_path: Optional[str] = None

    try:
        # ── Step 1: Download ────────────────────────────────────────────────
        if not download_pdf_with_selenium_cookies(driver, href, filepath):
            att_record.downloaded = "❌ DL Failed"
            log_crawl_attachment(
                meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                attachment_name=attachment_name, outcome="❌ DL Failed", is_duplicate=False,
            )
            meeting_record.errors += 1
            return None

        if not debug_check_pdf_file(filepath) or is_pdf_corrupted(filepath):
            att_record.downloaded = "❌ Not a PDF"
            log_crawl_attachment(
                meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                attachment_name=attachment_name, outcome="❌ Not a PDF", is_duplicate=False,
            )
            meeting_record.errors += 1
            if os.path.exists(filepath):
                os.remove(filepath)
            return None

        # ── Step 2: SHA-256 dupe check ──────────────────────────────────────
        file_hash = compute_sha256_from_file(filepath)
        if db_check_sha256_dupe(file_hash):
            att_record.downloaded  = "🔁 Duplicate"
            att_record.dupe_check_type = "sha256"
            att_record.passed_dupe = False
            log_crawl_attachment(
                meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                attachment_name=attachment_name, outcome="🔁 Duplicate",
                sha256_hash=file_hash, is_duplicate=True,
            )
            meeting_record.dupes += 1
            if os.path.exists(filepath):
                os.remove(filepath)
            return None

        # ── Step 3: OCR + MinHash dupe check ────────────────────────────────
        text, _        = extract_text_with_ocr(filepath)
        minhash_obj    = build_minhash(text)
        minhash_str    = serialize_minhash(minhash_obj)
        minhash_is_dupe, matching_link = db_check_minhash_dupe(minhash_obj)

        if minhash_is_dupe:
            LOGGER.info(f"  🔁 MinHash dupe ({matching_link}) — skipping #{attachment_index}.")
            att_record.downloaded  = "🔁 Duplicate"
            att_record.dupe_check_type = "minhash"
            att_record.passed_dupe = False
            log_crawl_attachment(
                meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                attachment_name=attachment_name, outcome="🔁 Duplicate",
                sha256_hash=file_hash, minhash_json=minhash_str, is_duplicate=True,
            )
            meeting_record.dupes += 1
            if os.path.exists(filepath):
                os.remove(filepath)
            return None

        log_crawl_attachment(
            meeting_id=meeting_id, nces_id=nces, pdf_url=href,
            attachment_name=attachment_name, outcome="✅ Downloaded",
            sha256_hash=file_hash, minhash_json=minhash_str, is_duplicate=False,
        )

        att_record.passed_dupe     = True
        att_record.dupe_check_type = "none"

        # ── Step 4: Route — specialist minutes upload or return for merge ───
        if specialist_mode and category.upper() == "BOARD_MINUTES":
            prompt_text, prompt_id = get_prompt_info(prompt_df, "minutes_and_agendas")
            analysis = analyze_pdf_with_gemini_with_retry(filepath, prompt_text, MODEL_NAME)
            res      = analysis.get("result", {})

            log_ai_usage(
                prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                tokens=analysis.get("tokens", {}),
                status="SUCCESS" if "error" not in res else "FAILED",
                meeting_id=meeting_id,
            )

            if not res.get("is_minutes", False):
                att_record.downloaded = "✅ Downloaded"
                return_path = filepath
                LOGGER.debug(f"  Non-minutes (AI): returning filepath for merge → {filepath}")
            else:
                MAPPING = {
                    "REGULAR": "BOE-REG", "COMMITTEE": "BOE-COM", "WORK":      "BOE-WS",
                    "FINANCE": "BOE-FIN", "PUBLIC":    "BOE-PUB", "EXECUTIVE": "BOE-EXE",
                    "SPECIAL": "BOE-SP",  "AGENDA":    "BOE-AGENDA",
                }
                raw_type       = res.get("meeting_type", "").upper()
                doc_type       = MAPPING.get(raw_type, classify_doc_type(meeting_type))
                meeting_date_r = res.get("meeting_date")
                ai_date        = parse_to_mmddyy(meeting_date_r) if meeting_date_r else None
                if not ai_date:
                    ai_date = parse_to_mmddyy(extract_first_date_from_pdf(filepath))
                final_date_str  = ai_date or file_date
                base_name       = f"{nces}_{district_upper}_{doc_type}_{final_date_str}.pdf"
                file_name_final = build_unique_filename(base_name)

                uploaded_file_id = upload_file_to_folder(
                    drive_service, MINUTES_FOLDER_ID, filepath, file_name_final
                )
                LOGGER.info(
                    f"  ✅ [Minutes] Uploaded: "
                    f"https://drive.google.com/file/d/{uploaded_file_id}/view?usp=sharing"
                )
                att_record.downloaded = "✅ Downloaded"
                att_record.file_name  = file_name_final
                meeting_record.downloaded += 1
                return_path = None
        else:
            att_record.downloaded = "✅ Downloaded"
            return_path = filepath
            LOGGER.debug(f"  Standard path: returning filepath for merge → {filepath}")

    except Exception as e:
        LOGGER.error(f"  ❌ Error on attachment #{attachment_index}: {e}")
        LOGGER.debug(traceback.format_exc())
        if att_record.downloaded == "—":
            att_record.downloaded  = "❌ Error"
            meeting_record.errors += 1
        return_path = None

    finally:
        if return_path is None and os.path.exists(filepath):
            try:
                os.remove(filepath)
                LOGGER.debug(f"  🗑️ Temp file removed: {filepath}")
            except OSError as e:
                LOGGER.warning(f"  ⚠️ Could not remove temp file '{filepath}': {e}")

    return return_path


# =============================================================================
# SECTION 8 — AGENDA ITEM TRAVERSAL
# =============================================================================

def search_and_download_agenda_attachments(
    driver,
    nces: str,
    district: str,
    row_date: datetime,
    meeting_type: str,
    link: str,
    prompt_df: pd.DataFrame,
    meeting_record: MeetingRecord,
    meeting_id: Optional[int] = None,
) -> None:
    """Iterate every #agenda li.item on the current BoardDocs meeting page."""
    main_window = driver.current_window_handle

    LOGGER.info(
        f"\n{'─' * 60}\n"
        f"  BOARDDOCS SCRAPE: {district} | {meeting_type} | {row_date.strftime('%m-%d-%y')}\n"
        f"{'─' * 60}"
    )

    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#agenda"))
        )
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "#agenda li.item"))
        )
    except TimeoutException:
        LOGGER.warning("  ⚠️ Agenda did not load — nothing to iterate.")
        return

    agenda_selector = "#agenda li.item"
    agenda_items    = driver.find_elements(By.CSS_SELECTOR, agenda_selector)
    total_items     = len(agenda_items)
    LOGGER.info(f"  Found {total_items} agenda item(s) to process.")

    cover_path: Optional[str] = None

    for item_index in range(total_items):
        cover_path = None

        try:
            # Re-fetch each loop to avoid stale elements
            agenda_items = driver.find_elements(By.CSS_SELECTOR, agenda_selector)
            li = agenda_items[item_index]

            raw_text = (
                li.get_attribute("xtitle")
                or li.text.strip().split("\n")[0].strip()
                or f"Item_{item_index}"
            )

            if not raw_text.strip():
                continue

            LOGGER.info(f"  [{item_index}] Processing: '{raw_text[:80]}'")

            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", li
            )
            time.sleep(0.5)

            try:
                safe_click(driver, li)
            except Exception:
                driver.execute_script("arguments[0].click();", li)

            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#view-agenda-item"))
            )
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "#ai-name"))
            )
            time.sleep(1.5)

            term_for_naming = re.sub(r"[^\w\s-]", "", raw_text).replace(" ", "_")[:30]

            # ── Step 1: Capture cover page ──────────────────────────────────
            cover_path = _get_boarddocs_cover_page_pdf(
                driver, nces, district, term_for_naming, row_date
            )

            # ── Step 2: AI routing ──────────────────────────────────────────
            router_result = {"is_minute": False, "category": "GENERAL_SUPPORTING"}
            if cover_path:
                router_prompt, prompt_id = get_prompt_info(prompt_df, "pre-classification")
                ai_routing    = analyze_pdf_with_gemini_with_retry(
                    cover_path, router_prompt, MODEL_NAME
                )
                router_result = ai_routing.get("result", router_result)
                log_ai_usage(
                    prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                    tokens=ai_routing.get("tokens", {}),
                    status="SUCCESS" if "error" not in ai_routing.get("result", {}) else "FAILED",
                    meeting_id=meeting_id,
                )
                LOGGER.debug(
                    f"  [AI] category={router_result.get('category')} | "
                    f"reasoning={router_result.get('reasoning', '')[:80]}"
                )

            use_specialist  = router_result.get("is_minute", False)
            target_category = router_result.get("category", "GENERAL_SUPPORTING")
            is_policy_item  = _cover_page_is_policy(cover_path)

            LOGGER.info(
                f"  [{item_index}] Routing: {target_category} | "
                f"Specialist: {use_specialist} | Policy: {is_policy_item}"
            )

            # ── Step 3: Find attachments ────────────────────────────────────
            detail_panel     = driver.find_element(By.CSS_SELECTOR, "#view-agenda-item")
            attachment_links = detail_panel.find_elements(
                By.XPATH, ".//div[contains(@id,'attachment-public')]//a"
            )
            LOGGER.info(f"  [{item_index}] Attachment link(s): {len(attachment_links)}")

            if not attachment_links:
                if cover_path:
                    combine_and_upload_documents(
                        cover_page_path=cover_path, attachment_paths=[],
                        nces=nces, district=district, meeting_type=meeting_type,
                        term_for_naming=term_for_naming, span_text=raw_text,
                        row_date=row_date, link=link, prompt_df=prompt_df,
                        pdf_urls=[], attachment_names=[],
                        meeting_record=meeting_record, meeting_id=meeting_id,
                        category=None,
                    )
                continue

            meeting_record.total += len(attachment_links)

            collected_paths: List[str] = []
            collected_urls:  List[str] = []
            collected_names: List[str] = []

            # ── Step 4: Process each attachment ─────────────────────────────
            for i, a_tag in enumerate(attachment_links, start=1):
                att_record = AttachmentRecord(
                    index=i,
                    name=a_tag.text.strip() or a_tag.get_attribute("title") or "",
                )
                meeting_record.attachments.append(att_record)

                result_path = _process_single_attachment(
                    driver=driver, a_tag=a_tag,
                    attachment_index=i, main_window=main_window,
                    nces=nces, district=district, meeting_type=meeting_type,
                    term_for_naming=term_for_naming, span_text=raw_text,
                    row_date=row_date, link=link, prompt_df=prompt_df,
                    att_record=att_record, meeting_record=meeting_record,
                    meeting_id=meeting_id,
                    specialist_mode=use_specialist, category=target_category,
                )

                if result_path is None:
                    continue

                if is_policy_item:
                    try:
                        combine_and_upload_documents(
                            cover_page_path=cover_path, attachment_paths=[result_path],
                            nces=nces, district=district, meeting_type=meeting_type,
                            term_for_naming=term_for_naming, span_text=raw_text,
                            row_date=row_date, link=link, prompt_df=prompt_df,
                            pdf_urls=[att_record.pdf_url or ""],
                            attachment_names=[att_record.name or ""],
                            meeting_record=meeting_record, meeting_id=meeting_id,
                            category=target_category,
                        )
                    finally:
                        if os.path.exists(result_path):
                            try:
                                os.remove(result_path)
                            except OSError:
                                pass
                else:
                    collected_paths.append(result_path)
                    collected_urls.append(att_record.pdf_url or "")
                    collected_names.append(att_record.name or "")

            # ── Step 5: Bulk merge + upload ─────────────────────────────────
            if collected_paths:
                combine_and_upload_documents(
                    cover_page_path=cover_path, attachment_paths=collected_paths,
                    nces=nces, district=district, meeting_type=meeting_type,
                    term_for_naming=term_for_naming, span_text=raw_text,
                    row_date=row_date, link=link, prompt_df=prompt_df,
                    pdf_urls=collected_urls, attachment_names=collected_names,
                    meeting_record=meeting_record, meeting_id=meeting_id,
                    category=target_category,
                )
                for p in collected_paths:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass

        except StaleElementReferenceException:
            LOGGER.warning(f"  [{item_index}] Stale element — skipping.")
            continue

        except Exception as e:
            LOGGER.error(f"  ❌ Failed item '{raw_text}': {e}")
            LOGGER.debug(traceback.format_exc())

        finally:
            if cover_path and os.path.exists(cover_path):
                try:
                    os.remove(cover_path)
                except OSError:
                    pass

            # Dismiss any open print dialog before the next item
            _dismiss_boarddocs_dialog(driver)
