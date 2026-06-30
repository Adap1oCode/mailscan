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

import os
from typing import Any, Callable, Optional

import fitz  # PyMuPDF
import numpy as np
from PIL import Image
from pylibdmtx.pylibdmtx import decode as dmtx_decode

from .ai_fallback import ai_extract, ai_summarise
from .pipeline import _match_clients, default_render_dpi, process_pdf

SEP_TOKEN = "MVOS-DOC-SEP"
_BLANK_OCR_LEN = 20  # a page with < this many OCR chars is a blank duplex back

# Ink coverage (% of dark pixels) below which a page is treated as blank — a
# duplex back, or the blank back of a single-sided separator sheet. Pixel-based,
# so it works WITHOUT OCR (the split-only path runs no Tesseract). Configurable.
_BLANK_INK_PCT = float(os.environ.get("MAILSCAN_BLANK_INK_PCT", "0.6"))

# Timeout (ms) for the centre-crop separator-barcode decode. A separator that IS
# present decodes in ~5ms; this ceiling only bounds the wasted time on pages that
# have NO separator (libdmtx scans to the deadline on a miss). Low scanner-output
# pages all look "light", so every page may attempt a decode — keep this small.
_SEP_DECODE_TIMEOUT_MS = int(os.environ.get("MAILSCAN_SEP_DECODE_TIMEOUT_MS", "250"))

# Absolute "this page has real marks" floor (% ink). Used only as a safety net:
# if EVERY page of a separator-delimited segment reads below the blank threshold,
# the densest page is still kept when it clears this floor — so a faint single-page
# letter is never silently lost. A true blank (e.g. a separator's ~0% duplex back)
# stays below it and is correctly dropped. Must be < _BLANK_INK_PCT.
_BLANK_KEEP_FLOOR = float(os.environ.get("MAILSCAN_BLANK_KEEP_FLOOR", "0.1"))


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
                res = dmtx_decode(crop, timeout=_SEP_DECODE_TIMEOUT_MS, max_count=1)
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


def _scan_pages(
    pdf_bytes: bytes,
    dpi: int,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> tuple[int, dict[int, float], set[int]]:
    """
    Single render pass over the batch for the SPLIT-ONLY path — no OCR, no
    full-page barcode scan. For each page it computes ink coverage (% dark pixels)
    and, on light pages, attempts the cheap centre-crop Data Matrix decode to find
    MVOS-DOC-SEP separator sheets.

    Returns (page_count, ink_pct_by_page_1based, separator_pages_1based).
    Reports progress as ("scan", page_index, page_count).
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    total = doc.page_count
    ink_by_page: dict[int, float] = {}
    seps: set[int] = set()
    try:
        for i in range(total):
            if on_progress:
                on_progress("scan", i, total)
            pix = doc[i].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
            ink = float((img < 128).mean()) * 100.0
            ink_by_page[i + 1] = ink
            # Separator sheets carry only a small centred barcode → low-but-nonzero
            # ink. Only those pages are worth a decode attempt (cheap centre crop).
            if 0.5 < ink < 12:
                h, w = img.shape
                crop = Image.fromarray(
                    img[int(h * 0.28):int(h * 0.62), int(w * 0.30):int(w * 0.70)]
                )
                try:
                    res = dmtx_decode(crop, timeout=_SEP_DECODE_TIMEOUT_MS, max_count=1)
                    if res and SEP_TOKEN in res[0].data.decode("ascii", "ignore"):
                        seps.add(i + 1)
                except Exception:
                    pass
        if on_progress:
            on_progress("scan", total, total)
    finally:
        doc.close()
    return total, ink_by_page, seps


def _group_split(
    page_count: int, ink_by_page: dict[int, float], separators: set[int]
) -> list[list[int]]:
    """
    Group content pages into letters using ONLY separator positions + ink coverage
    (no OCR). Separator sheets are cut points; pages below the blank threshold —
    including the blank back that follows a single-sided separator — are dropped.

    Works per separator-delimited segment so an all-blank segment is handled
    deliberately: if every page is sub-threshold but the densest still has real
    marks (>= _BLANK_KEEP_FLOOR) it's a faint letter and is kept, so a letter is
    never silently lost; a true blank (e.g. two separators around a duplex back,
    ~0% ink) yields no letter, as intended.
    """
    # 1. carve the page range into segments at the separator sheets
    segments: list[list[int]] = []
    cur: list[int] = []
    for n in range(1, page_count + 1):
        if n in separators:
            if cur:
                segments.append(cur)
                cur = []
            continue
        cur.append(n)
    if cur:
        segments.append(cur)

    # 2. per segment, keep non-blank pages; salvage faint single-page letters
    docs: list[list[int]] = []
    for seg in segments:
        kept = [n for n in seg if ink_by_page.get(n, 100.0) >= _BLANK_INK_PCT]
        if kept:
            docs.append(kept)
            continue
        densest = max(seg, key=lambda n: ink_by_page.get(n, 0.0))
        if ink_by_page.get(densest, 0.0) >= _BLANK_KEEP_FLOOR:
            docs.append([densest])  # faint letter — not a true blank
    return docs


def split_batch(
    pdf_bytes: bytes,
    dpi: Optional[int] = None,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> dict[str, Any]:
    """
    FAST split-only pass: separate a multi-letter batch into per-letter page groups
    WITHOUT OCR, full-page barcode scanning, client matching, or AI. This is the
    first wizard step — operators get the letter list in seconds; OCR/barcode/AI
    run later, per letter, on the small slices.

    Drops separator sheets and any blank page (incl. the separator's blank back).
    Returns {page_count, separators, documents:[{doc, pages, carrier_page, ...}]}
    with the same document shape process_batch emits, but with the OCR/barcode/
    match fields left empty (to be filled by the deferred per-letter steps).
    """
    if not dpi or dpi <= 0:
        dpi = default_render_dpi()

    page_count, ink_by_page, separators = _scan_pages(pdf_bytes, dpi, on_progress)
    groups = _group_split(page_count, ink_by_page, separators)

    documents: list[dict[str, Any]] = []
    total_letters = len(groups)
    for did, pgs in enumerate(groups, start=1):
        if on_progress:
            on_progress("split", did, total_letters)
        documents.append(
            {
                "doc": did,
                "pages": pgs,
                # Real Mailmark carrier is identified later (per-letter OCR); until
                # then the first page of the letter is the carrier placeholder.
                "carrier_page": pgs[0],
                "barcode_type": "unknown",
                "postcode": None,
                "barcode_return_postcode": None,
                "recipient_name": None,
                "matched_client": None,
                "match_score": None,
                "decision": "review",
                "tier": None,
                "summary": None,
                "ocr": [],
            }
        )

    return {
        "page_count": page_count,
        "separators": sorted(separators),
        "documents": documents,
    }


def process_batch(
    pdf_bytes: bytes,
    client_list: Optional[list[str]] = None,
    dpi: int = 300,
    ai_credentials: Optional[dict] = None,
    ai_prefer: Optional[str] = None,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> dict[str, Any]:
    """Separate a batch into letters with tiered extraction + summary."""
    creds = ai_credentials or {}
    has_textract = bool(creds.get("textract"))
    has_openrouter = bool(creds.get("openrouter"))

    # 1. free stack over the whole batch (no AI yet — cheaper, per-letter AI below)
    if on_progress:
        on_progress("ocr", 0, 1)
    base = process_pdf(pdf_bytes, client_list=client_list, dpi=dpi, enable_ai=False)
    pages = {p["page"]: p for p in base["pages"]}
    if on_progress:
        on_progress("ocr", 1, 1)

    # 2. deterministic split
    separators = _detect_separators(pdf_bytes)
    groups = _group_documents(pages, separators)

    documents: list[dict[str, Any]] = []
    total_letters = len(groups)
    for did, pgs in enumerate(groups, start=1):
        if on_progress:
            on_progress("ai", did - 1, total_letters)
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
            "barcode_return_postcode": (carrier.get("barcode_fields") or {}).get("return_postcode"),
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
        if on_progress:
            on_progress("ai", did, total_letters)

    return {"page_count": base["page_count"], "separators": sorted(separators), "documents": documents}
