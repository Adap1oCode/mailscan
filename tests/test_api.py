"""
Integration tests for the FastAPI HTTP endpoints.
Runs against the app in-process using httpx — no running server needed.
"""
import io
import os

import fitz  # PyMuPDF
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("MAILSCAN_API_KEY", "test-key-123")

from app.main import app  # noqa: E402 — env must be set before import

client = TestClient(app)
VALID_KEY = "test-key-123"


def _make_pdf(text: str = "Test letter LU1 1AA") -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# --- /health ---

def test_health_no_auth():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# --- /process auth ---

def test_process_missing_key_returns_401():
    pdf = _make_pdf()
    resp = client.post("/process", files={"file": ("scan.pdf", pdf, "application/pdf")})
    assert resp.status_code == 401


def test_process_wrong_key_returns_401():
    pdf = _make_pdf()
    resp = client.post(
        "/process",
        files={"file": ("scan.pdf", pdf, "application/pdf")},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


# --- /process validation ---

def test_process_non_pdf_returns_400():
    resp = client.post(
        "/process",
        files={"file": ("image.png", b"fakepng", "image/png")},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 400


def test_process_empty_file_returns_400():
    resp = client.post(
        "/process",
        files={"file": ("empty.pdf", b"", "application/pdf")},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 400


# --- /process happy path ---

def test_process_valid_pdf_returns_results():
    pdf = _make_pdf("Test letter LU1 1AA")
    resp = client.post(
        "/process",
        files={"file": ("scan.pdf", pdf, "application/pdf")},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["page_count"] == 1
    assert len(body["pages"]) == 1
    assert body["pages"][0]["page"] == 1


def test_process_with_clients():
    pdf = _make_pdf("Dear Acme Ltd, please find enclosed...")
    resp = client.post(
        "/process",
        files={"file": ("scan.pdf", pdf, "application/pdf")},
        data={"clients": "Acme Ltd,Beta Corp"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["pages"][0]["matched_client"] is not None


def test_process_invalid_dpi_returns_400():
    pdf = _make_pdf()
    resp = client.post(
        "/process",
        files={"file": ("scan.pdf", pdf, "application/pdf")},
        data={"dpi": "9999"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 400
