# ─────────────────────────────────────────────────────────────────────────────
# crawlers/simbli/main.py
# Simbli eBoard — sequential batch orchestration entry point.
# Run from the repo root: python -m crawlers.simbli.main
# ─────────────────────────────────────────────────────────────────────────────

import logging
import random
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from typing import List, Optional, Tuple

import pandas as pd

import crawlers.simbli.scraper as simbli_scraper
from config.settings import (
    BUCKET_NAME,
    CRED_PATH_NAME,
    SIMBLI_CSV_INPUT_PATH_NAME,
)
from core.database import (
    get_all_prompts_df,
    load_all_db_credentials,
    log_error_to_db,
    log_meeting_to_db,
    log_to_overall_sbd_log,
)
from core.document_utils import classify_doc_type
from core.driver import create_undetected_driver
from core.gcs import download_blob_to_tmp
from core.google_auth import get_authenticated_services
from core.humanize import random_idle, slow_scroll
from core.models import MeetingRecord
from core.utils import debug_summarize_run_stats, setup_logger
from selenium.common.exceptions import WebDriverException

LOGGER = setup_logger(log_level="INFO")

# Tune to available CPU cores / RAM (2 is safe; 4 viable on 8-core / ≥16 GB).
WORKER_PROCESSES = 2
# Set to False to run districts sequentially (useful for debugging).
PARALLEL_ENABLED = True

drive_service = None

# Shared prompt DataFrame — set in __main__ before ProcessPoolExecutor fork.
_SHARED_PROMPT_DF = None


# =============================================================================
# SECTION 1 — PER-DISTRICT PIPELINE
# =============================================================================

def run_district(
    nces:        str,
    district:    str,
    link:        str,
    prompt_df:   pd.DataFrame,
    ctrl_f_term: str,
    check_date:  str = "06-01-25",
) -> List[MeetingRecord]:
    """Run the full Simbli scrape pipeline for one district."""
    last_date  = datetime.strptime(check_date, "%m-%d-%y")
    driver     = create_undetected_driver()
    meeting_records: List[MeetingRecord] = []

    LOGGER.info(
        f"\n{'═'*60}\n"
        f"  SIMBLI DISTRICT: {district}  |  NCES: {nces}\n"
        f"  Link: {link}\n"
        f"  check_date: {check_date}\n"
        f"{'═'*60}"
    )

    try:
        driver.get(link)
        slow_scroll(driver)
        random_idle()

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        WebDriverWait(driver, 30).until(
            EC.presence_of_all_elements_located((By.CSS_SELECTOR, "tr"))
        )

        row_index = 0

        while True:
            try:
                rows = driver.find_elements(By.CSS_SELECTOR, "tr")
                data_rows = [
                    r for r in rows
                    if r.find_elements(By.CSS_SELECTOR, "td:first-child span[title]")
                ]

                if row_index >= len(data_rows):
                    LOGGER.info(
                        f"  [{district}] All {len(data_rows)} meeting row(s) processed."
                    )
                    break

                row = data_rows[row_index]

                # ── Parse date ─────────────────────────────────────────────
                try:
                    date_span = row.find_element(By.CSS_SELECTOR, "td:first-child span[title]")
                    date_raw  = date_span.get_attribute("title") or date_span.text.strip()
                    date_part = date_raw.split()[0] if date_raw else ""
                    row_date  = datetime.strptime(date_part, "%m/%d/%Y")
                except Exception as e:
                    LOGGER.warning(f"  Row {row_index}: Could not parse date — {e}")
                    row_index += 1
                    continue

                if row_date <= last_date:
                    LOGGER.debug(
                        f"  Row {row_index}: {row_date.date()} ≤ check_date — skipping."
                    )
                    row_index += 1
                    continue

                # ── Parse meeting title and type ───────────────────────────
                try:
                    title_link = row.find_element(By.CSS_SELECTOR, "td:nth-child(2) a")
                    title      = title_link.text.strip()
                except Exception:
                    title = f"Meeting_{row_index}"

                try:
                    type_cell    = row.find_element(By.CSS_SELECTOR, "td:nth-child(4)")
                    meeting_type = type_cell.text.strip() or classify_doc_type(title)
                except Exception:
                    meeting_type = classify_doc_type(title)

                meeting_date_str = row_date.strftime("%m-%d-%y")

                LOGGER.info(
                    f"\n{'─'*60}\n"
                    f"  Row {row_index}: {title} | {row_date.date()} | {meeting_type}\n"
                    f"{'─'*60}"
                )

                # ── Log meeting to DB ──────────────────────────────────────
                meeting_id = log_meeting_to_db(
                    nces_id=nces,
                    meeting_date=meeting_date_str,
                    meeting_link=link,
                    meeting_type=meeting_type,
                )

                meeting_record = MeetingRecord(
                    date=meeting_date_str,
                    meeting_type=meeting_type,
                    keyword=ctrl_f_term,
                )
                meeting_record.meeting_id = meeting_id
                meeting_records.append(meeting_record)

                # ── Navigate to meeting ────────────────────────────────────
                try:
                    title_link = row.find_element(By.CSS_SELECTOR, "td:nth-child(2) a")
                    mtg_url    = title_link.get_attribute("href") or link
                    driver.get(mtg_url)
                    WebDriverWait(driver, 30).until(
                        lambda d: d.execute_script("return document.readyState") == "complete"
                    )
                    time.sleep(random.uniform(3, 6))
                except Exception as nav_err:
                    LOGGER.error(f"  Navigation failed for row {row_index}: {nav_err}")
                    meeting_record.errors += 1
                    driver.get(link)
                    time.sleep(5)
                    row_index += 1
                    continue

                # ── Process agenda items ───────────────────────────────────
                simbli_scraper.search_and_download_agenda_attachments(
                    driver=driver,
                    nces=nces,
                    district=district,
                    row_date=row_date,
                    meeting_type=meeting_type,
                    link=link,
                    prompt_df=prompt_df,
                    meeting_record=meeting_record,
                )

                LOGGER.info(
                    f"  ✅ Meeting done — downloaded={meeting_record.downloaded}, "
                    f"dupes={meeting_record.dupes}, errors={meeting_record.errors}"
                )

                # ── Return to district page ────────────────────────────────
                driver.get(link)
                WebDriverWait(driver, 30).until(
                    lambda d: d.execute_script("return document.readyState") == "complete"
                )
                time.sleep(random.uniform(5, 10))
                random_idle()

                row_index += 1

            except WebDriverException as wde:
                err_str = str(wde).lower()
                if "invalid session id" in err_str or "disconnected" in err_str:
                    LOGGER.error(
                        f"  ❌ Chrome session died at row {row_index} — resurrecting driver."
                    )
                    log_error_to_db(
                        error_type="SESSION_DIED",
                        message=str(wde)[:2000],
                        nces_id=nces,
                    )
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = create_undetected_driver()
                    driver.get(link)
                    WebDriverWait(driver, 30).until(
                        EC.presence_of_all_elements_located((By.CSS_SELECTOR, "tr"))
                    )
                    time.sleep(random.uniform(5, 10))
                    # Retry the same row
                    continue
                else:
                    LOGGER.error(
                        f"  ❌ WebDriverException at row {row_index}: {wde}"
                    )
                    log_error_to_db(
                        error_type="WEBDRIVER_ERROR",
                        message=str(wde)[:2000],
                        stack_trace=traceback.format_exc(),
                        nces_id=nces,
                    )
                    row_index += 1
                    driver.get(link)
                    time.sleep(5)

    finally:
        LOGGER.info(f"  [{district}] Quitting Chrome driver.")
        try:
            driver.quit()
        except Exception as e:
            LOGGER.warning(f"  Error quitting driver: {e}")

    total_dl   = sum(r.downloaded for r in meeting_records)
    total_dupe = sum(r.dupes      for r in meeting_records)
    total_err  = sum(r.errors     for r in meeting_records)
    LOGGER.info(
        f"\n{'═'*60}\n"
        f"  [{district}] COMPLETE — Meetings: {len(meeting_records)} | "
        f"Downloaded: {total_dl} | Dupes: {total_dupe} | Errors: {total_err}\n"
        f"{'═'*60}"
    )
    return meeting_records


# =============================================================================
# SECTION 2 — PARALLEL WORKER ENTRY POINT
# =============================================================================

def _worker(task: Tuple) -> Tuple[str, str, List[MeetingRecord], Optional[str]]:
    """Top-level function executed inside each child process."""
    idx, nces, district, link, last_meeting_date, ctrl_f_term = task
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
            nces=nces, district=district, link=link,
            prompt_df=prompt_df, ctrl_f_term=ctrl_f_term,
            check_date=last_meeting_date,
        )
        return (nces, district, records, None)

    except Exception as e:
        worker_logger.error(f"[Worker PID {os.getpid()}] Fatal error for '{district}': {e}")
        return (nces, district, [], str(e))


# =============================================================================
# SECTION 3 — BATCH RUNNER
# =============================================================================

if __name__ == "__main__":

    LOGGER.info(
        f"\n{'═'*60}\n"
        f"  SIMBLI CRAWLER — STARTING RUN\n"
        f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"{'═'*60}"
    )

    LOGGER.info("Bootstrapping: downloading runtime assets from GCS...")
    CSV_INPUT_PATH = download_blob_to_tmp(BUCKET_NAME, SIMBLI_CSV_INPUT_PATH_NAME)
    CRED_PATH      = download_blob_to_tmp(BUCKET_NAME, CRED_PATH_NAME)
    TOKEN_PATH     = download_blob_to_tmp(BUCKET_NAME, "JsonKeys/token.pkl")

    load_all_db_credentials()

    LOGGER.info("Authenticating Google Drive...")
    drive_service, _ = get_authenticated_services(CRED_PATH, TOKEN_PATH)
    LOGGER.info("✅ Google services authenticated.")

    # Inject drive_service into the scraper module
    simbli_scraper.drive_service = drive_service

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
        link        = row["Link"]
        nces        = str(row["NCES ID"])
        district    = str(row["District Name"]).strip()
        check_date  = str(row["Check Date"]).strip().replace("/", "-")
        ctrl_f_term = str(row.get("Search Term", "minutes")).strip()

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

        # Clean tmp PDFs older than 2 h before each district
        simbli_scraper.cleanup_tmp_pdfs(max_age_hours=2)

        try:
            district_records = run_district(
                nces=nces, district=district, link=link,
                prompt_df=prompt_df, ctrl_f_term=ctrl_f_term,
                check_date=check_date,
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
            log_error_to_db(
                error_type="DISTRICT_FATAL",
                message=str(e)[:2000],
                stack_trace=traceback.format_exc()[:5000],
                nces_id=nces,
            )
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
        LOGGER.info("Clean run — no errors.")
