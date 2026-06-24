# ─────────────────────────────────────────────────────────────────────────────
# src/database.py
# ─────────────────────────────────────────────────────────────────────────────

import json
import logging
import traceback
from datetime import datetime
from typing import List, Optional, Tuple, Dict, Any

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
from google.cloud import secretmanager

# Internal relative configuration imports
from config.settings import (
    DB_SECRET_RESOURCE,
    DB_SECRET_RESOURCE_READER,
    MINHASH_NUM_PERM,
    MINHASH_SIM_THRESHOLD,
    MODEL_NAME,
    filename_run_counter
)

# Reference the named logger established in the setup routine
LOGGER = logging.getLogger("simbli_minutes")

# Global containers to store lazy-loaded credentials from Secret Manager
_DB_CREDS: Optional[dict] = None
_DB_CREDS_READER: Optional[dict] = None

# ═════════════════════════════════════════════════════════════════════════════
# SECRET MANAGER INGRESS & BOOTSTRAP HELPERS
# ═════════════════════════════════════════════════════════════════════════════

def load_all_db_credentials() -> None:
    """
    Fetches both write and read-only database credentials from GCP Secret Manager.
    Memoizes payloads into global parameters to prevent repetitive API round-trips.
    """
    global _DB_CREDS, _DB_CREDS_READER
    sm_client = secretmanager.SecretManagerServiceClient()

    # 1. Fetch Main Write Account Credentials
    if not _DB_CREDS:
        LOGGER.info(f"Fetching Main DB credentials from Secret Manager: {DB_SECRET_RESOURCE}...")
        try:
            secret_name = f"{DB_SECRET_RESOURCE}/versions/latest"
            response = sm_client.access_secret_version(request={"name": secret_name})
            payload = response.payload.data.decode("utf-8")
            _DB_CREDS = json.loads(payload)

            required_keys = {"host", "user", "password", "dbname", "port"}
            missing = required_keys - set(_DB_CREDS.keys())
            if missing:
                raise KeyError(f"Secret is missing required DB credential keys: {missing}")

            LOGGER.info(
                f"✅ Main DB credentials loaded — host={_DB_CREDS['host']}  "
                f"dbname={_DB_CREDS['dbname']}  port={_DB_CREDS['port']}  user={_DB_CREDS['user']}"
            )
        except Exception as e:
            LOGGER.critical(f"❌ Failed to load main DB credentials: {e}")
            raise

    # 2. Fetch Reader Account Credentials
    if not _DB_CREDS_READER:
        LOGGER.info(f"Fetching Reader DB credentials from Secret Manager: {DB_SECRET_RESOURCE_READER}...")
        try:
            secret_name = f"{DB_SECRET_RESOURCE_READER}/versions/latest"
            response = sm_client.access_secret_version(request={"name": secret_name})
            payload = response.payload.data.decode("utf-8")
            _DB_CREDS_READER = json.loads(payload)
            
            _DB_CREDS_READER.setdefault("port", "5432")

            missing = {"host", "user", "password", "dbname"} - set(_DB_CREDS_READER.keys())
            if missing:
                raise KeyError(f"Secret is missing required DB credential keys: {missing}")

            LOGGER.info(
                f"✅ Reader DB credentials loaded — host={_DB_CREDS_READER['host']}  "
                f"dbname={_DB_CREDS_READER['dbname']}  user={_DB_CREDS_READER['user']}"
            )
        except Exception as e:
            LOGGER.critical(f"❌ Failed to load reader DB credentials: {e}")
            raise


# ═════════════════════════════════════════════════════════════════════════════
# CONNECTION ENGINE FACTORY
# ═════════════════════════════════════════════════════════════════════════════

def get_db_connection(creds: Optional[dict] = None) -> psycopg2.extensions.connection:
    """
    Opens and returns a new psycopg2 secure connection block.
    
    Args:
        creds: Explicit credential dictionary overrides. If None, falls back
               to loading the standard write credentials from cache layer.
    """
    if creds is None:
        if not _DB_CREDS:
            load_all_db_credentials()
        c = _DB_CREDS
    else:
        c = creds

    LOGGER.debug(f"Opening PostgreSQL connection to {c['host']}:{c['port']}/{c['dbname']} as user '{c['user']}'...")
    
    conn = psycopg2.connect(
        host=c["host"],
        port=int(c["port"]),
        dbname=c["dbname"],
        user=c["user"],
        password=c["password"],
        connect_timeout=10,
        sslmode="require",
    )
    LOGGER.debug("PostgreSQL connection established.")
    return conn


# ═════════════════════════════════════════════════════════════════════════════
# DEDUPLICATION & COLLISION CHECKS (SELECT INTERACTION)
# ═════════════════════════════════════════════════════════════════════════════

def db_check_sha256_dupe(sha256_hex: str) -> bool:
    """Return True if sha256_hex already exists in documents.pdfs or crawler_hash."""
    LOGGER.debug(f"  SHA-256 dupe check: {sha256_hex[:16]}...")

    # Ensure assets are resolved before establishing network boundaries
    if not _DB_CREDS_READER or not _DB_CREDS:
        load_all_db_credentials()

    # -- Check 1: documents.pdfs (reader account boundary context) -----------------------
    try:
        with get_db_connection(_DB_CREDS_READER) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM documents.pdfs WHERE checksum = %s LIMIT 1;",
                    (sha256_hex,)
                )
                if cur.fetchone() is not None:
                    LOGGER.info("  SHA-256 dupe check → DUPE (found in documents.pdfs)")
                    return True
    except Exception as e:
        LOGGER.error(f"  DB SHA-256 check error (documents.pdfs, treating as novel): {e}")

    # -- Check 2: doc_collection.crawler_hash (main write system boundary context) -------
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM doc_collection.crawler_hash WHERE hash = %s LIMIT 1;",
                    (sha256_hex,)
                )
                if cur.fetchone() is not None:
                    LOGGER.info("  SHA-256 dupe check → DUPE (found in doc_collection.crawler_hash)")
                    return True
    except Exception as e:
        LOGGER.error(f"  DB SHA-256 check error (crawler_hash, treating as novel): {e}")

    LOGGER.info("  SHA-256 dupe check → novel")
    return False


def db_check_minhash_dupe(new_mh: Any, threshold: float = MINHASH_SIM_THRESHOLD) -> Tuple[bool, Optional[str]]:
    """
    Compare a new document's MinHash array against existing BIGINT[] arrays
    directly inside PostgreSQL using the custom jaccard_similarity function.
    """
    # ─── FIX: Convert datasketch MinHash object into a standard Python list of integers ───
    new_mh_array = new_mh.hashvalues.tolist()

    # SQL query that calculates similarity entirely on the database server
    sql = """
        SELECT pdf_link, jaccard_similarity(min_hash, %s) AS sim
        FROM doc_collection.crawler_hash 
        WHERE min_hash IS NOT NULL 
          AND jaccard_similarity(min_hash, %s) >= %s
        ORDER BY sim DESC
        LIMIT 1;
    """
    
    LOGGER.debug("  MinHash dupe check: Executing server-side Jaccard array evaluation...")
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                # Pass the array list twice (for select and filter) along with the threshold
                cur.execute(sql, (new_mh_array, new_mh_array, threshold))
                row = cur.fetchone()

        if row:
            pdf_link, similarity = row
            LOGGER.info(
                f"  MinHash dupe found: similarity={similarity:.4f} ≥ {threshold}  "
                f"matching_link={pdf_link}"
            )
            return True, pdf_link

        LOGGER.info("  MinHash dupe check → novel (no match above threshold).")
        return False, None

    except Exception as e:
        LOGGER.error(f"  DB MinHash check error (treating as novel): {e}")
        return False, None

def db_get_filename_count(base_file_name: str) -> int:
    """Count how many times base_file_name appears across global tracking environments."""
    LOGGER.debug(f"  Filename collision check for: '{base_file_name}'")
    count = 0

    if not _DB_CREDS_READER:
        load_all_db_credentials()

    # -- Check 1: documents.pdfs (Reader Engine Context) ---------------------------
    try:
        with get_db_connection(_DB_CREDS_READER) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM documents.pdfs WHERE pdf_name = %s;",
                    (base_file_name,)
                )
                count += cur.fetchone()[0]
    except Exception as e:
        LOGGER.error(f"  DB filename-count check error (documents.pdfs): {e}")

    # -- Check 2: doc_collection.crawler_hash (Main Write context) ----------------
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM doc_collection.crawler_hash WHERE file_name = %s;",
                    (base_file_name,)
                )
                count += cur.fetchone()[0]
    except Exception as e:
        LOGGER.error(f"  DB filename-count check error (doc_collection.crawler_hash): {e}")

    # -- Check 3: Volatile runtime memory cache checks -----------------------------
    count += filename_run_counter[base_file_name]

    LOGGER.debug(f"  Filename '{base_file_name}' existing count = {count}")
    return count


# ═════════════════════════════════════════════════════════════════════════════
# PROMPT INGESTION PIPELINE (SELECT INTERACTION)
# ═════════════════════════════════════════════════════════════════════════════

def get_all_prompts_df() -> pd.DataFrame:
    """Fetches all system prompts, sorting to resolve the newest structural deployment version."""
    LOGGER.info("Fetching latest prompts into a DataFrame...")
    try:
        with get_db_connection() as conn:
            query = """
                SELECT id, prompt_name, version, prompt_text 
                FROM doc_collection.prompts 
                ORDER BY version DESC
            """
            df = pd.read_sql(query, conn)
            df_latest = df.groupby('prompt_name').first().reset_index()
            df_latest.set_index('prompt_name', inplace=True)
            LOGGER.info(f"✅ Cached {len(df_latest)} latest prompts.")
            return df_latest
    except Exception as e:
        LOGGER.error(f"❌ Failed to fetch prompts table: {e}")
        return pd.DataFrame()


def get_prompt_info(df: pd.DataFrame, prompt_name: str) -> Tuple[str, Optional[int]]:
    """Retrieves localized active execution text and DB prompt references using dataframe caching."""
    try:
        if prompt_name in df.index:
            row = df.loc[prompt_name]
            return row['prompt_text'], int(row['id'])
        LOGGER.warning(f"⚠️ Prompt '{prompt_name}' not found. Available: {df.index.tolist()}")
        return "", None
    except Exception as e:
        LOGGER.error(f"❌ Error retrieving prompt '{prompt_name}': {e}")
        return "", None


# ═════════════════════════════════════════════════════════════════════════════
# TELEMETRY LOGGER INTERFACE FUNCTIONS (INSERT / UPDATE ONSHORE WRITES)
# ═════════════════════════════════════════════════════════════════════════════

def log_meeting_to_db(nces_id: str, meeting_date: str, meeting_link: str, meeting_type: str) -> Optional[int]:
    """INSERT a tracking transaction row into doc_collection.meetings table."""
    sql = """
        INSERT INTO doc_collection.meetings
            (nces_id, meeting_date, meeting_link, meeting_type, crawl_timestamp)
        VALUES
            (%(nces_id)s, %(meeting_date)s, %(meeting_link)s,
             %(meeting_type)s, %(crawl_timestamp)s)
        ON CONFLICT DO NOTHING
        RETURNING meeting_id;
    """
    try:
        parsed_date = datetime.strptime(meeting_date, "%m-%d-%y").date()
    except ValueError:
        LOGGER.warning(f"log_meeting_to_db: bad date string '{meeting_date}' — skipping.")
        return None

    params = {
        "nces_id":       str(nces_id),
        "meeting_date":    parsed_date,
        "meeting_link":    meeting_link,
        "meeting_type":    meeting_type,
        "crawl_timestamp": datetime.utcnow(),
    }

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()

        if row:
            meeting_id = row[0]
            LOGGER.info(f"   ✅ meetings row inserted — meeting_id={meeting_id}")
            return meeting_id

        # Secondary fallback context scanning constraint checking row allocations
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT meeting_id FROM doc_collection.meetings
                       WHERE nces_id = %s AND meeting_date = %s AND meeting_type = %s
                       LIMIT 1;""",
                    (str(nces_id), parsed_date, meeting_type),
                )
                existing = cur.fetchone()
        if existing:
            LOGGER.info(f"   ℹ️  Meeting already exists — meeting_id={existing[0]}")
            return existing[0]

        LOGGER.warning("  log_meeting_to_db: no id returned and no existing row found.")
        return None

    except Exception as e:
        LOGGER.error(f"   ❌ log_meeting_to_db failed: {e}")
        LOGGER.debug(traceback.format_exc())
        return None


def log_ai_usage(
    prompt_id: Optional[int],
    nces_id: str,
    model_name: str,
    tokens: Dict[str, Any],
    status: str = "SUCCESS",
    meeting_id: Optional[int] = None,
    document_id: Optional[int] = None,
    attachment_id: Optional[int] = None,
    prompt_snapshot: Optional[str] = None,
    response_json: Optional[str] = None,
) -> None:
    """Calculates granular run costs and inputs execution states into doc_collection.ai_calls."""
    input_tokens  = tokens.get("input_tokens")  or 0
    output_tokens = tokens.get("output_tokens") or 0
 
    if "gemini-2.5-flash-lite" in model_name.lower():
        input_rate  = 0.10 / 1_000_000
        output_rate = 0.40 / 1_000_000
    else:
        input_rate  = 0.10 / 1_000_000
        output_rate = 0.40 / 1_000_000
 
    cost_usd = (input_tokens * input_rate) + (output_tokens * output_rate)
 
    sql = """
        INSERT INTO doc_collection.ai_calls
            (prompt_id, meeting_id, attachment_id, document_id, nces_id,
             model_name, prompt_snapshot, response_json,
             input_tokens, output_tokens, cost_usd, status, called_at)
        VALUES
            (%(p_id)s, %(m_id)s, %(att_id)s, %(d_id)s, %(nces)s,
             %(model)s, %(prompt_snapshot)s, %(response_json)s,
             %(in_t)s, %(out_t)s, %(cost)s, %(status)s, %(ts)s)
    """
 
    params = {
        "p_id":            prompt_id,
        "m_id":            meeting_id,
        "att_id":          attachment_id,
        "d_id":            document_id,
        "nces":            str(nces_id),
        "model":           model_name,
        "prompt_snapshot": prompt_snapshot,
        "response_json":   response_json,
        "in_t":            input_tokens,
        "out_t":           output_tokens,
        "cost":            cost_usd,
        "status":          status,
        "ts":              datetime.utcnow(),
    }
 
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        LOGGER.debug(
            f"  💰 AI cost logged: ${cost_usd:.6f} "
            f"({input_tokens + output_tokens} tokens, status={status})"
        )
    except Exception as e:
        LOGGER.error(f"  ❌ Failed to log AI usage: {e}")


def log_uploaded_document(
    meeting_id: Optional[int],
    nces_id: str,
    file_name: str,
    drive_folder: str,
    google_drive_url: str,
    sha256_hash: str,
    minhash_obj: Optional[Any],
    category: str,
    doc_type: str,
    fiscal_year: Optional[str],
    page_count: Optional[int],
    attachment_id: Optional[int] = None,
) -> Optional[int]:
    """Registers operational outputs within doc_collection.uploaded_documents."""
    
    # ─── FIX: Extract the raw list of integers instead of a json.dumps text block ───
    minhash_array = minhash_obj.hashvalues.tolist() if minhash_obj is not None else None
 
    sql = """
        INSERT INTO doc_collection.uploaded_documents
            (attachment_id, meeting_id, nces_id, file_name, drive_folder,
             google_drive_url, sha256_hash, minhash_json, category, doc_type,
             fiscal_year, page_count, uploaded_at)
        VALUES
            (%(attachment_id)s, %(meeting_id)s, %(nces_id)s, %(file_name)s,
             %(drive_folder)s, %(google_drive_url)s, %(sha256_hash)s,
             %(minhash_array)s, %(category)s, %(doc_type)s,
             %(fiscal_year)s, %(page_count)s, %(uploaded_at)s)
        RETURNING document_id;
    """
 
    params = {
        "attachment_id":    attachment_id,
        "meeting_id":      meeting_id,
        "nces_id":          str(nces_id),
        "file_name":        file_name,
        "drive_folder":     drive_folder,
        "google_drive_url": google_drive_url,
        "sha256_hash":      sha256_hash,
        "minhash_array":    minhash_array, # Named mapping points to the raw integer list
        "category":         category,
        "doc_type":         doc_type,
        "fiscal_year":      fiscal_year,
        "page_count":       page_count,
        "uploaded_at":      datetime.utcnow(),
    }
 
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
 
        document_id = row[0] if row else None
        LOGGER.info(f"   ✅ uploaded_documents row inserted — document_id={document_id} | file_name={file_name}")
        return document_id
    except Exception as e:
        LOGGER.error(f"   ❌ log_uploaded_document failed: {e}")
        LOGGER.debug(traceback.format_exc())
        return None
    
def log_to_crawler_hash(
    nces_id: int,
    starting_link: str,
    pdf_link: str,
    downloaded: bool,
    sha256_hex: str,
    minhash_obj: Optional[Any],
    is_duplicate: bool,
    drive_link: Optional[str],
    file_name: Optional[str],
) -> None:
    """Inserts precise caching signatures into the main validation schema lookup table."""
    minhash_json = json.dumps(minhash_obj.hashvalues.tolist()) if minhash_obj is not None else None
    crawler_date = datetime.utcnow().date()

    sql = """
        INSERT INTO doc_collection.crawler_hash
            (nces_id, starting_link, pdf_link, downloaded, hash,
             min_hash, duplicate, crawler_date_ran, drive_link,
             manual_check, file_name)
        VALUES
            (%(nces_id)s, %(starting_link)s, %(pdf_link)s, %(downloaded)s,
             %(hash)s, %(min_hash)s, %(duplicate)s, %(crawler_date_ran)s,
             %(drive_link)s, %(manual_check)s, %(file_name)s)
        ON CONFLICT (pdf_link, hash) DO NOTHING;
    """

    params = {
        "nces_id":          int(nces_id),
        "starting_link":    starting_link,
        "pdf_link":         pdf_link,
        "downloaded":       downloaded,
        "hash":             sha256_hex or "",
        "min_hash":         minhash_json,
        "duplicate":        is_duplicate,
        "crawler_date_ran": crawler_date,
        "drive_link":       drive_link,
        "manual_check":     False,
        "file_name":        file_name,
    }

    LOGGER.debug(
        f"  Inserting crawler_hash row: nces={nces_id} pdf_link={pdf_link[:60]}... "
        f"downloaded={downloaded} duplicate={is_duplicate}"
    )

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        LOGGER.info("  ✅ crawler_hash row inserted.")
    except Exception as e:
        LOGGER.error(f"  ❌ Failed to insert crawler_hash row: {e}")
        LOGGER.debug(traceback.format_exc())


def log_to_overall_sbd_log(nces_id: int, meeting_records: List[Any], notes: str = "") -> None:
    """Upserts daily analytics records tracking aggregate scraper processing efficiency states."""
    num_new_meetings    = len(meeting_records)
    num_docs_downloaded = sum(r.downloaded for r in meeting_records)

    max_meeting_date = datetime.utcnow().date()
    if meeting_records:
        parsed_dates = []
        for rec in meeting_records:
            try:
                parsed_dates.append(datetime.strptime(rec.date, "%m-%d-%y").date())
            except ValueError:
                LOGGER.warning(f"  overall_sbd_log: could not parse date '{rec.date}'")
        if parsed_dates:
            max_meeting_date = max(parsed_dates)

    crawler_run_date = datetime.utcnow()

    sql = """
        INSERT INTO doc_collection.overall_sbd_log
            (nces_id, crawler_run_date, num_new_meetings, max_meeting_date,
             num_docs_downloaded, notes, crawler_type)
        VALUES
            (%(nces_id)s, %(crawler_run_date)s, %(num_new_meetings)s,
             %(max_meeting_date)s, %(num_docs_downloaded)s, %(notes)s,
             %(crawler_type)s)
        ON CONFLICT (nces_id, crawler_run_date) DO UPDATE SET
            num_new_meetings    = EXCLUDED.num_new_meetings,
            max_meeting_date    = EXCLUDED.max_meeting_date,
            num_docs_downloaded = EXCLUDED.num_docs_downloaded,
            notes               = EXCLUDED.notes;
    """

    params = {
        "nces_id":              int(nces_id),
        "crawler_run_date":    crawler_run_date,
        "num_new_meetings":    num_new_meetings,
        "max_meeting_date":    max_meeting_date,
        "num_docs_downloaded": num_docs_downloaded,
        "notes":               notes,
        "crawler_type":        "SIMBLI_MINUTES",
    }

    LOGGER.info(
        f"  Logging overall_sbd_log: nces={nces_id}  "
        f"run_date={crawler_run_date}  new_meetings={num_new_meetings}  "
        f"docs_downloaded={num_docs_downloaded}"
    )

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        LOGGER.info("  ✅ overall_sbd_log row upserted.")
    except Exception as e:
        LOGGER.error(f"  ❌ Failed to upsert overall_sbd_log row: {e}")
        LOGGER.debug(traceback.format_exc())


def log_contact_to_db(nces_id: str, name: str, email: str, phone: str, title: str) -> None:
    """Upserts extracted regional administrative contacts metadata directly into storage rosters."""
    if not name:
        return
    sql = """
        INSERT INTO doc_collection.contacts (nces_id, name, email, phone, title, first_seen_date, last_seen_date)
        VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (name, title) DO UPDATE SET
            last_seen_date = EXCLUDED.last_seen_date,
            email          = EXCLUDED.email,
            phone          = EXCLUDED.phone;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (nces_id, name, email or "", phone or "", title or ""))
            conn.commit()
        LOGGER.info(f"DB upsert OK → name='{name}' title='{title}'")
    except psycopg2.Error as e:
        LOGGER.error(f"DB upsert failed for '{name}': {e}")


def log_crawl_attachment(
    meeting_id: Optional[int],
    nces_id: str,
    pdf_url: str,
    attachment_name: Optional[str],
    outcome: str,
    sha256_hash: Optional[str] = None,
    minhash_json: Optional[str] = None,
    is_duplicate: bool = False
) -> int:
    """
    Logs attachment run traces. Uses ON CONFLICT ON CONSTRAINT to gracefully 
    handle multi-worker collisions without raising application exceptions.
    """
    # ─── FIX: Parse MinHash text array safely if string representation gets passed ───
    if minhash_json and isinstance(minhash_json, str) and minhash_json.startswith('['):
        try:
            minhash_data = json.loads(minhash_json)
        except:
            minhash_data = None
    else:
        minhash_data = minhash_json

    query = """
        INSERT INTO doc_collection.crawl_attachments (
            meeting_id, nces_id, pdf_url, attachment_name, outcome, sha256_hash, minhash_json, is_duplicate, crawled_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
        
        -- ─── FIX: Point unique key handling at our composite structural index ───
        ON CONFLICT ON CONSTRAINT unique_meeting_attachment 
        DO UPDATE SET
            outcome         = EXCLUDED.outcome,
            sha256_hash     = COALESCE(EXCLUDED.sha256_hash, doc_collection.crawl_attachments.sha256_hash),
            minhash_json    = COALESCE(EXCLUDED.minhash_json, doc_collection.crawl_attachments.minhash_json),
            is_duplicate    = EXCLUDED.is_duplicate,
            crawled_at      = NOW()
        RETURNING attachment_id;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (
                    meeting_id,
                    nces_id,
                    pdf_url,
                    attachment_name[:512] if attachment_name else None,
                    outcome[:32],
                    sha256_hash,
                    minhash_data, # Directly binds array list parameter
                    is_duplicate
                ))
                res = cur.fetchone()
                
                # Handle edge case where row upsert evaluates to a blank tuple
                attachment_id = res[0] if res else 0 
                conn.commit()
                LOGGER.debug(f"Logged attachment ID {attachment_id} to crawl_attachments. (Outcome: {outcome})")
                return attachment_id
    except Exception as e:
        LOGGER.error(f"Failed to write crawl attachment log: {e}")
        raise