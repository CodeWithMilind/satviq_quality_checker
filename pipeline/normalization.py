"""Text normalization stage -- thin wrapper over the existing
deterministic normalizer in api.text_normalization.

Both the original and the normalized text are preserved; the original
is never modified (strings are immutable and both are returned).
"""
from __future__ import annotations

from typing import Any


def normalize_extracted_text(text: str | None) -> dict[str, Any]:
    """Normalize extracted text for the LLM.

    Returns dict with status, original_text, normalized_text,
    original_chars, normalized_chars, normalization_applied,
    normalization_steps, error.
    """
    if text is None:
        return {
            "status": "failed",
            "original_text": None,
            "normalized_text": None,
            "original_chars": 0,
            "normalized_chars": 0,
            "normalization_applied": False,
            "normalization_steps": [],
            "error": "No text provided for normalization.",
        }
    if not isinstance(text, str):
        return {
            "status": "failed",
            "original_text": text,
            "normalized_text": None,
            "original_chars": 0,
            "normalized_chars": 0,
            "normalization_applied": False,
            "normalization_steps": [],
            "error": f"Expected str, got {type(text).__name__}",
        }
    original = text
    try:
        from api.text_normalization import normalize_with_steps
        normalized, steps = normalize_with_steps(original)
    except Exception as exc:
        return {
            "status": "failed",
            "original_text": original,
            "normalized_text": original,
            "original_chars": len(original),
            "normalized_chars": len(original),
            "normalization_applied": False,
            "normalization_steps": [],
            "error": str(exc),
        }
    return {
        "status": "success",
        "original_text": original,
        "normalized_text": normalized,
        "original_chars": len(original),
        "normalized_chars": len(normalized),
        "normalization_applied": normalized != original,
        "normalization_steps": list(steps),
        "error": None,
    }
