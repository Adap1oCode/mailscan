"""
Tests for batch splitting + processing (app/batch.py).
Builds a synthetic batch PDF in memory: letters separated by MVOS-DOC-SEP
separator sheets (real Data Matrix, as the scanner produces), plus blank backs.
"""
import io

import fitz  # PyMuPDF
from PIL import Image
from pylibdmtx.pylibdmtx import encode as dmtx_encode


def _separator_png() -> bytes:
    """A centred MVOS-DOC-SEP Data Matrix, as printed on a real separator sheet."""
    enc = dmtx_encode(b"MVOS-DOC-SEP")
    img = Image.frombytes("RGB", (enc.width, enc.height), enc.pixels)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _dense_text(label: str) -> str:
    # Enough ink that the page clears the blank threshold (sparse single lines
    # read as near-blank at pixel level, like a real faint page would).
    return "\n".join(f"{label} — line {i} of substantive letter body content." for i in range(40))


def _make_batch_pdf() -> bytes:
    """[letter1 page] [separator] [blank back] [letter2 page a] [letter2 page b]"""
    sep_png = _separator_png()
    doc = fitz.open()

    p = doc.new_page()
    p.insert_text((72, 72), _dense_text("Letter one LU1 1AA"), fontsize=11)

    sep = doc.new_page()  # separator sheet: small centred barcode, nothing else
    w, h = sep.rect.width, sep.rect.height
    sep.insert_image(fitz.Rect(w * 0.38, h * 0.38, w * 0.62, h * 0.55), stream=sep_png)

    doc.new_page()  # blank duplex back of the separator — must be dropped

    for suffix in ("page A", "page B"):
        p = doc.new_page()
        p.insert_text((72, 72), _dense_text(f"Letter two {suffix} LU4 8DP"), fontsize=11)

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_split_batch_groups_letters_and_drops_blanks():
    from app.batch import split_batch

    result = split_batch(_make_batch_pdf())
    assert result["page_count"] == 5
    assert result["separators"] == [2]
    docs = result["documents"]
    assert [d["pages"] for d in docs] == [[1], [4, 5]]
    # split-only: extraction fields deferred
    assert docs[0]["decision"] == "review"
    assert docs[0]["ocr"] == []


def test_split_batch_reports_progress():
    from app.batch import split_batch

    steps = []
    split_batch(_make_batch_pdf(), on_progress=lambda s, c, t: steps.append((s, c, t)))
    assert ("scan", 5, 5) in steps
    assert any(s == "split" for s, _, _ in steps)


def test_process_batch_ocrs_content_pages_only():
    from app.batch import process_batch

    steps = []
    result = process_batch(
        _make_batch_pdf(), on_progress=lambda s, c, t: steps.append((s, c, t))
    )
    assert result["page_count"] == 5
    assert result["separators"] == [2]
    docs = result["documents"]
    assert [d["pages"] for d in docs] == [[1], [4, 5]]

    # OCR ran on the 3 content pages, never on the separator or blank back
    ocr_totals = {t for s, _, t in steps if s == "ocr"}
    assert ocr_totals == {3}
    assert "Letter one" in docs[0]["ocr"][0]["text"]
    assert len(docs[1]["ocr"]) == 2

    # per-letter AI progress reported even with no AI creds
    assert ("ai", 2, 2) in steps
    # no creds → no summary, tier from free stack only
    assert docs[0]["summary"] is None
