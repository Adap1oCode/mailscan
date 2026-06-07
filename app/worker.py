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
) -> dict:
    """
    Process a PDF scan asynchronously.
    PDF bytes are base64-encoded for JSON serialisation over Redis.
    """
    import base64
    from .pipeline import process_pdf

    self.update_state(state="PROCESSING")
    pdf_bytes = base64.b64decode(pdf_b64)
    return process_pdf(pdf_bytes, client_list=client_list, dpi=dpi)
