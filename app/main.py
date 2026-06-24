"""
Mailscan microservice — FastAPI HTTP wrapper.
Endpoints:
  GET  /health         — liveness check, no auth
  POST /process        — upload PDF, returns job_id (async)
  GET  /jobs/{job_id}  — poll job status + result
  POST /process/sync   — upload PDF, block until result (for simple callers)
"""
import base64
import json
import os
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Security, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.security.api_key import APIKeyHeader

from .pipeline import process_pdf

app = FastAPI(
    title="Mailscan",
    version="2.0.0",
    description="PDF mail scan → OCR + barcode + client matching",
)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _require_api_key(key: str | None = Security(_api_key_header)) -> None:
    api_key = os.environ.get("MAILSCAN_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="MAILSCAN_API_KEY is not configured on the server")
    if key != api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


def _validate_upload(file: UploadFile, dpi: int) -> None:
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    if dpi < 72 or dpi > 600:
        raise HTTPException(status_code=400, detail="dpi must be between 72 and 600")


def _parse_creds(ai_credentials: str) -> dict | None:
    """Parse the AI-credentials bundle MVOS passes (org_integrations) — JSON string."""
    if not ai_credentials or not ai_credentials.strip():
        return None
    try:
        parsed = json.loads(ai_credentials)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _run_pipeline(
    pdf_bytes: bytes,
    client_list: list[str] | None,
    dpi: int,
    separate: bool,
    enable_ai: bool,
    ai_credentials: str,
    ai_prefer: str,
) -> dict[str, Any]:
    """Dispatch to the batch separator pipeline or the per-page pipeline."""
    creds = _parse_creds(ai_credentials)
    prefer = ai_prefer.strip() or None
    if separate:
        from .batch import process_batch

        return process_batch(
            pdf_bytes, client_list=client_list, dpi=dpi, ai_credentials=creds, ai_prefer=prefer
        )
    return process_pdf(
        pdf_bytes,
        client_list=client_list,
        dpi=dpi,
        enable_ai=enable_ai,
        ai_prefer=prefer,
        ai_credentials=creds,
    )


def _get_celery() -> Any | None:
    """Return Celery app if Redis is configured, otherwise None (sync fallback)."""
    if not os.environ.get("REDIS_URL"):
        return None
    try:
        from .worker import celery_app
        return celery_app
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/process")
async def process_async(
    file: UploadFile = File(..., description="PDF file to process"),
    clients: str = Form(default="", description="Comma-separated client names for fuzzy matching"),
    dpi: int = Form(default=300, description="Render DPI — 300 optimal, lower is faster"),
    separate: bool = Form(default=False, description="Split a multi-letter batch on MVOS-DOC-SEP and return documents[]"),
    enable_ai: bool = Form(default=False, description="Allow AI fallback on low-confidence pages/letters"),
    ai_credentials: str = Form(default="", description="JSON bundle of AI provider creds (from MVOS org_integrations)"),
    ai_prefer: str = Form(default="", description="Preferred AI provider (e.g. 'openrouter')"),
    _: None = Security(_require_api_key),
) -> dict[str, Any]:
    """
    Submit a PDF for processing. Returns a job_id to poll with GET /jobs/{job_id}.

    With separate=true the result is a batch: {page_count, documents:[...]} — one
    entry per letter (separated, extracted, AI-resolved, summarised).

    If Redis is not configured (REDIS_URL not set), falls back to synchronous
    processing and returns the result directly (same shape as GET /jobs/{job_id}
    with status='complete').
    """
    _validate_upload(file, dpi)
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    client_list = [c.strip() for c in clients.split(",") if c.strip()] if clients else None
    celery = _get_celery()

    if celery is not None:
        # Async path — submit to Celery
        from .worker import process_pdf_task
        pdf_b64 = base64.b64encode(pdf_bytes).decode()
        task = process_pdf_task.delay(
            pdf_b64,
            client_list=client_list,
            dpi=dpi,
            separate=separate,
            enable_ai=enable_ai,
            ai_credentials=ai_credentials,
            ai_prefer=ai_prefer,
        )
        return {"job_id": task.id, "status": "pending"}

    # Sync fallback — no Redis configured. Run the CPU-bound pipeline in a
    # threadpool so it doesn't block the event loop (and other requests / health).
    try:
        result = await run_in_threadpool(
            _run_pipeline, pdf_bytes, client_list, dpi, separate, enable_ai, ai_credentials, ai_prefer
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {"job_id": None, "status": "complete", "result": result}


@app.get("/jobs/{job_id}")
def job_status(
    job_id: str,
    _: None = Security(_require_api_key),
) -> dict[str, Any]:
    """
    Poll job status and result.

    Response:
      { "job_id": str, "status": "pending"|"processing"|"complete"|"error", "result": dict|null }
    """
    celery = _get_celery()
    if celery is None:
        raise HTTPException(status_code=404, detail="Async jobs not available — REDIS_URL not configured")

    from celery.result import AsyncResult
    task = AsyncResult(job_id, app=celery)

    state = task.state
    if state == "PENDING":
        return {"job_id": job_id, "status": "pending", "result": None}
    if state == "STARTED" or state == "PROCESSING":
        return {"job_id": job_id, "status": "processing", "result": None, "progress": None}
    if state == "PROGRESS":
        info = task.info or {}
        return {
            "job_id": job_id,
            "status": "processing",
            "result": None,
            "progress": {
                "step": info.get("step", ""),
                "current": int(info.get("current", 0)),
                "total": int(info.get("total", 0)),
            },
        }
    if state == "SUCCESS":
        return {"job_id": job_id, "status": "complete", "result": task.result}
    if state == "FAILURE":
        return {"job_id": job_id, "status": "error", "result": None, "error": str(task.result)}

    return {"job_id": job_id, "status": state.lower(), "result": None}


@app.post("/ai/letter")
async def ai_letter(
    ocr_text: str = Form(..., description="Full OCR text of the letter pages (concatenated)"),
    ai_credentials: str = Form(default="", description="JSON bundle of AI provider creds"),
    ai_prefer: str = Form(default="openrouter", description="Preferred AI provider"),
    _: None = Security(_require_api_key),
) -> dict[str, Any]:
    """
    Run AI extraction + summary on a single letter using its OCR text only (no PDF needed).
    Used for per-letter AI re-runs without re-processing the whole batch.

    Returns:
      { recipient_name: str|null, summary: {mail_type, sender, summary, action_required}|null }
    """
    import json as _json

    creds: dict = {}
    if ai_credentials.strip():
        try:
            creds = _json.loads(ai_credentials)
        except Exception:
            pass
    prefer = ai_prefer.strip() or "openrouter"

    from .ai_fallback import ai_extract, ai_summarise

    extraction = None
    if creds.get("openrouter") or creds.get("textract"):
        extraction = ai_extract(b"", {"ocr_text": ocr_text, "credentials": creds}, prefer=prefer)

    summary_obj = None
    if creds.get("openrouter"):
        summary_obj = ai_summarise(ocr_text, {"credentials": creds})

    return {
        "recipient_name": extraction.recipient_name if extraction else None,
        "summary": {
            "mail_type": summary_obj.mail_type if summary_obj else None,
            "sender": summary_obj.sender if summary_obj else None,
            "summary": summary_obj.summary if summary_obj else None,
            "action_required": summary_obj.action_required if summary_obj else None,
        } if summary_obj else None,
    }


@app.post("/process/sync")
async def process_sync(
    file: UploadFile = File(..., description="PDF file to process"),
    clients: str = Form(default="", description="Comma-separated client names for fuzzy matching"),
    dpi: int = Form(default=300, description="Render DPI — 300 optimal, lower is faster"),
    separate: bool = Form(default=False, description="Split a multi-letter batch on MVOS-DOC-SEP and return documents[]"),
    enable_ai: bool = Form(default=False, description="Allow AI fallback on low-confidence pages/letters"),
    ai_credentials: str = Form(default="", description="JSON bundle of AI provider creds (from MVOS org_integrations)"),
    ai_prefer: str = Form(default="", description="Preferred AI provider (e.g. 'openrouter')"),
    _: None = Security(_require_api_key),
) -> dict[str, Any]:
    """
    Synchronous endpoint — blocks until processing is complete and returns result directly.
    Use for simple integrations (n8n, scripts) that don't want to poll.
    May timeout on large PDFs — use POST /process + GET /jobs/{id} for production.
    """
    _validate_upload(file, dpi)
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    client_list = [c.strip() for c in clients.split(",") if c.strip()] if clients else None

    # Run the CPU-bound pipeline in a threadpool so a heavy OCR job doesn't block
    # the event loop — otherwise one large PDF freezes the whole service (health
    # checks included) until it finishes.
    try:
        result = await run_in_threadpool(
            _run_pipeline, pdf_bytes, client_list, dpi, separate, enable_ai, ai_credentials, ai_prefer
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return result
