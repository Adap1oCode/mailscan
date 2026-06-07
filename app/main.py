"""
Mailscan microservice — FastAPI HTTP wrapper.
Endpoints:
  GET  /health   — liveness check, no auth
  POST /process  — upload PDF, get structured results, requires X-API-Key
"""
import os
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Security, UploadFile
from fastapi.security.api_key import APIKeyHeader

from .pipeline import process_pdf

app = FastAPI(
    title="Mailscan",
    version="1.0.0",
    description="PDF mail scan → OCR + barcode + client matching",
)

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def _require_api_key(key: str | None = Security(_api_key_header)) -> None:
    api_key = os.environ.get("MAILSCAN_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=500, detail="MAILSCAN_API_KEY is not configured on the server")
    if key != api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/process")
async def process(
    file: UploadFile = File(..., description="PDF file to process"),
    clients: str = Form(default="", description="Comma-separated client names for fuzzy matching"),
    dpi: int = Form(default=300, description="Render DPI — 300 optimal, lower is faster"),
    _: None = Security(_require_api_key),
) -> dict[str, Any]:
    """
    Accept a scanned PDF, return per-page OCR text, postcode, barcode, and
    optional fuzzy-matched client name.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    if dpi < 72 or dpi > 600:
        raise HTTPException(status_code=400, detail="dpi must be between 72 and 600")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    client_list = [c.strip() for c in clients.split(",") if c.strip()] if clients else None

    try:
        result = process_pdf(pdf_bytes, client_list=client_list, dpi=dpi)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return result
