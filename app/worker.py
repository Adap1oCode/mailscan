"""
Celery worker — async PDF processing tasks.
Start with: celery -A app.worker worker --loglevel=info
"""
import os
from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

celery_app = Celery(
    "mailscan",
    broker=REDIS_URL,
    backend=REDIS_URL,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_track_started=True,
    result_expires=3600,  # results kept in Redis for 1 hour
)


@celery_app.task(bind=True, name="mailscan.process_pdf")
def process_pdf_task(
    self,
    pdf_b64: str,
    client_list: list[str] | None = None,
    dpi: int = 300,
    separate: bool = False,
    enable_ai: bool = False,
    ai_credentials: str = "",
    ai_prefer: str = "",
    options: str = "",
) -> dict:
    """
    Process a PDF scan asynchronously.
    PDF bytes are base64-encoded for JSON serialisation over Redis.
    With separate=true, splits a multi-letter batch and returns documents[].
    """
    import base64

    from .ai_fallback import parse_credentials, parse_json_object

    self.update_state(state="PROCESSING")
    pdf_bytes = base64.b64decode(pdf_b64)
    creds = parse_credentials(ai_credentials)
    opts = parse_json_object(options, "options")
    prefer = ai_prefer.strip() or None

    def _progress(step: str, current: int, total: int) -> None:
        self.update_state(state="PROGRESS", meta={"step": step, "current": current, "total": total})

    if separate:
        from .batch import process_batch

        return process_batch(
            pdf_bytes, client_list=client_list, dpi=dpi, ai_credentials=creds, ai_prefer=prefer,
            on_progress=_progress, options=opts,
        )
    from .pipeline import process_pdf

    return process_pdf(
        pdf_bytes,
        client_list=client_list,
        dpi=dpi,
        enable_ai=enable_ai,
        ai_prefer=prefer,
        ai_credentials=creds,
        on_progress=_progress,
        options=opts,
    )


@celery_app.task(bind=True, name="mailscan.split_batch")
def split_batch_task(self, pdf_b64: str, dpi: int = 0, options: str = "") -> dict:
    """
    Split-only batch separation (no OCR/barcode/match/AI) — the fast first step.
    Reports PROGRESS state as ("scan", page, total) then ("split", letter, total)
    so the poller can show a live "page N of M" / "letter N of M" indicator.
    """
    import base64

    from .ai_fallback import parse_json_object

    self.update_state(state="PROCESSING")
    pdf_bytes = base64.b64decode(pdf_b64)

    from .batch import split_batch

    def _progress(step: str, current: int, total: int) -> None:
        self.update_state(state="PROGRESS", meta={"step": step, "current": current, "total": total})

    return split_batch(
        pdf_bytes, dpi=dpi or None, on_progress=_progress,
        options=parse_json_object(options, "options"),
    )
