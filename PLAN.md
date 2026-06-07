# Mailscan — Build Plan

## What We're Building

A self-contained Python microservice that accepts a scanned PDF, processes it through
an OCR + barcode pipeline, and returns structured JSON results.

Consumed by:
- **luton-eng-dashboard** via `POST /api/process/mailscan` (Next.js proxy route, API key auth)
- **n8n** directly via HTTP Request node
- Any other system via HTTP — fully agnostic

---

## Architecture

```
Caller (dashboard / n8n / CLI)
        │
        │  POST /process
        │  multipart: file=<pdf>, clients=<csv>
        │  Header: X-API-Key: <key>
        ▼
┌─────────────────────────┐
│  FastAPI microservice   │  /opt/projects/mailscan
│  app/main.py            │
└────────────┬────────────┘
             │ calls
             ▼
┌─────────────────────────┐
│  pipeline.py            │
│  PDF → images           │
│  preprocess (deskew,    │
│    binarise)            │
│  OCR (Tesseract)        │
│  Barcode (Mailmark)     │
│  Postcode extract       │
│  Client fuzzy match     │
└─────────────────────────┘
             │
             ▼
{
  "page_count": N,
  "pages": [
    {
      "page": 1,
      "ocr_text": "...",
      "postcode": "LU1 1AA",
      "barcode": "...",
      "matched_client": "Acme Ltd",
      "match_score": 94.2
    }
  ]
}
```

---

## Tech Stack

| Component | Choice | Reason |
|-----------|--------|--------|
| HTTP framework | FastAPI | Async, typed, auto-docs at `/docs` |
| PDF → image | PyMuPDF (`fitz`) | No poppler dependency |
| OCR | Tesseract + pytesseract | Free, accurate, battle-tested |
| Barcode | pylibdmtx | Royal Mail Mailmark Data Matrix |
| Image processing | OpenCV headless + numpy | Deskew + binarise |
| Fuzzy matching | rapidfuzz | Closed-set client name matching |
| Container | Docker (python:3.12-slim) | Deployed via Coolify |
| Auth | `X-API-Key` header | Simple, works with n8n + dashboard |

---

## Project Structure

```
mailscan/
├── app/
│   ├── __init__.py
│   ├── main.py          ← FastAPI app, /health + /process endpoints
│   └── pipeline.py      ← PDF processing logic (no HTTP here)
├── tests/
│   ├── test_pipeline.py ← unit tests for pipeline functions
│   └── test_api.py      ← integration tests for HTTP endpoints
├── Dockerfile
├── docker-compose.yml   ← local dev + Coolify stack
├── requirements.txt
├── .env.example
├── PLAN.md              ← this file
└── README.md
```

---

## API Contract

### `GET /health`
No auth required.
```json
{ "status": "ok" }
```

### `POST /process`
**Auth:** `X-API-Key: <MAILSCAN_API_KEY>` header required.

**Request:** `multipart/form-data`
| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | PDF file | Yes | Scanned letter(s) — one or more pages |
| `clients` | string | No | Comma-separated client names for fuzzy match |

**Response 200:**
```json
{
  "page_count": 2,
  "pages": [
    {
      "page": 1,
      "ocr_text": "Mr John Smith\n14 High Street\nLuton LU1 1AA",
      "postcode": "LU1 1AA",
      "barcode": "JC123456GB",
      "matched_client": "John Smith",
      "match_score": 91.5
    }
  ]
}
```

**Error responses:**
| Status | Reason |
|--------|--------|
| 400 | Not a PDF / missing file |
| 401 | Missing or wrong API key |
| 500 | Processing error (detail included) |

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MAILSCAN_API_KEY` | Yes | Shared secret for `X-API-Key` auth |
| `TESSERACT_CMD` | Linux/Mac only | Path to tesseract binary (e.g. `/usr/bin/tesseract`) |
| `PORT` | No | HTTP port, default `8000` |

---

## Deployment — Coolify

Deployed as a Docker container alongside the dashboard.

**Build command:** `docker build -t mailscan .`
**Start command:** `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}`

Coolify env vars to set:
- `MAILSCAN_API_KEY` — generate a strong random string
- `TESSERACT_CMD=/usr/bin/tesseract` — set in Dockerfile, override if needed
- `PORT=8000`

Service will be reachable internally at `http://mailscan:8000` for the dashboard,
and optionally exposed at `https://mailscan.adaplo.io` for n8n.

---

## luton-eng-dashboard Integration

A new route in the dashboard acts as an authenticated proxy:

```
src/app/api/process/mailscan/route.ts
```

- Accepts the file upload from the browser (or n8n)
- Verifies `X-API-Key` header against `MAILSCAN_API_KEY` env var
- Forwards to `http://mailscan:8000/process`
- Returns the JSON result

Dashboard env var to add: `MAILSCAN_SERVICE_URL=http://mailscan:8000`

---

## Build Stages

### Stage 1 — Python service core ✅
- [x] `app/__init__.py`
- [x] `app/pipeline.py` — PDF → images, preprocess, OCR, barcode, postcode extract, fuzzy match
- [x] `app/main.py` — FastAPI app with `/health` and `/process`
- [x] `requirements.txt`
- [x] `.env.example`
- [ ] Manual smoke test: `curl -F file=@sample.pdf http://localhost:8000/process`

### Stage 2 — Docker + local dev ✅
- [x] `Dockerfile` — python:3.12-slim, install tesseract + libdmtx system deps
- [x] `docker-compose.yml` — local dev stack
- [ ] Verify container builds and service starts
- [ ] Smoke test against container

### Stage 3 — Tests ✅
- [x] `tests/test_pipeline.py` — unit test each pipeline function with a sample PDF
- [x] `tests/test_api.py` — HTTP integration tests (valid upload, bad key, non-PDF)

### Stage 4 — Dashboard integration ⬜
- [ ] `src/app/api/process/[processor]/route.ts` in luton-eng-dashboard
- [ ] API key middleware
- [ ] Processor registry (`lib/processors/registry.ts`)
- [ ] End-to-end test: browser upload → dashboard route → mailscan service → results

### Stage 5 — Coolify deployment ⬜
- [ ] Push to dev, deploy via Coolify
- [ ] Set env vars in Coolify
- [ ] Smoke test live endpoint
- [ ] Wire n8n HTTP Request node to live URL

---

## Key Rules
- `pipeline.py` has no HTTP code — pure processing logic, independently testable
- `main.py` has no processing logic — pure HTTP wrapper
- API key checked on every `/process` call — `/health` is open
- Always work on `dev` branch — never push to `main`
