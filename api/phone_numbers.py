"""Deterministic Indian mobile number normalization for SATVIQ.

Single source of truth for how phone numbers are formatted anywhere
in SATVIQ. Pure Python, no LLM, no I/O — safe to reuse from the
pipeline, the Muse service, the API store and tests.

Canonical format::

    +91 9876543210

i.e. the Indian country code, one space, then exactly 10 digits.

Rules (deliberately conservative — never invent or alter digits):

- Formatting characters (spaces, hyphens, brackets, dots) are
  removed before analysis; a leading ``+`` is preserved as the
  international marker.
- ``+91`` / ``91`` country code: detected and never duplicated.
- A trunk ``0`` before the 10 digits (``098765 43210``) is dropped.
- The national number must be exactly 10 digits AND start with
  6, 7, 8 or 9 (the valid Indian mobile series). Anything else —
  landlines (e.g. a Pune ``020`` number), foreign numbers, shorter
  or longer digit strings, ambiguous values — is returned
  byte-identical to the input.
- Non-phone numerics are never touched by this module: it is only
  ever applied to values already identified as phone numbers (the
  structured ``phone`` field, recovered contact values), never
  scanned blindly across free text, dates, IDs or amounts.

Public API:
- :func:`normalize_indian_mobile` — one value -> canonical or original.
- :func:`normalize_phone_field` — a phone FIELD that may carry one
  or several numbers separated by ``,`` ``;`` ``|`` or ``/``; every
  segment is normalized independently, order and separators kept.
- :func:`is_valid_indian_mobile` — whether a value is (or normalizes
  to) a valid Indian mobile.
"""

from __future__ import annotations

import re

__all__ = [
    "normalize_indian_mobile",
    "normalize_phone_field",
    "is_valid_indian_mobile",
]

# Indian mobile numbers are issued in the 6/7/8/9 series only.
_MOBILE_FIRST_DIGITS = frozenset("6789")

# Formatting noise stripped before analysis: whitespace, hyphens,
# brackets, dots and slashes commonly found in resume headers.
_NOISE_RE = re.compile(r"[\s\-()./]+")

# A phone field carrying several numbers: split for normalization on
# the common list separators. Each segment is stripped and rejoined
# with the canonical separator style (", " etc.) below.
_FIELD_SPLIT_RE = re.compile(r"([,;|/])")
_FIELD_JOINERS = {",": ", ", ";": "; ", "|": " | ", "/": " / "}


def _digits_and_plus(value: str) -> tuple[str, bool]:
    """Return (digits, had_leading_plus) with formatting removed."""
    cleaned = _NOISE_RE.sub("", value.strip())
    had_plus = cleaned.startswith("+")
    digits = re.sub(r"\D", "", cleaned)
    return digits, had_plus


def normalize_indian_mobile(value: object) -> str:
    """Normalize ONE phone number to ``+91 XXXXXXXXXX``.

    Returns the input unchanged (as a string) whenever the value is
    not unambiguously a valid Indian mobile number.
    """
    if value is None:
        return ""
    original = str(value)
    if not original.strip():
        return original
    digits, had_plus = _digits_and_plus(original)

    national: str | None = None
    if len(digits) == 10:
        # Bare 10-digit number: an Indian mobile only if it is in
        # the mobile series; otherwise (e.g. a landline fragment)
        # leave it alone.
        national = digits if digits[0] in _MOBILE_FIRST_DIGITS else None
    elif len(digits) == 11 and digits.startswith("0"):
        # Trunk-prefixed form: 0 + 10 digits.
        candidate = digits[1:]
        if candidate[0] in _MOBILE_FIRST_DIGITS:
            national = candidate
    elif len(digits) == 12 and digits.startswith("91"):
        # Country code without '+': 91 + 10 digits. The '+' form
        # lands here too after digit extraction.
        candidate = digits[2:]
        if candidate[0] in _MOBILE_FIRST_DIGITS:
            national = candidate
    elif (len(digits) == 13 and digits.startswith("091")):
        # International trunk form: 091 + 10 digits.
        candidate = digits[3:]
        if candidate[0] in _MOBILE_FIRST_DIGITS:
            national = candidate

    if national is None:
        # Invalid or ambiguous (foreign number, landline, wrong
        # length, implausible series): preserve the original value.
        return original
    return f"+91 {national}"


def is_valid_indian_mobile(value: object) -> bool:
    """True when ``value`` is, or normalizes to, a valid Indian mobile."""
    if value is None:
        return False
    normalized = normalize_indian_mobile(value)
    return normalized.startswith("+91 ") and len(normalized) == 14


def normalize_phone_field(value: object) -> str:
    """Normalize every number in a phone FIELD value.

    A resume may list several numbers (``98220 12345, +91 98220
    67890``). Each comma/semicolon/pipe/slash-separated segment is
    normalized independently with :func:`normalize_indian_mobile`;
    segments that are not valid Indian mobiles are preserved as-is,
    and the original separators are kept.
    """
    if value is None:
        return ""
    text = str(value)
    if not text.strip():
        return text
    parts = _FIELD_SPLIT_RE.split(text)
    out: list[str] = []
    for part in parts:
        if _FIELD_SPLIT_RE.fullmatch(part or ""):
            out.append(_FIELD_JOINERS.get(part, part))
        elif part.strip():
            out.append(normalize_indian_mobile(part.strip()))
        else:
            out.append(part)
    return "".join(out)
