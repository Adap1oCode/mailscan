"""
AI fallback for mailscan — provider-agnostic recipient extraction.

When the free pipeline isn't confident (no barcode, no clean recipient name), a
page image is handed to a document/vision AI to extract a structured recipient.
Providers are pluggable; the router picks the first available one (or `prefer`
first), falling back to a no-key mock so the flow runs in tests/demos.

Credentials are passed in per-request via context["credentials"] (resolved by
MVOS from org_integrations) — so mailscan stays credential-free. Each provider
falls back to env vars for local testing.

context shape:
  {
    "ocr_text": str,                       # free-stack OCR (for the mock / hints)
    "credentials": {                       # from MVOS resolveAiCredentials()
      "textract": {"access_key_id","secret_access_key","region"},
      "gemini":   {"api_key"},
      "claude":   {"api_key"},
      ...
    },
  }
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from typing import Optional

# UK postcode (lenient, optional space) for locating the address block.
_PC_RE = re.compile(r"([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})")


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


def _recipient_from_lines(lines: list[dict]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    lines: [{"text": str, "top": float 0-1}] sorted top→bottom.
    Find the address block (lines around the first postcode in the upper page) and
    return (name, block_text, postcode). Lines from AI OCR are clean, so a simple
    "postcode line + up to 4 lines above" window is reliable enough.
    """
    region = [l for l in lines if l.get("top", 0) <= 0.55]
    pc_idx = next((i for i, l in enumerate(region) if _PC_RE.search(l["text"].upper())), None)
    if pc_idx is None:
        return None, None, None
    block = [region[j]["text"].strip() for j in range(max(0, pc_idx - 4), pc_idx + 1) if region[j]["text"].strip()]
    name = block[0] if block else None
    m = _PC_RE.search(region[pc_idx]["text"].upper())
    pc = m.group(1) if m else None
    return name, ("\n".join(block) if block else None), pc


class AIProvider:
    name = "base"

    def available(self, context: dict) -> bool:
        return False

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        raise NotImplementedError


def _creds(context: dict, name: str) -> dict:
    return ((context or {}).get("credentials") or {}).get(name) or {}


class TextractProvider(AIProvider):
    name = "textract"

    def available(self, context: dict) -> bool:
        c = _creds(context, "textract")
        if c.get("access_key_id") and c.get("secret_access_key"):
            return True
        return bool(os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"))

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        c = _creds(context, "textract")
        ak = c.get("access_key_id") or os.environ.get("AWS_ACCESS_KEY_ID")
        sk = c.get("secret_access_key") or os.environ.get("AWS_SECRET_ACCESS_KEY")
        region = c.get("region") or os.environ.get("AWS_REGION") or "eu-west-2"
        if not (ak and sk):
            raise NotImplementedError("no textract credentials")

        import boto3

        client = boto3.client(
            "textract",
            aws_access_key_id=ak,
            aws_secret_access_key=sk,
            region_name=region,
        )
        resp = client.detect_document_text(Document={"Bytes": image_png})
        lines = [
            {"text": b["Text"], "top": b["Geometry"]["BoundingBox"]["Top"]}
            for b in resp.get("Blocks", [])
            if b.get("BlockType") == "LINE" and b.get("Text")
        ]
        lines.sort(key=lambda l: l["top"])
        name, block, pc = _recipient_from_lines(lines)
        full_text = "\n".join(l["text"] for l in lines)
        return AIResult(
            recipient_name=name,
            address=block or (full_text[:400] or None),
            postcode=pc,
            confidence=0.8 if name else 0.5,
            provider="textract",
        )


class GeminiProvider(AIProvider):
    name = "gemini"

    def available(self, context: dict) -> bool:
        return bool(_creds(context, "gemini").get("api_key") or os.environ.get("GEMINI_API_KEY"))

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        # TODO: google-genai vision call, structured-JSON recipient prompt.
        raise NotImplementedError("Gemini provider not yet wired")


class ClaudeProvider(AIProvider):
    name = "claude"

    def available(self, context: dict) -> bool:
        return bool(_creds(context, "claude").get("api_key") or os.environ.get("ANTHROPIC_API_KEY"))

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        # TODO: anthropic messages API, image block + structured-output prompt.
        raise NotImplementedError("Claude provider not yet wired")


class MockProvider(AIProvider):
    """No-key stand-in so the flow is testable before real keys exist."""
    name = "mock"

    def available(self, context: dict) -> bool:
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


# Real providers first (preferred when usable), mock last as a fallback.
_REGISTRY: list[AIProvider] = [
    TextractProvider(),
    GeminiProvider(),
    ClaudeProvider(),
    MockProvider(),
]


def available_providers(context: dict | None = None) -> list[str]:
    ctx = context or {}
    return [p.name for p in _REGISTRY if p.available(ctx)]


def ai_extract(
    image_png: bytes, context: dict | None = None, prefer: str | None = None
) -> AIResult | None:
    """Route to the first available provider (or `prefer` first)."""
    context = context or {}
    order = _REGISTRY
    if prefer:
        order = sorted(_REGISTRY, key=lambda p: 0 if p.name == prefer else 1)
    for provider in order:
        if not provider.available(context):
            continue
        try:
            return provider.extract(image_png, context)
        except NotImplementedError:
            continue
        except Exception:
            continue
    return None
