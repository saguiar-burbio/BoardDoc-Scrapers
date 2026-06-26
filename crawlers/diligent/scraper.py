# ─────────────────────────────────────────────────────────────────────────────
# crawlers/diligent/scraper.py
# Diligent BoardDocs — platform-specific scraping layer.
#
# Shared infrastructure is imported from core/.
# Only Diligent-specific DOM logic lives here.
# ─────────────────────────────────────────────────────────────────────────────

import base64
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
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer
from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from config.settings import MODEL_NAME
from core.analyzer import analyze_pdf_with_gemini_with_retry
from core.database import (
    db_check_minhash_dupe,
    db_check_sha256_dupe,
    get_drive_folder_map,
    get_prompt_info,
    log_ai_usage,
    log_crawl_attachment,
    log_error_to_db,
)


def _get_folder_id(name: str) -> str:
    return get_drive_folder_map().get(name, "")
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
from core.pdf_functions import (
    extract_first_date_from_pdf,
    extract_text_with_ocr,
    parse_to_mmddyy,
)

LOGGER = logging.getLogger("simbli_minutes")

# drive_service is injected by crawlers/diligent/main.py at startup:
#   import crawlers.diligent.scraper as dil_scraper
#   dil_scraper.drive_service = drive_service
drive_service = None


# =============================================================================
# SECTION 1 — PDF DOWNLOAD
# =============================================================================

def download_pdf(driver, pdf_url: str, filename: str) -> Optional[str]:
    """
    Download a Diligent attachment to a local temp file.

    Tries a direct HTTP download first. Falls back to Chrome's
    Page.printToPDF CDP command for HTML-rendered attachment pages.
    """
    file_path = os.path.join(tempfile.gettempdir(), filename)

    try:
        try:
            head         = requests.head(pdf_url, allow_redirects=True, timeout=10)
            content_type = head.headers.get("Content-Type", "").lower()
        except Exception:
            content_type = ""

        if "pdf" in content_type or pdf_url.lower().endswith(".pdf"):
            response = requests.get(pdf_url, timeout=30)
            response.raise_for_status()
            with open(file_path, "wb") as f:
                f.write(response.content)
            LOGGER.info(f"  ✅ Direct PDF saved: {file_path}")
            return file_path

        LOGGER.debug(f"  ⚠️ Non-direct PDF — using Page.printToPDF for: {pdf_url}")
        origin_url = driver.current_url
        try:
            driver.get(pdf_url)
            time.sleep(5)
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            pdf_result = driver.execute_cdp_cmd(
                "Page.printToPDF", {"printBackground": True}
            )
            with open(file_path, "wb") as f:
                f.write(base64.b64decode(pdf_result["data"]))
            LOGGER.info(f"  ✅ Print-to-PDF saved: {file_path}")
            return file_path
        finally:
            try:
                driver.get(origin_url)
                time.sleep(3)
                iframes = driver.find_elements(By.NAME, "MeetingDocument")
                if iframes:
                    driver.switch_to.frame(iframes[0])
                    LOGGER.debug("  ↪️ Re-entered MeetingDocument iframe after page restore.")
            except Exception:
                pass

    except Exception as e:
        LOGGER.error(f"  ❌ download_pdf failed for {pdf_url}: {e}")
        return None


# =============================================================================
# SECTION 2 — DILIGENT COVER PAGE BUILDER
# =============================================================================

def _get_cover_page_pdf(
    driver,
    main_window:     str,
    nces:            str,
    district:        str,
    term_for_naming: str,
    row_date:        datetime,
    block_element=None,
    meeting_title:   str = "",
    meeting_url:     str = "",
) -> Optional[str]:
    """Build a plain-text cover PDF from the context of one Diligent <li> block."""
    try:
        context_lines: List[str] = []

        if meeting_title:
            context_lines.append(f"Meeting: {meeting_title}")
            context_lines.append(f"URL: {meeting_url}")
            context_lines.append("")

        if block_element is not None:
            try:
                block_text = driver.execute_script(
                    "return arguments[0].innerText;", block_element
                ).strip()
            except Exception:
                block_text = ""

            if block_text:
                context_lines.append("Agenda Item Text:")
                context_lines.append("─" * 60)
                prev_blank = False
                for line in block_text.splitlines():
                    stripped = line.strip()
                    if not stripped:
                        if not prev_blank:
                            context_lines.append("")
                        prev_blank = True
                    else:
                        context_lines.append(stripped)
                        prev_blank = False
            else:
                context_lines.append("[No text could be extracted from this agenda item]")
        else:
            context_lines.append("[No block element provided — cover page is text-only]")

        if block_element is not None:
            try:
                section_heading = driver.execute_script("""
                    var el = arguments[0];
                    var current = el.parentElement;
                    for (var i = 0; i < 6; i++) {
                        if (!current) break;
                        var prev = current.previousElementSibling;
                        if (prev) {
                            var txt = prev.innerText.trim();
                            if (txt.length > 3 && txt.length < 120) return txt;
                        }
                        current = current.parentElement;
                    }
                    return null;
                """, block_element)
                if section_heading:
                    insert_idx = 3 if meeting_title else 0
                    context_lines.insert(insert_idx, f"SECTION: {section_heading}")
            except Exception:
                pass

        attachments: List[str] = []
        if block_element is not None:
            try:
                names = driver.execute_script("""
                    var el = arguments[0];
                    var links = el.querySelectorAll('a[href*="/document/"]');
                    return Array.from(links).map(a => a.innerText.trim());
                """, block_element)
                seen: Set[str] = set()
                for name in names:
                    clean = name.strip()
                    if clean and clean not in seen:
                        attachments.append(clean)
                        seen.add(clean)
            except Exception:
                pass

        if attachments:
            context_lines.append("")
            context_lines.append("ATTACHMENTS:")
            context_lines.append("─" * 60)
            for att in attachments:
                context_lines.append(f"- {att}")

        file_date  = row_date.strftime("%m-%d-%y")
        cover_name = f"{nces}_{district.upper()}_{term_for_naming}_{file_date}_cover.pdf"
        cover_path = os.path.join(tempfile.gettempdir(), cover_name)

        doc = SimpleDocTemplate(
            cover_path, pagesize=letter,
            leftMargin=inch, rightMargin=inch,
            topMargin=inch,  bottomMargin=inch,
        )
        styles = getSampleStyleSheet()
        heading_style = ParagraphStyle(
            "dil_heading", parent=styles["Heading2"], fontSize=13, spaceAfter=8
        )
        normal_style = ParagraphStyle(
            "dil_normal", parent=styles["Normal"], fontSize=11, leading=16, spaceAfter=4
        )

        story = [
            Paragraph("Diligent Agenda Item Cover Page", heading_style),
            Spacer(1, 0.15 * inch),
        ]
        for line in context_lines:
            if not line:
                story.append(Spacer(1, 0.1 * inch))
            elif line.startswith("─"):
                story.append(Spacer(1, 0.05 * inch))
            else:
                safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(safe, normal_style))

        doc.build(story)

        if os.path.exists(cover_path) and os.path.getsize(cover_path) > 0:
            LOGGER.info(f"  ✅ Cover page created: {cover_path}")
            return cover_path

        LOGGER.error("  ❌ Cover page PDF was empty after build.")
        return None

    except Exception as e:
        LOGGER.error(f"  ❌ Cover page creation failed: {e}")
        return None


# =============================================================================
# SECTION 3 — SINGLE ATTACHMENT PROCESSOR
# =============================================================================

def _process_single_attachment(
    driver,
    href:             str,
    att_name:         str,
    attachment_index: int,
    main_window:      str,
    nces:             str,
    district:         str,
    meeting_type:     str,
    term_for_naming:  str,
    span_text:        str,
    row_date:         datetime,
    link:             str,
    prompt_df:        pd.DataFrame,
    meeting_record:   Any,
    meeting_id:       Optional[int] = None,
    specialist_mode:  bool = False,
    category:         str  = "GENERAL_SUPPORTING",
) -> Optional[str]:
    """Download and hash-check one Diligent agenda attachment."""
    file_date      = row_date.strftime("%m-%d-%y")
    district_upper = district.upper()

    LOGGER.debug(
        f"\n  ┌─ Attachment #{attachment_index} ──────────────────────────\n"
        f"  │ name : {att_name}\n"
        f"  │ href : {href}"
    )

    if not href:
        LOGGER.warning(f"  ⚠️ Attachment #{attachment_index} has no href — skipping.")
        meeting_record.errors += 1
        return None

    file_name = f"{nces}_{district_upper}_{term_for_naming}_{file_date}_{attachment_index}.pdf"
    filepath  = os.path.join(tempfile.gettempdir(), file_name)
    return_path: Optional[str] = None

    try:
        if not _is_session_alive(driver):
            LOGGER.error(f"  ❌ Chrome session dead before attachment #{attachment_index}.")
            meeting_record.errors += 1
            raise InvalidSessionIdException(
                f"Chrome session dead before attachment #{attachment_index}."
            )

        result = download_pdf(driver, href, file_name)
        if not result or not os.path.exists(result):
            LOGGER.error(f"  ❌ Download failed for attachment #{attachment_index}.")
            log_crawl_attachment(
                meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                attachment_name=att_name, outcome="❌ DL Failed", is_duplicate=False,
            )
            meeting_record.errors += 1
            return None
        filepath = result

        if not debug_check_pdf_file(filepath) or is_pdf_corrupted(filepath):
            LOGGER.error(f"  ❌ Corrupt or invalid PDF — skipping #{attachment_index}.")
            log_crawl_attachment(
                meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                attachment_name=att_name, outcome="❌ Not a PDF", is_duplicate=False,
            )
            meeting_record.errors += 1
            if os.path.exists(filepath):
                os.remove(filepath)
            return None

        file_hash = compute_sha256_from_file(filepath)
        LOGGER.debug(f"  SHA-256: {file_hash}")

        if db_check_sha256_dupe(file_hash):
            LOGGER.info(f"  🔁 SHA-256 dupe — skipping #{attachment_index}.")
            log_crawl_attachment(
                meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                attachment_name=att_name, outcome="🔁 Duplicate",
                sha256_hash=file_hash, is_duplicate=True,
            )
            meeting_record.dupes += 1
            if os.path.exists(filepath):
                os.remove(filepath)
            return None

        text, _ = extract_text_with_ocr(filepath)
        minhash_obj = build_minhash(text)
        minhash_str = serialize_minhash(minhash_obj)
        minhash_is_dupe, matching_link = db_check_minhash_dupe(minhash_obj)

        if minhash_is_dupe:
            LOGGER.info(f"  🔁 MinHash dupe ({matching_link}) — skipping #{attachment_index}.")
            log_crawl_attachment(
                meeting_id=meeting_id, nces_id=nces, pdf_url=href,
                attachment_name=att_name, outcome="🔁 Duplicate",
                sha256_hash=file_hash, minhash_json=minhash_str, is_duplicate=True,
            )
            meeting_record.dupes += 1
            if os.path.exists(filepath):
                os.remove(filepath)
            return None

        log_crawl_attachment(
            meeting_id=meeting_id, nces_id=nces, pdf_url=href,
            attachment_name=att_name, outcome="✅ Downloaded",
            sha256_hash=file_hash, minhash_json=minhash_str, is_duplicate=False,
        )

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
                LOGGER.info("  ℹ️ AI flagged as non-minutes → returning for merge.")
                meeting_record.downloaded += 1
                return filepath

            MAPPING = {
                "REGULAR": "BOE-REG", "COMMITTEE": "BOE-COM", "WORK":      "BOE-WS",
                "FINANCE": "BOE-FIN", "PUBLIC":    "BOE-PUB", "EXECUTIVE": "BOE-EXE",
                "SPECIAL": "BOE-SP",  "AGENDA":    "BOE-AGENDA",
            }
            raw_type  = res.get("meeting_type", "").upper()
            doc_type  = MAPPING.get(raw_type) or classify_doc_type(meeting_type)
            ai_date   = parse_to_mmddyy(res.get("meeting_date")) if res.get("meeting_date") else None

            if not ai_date:
                LOGGER.warning("  ⚠️ Gemini found no date — falling back to regex.")
                ai_date = parse_to_mmddyy(extract_first_date_from_pdf(filepath))

            final_date = ai_date or file_date
            base_name  = f"{nces}_{district_upper}_{doc_type}_{final_date}.pdf"
            final_name = build_unique_filename(base_name)

            uploaded_file_id = upload_file_to_folder(
                drive_service, _get_folder_id("MINUTES"), filepath, final_name
            )
            LOGGER.info(
                f"  ✅ [Minutes] Uploaded: "
                f"https://drive.google.com/file/d/{uploaded_file_id}/view?usp=sharing"
            )
            meeting_record.downloaded += 1
            return_path = None

        else:
            LOGGER.debug(f"  ↩️ Novel, returning for merge: {filepath}")
            meeting_record.downloaded += 1
            return_path = filepath

    except InvalidSessionIdException:
        LOGGER.error(f"  ❌ Chrome session died on attachment #{attachment_index} — re-raising.")
        meeting_record.errors += 1
        raise

    except Exception as e:
        LOGGER.error(f"  ❌ Error on attachment #{attachment_index}: {e}")
        log_crawl_attachment(
            meeting_id=meeting_id, nces_id=nces, pdf_url=href,
            attachment_name=att_name, outcome="❌ Error", is_duplicate=False,
        )
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
# SECTION 4 — AGENDA TRAVERSAL
# =============================================================================

def search_and_download_agenda_attachments(
    driver,
    nces:           str,
    district:       str,
    meeting_title:  str,
    meeting_url:    str,
    row_date:       datetime,
    meeting_type:   str,
    link:           str,
    prompt_df:      pd.DataFrame,
    processed_urls: Set[str],
    meeting_record: Any,
    meeting_id:     Optional[int] = None,
) -> None:
    """Walk every leaf <li> block on a Diligent meeting page."""
    main_window = driver.current_window_handle

    LOGGER.info(
        f"\n{'─' * 60}\n"
        f"  DILIGENT SCRAPE: {meeting_title}\n"
        f"  Type: {meeting_type} | Date: {row_date.strftime('%m-%d-%y')}\n"
        f"{'─' * 60}"
    )

    in_iframe = False
    try:
        iframes = driver.find_elements(By.NAME, "MeetingDocument")
        if iframes:
            driver.switch_to.frame(iframes[0])
            in_iframe = True
            LOGGER.debug("  ↪️ Switched into MeetingDocument iframe.")
    except Exception as e:
        LOGGER.warning(f"  ⚠️ Could not switch into MeetingDocument iframe: {e}")

    try:
        all_lis = driver.find_elements(By.TAG_NAME, "li")
        blocks  = [
            li for li in all_lis
            if li.find_elements(By.XPATH, ".//table[.//a[contains(@name,'TemplateTable')]]")
            and not li.find_elements(By.TAG_NAME, "li")
        ]
        LOGGER.debug(f"  {len(all_lis)} total <li>s → {len(blocks)} leaf blocks.")

        block_index = 0
        while block_index < len(blocks):
            block      = blocks[block_index]
            cover_path: Optional[str] = None

            if not _is_session_alive(driver):
                LOGGER.error(f"  ❌ Chrome session dead before block[{block_index}] — aborting.")
                raise InvalidSessionIdException(f"Chrome session died at block[{block_index}].")

            try:
                all_possible_links = block.find_elements(By.TAG_NAME, "a")
                attachment_links   = []

                for a in all_possible_links:
                    is_in_sublist = driver.execute_script("""
                        var link  = arguments[0];
                        var block = arguments[1];
                        var current = link.parentElement;
                        while (current && current !== block) {
                            if (current.tagName === 'OL' || current.tagName === 'UL')
                                return true;
                            current = current.parentElement;
                        }
                        return false;
                    """, a, block)

                    if not is_in_sublist:
                        href = a.get_attribute("href")
                        text = driver.execute_script(
                            "return arguments[0].textContent;", a
                        ).strip().lower()
                        if href and ("document" in href.lower() or "download" in text):
                            attachment_links.append(a)

                if not attachment_links:
                    LOGGER.debug(f"  [{block_index}] No attachment links — skipping.")
                    block_index += 1
                    continue

                block_text = driver.execute_script(
                    "return arguments[0].textContent;", block
                ).strip()

                LOGGER.debug(
                    f"\n  ┌─ Block [{block_index}] — {len(attachment_links)} attachment(s)\n"
                    f"  │ {block_text[:120]}"
                )

                term_for_naming = re.sub(r"[^\w\s-]", "", block_text[:60]).replace(" ", "_")

                cover_path = _get_cover_page_pdf(
                    driver=driver, main_window=main_window,
                    nces=nces, district=district,
                    term_for_naming=term_for_naming, row_date=row_date,
                    block_element=block,
                    meeting_title=meeting_title, meeting_url=meeting_url,
                )

                router_result: Dict[str, Any] = {
                    "is_minute": False,
                    "category":  "GENERAL_SUPPORTING",
                }
                if cover_path:
                    router_prompt, prompt_id = get_prompt_info(prompt_df, "pre-classification")
                    ai_routing    = analyze_pdf_with_gemini_with_retry(
                        cover_path, router_prompt, MODEL_NAME
                    )
                    router_result = ai_routing.get("result", router_result)
                    log_ai_usage(
                        prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                        tokens=ai_routing.get("tokens", {}), meeting_id=meeting_id,
                    )
                    LOGGER.debug(
                        f"  [AI] category={router_result.get('category')} | "
                        f"reasoning={router_result.get('reasoning', '')[:80]}"
                    )

                use_specialist  = router_result.get("is_minute", False)
                target_category = router_result.get("category", "GENERAL_SUPPORTING")
                is_policy_item  = _cover_page_is_policy(cover_path)

                attachment_info = [
                    (
                        a.get_attribute("href") or "",
                        driver.execute_script("return arguments[0].textContent;", a).strip(),
                    )
                    for a in attachment_links
                ]

                collected_paths: List[str] = []
                collected_urls:  List[str] = []
                collected_names: List[str] = []

                for i, (att_href, att_name) in enumerate(attachment_info, start=1):
                    if not att_href or att_href in processed_urls:
                        LOGGER.debug(f"  [{i}] Already processed or empty href — skipping.")
                        continue
                    processed_urls.add(att_href)

                    result_path = _process_single_attachment(
                        driver=driver, href=att_href, att_name=att_name,
                        attachment_index=i, main_window=main_window,
                        nces=nces, district=district, meeting_type=meeting_type,
                        term_for_naming=term_for_naming, span_text=block_text,
                        row_date=row_date, link=link, prompt_df=prompt_df,
                        meeting_record=meeting_record, meeting_id=meeting_id,
                        specialist_mode=use_specialist, category=target_category,
                    )

                    if result_path is None:
                        continue

                    if is_policy_item:
                        LOGGER.info(f"  [POLICY] Uploading attachment #{i} individually.")
                        try:
                            combine_and_upload_documents(
                                cover_page_path=cover_path, attachment_paths=[result_path],
                                nces=nces, district=district, meeting_type=meeting_type,
                                term_for_naming=term_for_naming, span_text=block_text,
                                row_date=row_date, link=link, prompt_df=prompt_df,
                                pdf_urls=[att_href], attachment_names=[att_name],
                                meeting_record=meeting_record, meeting_id=meeting_id,
                                category=target_category,
                            )
                        except Exception as e:
                            LOGGER.error(f"  ❌ Policy upload failed for #{i}: {e}")
                        finally:
                            if os.path.exists(result_path):
                                try:
                                    os.remove(result_path)
                                except OSError:
                                    pass
                    else:
                        collected_paths.append(result_path)
                        collected_urls.append(att_href)
                        collected_names.append(att_name)

                if collected_paths:
                    LOGGER.info(
                        f"  Merging {len(collected_paths)} attachment(s) for: {term_for_naming}"
                    )
                    combine_and_upload_documents(
                        cover_page_path=cover_path, attachment_paths=collected_paths,
                        nces=nces, district=district, meeting_type=meeting_type,
                        term_for_naming=term_for_naming, span_text=block_text,
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

                time.sleep(random.uniform(1, 3))
                block_index += 1

            except InvalidSessionIdException:
                LOGGER.error(f"  ❌ InvalidSessionIdException at block[{block_index}] — aborting.")
                raise

            except (StaleElementReferenceException, NoSuchElementException):
                LOGGER.warning(f"  ⚠️ Stale element at block[{block_index}] — re-entering iframe.")
                try:
                    driver.switch_to.default_content()
                    iframes = driver.find_elements(By.NAME, "MeetingDocument")
                    if iframes:
                        driver.switch_to.frame(iframes[0])
                        all_lis = driver.find_elements(By.TAG_NAME, "li")
                        blocks  = [
                            li for li in all_lis
                            if li.find_elements(
                                By.XPATH, ".//table[.//a[contains(@name,'TemplateTable')]]",
                            )
                            and not li.find_elements(By.TAG_NAME, "li")
                        ]
                        LOGGER.info(f"  ↪️ Re-entered iframe. Resuming at block[{block_index}].")
                        continue
                    else:
                        LOGGER.warning("  ⚠️ MeetingDocument iframe gone — stopping.")
                        break
                except Exception as re_entry_err:
                    LOGGER.warning(f"  ⚠️ Could not re-enter iframe: {re_entry_err} — stopping.")
                    break
                block_index += 1

            except Exception as e:
                LOGGER.error(f"  ❌ Error in block[{block_index}]: {e}")
                log_error_to_db(
                    error_type="AGENDA_ITEM_ERROR",
                    message=str(e)[:2000],
                    stack_trace=traceback.format_exc(),
                    nces_id=nces,
                    meeting_id=meeting_id,
                )
                block_index += 1

            finally:
                if cover_path and os.path.exists(cover_path):
                    try:
                        os.remove(cover_path)
                    except OSError:
                        pass

    finally:
        if in_iframe:
            try:
                driver.switch_to.default_content()
                LOGGER.debug("  ↩️ Exited MeetingDocument iframe.")
            except Exception:
                pass
        try:
            driver.switch_to.window(main_window)
        except Exception:
            pass
