# District Platform Crawlers

Headless Selenium crawlers for scraping school district board meeting documents from multiple platforms. Documents are deduplicated, classified via Gemini, and uploaded to Google Drive with metadata logged to PostgreSQL.

## Supported Platforms

| Platform | Crawler | Parallelism |
|---|---|---|
| BoardBook Premier | `crawlers/boardbook/` | Parallel (`ProcessPoolExecutor`) |
| Diligent BoardDocs | `crawlers/diligent/` | Parallel |
| BoardDocs | `crawlers/boarddocs/` | Parallel |
| Simbli eBoard | `crawlers/simbli/` | Parallel |

## VM / GitHub Workflow

The VM is connected to the `saguiar-burbio/BoardDoc-Scrapers` repository. Always run Git commands from inside the project folder:

```bash
cd BoardDoc-Scrapers
```

**Pull latest changes from GitHub to the VM** (most common — after pushing from your local machine):
```bash
git pull
```

**Push changes made on the VM back to GitHub:**
```bash
git add .
git commit -m "Updated scrapers on VM"
git push origin main
```

---

## Setup

**Prerequisites (Ubuntu Server):**
```bash
sudo apt-get install -y xvfb chromium-browser chromium-chromedriver
```

**Python environment:**
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**`.env` file (repo root):**
```
GEMINI_KEY=<your-gemini-api-key>
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

All commands must be run from the repo root so that `config`, `core`, and `crawlers` are on the Python path.

## Running

**BoardBook** (parallel, requires Xvfb):
```bash
Xvfb :99 -screen 0 1920x1080x24 &
export DISPLAY=:99
python -m crawlers.boardbook.main
```

**Diligent:**
```bash
export DISPLAY=:99
python -m crawlers.diligent.main
```

**BoardDocs:**
```bash
export DISPLAY=:99
python -m crawlers.boarddocs.main
```

**Simbli:**
```bash
export DISPLAY=:99
python -m crawlers.simbli.main
```

## Architecture

```
district_platform_crawlers/
├── config/
│   └── settings.py          # GCS paths, DB secret names, ML params
│                            # Drive folder IDs are in doc_collection.drive_folders (DB)
├── core/                    # Shared infrastructure — imported by every crawler
│   ├── analyzer.py          # Gemini API calls; PDF → JSON classification
│   ├── database.py          # All PostgreSQL reads/writes; GCP Secret Manager bootstrap
│   ├── document_utils.py    # Cover page, PDF merge, OCR, AI routing pipeline
│   ├── driver.py            # Chrome WebDriver factory + browser helpers
│   ├── gcs.py               # GCS download/upload helpers
│   ├── google_auth.py       # OAuth2 for Drive
│   ├── google_functions.py  # Drive upload/download helpers
│   ├── hashing.py           # SHA-256 exact-dupe + MinHash (128 perms, k=5) soft-dupe
│   ├── humanize.py          # Anti-detection mouse/timing simulation
│   ├── models.py            # AttachmentRecord, MeetingRecord dataclasses
│   ├── pdf_functions.py     # PyMuPDF/OCR text extraction, date parsing
│   └── utils.py             # setup_logger(), debug helpers
└── crawlers/
    ├── boardbook/
    │   ├── main.py          # ProcessPoolExecutor batch runner
    │   └── scraper.py       # BoardBook DOM navigation, WebViewer download
    ├── boarddocs/
    │   ├── main.py          # Parallel batch runner
    │   └── scraper.py       # BoardDocs JSON-LD meeting discovery, agenda traversal
    ├── diligent/
    │   ├── main.py          # Parallel batch runner
    │   └── scraper.py       # Diligent iframe traversal, CDP print-to-PDF
    └── simbli/
        ├── main.py          # Parallel batch runner; Chrome session resurrection
        └── scraper.py       # Simbli print-dialog cover page, 5-tier attachment download
```

### Data Flow (per district)

1. **Bootstrap** — GCS downloads district CSV, credentials, and OAuth token to `/tmp`
2. **District config upsert** — `upsert_district_config()` writes one row to `district_configs` per (district, platform, crawler_type); returns `district_config_id` used to link all downstream records
3. **Batch run** — `insert_batch_run()` opens a `batch_runs` row for the full crawl invocation
4. **Scrape** — `undetected_chromedriver` navigates the platform's meeting list; meetings newer than `check_date` are processed
5. **SHA-256 dupe check** — exact match against `documents.pdfs`, `documents.quarantine` (reader creds), then `doc_collection.crawler_hash`; skips if any match
6. **Cover page + merge** — cover page captured per agenda item; PyMuPDF merges with downloaded attachments
7. **OCR + MinHash soft-dupe** — text extracted via PyMuPDF (OCR fallback); LSH band index narrows candidates before Jaccard similarity check; ≥95% match skips the document (bypassed if SHA-256 is in `documents.duplicate_whitelist`)
8. **Gemini classification** — merged PDF sent to `gemini-2.5-flash-lite`; structured JSON response routes the doc to the correct Drive folder (folder IDs read from `doc_collection.drive_folders`)
9. **DB logging** — meetings, uploaded docs, AI calls, cover pages, crawl attachments, and errors written to `doc_collection`; `district_configs.last_crawl` stamped on success
10. **Batch close** — `close_batch_run()` records `completed_at`, `districts_succeeded`, `districts_errored`

### Key Configuration Knobs

| Setting | Location | Purpose |
|---|---|---|
| `WORKER_PROCESSES` | top of each `main.py` | Parallel Chrome processes |
| `MODEL_NAME` | `config/settings.py` | Gemini model for classification |
| `MINHASH_SIM_THRESHOLD` | `config/settings.py` | Soft-dupe rejection ceiling (default 0.95) |
| `check_date` | District CSV column `"Check Date"` | Per-district cutoff; older meetings are skipped |
| Drive folder IDs | `doc_collection.drive_folders` table | Updated in DB, no deploy needed |
| AI model pricing | `doc_collection.model_pricing` table | Updated in DB, no deploy needed |
| Active prompts | `doc_collection.prompts.is_active` flag | Set `is_active = TRUE` on the target row |

### Database Schemas

Credentials are bootstrapped via GCP Secret Manager (`doc_collection_schema_manager` for writes, `DOCUMENTS_READER_POSTGRES` for read-only cross-schema checks).

#### `doc_collection` (write)

```
districts
  nces_id        TEXT  PK
  district_name  TEXT
  state          CHAR(2)
  created_at     TIMESTAMPTZ  DEFAULT NOW()
  updated_at     TIMESTAMPTZ  DEFAULT NOW()

district_configs
  district_config_id  SERIAL  PK
  nces_id             TEXT    FK → districts.nces_id
  platform            TEXT    CHECK IN ('BOARDBOOK','DILIGENT','BOARDDOCS','SIMBLI','DISTRICT_WEBSITE')
  crawler_type        TEXT    CHECK IN ('super','icon','legacy_ctrl_f','budget','strategic','spending')
  link                TEXT    NOT NULL
  check_date          DATE
  status              TEXT    DEFAULT 'active'  CHECK IN ('active','inactive')
  last_crawl          TIMESTAMPTZ
  created_at          TIMESTAMPTZ  DEFAULT NOW()
  updated_at          TIMESTAMPTZ  DEFAULT NOW()
  UNIQUE (nces_id, platform, crawler_type)

batch_runs
  batch_run_id         SERIAL  PK
  platform             TEXT    NOT NULL
  crawler_type         TEXT    NOT NULL
  worker_processes     SMALLINT
  total_districts      INT
  districts_succeeded  INT
  districts_errored    INT
  started_at           TIMESTAMPTZ  DEFAULT NOW()
  completed_at         TIMESTAMPTZ
  notes                TEXT
  created_at           TIMESTAMPTZ  DEFAULT NOW()
  updated_at           TIMESTAMPTZ  DEFAULT NOW()

meetings
  meeting_id          SERIAL  PK
  nces_id             TEXT    FK → districts.nces_id
  district_config_id  INT     FK → district_configs.district_config_id
  platform            TEXT    DEFAULT 'UNKNOWN'
  meeting_date        TEXT
  meeting_link        TEXT
  meeting_type        TEXT
  created_at          TIMESTAMPTZ  DEFAULT NOW()
  updated_at          TIMESTAMPTZ  DEFAULT NOW()

crawl_attachments
  attachment_id   SERIAL  PK
  meeting_id      INT     FK → meetings.meeting_id
  nces_id         TEXT
  file_name       TEXT
  sha256_hash     TEXT
  minhash         BIGINT[]
  is_duplicate    BOOLEAN  DEFAULT FALSE
  dupe_type       TEXT     CHECK IN ('hash','minhash')   -- NULL when not a dupe
  dupe_similarity NUMERIC(5,4)                           -- NULL when not a dupe; 1.0 for hash dupes
  created_at      TIMESTAMPTZ  DEFAULT NOW()
  updated_at      TIMESTAMPTZ  DEFAULT NOW()

minhash_lsh_index
  lsh_id        SERIAL  PK
  source_table  TEXT    -- 'crawl_attachments'
  source_id     INT     -- FK → crawl_attachments.attachment_id
  band_number   INT     -- 0–15 (16 bands of 8 ints from 128-perm MinHash)
  band_hash     TEXT    -- MD5 of the band's 8 integers
  created_at    TIMESTAMPTZ  DEFAULT NOW()

uploaded_documents
  document_id       SERIAL  PK
  meeting_id        INT     FK → meetings.meeting_id
  attachment_id     INT     FK → crawl_attachments.attachment_id
  nces_id           TEXT
  file_name         TEXT
  drive_file_id     TEXT
  drive_folder      TEXT
  sha256_hash       TEXT
  minhash           BIGINT[]
  documents_pdfs_id INT     -- backfilled by trigger when documents.pdfs row is inserted
  created_at        TIMESTAMPTZ  DEFAULT NOW()
  updated_at        TIMESTAMPTZ  DEFAULT NOW()

ai_calls
  ai_call_id    SERIAL  PK
  meeting_id    INT     FK → meetings.meeting_id
  attachment_id INT     FK → crawl_attachments.attachment_id
  nces_id       TEXT
  prompt_id     INT     FK → prompts.prompt_id
  model_name    TEXT
  input_tokens  INT
  output_tokens INT
  cost_usd      NUMERIC(12,8)
  response_json JSONB
  created_at    TIMESTAMPTZ  DEFAULT NOW()

document_responses
  response_id    SERIAL  PK
  ai_call_id     INT     FK → ai_calls.ai_call_id
  document_id    INT     FK → uploaded_documents.document_id
  extracted_data JSONB
  created_at     TIMESTAMPTZ  DEFAULT NOW()

cover_pages
  cover_page_id  SERIAL  PK
  meeting_id     INT     FK → meetings.meeting_id
  nces_id        TEXT
  file_name      TEXT
  minhash        BIGINT[]
  created_at     TIMESTAMPTZ  DEFAULT NOW()
  updated_at     TIMESTAMPTZ  DEFAULT NOW()

error_logs
  error_id     SERIAL  PK
  meeting_id   INT     FK → meetings.meeting_id  (nullable)
  nces_id      TEXT
  platform     TEXT
  error_type   TEXT
  message      TEXT
  stack_trace  TEXT
  is_resolved  BOOLEAN  DEFAULT FALSE
  resolved_at  TIMESTAMPTZ
  resolved_by  TEXT
  created_at   TIMESTAMPTZ  DEFAULT NOW()

contacts
  contact_id  SERIAL  PK
  nces_id     TEXT
  name        TEXT
  title       TEXT
  created_at  TIMESTAMPTZ  DEFAULT NOW()
  updated_at  TIMESTAMPTZ  DEFAULT NOW()
  UNIQUE (nces_id, name, title)

prompts
  prompt_id    SERIAL  PK
  prompt_name  TEXT
  version      INT
  prompt_text  TEXT
  is_active    BOOLEAN  DEFAULT FALSE
  created_at   TIMESTAMPTZ  DEFAULT NOW()
  updated_at   TIMESTAMPTZ  DEFAULT NOW()
  UNIQUE (prompt_name) WHERE is_active = TRUE   -- partial index; one active row per name

drive_folders
  drive_folder_name  TEXT  PK   -- 'MINUTES', 'BUDGET', 'STRATEGIC_PLANNING', etc.
  drive_folder_id    TEXT  NOT NULL
  description        TEXT
  is_active          BOOLEAN  DEFAULT TRUE
  created_at         TIMESTAMPTZ  DEFAULT NOW()
  updated_at         TIMESTAMPTZ  DEFAULT NOW()

model_pricing
  model_name          TEXT  PK
  input_usd_per_1k    NUMERIC(12,8)  NOT NULL
  output_usd_per_1k   NUMERIC(12,8)  NOT NULL
  effective_from      DATE  DEFAULT CURRENT_DATE
  effective_until     DATE  -- NULL means currently active
  created_at          TIMESTAMPTZ  DEFAULT NOW()
  updated_at          TIMESTAMPTZ  DEFAULT NOW()

crawler_run_log                        -- formerly overall_sbd_log
  crawler_run_id  SERIAL  PK
  batch_run_id    INT     FK → batch_runs.batch_run_id  (nullable)
  nces_id         TEXT
  crawler_type    TEXT
  docs_found      INT
  docs_uploaded   INT
  docs_duped      INT
  notes           TEXT
  created_at      TIMESTAMPTZ  DEFAULT NOW()
  updated_at      TIMESTAMPTZ  DEFAULT NOW()
  UNIQUE (nces_id, DATE(created_at), crawler_type)
```

#### `documents` (read-only)

```
pdfs
  id        SERIAL  PK
  checksum  TEXT    -- SHA-256; checked in db_check_sha256_dupe

quarantine
  id        SERIAL  PK
  checksum  TEXT    -- SHA-256; checked in db_check_sha256_dupe

duplicate_whitelist
  id              SERIAL  PK
  whitelist_type  TEXT    -- 'checksum' triggers MinHash bypass in db_check_minhash_dupe
  whitelist_value TEXT
  folder_name     TEXT
```

## Adding a New Crawler

Create `crawlers/<platform>/` with:
- `__init__.py`
- `main.py` — copy structure from an existing main; update the CSV path constant, platform string, and `crawler_type` passed to `insert_batch_run` and `upsert_district_config`
- `scraper.py` — implement `search_and_download_agenda_attachments()` with the same signature; import shared logic from `core/`

No changes to `core/` or `config/` are needed unless the new platform requires new constants.

## Troubleshooting

- **`PublicMeetingsTable not found`** — BoardBook rate-limited the IP or changed layout. Check `/tmp/screenshot_*.png`.
- **`Could not load DB credentials`** — GCP service account lacks Secret Manager access. Verify: `gcloud secrets versions describe latest --secret="doc_collection_schema_manager"`
- **PDF merge errors** — Corrupted or zero-byte download in `/tmp/*.pdf`. Check with `ls -lh /tmp/*.pdf`.
- **Simbli Chrome session died** — `InvalidSessionIdException` is caught automatically; the driver restarts and retries the same meeting row.
- **`get_drive_folder_map` returns empty dict** — `doc_collection.drive_folders` table is empty or `is_active = FALSE` on all rows. Seed the table per `TODO.md`.
- **`get_all_prompts_df` returns empty DataFrame** — No prompt rows have `is_active = TRUE`. Run `UPDATE doc_collection.prompts SET is_active = TRUE WHERE ...` for the desired versions.
- **`invalid input value for enum doc_collection.dupe_source: "crawl_attachments"`** — The `dupe_source` enum is missing the `'crawl_attachments'` value. Run: `ALTER TYPE doc_collection.dupe_source ADD VALUE 'crawl_attachments';`
