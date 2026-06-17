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

# Decoded payloads shorter than this are scan-noise false positives (e.g. a
# stray pattern decoding to "0"), not real barcodes — discard them.
_MIN_BARCODE_LEN = 4

# Minimum score (0–100) for a client fuzzy match. partial_token_set_ratio scores
# a recipient that appears anywhere in the page text ~100, and non-matches well
# below, so a high cutoff keeps precision without missing genuine recipients.
_MATCH_CUTOFF = float(os.environ.get("MAILSCAN_MATCH_CUTOFF", "85"))

# Minimum margin (best − second-best client score) for a match to count as
# unambiguous. Two near-tied candidates are not confident → hand to AI / review.
_MATCH_MARGIN = float(os.environ.get("MAILSCAN_MATCH_MARGIN", "10"))

# Minimum recipient-name extraction confidence (0–1) to route on the free stack.
_NAME_CONF_AUTO = float(os.environ.get("MAILSCAN_NAME_CONF", "0.6"))

# Postcodes that are shared/virtual offices: the postcode is identical for many
# clients, so the recipient NAME (not the postcode) is what routes there.
_SHARED_POSTCODES = {
    p.strip().upper()
    for p in os.environ.get("MAILSCAN_SHARED_POSTCODES", "LU1 2DW").split(",")
    if p.strip()
}

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
    Run OCR and return (full_text, word_list) with word-level bounding boxes.
    word_list items: {"text": str, "x0": int, "y0": int, "x1": int, "y1": int}

    Uses pytesseract's hOCR output (image_to_pdf_or_hocr) for the boxes — this only
    needs the Tesseract binary, NOT ocrmypdf. Falls back to plain image_to_string
    only if hOCR parsing yields nothing.
    """
    pil = Image.fromarray(img)
    try:
        hocr_bytes = pytesseract.image_to_pdf_or_hocr(pil, extension="hocr", config="--psm 6")
        words = _parse_hocr(hocr_bytes)
        if words:
            full_text = " ".join(w["text"] for w in words if w["text"].strip())
            return full_text, words
    except Exception:
        pass
    return pytesseract.image_to_string(pil, config="--psm 6"), []


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
# Recipient / address-block extraction (uses hOCR word positions)
# ---------------------------------------------------------------------------

def _mk_line(ws: list[dict]) -> dict:
    ws = sorted(ws, key=lambda w: w["x0"])
    return {
        "text": " ".join(w["text"] for w in ws).strip(),
        "x0": min(w["x0"] for w in ws), "x1": max(w["x1"] for w in ws),
        "y0": min(w["y0"] for w in ws), "y1": max(w["y1"] for w in ws),
    }


def _group_lines(words: list[dict]) -> list[dict]:
    """Group hOCR words into text lines by vertical proximity (top to bottom)."""
    if not words:
        return []
    ws = sorted(words, key=lambda w: (w["y0"], w["x0"]))
    heights = sorted(w["y1"] - w["y0"] for w in ws if w["y1"] > w["y0"])
    tol = (heights[len(heights) // 2] if heights else 10) * 0.6
    lines, cur = [], [ws[0]]
    cur_y = (ws[0]["y0"] + ws[0]["y1"]) / 2
    for w in ws[1:]:
        cy = (w["y0"] + w["y1"]) / 2
        if abs(cy - cur_y) <= tol:
            cur.append(w)
        else:
            lines.append(_mk_line(cur))
            cur, cur_y = [w], cy
    lines.append(_mk_line(cur))
    return lines


def _looks_like_name(text: str) -> bool:
    """Heuristic: is this line plausibly a recipient name/company (not a sentence)?"""
    t = text.strip()
    if not (2 <= len(t) <= 60) or not re.search(r"[A-Za-z]", t):
        return False
    if len(t.split()) > 7:
        return False
    if any(x in t.lower() for x in ("www.", "http", "@", ".co", ".com", "dear ")):
        return False
    return True


def _extract_recipient(
    lines: list[dict], page_h: int, delivery_postcode: str | None
) -> tuple[str | None, float, str | None]:
    """
    Find the recipient address block — contiguous short lines ending in a postcode,
    in the upper part of the page — and return (name, confidence, block_text).

    The name is the first line of that block. Confidence is highest when the block
    ends in the Mailmark delivery postcode. Heuristic by design; low confidence is
    what triggers the AI fallback.
    """
    if not lines:
        return None, 0.0, None
    region = [ln for ln in lines if ln["y1"] <= page_h * 0.6]  # recipient sits high
    dn = (delivery_postcode or "").replace(" ", "").upper()

    pc_hits = []
    for idx, ln in enumerate(region):
        m = _POSTCODE_RE.search(ln["text"].upper()) or _BARCODE_POSTCODE_RE.search(
            ln["text"].replace(" ", "").upper()
        )
        if m:
            pc_hits.append((idx, ln, m.group(1).replace(" ", "").upper()))
    if not pc_hits:
        return None, 0.0, None

    chosen = next((h for h in pc_hits if dn and h[2] == dn), pc_hits[0])
    idx, pc_line, pc_val = chosen

    block = [pc_line]
    line_h = max(8, pc_line["y1"] - pc_line["y0"])
    j = idx - 1
    while j >= 0 and len(block) < 6:
        above = region[j]
        if block[0]["y0"] - above["y1"] > line_h * 2.0:
            break
        txt = above["text"].strip()
        if txt and (len(txt.split()) > 8 or len(txt) > 70):
            break
        if txt:
            block.insert(0, above)
        j -= 1

    name_line = block[0]["text"].strip()
    block_text = "\n".join(l["text"].strip() for l in block if l["text"].strip())
    if _looks_like_name(name_line):
        conf = 0.85 if (dn and pc_val == dn) else 0.6
        if len(block) < 2:
            conf = min(conf, 0.4)
    else:
        return None, 0.3, block_text
    return name_line, round(conf, 2), block_text


# ---------------------------------------------------------------------------
# Confidence gate — decide AUTO (free) / AI fallback / human REVIEW
# ---------------------------------------------------------------------------

def _assess_confidence(page: dict, match_margin: float | None) -> dict:
    """
    Decide how to handle a document from its extraction signals.
    Returns {"decision": auto|ai|review, "confidence": 0-100, "reasons": [...]}.
    The whole "when to hand off to AI" policy lives here.
    """
    reasons: list[str] = []
    mm = page["barcode_type"] == "mailmark"
    pc = page["postcode"]
    shared = bool(pc) and pc.upper() in _SHARED_POSTCODES
    name = page.get("recipient_name")
    name_conf = page.get("recipient_confidence") or 0.0
    score = page.get("match_score")
    text_len = len(page.get("ocr_text") or "")

    strong_match = bool(score and score >= _MATCH_CUTOFF and (match_margin is None or match_margin >= _MATCH_MARGIN))
    good_name = bool(name and name_conf >= _NAME_CONF_AUTO)

    if mm and pc:
        reasons.append(f"Mailmark barcode → delivery postcode {pc} (deterministic)")
    # AUTO requires confident routing: a client match, or an individual
    # (non-shared) delivery postcode. Extracting a recipient name is NOT enough
    # on its own — we must map it to a client or the routing is a guess.
    if strong_match:
        reasons.append(f"Matched client: {page.get('matched_client')} ({score})")
        return {"decision": "auto", "confidence": 95 if mm else 85, "reasons": reasons}
    if mm and pc and not shared:
        reasons.append("Individual delivery postcode routes directly")
        return {"decision": "auto", "confidence": 90, "reasons": reasons}
    if mm and pc and shared:
        reasons.append("Shared-office postcode → a client match is required to route")
    # We could not confidently map to a client.
    if good_name:
        reasons.append(f"Recipient '{name}' extracted but no client match → human review")
        return {"decision": "review", "confidence": 55, "reasons": reasons}
    if text_len > 200:
        reasons.append("Readable text but no confident recipient → AI extraction")
        return {"decision": "ai", "confidence": 40, "reasons": reasons}
    reasons.append("No usable content → human review")
    return {"decision": "review", "confidence": 10, "reasons": reasons}


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
    if len(raw) < _MIN_BARCODE_LEN:
        return None, "unknown", None
    barcode_type, fields = _classify_and_parse_barcode(raw)
    return raw, barcode_type, fields


def _significant_name(name: str) -> str:
    """
    Drop initials / short tokens so client matching keys on the distinctive
    parts of a name (e.g. 'T M Choudhary' -> 'Choudhary'). Matching the full name
    against a whole page lets single letters ('T', 'M') and common words ('Ltd')
    in the body text produce false matches; significant tokens avoid that.
    """
    tokens = [t for t in re.split(r"\s+", name.replace(".", " ")) if len(t) >= 3]
    return " ".join(tokens) if tokens else name


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

def _png_bytes(img: np.ndarray) -> bytes:
    """Encode a page image as PNG for an AI provider."""
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return buf.getvalue()


def process_pdf(
    pdf_bytes: bytes,
    client_list: list[str] | None = None,
    dpi: int = 300,
    enable_ai: bool = False,
    ai_prefer: str | None = None,
) -> dict[str, Any]:
    """
    Process a PDF scan and return structured per-page results.

    Args:
        pdf_bytes:   Raw PDF file bytes.
        client_list: Optional list of known client names for fuzzy matching.
        dpi:         Render DPI — 300 is optimal for OCR, lower is faster.
        enable_ai:   If True, pages the confidence gate routes to 'ai' are sent to
                     the AI fallback (app.ai_fallback). Off by default.
        ai_prefer:   Preferred AI provider name (e.g. 'textract'); else first available.

    Each page dict contains: page, ocr_text, postcode, address_components, barcode,
    barcode_type, barcode_fields, matched_client, match_score, recipient_name,
    recipient_confidence, decision ('auto'|'ai'|'review'), confidence (0-100),
    reasons[list], and ai (provider result dict or None).
    """
    from rapidfuzz import fuzz
    from .ai_fallback import ai_extract

    pages = []

    # Stream pages one at a time — only the result dicts (small JSON) accumulate;
    # page bitmaps are processed and discarded as we go.
    for i, img in enumerate(_iter_pdf_images(pdf_bytes, dpi=dpi)):
        processed = _preprocess(img)
        ocr_text, words = _ocr_with_hocr(processed)

        ocr_postcode, address_components = _extract_postcode(ocr_text)

        # Barcode decode on original (not preprocessed) image
        barcode, barcode_type, barcode_fields = _decode_barcode(img)
        barcode_postcode = (barcode_fields or {}).get("postcode")

        # A Mailmark/stamp barcode encodes the machine-readable delivery
        # (recipient) postcode — the authoritative routing destination, far more
        # reliable than a regex over a dense page. Prefer it; OCR is the fallback.
        postcode = barcode_postcode or ocr_postcode

        # Recipient name + address block (uses hOCR word positions).
        recipient_name, recipient_conf, recipient_block = _extract_recipient(
            _group_lines(words), processed.shape[0], postcode
        )

        # Client match — scoped to the recipient ADDRESS BLOCK, not the whole page.
        # Matching the full page false-positives on generic tokens ("Services",
        # "Limited") that occur in body text; the block holds only the addressee,
        # so a hit means the client really is the recipient. No block → no match
        # (the page goes to AI/review instead of risking a wrong auto-route).
        matched_client: str | None = None
        match_score: float | None = None
        match_margin: float | None = None
        if client_list and recipient_block:
            block_upper = recipient_block.upper()
            scored = sorted(
                (
                    (fuzz.partial_ratio(_significant_name(c).upper(), block_upper), c)
                    for c in client_list
                ),
                key=lambda t: t[0],
                reverse=True,
            )
            if scored and scored[0][0] >= _MATCH_CUTOFF:
                best_score, matched_client = scored[0]
                second = scored[1][0] if len(scored) > 1 else 0.0
                match_score = round(best_score, 1)
                match_margin = round(best_score - second, 1)

        page: dict[str, Any] = {
            "page": i + 1,
            "ocr_text": ocr_text.strip(),
            "postcode": postcode,
            "address_components": address_components,
            "barcode": barcode,
            "barcode_type": barcode_type,
            "barcode_fields": barcode_fields,
            "recipient_name": recipient_name,
            "recipient_confidence": recipient_conf,
            "recipient_block": recipient_block,
            "matched_client": matched_client,
            "match_score": match_score,
            "ai": None,
        }

        assessment = _assess_confidence(page, match_margin)

        # Hand off to AI only when the gate says so (and AI is enabled).
        if enable_ai and assessment["decision"] == "ai":
            ai = ai_extract(
                _png_bytes(img),
                {"ocr_text": ocr_text, "postcode": postcode},
                prefer=ai_prefer,
            )
            if ai is not None:
                page["ai"] = ai.as_dict()
                if ai.recipient_name and recipient_conf < _NAME_CONF_AUTO:
                    page["recipient_name"] = ai.recipient_name
                    page["recipient_confidence"] = ai.confidence
                if ai.postcode and not page["postcode"]:
                    page["postcode"] = ai.postcode
                assessment = _assess_confidence(page, match_margin)
                if assessment["decision"] == "ai":
                    # AI ran but still couldn't resolve confidently → human review.
                    assessment["decision"] = "review"
                    assessment["reasons"].append(f"AI ({ai.provider}) inconclusive → human review")
                else:
                    assessment["reasons"].append(f"AI ({ai.provider}) resolved recipient")

        page["decision"] = assessment["decision"]
        page["confidence"] = assessment["confidence"]
        page["reasons"] = assessment["reasons"]
        pages.append(page)

    return {
        "page_count": len(pages),
        "pages": pages,
    }
