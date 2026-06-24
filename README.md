# District Platform Crawlers

Headless Selenium crawlers for scraping school district board meeting documents from multiple platforms. Documents are deduplicated, classified via Gemini, and uploaded to Google Drive with metadata logged to PostgreSQL.

## Supported Platforms

| Platform | Crawler |
|---|---|
| BoardBook Premier | `crawlers/boardbook/` |
| Diligent BoardDocs | `crawlers/diligent/` |

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

**BoardBook:**
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

## Architecture

```
district_platform_crawlers/
├── config/
│   └── settings.py          # GCS paths, Drive folder IDs, DB secret names, ML params
├── core/                    # Shared infrastructure
│   ├── analyzer.py          # Gemini API calls; PDF → JSON classification
│   ├── database.py          # PostgreSQL reads/writes; GCP Secret Manager bootstrap
│   ├── document_utils.py    # Cover page, PDF merge, OCR, AI routing pipeline
│   ├── driver.py            # Chrome WebDriver factory + browser helpers
│   ├── gcs.py               # GCS download/upload helpers
│   ├── google_auth.py       # OAuth2 for Drive + Sheets
│   ├── google_functions.py  # Drive upload/download, Sheets logging
│   ├── hashing.py           # SHA-256 exact-dupe + MinHash (128 perms, k=5) soft-dupe
│   ├── humanize.py          # Anti-detection mouse/timing simulation
│   ├── models.py            # AttachmentRecord, MeetingRecord dataclasses
│   ├── pdf_functions.py     # PyMuPDF/OCR text extraction, date parsing
│   └── utils.py             # setup_logger(), debug helpers
└── crawlers/
    ├── boardbook/
    │   ├── main.py          # ProcessPoolExecutor batch runner
    │   └── scraper.py       # BoardBook DOM navigation, WebViewer download
    └── diligent/
        ├── main.py          # Sequential batch runner
        └── scraper.py       # Diligent iframe traversal, CDP print-to-PDF
```

### Data Flow (per district)

1. **Bootstrap** — GCS downloads district CSV, credentials, OAuth token to `/tmp`
2. **Scrape** — `undetected_chromedriver` navigates the platform's meeting list; meetings newer than `check_date` are processed
3. **SHA-256 dupe check** — exact binary match against `documents.pdfs` and `doc_collection.crawler_hash`; skips if matched
4. **Cover page + merge** — ReportLab generates a metadata cover; PyMuPDF merges with the downloaded PDF
5. **OCR + MinHash soft-dupe** — text extracted via PyMuPDF (OCR fallback); rejects ≥95% Jaccard similarity matches
6. **Gemini classification** — PDF bytes sent to `gemini-2.5-flash-lite`; structured JSON response routes the doc to its Drive folder
7. **DB logging** — `doc_collection` schema records meeting, uploaded doc, hash, and AI call cost

### Key Config Knobs

| Setting | Location | Purpose |
|---|---|---|
| `WORKER_PROCESSES` | `crawlers/boardbook/main.py` | Parallel Chrome processes |
| `MODEL_NAME` | `config/settings.py` | Gemini model for classification |
| `MINHASH_SIM_THRESHOLD` | `config/settings.py` | Soft-dupe rejection ceiling (default 0.95) |
| `check_date` | District CSV column | Per-district cutoff; older meetings are skipped |

### Database

- **`doc_collection`** (write) — `meetings`, `uploaded_docs`, `crawler_hash`, `contacts`, `prompts`, `ai_calls`
- **`documents`** (read-only) — `pdfs` used for early SHA-256 dupe screening

Credentials are bootstrapped via GCP Secret Manager (`doc_collection_schema_manager`, `DOCUMENTS_READER_POSTGRES`).

## Adding a New Crawler

Create `crawlers/<platform>/` with:
- `__init__.py`
- `main.py` — copy structure from an existing main; update the CSV path and platform navigation
- `scraper.py` — implement `search_and_download_agenda_attachments()` with the same signature

No changes to `core/` or `config/` are needed unless the new platform requires new constants.

## Troubleshooting

- **`PublicMeetingsTable not found`** — BoardBook rate-limited the IP or changed layout. Check `/tmp/screenshot_*.png`.
- **`Could not load DB credentials`** — GCP service account lacks Secret Manager access. Verify: `gcloud secrets versions describe latest --secret="doc_collection_schema_manager"`
- **PDF merge errors** — Corrupted or zero-byte download in `/tmp/*.pdf`. Check with `ls -lh /tmp/*.pdf`.
