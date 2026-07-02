"""
Integration tests for the FastAPI HTTP endpoints.
Runs against the app in-process using TestClient — no running server needed.
"""
import io
import os
import time

import fitz  # PyMuPDF
from fastapi.testclient import TestClient

os.environ.setdefault("MAILSCAN_API_KEY", "test-key-123")
# Ensure no Redis is configured so tests run the in-process job registry
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


def _submit_and_wait(files: dict, data: dict | None = None, timeout: float = 120.0) -> dict:
    """POST /process (no-Redis path returns a job_id) and poll /jobs to completion."""
    resp = client.post("/process", files=files, data=data or {}, headers={"X-API-Key": VALID_KEY})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] in ("processing", "pending")
    job_id = body["job_id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        poll = client.get(f"/jobs/{job_id}", headers={"X-API-Key": VALID_KEY}).json()
        if poll["status"] in ("complete", "error"):
            return poll
        time.sleep(0.2)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


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
# /process upload caps
# ---------------------------------------------------------------------------

def test_process_oversized_file_returns_413(monkeypatch):
    monkeypatch.setenv("MAILSCAN_MAX_UPLOAD_MB", "0.0001")  # ~100-byte cap
    resp = client.post(
        "/process",
        files={"file": ("scan.pdf", _make_pdf("x" * 5000), "application/pdf")},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 413


def test_process_too_many_pages_returns_413(monkeypatch):
    monkeypatch.setenv("MAILSCAN_MAX_PAGES", "1")
    doc = fitz.open()
    for _ in range(3):
        doc.new_page()
    buf = io.BytesIO()
    doc.save(buf)
    resp = client.post(
        "/process",
        files={"file": ("scan.pdf", buf.getvalue(), "application/pdf")},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 413


def test_process_corrupt_pdf_returns_400():
    resp = client.post(
        "/process",
        files={"file": ("scan.pdf", b"not a pdf at all", "application/pdf")},
        headers={"X-API-Key": VALID_KEY},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /process happy path (no Redis → in-process job registry, poll /jobs/{id})
# ---------------------------------------------------------------------------

def test_process_valid_pdf_returns_result():
    body = _submit_and_wait({"file": ("scan.pdf", _make_pdf(), "application/pdf")})
    assert body["status"] == "complete"
    assert body["result"]["page_count"] == 1
    assert len(body["result"]["pages"]) == 1
    assert body["result"]["pages"][0]["page"] == 1


def test_process_result_has_new_fields():
    body = _submit_and_wait({"file": ("scan.pdf", _make_pdf("Test LU1 1AA"), "application/pdf")})
    page = body["result"]["pages"][0]
    assert "barcode_type" in page
    assert "barcode_fields" in page
    assert "address_components" in page
    assert page["barcode_type"] == "unknown"


def test_process_with_clients():
    body = _submit_and_wait(
        {"file": ("scan.pdf", _make_pdf("Acme Ltd\n14 High Street\nLuton LU1 1AA\n\nDear Sir..."), "application/pdf")},
        data={"clients": "Acme Ltd,Beta Corp"},
    )
    page = body["result"]["pages"][0]
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
