"""
Unit tests for pipeline.py.
Uses a minimal in-memory PDF so no test fixture file is needed.
"""
import io
import pytest
import fitz  # PyMuPDF


def _make_pdf(text: str) -> bytes:
    """Create a single-page PDF with the given text."""
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_process_pdf_returns_expected_shape():
    from app.pipeline import process_pdf

    pdf = _make_pdf("Test letter content")
    result = process_pdf(pdf)

    assert "page_count" in result
    assert "pages" in result
    assert result["page_count"] == 1
    assert len(result["pages"]) == 1

    page = result["pages"][0]
    assert page["page"] == 1
    assert "ocr_text" in page
    assert "postcode" in page
    assert "barcode" in page
    assert "matched_client" in page
    assert "match_score" in page


def test_postcode_extraction():
    from app.pipeline import process_pdf

    pdf = _make_pdf("Mr John Smith\n14 High Street\nLuton LU1 1AA\nEngland")
    result = process_pdf(pdf)

    assert result["pages"][0]["postcode"] == "LU1 1AA"


def test_no_postcode_returns_none():
    from app.pipeline import process_pdf

    pdf = _make_pdf("No address here, just some random text.")
    result = process_pdf(pdf)

    assert result["pages"][0]["postcode"] is None


def test_client_fuzzy_match():
    from app.pipeline import process_pdf

    pdf = _make_pdf("Dear Acme Industries Ltd\nPlease find enclosed...")
    result = process_pdf(pdf, client_list=["Acme Industries Ltd", "Beta Corp", "Gamma LLC"])

    page = result["pages"][0]
    assert page["matched_client"] == "Acme Industries Ltd"
    assert page["match_score"] is not None
    assert page["match_score"] > 70


def test_no_clients_returns_none_match():
    from app.pipeline import process_pdf

    pdf = _make_pdf("Some letter content")
    result = process_pdf(pdf, client_list=None)

    assert result["pages"][0]["matched_client"] is None
    assert result["pages"][0]["match_score"] is None


def test_multipage_pdf():
    from app.pipeline import process_pdf

    doc = fitz.open()
    for i in range(3):
        page = doc.new_page()
        page.insert_text((72, 72), f"Page {i + 1} content LU{i + 1} 1AA", fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    pdf = buf.getvalue()

    result = process_pdf(pdf)
    assert result["page_count"] == 3
    assert len(result["pages"]) == 3
    assert result["pages"][0]["page"] == 1
    assert result["pages"][2]["page"] == 3
