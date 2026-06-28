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
from concurrent.futures import ProcessPoolExecutor, as_completed
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
)
from core.database import (
    get_all_prompts_df,
    load_all_db_credentials,
    log_error_to_db,
    log_meeting_to_db,
    log_to_overall_sbd_log,
    upsert_district_config,
    insert_batch_run,
    close_batch_run,
    update_district_last_crawl,
)
from core.driver import create_undetected_driver
from core.gcs import download_blob_to_tmp
from core.google_auth import get_authenticated_services
from core.humanize import random_idle
from core.models import AttachmentRecord, MeetingRecord
from core.utils import debug_summarize_run_stats, setup_logger, parse_check_date
from crawlers.diligent.scraper import search_and_download_agenda_attachments

LOGGER = setup_logger(log_level="INFO")

# Tune to available CPU cores / RAM (2 is safe; 4 viable on 8-core / ≥16 GB).
WORKER_PROCESSES = 2
# Set to False to run districts sequentially (useful for debugging).
PARALLEL_ENABLED = True

drive_service = None

# Shared prompt DataFrame — set in __main__ before ProcessPoolExecutor fork.
_SHARED_PROMPT_DF = None


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

def run_district(
    nces:               str,
    district:           str,
    link:               str,
    prompt_df:          pd.DataFrame,
    ctrl_f_term:        str,
    check_date:         str = "06-01-25",
    district_config_id: Optional[int] = None,
) -> List[MeetingRecord]:
    """Run the full Diligent scrape pipeline for one district."""
    driver = create_undetected_driver()
    meeting_records: List[MeetingRecord] = []

    try:
        LOGGER.info(
            f"\n{'═'*60}\n"
            f"  DILIGENT DISTRICT: {district}  |  NCES: {nces}\n"
            f"  Link: {link}  |  Check Date: {check_date}\n"
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
            log_error_to_db(error_type="NAV_ERROR", message="Could not reach Meetings page", nces_id=nces, platform="DILIGENT")
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
            log_error_to_db(error_type="NAV_ERROR", message="Could not find Meetings Element — Bad Link?", nces_id=nces, platform="DILIGENT")
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
                    log_error_to_db(error_type="PARSE_ERROR", message=f"Unformatted title or meeting date: {display_title[:200]}", nces_id=nces, platform="DILIGENT")
                    continue

                LOGGER.info(
                    f"  Type: '{meeting_type}' | Date: '{meeting_date_str}' | "
                    f"Check: '{check_date}'"
                )

                if meeting_date_formatted <= last_date:
                    LOGGER.info(f"  👵 Meeting {meeting_date_str} ≤ check_date — skipping.")
                    continue

                meeting_id = log_meeting_to_db(
                    nces_id            = nces,
                    meeting_date       = meeting_date_str,
                    meeting_link       = meeting_url,
                    meeting_type       = meeting_type,
                    platform           = "DILIGENT",
                    district_config_id = district_config_id,
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
# SECTION 3 — PARALLEL WORKER ENTRY POINT
# =============================================================================

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

        records = run_district(
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


# =============================================================================
# SECTION 4 — BATCH RUNNER
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

    LOGGER.info("Authenticating Google Drive...")
    drive_service, _ = get_authenticated_services(CRED_PATH, TOKEN_PATH)
    LOGGER.info("✅ Google services authenticated.")

    # Inject drive_service before forking — workers inherit it via fork.
    dil_scraper.drive_service = drive_service

    df = pd.read_csv(CSV_INPUT_PATH)

    _SHARED_PROMPT_DF = get_all_prompts_df()
    LOGGER.info(f"CSV: {len(df)} district(s). Prompts: {len(_SHARED_PROMPT_DF)} entries.")

    batch_run_id = insert_batch_run("DILIGENT", "super", WORKER_PROCESSES)

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

    tasks: List[Tuple] = []
    nces_to_config_id: dict = {}
    for idx, row in df.iterrows():
        link              = row["Link"]
        nces              = str(row["NCES ID"])
        district          = str(row["District Name"]).strip()
        last_meeting_date = parse_check_date(str(row["Check Date"]).strip().replace("/", "-"))
        ctrl_f_term       = str(row.get("Search Term", "minutes")).strip()

        if not ctrl_f_term or ctrl_f_term.lower() == "nan":
            ctrl_f_term = "minutes"
        if not district or district.lower() == "nan":
            LOGGER.debug(f"Row {idx}: empty district — skipping.")
            continue

        district_config_id = upsert_district_config(
            nces_id=nces, district_name=district, platform="DILIGENT",
            crawler_type="super", link=link, check_date=last_meeting_date,
        )
        nces_to_config_id[nces] = district_config_id
        tasks.append((idx, nces, district, link, last_meeting_date, ctrl_f_term, district_config_id))

    run_stats["total_districts"] = len(tasks)
    LOGGER.info(
        f"Dispatching {len(tasks)} district(s) — "
        f"{'parallel x' + str(WORKER_PROCESSES) if PARALLEL_ENABLED else 'sequential'}."
    )

    def _handle_result(nces, district, district_records, error_msg):
        if error_msg:
            LOGGER.error(f"❌ Fatal failure for '{district}': {error_msg}")
            error_docs.append((district, error_msg))
            run_stats["districts_errored"] += 1
            run_stats["error_districts"].append(district)
            log_to_overall_sbd_log(nces_id=nces, meeting_records=[], notes=f"CRAWLER FATAL ERROR: {error_msg[:500]}", crawler_type="DILIGENT", batch_run_id=batch_run_id)
        else:
            LOGGER.info(f"✅ {district} completed.")
            run_stats["districts_succeeded"] += 1
            for rec in district_records:
                run_stats["total_attachments"] += rec.total
                run_stats["total_downloaded"]  += rec.downloaded
                run_stats["total_dupes"]       += rec.dupes
                run_stats["total_errors"]      += rec.errors
            log_to_overall_sbd_log(nces_id=nces, meeting_records=district_records, notes="", crawler_type="DILIGENT", batch_run_id=batch_run_id)
            config_id = nces_to_config_id.get(nces)
            if config_id:
                update_district_last_crawl(config_id)

    if PARALLEL_ENABLED and WORKER_PROCESSES > 1:
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
                _handle_result(nces, district, district_records, error_msg)
    else:
        for task in tasks:
            run_stats["districts_attempted"] += 1
            nces, district, district_records, error_msg = _worker(task)
            _handle_result(nces, district, district_records, error_msg)
            time.sleep(random.uniform(10, 40))

    close_batch_run(
        batch_run_id = batch_run_id,
        succeeded    = run_stats["districts_succeeded"],
        errored      = run_stats["districts_errored"],
    )

    LOGGER.info(f"\n{'═'*60}\n  BATCH RUN COMPLETE\n{'═'*60}")
    debug_summarize_run_stats(run_stats)

    if error_docs:
        LOGGER.warning("Districts with errors:")
        for district_name, err_msg in error_docs:
            LOGGER.warning(f"  • {district_name}: {err_msg}")
    else:
        LOGGER.info("Clean run — no errors.")
