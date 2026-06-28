# ─────────────────────────────────────────────────────────────────────────────
# crawlers/boardbook/main.py
# BoardBook Premier — batch orchestration entry point.
# Run from the repo root: python -m crawlers.boardbook.main
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import random
import re
import shutil
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from typing import List, Optional, Tuple

import pandas as pd
from dateutil import parser as dateutil_parser
from dateutil.parser import ParserError
from google.cloud import storage
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import NoSuchElementException, TimeoutException

from config.settings import (
    BUCKET_NAME,
    CSV_INPUT_PATH_NAME,
    CSV_OUTPUT_PATH_NAME,
    CRED_PATH_NAME,
    DOWNLOAD_DIR,
)
from core.gcs import download_blob_to_tmp, upload_file_to_gcs
from core.models import AttachmentRecord, MeetingRecord
from core.utils import setup_logger, debug_summarize_run_stats
from core.database import (
    load_all_db_credentials,
    get_all_prompts_df,
    log_meeting_to_db,
    log_to_overall_sbd_log,
    log_error_to_db,
    upsert_district_config,
    insert_batch_run,
    close_batch_run,
    update_district_last_crawl,
)
from core.driver import create_undetected_driver
from core.google_auth import get_authenticated_services
from core.humanize import human_click, random_idle
from crawlers.boardbook.scraper import search_and_download_agenda_attachments

LOGGER = setup_logger(log_level="INFO")

# Tune to available CPU cores / RAM (2 is safe; 4 viable on 8-core / ≥16 GB).
WORKER_PROCESSES = 2

# Global drive_service resolved in __main__ and injected into scraper sub-module.
drive_service = None

# Shared prompt DataFrame set in __main__ before ProcessPoolExecutor fork.
_SHARED_PROMPT_DF = None


# ═════════════════════════════════════════════════════════════════════════════
# 1. MAIN PER-DISTRICT SCRAPER PIPELINE
# ═════════════════════════════════════════════════════════════════════════════

def main_district_pipeline(
    nces: str,
    district: str,
    link: str,
    prompt_df: pd.DataFrame,
    ctrl_f_term: str,
    check_date: str = "06-01-25",
    district_config_id: Optional[int] = None,
) -> List[MeetingRecord]:
    """Runs the full BoardBook Premier scrape sequence for a single district."""

    meeting_records: List[MeetingRecord] = []
    chrome_profile_dir = f'/tmp/chrome_profile_{os.getpid()}'

    try:
        driver = create_undetected_driver()
    except Exception as e:
        LOGGER.error(f"[{district}] ❌ Unable to create Chrome driver: {e}")
        return meeting_records

    try:
        try:
            last_date = datetime.strptime(check_date, "%m-%d-%y")
        except ValueError:
            LOGGER.error(f"❌ Invalid date format '{check_date}' — defaulting to 06-01-25.")
            last_date = datetime.strptime("06-01-25", "%m-%d-%y")

        LOGGER.info(f"\n{'═'*60}\n DISTRICT PIPELINE START: {district} | NCES: {nces} | Check Date: {check_date}\n{'═'*60}")
        driver.get(link)
        time.sleep(5)

        main_window = driver.current_window_handle

        try:
            WebDriverWait(driver, 20).until(
                EC.presence_of_element_located((By.ID, "PublicMeetingsTable"))
            )
        except TimeoutException:
            LOGGER.error(f"[{district}] ❌ PublicMeetingsTable missing — terminating.")
            return meeting_records

        row_index = 0
        while True:
            rows = driver.find_elements(By.CSS_SELECTOR, "#PublicMeetingsTable tbody tr.row-for-board")
            if row_index >= len(rows):
                LOGGER.info(f"Finished processing all {len(rows)} rows for {district}.")
                break

            row = rows[row_index]
            try:
                info_text    = row.find_element(By.CSS_SELECTOR, "td:first-child div").text.strip()
                date_str_raw = info_text.split(" - ")[0].split(" at ")[0]
                date_match   = re.search(
                    r'[A-Za-z]+ \d{1,2},? \d{4}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}',
                    date_str_raw
                )
                if not date_match:
                    raise ValueError(f"No parseable date in: {date_str_raw!r}")

                try:
                    meeting_date_formatted = dateutil_parser.parse(date_match.group(0), fuzzy=True)
                except ParserError:
                    raise ValueError(f"Could not parse date from: {date_str_raw!r}")

                meeting_type = info_text.split(" - ")[1] if " - " in info_text else "Meeting"
                LOGGER.debug(f"Row {row_index}: Date={date_str_raw}, Type={meeting_type}")

                if meeting_date_formatted <= last_date:
                    LOGGER.info(f"   👵 Skipping historical meeting: {date_str_raw}")
                    row_index += 1
                    continue

                try:
                    agenda_link = row.find_element(By.LINK_TEXT, "Agenda")
                    meeting_url = agenda_link.get_attribute("href")
                except NoSuchElementException:
                    LOGGER.warning(f"   ⚠️ No Agenda link for {date_str_raw} — skipping.")
                    row_index += 1
                    continue

                LOGGER.info(f"   → Processing: {date_str_raw} | {meeting_type}")

                meeting_id = log_meeting_to_db(
                    nces_id             = nces,
                    meeting_date        = meeting_date_formatted.strftime("%m-%d-%y"),
                    meeting_link        = meeting_url,
                    meeting_type        = meeting_type,
                    platform            = "BOARDBOOK",
                    district_config_id  = district_config_id,
                )

                meeting_record = MeetingRecord(
                    date         = meeting_date_formatted.strftime("%m-%d-%y"),
                    meeting_type = meeting_type,
                    keyword      = ctrl_f_term,
                )
                meeting_records.append(meeting_record)

                human_click(driver, agenda_link)
                time.sleep(5)

                search_and_download_agenda_attachments(
                    driver         = driver,
                    nces           = nces,
                    district       = district,
                    meeting_title  = info_text,
                    meeting_url    = meeting_url,
                    row_date       = meeting_date_formatted,
                    meeting_type   = meeting_type,
                    link           = link,
                    prompt_df      = prompt_df,
                    processed_urls = set(),
                    meeting_record = meeting_record,
                    meeting_id     = meeting_id,
                )

                driver.get(link)
                WebDriverWait(driver, 20).until(
                    EC.presence_of_element_located((By.ID, "PublicMeetingsTable"))
                )
                time.sleep(2)

            except Exception as e:
                LOGGER.error(f"   ❌ Error processing row {row_index}: {e}")
                LOGGER.debug(traceback.format_exc())

            row_index += 1
            random_idle()

        total_dl = sum(r.downloaded for r in meeting_records)
        LOGGER.info(
            f"\n{'═'*60}\n  [{district}] COMPLETE | "
            f"Meetings: {len(meeting_records)} | Downloads: {total_dl}\n{'═'*60}"
        )

    finally:
        try:
            driver.quit()
        except Exception:
            pass
        shutil.rmtree(chrome_profile_dir, ignore_errors=True)
        LOGGER.debug(f"[{district}] Chrome process and profile dir cleaned up.")

    return meeting_records


# ═════════════════════════════════════════════════════════════════════════════
# 2. PARALLEL WORKER ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

def _worker(task: Tuple) -> Tuple[str, str, List[MeetingRecord], Optional[str]]:
    """Top-level function executed inside each child process."""
    idx, nces, district, link, last_meeting_date, ctrl_f_term, district_config_id = task
    prompt_df = _SHARED_PROMPT_DF

    worker_logger = setup_logger(log_level="INFO")
    worker_logger.info(f"[Worker PID {os.getpid()}] Starting: {district} (row {idx})")

    try:
        load_all_db_credentials()

        startup_delay = (idx % WORKER_PROCESSES) * 6 + random.uniform(0, 2)
        if startup_delay > 0:
            worker_logger.info(f"[Worker PID {os.getpid()}] Staggered startup: {startup_delay:.1f}s")
            time.sleep(startup_delay)

        records = main_district_pipeline(
            nces               = nces,
            district           = district,
            link               = link,
            prompt_df          = prompt_df,
            ctrl_f_term        = ctrl_f_term,
            check_date         = last_meeting_date,
            district_config_id = district_config_id,
        )
        return (nces, district, records, None)

    except Exception as e:
        worker_logger.error(f"[Worker PID {os.getpid()}] Fatal error for '{district}': {e}")
        return (nces, district, [], str(e))


# ═════════════════════════════════════════════════════════════════════════════
# 3. MAIN BATCH SYSTEM ENTRY POINT
# ═════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    LOGGER.info(
        f"\n{'═'*60}\n"
        f"   BOARDBOOK CRAWLER — RUNTIME START\n"
        f"   {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"{'═'*60}"
    )

    LOGGER.info("Bootstrapping: downloading runtime assets from GCS...")
    CSV_INPUT_PATH = download_blob_to_tmp(BUCKET_NAME, CSV_INPUT_PATH_NAME)
    CRED_PATH      = download_blob_to_tmp(BUCKET_NAME, CRED_PATH_NAME)
    TOKEN_PATH     = download_blob_to_tmp(BUCKET_NAME, "JsonKeys/token.pkl")

    if not os.path.exists(DOWNLOAD_DIR):
        os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    load_all_db_credentials()

    LOGGER.info("Authenticating Google Drive...")
    drive_service, _ = get_authenticated_services(CRED_PATH, TOKEN_PATH)
    LOGGER.info("✅ Google services authenticated.")

    LOGGER.info(f"Loading district CSV: {CSV_INPUT_PATH}")
    df_targets = pd.read_csv(CSV_INPUT_PATH)

    _SHARED_PROMPT_DF = get_all_prompts_df()
    LOGGER.info(f"Prompts loaded: {len(_SHARED_PROMPT_DF)} entries.")

    batch_run_id = insert_batch_run("BOARDBOOK", "super", WORKER_PROCESSES)

    error_docs = []
    run_stats = {
        "total_districts":     len(df_targets),
        "districts_attempted": 0,
        "districts_succeeded": 0,
        "districts_errored":   0,
        "error_districts":     [],
        "total_attachments":   0,
        "total_downloaded":    0,
        "total_dupes":         0,
        "total_errors":        0,
    }

    tasks: List[Tuple] = []
    nces_to_config_id: dict = {}
    for idx, row in df_targets.iterrows():
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

        district_config_id = upsert_district_config(
            nces_id=nces, district_name=district, platform="BOARDBOOK",
            crawler_type="super", link=link, check_date=last_meeting_date,
        )
        nces_to_config_id[nces] = district_config_id
        tasks.append((idx, nces, district, link, last_meeting_date, ctrl_f_term, district_config_id))

    run_stats["total_districts"] = len(tasks)
    LOGGER.info(f"Dispatching {len(tasks)} district(s) across {WORKER_PROCESSES} worker(s).")

    with ProcessPoolExecutor(max_workers=WORKER_PROCESSES) as executor:
        future_to_district = {executor.submit(_worker, task): task[2] for task in tasks}

        for future in as_completed(future_to_district):
            district_name = future_to_district[future]
            run_stats["districts_attempted"] += 1

            try:
                nces, district, district_records, error_msg = future.result()
            except Exception as e:
                LOGGER.error(f"❌ Future failed for '{district_name}': {e}")
                LOGGER.debug(traceback.format_exc())
                error_docs.append((district_name, str(e)))
                run_stats["districts_errored"] += 1
                run_stats["error_districts"].append(district_name)
                continue

            if error_msg:
                LOGGER.error(f"❌ Fatal failure for '{district}': {error_msg}")
                error_docs.append((district, error_msg))
                run_stats["districts_errored"] += 1
                run_stats["error_districts"].append(district)

                log_to_overall_sbd_log(
                    nces_id         = nces,
                    meeting_records = [],
                    notes           = f"CRAWLER FATAL ERROR: {error_msg[:500]}",
                    crawler_type    = "BOARDBOOK",
                    batch_run_id    = batch_run_id,
                )
            else:
                LOGGER.info(f"✅ {district} completed.")
                run_stats["districts_succeeded"] += 1

                for rec in district_records:
                    run_stats["total_attachments"] += rec.total
                    run_stats["total_downloaded"]  += rec.downloaded
                    run_stats["total_dupes"]       += rec.dupes
                    run_stats["total_errors"]      += rec.errors

                log_to_overall_sbd_log(
                    nces_id         = nces,
                    meeting_records = district_records,
                    notes           = "",
                    crawler_type    = "BOARDBOOK",
                    batch_run_id    = batch_run_id,
                )

                config_id = nces_to_config_id.get(nces)
                if config_id:
                    update_district_last_crawl(config_id)

    close_batch_run(
        batch_run_id = batch_run_id,
        succeeded    = run_stats["districts_succeeded"],
        errored      = run_stats["districts_errored"],
    )

    LOGGER.info(f"\n{'═'*60}\n   BATCH RUN FINISHED\n{'═'*60}")
    debug_summarize_run_stats(run_stats)

    if error_docs:
        LOGGER.warning("Districts with failures:")
        for name, message in error_docs:
            LOGGER.warning(f"   • {name}: {message}")
    else:
        LOGGER.info("All operations completed with zero failures. 🎉")
