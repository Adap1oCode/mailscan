# AGENTS.md

# Mailscan AI Agent Operating Instructions

This repository is maintained with AI coding agents including:
- Claude Code
- OpenCode
- Cursor
- Cline
- Aider

ALL agents MUST follow these instructions strictly.

Failure to follow these rules will result in rejected changes.

---

# PRIMARY OBJECTIVE

Build and maintain a focused, self-contained Python microservice that:

1. Accepts a scanned PDF via HTTP
2. Processes it through an OCR + barcode pipeline
3. Returns structured JSON results — one object per page

This is NOT a general-purpose document processing platform.
Do NOT add features beyond the scope defined in `PLAN.md`.

---

# REPOSITORY STRUCTURE

Agents MUST preserve this structure.

```
mailscan/
├── app/
│   ├── __init__.py
│   ├── main.py         ← FastAPI HTTP layer ONLY — no processing logic here
│   ├── pipeline.py     ← Processing logic ONLY — no HTTP code here
│   └── worker.py       ← Celery task wrapper ONLY — calls pipeline.process_pdf()
├── tests/
│   ├── __init__.py
│   ├── test_pipeline.py
│   └── test_api.py
├── Dockerfile
├── docker-compose.yml          ← local dev (with app/ volume mount)
├── docker-compose.coolify.yml  ← Coolify production (no volume mount)
├── requirements.txt
├── .env.example
├── AGENTS.md           ← this file
├── PLAN.md
├── RESEARCH.md
├── TESTS.md
└── README.md
```

Do NOT:
- create subdirectories inside `app/` without explicit instruction
- create additional `main.py` or entrypoint files
- introduce a `src/` wrapper
- add a CLI entrypoint unless specifically requested

---

# ARCHITECTURAL RULES

## Separation of concerns — MANDATORY

Three files, three responsibilities. These boundaries are hard:

| File | Contains | Must NOT contain |
|------|----------|-----------------|
| `pipeline.py` | All processing logic | Any FastAPI / HTTP imports |
| `main.py` | All HTTP concerns | Any cv2, fitz, pytesseract, pylibdmtx imports |
| `worker.py` | Celery task definition | Any direct HTTP concerns or processing logic |

`pipeline.py` contains ONLY:
- PDF → images (PyMuPDF)
- Image preprocessing (deskew, binarise) via OpenCV
- OCR via OCRmyPDF hOCR mode (pytesseract as fallback)
- Barcode decode via pylibdmtx + field parsers (Mailmark, stamp)
- Postcode extraction (regex or libpostal — controlled by `ADDRESS_PARSER` env var)
- Client fuzzy matching via rapidfuzz

`main.py` contains ONLY:
- FastAPI app definition
- Route handlers
- Request validation
- API key auth
- Celery task submission / job polling
- Error → HTTP status mapping

`worker.py` contains ONLY:
- Celery app configuration
- `process_pdf_task` — base64-decodes PDF bytes, calls `pipeline.process_pdf()`

These three files MUST remain independently testable.
`pipeline.py` MUST be importable with no FastAPI or Celery dependency.
`main.py` MUST contain no image processing, OCR, or regex logic.
`worker.py` MUST contain no HTTP routing or request parsing.

## Do NOT

- mix HTTP and processing logic in any single function
- import `fastapi` anywhere in `pipeline.py` or `worker.py`
- import `cv2`, `fitz`, `pytesseract`, or `pylibdmtx` in `main.py`
- add a database layer without explicit instruction
- add file storage (S3, Supabase) without explicit instruction

---

# API RULES

## Endpoints

The service exposes four endpoints:

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | None | Liveness check |
| POST | `/process` | X-API-Key | Submit PDF — returns job_id (async) or result (sync fallback) |
| GET | `/jobs/{job_id}` | X-API-Key | Poll async job status and result |
| POST | `/process/sync` | X-API-Key | Submit PDF — always blocks until result returned |

Do NOT add endpoints without explicit instruction.

## Authentication

All endpoints except `/health` MUST be authenticated via `X-API-Key` header.
The key is read from the `MAILSCAN_API_KEY` environment variable.
`/health` is always open — no auth.

Do NOT:
- add session-based auth
- add OAuth
- add JWT
- bypass the API key check

## Async behaviour

`POST /process` behaviour depends on whether `REDIS_URL` is set:
- `REDIS_URL` set → submit to Celery → return `{"job_id": "...", "status": "pending"}`
- `REDIS_URL` not set → run synchronously → return `{"job_id": null, "status": "complete", "result": {...}}`

This fallback is intentional and MUST be preserved. Do NOT remove it.

## Response shape

The `/process/sync` and job result response shape is the contract.
Do NOT change field names or types. New fields may be added but existing ones are locked.

```json
{
  "page_count": int,
  "pages": [
    {
      "page": int,
      "ocr_text": string,
      "postcode": string | null,
      "address_components": dict | null,
      "barcode": string | null,
      "barcode_type": string,
      "barcode_fields": dict | null,
      "matched_client": string | null,
      "match_score": float | null
    }
  ]
}
```

Field notes:
- `address_components` — populated only when `ADDRESS_PARSER=libpostal`. Null with default regex parser.
- `barcode_type` — always present. Values: `mailmark` | `stamp` | `unknown`
- `barcode_fields` — null when barcode is null or type is unknown. Contains parsed fields when type is known.

---

# PIPELINE RULES

## `process_pdf()` signature

```python
def process_pdf(
    pdf_bytes: bytes,
    client_list: list[str] | None = None,
    dpi: int = 300,
) -> dict[str, Any]:
```

This function signature is the internal contract. Do NOT change parameter names.

## Processing steps — required order

1. PDF → images (PyMuPDF at requested DPI)
2. Preprocess each image (deskew → binarise) via OpenCV
3. OCR via OCRmyPDF hOCR mode for word-level bounding boxes
   → Silent fallback to pytesseract if OCRmyPDF unavailable
4. Barcode decode on ORIGINAL RGB image (not preprocessed) via pylibdmtx
   → Classify barcode type: `mailmark` | `stamp` | `unknown`
   → Parse fields if type is known
5. Postcode extraction from OCR text
   → `ADDRESS_PARSER=regex` (default): regex pattern
   → `ADDRESS_PARSER=libpostal`: ML-based parser, sets `address_components`
   → Fallback to barcode data if postcode not found in OCR text
6. Client fuzzy match (rapidfuzz, score cutoff 70, only if `client_list` provided)

Do NOT reorder these steps.
Do NOT skip preprocessing — it directly affects OCR accuracy.
Barcode decode MUST run on the original RGB image, not the binarised version.
OCRmyPDF fallback to pytesseract MUST be silent — never raise an exception.

## Tesseract path

Resolved in this priority order:

1. `TESSERACT_CMD` environment variable (Linux/Mac/Docker)
2. Windows default: `C:\Program Files\Tesseract-OCR\tesseract.exe`

Do NOT hardcode any other path.
Do NOT remove the env var override.

---

# DEPENDENCY RULES

## Approved dependencies (in requirements.txt)

```
fastapi
uvicorn[standard]
python-multipart
PyMuPDF
pytesseract
ocrmypdf
git+https://github.com/NaturalHistoryMuseum/pylibdmtx.git
rapidfuzz
opencv-python-headless
numpy
Pillow
celery[redis]
tabulate
setuptools
```

Do NOT add new dependencies without explicit instruction.
Do NOT swap `opencv-python-headless` for `opencv-python` — headless build is required in Docker.
Do NOT use `poppler` or `pdf2image` — PyMuPDF handles PDF rendering without system deps.
Do NOT install pylibdmtx from PyPI — install from GitHub HEAD only (PyPI v0.1.10 has known bugs).

## Python version

Target: Python 3.12+
Use `str | None` union syntax, not `Optional[str]`.
Use built-in `list[str]` generics, not `List[str]`.

---

# DOCKER RULES

## Dockerfile requirements

- Base image: `python:3.12-slim`
- System packages (all required — do not remove any):
  - `tesseract-ocr` — OCR engine
  - `libdmtx0b` — Data Matrix barcode decode (pylibdmtx)
  - `ghostscript` — OCRmyPDF dependency
  - `pngquant` — OCRmyPDF dependency
  - `unpaper` — OCRmyPDF deskew
  - `libgl1` — OpenCV
  - `libglib2.0-0` — OpenCV
  - `curl` — Docker HEALTHCHECK
  - `git` — pip install pylibdmtx from GitHub HEAD
- `TESSERACT_CMD=/usr/bin/tesseract` set as ENV
- `HEALTHCHECK` defined in Dockerfile (not only in compose)
- Single stage build unless image size requires multi-stage

## Compose files

| File | Purpose |
|------|---------|
| `docker-compose.yml` | Local dev — includes `app/` volume mount for live reload |
| `docker-compose.coolify.yml` | Coolify deploy — NO volume mount, PORT from env |

Both MUST include three services: `mailscan`, `worker`, `redis`.
The service name `mailscan` MUST NOT change.
Port MUST default to `8000`.

Do NOT:
- remove the `worker` or `redis` services
- add the `app/` volume mount to `docker-compose.coolify.yml`
- change the Redis image from `redis:7-alpine`

---

# ENVIRONMENT VARIABLE RULES

| Variable | Required | Description |
|----------|----------|-------------|
| `MAILSCAN_API_KEY` | Yes | Shared secret for X-API-Key auth |
| `TESSERACT_CMD` | Linux/Mac | Path to tesseract binary (set in Dockerfile) |
| `PORT` | No | HTTP port, default 8000 |
| `REDIS_URL` | No | Redis connection string. Enables async queue. Without it, `/process` runs synchronously. |
| `ADDRESS_PARSER` | No | `regex` (default) or `libpostal`. Controls postcode extraction method. |

Do NOT read config from files, flags, or any source other than environment variables.
Do NOT add new env vars without updating `.env.example` and `README.md`.

---

# TESTING RULES

Tests live in `tests/` only. No test files in `app/`.
Tests run without Redis — `/process` uses sync fallback in test environment.
Set `os.environ.pop("REDIS_URL", None)` at the top of `test_api.py` to ensure this.

## test_pipeline.py

Tests `pipeline.py` functions directly — no HTTP client, no FastAPI.
MUST cover:
- response shape (all fields including `barcode_type`, `barcode_fields`, `address_components`)
- postcode extraction (found + not found)
- client fuzzy match (found + not found)
- multipage PDF
- `barcode_type=unknown` when no barcode present
- `address_components=null` when `ADDRESS_PARSER=regex`

## test_api.py

Tests via `fastapi.testclient.TestClient` — no running server required.
MUST cover:
- `/health` with no auth → 200
- `/process` with missing key → 401
- `/process` with wrong key → 401
- `/process` with non-PDF → 400
- `/process` with empty file → 400
- `/process` with dpi out of range → 400
- `/process` with valid PDF → sync fallback result with `status=complete`
- `/process` result contains new fields (`barcode_type`, `barcode_fields`, `address_components`)
- `/process/sync` returns result shape directly (no job wrapper)
- `/jobs/{id}` returns 404 when `REDIS_URL` not set

Do NOT use `pytest-asyncio` — `TestClient` is synchronous and sufficient.
Do NOT mock `pipeline.process_pdf` in integration tests — test the real pipeline.

---

# FILE MODIFICATION RULES

Modify ONLY files directly related to the task.

Do NOT:
- reformat unrelated files
- reorder imports in files not being changed
- rename files or variables without explicit instruction
- modify `PLAN.md` task status unless completing a stage

---

# GIT RULES

- Always work on `dev` branch
- Never push to `main`
- Waseem handles all merges from `dev` → `main`
- Commit one logical change at a time
- Use conventional commit prefix: `feat:`, `fix:`, `test:`, `docs:`, `chore:`

---

# WHEN UNSURE

If uncertain:
- preserve the existing implementation
- choose the smallest possible change
- ask for clarification
- do not add abstractions speculatively

---

# SUCCESS CRITERIA

A task is ONLY complete when:

- `pipeline.py` contains no HTTP or Celery imports
- `main.py` contains no processing logic
- `worker.py` contains no HTTP routing
- API response shape is unchanged (new fields are additive only)
- All tests in `tests/` pass
- Docker build succeeds (all three services: mailscan, worker, redis)
- `PLAN.md` stage is marked complete
- Changes are committed to `dev`
