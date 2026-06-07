# Live Service Test Results

---

## Test Run 2 — Post stages 6–9 deployment

**Service:** https://mailscan.adaplo.io
**Tested:** 2026-06-07
**Branch:** dev
**Build includes:** pylibdmtx from GitHub HEAD, OCRmyPDF hOCR, libpostal opt-in, Celery + Redis async queue
**Tested by:** Claude Code (automated curl against live Coolify deployment)

### Test PDF

Single-page PDF with address block and body text:
```
Acme Industries Ltd
14 High Street
Luton LU1 1AA
Dear Sir/Madam Please find enclosed your invoice.
```

### Results

| # | Test | Expected | Result |
|---|------|----------|--------|
| 1 | `GET /health` | `{"status":"ok"}` | ✅ |
| 2 | `POST /process` — no API key | 401 | ✅ |
| 3 | `POST /process` — wrong API key | 401 | ✅ |
| 4 | `POST /process` — PNG file | 400 `Only PDF files are accepted` | ✅ |
| 5 | `POST /process` — empty file | 400 `Uploaded file is empty` | ✅ |
| 6 | `POST /process` — `dpi=9999` | 400 `dpi must be between 72 and 600` | ✅ |
| 7 | `POST /process` — async submit | `job_id` + `status: pending` | ✅ |
| 8 | `GET /jobs/{id}` — poll result | `processing` → `complete` with result | ✅ |
| 9 | `POST /process/sync` — with client list | `matched_client: Acme Industries Ltd`, score `90.0` | ✅ |

**9/9 tests passed.**

### Test 7 — Async submit response
```json
{
    "job_id": "3bb9a360-2ee3-45d4-9ae6-c3ed7cf325eb",
    "status": "pending"
}
```

### Test 8 — Poll job result (after ~13s)
```json
{
    "job_id": "3bb9a360-2ee3-45d4-9ae6-c3ed7cf325eb",
    "status": "complete",
    "result": {
        "page_count": 1,
        "pages": [
            {
                "page": 1,
                "ocr_text": "Acme Industries Ltd 14 High Street Luton LU1 1AA Dear Sir/Madam Please find enclosed your invoice.",
                "postcode": "LU1 1AA",
                "address_components": null,
                "barcode": null,
                "barcode_type": "unknown",
                "barcode_fields": null,
                "matched_client": null,
                "match_score": null
            }
        ]
    }
}
```

### Test 9 — /process/sync with client list
```json
{
    "page_count": 1,
    "pages": [
        {
            "page": 1,
            "ocr_text": "Acme Industries Ltd 14 High Street Luton LU1 1AA Dear Sir/Madam Please find enclosed your invoice.",
            "postcode": "LU1 1AA",
            "address_components": null,
            "barcode": null,
            "barcode_type": "unknown",
            "barcode_fields": null,
            "matched_client": "Acme Industries Ltd",
            "match_score": 90.0
        }
    ]
}
```

### Observations

- Celery worker and Redis both confirmed operational — job moved from `pending` → `processing` → `complete`
- OCR text accurate, postcode `LU1 1AA` correctly extracted
- `address_components: null` as expected — `ADDRESS_PARSER=regex` (default)
- `barcode_type: unknown`, `barcode_fields: null` as expected — no Data Matrix in test PDF
- `matched_client: Acme Industries Ltd` with score `90.0` — fuzzy match working correctly
- `/process/sync` returns result directly (no job wrapper) — confirmed correct for n8n use

### Known Gaps

| Gap | Detail | Recommended fix |
|-----|--------|-----------------|
| Barcode path untested | No Mailmark Data Matrix in test PDF — `_decode_barcode`, `_parse_mailmark`, `_parse_stamp_barcode` untested live | Test with a real scanned Royal Mail envelope |
| Multipage PDF untested | Only single-page PDF used | Test with a multi-page scan |
| libpostal path untested | `ADDRESS_PARSER=regex` in production — libpostal branch never exercised | Enable `ADDRESS_PARSER=libpostal` once libpostal compiled into Docker image |
| Async timing | Job took ~13s to complete — acceptable for single page, monitor under load | Load test with multi-page PDFs at volume |

---

## Test Run 1 — Initial deployment (pre stages 6–9)

**Service:** https://mailscan.adaplo.io
**Tested:** 2026-06-07
**Branch:** dev
**Tested by:** Claude Code (automated curl against live Coolify deployment)

### Test PDF

Single-page PDF with address block:
```
Acme Industries Ltd
14 High Street
Luton LU1 1AA
Dear Sir/Madam Please find enclosed your invoice.
```

### Results

| # | Test | Expected | Result |
|---|------|----------|--------|
| 1 | `GET /health` | `{"status":"ok"}` | ✅ |
| 2 | `POST /process` — no API key | 401 | ✅ |
| 3 | `POST /process` — wrong API key | 401 | ✅ |
| 4 | `POST /process` — PNG file | 400 `Only PDF files are accepted` | ✅ |
| 5 | `POST /process` — empty file | 400 `Uploaded file is empty` | ✅ |
| 6 | `POST /process` — valid PDF, no clients | 200, postcode `LU1 1AA` extracted | ✅ |
| 7 | `POST /process` — valid PDF + client list | `matched_client: Acme Industries Ltd`, score `90.0` | ✅ |
| 8 | `POST /process` — `dpi=9999` | 400 `dpi must be between 72 and 600` | ✅ |
| 9 | `POST /process` — `dpi=72` (degraded OCR) | 200, OCR degraded — postcode not extracted | ✅ |

**9/9 tests passed.**

### Notable finding — Test 9 (dpi=72)

At 72 DPI Tesseract read `LU1` as `LUT` — postcode regex failed to match.
This is the exact failure mode `libpostal` addresses (see `RESEARCH.md`).
Confirmed that **300 DPI is the correct default** — do not lower without good reason.

---

## Re-running the Full Test Suite

```bash
KEY="<your-api-key>"

# Health
curl -s https://mailscan.adaplo.io/health

# Auth
curl -s -o /dev/null -w "%{http_code}" -X POST https://mailscan.adaplo.io/process
curl -s -o /dev/null -w "%{http_code}" -X POST https://mailscan.adaplo.io/process -H "X-API-Key: wrongkey"

# Validation
curl -s -X POST https://mailscan.adaplo.io/process -H "X-API-Key: $KEY" -F "file=@/etc/hostname;type=image/png"
curl -s -X POST https://mailscan.adaplo.io/process -H "X-API-Key: $KEY" -F "file=@/dev/null;filename=empty.pdf;type=application/pdf"
curl -s -X POST https://mailscan.adaplo.io/process -H "X-API-Key: $KEY" -F "file=@scan.pdf;type=application/pdf" -F "dpi=9999"

# Async flow
JOB=$(curl -s -X POST https://mailscan.adaplo.io/process \
  -H "X-API-Key: $KEY" \
  -F "file=@scan.pdf;type=application/pdf" | python3 -c "import sys,json; print(json.load(sys.stdin)['job_id'])")
sleep 15
curl -s https://mailscan.adaplo.io/jobs/$JOB -H "X-API-Key: $KEY"

# Sync with client matching
curl -s -X POST https://mailscan.adaplo.io/process/sync \
  -H "X-API-Key: $KEY" \
  -F "file=@scan.pdf;type=application/pdf" \
  -F "clients=Acme Ltd,Beta Corp"
```
