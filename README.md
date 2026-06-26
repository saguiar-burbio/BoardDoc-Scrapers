# District Platform Crawlers

Headless Selenium crawlers for scraping school district board meeting documents from multiple platforms. Documents are deduplicated, classified via Gemini, and uploaded to Google Drive with metadata logged to PostgreSQL.

## Supported Platforms

| Platform | Crawler | Parallelism |
|---|---|---|
| BoardBook Premier | `crawlers/boardbook/` | Parallel (`ProcessPoolExecutor`) |
| Diligent BoardDocs | `crawlers/diligent/` | Parallel |
| BoardDocs | `crawlers/boarddocs/` | Parallel |
| Simbli eBoard | `crawlers/simbli/` | Parallel |

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

**`doc_collection`** (write) — core crawler tables:

| Table | Purpose |
|---|---|
| `districts` | One row per NCES district (identity) |
| `district_configs` | One row per (district, platform, crawler_type); holds `link`, `check_date`, `last_crawl` |
| `batch_runs` | One row per full crawler invocation; tracks duration and district counts |
| `meetings` | One row per scraped meeting; links to `district_configs` |
| `crawl_attachments` | Per-attachment outcome log with SHA-256, MinHash BIGINT[], dupe metadata |
| `minhash_lsh_index` | LSH band index over `crawl_attachments` for O(candidates) soft-dupe lookup |
| `uploaded_documents` | Docs that passed dedup and were uploaded to Drive |
| `ai_calls` | Per-Gemini-call cost log; rates from `model_pricing` |
| `document_responses` | Structured JSON extracted by AI (JSONB) |
| `cover_pages` | Cover page metadata per agenda item |
| `error_logs` | Crawler errors with platform, type, stack trace |
| `contacts` | Board contacts extracted from meeting pages |
| `prompts` | Versioned prompts; active version controlled by `is_active` flag |
| `drive_folders` | Drive folder ID registry (replaces hardcoded constants) |
| `model_pricing` | AI model cost rates (replaces hardcoded rates) |
| `crawler_run_log` | Daily per-district run summary (formerly `overall_sbd_log`) |

**`documents`** (read-only) — validation pipeline output:

| Table | Purpose |
|---|---|
| `pdfs` | Validated canonical documents; SHA-256 checked to skip re-uploads |
| `quarantine` | Rejected documents; SHA-256 checked to skip re-downloads |
| `duplicate_whitelist` | Per-document MinHash bypass allowlist |

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
