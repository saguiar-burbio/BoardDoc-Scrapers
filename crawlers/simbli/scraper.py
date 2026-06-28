# ─────────────────────────────────────────────────────────────────────────────
# crawlers/simbli/scraper.py
# Simbli eBoard — platform-specific DOM logic, download tiers, and DB helpers.
# Run from the repo root: python -m crawlers.simbli.main
# ─────────────────────────────────────────────────────────────────────────────

import base64
import glob
import json
import os
import random
import re
import shutil
import tempfile
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import fitz  # PyMuPDF
import psycopg2
import requests

from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# ── Core infrastructure imports ───────────────────────────────────────────────
from config.settings import (
    BUCKET_NAME,
    DOWNLOAD_DIR,
    MODEL_NAME,
    MINHASH_SIM_THRESHOLD,
)
from core.analyzer import analyze_pdf_with_gemini_with_retry
from core.database import (
    db_check_attachment_dupe,
    db_check_minhash_dupe,
    db_check_sha256_dupe,
    get_db_connection,
    get_drive_folder_map,
    get_prompt_info,
    log_ai_usage,
    log_attachment_to_db,
    log_contact_to_db,
    log_cover_page_to_db,
    log_document_response_to_db,
    log_error_to_db,
    log_meeting_to_db,
    log_to_crawler_hash,
    log_uploaded_document,
)


def _get_folder_id(name: str) -> str:
    return get_drive_folder_map().get(name, "")
from core.document_utils import (
    _cover_page_is_policy,
    build_unique_filename,
    classify_doc_type,
    debug_check_pdf_file,
)
from core.driver import _is_session_alive, create_undetected_driver, wait_for_download
from core.gcs import download_blob_to_tmp
from core.google_auth import get_authenticated_services
from core.google_functions import upload_file_to_folder
from core.hashing import build_minhash, compute_sha256_from_file, serialize_minhash
from core.humanize import human_click, human_like_mouse_move, random_idle, slow_scroll
from core.models import AttachmentRecord, MeetingRecord
from core.pdf_functions import extract_first_date_from_pdf, extract_text_with_ocr, parse_to_mmddyy
from core.utils import setup_logger

LOGGER = setup_logger(log_level="INFO")

# drive_service is set at module level so combine_and_upload_documents can
# reference it directly (injected by main.py before the batch loop starts).
drive_service = None

# ── CDP / download constants ──────────────────────────────────────────────────
_CDP_PDF_CONTENT_TYPES = {"application/pdf", "application/octet-stream", "binary/octet-stream"}
_TIER0_URL_SETTLE_SECS = 15
_NEW_TAB_TIMEOUT_SECS  = 20
_PRINT_TIMEOUT_SECS    = 45

# ── Outcome labels ────────────────────────────────────────────────────────────
OUTCOME_DOWNLOADED = "✅ Downloaded"
OUTCOME_DUPE       = "🔁 Duplicate"
OUTCOME_DL_FAILED  = "❌ DL Failed"
OUTCOME_NOT_PDF    = "❌ Not a PDF"
OUTCOME_NO_HREF    = "⚠️  No href"
OUTCOME_ERROR      = "❌ Error"


# ═════════════════════════════════════════════════════════════════════════════
# BROWSER HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def safe_click(driver, element) -> None:
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", element)
        WebDriverWait(driver, 5).until(EC.element_to_be_clickable(element))
        element.click()
    except Exception:
        driver.execute_script("arguments[0].click();", element)


def wait_for_new_tab(driver, original_handles: list, timeout: int = _NEW_TAB_TIMEOUT_SECS) -> Optional[str]:
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: len(d.window_handles) > len(original_handles)
        )
        new = [h for h in driver.window_handles if h not in original_handles]
        return new[0] if new else None
    except TimeoutException:
        LOGGER.debug("  No new tab appeared within timeout.")
        return None


def close_extra_tabs(driver, keep_handle: str) -> None:
    for handle in driver.window_handles:
        if handle != keep_handle:
            driver.switch_to.window(handle)
            driver.close()
    driver.switch_to.window(keep_handle)


# ═════════════════════════════════════════════════════════════════════════════
# CONTACT EXTRACTION
# ═════════════════════════════════════════════════════════════════════════════

def _looks_like_person_line(line: str) -> bool:
    if "," not in line:
        return False
    name_part, _, title_part = line.partition(",")
    name_part  = name_part.strip()
    title_part = title_part.strip()
    if not name_part or not title_part:
        return False
    name_words = name_part.split()
    if not (2 <= len(name_words) <= 4):
        return False
    if any(ch.isdigit() for ch in name_part):
        return False
    if not all(w[0].isupper() for w in name_words if w):
        return False
    if len(title_part) > 100:
        return False
    return True


def _log_person(p: dict, nces_id: str = "") -> None:
    if not p["name"]:
        return
    title_str = ", ".join(p["title"])
    LOGGER.info(f"  Contact: {p['name']} | {title_str}")
    log_contact_to_db(
        nces_id=nces_id,
        name=p["name"],
        email=p["email"],
        phone=p["phone"],
        title=title_str,
    )


def _parse_contact_block(content: str, nces_id: str) -> None:
    lines = [l.strip() for l in content.split("\n") if l.strip()]
    current_person = None
    for line in lines:
        email_match = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", line)
        phone_match = re.search(r"\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}", line)
        if email_match:
            if current_person:
                current_person["email"] = email_match.group(0)
        elif phone_match:
            if current_person:
                current_person["phone"] = phone_match.group(0)
        elif _looks_like_person_line(line):
            if current_person:
                _log_person(current_person, nces_id)
            name, _, title = line.partition(",")
            current_person = {
                "name": name.strip(),
                "title": [title.strip()],
                "phone": "",
                "email": "",
            }
    if current_person:
        _log_person(current_person, nces_id)


def extract_and_log_contact_info(driver, nces_id: str = "") -> None:
    try:
        container = driver.find_element(By.CSS_SELECTOR, "div.data-scroll")
        text = container.text.strip()
        if not text:
            return
        contact_headers = [
            "DIVISION SUPERVISOR", "PRESENTER", "OTHER CONTRIBUTOR",
            "CONTACT PERSON", "SUBMITTED BY",
            "DEPARTMENT AND/OR BOARD STAFF LIAISON",
            "DEPARTMENT ANDOR BOARD STAFF LIAISON",
        ]
        stop_headers = [
            "STRATEGIC INITIATIVE", "FINANCIAL INFORMATION",
            "MOTION STATEMENT", "BACKGROUND", "QUICK SUMMARY",
            "ABSTRACT", "GOALS", "CONTENT",
        ]
        all_headers = contact_headers + stop_headers
        headers_pattern = "|".join(re.escape(h) for h in all_headers)
        section_pattern = rf"(?i)({headers_pattern})\s*[\n:]*(.*?)(?=(?:{headers_pattern})|$)"
        matches = re.findall(section_pattern, text, re.DOTALL)
        for header, content in matches:
            header_upper = header.strip().upper()
            if not any(ch in header_upper for ch in contact_headers):
                continue
            _parse_contact_block(content, nces_id)
    except Exception as e:
        LOGGER.debug(f"  extract_and_log_contact_info: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# COVER PAGE
# ═════════════════════════════════════════════════════════════════════════════

def _is_cover_page_blank(driver, nces_id: str) -> bool:
    """Check Simbli data-scroll container for content. Extracts contacts as side effect."""
    try:
        wait = WebDriverWait(driver, 10)
        container = wait.until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.data-scroll"))
        )
        raw_text  = container.text.strip()
        doc_links = container.find_elements(
            By.CSS_SELECTOR, "a.supportingDocText, a[href*='Attachment.aspx']"
        )
        all_links = container.find_elements(By.CSS_SELECTOR, "a")
        LOGGER.debug(
            f"  _is_cover_page_blank: text={len(raw_text)} chars, "
            f"doc_links={len(doc_links)}, all_links={len(all_links)}"
        )
        if raw_text or doc_links or all_links:
            LOGGER.info(
                f"  div.data-scroll found — text={len(raw_text)} chars, "
                f"doc_links={len(doc_links)}, total_links={len(all_links)}"
            )
            extract_and_log_contact_info(driver, nces_id)
            return False
        LOGGER.info("  div.data-scroll found but empty (no text, no links).")
        return True
    except Exception as e:
        LOGGER.info(f"  div.data-scroll not found within 10s — {type(e).__name__}: {e}")
        return True


def _get_cover_page_pdf(
    driver,
    main_window: str,
    nces: str,
    district: str,
    term_for_naming: str,
    row_date: datetime,
) -> Optional[str]:
    """Capture the Simbli cover page via the print dialog."""
    if _is_cover_page_blank(driver, nces):
        LOGGER.info("  Cover page content is blank — skipping.")
        return None

    handles_before = list(driver.window_handles)
    try:
        # Click Print Options button
        try:
            print_button = WebDriverWait(driver, 15).until(
                EC.element_to_be_clickable(
                    (By.CSS_SELECTOR, "button[aria-controls='printOptions']")
                )
            )
        except TimeoutException:
            LOGGER.warning("  Print dropdown button [aria-controls='printOptions'] not found.")
            return None

        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", print_button)
        time.sleep(1)
        safe_click(driver, print_button)

        # Click "Print Item" in dropdown (href is javascript:void(0); — match by label text)
        print_item_link = WebDriverWait(driver, 10).until(
            EC.element_to_be_clickable(
                (By.XPATH,
                 "//ul[@id='printOptions']//label[normalize-space()='Print Item']/parent::a")
            )
        )
        safe_click(driver, print_item_link)

        # Handle new tab
        new_tab = wait_for_new_tab(driver, handles_before, timeout=20)
        if not new_tab:
            LOGGER.warning("  No new tab appeared after clicking Print Item.")
            return None

        driver.switch_to.window(new_tab)
        time.sleep(10)

        # Click final Print button inside preview tab
        try:
            final_print_button = WebDriverWait(driver, 20).until(
                EC.element_to_be_clickable(
                    (By.XPATH,
                     "//button[contains(., 'Print') and .//img[contains(@src, 'print_icon_black')]]")
                )
            )
            driver.execute_script("arguments[0].click();", final_print_button)
            time.sleep(8)
        except TimeoutException:
            LOGGER.warning("  Final Print button not found — trying fallback.")
            try:
                fallback_btn = driver.find_element(By.CSS_SELECTOR, "button.btn-default.font14")
                safe_click(driver, fallback_btn)
                time.sleep(5)
            except Exception:
                LOGGER.error("  Could not trigger final print.")
                return None

        # Chrome auto-downloads the PDF to the per-PID download dir
        # (plugins.always_open_pdf_externally=True in core/driver.py).
        # driver.current_url stays on the HTML preview page, so cookies-based
        # download would fetch HTML. Use wait_for_download instead.
        download_dir = f"/tmp/dl_{os.getpid()}"
        LOGGER.info(f"  Waiting for PDF to appear in {download_dir} (up to 60s)...")
        downloaded_path = wait_for_download(download_dir, timeout=60)

        if not downloaded_path:
            LOGGER.warning("  ⚠️ PDF never appeared in download dir — print may have failed.")
            return None

        file_date  = row_date.strftime("%m-%d-%y")
        cover_name = f"{nces}_{district.upper()}_{term_for_naming}_{file_date}_cover.pdf"
        cover_path = os.path.join(tempfile.gettempdir(), cover_name)

        shutil.move(downloaded_path, cover_path)
        LOGGER.info(f"  Cover page moved to: {cover_path}")

        if debug_check_pdf_file(cover_path):
            LOGGER.info(f"  ✅ Cover page captured: {cover_path}")
            return cover_path

        LOGGER.warning(f"  ⚠️ Moved file is not a valid PDF: {cover_path}")
        return None

    except Exception as e:
        LOGGER.error(f"  ❌ Cover page capture failed: {e}")
        return None

    finally:
        try:
            for handle in driver.window_handles:
                if handle != main_window:
                    driver.switch_to.window(handle)
                    driver.close()
            driver.switch_to.window(main_window)
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
# PDF DOWNLOAD — SESSION COOKIE METHOD (used for cover pages)
# ═════════════════════════════════════════════════════════════════════════════

def download_pdf_with_selenium_cookies(driver, pdf_url: str, filepath: str) -> bool:
    """Download a PDF by transferring the Selenium session cookies to requests."""
    LOGGER.debug(f"  Downloading PDF via requests: {pdf_url[:80]}...")
    session = requests.Session()
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


# ═════════════════════════════════════════════════════════════════════════════
# TIERED ATTACHMENT DOWNLOAD (from simbli_ctrl_f.py)
# ═════════════════════════════════════════════════════════════════════════════

def build_session_from_driver(driver) -> requests.Session:
    session = requests.Session()
    for cookie in driver.get_cookies():
        session.cookies.set(cookie["name"], cookie["value"])
    session.headers.update({
        "User-Agent": driver.execute_script("return navigator.userAgent;"),
        "Accept":     "application/pdf,application/octet-stream,*/*;q=0.8",
    })
    return session


def _purge_download_dir() -> int:
    removed = 0
    for pattern in ("*.pdf", "*.crdownload"):
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, pattern)):
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
    return removed


def _wait_for_new_pdf(timeout: int = _PRINT_TIMEOUT_SECS) -> Optional[str]:
    baseline = set(glob.glob(os.path.join(DOWNLOAD_DIR, "*.pdf")))
    deadline = time.time() + timeout
    while time.time() < deadline:
        current  = set(glob.glob(os.path.join(DOWNLOAD_DIR, "*.pdf")))
        new_pdfs = [p for p in current - baseline if os.path.getsize(p) > 512]
        if new_pdfs:
            newest = max(new_pdfs, key=os.path.getmtime)
            LOGGER.info(f"  [WATCHER] New PDF found: '{newest}' ({os.path.getsize(newest):,} bytes)")
            return newest
        time.sleep(1)
    LOGGER.error("  [WATCHER] Timeout — no new PDF appeared.")
    return None


def _is_pdf_content_type(ct: str) -> bool:
    return ct.lower().split(";")[0].strip() in _CDP_PDF_CONTENT_TYPES


def _url_looks_like_pdf(url: str) -> bool:
    path = urlparse(url).path.lower()
    return path.endswith(".pdf") or "/tempfolder/" in url.lower()


def dynamic_js_html_preview(driver) -> dict:
    """Inject JS to extract a text/element map of the page and any iframes."""
    js = """
    var report = {
        main_url: window.location.href,
        main_text: document.body ? document.body.innerText.substring(0, 600) : "No Body",
        interactive_elements: [],
        iframes_found: []
    };
    var nodes = document.querySelectorAll('a, button, span, input, div[class*="btn"], div[class*="download"]');
    nodes.forEach(function(el, i) {
        if(i < 30) {
            report.interactive_elements.push({
                tag: el.tagName, text: el.innerText ? el.innerText.trim() : "",
                id: el.id || "", class: el.className || "", value: el.value || ""
            });
        }
    });
    var frames = document.querySelectorAll('iframe');
    frames.forEach(function(f, idx) {
        var frame_info = { index: idx, id: f.id, src: f.src, inner_html_snippet: "" };
        try {
            var fd = f.contentDocument || f.contentWindow.document;
            if(fd && fd.body) { frame_info.inner_html_snippet = fd.body.innerHTML.substring(0, 1000); }
        } catch(e) { frame_info.inner_html_snippet = "CROSS-ORIGIN: " + e.message; }
        report.iframes_found.push(frame_info);
    });
    return report;
    """
    try:
        return driver.execute_script(js)
    except Exception as e:
        LOGGER.warning(f"  dynamic_js_html_preview failed: {e}")
        return {}


def _tier0_direct_pdf_url(driver, session: requests.Session,
                          href: str, filepath: str) -> Tuple[bool, str]:
    LOGGER.info("[TIER 0] Checking for direct PDF URL...")
    try:
        WebDriverWait(driver, _TIER0_URL_SETTLE_SECS).until(
            lambda d: _url_looks_like_pdf(d.current_url)
        )
        pdf_url = driver.current_url
        session = build_session_from_driver(driver)
        resp    = session.get(pdf_url, timeout=30, allow_redirects=True)
        if resp.status_code == 200 and len(resp.content) > 512:
            with open(filepath, "wb") as f:
                f.write(resp.content)
            if debug_check_pdf_file(filepath):
                return True, pdf_url
    except TimeoutException:
        LOGGER.debug(f"  [TIER 0] URL did not resolve to PDF.")
    except Exception as e:
        LOGGER.warning(f"  [TIER 0] Error: {e}")
    return False, driver.current_url


def _tier1_attachment_aspx_scrape(session: requests.Session,
                                   href: str, referer: str, filepath: str) -> bool:
    LOGGER.info(f"[TIER 1] Direct requests.Session download: {href[:80]}...")
    try:
        session.headers["Referer"] = referer
        resp = session.get(href, timeout=30, allow_redirects=True)
        ct   = resp.headers.get("Content-Type", "")
        if resp.status_code == 200 and (_is_pdf_content_type(ct) or resp.content[:4] == b"%PDF"):
            with open(filepath, "wb") as f:
                f.write(resp.content)
            if debug_check_pdf_file(filepath):
                return True
    except Exception as e:
        LOGGER.warning(f"  [TIER 1] Error: {e}")
    return False


def _tier2_dom_embed_src(driver, session: requests.Session,
                          base_url: str, filepath: str) -> bool:
    LOGGER.info("[TIER 2] Scanning DOM for embedded PDF src...")
    try:
        for selector in ("embed[src]", "iframe[src]", "object[data]", "embed[data]"):
            for el in driver.find_elements(By.CSS_SELECTOR, selector):
                src = el.get_attribute("src") or el.get_attribute("data") or ""
                if not src:
                    continue
                abs_src = urljoin(base_url, src)
                if _url_looks_like_pdf(abs_src) or "pdf" in abs_src.lower() or "raw=1" in abs_src.lower():
                    resp = session.get(abs_src, timeout=30, allow_redirects=True)
                    if resp.status_code == 200 and len(resp.content) > 512:
                        with open(filepath, "wb") as f:
                            f.write(resp.content)
                        if debug_check_pdf_file(filepath):
                            return True
    except Exception as e:
        LOGGER.warning(f"  [TIER 2] Error: {e}")
    return False


def _tier3_cdp_intercept(driver, a_tag, filepath: str) -> bool:
    LOGGER.info("[TIER 3] CDP Network intercept...")
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        safe_click(driver, a_tag)
        time.sleep(random.uniform(4, 7))

        logs = driver.get_log("performance")
        for entry in logs:
            try:
                msg = json.loads(entry["message"])["message"]
                if msg.get("method") != "Network.responseReceived":
                    continue
                params = msg.get("params", {})
                mime   = params.get("response", {}).get("mimeType", "")
                url    = params.get("response", {}).get("url", "")
                rid    = params.get("requestId")
                if _is_pdf_content_type(mime) or _url_looks_like_pdf(url):
                    body_resp = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": rid})
                    raw = (base64.b64decode(body_resp["body"])
                           if body_resp.get("base64Encoded")
                           else body_resp["body"].encode())
                    if raw and raw[:4] == b"%PDF":
                        with open(filepath, "wb") as f:
                            f.write(raw)
                        if debug_check_pdf_file(filepath):
                            return True
            except Exception:
                continue
    except Exception as e:
        LOGGER.warning(f"  [TIER 3] Error: {e}")
    finally:
        try:
            driver.execute_cdp_cmd("Network.disable", {})
        except Exception:
            pass
    return False


def _tier3_5_click_download_button(driver, filepath: str) -> bool:
    """Find and click a 'Download Attachment' button, including inside iframes."""
    LOGGER.info("[TIER 3.5] Searching for download button...")
    try:
        _purge_download_dir()
        dynamic_js_html_preview(driver)

        selectors = [
            "//span[contains(translate(text(), 'DOWNLOAD', 'download'), 'download attachment')]",
            "//a[contains(translate(text(), 'DOWNLOAD', 'download'), 'download attachment')]",
            "//button[contains(translate(., 'DOWNLOAD', 'download'), 'download attachment')]",
            "//input[@value='Download Attachment' or @type='submit']",
            "//*[contains(text(), 'Download') or contains(text(), 'download')]",
        ]

        def _find_btn(d):
            for xpath in selectors:
                for el in d.find_elements(By.XPATH, xpath):
                    try:
                        if el.is_displayed():
                            return el
                    except Exception:
                        continue
            return None

        btn = _find_btn(driver)
        if not btn:
            for idx, frame in enumerate(driver.find_elements(By.TAG_NAME, "iframe")):
                try:
                    driver.switch_to.frame(frame)
                    btn = _find_btn(driver)
                    if btn:
                        break
                    driver.switch_to.default_content()
                except Exception:
                    driver.switch_to.default_content()

        if btn:
            safe_click(driver, btn)
            downloaded_file = _wait_for_new_pdf(timeout=30)
            driver.switch_to.default_content()
            if downloaded_file:
                shutil.move(downloaded_file, filepath)
                if debug_check_pdf_file(filepath):
                    return True
        else:
            driver.switch_to.default_content()
    except Exception as e:
        LOGGER.warning(f"  [TIER 3.5] Error: {e}")
        try:
            driver.switch_to.default_content()
        except Exception:
            pass
    return False


def _tier4_kiosk_print(driver, filepath: str) -> bool:
    LOGGER.info("[TIER 4] Kiosk print (window.print())...")
    try:
        _purge_download_dir()
        driver.execute_script("window.print();")
        printed = _wait_for_new_pdf(timeout=_PRINT_TIMEOUT_SECS)
        if printed:
            shutil.move(printed, filepath)
            if debug_check_pdf_file(filepath):
                return True
    except Exception as e:
        LOGGER.warning(f"  [TIER 4] Error: {e}")
    return False


def cleanup_tmp_pdfs(max_age_hours: int = 2) -> None:
    cutoff = time.time() - (max_age_hours * 3600)
    for f in glob.glob("/tmp/*.pdf"):
        if os.path.getmtime(f) < cutoff:
            try:
                os.remove(f)
            except OSError:
                pass


# ═════════════════════════════════════════════════════════════════════════════
# COMBINE + UPLOAD (Simbli-specific AI routing + DB logging)
# ═════════════════════════════════════════════════════════════════════════════

def _merge_pdfs(pdf_paths: list, output_path: str) -> bool:
    try:
        merged = fitz.open()
        for path in pdf_paths:
            if not os.path.exists(path):
                LOGGER.warning(f"  Skipping missing PDF during merge: {path}")
                continue
            with fitz.open(path) as doc:
                merged.insert_pdf(doc)
        merged.save(output_path)
        merged.close()
        return True
    except Exception as e:
        LOGGER.error(f"  ❌ PDF merge failed: {e}")
        return False



def _resolve_folder(primary_folder_id: str) -> str:
    return primary_folder_id


def combine_and_upload_documents(
    cover_page_path: Optional[str],
    attachment_paths: List[str],
    nces: str,
    district: str,
    meeting_type: str,
    term_for_naming: str,
    span_text: str,
    row_date: datetime,
    link: str,
    prompt_df: Any,
    pdf_urls: List[str],
    attachment_names: List[str],
    meeting_record: MeetingRecord,
    category: Optional[str] = None,
    attachment_ids: Optional[List[int]] = None,
) -> None:
    """
    Merge cover page + attachment PDFs, run dupe checks, classify with Gemini,
    and upload to the appropriate Drive folder.
    """
    district_upper = district.upper()
    file_date      = row_date.strftime("%m-%d-%y")

    parts: List[str] = []
    if cover_page_path and os.path.exists(cover_page_path):
        parts.append(cover_page_path)
    parts.extend(p for p in attachment_paths if p and os.path.exists(p))

    if not parts:
        LOGGER.warning("  ⚠️  No PDFs to merge — nothing to upload.")
        return

    merged_name = f"{nces}_{district_upper}_{term_for_naming}_{file_date}_merged.pdf"
    merged_path = os.path.join(tempfile.gettempdir(), merged_name)

    try:
        if not _merge_pdfs(parts, merged_path):
            meeting_record.errors += 1
            return

        # SHA-256 dupe check
        file_hash = compute_sha256_from_file(merged_path)
        if db_check_sha256_dupe(file_hash):
            LOGGER.info("  🔁 SHA-256 dupe on merged doc — skipping upload.")
            meeting_record.dupes += 1
            return

        # Text + MinHash
        text, page_count = extract_text_with_ocr(merged_path)
        LOGGER.info(f"  Merged page count: {page_count}")
        minhash_obj = build_minhash(text)
        minhash_is_dupe, _ = db_check_minhash_dupe(minhash_obj)
        if minhash_is_dupe:
            LOGGER.info("  🔁 MinHash near-dupe on merged doc — skipping upload.")
            meeting_record.dupes += 1
            return

        # Log the merged attachment to DB
        merged_attachment_id = log_attachment_to_db(
            meeting_id=meeting_record.meeting_id,
            agenda_url=link,
            attachment_title=term_for_naming,
            attachment_link="",
            sha256_hex=file_hash,
            minhash_obj=minhash_obj,
        )

        uploaded_successfully = False

        # ── Primary category classifiers ─────────────────────────────────────
        if category:
            cat_upper = category.upper()

            if cat_upper == "BUDGET":
                prompt_text, prompt_id = get_prompt_info(prompt_df, "budget")
                ar = analyze_pdf_with_gemini_with_retry(merged_path, prompt_text, MODEL_NAME)
                usage_id = log_ai_usage(
                    prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                    tokens=ar.get("tokens", {}),
                    status="SUCCESS" if "error" not in ar.get("result", {}) else "FAILED",
                    meeting_id=meeting_record.meeting_id, document_id=merged_attachment_id,
                )
                log_document_response_to_db(
                    document_id=merged_attachment_id, usage_id=usage_id,
                    completion_text=str(ar.get("result", {})), extracted_data=ar.get("result", {}),
                )
                result_data = ar.get("result", {})
                if result_data.get("is_budget_document", False):
                    budget_type = result_data.get("document_type", "NA")
                    fiscal_year = result_data.get("fiscal_year", "NA")
                    base_name = f"{nces}_{district_upper}_{fiscal_year}_{budget_type}.pdf"
                    file_name_final = build_unique_filename(base_name)
                    uid = upload_file_to_folder(drive_service, _get_folder_id("BUDGET"), merged_path, file_name_final)
                    file_url = f"https://drive.google.com/file/d/{uid}/view?usp=sharing"
                    LOGGER.info(f"  ✅ [BUDGET] Uploaded: {file_url}")
                    meeting_record.downloaded += 1
                    uploaded_successfully = True

            elif cat_upper == "STRATEGIC_PLANNING":
                prompt_text, prompt_id = get_prompt_info(prompt_df, "strategic")
                ar = analyze_pdf_with_gemini_with_retry(merged_path, prompt_text, MODEL_NAME)
                usage_id = log_ai_usage(
                    prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                    tokens=ar.get("tokens", {}),
                    status="SUCCESS" if "error" not in ar.get("result", {}) else "FAILED",
                    meeting_id=meeting_record.meeting_id, document_id=merged_attachment_id,
                )
                log_document_response_to_db(
                    document_id=merged_attachment_id, usage_id=usage_id,
                    completion_text=str(ar.get("result", {})), extracted_data=ar.get("result", {}),
                )
                result_data = ar.get("result", {})
                if result_data.get("is_planning_document", False):
                    doc_type   = result_data.get("document_category", "NA")
                    start_year = result_data.get("start_year", "NA")
                    end_year   = result_data.get("end_year", "NA")
                    base_name = f"{nces}_{district_upper}_{start_year}-{end_year}_{doc_type}.pdf"
                    file_name_final = build_unique_filename(base_name)
                    uid = upload_file_to_folder(drive_service, _get_folder_id("STRATEGIC_PLANNING"), merged_path, file_name_final)
                    file_url = f"https://drive.google.com/file/d/{uid}/view?usp=sharing"
                    LOGGER.info(f"  ✅ [STRAT] Uploaded: {file_url}")
                    meeting_record.downloaded += 1
                    uploaded_successfully = True

            elif cat_upper == "BOND_LEVY":
                prompt_text, prompt_id = get_prompt_info(prompt_df, "bond")
                ar = analyze_pdf_with_gemini_with_retry(merged_path, prompt_text, MODEL_NAME)
                usage_id = log_ai_usage(
                    prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                    tokens=ar.get("tokens", {}),
                    status="SUCCESS" if "error" not in ar.get("result", {}) else "FAILED",
                    meeting_id=meeting_record.meeting_id, document_id=merged_attachment_id,
                )
                log_document_response_to_db(
                    document_id=merged_attachment_id, usage_id=usage_id,
                    completion_text=str(ar.get("result", {})), extracted_data=ar.get("result", {}),
                )
                result_data = ar.get("result", {})
                if result_data.get("is_bond", False):
                    bond_year = result_data.get("year", "NA")
                    bond_type = result_data.get("document_type", "NA")
                    is_update = result_data.get("is_bond_update", False)
                    f_p = "F" if result_data.get("f_p", "Unknown") == "Final" else "P"
                    if is_update:
                        base_name = f"{nces}_{district_upper}_{f_p}-{bond_type}-UPDATE-{file_date}_{bond_year}.pdf"
                    else:
                        base_name = f"{nces}_{district_upper}_{f_p}-{bond_type}_{bond_year}.pdf"
                    file_name_final = build_unique_filename(base_name)
                    uid = upload_file_to_folder(drive_service, _get_folder_id("BOND_LEVY"), merged_path, file_name_final)
                    file_url = f"https://drive.google.com/file/d/{uid}/view?usp=sharing"
                    LOGGER.info(f"  ✅ [BOND] Uploaded: {file_url}")
                    meeting_record.downloaded += 1
                    uploaded_successfully = True

            elif cat_upper == "CALENDAR":
                prompt_text, prompt_id = get_prompt_info(prompt_df, "calendar")
                ar = analyze_pdf_with_gemini_with_retry(merged_path, prompt_text, MODEL_NAME)
                usage_id = log_ai_usage(
                    prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                    tokens=ar.get("tokens", {}),
                    status="SUCCESS" if "error" not in ar.get("result", {}) else "FAILED",
                    meeting_id=meeting_record.meeting_id, document_id=merged_attachment_id,
                )
                log_document_response_to_db(
                    document_id=merged_attachment_id, usage_id=usage_id,
                    completion_text=str(ar.get("result", {})), extracted_data=ar.get("result", {}),
                )
                result_data = ar.get("result", {})
                if result_data.get("is_calendar", False):
                    calendar_year = result_data.get("academic_year", "NA")
                    base_name = f"{nces}_{district_upper}_CALENDAR_{calendar_year}.pdf"
                    file_name_final = build_unique_filename(base_name)
                    uid = upload_file_to_folder(drive_service, _get_folder_id("CALENDAR"), merged_path, file_name_final)
                    file_url = f"https://drive.google.com/file/d/{uid}/view?usp=sharing"
                    LOGGER.info(f"  ✅ [CALENDAR] Uploaded: {file_url}")
                    meeting_record.downloaded += 1
                    uploaded_successfully = True

        # ── Fallback: spending → general classifier ───────────────────────────
        if not uploaded_successfully:
            prompt_text, prompt_id = get_prompt_info(prompt_df, "spending")
            ar = analyze_pdf_with_gemini_with_retry(merged_path, prompt_text, MODEL_NAME)
            usage_id = log_ai_usage(
                prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                tokens=ar.get("tokens", {}),
                status="SUCCESS" if "error" not in ar.get("result", {}) else "FAILED",
                meeting_id=meeting_record.meeting_id, document_id=merged_attachment_id,
            )
            log_document_response_to_db(
                document_id=merged_attachment_id, usage_id=usage_id,
                completion_text=str(ar.get("result", {})), extracted_data=ar.get("result", {}),
            )
            result_data = ar.get("result", {})

            if result_data.get("is_spending_document", False):
                doc_type = result_data.get("document_type", "Spending")
                base_name = f"{nces}_{district_upper}_{file_date}_{doc_type}.pdf"
                file_name_final = build_unique_filename(base_name)
                uid = upload_file_to_folder(drive_service, _get_folder_id("SPENDING"), merged_path, file_name_final)
                file_url = f"https://drive.google.com/file/d/{uid}/view?usp=sharing"
                LOGGER.info(f"  ✅ [SPENDING] Uploaded: {file_url}")
                meeting_record.downloaded += 1
            else:
                # General classifier
                prompt_text, prompt_id = get_prompt_info(prompt_df, "supporting_doc")
                ar = analyze_pdf_with_gemini_with_retry(merged_path, prompt_text, MODEL_NAME)
                usage_id = log_ai_usage(
                    prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                    tokens=ar.get("tokens", {}),
                    status="SUCCESS" if "error" not in ar.get("result", {}) else "FAILED",
                    meeting_id=meeting_record.meeting_id, document_id=merged_attachment_id,
                )
                log_document_response_to_db(
                    document_id=merged_attachment_id, usage_id=usage_id,
                    completion_text=str(ar.get("result", {})), extracted_data=ar.get("result", {}),
                )
                result_data  = ar.get("result", {})
                extracted_cat  = result_data.get("category", "SUPPORT")
                extracted_date = parse_to_mmddyy(file_date)
                doc_type_code  = classify_doc_type(meeting_type)
                boe_type       = doc_type_code.split("-")[-1]
                base_name = f"{nces}_{district_upper}_BOE-AGENDA-{boe_type}-{extracted_cat}_{extracted_date}.pdf"
                file_name_final = build_unique_filename(base_name)

                if extracted_cat == "SUPPORT":
                    folder = _get_folder_id("MANUAL_INTERVENTION")
                elif extracted_cat in ("GOVERNANCE", "NON-RELEVANT"):
                    folder = _get_folder_id("GOVERNANCE")
                else:
                    folder = _get_folder_id("SUPPORTING")

                uid = upload_file_to_folder(drive_service, folder, merged_path, file_name_final)
                file_url = f"https://drive.google.com/file/d/{uid}/view?usp=sharing"
                LOGGER.info(f"  ✅ [GENERAL] Uploaded: {file_url}")
                meeting_record.downloaded += 1

        # Log to crawler_hash
        log_to_crawler_hash(
            nces_id=int(nces), starting_link=link, pdf_link="merged",
            downloaded=True, sha256_hex=file_hash, minhash_obj=minhash_obj,
            is_duplicate=False, drive_link=file_url if 'file_url' in dir() else None,
            file_name=file_name_final if 'file_name_final' in dir() else None,
        )

    except Exception as e:
        LOGGER.error(f"  ❌ combine_and_upload_documents failed: {e}")
        LOGGER.debug(traceback.format_exc())
        meeting_record.errors += 1
    finally:
        if os.path.exists(merged_path):
            try:
                os.remove(merged_path)
            except OSError:
                pass


# ═════════════════════════════════════════════════════════════════════════════
# ATTACHMENT PROCESSING
# ═════════════════════════════════════════════════════════════════════════════

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
    prompt_df: Any,
    att_record: AttachmentRecord,
    meeting_record: MeetingRecord,
    specialist_mode: bool = False,
    category: str = "GENERAL_SUPPORTING",
) -> Optional[str]:
    """
    Download and pipeline a single agenda attachment.
    specialist_mode=True / category="BOARD_MINUTES" → upload to Drive here, return None.
    Otherwise → return temp filepath for the caller to merge.
    """
    href             = a_tag.get_attribute("href")
    attachment_name  = a_tag.text.strip()
    file_date        = row_date.strftime("%m-%d-%y")
    district_upper   = district.upper()
    meeting_id       = meeting_record.meeting_id

    att_record.name = attachment_name
    LOGGER.debug(f"  Attachment #{attachment_index}: '{attachment_name}'  href='{href}'")

    if not href:
        LOGGER.warning(f"  Attachment #{attachment_index} has no href — skipping.")
        att_record.downloaded = OUTCOME_NO_HREF
        meeting_record.errors += 1
        return None

    handles_before            = list(driver.window_handles)
    current_url_before_switch = driver.current_url

    # URL pre-check: skip if already in doc_collection.attachments
    url_is_dupe, existing_att_id = db_check_attachment_dupe("", href)
    if url_is_dupe:
        LOGGER.info(f"  🔁 URL already in attachments (id={existing_att_id}) — skipping.")
        att_record.dupe_check_type = "attachments_url"
        att_record.passed_dupe     = False
        att_record.downloaded      = OUTCOME_DUPE
        meeting_record.dupes      += 1
        return None

    temp_name = f"{nces}_{district_upper}_{term_for_naming}_{file_date}_{attachment_index}.pdf"
    filepath  = os.path.join(tempfile.gettempdir(), temp_name)

    file_hash    = ""
    minhash_obj  = None
    is_duplicate = False
    file_url     = ""
    return_path  = None

    try:
        # ── Click the link ──────────────────────────────────────────────────
        human_like_mouse_move(driver, a_tag)
        time.sleep(1)
        safe_click(driver, a_tag)
        time.sleep(random.uniform(6, 11))

        new_tab = wait_for_new_tab(driver, handles_before, timeout=15)
        if new_tab:
            driver.switch_to.window(new_tab)
        time.sleep(random.uniform(3, 17))

        pdf_url = driver.current_url
        att_record.pdf_url = pdf_url
        LOGGER.info(f"  PDF URL resolved: {pdf_url}")

        session = build_session_from_driver(driver)

        # ── Tiered download ─────────────────────────────────────────────────
        success   = False
        tier_used = "—"

        ok, final_pdf_url = _tier0_direct_pdf_url(driver, session, href, filepath)
        if ok:
            tier_used = "Tier0"; success = True; pdf_url = final_pdf_url

        if not success:
            if _tier1_attachment_aspx_scrape(session, href, current_url_before_switch, filepath):
                tier_used = "Tier1"; success = True; pdf_url = href

        if not success:
            if _tier2_dom_embed_src(driver, session, driver.current_url, filepath):
                tier_used = "Tier2"; success = True

        if not success:
            try:
                if driver.current_window_handle == main_window:
                    if _tier3_cdp_intercept(driver, a_tag, filepath):
                        tier_used = "Tier3"; success = True
            except StaleElementReferenceException:
                pass

        if not success:
            if _tier3_5_click_download_button(driver, filepath):
                tier_used = "Tier3.5"; success = True

        if not success:
            if _tier4_kiosk_print(driver, filepath):
                tier_used = "Tier4"; success = True

        if not success:
            raise RuntimeError(
                f"All download tiers failed for attachment #{attachment_index}"
            )

        LOGGER.info(
            f"  ✅ Downloaded via {tier_used} "
            f"({os.path.getsize(filepath):,} bytes)"
        )
        att_record.tier_used = tier_used

        # ── Validate PDF ────────────────────────────────────────────────────
        if not debug_check_pdf_file(filepath):
            att_record.downloaded = OUTCOME_NOT_PDF
            meeting_record.errors += 1
            log_to_crawler_hash(
                nces_id=int(nces), starting_link=link, pdf_link=pdf_url,
                downloaded=False, sha256_hex="", minhash_obj=None,
                is_duplicate=False, drive_link=None, file_name=None,
            )
            return None

        # ── SHA-256 dupe check ──────────────────────────────────────────────
        file_hash = compute_sha256_from_file(filepath)
        if db_check_sha256_dupe(file_hash):
            LOGGER.info("  🔁 SHA-256 dupe — skipping.")
            is_duplicate               = True
            att_record.dupe_check_type = "sha256"
            att_record.passed_dupe     = False
            att_record.downloaded      = OUTCOME_DUPE
            meeting_record.dupes      += 1
            return None

        # ── OCR + MinHash dupe check ────────────────────────────────────────
        text, page_count = extract_text_with_ocr(filepath)
        LOGGER.info(f"  Page count: {page_count}")
        minhash_obj = build_minhash(text)
        minhash_is_dupe, matching_link = db_check_minhash_dupe(minhash_obj)
        if minhash_is_dupe:
            LOGGER.info(
                f"  🔁 MinHash near-dupe (≥{MINHASH_SIM_THRESHOLD:.0%}) "
                f"→ {matching_link} — skipping."
            )
            is_duplicate               = True
            att_record.dupe_check_type = "minhash"
            att_record.passed_dupe     = False
            att_record.downloaded      = OUTCOME_DUPE
            meeting_record.dupes      += 1
            log_to_crawler_hash(
                nces_id=int(nces), starting_link=link, pdf_link=pdf_url,
                downloaded=False, sha256_hex=file_hash, minhash_obj=minhash_obj,
                is_duplicate=True, drive_link=None, file_name=None,
            )
            return None

        # Novel — log to attachments table
        attachment_id = log_attachment_to_db(
            meeting_id=meeting_id,
            agenda_url=link,
            attachment_title=attachment_name,
            attachment_link=pdf_url,
            sha256_hex=file_hash,
            minhash_obj=minhash_obj,
        )
        att_record.db_id           = attachment_id
        att_record.passed_dupe     = True
        att_record.dupe_check_type = "none"

        # ── Minutes specialist path ─────────────────────────────────────────
        if specialist_mode and category.upper() == "BOARD_MINUTES":
            prompt_text, prompt_id = get_prompt_info(prompt_df, "minutes_and_agendas")
            ar = analyze_pdf_with_gemini_with_retry(filepath, prompt_text, MODEL_NAME)
            usage_id = log_ai_usage(
                prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                tokens=ar.get("tokens", {}),
                status="SUCCESS" if "error" not in ar.get("result", {}) else "FAILED",
            )
            result_data = ar.get("result", {})
            is_minutes  = result_data.get("is_minutes", False)

            if not is_minutes:
                att_record.downloaded = OUTCOME_DOWNLOADED
                return_path = filepath
            else:
                MAPPING = {
                    "REGULAR": "BOE-REG", "COMMITTEE": "BOE-COM", "WORK": "BOE-WS",
                    "FINANCE": "BOE-FIN", "PUBLIC": "BOE-PUB", "EXECUTIVE": "BOE-EXE",
                    "SPECIAL": "BOE-SP", "AGENDA": "BOE-AGENDA",
                }
                raw_type     = result_data.get("meeting_type", "").upper()
                doc_type     = MAPPING.get(raw_type) or classify_doc_type(meeting_type)
                meeting_date_raw  = result_data.get("meeting_date")
                ai_date      = parse_to_mmddyy(meeting_date_raw) if meeting_date_raw else None
                if not ai_date:
                    ai_date = parse_to_mmddyy(extract_first_date_from_pdf(filepath))
                final_date_str = ai_date or row_date.strftime("%m%d%y")
                base_name      = f"{nces}_{district_upper}_{doc_type}_{final_date_str}.pdf"
                file_name_final = build_unique_filename(base_name)

                uid = upload_file_to_folder(drive_service, _get_folder_id("MINUTES"), filepath, file_name_final)
                file_url = f"https://drive.google.com/file/d/{uid}/view?usp=sharing"
                LOGGER.info(f"  ✅ [MINUTES] Uploaded: {file_url}")

                att_record.downloaded  = OUTCOME_DOWNLOADED
                att_record.file_name   = file_name_final
                meeting_record.downloaded += 1

                log_uploaded_document(
                    meeting_id=meeting_id,
                    nces_id=nces,
                    file_name=file_name_final,
                    drive_folder="MINUTES",
                    google_drive_url=file_url,
                    sha256_hash=file_hash,
                    minhash_obj=minhash_obj,
                    category="MINUTES",
                    doc_type=doc_type,
                    attachment_id=attachment_id,
                )
                log_to_crawler_hash(
                    nces_id=int(nces), starting_link=link, pdf_link=pdf_url,
                    downloaded=True, sha256_hex=file_hash, minhash_obj=minhash_obj,
                    is_duplicate=False, drive_link=file_url, file_name=file_name_final,
                )
                return_path = None

        else:
            # Non-minutes: return filepath for caller to merge
            att_record.downloaded = OUTCOME_DOWNLOADED
            return_path = filepath

    except Exception as e:
        LOGGER.error(f"  ❌ Error processing attachment #{attachment_index}: {e}")
        LOGGER.debug(traceback.format_exc())
        log_error_to_db(
            error_type="ATTACHMENT_ERROR",
            message=str(e),
            stack_trace=traceback.format_exc(),
            nces_id=nces,
            meeting_id=meeting_id,
            document_id=getattr(att_record, "db_id", None),
        )
        if att_record.downloaded == "—":
            att_record.downloaded  = OUTCOME_ERROR
            meeting_record.errors += 1
        log_to_crawler_hash(
            nces_id=int(nces), starting_link=link, pdf_link=pdf_url,
            downloaded=False, sha256_hex=file_hash, minhash_obj=minhash_obj,
            is_duplicate=is_duplicate, drive_link=None, file_name=None,
        )
        return_path = None

    finally:
        # Delete temp file if not being returned to caller
        if return_path is None and os.path.exists(filepath):
            try:
                os.remove(filepath)
            except OSError:
                pass

        # Restore driver state
        try:
            current_url_after = driver.current_url
            if current_url_after != current_url_before_switch:
                driver.close()
                driver.switch_to.window(main_window)
                time.sleep(random.uniform(1, 5))
                time.sleep(random.uniform(3, 7))
            else:
                pass
        except Exception:
            pass
        time.sleep(random.uniform(1, 5))

    return return_path


# ═════════════════════════════════════════════════════════════════════════════
# AGENDA TRAVERSAL
# ═════════════════════════════════════════════════════════════════════════════

def search_and_download_agenda_attachments(
    driver,
    nces: str,
    district: str,
    row_date: datetime,
    meeting_type: str,
    link: str,
    prompt_df: Any,
    meeting_record: MeetingRecord,
) -> None:
    """
    Iterate ALL Simbli agenda items. For each item:
    1. Capture cover page → run AI pre-classification.
    2. Download attachments.
    3. Minutes items upload individually; policy items upload per-attachment;
       all others are merged into one PDF and classified.
    """
    main_window = driver.current_window_handle
    meeting_id  = meeting_record.meeting_id
    LOGGER.info(f"  Processing meeting_id: {meeting_id}")

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".agl-tree"))
        )
    except TimeoutException:
        LOGGER.error("  ❌ Agenda tree '.agl-tree' did not load.")
        return

    buttons = driver.find_elements(By.CSS_SELECTOR, ".agl-tree button span")
    LOGGER.info(f"  Found {len(buttons)} agenda items.")

    for span_index, span in enumerate(buttons):
        cover_path = None
        try:
            raw_text = span.text.strip()
            if not raw_text:
                continue
            LOGGER.info(f"  [{span_index}] Processing: '{raw_text}'")

            # Click item to load details
            button = span.find_element(By.XPATH, "..")
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});", button
            )
            time.sleep(1)
            safe_click(driver, button)
            time.sleep(random.uniform(3, 6))

            term_for_naming = re.sub(r"[^\w\s-]", "", raw_text).replace(" ", "_")[:30]

            # ── Cover page + AI pre-classification ──────────────────────────
            cover_path = _get_cover_page_pdf(
                driver, main_window, nces, district, term_for_naming, row_date
            )
            router_result = {"is_standard": True, "category": "GENERAL_SUPPORTING"}

            if cover_path:
                cover_hash  = compute_sha256_from_file(cover_path)
                cover_text, _ = extract_text_with_ocr(cover_path)
                cover_mh    = build_minhash(cover_text)
                cover_id    = log_cover_page_to_db(
                    nces_id=nces, meeting_id=meeting_id,
                    agenda_item_text=raw_text, drive_link="",
                    sha256_hex=cover_hash, minhash_obj=cover_mh,
                )
                router_prompt, prompt_id = get_prompt_info(prompt_df, "pre-classification")
                ai_routing = analyze_pdf_with_gemini_with_retry(
                    cover_path, router_prompt, MODEL_NAME
                )
                router_result = ai_routing.get("result", {})
                usage_id = log_ai_usage(
                    prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                    tokens=ai_routing.get("tokens", {}),
                    status="SUCCESS" if "error" not in router_result else "FAILED",
                )
                if cover_id:
                    log_document_response_to_db(
                        document_id=cover_id, usage_id=usage_id,
                        completion_text=str(router_result),
                        extracted_data=router_result if isinstance(router_result, dict) else {},
                    )

            use_specialist  = router_result.get("is_minute", False)
            target_category = router_result.get("category", "GENERAL_SUPPORTING")
            is_policy_item  = _cover_page_is_policy(cover_path)
            LOGGER.info(
                f"  [{span_index}] Route: {target_category} | "
                f"Minutes: {use_specialist} | Policy: {is_policy_item}"
            )

            # ── Find attachments ─────────────────────────────────────────────
            attachment_links = driver.find_elements(By.CSS_SELECTOR, "a.supportingDocText")
            if not attachment_links:
                attachment_links = driver.find_elements(By.CSS_SELECTOR, "a.ptitle")

            if not attachment_links:
                if cover_path:
                    LOGGER.info("    No attachments — uploading cover page only.")
                    combine_and_upload_documents(
                        cover_path, [], nces, district, meeting_type,
                        term_for_naming, raw_text, row_date, link, prompt_df,
                        [], [], meeting_record, category=None, attachment_ids=[],
                    )
                continue

            meeting_record.total += len(attachment_links)

            # ── Process each attachment ──────────────────────────────────────
            collected_paths, collected_urls, collected_names = [], [], []

            for i, a_tag in enumerate(attachment_links, start=1):
                att_record = AttachmentRecord(index=i, name=a_tag.text.strip())
                meeting_record.attachments.append(att_record)

                result_path = _process_single_attachment(
                    driver, a_tag, i, main_window,
                    nces, district, meeting_type, term_for_naming,
                    raw_text, row_date, link, prompt_df,
                    att_record, meeting_record,
                    specialist_mode=use_specialist,
                    category=target_category,
                )

                if result_path is None:
                    continue

                if is_policy_item:
                    # Upload cover + this attachment individually
                    LOGGER.info(f"  [POLICY] Uploading attachment #{i} individually.")
                    try:
                        combine_and_upload_documents(
                            cover_path, [result_path],
                            nces, district, meeting_type, term_for_naming,
                            raw_text, row_date, link, prompt_df,
                            [att_record.pdf_url or ""], [att_record.name or ""],
                            meeting_record, category=target_category,
                            attachment_ids=[att_record.db_id] if att_record.db_id else [],
                        )
                    except Exception as e:
                        LOGGER.error(f"  ❌ Policy upload failed for #{i}: {e}")
                        meeting_record.errors += 1
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

            # ── Bulk merge for standard path ─────────────────────────────────
            if collected_paths:
                LOGGER.info(f"  Finalising merged package for: {term_for_naming}")
                collected_att_ids = [
                    att.db_id for att in meeting_record.attachments
                    if att.db_id is not None
                ]
                combine_and_upload_documents(
                    cover_path, collected_paths,
                    nces, district, meeting_type, term_for_naming,
                    raw_text, row_date, link, prompt_df,
                    collected_urls, collected_names,
                    meeting_record, category=target_category,
                    attachment_ids=collected_att_ids,
                )
                for p in collected_paths:
                    if os.path.exists(p):
                        try:
                            os.remove(p)
                        except OSError:
                            pass

            time.sleep(random.uniform(2, 4))

        except StaleElementReferenceException:
            LOGGER.warning(f"  Span[{span_index}] went stale.")
            continue
        except Exception as e:
            LOGGER.error(f"  ❌ Failed processing item '{raw_text}': {e}")
            log_error_to_db(
                error_type="AGENDA_ITEM_ERROR",
                message=str(e),
                stack_trace=traceback.format_exc(),
                nces_id=nces,
                meeting_id=meeting_id,
            )
        finally:
            if cover_path and os.path.exists(cover_path):
                try:
                    os.remove(cover_path)
                except OSError:
                    pass
