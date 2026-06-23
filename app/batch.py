"""
Batch processing — a multi-letter scan (separator-sheet format) → separated letters.

This is the production version of scans/run_e2e_new.py: it does the whole
engine-side job in one call so MVOS can ingest the result letter-by-letter.

Pipeline per batch:
  1. free stack over every page (Tesseract hOCR + barcode + localise + match)
  2. SPLIT deterministically on MVOS-DOC-SEP separator sheets (center-crop decode,
     reliable where the incidental full-page decode is not), dropping blank backs
  3. per letter: if the free stack isn't confident, tier up — AWS Textract on the
     carrier image, then DeepSeek (OpenRouter) on the combined text
  4. per letter: a DeepSeek client-facing summary

Returns {page_count, documents:[...]} — one entry per letter, ready for MVOS to map
into a mail_items ingest. mailscan stays credential-free: AI creds are passed in.
"""
from __future__ import annotations

from typing import Any, Optional

import fitz  # PyMuPDF
import numpy as np
from PIL import Image
from pylibdmtx.pylibdmtx import decode as dmtx_decode

from .ai_fallback import ai_extract, ai_summarise
from .pipeline import _match_clients, process_pdf

SEP_TOKEN = "MVOS-DOC-SEP"
_BLANK_OCR_LEN = 20  # a page with < this many OCR chars is a blank duplex back


def _detect_separators(pdf_bytes: bytes, dpi: int = 150) -> set[int]:
    """Reliably find separator sheets by decoding the centre Data Matrix.

    The full-page barcode decode in pipeline.py misses these at 300 DPI; a
    centre-crop at a modest DPI decodes every MVOS-DOC-SEP sheet. Cheap — runs at
    150 DPI and only attempts a decode on light (separator/blank-ish) pages.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    seps: set[int] = set()
    try:
        for i in range(doc.page_count):
            pix = doc[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
            ink = float((img < 128).mean()) * 100.0
            if not (0.5 < ink < 12):
                continue
            h, w = img.shape
            crop = Image.fromarray(img[int(h * 0.28):int(h * 0.62), int(w * 0.30):int(w * 0.70)])
            try:
                res = dmtx_decode(crop, timeout=2000, max_count=1)
                if res and SEP_TOKEN in res[0].data.decode("ascii", "ignore"):
                    seps.add(i + 1)
            except Exception:
                pass
    finally:
        doc.close()
    return seps


def _carrier_png(pdf_bytes: bytes, page_1based: int, dpi: int = 300) -> bytes:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc[page_1based - 1].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        doc.close()


def _group_documents(pages: dict[int, dict], separators: set[int]) -> list[list[int]]:
    """Content pages between separators form a document; blanks/separators dropped."""
    docs: list[list[int]] = []
    cur: list[int] = []
    for n in sorted(pages):
        if n in separators:
            if cur:
                docs.append(cur)
                cur = []
            continue
        if len((pages[n].get("ocr_text") or "").strip()) < _BLANK_OCR_LEN:
            continue
        cur.append(n)
    if cur:
        docs.append(cur)
    return docs


def process_batch(
    pdf_bytes: bytes,
    client_list: Optional[list[str]] = None,
    dpi: int = 300,
    ai_credentials: Optional[dict] = None,
    ai_prefer: Optional[str] = None,
) -> dict[str, Any]:
    """Separate a batch into letters with tiered extraction + summary."""
    creds = ai_credentials or {}
    has_textract = bool(creds.get("textract"))
    has_openrouter = bool(creds.get("openrouter"))

    # 1. free stack over the whole batch (no AI yet — cheaper, per-letter AI below)
    base = process_pdf(pdf_bytes, client_list=client_list, dpi=dpi, enable_ai=False)
    pages = {p["page"]: p for p in base["pages"]}

    # 2. deterministic split
    separators = _detect_separators(pdf_bytes)
    groups = _group_documents(pages, separators)

    documents: list[dict[str, Any]] = []
    for did, pgs in enumerate(groups, start=1):
        carrier = next(
            (pages[n] for n in pgs if pages[n]["barcode_type"] == "mailmark"),
            pages[pgs[0]],
        )
        combined = "\n".join(pages[n]["ocr_text"] for n in pgs)
        rec: dict[str, Any] = {
            "doc": did,
            "pages": pgs,
            "carrier_page": carrier["page"],
            "barcode_type": carrier["barcode_type"],
            "postcode": carrier["postcode"],
            "recipient_name": carrier.get("recipient_name"),
            "matched_client": carrier.get("matched_client"),
            "match_score": carrier.get("match_score"),
            "decision": carrier["decision"],
            "tier": "own" if carrier["decision"] == "auto" else None,
        }

        # 3. tier up only when the free stack wasn't confident
        if carrier["decision"] != "auto":
            if has_textract:
                ai = ai_extract(
                    _carrier_png(pdf_bytes, carrier["page"], dpi),
                    {"ocr_text": carrier["ocr_text"], "credentials": creds},
                    prefer="textract",
                )
                if ai:
                    rec["recipient_name"] = ai.recipient_name or rec["recipient_name"]
                    if ai.address:
                        c2, s2, _ = _match_clients(ai.address, client_list)
                        if c2:
                            rec.update(tier="aws", decision="auto", matched_client=c2, match_score=s2)
            if rec["decision"] != "auto" and has_openrouter:
                ai3 = ai_extract(b"", {"ocr_text": combined, "credentials": creds}, prefer="openrouter")
                if ai3 and ai3.recipient_name:
                    rec["recipient_name"] = ai3.recipient_name
                    c3, s3, _ = _match_clients(ai3.recipient_name, client_list)
                    if c3:
                        rec.update(tier="deepseek", decision="auto", matched_client=c3, match_score=s3)
                    else:
                        rec["tier"] = rec["tier"] or "deepseek-extract"

        # 4. client-facing summary
        rec["summary"] = ai_summarise(combined, {"credentials": creds}) if has_openrouter else None
        rec["ocr"] = [{"page": n, "text": pages[n]["ocr_text"]} for n in pgs]
        documents.append(rec)

    return {"page_count": base["page_count"], "separators": sorted(separators), "documents": documents}
