"""
Mailscan microservice — FastAPI HTTP wrapper.
Endpoints:
  GET  /health         — liveness check, no auth
  POST /process        — upload PDF, returns job_id (async)
  GET  /jobs/{job_id}  — poll job status + result
  POST /process/sync   — upload PDF, block until result (for simple callers)
"""
import base64
import logging
import os
import secrets
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional

from fastapi import FastAPI, File, Form, HTTPException, Security, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.security.api_key import APIKeyHeader

from .ai_fallback import parse_credentials, parse_json_object
from .pipeline import default_render_dpi, pdf_page_count, process_pdf

logger = logging.getLogger("mailscan.api")

APP_VERSION = "2.3.0"  # + recipient company_number / vat_number extraction (deterministic matching keys)

app = FastAPI(
    title="Mailscan",
    version=APP_VERSION,
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
_JOBS_MAX = int(os.environ.get("MAILSCAN_JOBS_MAX", "64"))
# Finished results (which can be MBs of OCR text each) expire after this many
# seconds even when the registry is under the cap — mirrors Celery result_expires.
_JOB_TTL_SEC = float(os.environ.get("MAILSCAN_JOB_TTL_SEC", "3600"))


def _evict_jobs_locked(keep: str | None = None) -> None:
    """Drop expired finished jobs, then oldest finished jobs while over the cap.
    Caller must hold _JOBS_LOCK. In-flight jobs are never evicted."""
    now = time.monotonic()
    for jid in list(_JOBS):
        job = _JOBS[jid]
        finished_at = job.get("finished_at")
        if jid != keep and finished_at is not None and now - finished_at > _JOB_TTL_SEC:
            del _JOBS[jid]
    if len(_JOBS) > _JOBS_MAX:
        for jid in list(_JOBS):
            if len(_JOBS) <= _JOBS_MAX:
                break
            if jid != keep and _JOBS[jid]["status"] in ("complete", "error"):
                del _JOBS[jid]


def _new_inproc_job() -> str:
    job_id = uuid.uuid4().hex
    with _JOBS_LOCK:
        _JOBS[job_id] = {
            "status": "processing", "progress": None, "result": None, "error": None,
            "finished_at": None,
        }
        _evict_jobs_locked(keep=job_id)
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
            job.update(status="error", error=error, finished_at=time.monotonic())
        else:
            job.update(status="complete", result=result, finished_at=time.monotonic())


def _run_inproc(job_id: str, fn: Callable[[Callable[[str, int, int], None]], Any]) -> None:
    """Run `fn(progress_cb)` on the pool, recording result/error into the registry."""
    def _go() -> None:
        try:
            result = fn(lambda s, c, t: _set_inproc_progress(job_id, s, c, t))
            _finish_inproc_job(job_id, result=result)
        except Exception as exc:  # noqa: BLE001 — surfaced to the poller as job error
            logger.exception("in-process job %s failed", job_id)
            _finish_inproc_job(job_id, error=str(exc))

    _INPROC_EXECUTOR.submit(_go)


def _require_api_key(key: str | None = Security(_api_key_header)) -> None:
    api_key = os.environ.get("MAILSCAN_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="MAILSCAN_API_KEY is not configured on the server")
    if not secrets.compare_digest(key or "", api_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


async def _read_upload(file: UploadFile, dpi: int) -> bytes:
    """
    Validate an uploaded PDF and return its bytes. Rejects non-PDF names, bad DPI,
    empty/oversized payloads, corrupt PDFs, and page counts beyond the cap — a
    500MB scan would otherwise be read fully into memory (and inflated ~33% again
    by base64 on the Celery path).
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")
    if dpi < 72 or dpi > 600:
        raise HTTPException(status_code=400, detail="dpi must be between 72 and 600")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    max_mb = float(os.environ.get("MAILSCAN_MAX_UPLOAD_MB", "100"))
    if len(pdf_bytes) > max_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"PDF exceeds the {max_mb:g} MB upload limit")

    try:
        page_count = pdf_page_count(pdf_bytes)
    except Exception:
        raise HTTPException(status_code=400, detail="File is not a readable PDF")
    max_pages = int(os.environ.get("MAILSCAN_MAX_PAGES", "500"))
    if page_count > max_pages:
        raise HTTPException(status_code=413, detail=f"PDF has {page_count} pages — limit is {max_pages}")
    return pdf_bytes


def _run_pipeline(
    pdf_bytes: bytes,
    client_list: list[str] | None,
    dpi: int,
    separate: bool,
    enable_ai: bool,
    ai_credentials: str,
    ai_prefer: str,
    options: str = "",
    on_progress: Optional[Callable[[str, int, int], None]] = None,
) -> dict[str, Any]:
    """Dispatch to the batch separator pipeline or the per-page pipeline."""
    creds = parse_credentials(ai_credentials)
    opts = parse_json_object(options, "options")
    prefer = ai_prefer.strip() or None
    if separate:
        from .batch import process_batch

        return process_batch(
            pdf_bytes, client_list=client_list, dpi=dpi, ai_credentials=creds,
            ai_prefer=prefer, on_progress=on_progress, options=opts,
        )
    return process_pdf(
        pdf_bytes,
        client_list=client_list,
        dpi=dpi,
        enable_ai=enable_ai,
        ai_prefer=prefer,
        ai_credentials=creds,
        on_progress=on_progress,
        options=opts,
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
    # version lets callers detect a stale deployment (e.g. one without the
    # /split endpoint or the v2 summary contract) instead of guessing from 404s.
    return {"status": "ok", "version": APP_VERSION}


@app.post("/process")
async def process_async(
    file: UploadFile = File(..., description="PDF file to process"),
    clients: str = Form(default="", description="Comma-separated client names for fuzzy matching"),
    dpi: int = Form(default=0, description="Render DPI — defaults to MAILSCAN_RENDER_DPI"),
    separate: bool = Form(default=False, description="Split a multi-letter batch on MVOS-DOC-SEP and return documents[]"),
    enable_ai: bool = Form(default=False, description="Allow AI fallback on low-confidence pages/letters"),
    ai_credentials: str = Form(default="", description="JSON bundle of AI provider creds (from MVOS org_integrations)"),
    ai_prefer: str = Form(default="", description="Preferred AI provider (e.g. 'openrouter')"),
    options: str = Form(default="", description="Per-request override JSON: prompts/models/limits/match/split"),
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
    pdf_bytes = await _read_upload(file, dpi)

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
            options=options,
        )
        return {"job_id": task.id, "status": "pending"}

    # No Redis configured — run the CPU-bound pipeline on the in-process async
    # registry and hand back a job_id immediately. This is what stops a heavy OCR
    # pass (≈20s for a single letter) from being one long blocking HTTP request
    # that a proxy could time out: the caller polls GET /jobs/{id} for progress and
    # the result instead. (Single uvicorn process, so the registry is shared.)
    job_id = _new_inproc_job()
    _run_inproc(
        job_id,
        lambda cb: _run_pipeline(
            pdf_bytes, client_list, dpi, separate, enable_ai, ai_credentials, ai_prefer,
            options=options, on_progress=cb,
        ),
    )
    return {"job_id": job_id, "status": "processing"}


@app.post("/split")
async def split_async(
    file: UploadFile = File(..., description="Batch PDF to split into letters"),
    dpi: int = Form(default=0, description="Render DPI — defaults to MAILSCAN_RENDER_DPI"),
    options: str = Form(default="", description="Per-request override JSON: split thresholds"),
    _: None = Security(_require_api_key),
) -> dict[str, Any]:
    """
    Split-only first step: separate a multi-letter batch into per-letter page
    groups, FAST — no OCR, no full-page barcode scan, no matching, no AI. Returns a
    job_id to poll with GET /jobs/{job_id}; progress reports ("scan", page, total)
    then ("split", letter, total). OCR/barcode/AI run later, per letter.

    Async via Celery when REDIS_URL is set; otherwise via the in-process registry.
    """
    dpi_val = dpi or default_render_dpi()
    pdf_bytes = await _read_upload(file, dpi_val)

    celery = _get_celery()
    if celery is not None:
        from .worker import split_batch_task
        pdf_b64 = base64.b64encode(pdf_bytes).decode()
        task = split_batch_task.delay(pdf_b64, dpi=dpi_val, options=options)
        return {"job_id": task.id, "status": "pending"}

    from .batch import split_batch
    opts = parse_json_object(options, "options")
    job_id = _new_inproc_job()
    _run_inproc(
        job_id,
        lambda cb: split_batch(pdf_bytes, dpi=dpi_val, on_progress=cb, options=opts),
    )
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
    options: str = Form(default="", description="Per-request override JSON: prompts/models/limits"),
    _: None = Security(_require_api_key),
) -> dict[str, Any]:
    """
    Run AI extraction + summary on a single letter using its OCR text only (no PDF needed).
    Used for per-letter AI re-runs without re-processing the whole batch.

    Returns:
      { recipient_name: str|null,
        summary: {mail_type, sender, subject, summary, action_required,
                  due_date, reference, amount, account_number,
                  payment_reference}|null,
        summary_error: str|null }   # set when the provider errored (retryable),
                                     # null when the summary is genuinely empty
    """
    creds = parse_credentials(ai_credentials) or {}
    opts = parse_json_object(options, "options") or {}
    prefer = ai_prefer.strip() or "openrouter"

    from .ai_fallback import ai_extract, summarise_letter

    ctx = {"ocr_text": ocr_text, "credentials": creds, "options": opts}

    # These are blocking network calls (retries × 90s timeout) — run them off the
    # event loop or one slow provider freezes the whole service, /health included.
    extraction = None
    if creds.get("openrouter") or creds.get("textract"):
        extraction = await run_in_threadpool(ai_extract, b"", ctx, prefer=prefer)

    summary_obj = None
    summary_error = None
    if creds.get("openrouter"):
        summary_obj, summary_error = await run_in_threadpool(
            summarise_letter, ocr_text, ctx
        )

    return {
        "recipient_name": extraction.recipient_name if extraction else None,
        "company_name": extraction.company if extraction else None,
        "individual_name": extraction.individual_name if extraction else None,
        "address_lines": extraction.address if extraction else None,
        "postcode": extraction.postcode if extraction else None,
        "company_number": extraction.company_number if extraction else None,
        "vat_number": extraction.vat_number if extraction else None,
        "summary": {
            "mail_type": summary_obj.get("mail_type"),
            "sender": summary_obj.get("sender"),
            "subject": summary_obj.get("subject"),
            "summary": summary_obj.get("summary"),
            "action_required": summary_obj.get("action_required"),
            "due_date": summary_obj.get("due_date"),
            "reference": summary_obj.get("reference"),
            "amount": summary_obj.get("amount"),
            "account_number": summary_obj.get("account_number"),
            "payment_reference": summary_obj.get("payment_reference"),
        } if summary_obj else None,
        "summary_error": summary_error,
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
    options: str = Form(default="", description="Per-request override JSON: prompts/models/limits/match/split"),
    _: None = Security(_require_api_key),
) -> dict[str, Any]:
    """
    Synchronous endpoint — blocks until processing is complete and returns result directly.
    Use for simple integrations (n8n, scripts) that don't want to poll.
    May timeout on large PDFs — use POST /process + GET /jobs/{id} for production.
    """
    dpi = dpi or default_render_dpi()
    pdf_bytes = await _read_upload(file, dpi)

    client_list = [c.strip() for c in clients.split(",") if c.strip()] if clients else None

    # Run the CPU-bound pipeline in a threadpool so a heavy OCR job doesn't block
    # the event loop — otherwise one large PDF freezes the whole service (health
    # checks included) until it finishes.
    try:
        result = await run_in_threadpool(
            _run_pipeline, pdf_bytes, client_list, dpi, separate, enable_ai,
            ai_credentials, ai_prefer, options,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return result
