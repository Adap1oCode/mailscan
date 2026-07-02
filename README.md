# Mailscan

A self-contained Python microservice that accepts a scanned PDF, processes it through
an OCR + barcode pipeline, and returns structured JSON results.

**Phase 1** (current) — FastAPI microservice, no UI.
**Phase 2** (planned) — Next.js dashboard with login, upload UI, API key management, and webhooks.

| Document | Purpose |
|----------|---------|
| [`PLAN.md`](./PLAN.md) | Full build plan — all stages, Phase 2 design |
| [`AGENTS.md`](./AGENTS.md) | Strict operating rules for AI coding agents |
| [`RESEARCH.md`](./RESEARCH.md) | Open source landscape, future improvement candidates |
| [`TESTS.md`](./TESTS.md) | Live service test results against https://mailscan.adaplo.io |

---

## What It Does

1. Accepts a PDF file via HTTP
2. Converts each page to an image at 300 DPI (PyMuPDF — no poppler needed)
3. Deskews and binarises each image (OpenCV)
4. Runs OCR via Tesseract, using OCRmyPDF for word-level bounding boxes
5. Decodes Royal Mail Mailmark or consumer stamp barcodes (pylibdmtx)
6. Extracts UK postcodes — regex by default, libpostal ML parser when enabled
7. Optionally fuzzy-matches recipient against a provided client list (rapidfuzz)
8. Returns structured JSON — one result object per page

Submissions are processed asynchronously via a Celery + Redis job queue.
A synchronous endpoint (`/process/sync`) is also available for simple callers like n8n.

---

## Repository Structure

```
mailscan/
│
├── app/
│   ├── __init__.py          ← package marker (empty)
│   ├── main.py              ← FastAPI app — HTTP layer ONLY
│   │                           Endpoints: GET /health, POST /process,
│   │                           GET /jobs/{id}, POST /process/sync
│   │                           API key auth via X-API-Key header
│   │                           No processing logic in this file
│   │
│   ├── pipeline.py          ← Processing logic ONLY — no HTTP imports
│   │                           Entry point: process_pdf(pdf_bytes, client_list, dpi)
│   │                           Called by worker.py and directly by tests
│   │
│   └── worker.py            ← Celery task definition
│                               Wraps process_pdf() for async execution
│                               PDF bytes base64-encoded for Redis serialisation
│
├── tests/
│   ├── __init__.py
│   ├── test_pipeline.py     ← Unit tests for pipeline.py (in-memory PDFs, no fixtures)
│   └── test_api.py          ← HTTP integration tests via FastAPI TestClient
│
├── Dockerfile               ← python:3.12-slim + tesseract + libdmtx + ghostscript
├── docker-compose.yml       ← Local dev: mailscan + worker + redis, app/ volume-mounted
├── docker-compose.coolify.yml  ← Coolify deploy: mailscan + worker + redis, no volume mount
├── requirements.txt         ← All Python dependencies
├── .env.example             ← All environment variables with descriptions
├── AGENTS.md                ← Strict operating rules for AI coding agents
├── PLAN.md                  ← Full build plan — Phase 1 stages + Phase 2 design
├── RESEARCH.md              ← Open source landscape and future improvement notes
├── TESTS.md                 ← Live service test results (9/9 pass)
└── README.md                ← This file
```

---

## Architecture

### Separation of Concerns — Enforced

`pipeline.py` and `main.py` have a hard boundary enforced in `AGENTS.md`:

| File | Contains | Must NOT contain |
|------|----------|-----------------|
| `pipeline.py` | All processing logic | Any FastAPI / HTTP imports |
| `main.py` | All HTTP concerns | Any cv2, fitz, pytesseract, pylibdmtx imports |
| `worker.py` | Celery task wrapper | Any direct HTTP concerns |

### Call Flow

```
HTTP client
    │
    │  POST /process  (or /process/sync for synchronous)
    │  Header: X-API-Key: <secret>
    │  Body:   file=<pdf>, clients=<csv>, dpi=300
    ▼
app/main.py  (FastAPI)
    │  1. Validate API key
    │  2. Validate file — PDF, non-empty
    │  3. Validate dpi range 72–600
    │  4. If REDIS_URL set → submit to Celery → return job_id
    │     If no REDIS_URL  → run pipeline directly → return result
    ▼
app/worker.py  (Celery — async path only)
    │  Deserialises PDF bytes (base64) → calls process_pdf()
    ▼
app/pipeline.py  process_pdf(pdf_bytes, client_list, dpi)
    │
    │  Per page:
    │  1. PDF → RGB image at DPI          (PyMuPDF)
    │  2. Deskew + binarise               (OpenCV)
    │  3. OCR via OCRmyPDF hOCR mode      (word-level bounding boxes)
    │     → falls back to pytesseract if OCRmyPDF unavailable
    │  4. Barcode decode on ORIGINAL img  (pylibdmtx)
    │     → classify: mailmark | stamp | unknown
    │     → parse fields if format known
    │  5. Extract postcode from OCR text  (regex OR libpostal — see ADDRESS_PARSER)
    │     → fallback to barcode data if not found in OCR
    │  6. Fuzzy match against client_list (rapidfuzz, score cutoff 70)
    ▼
Result returned via job poll (GET /jobs/{id}) or directly (/process/sync)
```

### Why OCRmyPDF instead of raw Tesseract?

The original implementation called `pytesseract.image_to_string()` directly, which returns
a flat text string. The problem: searching the entire page for a postcode regex produces
false positives on dense documents.

OCRmyPDF runs Tesseract in hOCR mode, which returns an XML document with the coordinates
of every word on the page. This lets the pipeline localise the address block (typically
top portion of a letter) and search that region first, reducing false positives and
improving accuracy on complex layouts. pytesseract remains as a silent fallback.

### Why a Celery job queue?

The original synchronous `/process` endpoint blocks the HTTP connection for the full
duration of OCR processing — typically 5–30 seconds depending on page count and DPI.
n8n HTTP nodes have a 30-second default timeout, meaning large PDFs would silently fail.

The async pattern (`POST /process` → job_id → `GET /jobs/{id}`) decouples submission
from processing. The `/process/sync` endpoint is kept for callers that genuinely want
to block (quick scripts, simple integrations). When `REDIS_URL` is not set, `/process`
falls back to synchronous mode automatically — fully backwards compatible.

### Why libpostal as an opt-in?

Live testing (`TESTS.md`, Test 9) proved the postcode regex fails on degraded scans:
at 72 DPI Tesseract read `LU1` as `LUT`, causing the regex to find no match. libpostal
is an ML model trained on 1 billion addresses that handles OCR noise like missing spaces
(`LU11AA`), extra spaces (`L U1 1AA`), and character substitutions (`LUT` → `LU1`).

It is opt-in (`ADDRESS_PARSER=libpostal`) because compiling libpostal from source
increases the Docker image from ~800MB to ~2GB. The default regex path is fast,
lightweight, and accurate at 300 DPI. Enable libpostal when processing poor-quality
scans or when postcode extraction accuracy is critical.

### Why pylibdmtx from GitHub HEAD?

The PyPI release (v0.1.10, March 2022) is significantly behind the GitHub HEAD and
contains known bugs in image handling. Since barcode decode is a core feature of the
pipeline, installing from source ensures the latest fixes are included.

---

## API Reference

### `GET /health`

No authentication required. Used by Docker healthcheck and Coolify uptime monitoring.

```json
{ "status": "ok" }
```

---

### `POST /process`

Submit a PDF for processing. Returns a job ID to poll asynchronously.
Falls back to synchronous result if `REDIS_URL` is not configured.

**Auth:** `X-API-Key: <MAILSCAN_API_KEY>`

**Request:** `multipart/form-data`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `file` | PDF | Yes | — | Scanned letter(s) — single or multi-page |
| `clients` | string | No | `""` | Comma-separated client names for fuzzy matching |
| `dpi` | integer | No | `MAILSCAN_RENDER_DPI` (200) | Render DPI. Range 72–600. Higher = better OCR, slower. |
| `separate` | bool | No | `false` | Split a multi-letter batch on MVOS-DOC-SEP → `documents[]` |
| `enable_ai` | bool | No | `false` | Allow AI fallback on low-confidence pages/letters |
| `ai_credentials` | JSON string | No | `""` | AI provider creds bundle (textract/openrouter/gemini) |
| `ai_prefer` | string | No | `""` | Preferred AI provider name |
| `options` | JSON string | No | `""` | **Per-request overrides** — see below |

**`options` — the tenant-override channel.** Every value overrides the server
default for this request only, so each caller/org can run mailscan with its own
prompts, models, and thresholds. All keys optional; unknown keys ignored:

```json
{
  "prompts": {
    "extract": "custom recipient-extraction system prompt (must keep the JSON output contract)",
    "summary": "custom letter-summary system prompt (must keep the JSON output contract)"
  },
  "models":  { "extract": "openrouter model id", "summary": "openrouter model id" },
  "limits":  { "extract_chars": 6000, "summary_chars": 8000 },
  "match":   { "cutoff": 85, "margin": 10, "name_conf": 0.6,
               "shared_postcodes": ["LU1 2DW"] },
  "split":   { "blank_ink_pct": 0.6, "blank_keep_floor": 0.1 }
}
```

Accepted by `POST /process`, `POST /process/sync`, `POST /split` (split section
only) and `POST /ai/letter` (prompts/models/limits).

**Response 200 — async (Redis configured):**
```json
{ "job_id": "abc-123", "status": "pending" }
```

**Response 200 — sync fallback (no Redis):**
```json
{
  "job_id": null,
  "status": "complete",
  "result": { ... }
}
```

---

### `GET /jobs/{job_id}`

Poll the status and result of an async job.

**Auth:** `X-API-Key: <MAILSCAN_API_KEY>`

**Response:**
```json
{
  "job_id": "abc-123",
  "status": "pending | processing | complete | error",
  "result": { ... } | null
}
```

Returns 404 if `REDIS_URL` is not configured (async not available).

---

### `POST /process/sync`

Always synchronous — blocks until processing is complete, returns result directly.
Use for n8n, scripts, and simple integrations that don't want to poll.
**Caution:** may timeout on large PDFs. Use async `/process` for production workloads.

**Auth:** `X-API-Key: <MAILSCAN_API_KEY>`

**Request:** same fields as `POST /process`

**Response 200:**
```json
{
  "page_count": 2,
  "pages": [
    {
      "page": 1,
      "ocr_text": "Mr John Smith\n14 High Street\nLuton LU1 1AA",
      "postcode": "LU1 1AA",
      "address_components": null,
      "barcode": "JGB21234567890ABCDE",
      "barcode_type": "mailmark",
      "barcode_fields": {
        "raw": "JGB21234567890ABCDE",
        "version": "J",
        "mail_class": "GB",
        "postcode": "LU1 1AA"
      },
      "matched_client": "John Smith",
      "match_score": 91.5
    }
  ]
}
```

> `address_components` is `null` when `ADDRESS_PARSER=regex` (default).
> When `ADDRESS_PARSER=libpostal`, it contains structured fields: `road`, `city`, `postcode`, etc.

> `barcode_type` is always present. Values: `mailmark` | `stamp` | `unknown`.
> `barcode_fields` is `null` when barcode is `null` or type is `unknown`.

**Response shape contract** — field names and types are fixed. New fields may be added
but existing fields will not be renamed or removed.

**Error responses:**

| Status | Condition |
|--------|-----------|
| 400 | Not a PDF, empty file, or `dpi` out of range |
| 401 | `X-API-Key` missing or incorrect |
| 404 | Job ID not found (GET /jobs only) |
| 500 | Processing error — `detail` contains the exception message |

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MAILSCAN_API_KEY` | **Yes** | — | Shared secret for `X-API-Key` auth. Generate with `openssl rand -hex 32`. |
| `TESSERACT_CMD` | Linux/Mac | — | Path to tesseract binary. Set to `/usr/bin/tesseract` automatically in Docker. |
| `PORT` | No | `8000` | HTTP port. |
| `REDIS_URL` | No | — | Redis connection string e.g. `redis://redis:6379/0`. Enables the Celery job queue. Without this, `/process` runs jobs on an in-process thread pool (same job_id/poll API). |
| `ADDRESS_PARSER` | No | `regex` | Set to `libpostal` for ML-based noise-tolerant address parsing. Requires libpostal compiled in image. See `RESEARCH.md`. |
| `MAILSCAN_MAX_UPLOAD_MB` | No | `100` | Reject uploads larger than this (HTTP 413). |
| `MAILSCAN_MAX_PAGES` | No | `500` | Reject PDFs with more pages than this (HTTP 413). |
| `MAILSCAN_JOBS_MAX` | No | `64` | Max jobs kept in the in-process registry (no-Redis mode). |
| `MAILSCAN_JOB_TTL_SEC` | No | `3600` | Finished in-process job results expire after this many seconds. |
| `MAILSCAN_AI_CONCURRENCY` | No | `4` | Letters enriched concurrently (AI tier-up + summary) in batch mode. |
| `MAILSCAN_AI_ENABLE_MOCK` | No | `0` | Set `1` to enable the no-key mock AI provider (tests/demos only — never in production). |
| `MAILSCAN_EXTRACT_PROMPT` | No | built-in | Server-default recipient-extraction prompt (per-request `options.prompts.extract` overrides). |
| `MAILSCAN_SUMMARY_PROMPT` | No | built-in | Server-default letter-summary prompt (per-request `options.prompts.summary` overrides). |
| `MAILSCAN_EXTRACT_CHARS` | No | `6000` | Max OCR chars sent to the extraction model. |
| `MAILSCAN_SUMMARY_CHARS` | No | `8000` | Max OCR chars sent to the summary model. |

Copy `.env.example` to `.env` and fill in `MAILSCAN_API_KEY` before starting.

---

## Quick Start (Docker — recommended)

```bash
git clone git@github.com:Adap1oCode/mailscan.git
cd mailscan
git checkout dev

cp .env.example .env
# Edit .env — set MAILSCAN_API_KEY

docker compose up --build
```

Starts three containers: `mailscan` (API), `worker` (Celery), `redis`.
API at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

**Smoke test — async:**
```bash
# Submit job
JOB=$(curl -s -X POST http://localhost:8000/process \
  -H "X-API-Key: your-key" \
  -F "file=@scan.pdf" | jq -r .job_id)

# Poll for result
curl -s http://localhost:8000/jobs/$JOB \
  -H "X-API-Key: your-key"
```

**Smoke test — synchronous:**
```bash
curl -s -X POST http://localhost:8000/process/sync \
  -H "X-API-Key: your-key" \
  -F "file=@scan.pdf" \
  -F "clients=Acme Ltd,Beta Corp"
```

---

## Local Development (no Docker)

Requires Python 3.12+, Tesseract, and Redis installed as system apps.

```bash
# Ubuntu / Debian
sudo apt-get install -y tesseract-ocr libdmtx0b ghostscript redis-server

# macOS
brew install tesseract libdmtx ghostscript redis

# Windows
winget install --id UB-Mannheim.TesseractOCR
# Redis on Windows: use WSL or Docker
```

```bash
pip install -r requirements.txt

export TESSERACT_CMD=$(which tesseract)
export MAILSCAN_API_KEY=dev-key-123
export REDIS_URL=redis://localhost:6379/0

# Terminal 1 — API server
uvicorn app.main:app --reload

# Terminal 2 — Celery worker
celery -A app.worker worker --loglevel=info
```

---

## Running Tests

No running server or test fixtures needed. PDFs generated in-memory by the tests.
Tests run without Redis — `/process` automatically uses sync fallback.

```bash
pip install -r requirements.txt
pytest tests/ -v
```

**test_pipeline.py:**
- `test_process_pdf_returns_expected_shape` — all fields present including new ones
- `test_postcode_extraction` — `LU1 1AA` extracted from address text
- `test_no_postcode_returns_none` — null when no postcode present
- `test_client_fuzzy_match` — rapidfuzz matches `Acme Industries Ltd` at score > 70
- `test_no_clients_returns_none_match` — null when no client list provided
- `test_multipage_pdf` — 3-page PDF returns 3 result objects
- `test_barcode_type_is_unknown_when_no_barcode` — `barcode_type=unknown`, `barcode_fields=null`
- `test_address_components_none_when_regex_parser` — `address_components=null` with default parser

**test_api.py:**
- `test_health_no_auth` — `/health` open, returns ok
- `test_process_missing_key_returns_401` — no header → 401
- `test_process_wrong_key_returns_401` — wrong key → 401
- `test_process_non_pdf_returns_400` — PNG → 400
- `test_process_empty_file_returns_400` — empty file → 400
- `test_process_invalid_dpi_returns_400` — dpi=9999 → 400
- `test_process_valid_pdf_returns_result` — sync fallback → status=complete + result
- `test_process_result_has_new_fields` — `barcode_type`, `barcode_fields`, `address_components` present
- `test_process_with_clients` — client list → matched_client populated
- `test_process_sync_returns_result_directly` — `/process/sync` returns result shape directly
- `test_jobs_endpoint_404_without_redis` — `/jobs/{id}` → 404 when no Redis

---

## Dependencies

| Package | Purpose | Why this one |
|---------|---------|-------------|
| `fastapi` | HTTP framework | Async, typed, auto /docs |
| `uvicorn[standard]` | ASGI server | Production-grade, works with FastAPI |
| `python-multipart` | Multipart form parsing | Required for file uploads in FastAPI |
| `PyMuPDF` | PDF → image | No poppler dependency — self-contained |
| `pytesseract` | OCR fallback | Wraps Tesseract binary. Used when OCRmyPDF unavailable. |
| `ocrmypdf` | OCR primary | hOCR output gives word-level bounding boxes — more accurate postcode localisation than flat text. `ghostscript` system dep required. |
| `git+...pylibdmtx` | Barcode decode | GitHub HEAD installed (not PyPI) — fixes known bugs in v0.1.10 (March 2022). Only credible Python option for Data Matrix. |
| `rapidfuzz` | Fuzzy client matching | Fast Levenshtein-based matching. Score cutoff 70 prevents false positives. |
| `opencv-python-headless` | Image preprocessing | Deskew + binarise. Headless build — no GUI deps, required in Docker. Do NOT swap for `opencv-python`. |
| `numpy` | Array operations | Used by OpenCV |
| `Pillow` | PIL Image | Required by pytesseract and pylibdmtx |
| `celery[redis]` | Async job queue | Prevents HTTP timeouts on large PDFs. Redis backend stores results for 1 hour. |
| `tabulate` | Table formatting | Optional CLI output |
| `setuptools` | distutils shim | Required on Python 3.12+ — pylibdmtx imports `distutils` which was removed from stdlib |

---

## Docker

### Dockerfile

Base: `python:3.12-slim`

System packages:
| Package | Required by |
|---------|------------|
| `tesseract-ocr` | pytesseract / OCRmyPDF |
| `libdmtx0b` | pylibdmtx (barcode decode) |
| `ghostscript` | OCRmyPDF |
| `pngquant` | OCRmyPDF image optimisation |
| `unpaper` | OCRmyPDF deskew |
| `libgl1` | OpenCV |
| `libglib2.0-0` | OpenCV |
| `curl` | Docker HEALTHCHECK |
| `git` | pip install pylibdmtx from GitHub HEAD |

### Compose files

| File | Use |
|------|-----|
| `docker-compose.yml` | Local dev — `app/` volume-mounted for live reload |
| `docker-compose.coolify.yml` | Coolify deploy — no volume mount, PORT from env |

Both include `mailscan`, `worker` (Celery), and `redis` services.

---

## Deployment — Coolify

### Step-by-step

**1.** Coolify → New Resource → Docker Compose → `Adap1oCode/mailscan` → branch `dev`

**2.** Compose file: **`docker-compose.coolify.yml`**
*(Not `docker-compose.yml` — that has the dev volume mount)*

**3.** Set domain + HTTPS (e.g. `mailscan.adaplo.io`)

**4.** Set environment variables:

| Variable | Value |
|----------|-------|
| `MAILSCAN_API_KEY` | `openssl rand -hex 32` |
| `TESSERACT_CMD` | `/usr/bin/tesseract` |
| `PORT` | `8000` |
| `REDIS_URL` | `redis://redis:6379/0` |
| `ADDRESS_PARSER` | `regex` (or `libpostal` if compiled) |

**5.** Deploy — first build takes ~3–4 minutes (pip installing heavy deps + ghostscript)

**6.** Verify:
```bash
curl https://mailscan.adaplo.io/health
# → {"status":"ok"}
```

**Internal URL** (for other Coolify containers on the same network):
```
http://mailscan:8000
```

---

## Integration — n8n

Use `POST /process/sync` for n8n — simpler than polling:

| Field | Value |
|-------|-------|
| Method | POST |
| URL | `https://mailscan.adaplo.io/process/sync` |
| Authentication | Header Auth → `X-API-Key: <key>` |
| Body | Form-Data (multipart) |
| `file` | Binary input from previous node |
| `clients` | String — comma-separated names (optional) |

For large PDFs or high-volume workflows, switch to `POST /process` + a polling loop
on `GET /jobs/{id}` to avoid n8n's 30-second timeout.

---

## Integration — luton-eng-dashboard

Stage 4 (not yet built) adds a proxy route to `/opt/projects/luton-eng-dashboard`:

```
src/app/api/process/[processor]/route.ts
```

Dashboard env vars to add:
- `MAILSCAN_SERVICE_URL=http://mailscan:8000`
- `MAILSCAN_API_KEY=<same key as the service>`

---

## Phase 2 — Dashboard (Planned)

A full web UI on top of this service. See [`PLAN.md`](./PLAN.md) for the full design.

- Login via Supabase Auth
- Drag-and-drop PDF upload with per-page results view
- Scan history with CSV export
- API key management (create, show once, revoke)
- Webhooks with HMAC signing on `scan.complete` / `scan.error`
- Multi-org with role-based access

---

## Branch Rules

| Rule | Detail |
|------|--------|
| Working branch | Always `dev` |
| Push target | `origin dev` only |
| Merges to `main` | Waseem only |
| Commit style | `feat:` `fix:` `test:` `docs:` `chore:` |

---

## Key Files Quick Reference

| File | Purpose | Read before... |
|------|---------|----------------|
| `PLAN.md` | All build stages + Phase 2 design | Starting any task |
| `AGENTS.md` | Rules for AI coding agents | Writing any code |
| `RESEARCH.md` | Open source landscape, future improvements | Adding new capabilities |
| `TESTS.md` | Live test results + known gaps | Running or updating tests |
| `app/pipeline.py` | Core processing logic | Changing OCR/barcode/matching |
| `app/main.py` | FastAPI HTTP layer | Changing endpoints or auth |
| `app/worker.py` | Celery async task | Changing job queue behaviour |
| `tests/test_pipeline.py` | Pipeline unit tests | Changing pipeline functions |
| `tests/test_api.py` | HTTP integration tests | Changing API routes or auth |
| `Dockerfile` | Container build + system deps | Changing any dependency |
| `.env.example` | All env vars | Setting up a new environment |
