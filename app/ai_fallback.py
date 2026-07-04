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
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Optional

logger = logging.getLogger("mailscan.ai")

# UK postcode (lenient, optional space) for locating the address block.
_PC_RE = re.compile(r"([A-Z]{1,2}\d{1,2}[A-Z]?\s?\d[A-Z]{2})")


def parse_json_object(raw: str | None, label: str = "payload") -> dict | None:
    """Parse a JSON-object form field. Returns None on empty/invalid input (never raises)."""
    if not raw or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        logger.warning("%s is not valid JSON — ignoring", label)
        return None
    if not isinstance(parsed, dict):
        logger.warning("%s is not a JSON object — ignoring", label)
        return None
    return parsed


def parse_credentials(raw: str | None) -> dict | None:
    """
    Parse the AI-credentials bundle passed per-request (MVOS org_integrations) —
    a JSON object string. Shared by the API layer, the Celery worker, and /ai/letter.
    """
    return parse_json_object(raw, "ai_credentials")


# ---------------------------------------------------------------------------
# Per-request options — the tenant-override channel.
#
# Callers (MVOS) pass an `options` JSON object alongside ai_credentials; every
# value here overrides the server default for THIS request only, so each org can
# run mailscan with its own prompts / models / thresholds without a redeploy:
#   {
#     "prompts": {"extract": str, "summary": str},   # full system-prompt overrides
#     "models":  {"extract": str, "summary": str},   # per-task OpenRouter model
#     "limits":  {"extract_chars": int, "summary_chars": int},
#     "match":   {"cutoff": float, "margin": float, "name_conf": float,
#                  "shared_postcodes": [str]},
#     "split":   {"blank_ink_pct": float, "blank_keep_floor": float},
#   }
# Unknown keys are ignored (forward compatible). Providers read them from
# context["options"].
# ---------------------------------------------------------------------------

def _options(context: dict | None) -> dict:
    opts = (context or {}).get("options")
    return opts if isinstance(opts, dict) else {}


def _opt_str(context: dict | None, section: str, key: str) -> Optional[str]:
    v = (_options(context).get(section) or {}).get(key)
    return v.strip() if isinstance(v, str) and v.strip() else None


def _opt_int(context: dict | None, section: str, key: str, default: int) -> int:
    v = (_options(context).get(section) or {}).get(key)
    try:
        n = int(v)
        return n if n > 0 else default
    except (TypeError, ValueError):
        return default


# Custom extraction prompt MUST keep the JSON contract below — the pipeline
# parses these exact keys. Env overrides the default; per-request options
# override both.
DEFAULT_EXTRACT_PROMPT = os.environ.get("MAILSCAN_EXTRACT_PROMPT") or (
    "You identify the DELIVERY RECIPIENT of a UK letter — the person or company "
    "it is physically addressed to, NOT the sender or letterhead organisation. "
    "The recipient is named in the POSTAL ADDRESS BLOCK (name + street + "
    "postcode). NEVER use generic heading or salutation words as the name — "
    "KEEPER, OCCUPIER, OWNER, DIRECTOR, SIR, MADAM identify a role, not the "
    "recipient; when a heading says e.g. 'NOTICE TO KEEPER', the recipient is "
    "still the company/person in the address block. "
    "Reply ONLY with JSON (no prose, no markdown): "
    '{"company_name": string|null, '
    '"individual_name": string|null, '
    '"address_lines": string|null, '
    '"postcode": string|null, '
    '"company_number": string|null, '
    '"vat_number": string|null}. '
    "company_name: registered business/organisation name (null for personal letters). "
    "individual_name: personal name including title (null if only a company is named). "
    "address_lines: street address excluding postcode, lines joined with \\n. "
    "postcode: UK postcode of the delivery address. "
    "company_number: the RECIPIENT company's Companies House registration number "
    "when the letter quotes it ABOUT the recipient (e.g. 'Company number' beside "
    "their name/reference on Companies House or HMRC letters) — NEVER the "
    "sender's own registration number from the letterhead or small-print footer. "
    "vat_number: the RECIPIENT's VAT registration number when the letter is "
    "about their VAT affairs — NEVER the sender's/supplier's VAT number printed "
    "on an invoice. "
    "Use null for any field that is genuinely absent."
)

DEFAULT_SUMMARY_PROMPT = os.environ.get("MAILSCAN_SUMMARY_PROMPT") or (
    "You summarise a scanned UK letter for the recipient's virtual-mailroom "
    "inbox — so they grasp it without opening the full scan. Return ONLY JSON: "
    '{"mail_type": string (EXACTLY one of: "official" (government/regulator, '
    'e.g. Companies House), "tax" (HMRC tax notices/demands), '
    '"debt_collection" (collection agencies, final demands, arrears), '
    '"legal" (courts, claims, solicitors), "bank" (bank statements/letters), '
    '"invoice" (supplier bills), "marketing" (offers/promotions), '
    '"junk", "business" (anything else)), '
    '"sender": string, '
    '"subject": string (one line, like an email subject — the letter\'s own '
    "subject/heading if it has one, else a short topic phrase), "
    '"summary": string (1-2 plain sentences with the key point/action), '
    '"action_required": string|null, '
    '"due_date": string|null (a DEADLINE stated in the letter — never the '
    "letter's own date), "
    '"amount": string|null (the main amount due/owed/balance, copied EXACTLY '
    "as printed — OCR may insert stray spaces in figures; reconstruct the "
    "amount carefully), "
    '"account_number": string|null (the account, agreement, customer or claim '
    "number this letter is about — the recipient's account with the sender; "
    "NEVER the sender's own bank/sort-code details for receiving payment, and "
    "NEVER the print/franking/mailing codes beside the address block), "
    '"payment_reference": string|null (the exact reference the letter says to '
    "quote when making a payment), "
    '"reference": string|null (any other sender reference on the letter)}. '
    "Fill every field whose value appears ANYWHERE in the letter; use null "
    "only when genuinely absent. Be concise and factual."
)

# Default truncation limits for the text handed to the LLM (chars).
_EXTRACT_CHARS = int(os.environ.get("MAILSCAN_EXTRACT_CHARS", "6000"))
_SUMMARY_CHARS = int(os.environ.get("MAILSCAN_SUMMARY_CHARS", "8000"))


@dataclass
class AIResult:
    """Structured recipient extracted by an AI provider."""
    recipient_name: Optional[str] = None   # best display label (company or individual)
    company: Optional[str] = None          # registered company/organisation name
    individual_name: Optional[str] = None  # personal name (title + surname etc.)
    address: Optional[str] = None          # street address lines (no postcode)
    postcode: Optional[str] = None
    # Recipient-side unique identifiers when the letter quotes them (never the
    # sender's own numbers) — deterministic matching keys downstream.
    company_number: Optional[str] = None
    vat_number: Optional[str] = None
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
        except urllib.error.HTTPError as e:
            # Client errors other than rate-limit/timeout are permanent (bad key,
            # bad model, malformed request) — retrying just burns latency.
            if 400 <= e.code < 500 and e.code not in (408, 429):
                logger.warning("openrouter %s: HTTP %s — not retryable", model, e.code)
                raise
            last_err = e
            if e.code == 429 and attempt < retries - 1:
                # Rate limit: the default 1.5–3s backoff doesn't clear a limit
                # window (observed dropping one letter per real batch run).
                # Honour Retry-After when present, else back off harder.
                retry_after = 0.0
                try:
                    retry_after = float(e.headers.get("Retry-After") or 0)
                except (TypeError, ValueError):
                    pass
                wait = min(max(retry_after, 5.0 * (attempt + 1)), 30.0)
                logger.warning("openrouter %s rate-limited — waiting %.0fs", model, wait)
                time.sleep(wait)
                continue
        except Exception as e:  # transient HTTP / network / parse — retry
            last_err = e
        logger.warning(
            "openrouter %s attempt %d/%d failed: %s", model, attempt + 1, retries, last_err
        )
        if attempt < retries - 1:
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


def _openrouter_model(context: dict, task: str = "extract") -> tuple[Optional[str], str]:
    """API key + model for a task. Per-task options override the shared creds model."""
    c = _creds(context, "openrouter")
    api_key = c.get("api_key") or os.environ.get("OPENROUTER_API_KEY")
    model = (
        _opt_str(context, "models", task)
        or c.get("model")
        or os.environ.get("OPENROUTER_MODEL")
        or "deepseek/deepseek-chat"
    )
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
        api_key, model = _openrouter_model(context, task="extract")
        text = (context or {}).get("ocr_text", "") or ""
        if not api_key or not text.strip():
            raise NotImplementedError("no openrouter credentials/text")
        prompt = _opt_str(context, "prompts", "extract") or DEFAULT_EXTRACT_PROMPT
        limit = _opt_int(context, "limits", "extract_chars", _EXTRACT_CHARS)
        out = _openrouter_chat(api_key, model, prompt, text[:limit])
        data = _loose_json(out) or {}
        company = (data.get("company_name") or "").strip() or None
        individual = (data.get("individual_name") or "").strip() or None
        address_lines = (data.get("address_lines") or "").strip() or None
        postcode = (data.get("postcode") or "").strip() or None
        company_number = (str(data.get("company_number") or "")).strip() or None
        vat_number = (str(data.get("vat_number") or "")).strip() or None
        # Best display label: company if present, else individual
        recipient_name = company or individual
        return AIResult(
            recipient_name=recipient_name,
            company=company,
            individual_name=individual,
            address=address_lines,
            postcode=postcode,
            company_number=company_number,
            vat_number=vat_number,
            confidence=0.85 if recipient_name else 0.3,
            provider=f"openrouter:{model}",
        )


def summarise_letter(
    text: str, context: dict | None = None
) -> tuple[Optional[dict], Optional[str]]:
    """
    Client-facing summary of a letter via OpenRouter. Returns (summary, error):

      (dict, None)  — success.
      (None, None)  — NOT attempted: no summary provider configured, or no text.
                      This is a legitimate empty, not a failure.
      (None, str)   — the provider ERRORED after its retries. The string is a
                      short machine reason (e.g. "HTTPError: 429") so the caller
                      can tell a TRANSIENT failure worth retrying apart from a
                      genuinely-empty summary. Never raises.

    This distinction is the contract that lets the mailroom brain (MVOS) retry
    only the letters that actually failed extraction, instead of guessing from a
    bare `null` why a letter has no sender/subject.
    """
    context = context or {}
    api_key, model = _openrouter_model(context, task="summary")
    if not api_key or not (text or "").strip():
        return None, None
    prompt = _opt_str(context, "prompts", "summary") or DEFAULT_SUMMARY_PROMPT
    limit = _opt_int(context, "limits", "summary_chars", _SUMMARY_CHARS)
    try:
        out = _openrouter_chat(api_key, model, prompt, (text or "")[:limit])
        return (_loose_json(out) or {"mail_type": "Letter", "summary": out[:400]}), None
    except Exception as e:
        # _openrouter_chat has already exhausted its retries/backoff — this is a
        # real failure, not a hiccup. Surface WHY instead of swallowing it.
        logger.warning("summarise_letter failed — letter will have no summary", exc_info=True)
        return None, f"{type(e).__name__}: {e}"[:300]


def ai_summarise(text: str, context: dict | None = None) -> Optional[dict]:
    """
    Back-compat shim — the summary dict only (None when absent OR errored).
    Prefer summarise_letter() when you need to know WHY a summary is missing.
    """
    summary, _error = summarise_letter(text, context)
    return summary


class GeminiProvider(AIProvider):
    name = "gemini"

    def available(self, context: dict) -> bool:
        return bool(_creds(context, "gemini").get("api_key") or os.environ.get("GEMINI_API_KEY"))

    def extract(self, image_png: bytes, context: dict) -> AIResult:
        # TODO: google-genai vision call, structured-JSON recipient prompt.
        raise NotImplementedError("Gemini provider not yet wired")


class MockProvider(AIProvider):
    """
    No-key stand-in so the flow is testable before real keys exist. OPT-IN
    (MAILSCAN_AI_ENABLE_MOCK=1): in production a silent mock would return the
    first OCR line as the "recipient" — a plausible-looking wrong answer that
    could mis-route real mail when credentials are missing or misconfigured.
    """
    name = "mock"

    def available(self, context: dict) -> bool:
        return os.environ.get("MAILSCAN_AI_ENABLE_MOCK") == "1"

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
            logger.warning("AI provider %s failed — trying next", provider.name, exc_info=True)
            continue
    return None
