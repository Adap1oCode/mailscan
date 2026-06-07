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
3. Returns structured JSON results

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
│   └── pipeline.py     ← Processing logic ONLY — no HTTP code here
├── tests/
│   ├── __init__.py
│   ├── test_pipeline.py
│   └── test_api.py
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
├── AGENTS.md           ← this file
├── PLAN.md
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

`pipeline.py` contains ONLY processing logic:
- PDF → images
- Image preprocessing (deskew, binarise)
- OCR via pytesseract
- Barcode decode via pylibdmtx
- Postcode extraction
- Client fuzzy matching via rapidfuzz

`main.py` contains ONLY HTTP concerns:
- FastAPI app definition
- Route handlers
- Request validation
- API key auth
- Error → HTTP status mapping

These two files MUST remain independently testable.
`pipeline.py` MUST be importable with no FastAPI dependency.
`main.py` MUST contain no image processing, OCR, or regex logic.

## Do NOT

- mix HTTP and processing logic in any single function
- import `fastapi` anywhere in `pipeline.py`
- import `cv2`, `fitz`, `pytesseract`, or `pylibdmtx` in `main.py`
- add a database layer without explicit instruction
- add a queue or async job system without explicit instruction
- add file storage (S3, Supabase) without explicit instruction

---

# API RULES

## Endpoints

The service exposes exactly two endpoints:

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/health` | None | Liveness check |
| POST | `/process` | X-API-Key | Process a PDF scan |

Do NOT add endpoints without explicit instruction.

## Authentication

All `/process` calls MUST be authenticated via `X-API-Key` header.
The key is read from the `MAILSCAN_API_KEY` environment variable.
`/health` is always open — no auth.

Do NOT:
- add session-based auth
- add OAuth
- add JWT
- bypass the API key check

## Response shape

The `/process` response shape is the contract. Do NOT change field names or types:

```json
{
  "page_count": int,
  "pages": [
    {
      "page": int,
      "ocr_text": string,
      "postcode": string | null,
      "barcode": string | null,
      "matched_client": string | null,
      "match_score": float | null
    }
  ]
}
```

If new fields are needed, ADD them — never rename or remove existing fields.

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
2. Preprocess each image (deskew → binarise)
3. OCR (Tesseract, `--psm 6`)
4. Barcode decode (pylibdmtx — attempt on original image, not preprocessed)
5. Postcode extraction (from OCR text, fallback to barcode data)
6. Client fuzzy match (rapidfuzz, score cutoff 70, only if `client_list` provided)

Do NOT reorder these steps.
Do NOT skip preprocessing — it directly affects OCR accuracy.
Barcode decode MUST run on the original RGB image, not the binarised version.

## Tesseract path

The Tesseract binary path is resolved in this priority order:

1. `TESSERACT_CMD` environment variable (Linux/Mac/Docker)
2. Windows default: `C:\Program Files\Tesseract-OCR\tesseract.exe`

Do NOT hardcode any other path.
Do NOT remove the env var override.

---

# DEPENDENCY RULES

## Approved dependencies (already in requirements.txt)

```
fastapi
uvicorn[standard]
python-multipart
PyMuPDF
pytesseract
pylibdmtx
rapidfuzz
opencv-python-headless
numpy
Pillow
tabulate
setuptools
```

Do NOT add new dependencies without explicit instruction.
Do NOT swap `opencv-python-headless` for `opencv-python` — the headless build is required in Docker.
Do NOT use `poppler` or `pdf2image` — PyMuPDF handles PDF rendering without system deps.

## Python version

Target: Python 3.12+
Use `str | None` union syntax, not `Optional[str]`.
Use built-in `list[str]` generics, not `List[str]`.

---

# DOCKER RULES

## Dockerfile requirements

- Base image: `python:3.12-slim`
- System packages: `tesseract-ocr`, `libdmtx0b`, `libgl1`, `libglib2.0-0`
- No other system packages unless explicitly required
- `TESSERACT_CMD=/usr/bin/tesseract` set as ENV
- Single stage build (no multi-stage unless image size becomes a problem)

## docker-compose.yml

The service name MUST remain `mailscan`.
Port MUST default to `8000`.
The `app/` volume mount is for local dev only — remove in production Coolify stack.

Do NOT:
- introduce additional services (Redis, Postgres, etc.) without instruction
- change the service name
- change the default port

---

# ENVIRONMENT VARIABLE RULES

Defined variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `MAILSCAN_API_KEY` | Yes | Shared secret for X-API-Key auth |
| `TESSERACT_CMD` | Linux/Mac | Path to tesseract binary |
| `PORT` | No | HTTP port, default 8000 |

Do NOT read config from files, flags, or any source other than environment variables.
Do NOT add new env vars without updating `.env.example` and `README.md`.

---

# TESTING RULES

Tests live in `tests/` only. No test files in `app/`.

## test_pipeline.py

Tests `pipeline.py` functions directly — no HTTP client, no FastAPI.
MUST cover:
- response shape
- postcode extraction (found + not found)
- client fuzzy match (found + not found)
- multipage PDF

## test_api.py

Tests via `fastapi.testclient.TestClient` — no running server required.
MUST cover:
- `/health` with no auth
- `/process` with missing key → 401
- `/process` with wrong key → 401
- `/process` with non-PDF → 400
- `/process` with empty file → 400
- `/process` with valid PDF → 200

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

- `pipeline.py` contains no HTTP imports
- `main.py` contains no processing logic
- API response shape is unchanged
- All tests in `tests/` pass
- Docker build succeeds
- `PLAN.md` stage is marked complete
- Changes are committed to `dev`
