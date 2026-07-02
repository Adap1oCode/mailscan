"""
Batch processing — a multi-letter scan (separator-sheet format) → separated letters.

This is the production version of scans/run_e2e_new.py: it does the whole
engine-side job in one call so MVOS can ingest the result letter-by-letter.

Pipeline per batch:
  1. SPLIT first — one cheap grayscale pass (ink coverage + centre-crop decode of
     MVOS-DOC-SEP separator sheets). Deterministic, no OCR. Separator sheets and
     blank duplex backs are identified here and never OCR'd at all.
  2. free stack over CONTENT pages only (Tesseract hOCR + barcode + localise + match)
  3. per letter: if the free stack isn't confident, tier up — AWS Textract on the
     carrier image, then DeepSeek (OpenRouter) on the combined text
  4. per letter: a DeepSeek client-facing summary
  Steps 3–4 run across letters on a small thread pool — the AI calls are
  network-bound, so N letters no longer cost N sequential round-trips.

Returns {page_count, documents:[...]} — one entry per letter, ready for MVOS to map
into a mail_items ingest. mailscan stays credential-free: AI creds are passed in.
"""
from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

import fitz  # PyMuPDF
import numpy as np
from PIL import Image
from pylibdmtx.pylibdmtx import decode as dmtx_decode

from .ai_fallback import ai_extract, ai_summarise
from .pipeline import MatchSettings, _match_clients, default_render_dpi, process_pdf

logger = logging.getLogger("mailscan.batch")

SEP_TOKEN = "MVOS-DOC-SEP"

# Ink coverage (% of dark pixels) below which a page is treated as blank — a
# duplex back, or the blank back of a single-sided separator sheet. Pixel-based,
# so it works WITHOUT OCR (the split pass runs no Tesseract). Configurable.
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

# How many letters run their AI tier-up + summary concurrently. The calls are
# network-bound (OpenRouter/Textract round-trips), so a small pool collapses the
# dominant wall-clock cost of a big batch without hammering the providers.
_AI_CONCURRENCY = int(os.environ.get("MAILSCAN_AI_CONCURRENCY", "4"))


def _split_thresholds(options: dict | None) -> tuple[float, float]:
    """Blank-page thresholds — per-request options["split"] override the env defaults."""
    s = (options or {}).get("split") or {}

    def num(v: object, default: float) -> float:
        try:
            return float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

    return (
        num(s.get("blank_ink_pct"), _BLANK_INK_PCT),
        num(s.get("blank_keep_floor"), _BLANK_KEEP_FLOOR),
    )


def _is_separator_page(img: np.ndarray, ink_pct: float) -> bool:
    """
    Detect an MVOS-DOC-SEP separator sheet on a rendered grayscale page.

    Separator sheets carry only a small centred Data Matrix → low-but-nonzero ink;
    only pages in that band are worth a decode attempt (cheap centre crop — the
    full-page decode in pipeline.py misses these at 300 DPI, the crop never does).
    """
    if not (0.5 < ink_pct < 12):
        return False
    h, w = img.shape
    crop = Image.fromarray(img[int(h * 0.28):int(h * 0.62), int(w * 0.30):int(w * 0.70)])
    try:
        res = dmtx_decode(crop, timeout=_SEP_DECODE_TIMEOUT_MS, max_count=1)
        return bool(res and SEP_TOKEN in res[0].data.decode("ascii", "ignore"))
    except Exception:
        logger.warning("separator decode failed on a candidate page", exc_info=True)
        return False


def _scan_pages(
    pdf_bytes: bytes,
    dpi: int,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> tuple[int, dict[int, float], set[int]]:
    """
    Single grayscale render pass over the batch — no OCR, no full-page barcode
    scan. For each page it computes ink coverage (% dark pixels) and, on light
    pages, attempts the cheap centre-crop Data Matrix decode to find MVOS-DOC-SEP
    separator sheets.

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
            if _is_separator_page(img, ink):
                seps.add(i + 1)
        if on_progress:
            on_progress("scan", total, total)
    finally:
        doc.close()
    return total, ink_by_page, seps


def _group_split(
    page_count: int,
    ink_by_page: dict[int, float],
    separators: set[int],
    blank_ink_pct: float | None = None,
    blank_keep_floor: float | None = None,
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
    ink_min = blank_ink_pct if blank_ink_pct is not None else _BLANK_INK_PCT
    keep_floor = blank_keep_floor if blank_keep_floor is not None else _BLANK_KEEP_FLOOR
    docs: list[list[int]] = []
    for seg in segments:
        kept = [n for n in seg if ink_by_page.get(n, 100.0) >= ink_min]
        if kept:
            docs.append(kept)
            continue
        densest = max(seg, key=lambda n: ink_by_page.get(n, 0.0))
        if ink_by_page.get(densest, 0.0) >= keep_floor:
            docs.append([densest])  # faint letter — not a true blank
    return docs


def split_batch(
    pdf_bytes: bytes,
    dpi: Optional[int] = None,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
    options: Optional[dict] = None,
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

    blank_ink_pct, blank_keep_floor = _split_thresholds(options)
    page_count, ink_by_page, separators = _scan_pages(pdf_bytes, dpi, on_progress)
    groups = _group_split(page_count, ink_by_page, separators, blank_ink_pct, blank_keep_floor)

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


def _render_carrier_pngs(pdf_bytes: bytes, pages_1based: set[int], dpi: int) -> dict[int, bytes]:
    """Render the requested pages as PNG in ONE document open (for Textract)."""
    if not pages_1based:
        return {}
    out: dict[int, bytes] = {}
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for n in sorted(pages_1based):
            out[n] = doc[n - 1].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        doc.close()
    return out


def _enrich_letter(
    rec: dict[str, Any],
    combined_text: str,
    carrier_png: bytes | None,
    carrier_ocr_text: str,
    client_list: Optional[list[str]],
    creds: dict,
    options: Optional[dict] = None,
) -> None:
    """
    AI tier-up + summary for ONE letter (mutates rec in place). Runs on the
    letter pool — must not touch shared mutable state beyond rec.
    """
    has_textract = bool(creds.get("textract"))
    has_openrouter = bool(creds.get("openrouter"))
    settings = MatchSettings(options)
    ctx_base = {"credentials": creds, "options": options or {}}

    # tier up only when the free stack wasn't confident
    if rec["decision"] != "auto":
        if has_textract and carrier_png:
            ai = ai_extract(
                carrier_png,
                {**ctx_base, "ocr_text": carrier_ocr_text},
                prefer="textract",
            )
            if ai:
                rec["recipient_name"] = ai.recipient_name or rec["recipient_name"]
                if ai.address:
                    c2, s2, _ = _match_clients(ai.address, client_list, cutoff=settings.cutoff)
                    if c2:
                        rec.update(tier="aws", decision="auto", matched_client=c2, match_score=s2)
        if rec["decision"] != "auto" and has_openrouter:
            ai3 = ai_extract(b"", {**ctx_base, "ocr_text": combined_text}, prefer="openrouter")
            if ai3 and ai3.recipient_name:
                rec["recipient_name"] = ai3.recipient_name
                c3, s3, _ = _match_clients(ai3.recipient_name, client_list, cutoff=settings.cutoff)
                if c3:
                    rec.update(tier="deepseek", decision="auto", matched_client=c3, match_score=s3)
                else:
                    rec["tier"] = rec["tier"] or "deepseek-extract"

    # client-facing summary
    rec["summary"] = ai_summarise(combined_text, ctx_base) if has_openrouter else None


def process_batch(
    pdf_bytes: bytes,
    client_list: Optional[list[str]] = None,
    dpi: int = 300,
    ai_credentials: Optional[dict] = None,
    ai_prefer: Optional[str] = None,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
    options: Optional[dict] = None,
) -> dict[str, Any]:
    """
    Separate a batch into letters with tiered extraction + summary.

    Progress steps: ("scan", page, total) → ("ocr", page, content_total) →
    ("ai", letter, letter_total).
    """
    creds = ai_credentials or {}
    if not dpi or dpi <= 0:
        dpi = default_render_dpi()

    # 1. split FIRST (cheap ink/separator scan) so separator sheets and blank
    # backs are never rendered at full quality, OCR'd, or barcode-scanned at all.
    blank_ink_pct, blank_keep_floor = _split_thresholds(options)
    page_count, ink_by_page, separators = _scan_pages(pdf_bytes, dpi, on_progress)
    groups = _group_split(page_count, ink_by_page, separators, blank_ink_pct, blank_keep_floor)
    content_pages = sorted(n for g in groups for n in g)

    # 2. free stack over content pages only (no AI yet — per-letter AI below)
    base = process_pdf(
        pdf_bytes,
        client_list=client_list,
        dpi=dpi,
        enable_ai=False,
        page_numbers=content_pages,
        on_progress=on_progress,
        options=options,
    )
    pages = {p["page"]: p for p in base["pages"]}

    # 3. per-letter records from the free-stack signals
    letters: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
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
            "barcode_return_postcode": (carrier.get("barcode_fields") or {}).get("return_postcode"),
            "recipient_name": carrier.get("recipient_name"),
            "matched_client": carrier.get("matched_client"),
            "match_score": carrier.get("match_score"),
            "decision": carrier["decision"],
            "tier": "own" if carrier["decision"] == "auto" else None,
            "summary": None,
            "ocr": [{"page": n, "text": pages[n]["ocr_text"]} for n in pgs],
        }
        letters.append((rec, combined, carrier))

    # 4. carrier PNGs for the Textract tier — rendered in one document open, only
    # for letters that can actually reach that tier.
    carrier_pngs: dict[int, bytes] = {}
    if creds.get("textract"):
        need = {carrier["page"] for rec, _, carrier in letters if rec["decision"] != "auto"}
        carrier_pngs = _render_carrier_pngs(pdf_bytes, need, dpi)

    # 5. AI tier-up + summary across letters on a small pool — the calls are
    # network-bound, so this collapses N sequential round-trips into ~N/pool.
    total_letters = len(letters)
    if on_progress:
        on_progress("ai", 0, total_letters)
    done = 0
    done_lock = threading.Lock()

    def _work(item: tuple[dict[str, Any], str, dict[str, Any]]) -> None:
        nonlocal done
        rec, combined, carrier = item
        try:
            _enrich_letter(
                rec, combined, carrier_pngs.get(carrier["page"]), carrier["ocr_text"],
                client_list, creds, options,
            )
        except Exception:
            # A failed enrichment must not sink the batch — the letter keeps its
            # free-stack fields and falls to review/AI downstream.
            logger.warning("AI enrichment failed for letter %s", rec["doc"], exc_info=True)
        if on_progress:
            with done_lock:
                done += 1
                on_progress("ai", done, total_letters)

    if creds.get("textract") or creds.get("openrouter"):
        with ThreadPoolExecutor(max_workers=max(1, _AI_CONCURRENCY)) as pool:
            list(pool.map(_work, letters))
    else:
        for item in letters:
            _work(item)

    documents = [rec for rec, _, _ in letters]
    return {"page_count": page_count, "separators": sorted(separators), "documents": documents}
