"""
Core mailscan pipeline — PDF → per-page OCR + barcode results.
No HTTP code here. Called by main.py or directly from CLI/tests.

OCR engine: OCRmyPDF in API mode (hOCR output) for word-level bounding boxes.
Address parsing: libpostal when ADDRESS_PARSER=libpostal, otherwise regex fallback.
Barcode: pylibdmtx for Data Matrix decode, with Mailmark + stamp field parsers.
"""
import os
import re
import io
import xml.etree.ElementTree as ET
from typing import Any, Iterator

import cv2
import fitz  # PyMuPDF
import numpy as np
import pytesseract
from PIL import Image
from pylibdmtx.pylibdmtx import decode as dmtx_decode

# Allow override via env — required on Linux/Mac
_tess_cmd = os.environ.get("TESSERACT_CMD")
if _tess_cmd:
    pytesseract.pytesseract.tesseract_cmd = _tess_cmd
elif os.name == "nt":
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# Address parser selection — set ADDRESS_PARSER=libpostal to enable ML-based parsing
_ADDRESS_PARSER = os.environ.get("ADDRESS_PARSER", "regex").lower()

# Cap the longest rendered side (pixels) to bound peak memory on huge / high-DPI
# pages. A4 @ 300 DPI is ~3508px, so 4500 leaves headroom for A3 while still
# clamping pathological inputs that would otherwise OOM the container.
_MAX_RENDER_PX = int(os.environ.get("MAILSCAN_MAX_RENDER_PX", "4500"))

# Hard ceiling (ms) for a single Data Matrix scan. Bounds the worst case on a
# barcode-free page, which would otherwise scan the whole high-DPI image.
_DMTX_TIMEOUT_MS = int(os.environ.get("MAILSCAN_DMTX_TIMEOUT_MS", "10000"))

# Minimum score (0–100) for a client fuzzy match. partial_token_set_ratio scores
# a recipient that appears anywhere in the page text ~100, and non-matches well
# below, so a high cutoff keeps precision without missing genuine recipients.
_MATCH_CUTOFF = float(os.environ.get("MAILSCAN_MATCH_CUTOFF", "85"))

_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})\b")

# Postcode embedded inside a barcode payload — no surrounding word boundaries,
# packed against digits (e.g. "...655099LU48DP1E..."), so the boundary-anchored
# _POSTCODE_RE above won't match. Same shape, no \b.
_BARCODE_POSTCODE_RE = re.compile(r"([A-Z]{1,2}\d{1,2}[A-Z]?\d[A-Z]{2})")

# Royal Mail Mailmark Data Matrix payloads begin with a Mailmark/country prefix,
# e.g. "JGB 01E..." or "JGB2..." (J = Mailmark indicator, GB = country code).
# Match leniently after stripping spaces; the old `^[A-Z]\d{2}` pattern never
# matched real payloads (TESTS.md flagged the barcode path as untested).
_MAILMARK_RE = re.compile(r"^J?GB", re.IGNORECASE)


# ---------------------------------------------------------------------------
# PDF → images
# ---------------------------------------------------------------------------

def _effective_dpi(page: "fitz.Page", requested_dpi: int) -> int:
    """
    Clamp the render DPI so the longest rendered side stays within _MAX_RENDER_PX.
    page.rect is in points (1/72 inch); pixels = points / 72 * dpi.
    """
    longest_pts = max(page.rect.width, page.rect.height)
    if longest_pts <= 0:
        return requested_dpi
    max_dpi = int(_MAX_RENDER_PX * 72 / longest_pts)
    # Never drop below 72 DPI — OCR accuracy collapses below that (see TESTS.md).
    return max(72, min(requested_dpi, max_dpi))


def _iter_pdf_images(pdf_bytes: bytes, dpi: int = 300) -> Iterator[np.ndarray]:
    """
    Yield one rendered RGB page image at a time.

    Streaming — rather than building a list of every page up front — keeps peak
    memory at roughly one page regardless of page count. A 300-page batch then
    uses the same RAM as a single-page letter, which is what stops large scans
    OOM-ing the container.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        for page in doc:
            pix = page.get_pixmap(dpi=_effective_dpi(page, dpi))
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            if pix.n == 4:
                img = cv2.cvtColor(img, cv2.COLOR_RGBA2RGB)
            # Copy off the pixmap buffer so it can be freed before the next page.
            yield img.copy()
            del pix, img
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------

def _preprocess(img: np.ndarray) -> np.ndarray:
    """Deskew and binarise — improves OCR accuracy on scanned docs."""
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

    coords = np.column_stack(np.where(gray < 200))
    if len(coords) > 100:
        angle = cv2.minAreaRect(coords)[-1]
        angle = -(90 + angle) if angle < -45 else -angle
        if abs(angle) > 0.5:
            h, w = gray.shape
            M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
            gray = cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)

    _, binarised = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binarised


# ---------------------------------------------------------------------------
# OCR — OCRmyPDF hOCR mode with pytesseract fallback
# ---------------------------------------------------------------------------

def _ocr_with_hocr(img: np.ndarray) -> tuple[str, list[dict]]:
    """
    Run OCR and return (full_text, word_list).
    word_list items: {"text": str, "x0": int, "y0": int, "x1": int, "y1": int}

    Uses OCRmyPDF hOCR output for word-level bounding boxes.
    Falls back to plain pytesseract if OCRmyPDF is unavailable.
    """
    try:
        import ocrmypdf
        pil = Image.fromarray(img)
        hocr_bytes = pytesseract.image_to_pdf_or_hocr(pil, extension="hocr", config="--psm 6")
        words = _parse_hocr(hocr_bytes)
        full_text = " ".join(w["text"] for w in words if w["text"].strip())
        return full_text, words
    except Exception:
        # Fallback to plain pytesseract
        pil = Image.fromarray(img)
        text = pytesseract.image_to_string(pil, config="--psm 6")
        return text, []


def _parse_hocr(hocr_bytes: bytes) -> list[dict]:
    """Extract word bounding boxes from hOCR XML output."""
    words = []
    try:
        root = ET.fromstring(hocr_bytes.decode("utf-8", errors="replace"))
        ns = {"html": "http://www.w3.org/1999/xhtml"}

        for elem in root.iter():
            cls = elem.get("class", "")
            if "ocrx_word" not in cls and "ocr_word" not in cls:
                continue
            title = elem.get("title", "")
            bbox_match = re.search(r"bbox (\d+) (\d+) (\d+) (\d+)", title)
            if bbox_match and elem.text:
                x0, y0, x1, y1 = map(int, bbox_match.groups())
                words.append({"text": elem.text.strip(), "x0": x0, "y0": y0, "x1": x1, "y1": y1})
    except ET.ParseError:
        pass
    return words


def _ocr(img: np.ndarray) -> str:
    """Run OCR and return full text string."""
    text, _ = _ocr_with_hocr(img)
    return text


# ---------------------------------------------------------------------------
# Barcode decode — pylibdmtx + field parsers
# ---------------------------------------------------------------------------

def _decode_barcode(img: np.ndarray) -> tuple[str | None, str, dict | None]:
    """
    Attempt Royal Mail Data Matrix decode.
    Returns (raw_string, barcode_type, barcode_fields).
    barcode_type: 'mailmark' | 'stamp' | 'unknown'
    """
    pil = Image.fromarray(img)
    # max_count=1: stop after the first symbol (a letter has one barcode).
    # timeout: hard ceiling so a barcode-free page can't scan the full 300-DPI
    # image indefinitely — this is the main per-page latency source.
    results = dmtx_decode(pil, max_count=1, timeout=_DMTX_TIMEOUT_MS)
    if not results:
        return None, "unknown", None

    raw = results[0].data.decode("utf-8", errors="replace").strip()
    barcode_type, fields = _classify_and_parse_barcode(raw)
    return raw, barcode_type, fields


def _normalise_postcode(pc: str) -> str:
    """Re-insert the space in a packed postcode: 'LU48DP' -> 'LU4 8DP'."""
    pc = pc.replace(" ", "").upper()
    return f"{pc[:-3]} {pc[-3:]}" if len(pc) >= 5 else pc


def _postcode_from_barcode(raw: str) -> str | None:
    """Find the first embedded (delivery) postcode in a barcode payload."""
    match = _BARCODE_POSTCODE_RE.search(raw.replace(" ", "").upper())
    return _normalise_postcode(match.group(1)) if match else None


def _classify_and_parse_barcode(raw: str) -> tuple[str, dict | None]:
    """Detect whether this is a Mailmark, consumer stamp, or unknown barcode."""
    cleaned = raw.strip().upper()
    if _MAILMARK_RE.match(cleaned.replace(" ", "")):
        return "mailmark", _parse_mailmark(raw)

    # Consumer stamp barcodes (post-2022) start with different identifiers
    # Format documented at: https://github.com/infrastructureclub/royal-mail-stamp-barcode
    if cleaned.startswith(("01", "02", "03")):
        return "stamp", _parse_stamp_barcode(raw)

    return "unknown", None


def _parse_mailmark(raw: str) -> dict:
    """
    Parse Royal Mail Mailmark business mail barcode fields.
    Payloads look like "JGB 01E...<delivery postcode>...<return postcode>".
    Returns whatever fields can be extracted — partial results are valid.
    """
    cleaned = raw.strip()
    fields: dict = {"raw": cleaned}
    try:
        compact = cleaned.replace(" ", "").upper()
        fields["version"] = compact[0] if compact else None  # 'J'
        fields["mail_class"] = compact[1:3] if len(compact) > 2 else None  # 'GB'
        postcode = _postcode_from_barcode(cleaned)
        if postcode:
            fields["postcode"] = postcode
    except Exception:
        pass
    return fields


def _parse_stamp_barcode(raw: str) -> dict:
    """
    Parse post-2022 Royal Mail consumer stamp barcode.
    Field layout per: https://github.com/infrastructureclub/royal-mail-stamp-barcode
    Returns whatever fields can be extracted.
    """
    cleaned = raw.strip()
    fields: dict = {"raw": cleaned}
    try:
        fields["product_id"] = cleaned[0:2] if len(cleaned) > 1 else None
        postcode = _postcode_from_barcode(cleaned)
        if postcode:
            fields["postcode"] = postcode
    except Exception:
        pass
    return fields


# ---------------------------------------------------------------------------
# Postcode extraction — regex or libpostal
# ---------------------------------------------------------------------------

def _extract_postcode_regex(text: str) -> str | None:
    match = _POSTCODE_RE.search(text.upper())
    return match.group(1) if match else None


def _extract_postcode_libpostal(text: str) -> tuple[str | None, dict | None]:
    """
    Parse address using libpostal ML model.
    Returns (postcode, address_components) or (None, None) if not found.
    Requires ADDRESS_PARSER=libpostal and the postal package installed.
    """
    try:
        from postal.parser import parse_address
        components = parse_address(text)
        comp_dict = {label: value for value, label in components}
        postcode = comp_dict.get("postcode")
        return postcode, comp_dict if comp_dict else None
    except ImportError:
        # libpostal not installed — fall back silently
        return _extract_postcode_regex(text), None
    except Exception:
        return _extract_postcode_regex(text), None


def _extract_postcode(text: str) -> tuple[str | None, dict | None]:
    """
    Extract postcode from text. Returns (postcode, address_components).
    address_components is populated only when ADDRESS_PARSER=libpostal.
    """
    if _ADDRESS_PARSER == "libpostal":
        return _extract_postcode_libpostal(text)
    postcode = _extract_postcode_regex(text)
    return postcode, None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def process_pdf(
    pdf_bytes: bytes,
    client_list: list[str] | None = None,
    dpi: int = 300,
) -> dict[str, Any]:
    """
    Process a PDF scan and return structured results.

    Args:
        pdf_bytes:   Raw PDF file bytes.
        client_list: Optional list of known client names for fuzzy matching.
        dpi:         Render DPI — 300 is optimal for OCR, lower is faster.

    Returns:
        {
            "page_count": int,
            "pages": [
                {
                    "page": int,
                    "ocr_text": str,
                    "postcode": str | None,
                    "address_components": dict | None,  # populated when ADDRESS_PARSER=libpostal
                    "barcode": str | None,
                    "barcode_type": str,                # 'mailmark' | 'stamp' | 'unknown'
                    "barcode_fields": dict | None,      # structured fields when type is known
                    "matched_client": str | None,
                    "match_score": float | None,
                }
            ]
        }
    """
    from rapidfuzz import fuzz, process as fuzz_process

    pages = []

    # Stream pages one at a time — only the result dicts (small JSON) accumulate;
    # page bitmaps are processed and discarded as we go.
    for i, img in enumerate(_iter_pdf_images(pdf_bytes, dpi=dpi)):
        processed = _preprocess(img)
        ocr_text = _ocr(processed)

        ocr_postcode, address_components = _extract_postcode(ocr_text)

        # Barcode decode on original (not preprocessed) image
        barcode, barcode_type, barcode_fields = _decode_barcode(img)
        barcode_postcode = (barcode_fields or {}).get("postcode")

        # A Mailmark/stamp barcode encodes the machine-readable delivery
        # (recipient) postcode. That is the authoritative routing destination and
        # is far more reliable than a regex over a dense page, which can latch
        # onto an unrelated postcode in the body. Prefer the barcode postcode when
        # present; fall back to the OCR-extracted one otherwise.
        postcode = barcode_postcode or ocr_postcode

        matched_client: str | None = None
        match_score: float | None = None
        if client_list:
            result = fuzz_process.extractOne(
                ocr_text,
                client_list,
                scorer=fuzz.partial_token_set_ratio,
                score_cutoff=_MATCH_CUTOFF,
            )
            if result:
                matched_client, match_score, _ = result
                match_score = round(match_score, 1)

        pages.append({
            "page": i + 1,
            "ocr_text": ocr_text.strip(),
            "postcode": postcode,
            "address_components": address_components,
            "barcode": barcode,
            "barcode_type": barcode_type,
            "barcode_fields": barcode_fields,
            "matched_client": matched_client,
            "match_score": match_score,
        })

    return {
        "page_count": len(pages),
        "pages": pages,
    }
