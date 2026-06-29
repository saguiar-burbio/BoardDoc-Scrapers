# ─────────────────────────────────────────────────────────────────────────────
# src/database.py
# ─────────────────────────────────────────────────────────────────────────────

import hashlib
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

# Module-level caches for infrequently-changing DB lookups
_DRIVE_FOLDER_MAP: Optional[dict] = None
_MODEL_RATES: dict = {}

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

def _compute_lsh_bands(minhash_list: list, num_bands: int = 16, band_size: int = 8) -> list:
    """Splits a 128-permutation MinHash array into 16 band hashes (MD5 of each band)."""
    bands = []
    for i in range(num_bands):
        band = minhash_list[i * band_size : (i + 1) * band_size]
        bands.append(hashlib.md5(",".join(str(v) for v in band).encode()).hexdigest())
    return bands


def db_check_sha256_dupe(sha256_hex: str) -> bool:
    """Return True if sha256_hex already exists in documents.pdfs or crawler_hash."""
    LOGGER.debug(f"  SHA-256 dupe check: {sha256_hex[:16]}...")

    # Ensure assets are resolved before establishing network boundaries
    if not _DB_CREDS_READER or not _DB_CREDS:
        load_all_db_credentials()

    # -- Check 1: documents.pdfs + documents.quarantine (reader creds, same connection) ---
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
                cur.execute(
                    "SELECT 1 FROM documents.quarantine WHERE checksum = %s LIMIT 1;",
                    (sha256_hex,)
                )
                if cur.fetchone() is not None:
                    LOGGER.info("  SHA-256 dupe check → SKIP (found in documents.quarantine)")
                    return True
    except Exception as e:
        LOGGER.error(f"  DB SHA-256 check error (reader, treating as novel): {e}")

    # -- Check 2: doc_collection.crawler_hash + crawl_attachments (main write creds) ------
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
                cur.execute(
                    "SELECT 1 FROM doc_collection.crawl_attachments WHERE sha256_hash = %s LIMIT 1;",
                    (sha256_hex,)
                )
                if cur.fetchone() is not None:
                    LOGGER.info("  SHA-256 dupe check → DUPE (found in doc_collection.crawl_attachments)")
                    return True
    except Exception as e:
        LOGGER.error(f"  DB SHA-256 check error (crawler_hash, treating as novel): {e}")

    LOGGER.info("  SHA-256 dupe check → novel")
    return False


def db_check_minhash_dupe(
    new_mh: Any,
    threshold: float = MINHASH_SIM_THRESHOLD,
    sha256_hex: str = "",
) -> Tuple[bool, Optional[str]]:
    """
    LSH-accelerated near-duplicate check via minhash_lsh_index + crawl_attachments.
    Falls back to full scan of crawler_hash for Simbli rows until Phase 7 migration.
    Returns (is_dupe, matching_pdf_url).
    """
    new_mh_array = new_mh.hashvalues.tolist()
    band_hashes  = _compute_lsh_bands(new_mh_array)
    band_numbers = list(range(16))

    lsh_sql = """
        WITH input_bands AS (
            SELECT * FROM UNNEST(%(band_numbers)s::smallint[], %(band_hashes)s::text[])
                       AS t(band_number, band_hash)
        ),
        candidates AS (
            SELECT DISTINCT lsh.source_id
            FROM doc_collection.minhash_lsh_index lsh
            JOIN input_bands ib
              ON lsh.band_number = ib.band_number
             AND lsh.band_hash   = ib.band_hash
            WHERE lsh.source_table = 'crawl_attachments'
        )
        SELECT ca.pdf_url, jaccard_similarity(ca.minhash, %(minhash)s::bigint[]) AS sim
        FROM doc_collection.crawl_attachments ca
        JOIN candidates c ON ca.attachment_id = c.source_id
        WHERE ca.minhash IS NOT NULL
          AND jaccard_similarity(ca.minhash, %(minhash)s::bigint[]) >= %(threshold)s
        ORDER BY sim DESC
        LIMIT 1;
    """

    LOGGER.debug("  MinHash dupe check: LSH candidate lookup...")
    match_url = None
    similarity = None

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(lsh_sql, {
                    "band_numbers": band_numbers,
                    "band_hashes":  band_hashes,
                    "minhash":      new_mh_array,
                    "threshold":    threshold,
                })
                row = cur.fetchone()
        if row:
            match_url, similarity = row
    except Exception as e:
        LOGGER.warning(f"  LSH minhash check error — falling back to full scan: {e}")

    # Fallback: full scan of crawler_hash (covers Simbli rows until Phase 7 migration)
    if match_url is None:
        fallback_sql = """
            SELECT pdf_link, jaccard_similarity(min_hash, %s) AS sim
            FROM doc_collection.crawler_hash
            WHERE min_hash IS NOT NULL
              AND jaccard_similarity(min_hash, %s) >= %s
            ORDER BY sim DESC
            LIMIT 1;
        """
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(fallback_sql, (new_mh_array, new_mh_array, threshold))
                    row = cur.fetchone()
            if row:
                match_url, similarity = row
        except Exception as e:
            LOGGER.error(f"  Fallback MinHash check error (treating as novel): {e}")

    if match_url:
        # Check duplicate_whitelist before skipping
        if sha256_hex:
            try:
                with get_db_connection(_DB_CREDS_READER) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """SELECT 1 FROM documents.duplicate_whitelist
                               WHERE whitelist_type = 'checksum'
                                 AND whitelist_value = %s LIMIT 1;""",
                            (sha256_hex,)
                        )
                        if cur.fetchone() is not None:
                            LOGGER.info("  MinHash match OVERRIDDEN by duplicate_whitelist — treating as novel.")
                            return False, None
            except Exception as e:
                LOGGER.warning(f"  Whitelist check failed (treating as dupe): {e}")

        LOGGER.info(
            f"  MinHash dupe found: similarity={similarity:.4f} ≥ {threshold}  "
            f"matching_url={match_url}"
        )
        return True, match_url

    LOGGER.info("  MinHash dupe check → novel (no match above threshold).")
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
    """Fetches active prompts (is_active = TRUE) into a DataFrame indexed by prompt_name."""
    LOGGER.info("Fetching active prompts into a DataFrame...")
    try:
        with get_db_connection() as conn:
            query = """
                SELECT prompt_id, prompt_name, version, prompt_text
                FROM doc_collection.prompts
                WHERE is_active = TRUE
            """
            df = pd.read_sql(query, conn)
            df.set_index('prompt_name', inplace=True)
            LOGGER.info(f"✅ Cached {len(df)} active prompts.")
            return df
    except Exception as e:
        LOGGER.error(f"❌ Failed to fetch prompts table: {e}")
        return pd.DataFrame()


def get_prompt_info(df: pd.DataFrame, prompt_name: str) -> Tuple[str, Optional[int]]:
    """Retrieves localized active execution text and DB prompt references using dataframe caching."""
    try:
        if prompt_name in df.index:
            row = df.loc[prompt_name]
            return row['prompt_text'], int(row['prompt_id'])
        LOGGER.warning(f"⚠️ Prompt '{prompt_name}' not found. Available: {df.index.tolist()}")
        return "", None
    except Exception as e:
        LOGGER.error(f"❌ Error retrieving prompt '{prompt_name}': {e}")
        return "", None


# ═════════════════════════════════════════════════════════════════════════════
# TELEMETRY LOGGER INTERFACE FUNCTIONS (INSERT / UPDATE ONSHORE WRITES)
# ═════════════════════════════════════════════════════════════════════════════

def log_meeting_to_db(
    nces_id: str,
    meeting_date: str,
    meeting_link: str,
    meeting_type: str,
    platform: str = "UNKNOWN",
    district_config_id: Optional[int] = None,
) -> Optional[int]:
    """INSERT a tracking transaction row into doc_collection.meetings table."""
    sql = """
        INSERT INTO doc_collection.meetings
            (nces_id, meeting_date, meeting_link, meeting_type,
             platform, district_config_id, created_at)
        VALUES
            (%(nces_id)s, %(meeting_date)s, %(meeting_link)s, %(meeting_type)s,
             %(platform)s, %(district_config_id)s, %(created_at)s)
        ON CONFLICT DO NOTHING
        RETURNING meeting_id;
    """
    try:
        parsed_date = datetime.strptime(meeting_date, "%m-%d-%y").date()
    except ValueError:
        LOGGER.warning(f"log_meeting_to_db: bad date string '{meeting_date}' — skipping.")
        return None

    params = {
        "nces_id":            str(nces_id),
        "meeting_date":       parsed_date,
        "meeting_link":       meeting_link,
        "meeting_type":       meeting_type,
        "platform":           platform,
        "district_config_id": district_config_id,
        "created_at":         datetime.utcnow(),
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
    response_json: Optional[Any] = None,
) -> Optional[int]:
    """Calculates granular run costs and inputs execution states into doc_collection.ai_calls."""
    input_tokens  = tokens.get("input_tokens")  or 0
    output_tokens = tokens.get("output_tokens") or 0

    input_rate_per_1k, output_rate_per_1k = get_model_rate(model_name)
    cost_usd = (input_tokens / 1000.0 * input_rate_per_1k) + (output_tokens / 1000.0 * output_rate_per_1k)

    # Convert response_json to JSONB-compatible form
    if isinstance(response_json, dict):
        response_json_db = psycopg2.extras.Json(response_json)
    elif isinstance(response_json, str):
        try:
            response_json_db = psycopg2.extras.Json(json.loads(response_json))
        except Exception:
            response_json_db = psycopg2.extras.Json({"raw": response_json})
    else:
        response_json_db = None

    sql = """
        INSERT INTO doc_collection.ai_calls
            (prompt_id, meeting_id, attachment_id, document_id, nces_id,
             model_name, prompt_snapshot, response_json,
             input_tokens, output_tokens, cost_usd, status, created_at)
        VALUES
            (%(p_id)s, %(m_id)s, %(att_id)s, %(d_id)s, %(nces)s,
             %(model)s, %(prompt_snapshot)s, %(response_json)s,
             %(in_t)s, %(out_t)s, %(cost)s, %(status)s, %(ts)s)
        RETURNING ai_call_id;
    """

    params = {
        "p_id":            prompt_id,
        "m_id":            meeting_id,
        "att_id":          attachment_id,
        "d_id":            document_id,
        "nces":            str(nces_id),
        "model":           model_name,
        "prompt_snapshot": prompt_snapshot,
        "response_json":   response_json_db,
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
                row = cur.fetchone()
            conn.commit()
        ai_call_id = row[0] if row else None
        LOGGER.debug(
            f"  💰 AI cost logged: ${cost_usd:.6f} "
            f"({input_tokens + output_tokens} tokens, status={status}, ai_call_id={ai_call_id})"
        )
        return ai_call_id
    except Exception as e:
        LOGGER.error(f"  ❌ Failed to log AI usage: {e}")
        return None


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
             google_drive_url, sha256_hash, minhash, category, doc_type,
             fiscal_year, page_count, created_at)
        VALUES
            (%(attachment_id)s, %(meeting_id)s, %(nces_id)s, %(file_name)s,
             %(drive_folder)s, %(google_drive_url)s, %(sha256_hash)s,
             %(minhash_array)s, %(category)s, %(doc_type)s,
             %(fiscal_year)s, %(page_count)s, %(created_at)s)
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
        "minhash_array":    minhash_array,
        "category":         category,
        "doc_type":         doc_type,
        "fiscal_year":      fiscal_year,
        "page_count":       page_count,
        "created_at":       datetime.utcnow(),
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
    # Pass as a Python list — psycopg2 converts list→PostgreSQL BIGINT[] array literal automatically.
    # json.dumps() produces "[1234, ...]" (JSON) which PostgreSQL rejects as malformed array literal.
    minhash_json = minhash_obj.hashvalues.tolist() if minhash_obj is not None else None
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


def log_to_overall_sbd_log(
    nces_id: str,
    meeting_records: List[Any],
    notes: str = "",
    crawler_type: str = "SIMBLI_MINUTES",
    batch_run_id: Optional[int] = None,
) -> None:
    """Upserts daily analytics records into doc_collection.crawler_run_log."""
    num_new_meetings    = len(meeting_records)
    num_docs_downloaded = sum(r.downloaded for r in meeting_records)

    max_meeting_date = datetime.utcnow().date()
    if meeting_records:
        parsed_dates = []
        for rec in meeting_records:
            try:
                parsed_dates.append(datetime.strptime(rec.date, "%m-%d-%y").date())
            except ValueError:
                LOGGER.warning(f"  crawler_run_log: could not parse date '{rec.date}'")
        if parsed_dates:
            max_meeting_date = max(parsed_dates)

    crawler_run_date = datetime.utcnow()

    sql = """
        INSERT INTO doc_collection.crawler_run_log
            (nces_id, created_at, num_new_meetings, max_meeting_date,
             num_docs_downloaded, notes, crawler_type, batch_run_id)
        VALUES
            (%(nces_id)s, %(created_at)s, %(num_new_meetings)s,
             %(max_meeting_date)s, %(num_docs_downloaded)s, %(notes)s,
             %(crawler_type)s, %(batch_run_id)s)
        ON CONFLICT (nces_id, ((created_at AT TIME ZONE 'UTC')::date), crawler_type) DO UPDATE SET
            num_new_meetings    = EXCLUDED.num_new_meetings,
            max_meeting_date    = EXCLUDED.max_meeting_date,
            num_docs_downloaded = EXCLUDED.num_docs_downloaded,
            notes               = EXCLUDED.notes,
            batch_run_id        = EXCLUDED.batch_run_id;
    """

    params = {
        "nces_id":             str(nces_id),
        "created_at":          crawler_run_date,
        "num_new_meetings":    num_new_meetings,
        "max_meeting_date":    max_meeting_date,
        "num_docs_downloaded": num_docs_downloaded,
        "notes":               notes,
        "crawler_type":        crawler_type,
        "batch_run_id":        batch_run_id,
    }

    LOGGER.info(
        f"  Logging crawler_run_log: nces={nces_id}  "
        f"run_date={crawler_run_date}  new_meetings={num_new_meetings}  "
        f"docs_downloaded={num_docs_downloaded}"
    )

    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        LOGGER.info("  ✅ crawler_run_log row upserted.")
    except Exception as e:
        LOGGER.error(f"  ❌ Failed to upsert crawler_run_log row: {e}")
        LOGGER.debug(traceback.format_exc())


def log_contact_to_db(nces_id: str, name: str, email: str, phone: str, title: str) -> None:
    """Upserts extracted regional administrative contacts metadata directly into storage rosters."""
    if not name:
        return
    sql = """
        INSERT INTO doc_collection.contacts (nces_id, name, email, phone, title, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (nces_id, name, title) DO UPDATE SET
            updated_at = NOW(),
            email      = EXCLUDED.email,
            phone      = EXCLUDED.phone;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (nces_id, name, email or "", phone or "", title or ""))
            conn.commit()
        LOGGER.info(f"DB upsert OK → name='{name}' title='{title}'")
    except psycopg2.Error as e:
        LOGGER.error(f"DB upsert failed for '{name}': {e}")


def db_check_attachment_dupe(sha256_hex: str, pdf_url: str) -> Tuple[bool, Optional[int]]:
    """
    Check doc_collection.attachments for an existing row matching either the
    SHA-256 hash or the exact attachment URL.  Returns (is_dupe, existing_id).
    """
    sql = """
        SELECT id FROM doc_collection.attachments
        WHERE hash = %(hash)s
           OR attachment_link = %(url)s
        LIMIT 1;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, {"hash": sha256_hex, "url": pdf_url})
                row = cur.fetchone()
        if row:
            LOGGER.info(f"  db_check_attachment_dupe → DUPE (attachments.id={row[0]})")
            return True, row[0]
        LOGGER.debug("  db_check_attachment_dupe → novel")
        return False, None
    except Exception as e:
        LOGGER.error(f"  db_check_attachment_dupe failed (treating as novel): {e}")
        return False, None


def log_attachment_to_db(
    meeting_id: Optional[int],
    agenda_url: str,
    attachment_title: str,
    attachment_link: str,
    sha256_hex: str = "",
    minhash_obj: Optional[Any] = None,
) -> Optional[int]:
    """INSERT one row into doc_collection.attachments."""
    # attachments.minhash is jsonb — must use psycopg2.extras.Json(), not a raw list.
    # (crawler_hash.min_hash is BIGINT[] and takes a raw list; these are different columns.)
    minhash_json = psycopg2.extras.Json(minhash_obj.hashvalues.tolist()) if minhash_obj is not None else None

    sql = """
        INSERT INTO doc_collection.attachments
            (meeting_id, agenda_url, attachment_title, attachment_link,
             hash, minhash, date_added)
        VALUES
            (%(meeting_id)s, %(agenda_url)s, %(attachment_title)s,
             %(attachment_link)s, %(hash)s, %(minhash)s, %(date_added)s)
        RETURNING id;
    """
    params = {
        "meeting_id":       meeting_id,
        "agenda_url":       agenda_url,
        "attachment_title": attachment_title,
        "attachment_link":  attachment_link,
        "hash":             sha256_hex or "",
        "minhash":          minhash_json,
        "date_added":       datetime.utcnow(),
    }
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
        attachment_id = row[0] if row else None
        LOGGER.info(f"  ✅ attachments row inserted — id={attachment_id}")
        return attachment_id
    except Exception as e:
        LOGGER.error(f"  ❌ log_attachment_to_db failed: {e}")
        LOGGER.debug(traceback.format_exc())
        return None


def log_cover_page_to_db(
    nces_id: str,
    meeting_id: Optional[int],
    agenda_item_text: str,
    drive_link: str = "",
    sha256_hex: str = "",
    minhash_obj: Optional[Any] = None,
) -> Optional[int]:
    """INSERT one row into doc_collection.cover_pages."""
    minhash_array = minhash_obj.hashvalues.tolist() if minhash_obj is not None else None

    sql = """
        INSERT INTO doc_collection.cover_pages
            (nces_id, meeting_id, agenda_item_text, drive_link,
             hash, minhash, created_at)
        VALUES
            (%(nces_id)s, %(meeting_id)s, %(agenda_item_text)s, %(drive_link)s,
             %(hash)s, %(minhash)s, %(created_at)s)
        RETURNING id;
    """
    params = {
        "nces_id":          str(nces_id),
        "meeting_id":       meeting_id,
        "agenda_item_text": agenda_item_text,
        "drive_link":       drive_link or "",
        "hash":             sha256_hex or "",
        "minhash":          minhash_array,
        "created_at":       datetime.utcnow(),
    }
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
        cover_id = row[0] if row else None
        LOGGER.info(f"  ✅ cover_pages row inserted — id={cover_id}")
        return cover_id
    except Exception as e:
        LOGGER.error(f"  ❌ log_cover_page_to_db failed: {e}")
        LOGGER.debug(traceback.format_exc())
        return None


def log_document_response_to_db(
    document_id: Optional[int],
    ai_call_id: Optional[int],
    completion_text: str,
    extracted_data: dict,
) -> Optional[int]:
    """INSERT one row into doc_collection.document_responses."""
    if document_id is None:
        LOGGER.warning("  log_document_response_to_db: document_id is None — skipping.")
        return None
    sql = """
        INSERT INTO doc_collection.document_responses
            (document_id, ai_call_id, completion_text, extracted_data, created_at)
        VALUES
            (%(document_id)s, %(ai_call_id)s, %(completion_text)s,
             %(extracted_data)s, %(created_at)s)
        RETURNING id;
    """
    params = {
        "document_id":     document_id,
        "ai_call_id":      ai_call_id,
        "completion_text": completion_text,
        "extracted_data":  psycopg2.extras.Json(extracted_data) if extracted_data is not None else None,
        "created_at":      datetime.utcnow(),
    }
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
            conn.commit()
        resp_id = row[0] if row else None
        LOGGER.debug(f"  ✅ document_responses row inserted — id={resp_id}")
        return resp_id
    except Exception as e:
        LOGGER.error(f"  ❌ log_document_response_to_db failed: {e}")
        LOGGER.debug(traceback.format_exc())
        return None


def log_error_to_db(
    error_type: str,
    message: str,
    stack_trace: str = "",
    nces_id: Optional[str] = None,
    meeting_id: Optional[int] = None,
    document_id: Optional[int] = None,
    platform: Optional[str] = None,
) -> None:
    """INSERT one row into doc_collection.error_logs. Never raises."""
    sql = """
        INSERT INTO doc_collection.error_logs
            (nces_id, meeting_id, document_id, error_type,
             message, stack_trace, platform, is_resolved, created_at)
        VALUES
            (%(nces_id)s, %(meeting_id)s, %(document_id)s, %(error_type)s,
             %(message)s, %(stack_trace)s, %(platform)s, FALSE, %(created_at)s);
    """
    params = {
        "nces_id":     str(nces_id) if nces_id is not None else None,
        "meeting_id":  meeting_id,
        "document_id": document_id,
        "error_type":  error_type[:100],
        "message":     message[:2000],
        "stack_trace": stack_trace[:5000],
        "platform":    platform,
        "created_at":  datetime.utcnow(),
    }
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()
        LOGGER.debug(f"  🪵 error_logs row inserted: {error_type}")
    except Exception as e:
        LOGGER.critical(f"  ❌ log_error_to_db itself failed: {e}")


def log_crawl_attachment(
    meeting_id: Optional[int],
    nces_id: str,
    pdf_url: str,
    attachment_name: Optional[str],
    outcome: str,
    sha256_hash: Optional[str] = None,
    minhash_json: Optional[Any] = None,
    is_duplicate: bool = False,
    dupe_type: Optional[str] = None,
    dupe_similarity: Optional[float] = None,
) -> int:
    """
    Logs attachment run traces. Uses ON CONFLICT ON CONSTRAINT to gracefully
    handle multi-worker collisions without raising application exceptions.
    """
    if minhash_json and isinstance(minhash_json, str) and minhash_json.startswith('['):
        try:
            minhash_data = json.loads(minhash_json)
        except Exception:
            minhash_data = None
    else:
        minhash_data = minhash_json

    query = """
        INSERT INTO doc_collection.crawl_attachments (
            meeting_id, nces_id, pdf_url, attachment_name, outcome,
            sha256_hash, minhash, is_duplicate, dupe_type, dupe_similarity, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        ON CONFLICT ON CONSTRAINT unique_meeting_attachment
        DO UPDATE SET
            outcome         = EXCLUDED.outcome,
            sha256_hash     = COALESCE(EXCLUDED.sha256_hash, doc_collection.crawl_attachments.sha256_hash),
            minhash         = COALESCE(EXCLUDED.minhash, doc_collection.crawl_attachments.minhash),
            is_duplicate    = EXCLUDED.is_duplicate,
            dupe_type       = EXCLUDED.dupe_type,
            dupe_similarity = EXCLUDED.dupe_similarity,
            created_at      = NOW()
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
                    minhash_data,
                    is_duplicate,
                    dupe_type,
                    dupe_similarity,
                ))
                res = cur.fetchone()
                attachment_id = res[0] if res else 0
                conn.commit()
                LOGGER.debug(f"Logged attachment ID {attachment_id} to crawl_attachments. (Outcome: {outcome})")
                return attachment_id
    except Exception as e:
        LOGGER.error(f"Failed to write crawl attachment log: {e}")
        raise


# ═════════════════════════════════════════════════════════════════════════════
# DISTRICT CONFIG & BATCH RUN MANAGEMENT
# ═════════════════════════════════════════════════════════════════════════════

def get_core_district_id(nces_id: str) -> Optional[int]:
    """Returns core.districts.id for the given NCES ID, or None if not found."""
    sql = "SELECT id FROM core.districts WHERE nces_id = %s LIMIT 1;"
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (int(nces_id),))
                row = cur.fetchone()
        return row[0] if row else None
    except Exception as e:
        LOGGER.warning(f"  get_core_district_id({nces_id}): {e}")
        return None


def upsert_district_config(
    nces_id: str,
    district_name: str,
    platform: str,
    crawler_type: str,
    link: str,
    check_date: str,
) -> Optional[int]:
    """Upserts district_configs via core.districts FK; returns district_config_id."""
    core_districts_id = get_core_district_id(nces_id)
    if core_districts_id is None:
        LOGGER.warning(f"  upsert_district_config: nces={nces_id} not found in core.districts — skipping")
        return None

    config_sql = """
        INSERT INTO doc_collection.district_configs
            (core_districts_id, nces_id, platform, crawler_type, link, check_date)
        VALUES
            (%(core_districts_id)s, %(nces_id)s, %(platform)s, %(crawler_type)s, %(link)s, %(check_date)s)
        ON CONFLICT (core_districts_id, platform, crawler_type) DO UPDATE SET
            link       = EXCLUDED.link,
            check_date = EXCLUDED.check_date,
            updated_at = NOW()
        RETURNING district_config_id;
    """
    try:
        parsed_date = None
        if check_date and check_date.lower() not in ("nan", ""):
            try:
                parsed_date = datetime.strptime(check_date, "%m-%d-%y").date()
            except ValueError:
                pass

        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(config_sql, {
                    "core_districts_id": core_districts_id,
                    "nces_id":           int(nces_id),
                    "platform":          platform,
                    "crawler_type":      crawler_type,
                    "link":              link,
                    "check_date":        parsed_date,
                })
                row = cur.fetchone()
            conn.commit()
        config_id = row[0] if row else None
        LOGGER.debug(f"  upsert_district_config: nces={nces_id} platform={platform} → config_id={config_id}")
        return config_id
    except Exception as e:
        LOGGER.error(f"  upsert_district_config failed for nces={nces_id}: {e}")
        return None


def update_district_last_crawl(district_config_id: int) -> None:
    """Sets last_crawl = NOW() on district_configs after a successful district run."""
    if not district_config_id:
        return
    sql = """
        UPDATE doc_collection.district_configs
        SET last_crawl = NOW(), updated_at = NOW()
        WHERE district_config_id = %s;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (district_config_id,))
            conn.commit()
        LOGGER.debug(f"  update_district_last_crawl: config_id={district_config_id}")
    except Exception as e:
        LOGGER.error(f"  update_district_last_crawl failed: {e}")


def insert_batch_run(platform: str, crawler_type: str, worker_processes: int) -> Optional[int]:
    """Creates a batch_runs row at crawler start; returns batch_run_id."""
    sql = """
        INSERT INTO doc_collection.batch_runs
            (platform, crawler_type, worker_processes, started_at, created_at)
        VALUES (%s, %s, %s, NOW(), NOW())
        RETURNING batch_run_id;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (platform, crawler_type, worker_processes))
                row = cur.fetchone()
            conn.commit()
        batch_run_id = row[0] if row else None
        LOGGER.info(f"  insert_batch_run: {platform}/{crawler_type} → batch_run_id={batch_run_id}")
        return batch_run_id
    except Exception as e:
        LOGGER.error(f"  insert_batch_run failed: {e}")
        return None


def close_batch_run(batch_run_id: Optional[int], succeeded: int, errored: int) -> None:
    """Closes a batch_runs row with final district counts."""
    if not batch_run_id:
        return
    sql = """
        UPDATE doc_collection.batch_runs
        SET completed_at        = NOW(),
            districts_succeeded = %s,
            districts_errored   = %s,
            updated_at          = NOW()
        WHERE batch_run_id = %s;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (succeeded, errored, batch_run_id))
            conn.commit()
        LOGGER.info(f"  close_batch_run: batch_run_id={batch_run_id} succeeded={succeeded} errored={errored}")
    except Exception as e:
        LOGGER.error(f"  close_batch_run failed: {e}")


def get_drive_folder_map() -> dict:
    """Returns {folder_name: folder_id} from doc_collection.drive_folders (cached)."""
    global _DRIVE_FOLDER_MAP
    if _DRIVE_FOLDER_MAP is not None:
        return _DRIVE_FOLDER_MAP
    sql = """
        SELECT drive_folder_name, drive_folder_id
        FROM doc_collection.drive_folders
        WHERE is_active = TRUE;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall()
        _DRIVE_FOLDER_MAP = {name: fid for name, fid in rows}
        LOGGER.info(f"  get_drive_folder_map: loaded {len(_DRIVE_FOLDER_MAP)} active folders.")
        return _DRIVE_FOLDER_MAP
    except Exception as e:
        LOGGER.error(f"  get_drive_folder_map failed: {e}")
        return {}


def get_model_rate(model_name: str) -> Tuple[float, float]:
    """Returns (input_usd_per_1k, output_usd_per_1k) from model_pricing (cached)."""
    if model_name in _MODEL_RATES:
        return _MODEL_RATES[model_name]
    sql = """
        SELECT input_usd_per_1k, output_usd_per_1k
        FROM doc_collection.model_pricing
        WHERE model_name = %s AND effective_until IS NULL
        LIMIT 1;
    """
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (model_name,))
                row = cur.fetchone()
        if row:
            rates = (float(row[0]), float(row[1]))
            _MODEL_RATES[model_name] = rates
            return rates
    except Exception as e:
        LOGGER.warning(f"  get_model_rate({model_name}) failed ({e}) — using defaults.")
    # Fallback defaults (gemini-2.5-flash-lite pricing)
    return (0.000075, 0.0003)