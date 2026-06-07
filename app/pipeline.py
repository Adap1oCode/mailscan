"""
Core mailscan pipeline — PDF → per-page OCR + barcode results.
No HTTP code here. Called by main.py or directly from CLI/tests.
"""
import os
import re
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

# Windows default path
elif os.name == "nt":
    pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

_POSTCODE_RE = re.compile(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})\b")


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


def _preprocess(img: np.ndarray) -> np.ndarray:
    """Deskew and binarise — improves OCR accuracy on scanned docs."""
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)

    # Deskew using minAreaRect on dark pixel coords
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


def _ocr(img: np.ndarray) -> str:
    pil = Image.fromarray(img)
    return pytesseract.image_to_string(pil, config="--psm 6")


def _decode_barcode(img: np.ndarray) -> str | None:
    """Attempt Royal Mail Mailmark Data Matrix decode."""
    pil = Image.fromarray(img)
    results = dmtx_decode(pil)
    if results:
        return results[0].data.decode("utf-8", errors="replace")
    return None


def _extract_postcode(text: str) -> str | None:
    match = _POSTCODE_RE.search(text.upper())
    return match.group(1) if match else None


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
                    "barcode": str | None,
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

        postcode = _extract_postcode(ocr_text)

        barcode = _decode_barcode(img)
        # Mailmark barcode often contains a postcode — use as fallback
        if not postcode and barcode:
            postcode = _extract_postcode(barcode)

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
            "barcode": barcode,
            "matched_client": matched_client,
            "match_score": match_score,
        })

    return {
        "page_count": len(pages),
        "pages": pages,
    }
