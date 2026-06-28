# ─────────────────────────────────────────────────────────────────────────────
# config/settings.py
# ─────────────────────────────────────────────────────────────────────────────

import os
import re
import collections
from google import genai
from dotenv import load_dotenv

# Load local environment variables if available (.env file)
load_dotenv()

# =====================================================================
# 1. THIRD-PARTY CLIENT INITIALIZATIONS
# =====================================================================
# Google Gemini SDK Client Integration
# Expects GEMINI_KEY to be populated via environment or .env file
GEMINI_KEY = os.getenv("GEMINI_KEY")
client = genai.Client(api_key=GEMINI_KEY)

# =====================================================================
# 2. GOOGLE CLOUD & STORAGE INFRASTRUCTURE
# =====================================================================
# Google Cloud Storage Core Bucket
BUCKET_NAME = "plasma-matter-398213"

# GCS Object Storage Path Ingress/Egress Pointers
CSV_INPUT_PATH_NAME  = "Simbli/Crawlers - BB Part 1.csv"
DILIGENT_CSV_INPUT_PATH_NAME  = "Diligent/Crawlers - Dil 1.csv"
BOARDDOCS_CSV_INPUT_PATH_NAME = "Simbli/Crawlers - BD Part 1.csv"
SIMBLI_CSV_INPUT_PATH_NAME   = "Simbli/Crawlers - Sim Part 1.csv"
CSV_OUTPUT_PATH_NAME = "Simbli/output.csv"
CRED_PATH_NAME       = "JsonKeys/google_sam.json"

# Local Sandbox Asset Delivery Endpoint Name
DOWNLOAD_DIR_NAME = "output"
DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), DOWNLOAD_DIR_NAME)

# =====================================================================
# 3. ARCHIVAL STORAGE - GOOGLE DRIVE TARGET FOLDER INSTANCE IDS
# =====================================================================
# Drive folder IDs are now stored in doc_collection.drive_folders and
# loaded at runtime via core.database.get_drive_folder_map().
# The hardcoded constants have been removed.


# =====================================================================
# 4. DATABASE RESOURCE MANAGER ROUTING RESOURCE KEYS
# =====================================================================
# Main read/write crawler role (GCP Secret Manager API Key Payload Name)
DB_SECRET_RESOURCE        = "projects/1088100045826/secrets/doc_collection_schema_manager"
# Read-only verification analytics profile
DB_SECRET_RESOURCE_READER = "projects/1088100045826/secrets/DOCUMENTS_READER_POSTGRES"

# =====================================================================
# 5. MACHINE LEARNING & ALGORITHMIC PARAMETERS
# =====================================================================
# MinHash LSH Shingle Dimensions and Similarity Rejection Index Constraints
MINHASH_NUM_PERM      = 128
MINHASH_SHINGLE_SIZE  = 5
MINHASH_SIM_THRESHOLD = 0.95   # 95% Jaccard Similarity Match Core Ceiling

# Primary Document Parsing Large Language Model Context Target 
MODEL_NAME = "gemini-2.5-flash-lite"

# =====================================================================
# 6. RUNTIME PIPELINE HYPERPARAMETERS & CACHES
# =====================================================================
# Recycle browser instance interval threshold limits
BATCH_SIZE = 3

# Document classification target keyword regex match logic filters
POLICY_RE = re.compile(r"\bpolic(?:y|ies)\b", re.IGNORECASE)

# Local volatile lookup ledger counter tracking runtime filename namespaces
filename_run_counter = collections.Counter()

# =====================================================================
# 7. BUSINESS PROCESS TELEMETRY STATUS TRANSLATION STRINGS
# =====================================================================
OUTCOME_DOWNLOADED = "✅ Downloaded"
OUTCOME_DUPE       = "🔁 Duplicate"
OUTCOME_DL_FAILED  = "❌ DL Failed"
OUTCOME_NOT_PDF    = "❌ Not a PDF"
OUTCOME_NO_HREF    = "⚠️  No href"
OUTCOME_ERROR      = "❌ Error"