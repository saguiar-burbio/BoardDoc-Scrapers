# ─────────────────────────────────────────────────────────────────────────────
# crawlers/boardbook/scraper.py
# BoardBook Premier — platform-specific DOM navigation and attachment download.
#
# Shared driver utilities (create_undetected_driver, _is_session_alive, etc.)
# live in core/driver.py.  Everything here is BoardBook-specific.
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import re
import time
from typing import List, Optional, Set
from urllib.parse import urlparse, parse_qs
import random

import requests
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    InvalidSessionIdException,
)

from config.settings import MODEL_NAME
from core.database import (
    db_check_sha256_dupe,
    db_check_minhash_dupe,
    get_drive_folder_map,
    get_prompt_info,
    db_get_filename_count,
    log_crawl_attachment,
)


def _get_folder_id(name: str) -> str:
    return get_drive_folder_map().get(name, "")
from core.driver import (
    _is_session_alive,
    wait_for_download,
    close_extra_tabs,
)

LOGGER = logging.getLogger("simbli_minutes")


# ═════════════════════════════════════════════════════════════════════════════
# 1. BOARDBOOK-SPECIFIC VISUAL EXTRACTORS
# ═════════════════════════════════════════════════════════════════════════════

def _is_cover_page_blank(driver: uc.Chrome, nces_id: str) -> bool:
    """Checks the Simbli data-scroll container for visual/textual content."""
    try:
        wait = WebDriverWait(driver, 10)
        container = wait.until(EC.visibility_of_element_located((By.CSS_SELECTOR, "div.data-scroll")))

        raw_text  = container.text.strip()
        doc_links = container.find_elements(By.CSS_SELECTOR, "a.supportingDocText, a[href*='Attachment.aspx']")
        all_links = container.find_elements(By.CSS_SELECTOR, "a")

        LOGGER.debug(f" [COVER PAGE] text: {bool(raw_text)} | docs: {len(doc_links)} | links: {len(all_links)}")

        if raw_text or len(doc_links) > 0 or len(all_links) > 0:
            from core.document_utils import extract_and_log_contact_info
            extract_and_log_contact_info(driver, nces_id)
            return False

        return True
    except Exception as e:
        LOGGER.debug(f" [COVER PAGE] Timeout or missing container: {e}")
        return True


def _boardbook_viewer_download(driver: uc.Chrome, wait: WebDriverWait) -> bool:
    """Navigates the BoardBook WebViewer iframe to trigger the download button."""
    try:
        LOGGER.debug("[VIEWER] Switching into webviewer-1 iframe...")
        iframe = wait.until(EC.presence_of_element_located((By.ID, "webviewer-1")))
        driver.switch_to.frame(iframe)

        wait.until(EC.invisibility_of_element_located((By.CSS_SELECTOR, "div.ProgressModal")))

        menu_btn = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, 'button[data-element="menuButton"]')))
        driver.execute_script("arguments[0].click();", menu_btn)

        download_btn = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "button[data-element='downloadButton']")))
        driver.execute_script("arguments[0].click();", download_btn)

        driver.switch_to.default_content()
        return True
    except Exception as e:
        LOGGER.warning(f"[VIEWER] Viewer download failed: {e}")
        driver.switch_to.default_content()
        return False


# ═════════════════════════════════════════════════════════════════════════════
# 2. SINGLE ATTACHMENT PROCESSING ENGINE
# ═════════════════════════════════════════════════════════════════════════════

def _process_single_attachment(
    driver: uc.Chrome,
    href: str,
    att_name: str,
    attachment_index: int,
    main_window: str,
    nces: str,
    district: str,
    meeting_type: str,
    term_for_naming: str,
    span_text: str,
    row_date: any,
    link: str,
    prompt_df,
    meeting_record,
    specialist_mode: bool = False,
    category: str = "GENERAL_SUPPORTING",
    meeting_id: Optional[int] = None,
) -> Optional[str]:
    """Executes multi-strategy payload downloads against a single attachment link."""
    LOGGER.info(f"\n{'='*80}\n [{district}] | [ATT #{attachment_index}] START\nhref: {href}\nname: {att_name}")

    wait = WebDriverWait(driver, 20)
    download_dir = f"/tmp/dl_{os.getpid()}"
    meeting_page_url = driver.current_url

    if not href or "formstack" in href.lower():
        LOGGER.warning(f" [{district}] | [SKIP] Invalid or insecure link.")
        log_crawl_attachment(
            meeting_id=meeting_id, nces_id=nces, pdf_url=href or "No URL",
            attachment_name=att_name, outcome="❌ Not a PDF", is_duplicate=False
        )
        return None

    clean_term = re.sub(r'[^\w\s-]', '', term_for_naming[:30]).strip().replace(' ', '_')
    file_date = row_date.strftime("%m-%d-%y")
    district_upper = district.upper()

    base_filename = f"{nces}_{district_upper}_{clean_term}_{file_date}_{attachment_index}.pdf"
    final_path = os.path.join(download_dir, base_filename)

    try:
        if not _is_session_alive(driver):
            raise InvalidSessionIdException("Driver session died unexpectedly.")

        LOGGER.debug(f" [{district}] | [STEP 1] Navigating to asset...")
        driver.get(href)
        wait.until(lambda d: d.execute_script("return document.readyState") == "complete")

        LOGGER.debug(f" [{district}] | [STEP 2] Attempting viewer download...")
        viewer_success = _boardbook_viewer_download(driver, wait)

        if viewer_success:
            downloaded_file = wait_for_download(download_dir)
            if not downloaded_file:
                LOGGER.error(f" [{district}] | [ERROR] Viewer triggered but no file materialized.")
                log_crawl_attachment(
                    meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                    attachment_name=att_name, outcome="❌ DL Failed", is_duplicate=False
                )
                return None
            os.rename(downloaded_file, final_path)
            LOGGER.info(f"[{district}] | [SUCCESS] WebViewer delivery → {final_path}")
        else:
            LOGGER.info(f" [{district}] | [STEP 3] Falling back to HTTP request...")
            parsed = urlparse(href)
            params = parse_qs(parsed.query)
            file_id = params.get("file", [None])[0]

            if not file_id:
                LOGGER.error(f"[{district}] | [ERROR] Could not extract file_id from URL.")
                log_crawl_attachment(
                    meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                    attachment_name=att_name, outcome="❌ DL Failed", is_duplicate=False
                )
                return None

            selenium_cookies = driver.get_cookies()
            session = requests.Session()
            for cookie in selenium_cookies:
                session.cookies.set(cookie["name"], cookie["value"])

            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": href,
            }
            downloaded = False

            view_url = f"https://meetings.boardbook.org/Meetings/ViewAttachment/{file_id}"
            try:
                r = session.get(view_url, headers=headers, timeout=30)
                if r.status_code == 200 and len(r.content) > 1000:
                    with open(final_path, "wb") as f:
                        f.write(r.content)
                    LOGGER.info(f"[{district}] | [SUCCESS] ViewAttachment → {final_path}")
                    downloaded = True
            except Exception as e:
                LOGGER.warning(f"[{district}] | [WARN] ViewAttachment error: {e}")

            if not downloaded:
                path_parts = parsed.path.rstrip("/").split("/")
                org_id = path_parts[-1] if path_parts[-1].isdigit() else None
                if org_id:
                    dl_url = f"https://meetings.boardbook.org/Documents/DownloadPDF/{file_id}?org={org_id}"
                    try:
                        r = session.get(dl_url, headers=headers, timeout=30)
                        r.raise_for_status()
                        with open(final_path, "wb") as f:
                            f.write(r.content)
                        LOGGER.info(f"[{district}] | [SUCCESS] DownloadPDF → {final_path}")
                        downloaded = True
                    except Exception as e:
                        LOGGER.warning(f"[{district}] | [WARN] DownloadPDF error: {e}")

            if not downloaded:
                LOGGER.error(f"[{district}] | [ERROR] All download strategies failed.")
                log_crawl_attachment(
                    meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                    attachment_name=att_name, outcome="❌ DL Failed", is_duplicate=False
                )
                return None

        if not os.path.exists(final_path):
            LOGGER.error(f"[{district}] | [ERROR] Final path vanished after download.")
            log_crawl_attachment(
                meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                attachment_name=att_name, outcome="❌ DL Failed", is_duplicate=False
            )
            return None

        from core.document_utils import debug_check_pdf_file, is_pdf_corrupted
        if not debug_check_pdf_file(final_path) or is_pdf_corrupted(final_path):
            LOGGER.error(f"[{district}] | [ERROR] Downloaded file is corrupted or not a PDF.")
            log_crawl_attachment(
                meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                attachment_name=att_name, outcome="❌ Not a PDF", is_duplicate=False
            )
            os.remove(final_path)
            return None

        LOGGER.info(f"[FILE] Size: {os.path.getsize(final_path)}")

        from core.hashing import compute_sha256_from_file, build_minhash, serialize_minhash
        from core.pdf_functions import extract_text_with_ocr
        file_hash = compute_sha256_from_file(final_path)
        LOGGER.info(f"[{district}] | [HASH] {file_hash}")

        if db_check_sha256_dupe(file_hash):
            LOGGER.info(f"[{district}] | [DUPE] Exact SHA-256 match — dropping.")
            log_crawl_attachment(
                meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                attachment_name=att_name, outcome="🔁 Duplicate",
                sha256_hash=file_hash, is_duplicate=True
            )
            os.remove(final_path)
            return None

        text, page_count = extract_text_with_ocr(final_path)
        minhash_obj = build_minhash(text)
        minhash_str = serialize_minhash(minhash_obj)

        minhash_is_dupe, matching_link = db_check_minhash_dupe(minhash_obj)
        if minhash_is_dupe:
            LOGGER.info(f"[{district}] | [DUPE] Soft MinHash match: {matching_link}")
            log_crawl_attachment(
                meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                attachment_name=att_name, outcome="🔁 Duplicate",
                sha256_hash=file_hash, minhash_json=minhash_str, is_duplicate=True
            )
            os.remove(final_path)
            return None

        log_crawl_attachment(
            meeting_id=meeting_id, nces_id=nces, pdf_url=href,
            attachment_name=att_name, outcome="✅ Downloaded",
            sha256_hash=file_hash, minhash_json=minhash_str, is_duplicate=False
        )

        if specialist_mode and category.upper() == "BOARD_MINUTES":
            LOGGER.info(f"[{district}] | [AI] Running minutes AI pipeline...")
            from core.analyzer import analyze_pdf_with_gemini_with_retry
            from core.document_utils import build_unique_filename
            from core.pdf_functions import parse_to_mmddyy
            from core.google_functions import upload_file_to_folder

            prompt_text, _ = get_prompt_info(prompt_df, "minutes_and_agendas")
            analysis = analyze_pdf_with_gemini_with_retry(final_path, prompt_text, MODEL_NAME)
            res = analysis.get("result", {})
            LOGGER.info(f"[AI RESULT] {res}")

            if not res.get("is_minutes", False):
                return final_path

            MAPPING = {"REGULAR": "BOE-REG", "WORK": "BOE-WS", "SPECIAL": "BOE-SP"}
            doc_type = MAPPING.get(res.get("meeting_type", "").upper(), "BOE-MIN")
            ai_date = parse_to_mmddyy(res.get("meeting_date"))
            final_date = ai_date if ai_date else file_date

            final_name = build_unique_filename(f"{nces}_{district_upper}_{doc_type}_{final_date}.pdf")

            from __main__ import drive_service
            file_id = upload_file_to_folder(drive_service, _get_folder_id("MINUTES"), final_path, final_name)

            LOGGER.info(f"[{district}] | [UPLOAD] {final_name} → {file_id}")
            meeting_record.downloaded += 1
            return None

        LOGGER.info(f"[{district}] | [RETURN] Novel file → {final_path}")
        return final_path

    except Exception as e:
        LOGGER.error(f"[{district}] | [ERROR] Attachment processing crashed: {e}")
        log_crawl_attachment(
            meeting_id=meeting_id, nces_id=nces, pdf_url=href,
            attachment_name=att_name, outcome="❌ Error", is_duplicate=False
        )
        return None
    finally:
        try:
            driver.switch_to.default_content()
            driver.get(meeting_page_url)
            wait.until(EC.presence_of_element_located((By.CLASS_NAME, "agenda-items-section")))
            LOGGER.debug(f"[{district}] | [NAV] Restored to agenda tree.")
        except Exception as nav_err:
            LOGGER.error(f"[{district}] | [NAV ERROR] Page recovery failed: {nav_err}")


# ═════════════════════════════════════════════════════════════════════════════
# 3. GRANULAR AGENDA RUNTIME HARVESTER
# ═════════════════════════════════════════════════════════════════════════════

def search_and_download_agenda_attachments(
    driver: uc.Chrome, nces: str, district: str, meeting_title: str, meeting_url: str,
    row_date: any, meeting_type: str, link: str, prompt_df, processed_urls: Set[str],
    meeting_record, meeting_id: Optional[int] = None,
) -> None:
    """Traverses BoardBook nested tree and processes every leaf agenda item."""
    main_window = driver.current_window_handle
    LOGGER.info(f"\n{'═'*60}\n  [{district}] 🚀 STARTING BOARDBOOK SCRAPE: {meeting_title}\n{'═'*60}")

    def get_valid_rows():
        all_rows = driver.find_elements(By.CSS_SELECTOR, "tr.agenda-item-information")
        parent_ids = driver.execute_script("""
            var ids = [];
            document.querySelectorAll('tr.agenda-item-information').forEach(row => {
                var classes = row.className.split(' ');
                classes.forEach(cls => {
                    if (cls.startsWith('agenda-item-children-of-')) {
                        var id = cls.replace('agenda-item-children-of-', '');
                        if (id !== "0" && !ids.includes(id)) { ids.push(id); }
                    }
                });
            });
            return ids;
        """)
        valid_blocks = []
        for row in all_rows:
            item_id = row.get_attribute("data-agendaitemid")
            if item_id not in parent_ids:
                valid_blocks.append(row)
        return valid_blocks

    try:
        blocks = get_valid_rows()
        LOGGER.info(f" [{district}] Found {len(blocks)} granular items to process.")

        block_index = 0
        while block_index < len(blocks):
            blocks = get_valid_rows()
            if block_index >= len(blocks):
                break

            block = blocks[block_index]
            cover_path = None

            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", block)

            attach_div_id = block.get_attribute("data-agendaitemid")
            try:
                WebDriverWait(driver, 5).until(
                    lambda d: len(
                        block.find_elements(By.CSS_SELECTOR, "div.Attachments a, div.Links a")
                    ) > 0
                    or block.find_elements(By.CSS_SELECTOR, f"#attachments-for-{attach_div_id}")
                )
            except TimeoutException:
                pass

            attachment_links = block.find_elements(By.CSS_SELECTOR, "div.Attachments a, div.Links a")
            block_text = driver.execute_script("""
                var row = arguments[0];
                var clone = row.cloneNode(true);
                var noise = clone.querySelectorAll('.Attachments, .Links, input, i, .agenda-item-checkbox-cell, th');
                noise.forEach(el => el.remove());
                return clone.innerText.trim();
            """, block).strip()

            if not block_text and not attachment_links:
                LOGGER.info(f"    [{district}] ⏩ ITEM {block_index + 1}: Empty — skipping.")
                block_index += 1
                continue

            display_text = block_text if block_text else f"Agenda Item {block_index + 1}"
            term_for_naming = re.sub(r'[^\w\s-]', '', display_text[:60]).strip().replace(' ', '_')
            LOGGER.info(f"\n   │ [{district}] | ITEM {block_index + 1}/{len(blocks)}: {display_text[:50]}...")

            try:
                from core.document_utils import _get_cover_page_pdf, _cover_page_is_policy, combine_and_upload_documents
                from core.analyzer import analyze_pdf_with_gemini_with_retry

                cover_path = _get_cover_page_pdf(
                    driver, main_window, nces, district, term_for_naming,
                    row_date, block, meeting_title, meeting_url
                )
                router_result = {"is_standard": True, "category": "GENERAL_SUPPORTING"}

                if cover_path:
                    router_prompt, _ = get_prompt_info(prompt_df, "pre-classification")
                    ai_routing = analyze_pdf_with_gemini_with_retry(cover_path, router_prompt, MODEL_NAME)
                    router_result = ai_routing.get("result", {})
                    LOGGER.debug(
                        f"   [{district}] | [AI] category={router_result.get('category', 'Unknown')} | "
                        f"reasoning={router_result.get('reasoning', '-')[:80]}"
                    )

                use_specialist  = router_result.get("is_minute", False)
                target_category = router_result.get("category", "GENERAL_SUPPORTING")
                is_policy       = _cover_page_is_policy(cover_path)

                LOGGER.debug(f"   [{block_index}] category={target_category} | specialist={use_specialist} | policy={is_policy}")

                if not attachment_links:
                    LOGGER.info(f"      [{district}] | [TEXT-ONLY] Uploading cover page only.")
                    combine_and_upload_documents(
                        cover_page_path=cover_path, attachment_paths=[],
                        nces=nces, district=district, meeting_type=meeting_type,
                        term_for_naming=term_for_naming, span_text=block_text,
                        row_date=row_date, link=link, prompt_df=prompt_df,
                        pdf_urls=[], attachment_names=[],
                        meeting_record=meeting_record, meeting_id=meeting_id,
                        category=target_category
                    )
                else:
                    collected_paths = []
                    link_data = []
                    for a in attachment_links:
                        href = a.get_attribute("href")
                        name = a.find_element(By.CLASS_NAME, "fileNameValue").text if a.find_elements(By.CLASS_NAME, "fileNameValue") else a.text
                        if href and "formstack" not in href.lower():
                            link_data.append((href, name))

                    for i, (href, name) in enumerate(link_data, 1):
                        if href in processed_urls:
                            continue
                        processed_urls.add(href)

                        res = _process_single_attachment(
                            driver, href, name, i, main_window, nces, district,
                            meeting_type, term_for_naming, block_text, row_date,
                            link, prompt_df, meeting_record, use_specialist,
                            target_category, meeting_id=meeting_id
                        )

                        if res:
                            if is_policy:
                                LOGGER.info(f"      [{district}] | 📋 Policy upload: {name}")
                                combine_and_upload_documents(
                                    cover_page_path=cover_path, attachment_paths=[res],
                                    nces=nces, district=district, meeting_type=meeting_type,
                                    term_for_naming=term_for_naming, span_text=block_text,
                                    row_date=row_date, link=link, prompt_df=prompt_df,
                                    pdf_urls=[href], attachment_names=[name],
                                    meeting_record=meeting_record, meeting_id=meeting_id,
                                    category=target_category
                                )
                                if os.path.exists(res):
                                    os.remove(res)
                            else:
                                collected_paths.append(res)

                    if collected_paths:
                        LOGGER.info(f"      [{district}] | 📤 Merging {len(collected_paths)} files.")
                        combine_and_upload_documents(
                            cover_page_path=cover_path, attachment_paths=collected_paths,
                            nces=nces, district=district, meeting_type=meeting_type,
                            term_for_naming=term_for_naming, span_text=block_text,
                            row_date=row_date, link=link, prompt_df=prompt_df,
                            pdf_urls=[l[0] for l in link_data],
                            attachment_names=[l[1] for l in link_data],
                            meeting_record=meeting_record, meeting_id=meeting_id,
                            category=target_category
                        )
                        for p in collected_paths:
                            if os.path.exists(p):
                                os.remove(p)

                block_index += 1

            except (StaleElementReferenceException, NoSuchElementException):
                LOGGER.warning("   ⚠️ DOM refresh detected — retrying current index.")
                time.sleep(3)
                continue

            finally:
                close_extra_tabs(driver, main_window)

                if cover_path and os.path.exists(cover_path):
                    try:
                        os.remove(cover_path)
                    except Exception:
                        pass

    except Exception as e:
        LOGGER.error(f"   ❌ Critical failure in BoardBook scan: {e}")

    LOGGER.info(f"\n{'═'*60}\n   [{district}] | 🏁 FINISHED SCRAPE\n{'═'*60}")
