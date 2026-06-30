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
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from fastapi import FastAPI, File, Form, HTTPException, Security, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.security.api_key import APIKeyHeader

from .pipeline import default_render_dpi, process_pdf

app = FastAPI(
    title="Mailscan",
    version="2.0.0",
    description="PDF mail scan → OCR + barcode + client matching",
)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


# ---------------------------------------------------------------------------
# In-process async job registry (no-Redis fallback)
#
# When REDIS_URL is unset there is no Celery worker, yet the clients still want a
# job_id to poll for live progress (the split step reports "page N of M"). The
# service runs as a SINGLE uvicorn process (see Dockerfile — no --workers), so a
# module-level dict guarded by a lock is a safe place to hold job state. Jobs run
# on a small thread pool; a poll reads the latest progress the worker thread wrote.
# State is in-memory only — lost on restart, which is acceptable for these jobs.
# ---------------------------------------------------------------------------

_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_INPROC_EXECUTOR = ThreadPoolExecutor(
    max_workers=int(os.environ.get("MAILSCAN_INPROC_WORKERS", "2"))
)
# Bound the registry so a long-running service can't leak memory across many jobs.
_JOBS_MAX = 64


def _new_inproc_job() -> str:
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "processing", "progress": None, "result": None, "error": None}
        # Evict oldest finished jobs once over the cap (insertion-ordered dict).
        if len(_JOBS) > _JOBS_MAX:
            for old in list(_JOBS):
                if len(_JOBS) <= _JOBS_MAX:
                    break
                if old != job_id and _JOBS[old]["status"] in ("complete", "error"):
                    del _JOBS[old]
    return job_id


def _set_inproc_progress(job_id: str, step: str, current: int, total: int) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is not None:
            job["progress"] = {"step": step, "current": current, "total": total}


def _finish_inproc_job(job_id: str, result: Any = None, error: Optional[str] = None) -> None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job is None:
            return
        if error is not None:
            job.update(status="error", error=error)
        else:
            job.update(status="complete", result=result)


def _run_inproc(job_id: str, fn: Callable[[Callable[[str, int, int], None]], Any]) -> None:
    """Run `fn(progress_cb)` on the pool, recording result/error into the registry."""
    def _go() -> None:
        try:
            result = fn(lambda s, c, t: _set_inproc_progress(job_id, s, c, t))
            _finish_inproc_job(job_id, result=result)
        except Exception as exc:  # noqa: BLE001 — surfaced to the poller as job error
            _finish_inproc_job(job_id, error=str(exc))

    _INPROC_EXECUTOR.submit(_go)


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
    dpi: int = Form(default=0, description="Render DPI — defaults to MAILSCAN_RENDER_DPI"),
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
    dpi = dpi or default_render_dpi()
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


@app.post("/split")
async def split_async(
    file: UploadFile = File(..., description="Batch PDF to split into letters"),
    dpi: int = Form(default=0, description="Render DPI — defaults to MAILSCAN_RENDER_DPI"),
    _: None = Security(_require_api_key),
) -> dict[str, Any]:
    """
    Split-only first step: separate a multi-letter batch into per-letter page
    groups, FAST — no OCR, no full-page barcode scan, no matching, no AI. Returns a
    job_id to poll with GET /jobs/{job_id}; progress reports ("scan", page, total)
    then ("split", letter, total). OCR/barcode/AI run later, per letter.

    Async via Celery when REDIS_URL is set; otherwise via the in-process registry.
    """
    _validate_upload(file, dpi or default_render_dpi())
    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    dpi_val = dpi or default_render_dpi()

    celery = _get_celery()
    if celery is not None:
        from .worker import split_batch_task
        pdf_b64 = base64.b64encode(pdf_bytes).decode()
        task = split_batch_task.delay(pdf_b64, dpi=dpi_val)
        return {"job_id": task.id, "status": "pending"}

    from .batch import split_batch
    job_id = _new_inproc_job()
    _run_inproc(job_id, lambda cb: split_batch(pdf_bytes, dpi=dpi_val, on_progress=cb))
    return {"job_id": job_id, "status": "processing"}


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
    # In-process registry first (no-Redis path) — the job ids it mints live here.
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        snapshot = dict(job) if job is not None else None
    if snapshot is not None:
        return {
            "job_id": job_id,
            "status": snapshot["status"],
            "result": snapshot["result"],
            "progress": snapshot["progress"],
            "error": snapshot["error"],
        }

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
        "company_name": extraction.company if extraction else None,
        "individual_name": extraction.individual_name if extraction else None,
        "address_lines": extraction.address if extraction else None,
        "postcode": extraction.postcode if extraction else None,
        "summary": {
            "mail_type": summary_obj.get("mail_type"),
            "sender": summary_obj.get("sender"),
            "summary": summary_obj.get("summary"),
            "action_required": summary_obj.get("action_required"),
        } if summary_obj else None,
    }


@app.post("/process/sync")
async def process_sync(
    file: UploadFile = File(..., description="PDF file to process"),
    clients: str = Form(default="", description="Comma-separated client names for fuzzy matching"),
    dpi: int = Form(default=0, description="Render DPI — defaults to MAILSCAN_RENDER_DPI"),
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
    dpi = dpi or default_render_dpi()
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
