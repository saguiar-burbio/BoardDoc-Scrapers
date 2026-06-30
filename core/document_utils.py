# ─────────────────────────────────────────────────────────────────────────────
# core/document_utils.py
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import re
import tempfile
import time
import traceback
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF
import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from selenium.webdriver.common.by import By
from selenium.common.exceptions import StaleElementReferenceException
import base64

from config.settings import (
    MODEL_NAME,
    POLICY_RE
)
from core.database import (
    db_check_sha256_dupe,
    db_check_minhash_dupe,
    db_get_filename_count,
    log_uploaded_document,
    log_ai_usage,
    log_contact_to_db,
    get_prompt_info,
    get_drive_folder_map,
)


def _get_folder_id(name: str) -> str:
    """Returns the Drive folder ID for the given folder name from the DB-backed map."""
    return get_drive_folder_map().get(name, "")
from core.driver import cdp_print_with_timeout
from core.hashing import (
    compute_sha256_from_file,
    build_minhash
)
from core.pdf_functions import extract_text_with_ocr, parse_to_mmddyy
from core.google_functions import upload_file_to_folder

LOGGER = logging.getLogger("simbli_minutes")


# ═════════════════════════════════════════════════════════════════════════════
# 1. FILE INTEGRITY & CORRUPTION CHECKS
# ═════════════════════════════════════════════════════════════════════════════

def is_pdf_corrupted(filepath: str) -> bool:
    """Returns True if the file is missing, empty, or not a valid PDF."""
    try:
        if not os.path.exists(filepath):
            return True
        if os.path.getsize(filepath) == 0:
            return True
        doc = fitz.open(filepath)
        if doc.page_count == 0:
            doc.close()
            return True
        doc.close()
        return False
    except Exception as e:
        LOGGER.warning(f"⚠️ Corrupted PDF check triggered: {filepath} ({e})")
        return True


def debug_check_pdf_file(filepath: str) -> bool:
    """Verify a downloaded file is a real PDF by checking its magic bytes."""
    try:
        with open(filepath, "rb") as f:
            header = f.read(5)
        is_pdf = header.startswith(b"%PDF")
        if not is_pdf:
            LOGGER.warning(f"⚠️  File does NOT look like a PDF! First bytes: {header!r}")
        else:
            LOGGER.debug(f"✅ PDF magic bytes confirmed: {filepath}")
        return is_pdf
    except FileNotFoundError:
        LOGGER.error(f"❌ File not found for PDF check: {filepath}")
        return False
    except Exception as e:
        LOGGER.error(f"PDF check error for '{filepath}': {e}")
        return False


# ═════════════════════════════════════════════════════════════════════════════
# 2. PDF GENERATION, CDP PORTRAIT AND MERGE COMPILER LAYER
# ═════════════════════════════════════════════════════════════════════════════

def _get_cover_page_pdf(
    driver,
    main_window: str,
    nces: str,
    district: str,
    term_for_naming: str,
    row_date: datetime,
    block_element=None,
    meeting_title: str = "",
    meeting_url: str = "",
) -> Optional[str]:
    """Generates a clean contextual cover metadata sheet using ReportLab."""
    try:
        context_lines = [
            f"Meeting: {meeting_title}",
            f"URL: {meeting_url}",
            "",
        ]

        try:
            cls = block_element.get_attribute("class")
            if "agenda-item-children-of-" in cls:
                parent_id = cls.split("agenda-item-children-of-")[1].split()[0]
                if parent_id and parent_id != "0":
                    parent_row = driver.find_element(
                        By.CSS_SELECTOR,
                        f"tr[data-agendaitemid='{parent_id}']"
                    )
                    parent_text = driver.execute_script(
                        "return arguments[0].innerText;", parent_row
                    ).strip()
                    if parent_text:
                        context_lines.append(f"SECTION: {parent_text.splitlines()[0]}")
        except Exception:
            pass

        try:
            item_text = block_element.find_element(By.CSS_SELECTOR, ".form-check").text.strip()
            if item_text:
                context_lines.append("")
                context_lines.append("AGENDA ITEM:")
                context_lines.append(item_text)
        except Exception:
            pass

        try:
            attachment_block = block_element.find_elements(By.CSS_SELECTOR, ".fileNameValue")
            if attachment_block:
                context_lines.append("")
                context_lines.append("ATTACHMENTS:")
                for att in attachment_block:
                    name = att.text.strip()
                    if name:
                        context_lines.append(f"- {name}")
            else:
                links = block_element.find_elements(By.CSS_SELECTOR, "a")
                if links:
                    context_lines.append("")
                    context_lines.append("ATTACHMENTS:")
                    for a in links:
                        txt = a.text.strip()
                        if txt and "REGISTER TO SPEAK" not in txt.upper():
                            context_lines.append(f"- {txt}")
        except Exception:
            pass

        cover_name = f"{nces}_{district.upper()}_{term_for_naming}_{row_date.strftime('%m%d%y')}_cover.pdf"
        cover_path = os.path.join(tempfile.gettempdir(), cover_name)

        doc = SimpleDocTemplate(cover_path, pagesize=letter)
        styles = getSampleStyleSheet()
        story = [
            Paragraph("BoardBook Agenda Item Cover", styles["Heading1"]),
            Spacer(1, 12)
        ]

        for line in context_lines:
            if line.strip():
                safe = line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                story.append(Paragraph(safe, styles["Normal"]))
            else:
                story.append(Spacer(1, 6))

        doc.build(story)
        return cover_path if os.path.exists(cover_path) else None

    except Exception as e:
        LOGGER.error(f"  ❌ Cover page failure: {e}")
        return None


def convert_url_to_pdf(driver, url: str, output_path: str) -> bool:
    """Navigates to a URL and dumps the page to PDF via CDP."""
    try:
        LOGGER.info(f"  [CONVERT] Navigating to preview URL: {url}")
        driver.get(url)
        time.sleep(7)

        print_options = {
            'landscape': True,
            'displayHeaderFooter': False,
            'printBackground': True,
            'preferCSSPageSize': True
        }

        result = cdp_print_with_timeout(driver, print_options, timeout=60)
        with open(output_path, "wb") as f:
            f.write(base64.b64decode(result['data']))

        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            LOGGER.info(f"  ✅ [CONVERT] Successfully converted preview to PDF: {output_path}")
            return True
        return False
    except Exception as e:
        LOGGER.error(f"  ❌ [CONVERT] Failed to convert to PDF: {e}")
        return False


def _merge_pdfs(pdf_paths: list, output_path: str) -> bool:
    """Merge an ordered list of PDF files into output_path using PyMuPDF."""
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
        LOGGER.debug(f"  Merged {len(pdf_paths)} PDF(s) → {output_path}")
        return True
    except Exception as e:
        LOGGER.error(f"  ❌ PDF merge failed: {e}")
        LOGGER.debug(traceback.format_exc())
        return False


# ═════════════════════════════════════════════════════════════════════════════
# 3. ADMINISTRATIVE TEXT PARSING UTILITIES
# ═════════════════════════════════════════════════════════════════════════════

def parse_meeting_info(text: str) -> tuple:
    """Parse meeting type and date from standard BoardBook title strings."""
    if not text:
        return "Unknown", "Unknown"

    text = re.sub(r"^Meeting:\s*", "", text, flags=re.IGNORECASE).strip()
    text = text.replace(",", " ").replace(".", " ").strip()
    text = re.sub(r"\s+", " ", text)

    date_pattern = re.search(
        r"([A-Za-z]{3,9})\s+(\d{1,2})\s+(\d{4})"
        r"|"
        r"(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})"
        r"|"
        r"([A-Za-z]{3,9})\s+(\d{4})",
        text
    )

    if not date_pattern:
        return text, "Unknown"

    matched_span = date_pattern.group(0)
    meeting_type = text[: date_pattern.start()].strip(" -–—").strip()
    if not meeting_type:
        meeting_type = "Unknown"

    date_str_clean = re.sub(r"\s+", " ", matched_span).strip()
    date_formats = [
        "%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%b %Y", "%B %Y",
    ]

    meeting_date = None
    for fmt in date_formats:
        try:
            dt = datetime.strptime(date_str_clean.title(), fmt)
            if "%d" not in fmt:
                dt = dt.replace(day=1)
            meeting_date = dt
            break
        except ValueError:
            continue

    if not meeting_date:
        return meeting_type, "Unknown"

    return meeting_type, meeting_date.strftime("%m-%d-%y")


def classify_doc_type(text: str) -> str:
    """Categorises a meeting type string into a short document-type identifier."""
    text_lower = text.lower()
    if any(k in text_lower for k in ["special", "called meeting", "tribunal"]):
        return "BOE-SP"
    if "executive" in text_lower:
        return "BOE-EXE"
    if any(k in text_lower for k in ["work session", "workshop", "planning"]):
        return "BOE-WS"
    if any(k in text_lower for k in ["committee", "council", "governance"]):
        return "BOE-COM"
    if any(k in text_lower for k in ["public", "hearing"]):
        return "BOE-PUB"
    if "finance" in text_lower:
        return "BOE-FIN"
    return "BOE-REG"


def _cover_page_is_policy(cover_path: str) -> bool:
    """Returns True if the cover page PDF contains the word 'policy' or 'policies'."""
    if not cover_path or not os.path.exists(cover_path):
        return False
    try:
        text, _ = extract_text_with_ocr(cover_path)
        return bool(POLICY_RE.search(text))
    except Exception as e:
        LOGGER.warning(f"  _cover_page_is_policy: OCR check crashed ({e}) — defaulting to False.")
        return False


def build_unique_filename(base_name: str) -> str:
    """Return a filename that does not collide with existing DB or current-run logs."""
    count = db_get_filename_count(base_name)

    if count == 0:
        final_name = base_name
        LOGGER.debug(f"  Filename '{base_name}' is unique — no suffix needed.")
    else:
        stem, ext = os.path.splitext(base_name)
        parts = stem.split("_")
        suffix = f"-{count + 1}"

        if parts[-1].endswith(("YY", "YYYY")) or any(char.isdigit() for char in parts[-1]):
            parts[-2] = f"{parts[-2]}{suffix}"
        else:
            parts[-1] = f"{parts[-1]}{suffix}"

        final_name = f"{'_'.join(parts)}{ext}"
        LOGGER.info(f"  Filename collision: '{base_name}' exists {count}× → using '{final_name}'")

    from config.settings import filename_run_counter
    filename_run_counter[base_name] += 1
    return final_name


# ═════════════════════════════════════════════════════════════════════════════
# 4. REGIONAL CONTACT EXTRACTION PARSER MECHANISMS
# ═════════════════════════════════════════════════════════════════════════════

def _looks_like_person_line(line: str) -> bool:
    """Returns True if the line matches standard name/title formatting."""
    if "," not in line:
        return False
    name_part, _, title_part = line.partition(",")
    name_part = name_part.strip()
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


def _log_person(p: dict, nces_id: str = ""):
    """Logs a parsed contact into the database."""
    if not p["name"]:
        return
    title_str = ", ".join(p["title"])
    LOGGER.info(f"name: {p['name']}")
    LOGGER.info(f"title: {title_str}")
    LOGGER.info(f"phone_number: {p['phone']}")
    LOGGER.info(f"email: {p['email']}\n")

    log_contact_to_db(
        nces_id=nces_id,
        name=p["name"],
        email=p["email"],
        phone=p["phone"],
        title=title_str,
    )


def _parse_contact_block(content: str, nces_id: str):
    """Parses a text block to extract personal contact properties."""
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


def extract_and_log_contact_info(driver, nces_id: str = ""):
    """Extracts contact cards from layout components in meeting data scroll fields."""
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
            LOGGER.info(f"--- {header_upper} ---")
            _parse_contact_block(content, nces_id)
    except Exception as e:
        LOGGER.error(f"Extraction Error: {e}")


# ═════════════════════════════════════════════════════════════════════════════
# 5. CORE EXPORT COMPILING & CLOUD STORAGE EXPORT PIPELINE
# ═════════════════════════════════════════════════════════════════════════════

def combine_and_upload_documents(
    cover_page_path:  Optional[str],
    attachment_paths: list,
    nces:              str,
    district:          str,
    meeting_type:      str,
    term_for_naming:  str,
    span_text:         str,
    row_date:          datetime,
    link:              str,
    prompt_df:          pd.DataFrame,
    pdf_urls:          list,
    attachment_names: list,
    meeting_record:    Any,
    meeting_id:        Optional[int] = None,
    category:          Optional[str] = None,
) -> None:
    """Assembles all components, runs deduplication checks, and saves to GDrive."""
    district_upper = district.upper()
    formatted_date = row_date.strftime("%m-%d-%y")

    parts: list = []
    if cover_page_path and os.path.exists(cover_page_path):
        parts.append(cover_page_path)
    parts.extend(p for p in attachment_paths if p and os.path.exists(p))

    if not parts:
        LOGGER.warning(f"   [{district}] | No PDFs found to assemble — skipping.")
        return

    merged_name = f"{nces}_{district_upper}_{term_for_naming}_{formatted_date}_merged.pdf"
    merged_path = os.path.join(tempfile.gettempdir(), merged_name)

    try:
        if not _merge_pdfs(parts, merged_path):
            LOGGER.error(f"   [{district}] | Merge failed for {term_for_naming}")
            meeting_record.errors += 1
            return

        file_hash = compute_sha256_from_file(merged_path)

        text, page_count = extract_text_with_ocr(merged_path)
        minhash_obj = build_minhash(text)

        uploaded_successfully = False

        def _upload_and_log(
            folder_id:    str,
            folder_label: str,
            file_name:    str,
            category_tag: str,
            doc_type:     str,
            fiscal_year:  Optional[str],
            prompt_id:    Optional[int],
            analysis_response: dict,
        ) -> bool:
            from __main__ import drive_service

            uploaded_file_id = upload_file_to_folder(drive_service, folder_id, merged_path, file_name)
            file_url = f"https://drive.google.com/file/d/{uploaded_file_id}/view?usp=sharing"
            LOGGER.info(f"   [{district}] | [{folder_label}] Uploaded: {file_url}")

            document_id = log_uploaded_document(
                meeting_id       = meeting_id,
                nces_id          = nces,
                file_name        = file_name,
                drive_folder     = folder_label,
                google_drive_url = file_url,
                sha256_hash      = file_hash,
                minhash_obj      = minhash_obj,
                category         = category_tag,
                doc_type         = doc_type,
                fiscal_year      = fiscal_year,
                page_count       = page_count,
            )

            log_ai_usage(
                prompt_id       = prompt_id,
                nces_id         = nces,
                model_name      = MODEL_NAME,
                tokens          = analysis_response.get("tokens", {}),
                status          = "SUCCESS" if "error" not in analysis_response.get("result", {}) else "FAILED",
                meeting_id      = meeting_id,
                document_id     = document_id,
                prompt_snapshot = None,
                response_json   = analysis_response.get("result", {}),
            )

            meeting_record.downloaded += 1
            return True

        from core.analyzer import analyze_pdf_with_gemini_with_retry

        if category:
            cat_upper = category.upper()

            if cat_upper == "BUDGET":
                prompt_text, prompt_id = get_prompt_info(prompt_df, "budget")
                analysis_response = analyze_pdf_with_gemini_with_retry(merged_path, prompt_text, MODEL_NAME)

                log_ai_usage(
                    prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                    tokens=analysis_response.get("tokens", {}),
                    status="SUCCESS" if "error" not in analysis_response.get("result", {}) else "FAILED",
                    meeting_id=meeting_id, prompt_snapshot=prompt_text,
                    response_json=str(analysis_response.get("result", {})),
                )

                result_data = analysis_response.get("result", {})
                LOGGER.info(f"  [{district}] | [BUDGET AI] is_budget={result_data.get('is_budget', False)}")

                if result_data.get("is_budget", False):
                    budget_type = result_data.get("document_type", "NA")
                    fiscal_year = result_data.get("fiscal_year", "NA")
                    base_name   = f"{nces}_{district_upper}_{fiscal_year}_{budget_type}.pdf"
                    file_name_final = build_unique_filename(base_name)

                    uploaded_successfully = _upload_and_log(
                        folder_id=_get_folder_id("BUDGET"), folder_label="BUDGET",
                        file_name=file_name_final, category_tag="BUDGET",
                        doc_type="BUDGET", fiscal_year=fiscal_year,
                        prompt_id=prompt_id, analysis_response=analysis_response,
                    )

            elif cat_upper == "STRATEGIC_PLANNING":
                prompt_text, prompt_id = get_prompt_info(prompt_df, "strategic")
                analysis_response = analyze_pdf_with_gemini_with_retry(merged_path, prompt_text, MODEL_NAME)

                log_ai_usage(
                    prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                    tokens=analysis_response.get("tokens", {}),
                    status="SUCCESS" if "error" not in analysis_response.get("result", {}) else "FAILED",
                    meeting_id=meeting_id, prompt_snapshot=prompt_text,
                    response_json=str(analysis_response.get("result", {})),
                )

                result_data = analysis_response.get("result", {})
                LOGGER.info(f"   [{district}] | [STRAT AI] is_strategic={result_data.get('is_strategic', False)}")

                if result_data.get("is_strategic", False):
                    document_type = result_data.get("document_type", "NA")
                    start_year    = result_data.get("start_year", "NA")
                    end_year      = result_data.get("end_year", "NA")
                    base_name     = f"{nces}_{district_upper}_{start_year}-{end_year}_{document_type}.pdf"
                    file_name_final = build_unique_filename(base_name)

                    uploaded_successfully = _upload_and_log(
                        folder_id=_get_folder_id("STRATEGIC_PLANNING"), folder_label="STRATEGIC_PLANNING",
                        file_name=file_name_final, category_tag="STRATEGIC_PLANNING",
                        doc_type=document_type, fiscal_year=f"{start_year}-{end_year}",
                        prompt_id=prompt_id, analysis_response=analysis_response,
                    )

            elif cat_upper == "BOND_LEVY":
                prompt_text, prompt_id = get_prompt_info(prompt_df, "bond")
                analysis_response = analyze_pdf_with_gemini_with_retry(merged_path, prompt_text, MODEL_NAME)

                log_ai_usage(
                    prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                    tokens=analysis_response.get("tokens", {}),
                    status="SUCCESS" if "error" not in analysis_response.get("result", {}) else "FAILED",
                    meeting_id=meeting_id, prompt_snapshot=prompt_text,
                    response_json=str(analysis_response.get("result", {})),
                )

                result_data = analysis_response.get("result", {})
                LOGGER.info(f"  [{district}] | [BOND AI] is_bond={result_data.get('is_bond', False)}")

                if result_data.get("is_bond", False):
                    bond_year = result_data.get("year", "NA")
                    bond_type = result_data.get("document_type", "NA")
                    is_update = result_data.get("is_bond_update", False)
                    f_p       = "F" if result_data.get("f_p", "Unknown") == "Final" else "P"

                    if is_update:
                        base_name = f"{nces}_{district_upper}_{f_p}-{bond_type}-UPDATE-{formatted_date}_{bond_year}.pdf"
                    else:
                        base_name = f"{nces}_{district_upper}_{f_p}-{bond_type}_{bond_year}.pdf"

                    file_name_final = build_unique_filename(base_name)

                    uploaded_successfully = _upload_and_log(
                        folder_id=_get_folder_id("BOND_LEVY"), folder_label="BOND_LEVY",
                        file_name=file_name_final, category_tag="BOND_LEVY",
                        doc_type=f"{f_p}-{bond_type}", fiscal_year=str(bond_year),
                        prompt_id=prompt_id, analysis_response=analysis_response,
                    )

            elif cat_upper == "CALENDAR":
                prompt_text, prompt_id = get_prompt_info(prompt_df, "calendar")
                analysis_response = analyze_pdf_with_gemini_with_retry(merged_path, prompt_text, MODEL_NAME)

                log_ai_usage(
                    prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                    tokens=analysis_response.get("tokens", {}),
                    status="SUCCESS" if "error" not in analysis_response.get("result", {}) else "FAILED",
                    meeting_id=meeting_id, prompt_snapshot=prompt_text,
                    response_json=str(analysis_response.get("result", {})),
                )

                result_data = analysis_response.get("result", {})
                LOGGER.info(f"  [{district}] | [CALENDAR AI] is_calendar={result_data.get('is_calendar', False)}")

                if result_data.get("is_calendar", False):
                    calendar_year   = result_data.get("academic_year", "NA")
                    base_name       = f"{nces}_{district_upper}_CALENDAR_{calendar_year}.pdf"
                    file_name_final = build_unique_filename(base_name)

                    uploaded_successfully = _upload_and_log(
                        folder_id=_get_folder_id("CALENDAR"), folder_label="CALENDAR",
                        file_name=file_name_final, category_tag="CALENDAR",
                        doc_type="CALENDAR", fiscal_year=str(calendar_year),
                        prompt_id=prompt_id, analysis_response=analysis_response,
                    )

        if not uploaded_successfully:
            prompt_text, prompt_id = get_prompt_info(prompt_df, "spending")
            analysis_response = analyze_pdf_with_gemini_with_retry(merged_path, prompt_text, MODEL_NAME)

            log_ai_usage(
                prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                tokens=analysis_response.get("tokens", {}),
                status="SUCCESS" if "error" not in analysis_response.get("result", {}) else "FAILED",
                meeting_id=meeting_id, prompt_snapshot=prompt_text,
                response_json=str(analysis_response.get("result", {})),
            )

            result_data = analysis_response.get("result", {})

            if result_data.get("is_spending_document", False):
                document_type   = result_data.get("document_type", "Spending")
                base_name       = f"{nces}_{district_upper}_{formatted_date}_{document_type}.pdf"
                file_name_final = build_unique_filename(base_name)

                _upload_and_log(
                    folder_id=_get_folder_id("SPENDING"), folder_label="SPENDING",
                    file_name=file_name_final, category_tag="SPENDING",
                    doc_type=document_type, fiscal_year=None,
                    prompt_id=prompt_id, analysis_response=analysis_response,
                )

            else:
                prompt_text, prompt_id = get_prompt_info(prompt_df, "supporting_doc")
                analysis_response = analyze_pdf_with_gemini_with_retry(merged_path, prompt_text, MODEL_NAME)

                log_ai_usage(
                    prompt_id=prompt_id, nces_id=nces, model_name=MODEL_NAME,
                    tokens=analysis_response.get("tokens", {}),
                    status="SUCCESS" if "error" not in analysis_response.get("result", {}) else "FAILED",
                    meeting_id=meeting_id, prompt_snapshot=prompt_text,
                    response_json=str(analysis_response.get("result", {})),
                )

                result_data    = analysis_response.get("result", {})
                extracted_cat  = result_data.get("category", "SUPPORT")
                extracted_date = parse_to_mmddyy(formatted_date)
                doc_type_code  = classify_doc_type(meeting_type)
                boe_type = doc_type_code.split("-")[-1]
                base_name      = f"{nces}_{district_upper}_BOE-AGENDA-{boe_type}-{extracted_cat}_{extracted_date}.pdf"
                file_name_final = build_unique_filename(base_name)

                LOGGER.info(f" [{district}] | [SUPPORT AI] category={extracted_cat}")

                if extracted_cat == "SUPPORT":
                    folder_id, folder_label = _get_folder_id("MANUAL_INTERVENTION"), "MANUAL_INTERVENTION"
                elif extracted_cat in (
                    "GOVERNANCE", "NON-RELEVANT", "POLICY", "STUDENT-SERVICES",
                    "PERSONNEL", "LEGAL-POLICY", "FINANCE-OPERATIONS",
                    "FINANCE-REPORTING", "DISCIPLINE", "CURRICULUM-POLICY", "BEHAVIOR",
                ):
                    folder_id, folder_label = _get_folder_id("GOVERNANCE"), "GOVERNANCE"
                else:
                    folder_id, folder_label = _get_folder_id("SUPPORTING"), "SUPPORTING"

                _upload_and_log(
                    folder_id=folder_id, folder_label=folder_label,
                    file_name=file_name_final, category_tag=extracted_cat,
                    doc_type=doc_type_code, fiscal_year=None,
                    prompt_id=prompt_id, analysis_response=analysis_response,
                )

        LOGGER.info(f"   [{district}] | 🏁 Compilation workflow complete: {merged_name}")

    except Exception as e:
        LOGGER.error(f"   [{district}] | ❌ Critical failure in document assembly: {e}")
        LOGGER.debug(traceback.format_exc())
        meeting_record.errors += 1
    finally:
        if os.path.exists(merged_path):
            os.remove(merged_path)
