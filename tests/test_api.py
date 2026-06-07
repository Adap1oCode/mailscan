"""
Integration tests for the FastAPI HTTP endpoints.
Runs against the app in-process using TestClient — no running server needed.
"""
import io
import os

import fitz  # PyMuPDF
from fastapi.testclient import TestClient

os.environ.setdefault("MAILSCAN_API_KEY", "test-key-123")
# Ensure no Redis is configured so tests run sync fallback
os.environ.pop("REDIS_URL", None)

from app.main import app  # noqa: E402

client = TestClient(app)
VALID_KEY = "test-key-123"


def _make_pdf(text: str = "Test letter LU1 1AA") -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text, fontsize=12)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

def test_health_no_auth():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# /process auth
# ---------------------------------------------------------------------------

def test_process_missing_key_returns_401():
    resp = client.post("/process", files={"file": ("scan.pdf", _make_pdf(), "application/pdf")})
    assert resp.status_code == 401


def test_process_wrong_key_returns_401():
    resp = client.post(
        "/process",
        files={"file": ("scan.pdf", _make_pdf(), "application/pdf")},
        headers={"X-API-Key": "wrong-key"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# /process validation
# ---------------------------------------------------------------------------

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


def test_process_invalid_dpi_returns_400():
    resp = client.post(
        "/process",
        files={"file": ("scan.pdf", _make_pdf(), "application/pdf")},
        data={"dpi": "9999"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /process happy path (sync fallback — no Redis in test env)
# ---------------------------------------------------------------------------

def test_process_valid_pdf_returns_result():
    resp = client.post(
        "/process",
        files={"file": ("scan.pdf", _make_pdf(), "application/pdf")},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Sync fallback returns result directly with status=complete
    assert body["status"] == "complete"
    assert body["result"]["page_count"] == 1
    assert len(body["result"]["pages"]) == 1
    assert body["result"]["pages"][0]["page"] == 1


def test_process_result_has_new_fields():
    resp = client.post(
        "/process",
        files={"file": ("scan.pdf", _make_pdf("Test LU1 1AA"), "application/pdf")},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200
    page = resp.json()["result"]["pages"][0]
    assert "barcode_type" in page
    assert "barcode_fields" in page
    assert "address_components" in page
    assert page["barcode_type"] == "unknown"


def test_process_with_clients():
    resp = client.post(
        "/process",
        files={"file": ("scan.pdf", _make_pdf("Dear Acme Ltd, please find enclosed..."), "application/pdf")},
        data={"clients": "Acme Ltd,Beta Corp"},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200
    page = resp.json()["result"]["pages"][0]
    assert page["matched_client"] is not None


# ---------------------------------------------------------------------------
# /process/sync — always synchronous
# ---------------------------------------------------------------------------

def test_process_sync_returns_result_directly():
    resp = client.post(
        "/process/sync",
        files={"file": ("scan.pdf", _make_pdf("Test LU1 1AA"), "application/pdf")},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 200
    body = resp.json()
    # Sync endpoint returns result shape directly (no job_id wrapper)
    assert "page_count" in body
    assert "pages" in body


# ---------------------------------------------------------------------------
# /jobs/{job_id} — requires Redis, not available in test env
# ---------------------------------------------------------------------------

def test_jobs_endpoint_404_without_redis():
    resp = client.get("/jobs/fake-job-id", headers={"X-API-Key": VALID_KEY})
    assert resp.status_code == 404
