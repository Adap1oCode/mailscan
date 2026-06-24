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
      "textract":  {"access_key_id","secret_access_key","region"},
      "gemini":    {"api_key"},
      "openrouter":{"api_key","model"},
      ...
    },
  }
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from dataclasses import asdict, dataclass
from typing import Optional

# UK postcode (lenient, optional space) for locating the address block.
_PC_RE = re.compile(r"([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})")


@dataclass
class AIResult:
    """Structured recipient extracted by an AI provider."""
    recipient_name: Optional[str] = None   # best display label (company or individual)
    company: Optional[str] = None          # registered company/organisation name
    individual_name: Optional[str] = None  # personal name (title + surname etc.)
    address: Optional[str] = None          # street address lines (no postcode)
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


def _openrouter_chat(api_key: str, model: str, system: str, user: str, json_mode: bool = True, retries: int = 3) -> str:
    """One OpenRouter chat completion (with retry/backoff). Used for reasoning + summary."""
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    data = json.dumps(body).encode()
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                "https://openrouter.ai/api/v1/chat/completions",
                data=data,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
            resp = json.load(urllib.request.urlopen(req, timeout=90))
            content = (resp.get("choices") or [{}])[0].get("message", {}).get("content")
            if content and content.strip():
                return content.strip()
            last_err = RuntimeError("empty completion")
        except Exception as e:  # transient HTTP / network / parse — retry
            last_err = e
        time.sleep(1.5 * (attempt + 1))
    raise last_err or RuntimeError("openrouter failed")


def _loose_json(s: str) -> Optional[dict]:
    """Parse JSON that may be wrapped in ```fences``` or surrounded by prose."""
    s = (s or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\n?", "", s)
        s = re.sub(r"```\s*$", "", s).strip()
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else None
    except Exception:
        pass
    a, b = s.find("{"), s.rfind("}")
    if 0 <= a < b:
        try:
            out = json.loads(s[a : b + 1])
            return out if isinstance(out, dict) else None
        except Exception:
            pass
    return None


def _openrouter_model(context: dict) -> tuple[Optional[str], str]:
    c = _creds(context, "openrouter")
    api_key = c.get("api_key") or os.environ.get("OPENROUTER_API_KEY")
    model = c.get("model") or os.environ.get("OPENROUTER_MODEL") or "deepseek/deepseek-chat"
    return api_key, model


class OpenRouterProvider(AIProvider):
    """
    Reasoning provider via OpenRouter (any model, e.g. DeepSeek). Works on TEXT
    (the OCR/Textract output in context["ocr_text"]) — it reasons out *who the
    letter is addressed to*, which pure OCR/layout engines get wrong on messy mail.
    """
    name = "openrouter"

    def available(self, context: dict) -> bool:
        return bool(_openrouter_model(context)[0])

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        api_key, model = _openrouter_model(context)
        text = (context or {}).get("ocr_text", "") or ""
        if not api_key or not text.strip():
            raise NotImplementedError("no openrouter credentials/text")
        out = _openrouter_chat(
            api_key,
            model,
            "You identify the DELIVERY RECIPIENT of a UK letter — the person or company "
            "it is physically addressed to, NOT the sender or letterhead organisation. "
            "Reply ONLY with JSON (no prose, no markdown): "
            '{"company_name": string|null, '
            '"individual_name": string|null, '
            '"address_lines": string|null, '
            '"postcode": string|null}. '
            "company_name: registered business/organisation name (null for personal letters). "
            "individual_name: personal name including title (null if only a company is named). "
            "address_lines: street address excluding postcode, lines joined with \\n. "
            "postcode: UK postcode of the delivery address. "
            "Use null for any field that is genuinely absent.",
            text[:6000],
        )
        data = _loose_json(out) or {}
        company = (data.get("company_name") or "").strip() or None
        individual = (data.get("individual_name") or "").strip() or None
        address_lines = (data.get("address_lines") or "").strip() or None
        postcode = (data.get("postcode") or "").strip() or None
        # Best display label: company if present, else individual
        recipient_name = company or individual
        return AIResult(
            recipient_name=recipient_name,
            company=company,
            individual_name=individual,
            address=address_lines,
            postcode=postcode,
            confidence=0.85 if recipient_name else 0.3,
            provider=f"openrouter:{model}",
        )


def ai_summarise(text: str, context: dict | None = None) -> Optional[dict]:
    """
    Client-facing summary of a letter (Hoxton-style) via OpenRouter. Returns a dict
    of structured fields, or None if unavailable. Never raises.
    """
    context = context or {}
    api_key, model = _openrouter_model(context)
    if not api_key or not (text or "").strip():
        return None
    try:
        out = _openrouter_chat(
            api_key,
            model,
            "You summarise a scanned UK letter for the recipient's virtual-mailroom "
            "inbox — so they grasp it without opening the full scan. Return ONLY JSON: "
            '{"mail_type": string, "sender": string, "summary": string (1-2 plain '
            'sentences with the key point/action), "action_required": string|null, '
            '"due_date": string|null, "reference": string|null, "amount": string|null}. '
            "Be concise and factual; use null when a field is absent.",
            (text or "")[:8000],
        )
        return _loose_json(out) or {"mail_type": "Letter", "summary": out[:400]}
    except Exception:
        return None


class GeminiProvider(AIProvider):
    name = "gemini"

    def available(self, context: dict) -> bool:
        return bool(_creds(context, "gemini").get("api_key") or os.environ.get("GEMINI_API_KEY"))

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        # TODO: google-genai vision call, structured-JSON recipient prompt.
        raise NotImplementedError("Gemini provider not yet wired")


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
    OpenRouterProvider(),
    GeminiProvider(),
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
