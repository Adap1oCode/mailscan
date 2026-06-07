"""
Unit tests for pipeline.py.
Uses a minimal in-memory PDF so no test fixture file is needed.
"""
import io
import fitz  # PyMuPDF


def _make_pdf(text: str) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_process_pdf_returns_expected_shape():
    from app.pipeline import process_pdf

    result = process_pdf(_make_pdf("Test letter content"))

    assert "page_count" in result
    assert "pages" in result
    assert result["page_count"] == 1
    assert len(result["pages"]) == 1

    page = result["pages"][0]
    assert page["page"] == 1
    assert "ocr_text" in page
    assert "postcode" in page
    assert "address_components" in page
    assert "barcode" in page
    assert "barcode_type" in page
    assert "barcode_fields" in page
    assert "matched_client" in page
    assert "match_score" in page


def test_postcode_extraction():
    from app.pipeline import process_pdf

    result = process_pdf(_make_pdf("Mr John Smith\n14 High Street\nLuton LU1 1AA"))
    assert result["pages"][0]["postcode"] == "LU1 1AA"


def test_no_postcode_returns_none():
    from app.pipeline import process_pdf

    result = process_pdf(_make_pdf("No address here, just random text."))
    assert result["pages"][0]["postcode"] is None


def test_client_fuzzy_match():
    from app.pipeline import process_pdf

    result = process_pdf(
        _make_pdf("Dear Acme Industries Ltd\nPlease find enclosed..."),
        client_list=["Acme Industries Ltd", "Beta Corp", "Gamma LLC"],
    )
    page = result["pages"][0]
    assert page["matched_client"] == "Acme Industries Ltd"
    assert page["match_score"] is not None
    assert page["match_score"] > 70


def test_no_clients_returns_none_match():
    from app.pipeline import process_pdf

    result = process_pdf(_make_pdf("Some letter content"), client_list=None)
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

    result = process_pdf(buf.getvalue())
    assert result["page_count"] == 3
    assert len(result["pages"]) == 3
    assert result["pages"][0]["page"] == 1
    assert result["pages"][2]["page"] == 3


def test_barcode_type_is_unknown_when_no_barcode():
    from app.pipeline import process_pdf

    result = process_pdf(_make_pdf("Simple letter LU1 1AA"))
    page = result["pages"][0]
    assert page["barcode"] is None
    assert page["barcode_type"] == "unknown"
    assert page["barcode_fields"] is None


def test_address_components_none_when_regex_parser():
    """When ADDRESS_PARSER=regex (default), address_components should be None."""
    import os
    os.environ["ADDRESS_PARSER"] = "regex"

    from app.pipeline import process_pdf
    result = process_pdf(_make_pdf("14 High Street Luton LU1 1AA"))
    assert result["pages"][0]["address_components"] is None
