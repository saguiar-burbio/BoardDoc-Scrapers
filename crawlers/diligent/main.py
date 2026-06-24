# ─────────────────────────────────────────────────────────────────────────────
# crawlers/diligent/main.py
# Diligent BoardDocs — batch orchestration entry point.
# Run from the repo root: python -m crawlers.diligent.main
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import random
import re
import tempfile
import time
import traceback
from datetime import datetime
from typing import List, Optional, Set, Tuple

import pandas as pd
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

import crawlers.diligent.scraper as dil_scraper
from config.settings import (
    BUCKET_NAME,
    DILIGENT_CSV_INPUT_PATH_NAME,
    CSV_OUTPUT_PATH_NAME,
    CRED_PATH_NAME,
    DOWNLOAD_DIR,
    SPREADSHEET_ID,
    TAB_NAME,
)
from core.database import (
    get_all_prompts_df,
    load_all_db_credentials,
    log_meeting_to_db,
    log_to_overall_sbd_log,
)
from core.driver import create_undetected_driver
from core.gcs import download_blob_to_tmp
from core.google_auth import get_authenticated_services
from core.google_functions import log_doc_info
from core.humanize import random_idle
from core.models import AttachmentRecord, MeetingRecord
from core.utils import debug_summarize_run_stats, setup_logger
from crawlers.diligent.scraper import search_and_download_agenda_attachments

LOGGER = setup_logger(log_level="INFO")


# =============================================================================
# SECTION 1 — DILIGENT TITLE PARSER
# =============================================================================

def parse_meeting_info(text: str) -> Tuple[str, str]:
    """
    Parse meeting type and date from a Diligent title string.
    Returns (meeting_type, date_str) where date_str is "MM-DD-YY".
    """
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
        text,
    )

    if not date_pattern:
        return text, "Unknown"

    matched_span = date_pattern.group(0)
    meeting_type = text[: date_pattern.start()].strip(" -–—").strip() or "Unknown"
    date_str_clean = re.sub(r"\s+", " ", matched_span).strip()

    date_formats = ["%b %d %Y", "%B %d %Y", "%d %b %Y", "%d %B %Y", "%b %Y", "%B %Y"]
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


# =============================================================================
# SECTION 2 — PER-DISTRICT PIPELINE
# =============================================================================

def main(
    nces:        str,
    district:    str,
    link:        str,
    prompt_df:   pd.DataFrame,
    ctrl_f_term: str,
    check_date:  str = "06-01-25",
) -> List[MeetingRecord]:
    """Run the full Diligent scrape pipeline for one district."""
    driver = create_undetected_driver()
    meeting_records: List[MeetingRecord] = []

    try:
        LOGGER.info(
            f"\n{'═'*60}\n"
            f"  DILIGENT DISTRICT: {district}  |  NCES: {nces}\n"
            f"  Link: {link}\n"
            f"{'═'*60}"
        )

        driver.get(link)
        time.sleep(5)
        wait = WebDriverWait(driver, 30)

        try:
            meetings_link = next(
                (
                    el for el in driver.find_elements(By.TAG_NAME, "a")
                    if el.text.strip().upper() == "MEETINGS"
                ),
                None,
            )
            if not meetings_link:
                raise RuntimeError("Could not find 'MEETINGS' navigation link.")
            meetings_link.click()
            LOGGER.info(f"[{district}] Navigated to Meetings page.")
            time.sleep(5)
        except Exception as e:
            LOGGER.error(f"[{district}] ❌ Failed to reach Meetings page: {e}")
            log_doc_info(
                sheets_service, SPREADSHEET_ID, TAB_NAME,
                nces, district, "",
                "Could not reach Meetings page", ctrl_f_term,
                url=link,
            )
            return meeting_records

        try:
            wait.until(
                EC.presence_of_element_located(
                    (By.XPATH, "//h2[contains(text(), 'Recent Meetings')]")
                )
            )
            meeting_items = wait.until(
                EC.presence_of_all_elements_located(
                    (By.XPATH, "//div[contains(@id, 'RecentMeetings')]//li[a]")
                )
            )
        except TimeoutException:
            LOGGER.error(f"[{district}] ❌ Recent Meetings list did not load.")
            log_doc_info(
                sheets_service, SPREADSHEET_ID, TAB_NAME,
                nces, district, "",
                "Could not find Meetings Element — Bad Link?", ctrl_f_term,
                url=link,
            )
            return meeting_records

        last_date = datetime.strptime(check_date, "%m-%d-%y")

        meeting_urls: List[Tuple[str, str]] = []
        for li in meeting_items:
            try:
                a_tag = li.find_element(By.TAG_NAME, "a")
                title = driver.execute_script(
                    "return arguments[0].textContent;", a_tag
                ).strip()
                href = a_tag.get_attribute("href")
                if not href:
                    continue

                try:
                    _, date_str = parse_meeting_info(title)
                    if datetime.strptime(date_str, "%m-%d-%y") <= last_date:
                        LOGGER.info(f"  👵 Skipping '{title}' — on/before {check_date}")
                        continue
                except Exception:
                    pass

                meeting_urls.append((title, href))
                LOGGER.debug(f"  Queued: '{title}' → {href}")
            except Exception as e:
                LOGGER.warning(f"  Could not extract link from meeting item: {e}")

        LOGGER.info(f"[{district}] {len(meeting_urls)} meeting(s) queued.")

        for meeting_index, (title_text, meeting_url) in enumerate(meeting_urls):
            LOGGER.info(
                f"\n{'─'*60}\n"
                f"  [{district}] Meeting {meeting_index + 1}/{len(meeting_urls)}: {title_text}\n"
                f"  URL: {meeting_url}\n"
                f"{'─'*60}"
            )

            meeting_date_str = ""
            try:
                driver.get(meeting_url)
                time.sleep(5)

                try:
                    title_el = WebDriverWait(driver, 10).until(
                        EC.visibility_of_element_located(
                            (By.ID, "ctl00_MainContent_MeetingTitle")
                        )
                    )
                    meeting_title = title_el.text.strip()
                except Exception:
                    meeting_title = title_text
                    LOGGER.warning(f"  MeetingTitle element not found — using list title.")

                display_title = meeting_title or title_text

                try:
                    meeting_type, meeting_date_str = parse_meeting_info(display_title)
                    meeting_date_formatted = datetime.strptime(meeting_date_str, "%m-%d-%y")
                except Exception as e:
                    LOGGER.warning(f"  Could not parse date from '{display_title}': {e}")
                    log_doc_info(
                        sheets_service, SPREADSHEET_ID, TAB_NAME,
                        nces, district, "UNKNOWN",
                        "Unformatted title or meeting date", ctrl_f_term,
                        url=meeting_url, paragraph_text=display_title,
                    )
                    continue

                LOGGER.info(
                    f"  Type: '{meeting_type}' | Date: '{meeting_date_str}' | "
                    f"Check: '{check_date}'"
                )

                if meeting_date_formatted <= last_date:
                    LOGGER.info(f"  👵 Meeting {meeting_date_str} ≤ check_date — skipping.")
                    continue

                meeting_id = log_meeting_to_db(
                    nces_id=nces,
                    meeting_date=meeting_date_str,
                    meeting_link=meeting_url,
                    meeting_type=meeting_type,
                )

                meeting_record = MeetingRecord(
                    date=meeting_date_str,
                    meeting_type=meeting_type,
                    keyword=ctrl_f_term,
                )
                meeting_records.append(meeting_record)

                search_and_download_agenda_attachments(
                    driver=driver,
                    nces=nces,
                    district=district,
                    meeting_title=display_title,
                    meeting_url=meeting_url,
                    row_date=meeting_date_formatted,
                    meeting_type=meeting_type,
                    link=link,
                    prompt_df=prompt_df,
                    processed_urls=set(),
                    meeting_record=meeting_record,
                    meeting_id=meeting_id,
                )

                LOGGER.info(
                    f"  ✅ Meeting done — downloaded={meeting_record.downloaded}, "
                    f"dupes={meeting_record.dupes}, errors={meeting_record.errors}"
                )

            except Exception as e:
                LOGGER.error(f"  ❌ Unexpected error on '{title_text}': {e}")
                LOGGER.debug(traceback.format_exc())
                if meeting_records and meeting_records[-1].date == meeting_date_str:
                    meeting_records[-1].errors += 1

            finally:
                try:
                    driver.switch_to.default_content()
                except Exception:
                    pass
                time.sleep(random.uniform(2, 5))

        total_dl   = sum(r.downloaded for r in meeting_records)
        total_dupe = sum(r.dupes      for r in meeting_records)
        total_err  = sum(r.errors     for r in meeting_records)
        LOGGER.info(
            f"\n{'═'*60}\n"
            f"  [{district}] COMPLETE — Meetings: {len(meeting_records)} | "
            f"Downloaded: {total_dl} | Dupes: {total_dupe} | Errors: {total_err}\n"
            f"{'═'*60}"
        )

    finally:
        LOGGER.info(f"[{district}] Quitting Chrome driver.")
        try:
            driver.quit()
        except Exception as e:
            LOGGER.warning(f"  Error quitting driver: {e}")

    return meeting_records


# =============================================================================
# SECTION 3 — BATCH RUNNER
# =============================================================================

if __name__ == "__main__":

    LOGGER.info(
        f"\n{'═'*60}\n"
        f"  DILIGENT CRAWLER — STARTING RUN\n"
        f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"{'═'*60}"
    )

    LOGGER.info("Bootstrapping: downloading runtime assets from GCS...")
    CSV_INPUT_PATH = download_blob_to_tmp(BUCKET_NAME, DILIGENT_CSV_INPUT_PATH_NAME)
    CRED_PATH      = download_blob_to_tmp(BUCKET_NAME, CRED_PATH_NAME)
    TOKEN_PATH     = download_blob_to_tmp(BUCKET_NAME, "JsonKeys/token.pkl")

    load_all_db_credentials()

    LOGGER.info("Authenticating Google Drive and Sheets...")
    drive_service, sheets_service = get_authenticated_services(CRED_PATH, TOKEN_PATH)
    LOGGER.info("✅ Google services authenticated.")

    # Inject drive_service into the scraper module so upload_file_to_folder can use it.
    dil_scraper.drive_service = drive_service

    df        = pd.read_csv(CSV_INPUT_PATH)
    prompt_df = get_all_prompts_df()
    LOGGER.info(f"CSV: {len(df)} district(s). Prompts: {len(prompt_df)} entry/entries.")

    error_docs = []
    run_stats  = {
        "total_districts":     len(df),
        "districts_attempted": 0,
        "districts_succeeded": 0,
        "districts_errored":   0,
        "error_districts":     [],
        "total_attachments":   0,
        "total_downloaded":    0,
        "total_dupes":         0,
        "total_errors":        0,
    }

    for idx, row in df.iterrows():
        link              = row["Link"]
        nces              = str(row["NCES ID"])
        district          = str(row["District Name"]).strip()
        last_meeting_date = str(row["Check Date"]).strip().replace("/", "-")
        ctrl_f_term       = str(row.get("Search Term", "minutes")).strip()

        if not ctrl_f_term or ctrl_f_term.lower() == "nan":
            ctrl_f_term = "minutes"
        if not district or district.lower() == "nan":
            LOGGER.debug(f"Row {idx}: empty district — skipping.")
            continue

        run_stats["districts_attempted"] += 1
        LOGGER.info(
            f"\n{'═'*60}\n"
            f"Row {idx + 1}/{len(df)} — {district} (NCES: {nces})\n"
            f"{'═'*60}"
        )

        try:
            district_records = main(
                nces=nces, district=district, link=link,
                prompt_df=prompt_df, ctrl_f_term=ctrl_f_term,
                check_date=last_meeting_date,
            )
            LOGGER.info(f"✅ {district} completed.")
            run_stats["districts_succeeded"] += 1

            for rec in district_records:
                run_stats["total_attachments"] += rec.total
                run_stats["total_downloaded"]  += rec.downloaded
                run_stats["total_dupes"]       += rec.dupes
                run_stats["total_errors"]      += rec.errors

            log_to_overall_sbd_log(
                nces_id=int(nces), meeting_records=district_records, notes=""
            )

        except Exception as e:
            LOGGER.error(f"❌ Fatal error for '{district}': {e}")
            LOGGER.debug(traceback.format_exc())
            error_docs.append((district, str(e)))
            run_stats["districts_errored"] += 1
            run_stats["error_districts"].append(district)
            log_to_overall_sbd_log(
                nces_id=int(nces), meeting_records=[],
                notes=f"CRAWLER ERROR: {str(e)[:500]}",
            )

        time.sleep(random.uniform(10, 40))

    LOGGER.info(f"\n{'═'*60}\n  BATCH RUN COMPLETE\n{'═'*60}")
    debug_summarize_run_stats(run_stats)

    if error_docs:
        LOGGER.warning("Districts with errors:")
        for district_name, err_msg in error_docs:
            LOGGER.warning(f"  • {district_name}: {err_msg}")
    else:
        LOGGER.info("Clean run — no errors. 🎉")
