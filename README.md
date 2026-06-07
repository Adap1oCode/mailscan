# Mailscan

Python microservice — accepts a scanned PDF, returns structured OCR + barcode + client-match results as JSON.

## Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| GET | `/health` | None | Liveness check |
| POST | `/process` | `X-API-Key` | Process a PDF scan |

Interactive docs available at `/docs` when running.

## Quick Start (Docker)

```bash
cp .env.example .env
# Set MAILSCAN_API_KEY in .env

docker compose up --build
```

Service starts at `http://localhost:8000`.

## Smoke Test

```bash
curl -X POST http://localhost:8000/process \
  -H "X-API-Key: your-api-key" \
  -F "file=@scan.pdf" \
  -F "clients=Acme Ltd,Beta Corp"
```

## Request Parameters

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `file` | PDF | Yes | Scanned letter(s) |
| `clients` | string | No | Comma-separated client names for fuzzy match |
| `dpi` | int | No | Render DPI — default 300, range 72–600 |

## Response

```json
{
  "page_count": 1,
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

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `MAILSCAN_API_KEY` | Yes | Shared secret for `X-API-Key` header |
| `TESSERACT_CMD` | Linux/Mac | Path to tesseract binary — set automatically in Docker |
| `PORT` | No | HTTP port, default `8000` |

## Running Tests

```bash
pip install -r requirements.txt
pytest tests/
```

## Branch Rules

- Always work on `dev` — never push to `main`
- See `PLAN.md` for full build stages
