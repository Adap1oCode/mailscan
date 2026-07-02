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

    # Matching is scoped to the recipient address block, so the addressee must
    # appear in an address block (name + lines + postcode), as on a real letter.
    result = process_pdf(
        _make_pdf("Acme Industries Ltd\n14 High Street\nLuton LU1 1AA\n\nDear Sir, Please find enclosed..."),
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


# --- Recipient extraction + confidence gate + AI fallback ------------------

def test_page_has_decision_and_recipient_fields():
    from app.pipeline import process_pdf
    page = process_pdf(_make_pdf("Mr John Smith\n14 High Street\nLuton LU1 1AA"))["pages"][0]
    assert page["decision"] in ("auto", "ai", "review")
    assert isinstance(page["confidence"], int)
    assert isinstance(page["reasons"], list) and page["reasons"]
    assert "recipient_name" in page
    assert "recipient_confidence" in page
    assert "ai" in page


def test_assess_individual_mailmark_postcode_needs_client_match():
    from app.pipeline import _assess_confidence
    # A bare delivery postcode + name with NO client match must not AUTO — routing
    # needs to know WHICH client (auto-ing on postcode alone routed to client=None).
    page = {
        "barcode_type": "mailmark", "postcode": "LU4 8DP", "ocr_text": "x" * 300,
        "recipient_name": "Mr T Choudhary", "recipient_confidence": 0.85, "match_score": None,
    }
    assert _assess_confidence(page, None)["decision"] == "ai"
    # With a strong client match it does AUTO.
    matched = {**page, "matched_client": "Mr T Choudhary", "match_score": 100.0}
    assert _assess_confidence(matched, 100.0)["decision"] == "auto"


def test_assess_shared_office_requires_client_match():
    from app.pipeline import _assess_confidence
    base = {"barcode_type": "mailmark", "postcode": "LU1 2DW", "ocr_text": "x" * 300}
    # shared postcode, no recipient at all → AI to extract one
    no_name = _assess_confidence({**base, "recipient_name": None, "recipient_confidence": 0.0, "match_score": None}, None)
    assert no_name["decision"] == "ai"
    # shared postcode, recipient extracted but NOT matched → AI gets a chance first
    name_no_match = _assess_confidence({**base, "recipient_name": "Acme Ltd", "recipient_confidence": 0.85, "match_score": None}, None)
    assert name_no_match["decision"] == "ai"
    # shared postcode + strong client match → auto
    matched = _assess_confidence({**base, "recipient_name": "Acme Ltd", "recipient_confidence": 0.85, "matched_client": "Acme Ltd", "match_score": 100.0}, 100.0)
    assert matched["decision"] == "auto"


def test_assess_review_on_blank_page():
    from app.pipeline import _assess_confidence
    page = {
        "barcode_type": "unknown", "postcode": None, "ocr_text": "",
        "recipient_name": None, "recipient_confidence": 0.0, "match_score": None,
    }
    assert _assess_confidence(page, None)["decision"] == "review"


def test_ai_fallback_module_mock(monkeypatch):
    # The mock provider is OPT-IN — a silent mock in production could mis-route mail.
    monkeypatch.setenv("MAILSCAN_AI_ENABLE_MOCK", "1")
    from app.ai_fallback import ai_extract, available_providers
    assert "mock" in available_providers()
    res = ai_extract(b"", {"ocr_text": "Acme Ltd\nLondon"})
    assert res is not None and res.provider == "mock"


def test_mock_provider_off_by_default(monkeypatch):
    monkeypatch.delenv("MAILSCAN_AI_ENABLE_MOCK", raising=False)
    from app.ai_fallback import available_providers
    assert "mock" not in available_providers()


def test_ai_fallback_invoked_when_enabled(monkeypatch):
    monkeypatch.setenv("MAILSCAN_AI_ENABLE_MOCK", "1")
    from app.pipeline import process_pdf
    # No barcode + multi-line body (insert_text does not wrap) → gate routes to
    # 'ai'; the mock provider is attempted.
    body = "\n".join(f"This is letter body line number {i} with some content." for i in range(8))
    page = process_pdf(_make_pdf(body), enable_ai=True)["pages"][0]
    assert page["ai"] is not None
    assert page["ai"]["provider"] == "mock"


# --- Per-request options (tenant-override channel) --------------------------

def test_options_override_prompts_models_limits():
    from app.ai_fallback import (
        DEFAULT_EXTRACT_PROMPT,
        _openrouter_model,
        _opt_int,
        _opt_str,
    )

    ctx = {
        "credentials": {"openrouter": {"api_key": "k", "model": "shared/model"}},
        "options": {
            "prompts": {"extract": "Custom extract prompt"},
            "models": {"summary": "org/summary-model"},
            "limits": {"extract_chars": 1234},
        },
    }
    # prompt: per-request override wins; absent → default
    assert _opt_str(ctx, "prompts", "extract") == "Custom extract prompt"
    assert _opt_str(ctx, "prompts", "summary") is None
    assert DEFAULT_EXTRACT_PROMPT  # default exists for the fallback path
    # model: per-task override beats the shared creds model; extract falls back
    assert _openrouter_model(ctx, task="summary") == ("k", "org/summary-model")
    assert _openrouter_model(ctx, task="extract") == ("k", "shared/model")
    # limits: valid override applies; invalid/absent → default
    assert _opt_int(ctx, "limits", "extract_chars", 6000) == 1234
    assert _opt_int(ctx, "limits", "summary_chars", 8000) == 8000
    assert _opt_int({"options": {"limits": {"extract_chars": "junk"}}}, "limits", "extract_chars", 6000) == 6000


def test_options_override_match_cutoff():
    from app.pipeline import process_pdf

    pdf = _make_pdf(
        "Acme Industries Ltd\n14 High Street\nLuton LU1 1AA\n\nDear Sir, Please find enclosed..."
    )
    clients = ["Acme Industries Ltd", "Beta Corp"]

    # Default cutoff matches this letter (see test_client_fuzzy_match)
    assert process_pdf(pdf, client_list=clients)["pages"][0]["matched_client"] is not None
    # An impossible per-request cutoff suppresses the match
    strict = process_pdf(pdf, client_list=clients, options={"match": {"cutoff": 101}})
    assert strict["pages"][0]["matched_client"] is None


def test_options_shared_postcodes_override():
    from app.pipeline import MatchSettings, _assess_confidence

    page = {
        "barcode_type": "mailmark", "postcode": "LU4 8DP", "ocr_text": "x" * 300,
        "recipient_name": "Acme Ltd", "recipient_confidence": 0.85, "match_score": None,
    }
    default = _assess_confidence(page, None)
    treated_shared = _assess_confidence(
        page, None, MatchSettings({"match": {"shared_postcodes": ["LU4 8DP"]}})
    )
    # Both go to AI (no client match), but the shared-postcode reason changes
    assert default["decision"] == treated_shared["decision"] == "ai"
    assert any("Shared-office" in r for r in treated_shared["reasons"])
    assert not any("Shared-office" in r for r in default["reasons"])


def test_options_invalid_values_fall_back_to_defaults():
    from app.pipeline import MatchSettings

    s = MatchSettings({"match": {"cutoff": "junk", "shared_postcodes": "not-a-list"}})
    d = MatchSettings(None)
    assert s.cutoff == d.cutoff
    assert s.shared_postcodes == d.shared_postcodes
