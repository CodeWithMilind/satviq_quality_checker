"""Prompts for the resume recovery task.

The system prompt encodes the task's hard rules: the original page
images are the source of truth, extraction outputs are evidence to
compare (not truth), nothing may be invented, and unreadable content
must be marked explicitly.
"""

from __future__ import annotations

import json
from typing import Any

from ..utils.token_utils import truncate_to_tokens

RESUME_RECOVERY_SYSTEM_PROMPT = """\
You are a resume text recovery specialist. Your job is to reconstruct
the exact text of a resume from its page images, using automated
extraction outputs only as supporting evidence.

Hard rules:
1. The original resume page images are the SOURCE OF TRUTH. Where an
   extraction output disagrees with what is visible in the images,
   the images win.
2. NEVER invent information. Do not add names, dates, skills,
   employers, education, metrics, or any other content that is not
   visible in the images or present in the extraction outputs.
3. Preserve the logical reading order of the original document
   (top to bottom, correct column order, sections in original order).
4. Preserve factual details exactly as written: names, dates, skills,
   education, and experience.
5. If a word or section cannot be read confidently from the images,
   write [UNREADABLE] in its place in the recovered text and list the
   section in "unreadable_sections".
6. List every problem you found in the extraction outputs (missing,
   corrupted, duplicated, or mis-ordered content) in "issues".
7. "recovery_confidence" is your honest estimate between 0 and 1 of
   how completely and accurately the recovered text matches the
   original resume.

Respond with a single JSON object with exactly these keys:
"recovered_text" (string), "recovery_confidence" (number 0-1),
"unreadable_sections" (array of strings), "issues" (array of strings).
"""


def build_resume_recovery_prompt(
    *,
    resume_id: str,
    extraction_results: dict[str, str],
    quality_report: dict[str, Any],
    page_count: int,
    per_extractor_token_budget: int = 6000,
) -> str:
    """Build the user prompt for one resume recovery request."""
    parts: list[str] = [
        f"Resume ID: {resume_id}",
        f"Page images attached: {page_count} "
        f"(the source of truth — read these first).",
        "",
        "Extraction outputs to compare against the images:",
    ]
    if extraction_results:
        for extractor, text in extraction_results.items():
            parts.append(f"\n--- Extraction from {extractor} ---")
            parts.append(truncate_to_tokens(text or "", per_extractor_token_budget))
    else:
        parts.append("(none provided — recover from the images alone)")

    parts.append("\nQuality report for the extraction outputs:")
    parts.append(
        json.dumps(quality_report, indent=2, default=str)
        if quality_report
        else "(none provided)"
    )
    parts.append(
        "\nReconstruct the resume text now. Follow the hard rules: "
        "images are the source of truth, invent nothing, preserve "
        "reading order, mark unreadable content with [UNREADABLE]."
    )
    return "\n".join(parts)
