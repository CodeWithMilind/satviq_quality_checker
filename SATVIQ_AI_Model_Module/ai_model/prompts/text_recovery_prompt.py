"""Prompts for the text-only recovery task (no page images)."""

from __future__ import annotations

import json
from typing import Any

from ..utils.token_utils import truncate_to_tokens

TEXT_RECOVERY_SYSTEM_PROMPT = """\
You are a text recovery specialist. You are given one corrupted or
low-quality extracted text and, when available, reference extractions
of the same content from other tools.

Hard rules:
1. NEVER invent information. Only restore content that is supported
   by the corrupted text or the reference extractions.
2. Where references disagree, prefer the reading that appears in the
   majority of references; if you cannot decide, keep the corrupted
   text's reading and note the disagreement in "issues".
3. Preserve the original logical reading order and all factual
   details (names, dates, skills, education, experience) exactly.
4. If a passage cannot be recovered from the available evidence,
   write [UNREADABLE] in its place and note it in "issues".
5. "recovery_confidence" is your honest estimate between 0 and 1.

Respond with a single JSON object with exactly these keys:
"recovered_text" (string), "recovery_confidence" (number 0-1),
"issues" (array of strings).
"""


def build_text_recovery_prompt(
    *,
    text_id: str,
    corrupted_text: str,
    reference_texts: dict[str, str],
    quality_report: dict[str, Any],
    per_text_token_budget: int = 6000,
) -> str:
    parts: list[str] = [
        f"Text ID: {text_id}",
        "",
        "Corrupted / low-quality text to recover:",
        truncate_to_tokens(corrupted_text or "", per_text_token_budget),
    ]
    if reference_texts:
        parts.append("\nReference extractions of the same content:")
        for source, text in reference_texts.items():
            parts.append(f"\n--- Reference from {source} ---")
            parts.append(truncate_to_tokens(text or "", per_text_token_budget))
    parts.append("\nQuality report:")
    parts.append(
        json.dumps(quality_report, indent=2, default=str)
        if quality_report
        else "(none provided)"
    )
    parts.append("\nRecover the text now, following the hard rules.")
    return "\n".join(parts)
