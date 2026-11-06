"""Muse AI service — the pipeline's two Muse stages.

1. **Resume JSON generation** — accepted extracted text → structured
   Resume JSON (schema-validated locally).
2. **Direct image/PDF extraction** — when the Tika → PyMuPDF →
   Docling chain does not reach a Quality Checker PASS, the ORIGINAL
   file goes to Muse: images as image parts, PDFs as a file part
   (the API converts them server-side). Nothing is invented:
   unreadable/absent fields keep the schema's empty representations,
   and every failure is returned as an explicit outcome.

Outcomes are plain dicts the pipeline records verbatim: model,
input type, duration, configured timeout, token usage, JSON
validation status and error details.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .client import (MuseClient, MuseConfigurationError, MuseError,
                     MuseTimeoutError)
from .config import INLINE_FILE_LIMIT_BYTES, PROVIDER_NAME, MuseConfig
from .schema import ResumeJSON, validate_resume_json

_IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg",
               ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".gif": "image/gif"}

_INSTRUCTIONS = (
    "You extract structured data from resumes. Reply with ONLY a "
    "JSON object matching this schema:\n"
    + json.dumps(ResumeJSON.model_json_schema())
    + "\nNever invent details: for anything unreadable or absent "
      "use the schema's empty value (\"\" for text, [] for lists).")

# Source-reconciliation rules for the combined vision request
# (spec: visual document first, Tika text supporting, hyperlink
# annotations authoritative for URLs).
_RECONCILE = (
    "You are given up to three sources for ONE resume. Reconcile "
    "them with this priority: (1) the original visual document is "
    "authoritative for content, layout, labels, dates and section "
    "interpretation; (2) the Tika extracted text is SUPPORTING "
    "context only — it may be incomplete, mis-ordered, or contain "
    "OCR errors, so never copy it where it conflicts with clear "
    "visual evidence, and do not ignore it where it is consistent; "
    "(3) the PDF hyperlink annotations are AUTHORITATIVE for URLs — "
    "preserve those URLs exactly in the links field. Never invent "
    "a URL: if a label such as 'Portfolio' is visible but no URL "
    "was extracted for it, do not put a placeholder URL in links.")

_TEXT_PROMPT = "Convert this extracted resume text into the JSON object."


def _effective_instructions() -> str:
    """System prompt for new Muse requests: the Settings-page
    override when one is saved, otherwise the built-in default.
    Any storage problem falls back to the default — a prompt-store
    failure must never break extraction."""
    try:
        from .prompt_config import get_effective_prompt

        return get_effective_prompt()
    except Exception:
        return _INSTRUCTIONS


class MuseService:
    """Config-bound facade used by the extraction pipeline."""

    provider_name = PROVIDER_NAME

    def __init__(self, config: MuseConfig, client: MuseClient | None = None):
        self.config = config
        self.client = client or MuseClient(config)

    @property
    def configured(self) -> bool:
        return self.config.configured

    @property
    def model(self) -> str:
        return self.config.model

    @property
    def vision_enabled(self) -> bool:
        return self.config.vision_enabled

    @property
    def configuration_error(self) -> str | None:
        return self.config.configuration_error

    # -- outcome helpers -------------------------------------------------
    def _base_outcome(self, input_type: str) -> dict[str, Any]:
        return {"ok": False, "error": None, "error_type": None,
                "resume_json": None, "validation": None,
                "provider": PROVIDER_NAME, "model": self.config.model,
                "raw_text": None, "usage": {}, "usage_available": False,
                "request_id": None, "duration_ms": None,
                "timeout_seconds": self.config.timeout_seconds,
                "input_type": input_type,
                "visual_provided": False, "tika_text_provided": False,
                "tika_text_chars": 0, "hyperlinks_provided": False,
                "hyperlink_count": 0}

    def _fail(self, outcome: dict, exc: Exception) -> dict[str, Any]:
        outcome["error"] = str(exc)
        outcome["error_type"] = (
            "TimeoutError" if isinstance(exc, MuseTimeoutError)
            else "ConfigurationError"
            if isinstance(exc, MuseConfigurationError)
            else type(exc).__name__)
        return outcome

    def _finish(self, outcome: dict, result: dict) -> dict[str, Any]:
        outcome["raw_text"] = result["text"]
        outcome["usage"] = result.get("usage") or {}
        outcome["usage_available"] = any(
            outcome["usage"].get(field) is not None
            for field in ("prompt_tokens", "completion_tokens",
                          "total_tokens"))
        outcome["request_id"] = result.get("request_id")
        outcome["duration_ms"] = result.get("duration_ms")
        if result.get("model"):
            outcome["model"] = result["model"]
        parsed, error = validate_resume_json(result["text"])
        outcome["validation"] = "valid" if parsed is not None else "invalid"
        if parsed is not None:
            outcome["ok"] = True
            outcome["resume_json"] = parsed
        else:
            outcome["error"] = f"JSON validation failed: {error}"
            outcome["error_type"] = "ValidationError"
        return outcome

    # -- stage 1: text → Resume JSON --------------------------------------
    def generate_resume_json(self, text: str, *,
                             hyperlinks: list[dict] | None = None
                             ) -> dict[str, Any]:
        outcome = self._base_outcome("text")
        user_text = f"{_TEXT_PROMPT}\n\nRESUME TEXT:\n{text}"
        if hyperlinks:
            # The extracted text cannot contain annotation URLs —
            # supply them as authoritative link evidence here too.
            user_text += (
                "\n\nPDF HYPERLINK ANNOTATIONS (authoritative URL "
                "evidence extracted from the file; preserve these "
                "URLs exactly in the links field):\n" + json.dumps(
                    [{"page": h.get("page"), "url": h.get("url"),
                      "visible_text": h.get("visible_text"),
                      "type": h.get("type")} for h in hyperlinks],
                    ensure_ascii=False))
            outcome["hyperlinks_provided"] = True
            outcome["hyperlink_count"] = len(hyperlinks)
        try:
            result = self.client.chat_text(_effective_instructions(), user_text)
        except MuseError as exc:
            return self._fail(outcome, exc)
        return self._finish(outcome, result)

    # -- stage 2: original image/PDF → Resume JSON -------------------------
    def vision_extract(self, path: str | Path, *,
                       tika_text: str | None = None,
                       hyperlinks: list[dict] | None = None
                       ) -> dict[str, Any]:
        """Combined-source extraction: the ORIGINAL document plus
        Tika's extracted text (supporting context) plus the PDF's
        hyperlink annotations (authoritative URLs) in ONE request.
        The flags on the outcome state exactly which sources were
        actually included — they are set when the request is
        composed, never assumed afterwards."""
        path = Path(path)
        suffix = path.suffix.lower()
        input_type = ("pdf" if suffix == ".pdf"
                      else "image" if suffix in _IMAGE_MIME
                      else "unsupported")
        outcome = self._base_outcome(input_type)
        if not self.config.configured:
            return self._fail(outcome, MuseConfigurationError(
                self.config.configuration_error
                or "Muse AI is not configured."))
        if not self.config.vision_enabled:
            return self._fail(outcome, MuseConfigurationError(
                "Muse vision input is disabled "
                "(MUSE_VISION_ENABLED=false in the backend .env)."))
        instructions = _effective_instructions() + "\n" + _RECONCILE
        parts: list[dict] = [MuseClient.text_part(
            "The attached file is a resume. Extract it into the "
            "JSON object described in the instructions, reconciling "
            "all provided sources.")]
        try:
            if suffix in _IMAGE_MIME:
                parts.append(MuseClient.image_part(
                    path.read_bytes(), _IMAGE_MIME[suffix]))
                outcome.update(page_count=1, pages_processed=1,
                               pages_skipped=0)
            elif suffix == ".pdf":
                blob = path.read_bytes()
                if len(blob) > INLINE_FILE_LIMIT_BYTES:
                    return self._fail(outcome, MuseError(
                        f"PDF is {len(blob)} bytes, above the Muse "
                        f"API inline-file limit of "
                        f"{INLINE_FILE_LIMIT_BYTES} bytes."))
                page_count = _pdf_page_count(path)
                parts.append(MuseClient.pdf_part(blob, path.name))
                outcome.update(
                    page_count=page_count, pages_processed=page_count,
                    pages_skipped=0,
                    pages_beyond_image_limit=(
                        max(0, page_count - 50)
                        if page_count is not None else None))
            else:
                return self._fail(outcome, MuseError(
                    f"Muse direct extraction supports PDF and image "
                    f"files; this file is '{suffix or 'unknown'}'."))
        except MuseError as exc:
            return self._fail(outcome, exc)
        outcome["visual_provided"] = True
        if tika_text and tika_text.strip():
            parts.append(MuseClient.text_part(
                "TIKA EXTRACTED TEXT (supporting context; may be "
                "incomplete or contain OCR errors):\n" + tika_text))
            outcome["tika_text_provided"] = True
            outcome["tika_text_chars"] = len(tika_text)
        if hyperlinks:
            parts.append(MuseClient.text_part(
                "PDF HYPERLINK ANNOTATIONS (authoritative URL "
                "evidence extracted from the file; preserve these "
                "URLs exactly):\n" + json.dumps(
                    [{"page": h.get("page"), "url": h.get("url"),
                      "visible_text": h.get("visible_text"),
                      "type": h.get("type")} for h in hyperlinks],
                    ensure_ascii=False)))
            outcome["hyperlinks_provided"] = True
            outcome["hyperlink_count"] = len(hyperlinks)
        try:
            result = self.client.chat_parts(instructions, parts)
        except MuseError as exc:
            return self._fail(outcome, exc)
        return self._finish(outcome, result)


def _pdf_page_count(path: Path) -> int | None:
    try:
        import fitz
        with fitz.open(path) as document:
            return document.page_count
    except Exception:
        return None
