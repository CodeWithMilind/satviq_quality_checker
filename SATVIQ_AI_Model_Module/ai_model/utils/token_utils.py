"""Lightweight token estimation helpers.

A character-based heuristic (~4 characters per token for English
text). This is deliberately dependency-free — it is used to size
prompts and warn about context limits, never for billing. For exact
counts, read the ``token_usage`` the provider returns in results.
"""

from __future__ import annotations

_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Estimate the token count of a text."""
    if not text:
        return 0
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def estimate_total_tokens(texts: dict[str, str] | list[str]) -> int:
    values = texts.values() if isinstance(texts, dict) else texts
    return sum(estimate_tokens(t) for t in values)


def fits_in_context(
    texts: dict[str, str] | list[str],
    max_context_window: int,
    *,
    reserved_output_tokens: int = 4096,
) -> bool:
    """True when the estimated input fits the model's context window
    with room reserved for the response."""
    return estimate_total_tokens(texts) + reserved_output_tokens <= max_context_window


def truncate_to_tokens(text: str, max_tokens: int) -> str:
    """Truncate text to an estimated token budget, keeping the start
    and end (resume headers and final sections both matter) with a
    visible omission marker in the middle."""
    if max_tokens <= 0:
        return ""
    if estimate_tokens(text) <= max_tokens:
        return text
    max_chars = max_tokens * _CHARS_PER_TOKEN
    marker = "\n[... content truncated to fit context ...]\n"
    keep = max(0, max_chars - len(marker))
    head = keep // 2
    tail = keep - head
    return text[:head] + marker + (text[-tail:] if tail else "")
