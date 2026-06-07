# Research — Open Source Landscape & Future Exploration

Compiled 2026-06-07. Research by AI agent across GitHub, PyPI, and general web search.

---

## Summary

There is no open source equivalent that does what Mailscan does end-to-end.
The combination of Mailmark barcode decode + postcode extraction + fuzzy recipient
matching + FastAPI microservice is unique. The projects below are partial overlaps
worth learning from or potentially adopting in future phases.

---

## Projects Worth Exploring

### 1. paperless-ngx
**GitHub:** https://github.com/paperless-ngx/paperless-ngx
**Stars:** 41,900 | **Active** (April 2026) | **License:** GPL-3.0

Self-hosted document management system. Ingests scanned PDFs from a watch folder,
runs OCR via Tesseract (via OCRmyPDF), auto-tags and indexes full text, exposes a
REST API and web UI. Celery + Redis for async processing.

**Relevant to Mailscan:**
- Has a barcode-based document splitter — detects patch codes and ASN barcodes on
  separator sheets to auto-split batch scans into individual documents. Worth studying
  if Mailscan ever needs to handle multi-letter batch PDFs in a single file.
- Auto-classification via trained scikit-learn model on existing documents — a future
  direction for client matching beyond fuzzy string matching.
- Email ingest pipeline — relevant to Phase 2 if digital mail (email attachments) needs
  to feed into the same pipeline as physical scans.

**Stack:** Python, Django, Angular, Celery, Redis, PostgreSQL, Tesseract, OCRmyPDF

---

### 2. OCRmyPDF
**GitHub:** https://github.com/ocrmypdf/OCRmyPDF
**Stars:** 33,800 | **Active** (April 2026) | **License:** MPL-2.0

The de facto standard Python library for adding a searchable text layer to scanned PDFs.
Handles deskewing, page rotation correction, PDF/A archival output, and multi-core batch
processing. Plugin architecture allows swapping OCR engine (PaddleOCR, EasyOCR etc).

**Relevant to Mailscan:**
- hOCR output gives **word-level bounding boxes** — lets the pipeline localise the
  address block on the page rather than extracting postcode from full-page OCR text.
  Would significantly improve accuracy on multi-column or complex layouts.
- Page rotation auto-detection — more robust than the current deskew-only approach.
- PDF/A output — if Mailscan ever needs to archive processed scans.
- MPL-2.0 license is compatible with proprietary usage provided OCRmyPDF itself is not modified.

**Potential adoption:** Replace or wrap the current `_preprocess` + `_ocr` steps in
`pipeline.py` with OCRmyPDF as a library. Low risk — it is actively maintained and
widely deployed in production.

**Stack:** Python, Tesseract, Ghostscript, Pillow, pikepdf

---

### 3. libpostal / pypostal
**GitHub:** https://github.com/openvenues/libpostal
**Stars:** 4,800 | **Last commit:** 2018 (unmaintained but stable) | **License:** MIT

C library with Python bindings for parsing and normalising street addresses globally.
Trained on 1 billion addresses including the full UK postcode dataset. Extracts labelled
components: house number, road, city, postcode, country.

**Relevant to Mailscan:**
- Handles OCR noise far better than regex — e.g. `LU11AA` (no space), `L U1 1AA`
  (extra space), `LU1 IAA` (OCR confused `1` with `I`). Current regex would miss these.
- Returns fully structured address components, not just postcode — could expose
  `address_line_1`, `city`, `postcode` as separate fields in the `/process` response.
- Higher confidence matching when the OCR quality is poor.

**Adoption consideration:** Requires compiling a large C binary (~1GB model data) — adds
significant Docker image size. Worth adding as an optional enhancement behind an env flag
(`ADDRESS_PARSER=libpostal`) so the default lightweight regex path is preserved.

**Stack:** C with Python bindings (pypostal on PyPI)

---

### 4. LATIS ocr-microservice
**GitHub:** https://github.com/LATIS-DocumentAI-Team/ocr-microservice
**Stars:** 5 | **License:** MIT

FastAPI microservice exposing a `/applyOcr/` endpoint that accepts image uploads and
returns structured JSON with word-level coordinates, bounding boxes, and content type.
Supports Tesseract, EasyOCR, and PaddleOCR via a single API parameter.

**Relevant to Mailscan:**
- Closest architectural match to what we've built — FastAPI + Tesseract + structured JSON.
- Multi-engine support pattern (`?engine=tesseract|easyocr|paddleocr`) is a clean way
  to add alternative OCR engines without changing the API contract.
- Word-level bounding box response format is worth borrowing for a future enhanced mode.

**Gaps vs Mailscan:** Image-only input (no PDF), no API key auth, no barcode decode,
no postcode extraction, no fuzzy matching.

**Stack:** Python 3.11, FastAPI, Tesseract, EasyOCR, PaddleOCR, Docker

---

### 5. pylibdmtx (already in use)
**GitHub:** https://github.com/NaturalHistoryMuseum/pylibdmtx
**Stars:** 174 | **License:** MIT

The Python wrapper for libdmtx — the only credible open source Python option for
Data Matrix barcode decoding.

**Note:** The PyPI package (v0.1.10, March 2022) is behind the GitHub HEAD.
Install from GitHub source for latest bug fixes:
```
pip install git+https://github.com/NaturalHistoryMuseum/pylibdmtx.git
```

---

### 6. royal-mail-stamp-barcode
**GitHub:** https://github.com/infrastructureclub/royal-mail-stamp-barcode
**Stars:** 3 | **Last commit:** 2022–2023

Documentation and research repo for parsing Royal Mail's new consumer stamp barcodes
introduced in 2022 (the "PennyBlack" 2D type-29 codes on consumer stamps).

**Relevant to Mailscan:**
- Consumer stamp barcodes differ from the Mailmark business mail spec. If the mail
  stream includes stamped consumer letters (not just franked business mail), the stamp
  barcode format requires separate handling.
- Documents the internal bit-layout and field structure of the newer stamp codes.

**Action if needed:** Parse stamp barcode fields after pylibdmtx decodes the raw bytes,
following the field layout documented in this repo.

---

### 7. Async OCR microservice pattern (Celery + Redis)
**Reference:** https://github.com/abizovnuralem/ocr
**Stars:** ~50

Demonstrates FastAPI + Tesseract + Celery + Redis async job queue pattern for OCR
workloads. Submit job via POST → receive job ID → poll for result.

**Relevant to Mailscan:**
- The current synchronous `/process` endpoint will timeout on large PDFs or slow
  Tesseract runs under load.
- Async pattern: `POST /process` → `{ "job_id": "abc" }` → `GET /jobs/abc` → result.
- Required if scan volume scales beyond a few PDFs per minute.

**Adoption path:** Add as Stage 6 in Phase 1 before Phase 2 dashboard is built —
the dashboard's upload UI would poll for job status naturally.

---

### 8. Mayan EDMS
**GitLab:** https://gitlab.com/mayan-edms/mayan-edms
**License:** Apache 2.0

Enterprise-grade Django document management system with Tesseract OCR, barcode indexing
(QR/1D via zxing), workflow engine, REST API, and granular ACLs.

**Relevant to Mailscan:**
- Workflow engine could model mail routing rules (received → assigned → actioned).
- Barcode indexing — uses barcode content to auto-file documents into folders.
- Heavy and complex — not worth adopting, but worth studying the workflow and
  barcode-indexing patterns for Phase 2 routing features.

**Stack:** Python, Django, Celery, PostgreSQL, Tesseract

---

## Gap Analysis — What the Open Source Ecosystem Is Missing

| Capability | Open source equivalent |
|---|---|
| Mailmark Data Matrix decode + field parse | None — pylibdmtx gives raw bytes only; field parsing is custom |
| UK postcode extraction from OCR text | Regex (current) or libpostal (ML-based, more robust) |
| Fuzzy recipient matching against a client list | RapidFuzz exists; no postal-context wrapper |
| Per-page structured JSON API (FastAPI, API key auth) | LATIS ocr-microservice is closest; no auth, no PDF |
| Combined PDF → deskew → binarise → OCR → barcode in one pipeline | Not found anywhere as a composable microservice |
| Royal Mail specific postal workflow end-to-end | Nothing — the combination is unique |

---

## Prioritised Future Improvements

| Priority | Improvement | Source project | Effort |
|----------|-------------|---------------|--------|
| High | Replace postcode regex with libpostal for OCR-noise resilience | libpostal / pypostal | Medium — Docker image size increase |
| High | Add async job queue for large PDF / high volume | abizovnuralem/ocr pattern | Medium — adds Redis + Celery |
| Medium | Use OCRmyPDF hOCR for word-level bounding boxes | OCRmyPDF | Low — library swap in pipeline.py |
| Medium | Consumer stamp barcode support (post-2022 Royal Mail stamps) | royal-mail-stamp-barcode | Low — field parser on top of pylibdmtx |
| Low | Multi-OCR-engine support (EasyOCR, PaddleOCR fallback) | LATIS ocr-microservice | Low — env flag to swap engine |
| Low | Barcode-based batch PDF splitting | paperless-ngx splitter | Medium — useful for bulk mail scans |
