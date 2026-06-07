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

### Stage 5 — Coolify deployment ✅
- [x] Push to dev, deploy via Coolify
- [x] Set env vars in Coolify
- [x] Smoke test live endpoint — 9/9 tests passed (see `TESTS.md`)
- [ ] Wire n8n HTTP Request node to live URL

---

### Stage 6 — pylibdmtx from GitHub HEAD ✅
Fixes known bugs in the PyPI release (v0.1.10, March 2022 — behind HEAD).

- [ ] Update `requirements.txt` — install from GitHub source
- [ ] Add `git` to Dockerfile apt packages (needed for pip git installs)
- [ ] Redeploy and verify `/health` still returns ok

### Stage 7 — OCRmyPDF hOCR integration ✅
Replace raw Tesseract call with OCRmyPDF to get word-level bounding boxes.
Enables address block localisation instead of full-page text search.
No API contract change — same response shape, better accuracy.

- [ ] Add `ocrmypdf` to `requirements.txt`
- [ ] Add `ghostscript` to Dockerfile apt packages
- [ ] Rewrite `_ocr()` in `pipeline.py` to use OCRmyPDF API mode + parse hOCR XML
- [ ] Update `_extract_postcode()` to search address-region words first, full page as fallback
- [ ] Update tests — verify postcode extraction still passes
- [ ] Redeploy and smoke test

### Stage 8 — libpostal noise-tolerant address parsing ✅
Replaces postcode regex with an ML-based address parser trained on 1 billion addresses.
Handles OCR noise: `LUT 1AA` → `LU1 1AA`, `LU11AA` (no space), `L U1 1AA` (extra space).
Opt-in via `ADDRESS_PARSER=libpostal` env var — default stays regex (lightweight).

- [ ] Add libpostal compile steps to Dockerfile (behind `ARG ENABLE_LIBPOSTAL=false` build arg)
- [ ] Add `postal` to `requirements.txt` (conditional install)
- [ ] Add `_extract_postcode_libpostal()` function to `pipeline.py`
- [ ] Route `_extract_postcode()` based on `ADDRESS_PARSER` env var
- [ ] Add `address_components` field to page result when libpostal is active (road, city, postcode)
- [ ] Add `.env.example` entry for `ADDRESS_PARSER`
- [ ] Update tests to cover both parser paths
- [ ] Redeploy with `ADDRESS_PARSER=libpostal` and smoke test

### Stage 9 — Async job queue (Celery + Redis) ✅
Prevents HTTP timeouts on large PDFs or high volume. Breaking API change — do before Phase 2.
`POST /process` → `{"job_id": "..."}` → `GET /jobs/{id}` → result when complete.

- [ ] Add `celery[redis]` to `requirements.txt`
- [ ] Add `app/worker.py` — Celery app + `process_pdf_task`
- [ ] Update `main.py` — `POST /process` submits task, returns job_id
- [ ] Add `GET /jobs/{job_id}` endpoint — returns status (`pending`|`processing`|`complete`|`error`) + result
- [ ] Add Redis service to `docker-compose.yml` and `docker-compose.coolify.yml`
- [ ] Add `REDIS_URL` to `.env.example` and `AGENTS.md` env var table
- [ ] Update `TESTS.md` — new test cases for async flow
- [ ] Update `README.md` — new API contract section
- [ ] Redeploy and smoke test full async flow

### Stage 10 — Consumer stamp barcode parser ⬜
Post-2022 Royal Mail consumer stamps use a different 2D barcode format to Mailmark.
Currently those decode as raw bytes with no field parsing.
Gate: requires a real Royal Mail stamped envelope scan to test against.

- [ ] Add `barcode_type` field to page result (`mailmark` | `stamp` | `unknown`)
- [ ] Add `barcode_fields` object to page result (structured fields when format is known)
- [ ] Add `_parse_mailmark()` function — parse Mailmark business mail barcode fields
- [ ] Add `_parse_stamp_barcode()` function — parse post-2022 consumer stamp fields
- [ ] Auto-detect format from decoded bytes and route to correct parser
- [ ] Update tests with known barcode byte sequences
- [ ] Document field layouts in `RESEARCH.md`

---

---

# Phase 2 — Mailscan Dashboard

## What We're Building

A standalone web application that sits in front of the Phase 1 microservice.
Users log in, upload scans via a browser UI, view results, manage their API keys,
and connect Mailscan to external systems (n8n, Zapier, custom webhooks).

The Phase 1 microservice is unchanged — Phase 2 is a product layer on top of it.

---

## Phase 2 Architecture

```
Browser (Next.js dashboard)
        │
        │  Supabase Auth (login / session)
        │
        ├─── Upload UI  ──────────────────────────────────────────┐
        │                                                          │
        │  POST /api/process (Next.js route)                       │
        │  ↓ forwards to mailscan service                          │
        │  ↓ stores results in Supabase                            │
        │                                                          │
        ├─── Results UI (scan history, per-page detail)            │
        │                                                          │
        ├─── API Keys UI (create / revoke / copy)                  │
        │                                                          │
        └─── Webhooks UI (configure outbound notifications)        │
                                                                   │
Supabase (Postgres + Auth + Storage)                               │
  ├── users / orgs                                                  │
  ├── api_keys                                                      │
  ├── scans (job + result store)                                    │
  └── webhooks                                                      │
                                                                   │
Phase 1 microservice (unchanged)  ◄────────────────────────────────┘
```

---

## Phase 2 Tech Stack

| Component | Choice | Reason |
|-----------|--------|--------|
| Framework | Next.js 15 (App Router) | Same stack as luton-eng-dashboard — reuse patterns |
| Auth | Supabase Auth | Email/password + magic link; multi-tenant via org model |
| Database | Supabase (Postgres) | Scan history, API keys, webhooks, users |
| File storage | Supabase Storage | Original PDF uploads retained per scan |
| UI | Tailwind CSS + shadcn/ui | Rapid build, consistent with existing projects |
| Deployment | Coolify (Docker) | Same infra as Phase 1 |

---

## Data Model

### `orgs`
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| name | text | Organisation name |
| slug | text | URL-safe identifier |
| created_at | timestamptz | |

### `org_members`
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| org_id | uuid FK → orgs | |
| user_id | uuid FK → auth.users | |
| role | text | `owner` \| `admin` \| `member` |
| created_at | timestamptz | |

### `api_keys`
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| org_id | uuid FK → orgs | |
| name | text | Human label e.g. "n8n production" |
| key_hash | text | SHA-256 of the key — never store plaintext |
| key_prefix | text | First 8 chars for display e.g. `ms_abc123` |
| created_by | uuid FK → auth.users | |
| last_used_at | timestamptz | Updated on each successful request |
| revoked_at | timestamptz | Null = active |
| created_at | timestamptz | |

> Keys are shown in full exactly once (on creation). After that only the prefix is shown.

### `scans`
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| org_id | uuid FK → orgs | |
| created_by | uuid FK → auth.users \| null | Null = API call |
| api_key_id | uuid FK → api_keys \| null | Set when submitted via API key |
| filename | text | Original filename |
| storage_path | text | Supabase Storage path to the PDF |
| page_count | int | |
| status | text | `pending` \| `processing` \| `complete` \| `error` |
| error_message | text \| null | Set on failure |
| result | jsonb | Full `/process` response stored here |
| clients_used | text[] | Client list passed with this scan |
| created_at | timestamptz | |
| completed_at | timestamptz \| null | |

### `webhooks`
| Column | Type | Notes |
|--------|------|-------|
| id | uuid PK | |
| org_id | uuid FK → orgs | |
| name | text | Human label |
| url | text | Target endpoint |
| secret | text | Signing secret (HMAC-SHA256) — stored encrypted |
| events | text[] | e.g. `["scan.complete", "scan.error"]` |
| enabled | bool | |
| created_at | timestamptz | |

---

## Phase 2 Routes

### Dashboard UI
| Route | Description |
|-------|-------------|
| `/login` | Supabase Auth login (email + password, magic link) |
| `/` | Redirect → `/scans` |
| `/scans` | Scan history — table with status, filename, date, postcode summary |
| `/scans/[id]` | Scan detail — per-page results, OCR text, matched client, postcode |
| `/upload` | Upload form — drag and drop PDF, optional client list |
| `/api-keys` | List active keys, create new, revoke |
| `/webhooks` | List webhooks, create, edit, delete |
| `/settings` | Org name, members, billing (future) |

### API Routes (Next.js)
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | `/api/process` | API key or session | Upload PDF → mailscan service → store result |
| GET | `/api/scans` | Session | List scans for org |
| GET | `/api/scans/[id]` | Session | Get single scan result |
| POST | `/api/api-keys` | Session | Create new API key |
| DELETE | `/api/api-keys/[id]` | Session | Revoke API key |
| GET | `/api/api-keys` | Session | List keys (prefix + metadata only) |
| POST | `/api/webhooks` | Session | Create webhook |
| PUT | `/api/webhooks/[id]` | Session | Update webhook |
| DELETE | `/api/webhooks/[id]` | Session | Delete webhook |

---

## API Key System

Keys are prefixed `ms_` and generated with 32 bytes of random data:

```
ms_<base62(32 random bytes)>
```

Flow:
1. User clicks "Create API Key", gives it a name
2. Server generates key, stores SHA-256 hash + prefix in `api_keys`
3. Full key shown once in UI — user copies it
4. On subsequent API calls: key is hashed, looked up in `api_keys`, org resolved
5. `last_used_at` updated on every successful call
6. Revoke = set `revoked_at` timestamp

---

## Webhook System

On `scan.complete` or `scan.error`, the dashboard fires an outbound HTTP POST
to each enabled webhook URL configured for the org.

Payload:
```json
{
  "event": "scan.complete",
  "scan_id": "uuid",
  "filename": "letters.pdf",
  "page_count": 3,
  "pages": [ ... ],
  "created_at": "2026-06-07T12:00:00Z"
}
```

Each request includes `X-Mailscan-Signature: sha256=<hmac>` for verification.
Delivery is fire-and-forget (background job) — failures are logged, not retried in Phase 2.

---

## Phase 2 Build Stages

### Stage 2.1 — Supabase schema + auth ⬜
- [ ] Create Supabase project (or reuse existing for the org)
- [ ] Write migrations: `orgs`, `org_members`, `api_keys`, `scans`, `webhooks`
- [ ] RLS policies: org isolation — users only see their own org's data
- [ ] Configure Supabase Auth: email/password + magic link
- [ ] `.env.example` updated with Supabase vars

### Stage 2.2 — Next.js app scaffold ⬜
- [ ] New repo or subdirectory: `mailscan-dashboard/`
- [ ] Next.js 15 App Router + TypeScript + Tailwind + shadcn/ui
- [ ] Supabase SSR client (`@supabase/ssr`)
- [ ] Auth middleware — redirect unauthenticated users to `/login`
- [ ] Login page (`/login`) — email/password + magic link
- [ ] Basic layout: sidebar nav + header
- [ ] Deploy skeleton to Coolify (`https://mailscan.adaplo.io` or equivalent)

### Stage 2.3 — Upload + process flow ⬜
- [ ] `/upload` page — drag-and-drop PDF, optional client list input
- [ ] `POST /api/process` route — accept file, forward to Phase 1 service, store result in `scans`
- [ ] Upload to Supabase Storage (original PDF retained)
- [ ] Status polling or optimistic redirect to scan detail on completion
- [ ] `/scans/[id]` — per-page results table: page number, postcode, barcode, matched client, OCR text (expandable)

### Stage 2.4 — Scan history ⬜
- [ ] `/scans` — paginated table: filename, date, page count, status, postcode summary
- [ ] Filter by date range, status
- [ ] Link to scan detail
- [ ] Export results as CSV

### Stage 2.5 — API key management ⬜
- [ ] `/api-keys` page — list active keys (prefix, name, created, last used)
- [ ] Create key flow — name input → generate → show full key once → copy button
- [ ] Revoke key — confirmation dialog → sets `revoked_at`
- [ ] `POST /api/api-keys` and `DELETE /api/api-keys/[id]` routes
- [ ] API key auth middleware for `POST /api/process` — allows machine-to-machine calls

### Stage 2.6 — Webhook management ⬜
- [ ] `/webhooks` page — list configured webhooks
- [ ] Create/edit webhook — URL, name, events, enable/disable
- [ ] Webhook delivery on `scan.complete` / `scan.error` (background, fire-and-forget)
- [ ] HMAC-SHA256 signing of outbound payloads
- [ ] Delivery log per webhook (last N attempts, status code)

### Stage 2.7 — Multi-org + settings ⬜
- [ ] `/settings` — org name, member list, invite by email
- [ ] Invite flow — email → Supabase invite → lands in org on first login
- [ ] Role enforcement: `owner` can manage members + billing, `member` can upload only
- [ ] Org switcher in header (for users in multiple orgs)

### Stage 2.8 — Hardening + deployment ⬜
- [ ] Rate limiting on `/api/process` per API key (e.g. 100 req/day free tier)
- [ ] Input validation on all routes
- [ ] Error states and empty states on all pages
- [ ] Coolify production deployment with env vars
- [ ] Smoke test: login → upload → view result → copy API key → call via curl → webhook fires

---

## Phase 2 Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `NEXT_PUBLIC_SUPABASE_URL` | Yes | Supabase project URL |
| `NEXT_PUBLIC_SUPABASE_ANON_KEY` | Yes | Supabase anon key |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Server-only — for admin operations |
| `MAILSCAN_SERVICE_URL` | Yes | Phase 1 service URL e.g. `http://mailscan:8000` |
| `MAILSCAN_API_KEY` | Yes | Key to authenticate with Phase 1 service |
| `NEXT_PUBLIC_SITE_URL` | Yes | Canonical URL for auth redirects |
| `WEBHOOK_SIGNING_SECRET` | Yes | Master secret for HMAC webhook signatures |

---

## Key Rules (Phase 2)
- Phase 1 microservice is NEVER modified as part of Phase 2 work
- All database access goes through `lib/db/` — never raw SQL in route handlers
- API keys are NEVER stored in plaintext — SHA-256 hash only
- Org isolation is enforced at the RLS layer in Supabase — not just in application code
- Always work on `dev` branch — Waseem merges to `main`
