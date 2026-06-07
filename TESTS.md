# Live Service Test Results

**Service:** https://mailscan.adaplo.io
**Tested:** 2026-06-07
**Branch:** dev
**Tested by:** Claude Code (automated curl against live Coolify deployment)

---

## Test Environment

- Service deployed via Coolify, Docker, branch `dev`
- Test PDF generated as a raw minimal PDF (no local Python deps)
- PDF content: single page with name, address, postcode, and body text

**Test PDF content:**
```
Acme Industries Ltd
14 High Street
Luton LU1 1AA
Dear Sir/Madam Please find enclosed your invoice.
```

---

## Results

### TEST 1 — Health check

```bash
curl -s https://mailscan.adaplo.io/health
```

**Expected:** `{"status":"ok"}`
**Result:** `{"status":"ok"}` ✅

---

### TEST 2 — Missing API key → 401

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST https://mailscan.adaplo.io/process
```

**Expected:** `401`
**Result:** `401` ✅

---

### TEST 3 — Wrong API key → 401

```bash
curl -s -o /dev/null -w "%{http_code}" -X POST https://mailscan.adaplo.io/process \
  -H "X-API-Key: wrongkey123"
```

**Expected:** `401`
**Result:** `401` ✅

---

### TEST 4 — Non-PDF file → 400

```bash
curl -s -X POST https://mailscan.adaplo.io/process \
  -H "X-API-Key: <key>" \
  -F "file=@/etc/hostname;type=image/png"
```

**Expected:** `400` with detail message
**Result:** `{"detail":"Only PDF files are accepted"}` ✅

---

### TEST 5 — Empty PDF → 400

```bash
curl -s -X POST https://mailscan.adaplo.io/process \
  -H "X-API-Key: <key>" \
  -F "file=@/dev/null;filename=empty.pdf;type=application/pdf"
```

**Expected:** `400` with detail message
**Result:** `{"detail":"Uploaded file is empty"}` ✅

---

### TEST 6 — Valid PDF, no client list → 200

```bash
curl -s -X POST https://mailscan.adaplo.io/process \
  -H "X-API-Key: <key>" \
  -F "file=@test_scan.pdf;type=application/pdf"
```

**Expected:** 200, postcode extracted, no client match
**Result:** ✅

```json
{
    "page_count": 1,
    "pages": [
        {
            "page": 1,
            "ocr_text": "Acme Industries Ltd\n\n14 High Street\n\nLuton LU1 1AA\n\nDear Sir/Madam Please find enclosed your invoice.",
            "postcode": "LU1 1AA",
            "barcode": null,
            "matched_client": null,
            "match_score": null
        }
    ]
}
```

**Observations:**
- OCR text matches PDF content exactly
- Postcode `LU1 1AA` correctly extracted
- `barcode` null as expected (no Data Matrix in test PDF)
- `matched_client` null as expected (no client list provided)

---

### TEST 7 — Valid PDF with client list → 200, fuzzy match

```bash
curl -s -X POST https://mailscan.adaplo.io/process \
  -H "X-API-Key: <key>" \
  -F "file=@test_scan.pdf;type=application/pdf" \
  -F "clients=Acme Industries Ltd,Beta Corp,Gamma LLC"
```

**Expected:** 200, `matched_client` populated with score
**Result:** ✅

```json
{
    "page_count": 1,
    "pages": [
        {
            "page": 1,
            "ocr_text": "Acme Industries Ltd\n\n14 High Street\n\nLuton LU1 1AA\n\nDear Sir/Madam Please find enclosed your invoice.",
            "postcode": "LU1 1AA",
            "barcode": null,
            "matched_client": "Acme Industries Ltd",
            "match_score": 90.0
        }
    ]
}
```

**Observations:**
- `matched_client` correctly identified `Acme Industries Ltd` from the client list
- `match_score` of `90.0` — high confidence match
- All other fields consistent with Test 6

---

### TEST 8 — DPI out of range → 400

```bash
curl -s -X POST https://mailscan.adaplo.io/process \
  -H "X-API-Key: <key>" \
  -F "file=@test_scan.pdf;type=application/pdf" \
  -F "dpi=9999"
```

**Expected:** `400` with detail message
**Result:** `{"detail":"dpi must be between 72 and 600"}` ✅

---

### TEST 9 — Valid PDF at low DPI (dpi=72) → 200, degraded OCR

```bash
curl -s -X POST https://mailscan.adaplo.io/process \
  -H "X-API-Key: <key>" \
  -F "file=@test_scan.pdf;type=application/pdf" \
  -F "dpi=72"
```

**Expected:** 200, but OCR quality degraded at low resolution
**Result:** ✅ (expected degradation confirmed)

```json
{
    "page_count": 1,
    "pages": [
        {
            "page": 1,
            "ocr_text": "Acme Industries Lid\n\n14 High Street\n\nLuton LUT 1AA\n\nDear Sir/Madam Please find enclosed your invoice.",
            "postcode": null,
            "barcode": null,
            "matched_client": null,
            "match_score": null
        }
    ]
}
```

**Observations:**
- OCR misread `Ltd` as `Lid` and `LU1` as `LUT` at 72 DPI
- Postcode extraction failed — `LUT 1AA` does not match the postcode regex
- Client match failed as a result of the corrupted OCR text
- **Confirms 300 DPI is the correct default** — do not lower without good reason
- This is the exact failure mode `libpostal` would mitigate (see `RESEARCH.md`)

---

## Summary

| # | Test | Status |
|---|------|--------|
| 1 | `GET /health` | ✅ Pass |
| 2 | `POST /process` — no API key | ✅ Pass |
| 3 | `POST /process` — wrong API key | ✅ Pass |
| 4 | `POST /process` — non-PDF file | ✅ Pass |
| 5 | `POST /process` — empty PDF | ✅ Pass |
| 6 | `POST /process` — valid PDF, no clients | ✅ Pass |
| 7 | `POST /process` — valid PDF + client list | ✅ Pass |
| 8 | `POST /process` — dpi out of range | ✅ Pass |
| 9 | `POST /process` — dpi=72 (degraded OCR) | ✅ Pass (expected degradation) |

**9/9 tests passed.**

---

## Known Gaps Identified During Testing

| Gap | Detail | Recommended fix |
|-----|--------|-----------------|
| Low DPI breaks postcode extraction | `LU1` → `LUT` at 72 DPI, regex fails | Add `libpostal` for noise-tolerant address parsing (see `RESEARCH.md`) |
| No barcode tested | No Mailmark Data Matrix in the test PDF — barcode path untested live | Test with a real scanned Royal Mail envelope |
| No multipage PDF tested | Only single-page PDF used | Test with a multi-page scan to confirm page indexing |

---

## Re-running These Tests

```bash
KEY="<your-api-key>"

# Health
curl -s https://mailscan.adaplo.io/health

# Auth tests
curl -s -o /dev/null -w "%{http_code}" -X POST https://mailscan.adaplo.io/process
curl -s -o /dev/null -w "%{http_code}" -X POST https://mailscan.adaplo.io/process -H "X-API-Key: wrongkey"

# Validation tests
curl -s -X POST https://mailscan.adaplo.io/process -H "X-API-Key: $KEY" -F "file=@/etc/hostname;type=image/png"
curl -s -X POST https://mailscan.adaplo.io/process -H "X-API-Key: $KEY" -F "file=@/dev/null;filename=empty.pdf;type=application/pdf"
curl -s -X POST https://mailscan.adaplo.io/process -H "X-API-Key: $KEY" -F "file=@scan.pdf;type=application/pdf" -F "dpi=9999"

# Happy path
curl -s -X POST https://mailscan.adaplo.io/process -H "X-API-Key: $KEY" -F "file=@scan.pdf;type=application/pdf"
curl -s -X POST https://mailscan.adaplo.io/process -H "X-API-Key: $KEY" -F "file=@scan.pdf;type=application/pdf" -F "clients=Acme Ltd,Beta Corp"
curl -s -X POST https://mailscan.adaplo.io/process -H "X-API-Key: $KEY" -F "file=@scan.pdf;type=application/pdf" -F "dpi=72"
```
