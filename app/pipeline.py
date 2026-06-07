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
from typing import Any

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

_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})\b")

# Mailmark barcode: starts with J (business mail) or similar identifier
# Format: JGB2...  or similar — first char identifies version
_MAILMARK_RE = re.compile(r"^[A-Z]\d{2}[A-Z0-9]")


# ---------------------------------------------------------------------------
# PDF → images
# ---------------------------------------------------------------------------

def _pdf_to_images(pdf_bytes: bytes, dpi: int = 300) -> list[np.ndarray]:
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    images = []
    for page in doc:
        pix = page.get_pixmap(dpi=dpi)
        img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 4:
            img = cv2.cvtColor(img, cv2.COLOR_RGBA2RGB)
        images.append(img)
    doc.close()
    return images


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
    results = dmtx_decode(pil)
    if not results:
        return None, "unknown", None

    raw = results[0].data.decode("utf-8", errors="replace")
    barcode_type, fields = _classify_and_parse_barcode(raw)
    return raw, barcode_type, fields


def _classify_and_parse_barcode(raw: str) -> tuple[str, dict | None]:
    """Detect whether this is a Mailmark, consumer stamp, or unknown barcode."""
    if _MAILMARK_RE.match(raw):
        return "mailmark", _parse_mailmark(raw)

    # Consumer stamp barcodes (post-2022) start with different identifiers
    # Format documented at: https://github.com/infrastructureclub/royal-mail-stamp-barcode
    if raw.startswith(("01", "02", "03")):
        return "stamp", _parse_stamp_barcode(raw)

    return "unknown", None


def _parse_mailmark(raw: str) -> dict:
    """
    Parse Royal Mail Mailmark business mail barcode fields.
    Mailmark format: version(1) + class(2) + format(1) + postcode(7..9) + ...
    Returns whatever fields can be extracted — partial results are valid.
    """
    fields: dict = {"raw": raw}
    try:
        fields["version"] = raw[0] if len(raw) > 0 else None
        fields["mail_class"] = raw[1:3] if len(raw) > 2 else None
        # Postcode is embedded — extract via regex from the raw string
        postcode_match = _POSTCODE_RE.search(raw.upper())
        if postcode_match:
            fields["postcode"] = postcode_match.group(1)
    except Exception:
        pass
    return fields


def _parse_stamp_barcode(raw: str) -> dict:
    """
    Parse post-2022 Royal Mail consumer stamp barcode.
    Field layout per: https://github.com/infrastructureclub/royal-mail-stamp-barcode
    Returns whatever fields can be extracted.
    """
    fields: dict = {"raw": raw}
    try:
        fields["product_id"] = raw[0:2] if len(raw) > 1 else None
        postcode_match = _POSTCODE_RE.search(raw.upper())
        if postcode_match:
            fields["postcode"] = postcode_match.group(1)
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
    from rapidfuzz import process as fuzz_process

    images = _pdf_to_images(pdf_bytes, dpi=dpi)
    pages = []

    for i, img in enumerate(images):
        processed = _preprocess(img)
        ocr_text = _ocr(processed)

        postcode, address_components = _extract_postcode(ocr_text)

        # Barcode decode on original (not preprocessed) image
        barcode, barcode_type, barcode_fields = _decode_barcode(img)

        # Fallback: extract postcode from barcode if OCR didn't find one
        if not postcode and barcode:
            postcode_from_barcode, _ = _extract_postcode(barcode)
            if postcode_from_barcode:
                postcode = postcode_from_barcode
            # Also check parsed barcode fields
            if not postcode and barcode_fields:
                postcode = barcode_fields.get("postcode")

        matched_client: str | None = None
        match_score: float | None = None
        if client_list:
            result = fuzz_process.extractOne(ocr_text, client_list, score_cutoff=70)
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
