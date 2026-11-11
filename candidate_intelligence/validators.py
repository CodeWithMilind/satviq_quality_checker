"""Validation helpers for canonical documents.

validate_canonical(data) -> the validated model (raises on error).
check_canonical(data) -> (True, model) or (False, [error strings]):
the non-raising form used where a build failure must be logged and
skipped rather than propagated (batch backfills). A document that
fails validation is NEVER stored as a canonical evaluation.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .schema import CanonicalEvaluation, validate_canonical

__all__ = ["CanonicalEvaluation", "validate_canonical", "check_canonical"]


def check_canonical(data: Any):
    try:
        return True, validate_canonical(data)
    except ValidationError as exc:
        errors = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            errors.append(f"{loc}: {err.get('msg')}")
        return False, errors
