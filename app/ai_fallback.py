"""
AI fallback for mailscan — provider-agnostic recipient extraction.

When the free pipeline isn't confident (no barcode, no clean recipient name), a
page image + its OCR text are handed to a document/vision AI to extract a
structured recipient. Providers are pluggable; a router picks the first available
one (by env credentials) or a caller-specified preference, and always falls back
to a no-key mock so the end-to-end flow runs in tests/demos before keys land.

Wire real providers by filling in each `extract()` (TODOs). The contract — input
(PNG bytes + context) and output (AIResult) — stays fixed, so the pipeline and
the confidence gate never change as providers are added or swapped.

Env keys that activate each provider:
  textract    AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION
  gemini      GEMINI_API_KEY
  claude      ANTHROPIC_API_KEY
  documentai  GOOGLE_APPLICATION_CREDENTIALS, DOCAI_PROCESSOR_ID, DOCAI_LOCATION
  azure       AZURE_DOCINTEL_ENDPOINT, AZURE_DOCINTEL_KEY
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class AIResult:
    """Structured recipient extracted by an AI provider."""
    recipient_name: Optional[str] = None
    company: Optional[str] = None
    address: Optional[str] = None
    postcode: Optional[str] = None
    is_continuation: Optional[bool] = None
    confidence: float = 0.0
    provider: str = "none"
    note: Optional[str] = None

    def as_dict(self) -> dict:
        return asdict(self)


class AIProvider:
    """Base provider. Subclasses set `name`, `available()`, and `extract()`."""
    name = "base"

    def available(self) -> bool:
        return False

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        raise NotImplementedError


class MockProvider(AIProvider):
    """
    No-key stand-in so the full flow is testable before real keys exist.
    Surfaces the first OCR line as a placeholder recipient. NOT a real extraction.
    """
    name = "mock"

    def available(self) -> bool:
        return os.environ.get("MAILSCAN_AI_DISABLE_MOCK") != "1"

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        text = (context or {}).get("ocr_text", "") or ""
        first = next((ln.strip() for ln in text.splitlines() if ln.strip()), None)
        return AIResult(
            recipient_name=first,
            confidence=0.5,
            provider="mock",
            note="mock provider — no real AI key configured",
        )


class TextractProvider(AIProvider):
    name = "textract"

    def available(self) -> bool:
        return bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        # TODO: boto3 textract.analyze_document with QUERIES for recipient name +
        # address + postcode; map response → AIResult(provider="textract").
        raise NotImplementedError("Textract provider not yet wired")


class GeminiProvider(AIProvider):
    name = "gemini"

    def available(self) -> bool:
        return bool(os.environ.get("GEMINI_API_KEY"))

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        # TODO: google-genai vision call with a structured-JSON prompt asking for
        # recipient_name/company/address/postcode/is_continuation.
        raise NotImplementedError("Gemini provider not yet wired")


class ClaudeProvider(AIProvider):
    name = "claude"

    def available(self) -> bool:
        return bool(os.environ.get("ANTHROPIC_API_KEY"))

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        # TODO: anthropic messages API, image block + structured-output prompt.
        raise NotImplementedError("Claude provider not yet wired")


class DocumentAIProvider(AIProvider):
    name = "documentai"

    def available(self) -> bool:
        return bool(os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and os.environ.get("DOCAI_PROCESSOR_ID"))

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        # TODO: google-cloud-documentai process_document with the OCR/form processor.
        raise NotImplementedError("Document AI provider not yet wired")


class AzureProvider(AIProvider):
    name = "azure"

    def available(self) -> bool:
        return bool(os.environ.get("AZURE_DOCINTEL_ENDPOINT") and os.environ.get("AZURE_DOCINTEL_KEY"))

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        # TODO: azure-ai-documentintelligence prebuilt-read/layout.
        raise NotImplementedError("Azure Document Intelligence provider not yet wired")


# Real providers first (preferred when their keys are set), mock last as a fallback.
_REGISTRY: list[AIProvider] = [
    TextractProvider(),
    GeminiProvider(),
    ClaudeProvider(),
    DocumentAIProvider(),
    AzureProvider(),
    MockProvider(),
]


def available_providers() -> list[str]:
    """Names of providers whose credentials are present (mock always available)."""
    return [p.name for p in _REGISTRY if p.available()]


def ai_extract(
    image_png: bytes, context: dict | None = None, prefer: str | None = None
) -> AIResult | None:
    """
    Route an extraction to the first available provider (or `prefer` first).
    Returns None only if every provider is unavailable or errors.
    """
    context = context or {}
    order = _REGISTRY
    if prefer:
        order = sorted(_REGISTRY, key=lambda p: 0 if p.name == prefer else 1)
    for provider in order:
        if not provider.available():
            continue
        try:
            return provider.extract(image_png, context)
        except NotImplementedError:
            continue
        except Exception:
            continue
    return None
