# ─────────────────────────────────────────────────────────────────────────────
# crawlers/boarddocs/main.py
# BoardDocs — batch orchestration entry point.
# Run from the repo root: python -m crawlers.boarddocs.main
# ─────────────────────────────────────────────────────────────────────────────

import logging
import os
import random
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from typing import List, Optional, Tuple

import pandas as pd

import crawlers.boarddocs.scraper as bd_scraper
from config.settings import (
    BOARDDOCS_CSV_INPUT_PATH_NAME,
    BUCKET_NAME,
    CRED_PATH_NAME,
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
from core.document_utils import classify_doc_type
from core.driver import create_undetected_driver
from core.gcs import download_blob_to_tmp
from core.google_auth import get_authenticated_services
from core.humanize import random_idle
from core.models import MeetingRecord
from core.utils import debug_summarize_run_stats, setup_logger

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
    nces:               str,
    district:           str,
    link:               str,
    prompt_df:          pd.DataFrame,
    ctrl_f_term:        str,
    check_date:         str = "06-01-25",
    district_config_id: Optional[int] = None,
) -> List[MeetingRecord]:
    """Run the full BoardDocs scrape pipeline for one district."""
    driver = create_undetected_driver()
    meeting_records: List[MeetingRecord] = []

    try:
        LOGGER.info(
            f"\n{'═'*60}\n"
            f"  BOARDDOCS DISTRICT: {district}  |  NCES: {nces}\n"
            f"  Link: {link}\n"
            f"{'═'*60}"
        )

        # ── Step 1: Collect meetings from JSON-LD ────────────────────────────
        meetings = bd_scraper.get_boarddocs_meetings(driver, link)
        if not meetings:
            LOGGER.warning(
                f"  [{district}] No meetings found in JSON-LD. "
                "Verify the URL points to a BoardDocs committee page."
            )
            log_error_to_db(error_type="NO_MEETINGS", message="No meetings found in JSON-LD", nces_id=nces, platform="BOARDDOCS")
            return meeting_records

        last_date = datetime.strptime(check_date, "%m-%d-%y")
        LOGGER.info(
            f"  [{district}] Processing meetings after {last_date.date()} "
            f"({len(meetings)} total found)."
        )

        # ── Step 2: Iterate meetings ─────────────────────────────────────────
        for mtg in meetings:
            title      = mtg["title"]
            start_date = mtg["start_date"]
            mtg_url    = mtg["url"]

            row_date = bd_scraper._parse_boarddocs_date(start_date)
            if row_date is None:
                LOGGER.warning(
                    f"  Skipping '{title}': could not parse date '{start_date}'."
                )
                continue

            if row_date <= last_date:
                LOGGER.debug(
                    f"  Skipping '{title}' ({row_date.date()}) — "
                    f"on/before check_date {check_date}."
                )
                continue

            LOGGER.info(
                f"\n{'─'*60}\n"
                f"  Processing: {title} | {row_date.date()}\n"
                f"{'─'*60}"
            )

            meeting_date_str = row_date.strftime("%m-%d-%y")
            meeting_type     = classify_doc_type(title)

            meeting_id = log_meeting_to_db(
                nces_id            = nces,
                meeting_date       = meeting_date_str,
                meeting_link       = mtg_url,
                meeting_type       = meeting_type,
                platform           = "BOARDDOCS",
                district_config_id = district_config_id,
            )

            meeting_record = MeetingRecord(
                date=meeting_date_str,
                meeting_type=meeting_type,
                keyword=ctrl_f_term,
            )
            meeting_records.append(meeting_record)

            main_window = driver.current_window_handle

            # ── Step 3: Navigate + open agenda ──────────────────────────────
            agenda_opened = bd_scraper.navigate_to_meeting_and_open_agenda(
                driver, mtg_url, title
            )
            if not agenda_opened:
                LOGGER.warning(f"  Could not open agenda for '{title}' — skipping.")
                meeting_record.errors += 1
                driver.get(link)
                time.sleep(5)
                continue

            # ── Step 4: Minutes icon (commented until validated) ─────────────
            # bd_scraper._download_boarddocs_minutes_icon(
            #     driver, main_window, nces, district, meeting_type,
            #     row_date, link, prompt_df, meeting_record, meeting_id,
            # )

            # ── Step 5: Iterate agenda items ─────────────────────────────────
            bd_scraper.search_and_download_agenda_attachments(
                driver=driver,
                nces=nces,
                district=district,
                row_date=row_date,
                meeting_type=meeting_type,
                link=link,
                prompt_df=prompt_df,
                meeting_record=meeting_record,
                meeting_id=meeting_id,
            )

            LOGGER.info(
                f"  ✅ Meeting done — downloaded={meeting_record.downloaded}, "
                f"dupes={meeting_record.dupes}, errors={meeting_record.errors}"
            )

            # ── Step 6: Return to district root ──────────────────────────────
            try:
                driver.switch_to.window(main_window)
            except Exception:
                pass
            driver.get(link)
            time.sleep(random.uniform(5, 10))
            random_idle()

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
# SECTION 2 — PARALLEL WORKER ENTRY POINT
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
# SECTION 3 — BATCH RUNNER
# =============================================================================

if __name__ == "__main__":

    LOGGER.info(
        f"\n{'═'*60}\n"
        f"  BOARDDOCS CRAWLER — STARTING RUN\n"
        f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        f"{'═'*60}"
    )

    LOGGER.info("Bootstrapping: downloading runtime assets from GCS...")
    CSV_INPUT_PATH = download_blob_to_tmp(BUCKET_NAME, BOARDDOCS_CSV_INPUT_PATH_NAME)
    CRED_PATH      = download_blob_to_tmp(BUCKET_NAME, CRED_PATH_NAME)
    TOKEN_PATH     = download_blob_to_tmp(BUCKET_NAME, "JsonKeys/token.pkl")

    load_all_db_credentials()

    LOGGER.info("Authenticating Google Drive...")
    drive_service, _ = get_authenticated_services(CRED_PATH, TOKEN_PATH)
    LOGGER.info("✅ Google services authenticated.")

    # Inject drive_service before forking — workers inherit it via fork.
    bd_scraper.drive_service = drive_service

    df = pd.read_csv(CSV_INPUT_PATH)

    _SHARED_PROMPT_DF = get_all_prompts_df()
    LOGGER.info(f"CSV: {len(df)} district(s). Prompts: {len(_SHARED_PROMPT_DF)} entries.")

    batch_run_id = insert_batch_run("BOARDDOCS", "super", WORKER_PROCESSES)

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

        district_config_id = upsert_district_config(
            nces_id=nces, district_name=district, platform="BOARDDOCS",
            crawler_type="super", link=link, check_date=check_date,
        )
        nces_to_config_id[nces] = district_config_id
        tasks.append((idx, nces, district, link, check_date, ctrl_f_term, district_config_id))

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
            log_to_overall_sbd_log(nces_id=nces, meeting_records=[], notes=f"CRAWLER FATAL ERROR: {error_msg[:500]}", crawler_type="BOARDDOCS", batch_run_id=batch_run_id)
        else:
            LOGGER.info(f"✅ {district} completed.")
            run_stats["districts_succeeded"] += 1
            for rec in district_records:
                run_stats["total_attachments"] += rec.total
                run_stats["total_downloaded"]  += rec.downloaded
                run_stats["total_dupes"]       += rec.dupes
                run_stats["total_errors"]      += rec.errors
            log_to_overall_sbd_log(nces_id=nces, meeting_records=district_records, notes="", crawler_type="BOARDDOCS", batch_run_id=batch_run_id)
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
