"""
Core mailscan pipeline — PDF → per-page OCR + barcode results.
No HTTP code here. Called by main.py or directly from CLI/tests.

OCR engine: OCRmyPDF in API mode (hOCR output) for word-level bounding boxes.
Address parsing: libpostal when ADDRESS_PARSER=libpostal, otherwise regex fallback.
Barcode: pylibdmtx for Data Matrix decode, with Mailmark + stamp field parsers.
"""
import logging
import os
import re
import io
import xml.etree.ElementTree as ET
from typing import Any, Callable, Iterator, Optional

import cv2
import fitz  # PyMuPDF
import numpy as np
import pytesseract
from PIL import Image
from pylibdmtx.pylibdmtx import decode as dmtx_decode

logger = logging.getLogger("mailscan.pipeline")

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

# Default page render DPI. 200 is the speed/accuracy sweet spot for this pipeline;
# raise to 300 (env) if OCR accuracy on small print suffers. Callers that pass an
# explicit dpi override this; callers passing dpi<=0 (or None) get this default.
_DEFAULT_DPI = int(os.environ.get("MAILSCAN_RENDER_DPI", "200"))


def default_render_dpi() -> int:
    """The configured default render DPI, clamped to the valid 72–600 range."""
    return max(72, min(600, _DEFAULT_DPI))

# Hard ceiling (ms) for a single full-page Data Matrix (Mailmark) scan. A barcode
# that IS present decodes in <100ms; this ceiling only bounds the wasted time on a
# barcode-free page (libdmtx scans the whole high-DPI image to the deadline on a
# miss). Most pages of a letter have no barcode, so a high ceiling dominates OCR
# latency — keep it tight. Missed barcodes fall back to OCR postcode/recipient.
_DMTX_TIMEOUT_MS = int(os.environ.get("MAILSCAN_DMTX_TIMEOUT_MS", "1500"))

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


class MatchSettings:
    """
    Per-request match/gate thresholds — resolved from the caller's `options`
    payload (options["match"]) with the env-configured values as defaults, so
    each tenant can tune matching without a redeploy.
    """

    def __init__(self, options: dict | None = None) -> None:
        m = (options or {}).get("match") or {}
        self.cutoff = self._num(m.get("cutoff"), _MATCH_CUTOFF)
        self.margin = self._num(m.get("margin"), _MATCH_MARGIN)
        self.name_conf = self._num(m.get("name_conf"), _NAME_CONF_AUTO)
        pcs = m.get("shared_postcodes")
        self.shared_postcodes = (
            {str(p).strip().upper() for p in pcs if str(p).strip()}
            if isinstance(pcs, list)
            else _SHARED_POSTCODES
        )

    @staticmethod
    def _num(v: object, default: float) -> float:
        try:
            return float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return default

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


def _iter_pdf_images(
    pdf_bytes: bytes, dpi: int = 300, page_numbers: list[int] | None = None
) -> Iterator[tuple[int, np.ndarray]]:
    """
    Yield (page_number_1based, grayscale image) one page at a time.

    Grayscale render — OCR, barcode decode, and preprocessing all consume gray,
    so rendering RGB only to convert it wastes ~3x the render time and memory.

    Streaming — rather than building a list of every page up front — keeps peak
    memory at roughly one page regardless of page count. A 300-page batch then
    uses the same RAM as a single-page letter, which is what stops large scans
    OOM-ing the container.

    page_numbers restricts rendering to those 1-based pages (batch mode skips
    separator sheets and blank backs entirely); None renders every page.
    """
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        numbers = page_numbers if page_numbers is not None else range(1, doc.page_count + 1)
        for n in numbers:
            page = doc[n - 1]
            pix = page.get_pixmap(
                dpi=_effective_dpi(page, dpi), colorspace=fitz.csGRAY, alpha=False
            )
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
            # Copy off the pixmap buffer so it can be freed before the next page.
            yield n, img.copy()
            del pix, img
    finally:
        doc.close()


def pdf_page_count(pdf_bytes: bytes) -> int:
    """Page count of a PDF (raises on a corrupt/non-PDF payload)."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc.page_count
    finally:
        doc.close()


# ---------------------------------------------------------------------------
# Image preprocessing
# ---------------------------------------------------------------------------

def _preprocess(gray: np.ndarray) -> np.ndarray:
    """Deskew and binarise a grayscale page — improves OCR accuracy on scanned docs."""
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

    PSM 3 (full automatic layout analysis) is deliberate: UK letters put the
    recipient address and a sender reference panel SIDE BY SIDE, and PSM 6
    ("one uniform block") reads straight across both, splicing them into the
    same line ("Eco Pressure Pro Limited Company number: 23-27 King Street
    14130864"). PSM 3 returns each block as its own hOCR area in reading
    order, and the text is rebuilt line by line with real newlines — so
    downstream consumers (the LLM summary, the search index, the recipient
    heuristic) see coherent blocks, not interleaved columns.
    """
    pil = Image.fromarray(img)
    try:
        hocr_bytes = pytesseract.image_to_pdf_or_hocr(pil, extension="hocr", config="--psm 3")
        full_text, words = _parse_hocr(hocr_bytes)
        if words:
            recovered = _recover_missed_amounts(pil, full_text)
            if recovered:
                full_text = full_text + "\n" + "\n".join(recovered)
            return full_text, words
    except Exception:
        logger.warning("hOCR pass failed — falling back to plain OCR (no word boxes)", exc_info=True)
    return pytesseract.image_to_string(pil, config="--psm 3"), []


_MONEY_RE = re.compile(r"£\s*\d[\d,.\s]*")


def _recover_missed_amounts(pil: Image.Image, main_text: str) -> list[str]:
    """
    PSM 3's layout analysis occasionally classifies a boxed/shaded figure (a
    bank letter's TOTAL cell, a payment slip amount) as a graphic and drops it
    entirely — money we can never afford to lose silently. Run a sparse-text
    pass (PSM 11: find text anywhere, no layout assumptions) and return any
    £-amount lines whose digits are absent from the main text. Costs a second
    OCR pass per page — accepted: completeness of captured amounts outranks
    OCR latency for this pipeline.
    """
    try:
        sparse = pytesseract.image_to_string(pil, config="--psm 11")
    except Exception:
        return []
    main_digits = re.sub(r"\D", "", main_text)
    out: list[str] = []
    for ln in sparse.splitlines():
        if "£" not in ln:
            continue
        for tok in _MONEY_RE.findall(ln):
            digits = re.sub(r"\D", "", tok)
            if len(digits) >= 3 and digits not in main_digits:
                line = ln.strip()
                if line not in out:
                    out.append(line)
                break
    return out


# hOCR line-level classes (ocr_line + its header/caption/float variants).
_HOCR_LINE_CLASSES = ("ocr_line", "ocr_header", "ocr_caption", "ocr_textfloat")


def _parse_hocr(hocr_bytes: bytes) -> tuple[str, list[dict]]:
    """
    Parse hOCR XML into (full_text, word_list).

    full_text preserves Tesseract's layout analysis: one text line per hOCR
    line element, blank line between blocks (ocr_carea) — instead of the old
    flat space-join of every word on the page.
    """
    words: list[dict] = []
    text_lines: list[str] = []
    try:
        root = ET.fromstring(hocr_bytes.decode("utf-8", errors="replace"))

        def _walk(elem: ET.Element) -> None:
            cls = elem.get("class", "")
            if any(c in cls for c in _HOCR_LINE_CLASSES):
                line_words: list[str] = []
                for w in elem.iter():
                    wcls = w.get("class", "")
                    if "ocrx_word" not in wcls and "ocr_word" not in wcls:
                        continue
                    title = w.get("title", "")
                    bbox_match = re.search(r"bbox (\d+) (\d+) (\d+) (\d+)", title)
                    if bbox_match and w.text and w.text.strip():
                        x0, y0, x1, y1 = map(int, bbox_match.groups())
                        words.append({"text": w.text.strip(), "x0": x0, "y0": y0, "x1": x1, "y1": y1})
                        line_words.append(w.text.strip())
                if line_words:
                    text_lines.append(" ".join(line_words))
                return  # words consumed; don't descend further
            if "ocr_carea" in cls and text_lines and text_lines[-1] != "":
                text_lines.append("")  # blank line between layout blocks
            for child in elem:
                _walk(child)

        _walk(root)
    except ET.ParseError:
        pass

    full_text = "\n".join(text_lines).strip()
    return full_text, words


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

def _assess_confidence(
    page: dict, match_margin: float | None, settings: MatchSettings | None = None
) -> dict:
    """
    Decide how to handle a document from its extraction signals.
    Returns {"decision": auto|ai|review, "confidence": 0-100, "reasons": [...]}.
    The whole "when to hand off to AI" policy lives here.
    """
    s = settings or MatchSettings()
    reasons: list[str] = []
    mm = page["barcode_type"] == "mailmark"
    pc = page["postcode"]
    shared = bool(pc) and pc.upper() in s.shared_postcodes
    name = page.get("recipient_name")
    name_conf = page.get("recipient_confidence") or 0.0
    score = page.get("match_score")
    text_len = len(page.get("ocr_text") or "")

    strong_match = bool(score and score >= s.cutoff and (match_margin is None or match_margin >= s.margin))
    good_name = bool(name and name_conf >= s.name_conf)

    if mm and pc:
        reasons.append(f"Mailmark barcode → delivery postcode {pc} (deterministic)")
    # AUTO requires confident routing: a client match, or an individual
    # (non-shared) delivery postcode. Extracting a recipient name is NOT enough
    # on its own — we must map it to a client or the routing is a guess.
    if strong_match:
        reasons.append(f"Matched client: {page.get('matched_client')} ({score})")
        return {"decision": "auto", "confidence": 95 if mm else 85, "reasons": reasons}
    # NOTE: a bare individual (non-shared) delivery postcode is NOT enough to AUTO
    # on its own — routing requires knowing WHICH client. Auto-ing on postcode
    # alone routes mail to nobody (it sent two letters to client=None). Fall through
    # to AI/review so a recipient is actually identified before routing.
    if mm and pc and not shared:
        reasons.append("Individual delivery postcode, but no client match yet → AI/review")
    if mm and pc and shared:
        reasons.append("Shared-office postcode → a client match is required to route")
    # No confident client match. Give AI a chance to extract a matching recipient
    # before giving up — the free-stack name is often the sender, not the
    # addressee, so AI may still find the real recipient. Only if there is no
    # usable content at all do we go straight to review.
    if good_name or text_len > 200:
        if good_name:
            reasons.append(f"Recipient '{name}' extracted but no client match → AI to confirm")
        else:
            reasons.append("Readable text but no confident recipient → AI extraction")
        return {"decision": "ai", "confidence": 45 if good_name else 40, "reasons": reasons}
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


# Legal-form / conjunction tokens that are NOT distinctive — present on most
# company names, so matching on them alone is noise. "K&N Limited" collapsing to
# just "Limited" made it fuzzy-match every "...Limited" recipient (it mis-routed
# "Elsiyam Investment Limited" → "K&N Limited"). Drop these so matching keys on
# the distinctive part of the name.
_GENERIC_NAME_TOKENS = {
    "LTD", "LIMITED", "LLP", "LLC", "PLC", "INC", "CO", "COMPANY", "AND", "THE", "TA",
}


def _significant_name(name: str) -> str:
    """
    Reduce a name to its distinctive tokens for matching. Drops initials / short
    tokens (single letters: 'T M Choudhary' -> 'Choudhary') AND generic legal
    forms ('K&N Limited' -> 'K&N', not 'Limited') so neither stray letters nor
    'Ltd' in body text produce false matches.
    """
    toks = [t for t in re.split(r"\s+", name.replace(".", " ")) if t]
    sig = [t for t in toks if len(t) >= 3 and t.upper() not in _GENERIC_NAME_TOKENS]
    if sig:
        return " ".join(sig)
    # No distinctive long token (e.g. 'K&N Ltd'): keep distinctive short tokens
    # like 'K&N' before falling back to the raw name.
    short = [t for t in toks if t.upper() not in _GENERIC_NAME_TOKENS]
    return " ".join(short) if short else name


def _match_clients(
    text: str | None, client_list: list[str] | None, cutoff: float | None = None
) -> tuple[str | None, float | None, float | None]:
    """
    Match a client list against text (the recipient address block, or an AI-
    extracted block). Returns (matched_client, score, margin) — margin is the gap
    to the 2nd-best candidate (ambiguity check). No match → (None, None, None).
    """
    if not client_list or not text:
        return None, None, None
    from rapidfuzz import fuzz

    min_score = cutoff if cutoff is not None else _MATCH_CUTOFF
    text_upper = text.upper()
    scored = sorted(
        ((fuzz.partial_ratio(_significant_name(c).upper(), text_upper), c) for c in client_list),
        key=lambda t: t[0],
        reverse=True,
    )
    if not scored or scored[0][0] < min_score:
        return None, None, None
    best_score, best_client = scored[0]
    second = scored[1][0] if len(scored) > 1 else 0.0
    return best_client, round(best_score, 1), round(best_score - second, 1)


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
    The first postcode is the delivery (recipient) address; the second is the
    return (sender) address — e.g. BX9 1BB = HMRC, SW1H 9AJ = Companies House.
    Returns whatever fields can be extracted — partial results are valid.
    """
    cleaned = raw.strip()
    fields: dict = {"raw": cleaned}
    try:
        compact = cleaned.replace(" ", "").upper()
        fields["version"] = compact[0] if compact else None  # 'J'
        fields["mail_class"] = compact[1:3] if len(compact) > 2 else None  # 'GB'
        pcs = _BARCODE_POSTCODE_RE.findall(compact)
        if pcs:
            fields["postcode"] = _normalise_postcode(pcs[0])
        if len(pcs) > 1:
            fields["return_postcode"] = _normalise_postcode(pcs[1])
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
    dpi: int | None = None,
    enable_ai: bool = False,
    ai_prefer: str | None = None,
    ai_credentials: dict | None = None,
    page_numbers: list[int] | None = None,
    on_progress: Optional[Callable[[str, int, int], None]] = None,
    options: dict | None = None,
) -> dict[str, Any]:
    """
    Process a PDF scan and return structured per-page results.

    Args:
        pdf_bytes:    Raw PDF file bytes.
        client_list:  Optional list of known client names for fuzzy matching.
        dpi:          Render DPI — 300 is optimal for OCR, lower is faster.
        enable_ai:    If True, pages the confidence gate routes to 'ai' are sent to
                      the AI fallback (app.ai_fallback). Off by default.
        ai_prefer:    Preferred AI provider name (e.g. 'textract'); else first available.
        page_numbers: Optional 1-based page subset to process (batch mode skips
                      separator sheets/blanks). None processes every page.
        on_progress:  Optional callback ("ocr", pages_done, pages_total) — one call
                      per page so long batches show live progress.
        options:      Per-request overrides (prompts/models/limits/match) — the
                      tenant-override channel; see ai_fallback options docs.

    Each page dict contains: page, ocr_text, postcode, address_components, barcode,
    barcode_type, barcode_fields, matched_client, match_score, recipient_name,
    recipient_confidence, decision ('auto'|'ai'|'review'), confidence (0-100),
    reasons[list], and ai (provider result dict or None).
    """
    from .ai_fallback import ai_extract

    if not dpi or dpi <= 0:
        dpi = default_render_dpi()
    settings = MatchSettings(options)
    pages = []
    doc_page_count = pdf_page_count(pdf_bytes)
    total = len(page_numbers) if page_numbers is not None else doc_page_count

    # Stream pages one at a time — only the result dicts (small JSON) accumulate;
    # page bitmaps are processed and discarded as we go.
    for i, (page_no, img) in enumerate(_iter_pdf_images(pdf_bytes, dpi=dpi, page_numbers=page_numbers)):
        if on_progress:
            on_progress("ocr", i, total)
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
        # "Limited") in body text; the block holds only the addressee. No block →
        # no match (the page goes to AI/review instead of risking a wrong route).
        matched_client, match_score, match_margin = _match_clients(
            recipient_block, client_list, cutoff=settings.cutoff
        )

        page: dict[str, Any] = {
            "page": page_no,
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

        assessment = _assess_confidence(page, match_margin, settings)

        # Hand off to AI only when the gate says so (and AI is enabled).
        if enable_ai and assessment["decision"] == "ai":
            ai = ai_extract(
                _png_bytes(img),
                {
                    "ocr_text": ocr_text,
                    "postcode": postcode,
                    "credentials": ai_credentials or {},
                    "options": options or {},
                },
                prefer=ai_prefer,
            )
            if ai is not None:
                page["ai"] = ai.as_dict()
                if ai.recipient_name and recipient_conf < settings.name_conf:
                    page["recipient_name"] = ai.recipient_name
                    page["recipient_confidence"] = ai.confidence
                if ai.postcode and not page["postcode"]:
                    page["postcode"] = ai.postcode
                # Re-match the client list against the AI-extracted address block —
                # this is what turns an AI extraction into an AUTO routing.
                if ai.address:
                    ai_client, ai_score, ai_margin = _match_clients(
                        ai.address, client_list, cutoff=settings.cutoff
                    )
                    if ai_client:
                        page["matched_client"] = matched_client = ai_client
                        page["match_score"] = match_score = ai_score
                        match_margin = ai_margin
                assessment = _assess_confidence(page, match_margin, settings)
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

    if on_progress:
        on_progress("ocr", total, total)

    return {
        # Full document page count even when a subset was processed — callers use
        # this as "pages in the PDF", not "pages in this result".
        "page_count": doc_page_count,
        "pages": pages,
    }
