# Mailscan

A self-contained Python microservice that accepts a scanned PDF, processes it through
an OCR + barcode pipeline, and returns structured JSON results.

**Phase 1** (current) — FastAPI microservice, no UI.
**Phase 2** (planned) — Next.js dashboard with login, upload UI, API key management, and webhooks.

Full build plan: [`PLAN.md`](./PLAN.md)
Agent operating rules: [`AGENTS.md`](./AGENTS.md)

---

## What It Does

1. Accepts a PDF file via HTTP `POST /process`
2. Converts each page to an image at 300 DPI (PyMuPDF — no poppler needed)
3. Deskews and binarises each image (OpenCV)
4. Runs OCR via Tesseract to extract text
5. Decodes Royal Mail Mailmark Data Matrix barcodes (pylibdmtx)
6. Extracts UK postcodes from OCR text or barcode data
7. Optionally fuzzy-matches recipient against a provided client list (rapidfuzz)
8. Returns structured JSON — one result object per page

---

## Repository Structure

```
mailscan/
│
├── app/
│   ├── __init__.py          ← package marker (empty)
│   ├── main.py              ← FastAPI app — HTTP layer ONLY
│   │                           Endpoints: GET /health, POST /process
│   │                           API key auth via X-API-Key header
│   │                           No processing logic in this file
│   │
│   └── pipeline.py          ← Processing logic ONLY — no HTTP imports
│                               Entry point: process_pdf(pdf_bytes, client_list, dpi)
│                               Called by main.py and directly by tests
│
├── tests/
│   ├── __init__.py          ← package marker (empty)
│   ├── test_pipeline.py     ← Unit tests for pipeline.py
│   │                           Generates PDFs in-memory (PyMuPDF) — no fixture files needed
│   │                           Tests: shape, postcode extract, fuzzy match, multipage, no-match
│   │
│   └── test_api.py          ← HTTP integration tests via FastAPI TestClient
│                               Tests: /health open, /process auth, validation, happy path
│
├── Dockerfile               ← python:3.12-slim + tesseract-ocr + libdmtx0b
├── docker-compose.yml       ← Local dev stack — mounts app/ for live reload
├── requirements.txt         ← All Python dependencies (see Dependencies section)
├── .env.example             ← Template for required environment variables
├── AGENTS.md                ← Strict operating rules for AI coding agents
├── PLAN.md                  ← Full build plan — Phase 1 stages + Phase 2 design
└── README.md                ← This file
```

---

## Architecture

### Separation of Concerns — Enforced

`pipeline.py` and `main.py` have a hard boundary:

| File | Contains | Must NOT contain |
|------|----------|-----------------|
| `pipeline.py` | All processing logic | Any FastAPI / HTTP imports |
| `main.py` | All HTTP concerns | Any cv2, fitz, pytesseract, pylibdmtx imports |

This is enforced in `AGENTS.md`. Do not cross this boundary.

### Call Flow

```
HTTP client
    │
    │  POST /process
    │  Header: X-API-Key: <secret>
    │  Body: multipart/form-data
    │         file=<pdf>
    │         clients=<comma-separated names>  (optional)
    │         dpi=300                           (optional)
    ▼
app/main.py  (FastAPI)
    │  1. Validate API key against MAILSCAN_API_KEY env var
    │  2. Validate file is PDF and non-empty
    │  3. Validate dpi in range 72–600
    │  4. Read file bytes
    ▼
app/pipeline.py  process_pdf(pdf_bytes, client_list, dpi)
    │  1. pdf_bytes → list of RGB images at requested DPI  (PyMuPDF)
    │  2. Per page:
    │     a. Deskew image using minAreaRect on dark pixels  (OpenCV)
    │     b. Binarise via Otsu threshold                    (OpenCV)
    │     c. OCR binarised image, --psm 6                  (Tesseract)
    │     d. Decode barcode on ORIGINAL (not binarised) img (pylibdmtx)
    │     e. Extract UK postcode from OCR text              (regex)
    │        Fallback: extract postcode from barcode data
    │     f. Fuzzy match OCR text against client_list       (rapidfuzz, cutoff 70)
    │  3. Return dict with page_count + pages array
    ▼
app/main.py
    │  Return JSON response
    ▼
HTTP client receives structured result
```

### Important: Barcode Decoding

Barcode decode (`_decode_barcode`) runs on the **original RGB image**, not the
preprocessed binarised version. pylibdmtx performs its own internal thresholding
and works better on the unmodified image.

---

## API Reference

### `GET /health`

No authentication required.

**Response 200:**
```json
{ "status": "ok" }
```

---

### `POST /process`

**Authentication:** `X-API-Key: <MAILSCAN_API_KEY>` header required on every call.

**Request:** `multipart/form-data`

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `file` | File (PDF) | Yes | — | Scanned letter(s) — single or multi-page PDF |
| `clients` | string | No | `""` | Comma-separated client names for fuzzy matching |
| `dpi` | integer | No | `300` | Render DPI. Range: 72–600. Higher = better OCR, slower. |

**Response 200:**
```json
{
  "page_count": 2,
  "pages": [
    {
      "page": 1,
      "ocr_text": "Mr John Smith\n14 High Street\nLuton LU1 1AA",
      "postcode": "LU1 1AA",
      "barcode": "JC123456GB1A2B3C4D",
      "matched_client": "John Smith",
      "match_score": 91.5
    },
    {
      "page": 2,
      "ocr_text": "Dear Sir / Madam...",
      "postcode": null,
      "barcode": null,
      "matched_client": null,
      "match_score": null
    }
  ]
}
```

**Response shape contract** — these field names and types are fixed. Never rename or remove.
New fields may be added in future but existing fields will not change.

**Error responses:**

| Status | Condition |
|--------|-----------|
| 400 | File missing, not a PDF, empty, or `dpi` out of range |
| 401 | `X-API-Key` header missing or does not match `MAILSCAN_API_KEY` |
| 500 | Processing error — `detail` field contains the exception message |

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `MAILSCAN_API_KEY` | **Yes** | — | Shared secret for `X-API-Key` header auth. Generate with `openssl rand -hex 32`. |
| `TESSERACT_CMD` | Linux/Mac | — | Path to tesseract binary. Set automatically in Docker to `/usr/bin/tesseract`. On Windows defaults to `C:\Program Files\Tesseract-OCR\tesseract.exe`. |
| `PORT` | No | `8000` | HTTP port the server listens on. |

Copy `.env.example` to `.env` and fill in `MAILSCAN_API_KEY` before starting.

---

## Quick Start (Docker — recommended)

```bash
git clone git@github.com:Adap1oCode/mailscan.git
cd mailscan
git checkout dev

cp .env.example .env
# Edit .env — set MAILSCAN_API_KEY to a strong random string

docker compose up --build
```

Service starts at `http://localhost:8000`.
Interactive API docs at `http://localhost:8000/docs`.

**Smoke test:**
```bash
curl -X POST http://localhost:8000/process \
  -H "X-API-Key: your-api-key" \
  -F "file=@/path/to/scan.pdf"
```

**With client matching:**
```bash
curl -X POST http://localhost:8000/process \
  -H "X-API-Key: your-api-key" \
  -F "file=@/path/to/scan.pdf" \
  -F "clients=Acme Ltd,Beta Corp,Gamma LLC"
```

---

## Local Development (no Docker)

Requires Python 3.12+ and Tesseract installed as a system app.

**Install Tesseract:**
```bash
# Ubuntu / Debian
sudo apt-get install -y tesseract-ocr libdmtx0b

# macOS
brew install tesseract libdmtx

# Windows
winget install --id UB-Mannheim.TesseractOCR
```

**Install Python dependencies:**
```bash
pip install -r requirements.txt
```

**Run the service:**
```bash
# Linux/Mac — point at tesseract binary
export TESSERACT_CMD=$(which tesseract)
export MAILSCAN_API_KEY=dev-key-123
uvicorn app.main:app --reload

# Windows (PowerShell)
$env:MAILSCAN_API_KEY="dev-key-123"
uvicorn app.main:app --reload
```

---

## Running Tests

Tests use `pytest` and `fastapi.testclient.TestClient` — no running server or test fixtures needed.
PDFs are generated in-memory by the tests themselves using PyMuPDF.

```bash
# Install deps if not already done
pip install -r requirements.txt

# Run all tests
pytest tests/

# Run with output
pytest tests/ -v
```

**test_pipeline.py** — tests `pipeline.py` directly, no HTTP:
- `test_process_pdf_returns_expected_shape` — verifies response structure
- `test_postcode_extraction` — verifies `LU1 1AA` extracted from address text
- `test_no_postcode_returns_none` — verifies `null` when no postcode present
- `test_client_fuzzy_match` — verifies rapidfuzz matches `Acme Industries Ltd`
- `test_no_clients_returns_none_match` — verifies null when no client list provided
- `test_multipage_pdf` — verifies 3-page PDF returns 3 result objects

**test_api.py** — tests HTTP layer via TestClient:
- `test_health_no_auth` — `/health` returns 200 without auth
- `test_process_missing_key_returns_401` — no header → 401
- `test_process_wrong_key_returns_401` — wrong key → 401
- `test_process_non_pdf_returns_400` — PNG file → 400
- `test_process_empty_file_returns_400` — empty PDF → 400
- `test_process_valid_pdf_returns_results` — valid PDF → 200 with result
- `test_process_with_clients` — client list passed → matched_client populated
- `test_process_invalid_dpi_returns_400` — dpi=9999 → 400

---

## Dependencies

All in `requirements.txt`. Do not add new dependencies without updating this file and `AGENTS.md`.

| Package | Purpose |
|---------|---------|
| `fastapi` | HTTP framework |
| `uvicorn[standard]` | ASGI server |
| `python-multipart` | Multipart form parsing (required for file uploads in FastAPI) |
| `PyMuPDF` | PDF → image conversion. Import name is `fitz`. No poppler required. |
| `pytesseract` | Python wrapper around the Tesseract binary. Tesseract must be installed separately as a system app. |
| `pylibdmtx` | Royal Mail Mailmark Data Matrix barcode decoding. Requires `libdmtx0b` on Linux. |
| `rapidfuzz` | Fast fuzzy string matching for client name matching |
| `opencv-python-headless` | Image deskew and binarisation. Headless build — no GUI deps. Do NOT swap for `opencv-python`. |
| `numpy` | Array operations used by OpenCV |
| `Pillow` | PIL Image — required by pytesseract and pylibdmtx |
| `tabulate` | Optional table formatting for CLI output |
| `setuptools` | Required on Python 3.12+ — pylibdmtx imports `distutils` which was removed from stdlib |

---

## Docker

### Dockerfile

Base: `python:3.12-slim`

System packages installed:
- `tesseract-ocr` — OCR engine
- `libdmtx0b` — Data Matrix barcode decoder (required by pylibdmtx on Linux)
- `libgl1` — OpenCV dependency
- `libglib2.0-0` — OpenCV dependency

`TESSERACT_CMD` is set to `/usr/bin/tesseract` in the image — no manual config needed in Docker.

### docker-compose.yml

Single service: `mailscan` on port `8000`.
In local dev mode, `app/` is volume-mounted for live reload.
Remove the volume mount for production Coolify deployments.

---

## Deployment — Coolify

This service is deployed via Coolify as a Docker Compose application.

**Branches → environments:**

| Branch | Environment |
|--------|-------------|
| `dev` | Development / staging |
| `main` | Production |

---

### Step-by-step Coolify setup

**1. Create a new resource**
- Coolify → New Resource → Docker Compose
- Connect to GitHub repo: `Adap1oCode/mailscan`
- Select branch: `dev` (or `main` for production)

**2. Set the Compose file**
- Compose file path: `docker-compose.coolify.yml`
  *(Do NOT use `docker-compose.yml` — that one has the dev volume mount)*

**3. Set the domain**
- Attach your domain e.g. `mailscan-dev.adaplo.io`
- Enable HTTPS / Let's Encrypt

**4. Set environment variables**

| Variable | Value |
|----------|-------|
| `MAILSCAN_API_KEY` | Generate: `openssl rand -hex 32` |
| `TESSERACT_CMD` | `/usr/bin/tesseract` |
| `PORT` | `8000` |

**5. Deploy**
- Click Deploy
- Watch build logs — pip install takes ~2 minutes on first build (heavy deps)
- Healthcheck at `/health` confirms service is up

**6. Smoke test after deploy**
```bash
curl https://mailscan-dev.adaplo.io/health
# → {"status":"ok"}

curl -X POST https://mailscan-dev.adaplo.io/process \
  -H "X-API-Key: <your-key>" \
  -F "file=@scan.pdf"
```

---

**Internal service URL** (for other containers on the same Coolify network):
```
http://mailscan:8000
```

**Public URL:**
```
https://mailscan-dev.adaplo.io
```

---

## Integration — n8n

In an n8n HTTP Request node:

| Field | Value |
|-------|-------|
| Method | POST |
| URL | `https://mailscan.adaplo.io/process` |
| Authentication | Header Auth |
| Header Name | `X-API-Key` |
| Header Value | `<MAILSCAN_API_KEY>` |
| Body Content Type | Form-Data (multipart) |
| Body Parameter `file` | Binary — pass the PDF file from a previous node |
| Body Parameter `clients` | String — comma-separated client names (optional) |

---

## Integration — luton-eng-dashboard

The dashboard at `/opt/projects/luton-eng-dashboard` will proxy calls to this service
via a Next.js API route (Phase 1, Stage 4 — not yet built):

```
src/app/api/process/[processor]/route.ts
```

Dashboard env vars required:
- `MAILSCAN_SERVICE_URL=http://mailscan:8000`
- `MAILSCAN_API_KEY=<same key as the service>`

---

## Phase 2 — Dashboard (Planned)

Phase 2 adds a full web dashboard on top of this service:

- **Login** via Supabase Auth (email/password + magic link)
- **Upload UI** — drag-and-drop PDF, view results per page
- **Scan history** — paginated table, CSV export
- **API key management** — create named keys, shown once, revocable
- **Webhooks** — configure outbound HTTP notifications on `scan.complete` / `scan.error` with HMAC signing
- **Multi-org** — invite team members, role-based access

Full Phase 2 design including data model, routes, and build stages is in [`PLAN.md`](./PLAN.md).

---

## Branch Rules

| Rule | Detail |
|------|--------|
| Working branch | Always `dev` |
| Push target | `origin dev` only |
| Merges to `main` | Waseem only |
| Commit style | Conventional commits: `feat:` `fix:` `test:` `docs:` `chore:` |

---

## Key Files Quick Reference

| File | Purpose | Read before... |
|------|---------|----------------|
| `PLAN.md` | Build plan — all stages, data model, API contract | Starting any task |
| `AGENTS.md` | Strict rules for AI coding agents | Writing any code |
| `app/pipeline.py` | Processing logic — the core of the service | Changing OCR/barcode/matching behaviour |
| `app/main.py` | FastAPI HTTP layer | Changing endpoints or auth |
| `tests/test_pipeline.py` | Unit tests for pipeline | Changing pipeline functions |
| `tests/test_api.py` | HTTP integration tests | Changing API routes or auth |
| `Dockerfile` | Container build — system deps listed here | Changing Python or system dependencies |
| `.env.example` | All env vars with descriptions | Setting up a new environment |
