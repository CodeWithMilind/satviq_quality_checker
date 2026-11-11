"""Deterministic content evidence for Responsibilities & Achievements.

Root cause this module fixes: a resume that documents duties inside
its Work Experience entries (the normal way resumes are written)
has NO dedicated "Responsibilities" heading, and measurable outcomes
inside job bullets have no "Achievements" heading either. The
deterministic Quality Checker detects HEADINGS, so it reports both
sections missing, and an evaluation that trusts the heading verdict
reports content as missing when it is demonstrably present.

This module scans the resume TEXT (never the model's claims) for
the CONTENT itself:

- Responsibilities: duty statements — lines phrased as work
  performed (duty verbs / responsibility phrasing), the substance
  of job descriptions wherever they appear in the text.
- Achievements: measurable outcome statements — percentages,
  multipliers, scaled quantities and growth/improvement claims
  tied to a number, plus explicit award/recognition wording. A
  bare number is NOT an achievement; an outcome is required.

:func:`reconcile_content_sections` then corrects a validated
evaluation: when the text carries sufficient evidence for a section
the evaluation listed as missing, that section is moved to
``matching_sections`` with the FOUND LINES as its evidence and a
remark recording that the content is present without a dedicated
heading (a heading remains a fair presentation recommendation —
made in the moved entry's remarks, never as a "missing" verdict).
When the evidence is not there, the evaluation is left untouched.
Nothing is ever invented: every evidence string is a literal line
from the resume text.
"""

from __future__ import annotations

import re
from typing import Any

# Duty phrasing: first-person-free resume bullet verbs and explicit
# responsibility markers. Matched case-insensitively at a word
# boundary anywhere in a line (bullets rarely start lines cleanly
# after extraction).
_DUTY_RE = re.compile(
    r"\b(responsible for|responsibilities include|duties include|"
    r"led|lead(?:ing)?|manag(?:e|ed|ing)|develop(?:ed|ing)?|"
    r"creat(?:e|ed|ing)|built|design(?:ed|ing)?|implement(?:ed|ing)?|"
    r" handl(?:e|ed|ing)|coordinat(?:e|ed|ing)|execut(?:e|ed|ing)|"
    r"maintain(?:ed|ing)?|publish(?:ed|ing)?|produc(?:e|ed|ing)|"
    r"plan(?:ned|ning)?|collaborat(?:e|ed|ing)|wrote|writ(?:ing|ten)|"
    r"edit(?:ed|ing)?|curat(?:e|ed|ing)|deliver(?:ed|ing)?|"
    r"own(?:ed|ing)?|overs(?:ee|aw|eeing)|supervis(?:e|ed|ing)|"
    r"mentor(?:ed|ing)?|train(?:ed|ing)?|support(?:ed|ing)?|"
    r"ensur(?:e|ed|ing)|driv(?:e|ing)|drove|grow(?:ing)?|grew|"
    r"increas(?:e|ed|ing)|improv(?:e|ed|ing)|optimis(?:e|ed|ing)|"
    r"optimiz(?:e|ed|ing))\b",
    re.IGNORECASE)

# Measurable outcomes: a number tied to a scale or a change.
_ACHIEVEMENT_PATTERNS = [
    # Percentages: "grew engagement by 40%", "90% audience penetration"
    re.compile(r"\d+(?:\.\d+)?\s*%", re.IGNORECASE),
    # Multipliers: "3x growth", "grew 2.5x"
    re.compile(r"\b\d+(?:\.\d+)?\s*x\b", re.IGNORECASE),
    # Scaled quantities: "2M+ organic reach", "500K views", "10k followers"
    re.compile(r"\b\d+(?:\.\d+)?\s*[MK]\+?\b"),
    re.compile(r"\b\d[\d,]*\+?\s*(users|customers|clients|followers|"
               r"subscribers|views|downloads|leads|students|members|"
               r"projects delivered)\b", re.IGNORECASE),
    # Change claims with a number anywhere in the line.
    re.compile(r"\b(increased|grew|growth of|improved|improvement of|"
               r"boosted|reduced|saved|achieved|delivered|generated|"
               r"drove)\b[^.\n]{0,60}\d", re.IGNORECASE),
    # Explicit recognition.
    re.compile(r"\b(award|awarded|winner|won the|recognition|"
               r"recognised|recognized|top performer|best employee|"
               r"employee of the)\b", re.IGNORECASE),
]

RESPONSIBILITIES_MIN_DUTIES = 3
ACHIEVEMENTS_MIN_OUTCOMES = 2
_MAX_EVIDENCE_LINES = 3
_MAX_LINE_CHARS = 140


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines()
            if line.strip()]


def find_responsibility_evidence(text: str) -> list[str]:
    """Duty-statement lines found in the text (order preserved)."""
    return [line for line in _lines(text) if _DUTY_RE.search(line)]


def find_achievement_evidence(text: str) -> list[str]:
    """Measurable-outcome lines found in the text."""
    found: list[str] = []
    for line in _lines(text):
        if any(pattern.search(line) for pattern in _ACHIEVEMENT_PATTERNS):
            found.append(line)
    return found


def _evidence_excerpt(lines: list[str]) -> list[str]:
    return [line[:_MAX_LINE_CHARS] for line in lines[:_MAX_EVIDENCE_LINES]]


def _reconcile_one(evaluation: dict[str, Any], standard_name: str,
                   evidence_lines: list[str], minimum: int,
                   embedded_where: str, summary_template: str,
                   ) -> dict[str, Any]:
    """Move one section from missing to matching when the TEXT
    proves the content exists. Returns the action record."""
    record = {"found": len(evidence_lines) >= minimum,
              "evidence_count": len(evidence_lines),
              "action": "none"}
    if not record["found"]:
        return record
    matching = evaluation.setdefault("matching_sections", [])
    if any(standard_name.lower() in str(m.get("section") or "").lower()
           for m in matching if isinstance(m, dict)):
        record["action"] = "already_matching"
        return record
    missing = evaluation.setdefault("missing_sections", [])
    index = next((i for i, m in enumerate(missing)
                  if isinstance(m, dict)
                  and standard_name.lower()
                  in str(m.get("section") or "").lower()), None)
    if index is None:
        record["action"] = "not_reported_missing"
        return record
    missing.pop(index)
    matching.append({
        "section": standard_name,
        "original_heading": f"Embedded in {embedded_where} "
                            f"(no dedicated heading)",
        "presence_status": "present",
        "completeness_status": ("complete" if len(evidence_lines) >= 6
                                else "partial"),
        "quality_assessment": "",
        "summary": summary_template.format(count=len(evidence_lines)),
        "evidence": _evidence_excerpt(evidence_lines),
        "remarks": ("Content verified present in the resume text by "
                    "deterministic scan; it is documented inside "
                    f"{embedded_where} rather than under a dedicated "
                    f"{standard_name} heading. Adding a dedicated "
                    "heading is a presentation suggestion only — the "
                    "content itself is not missing."),
    })
    record["action"] = "moved_to_matching"
    return record


def reconcile_content_sections(evaluation: dict[str, Any],
                               resume_text: str) -> dict[str, Any]:
    """Correct Responsibilities/Achievements verdicts against the
    resume text. Returns the transparency record stored on the
    analysis entry as ``content_evidence``."""
    responsibilities = find_responsibility_evidence(resume_text)
    achievements = find_achievement_evidence(resume_text)
    return {
        "responsibilities": _reconcile_one(
            evaluation, "Responsibilities", responsibilities,
            RESPONSIBILITIES_MIN_DUTIES, "Work Experience",
            "Responsibility content documented within the resume's "
            "experience narrative ({count} duty statements found); "
            "no dedicated Responsibilities heading."),
        "achievements": _reconcile_one(
            evaluation, "Achievements", achievements,
            ACHIEVEMENTS_MIN_OUTCOMES, "Work Experience / Summary",
            "Measurable achievement content documented in the "
            "resume ({count} outcome statements found); no "
            "dedicated Achievements heading."),
    }


# --------------------------------------------------------------------- #
# Section classification reconciliation (deterministic, no scoring)
# --------------------------------------------------------------------- #
# Classification corrections that follow from the evaluation's OWN
# content and the candidate's career stage. These adjust which
# sections are REPORTED as missing; they never touch any score —
# scores remain exactly what the model produced (changing scoring
# requires a separate validation exercise).

_ENTRY_STAGES = {"student", "fresher", "entry-level"}
# Optional for entry-level candidates: reporting them "missing"
# penalises candidates for sections their stage does not call for
# (Publications also exists as a good-to-have section).
_ENTRY_OPTIONAL_SECTIONS = {"publications", "research experience"}


def _section_name(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("section") or "").strip().lower()
    return ""


def reconcile_section_classification(
        evaluation: dict[str, Any]) -> list[str]:
    """Remove missing-section verdicts that misclassify the resume:

    - "Academic Projects" is a subtype of "Projects": when Projects
      content was recognised, a separate missing verdict for
      Academic Projects double-counts one absence.
    - For Student / Fresher / Entry-Level candidates, Publications
      and Research Experience are optional sections, not expected
      content — they are not reported missing for those stages.

    Returns the list of corrections applied (stored on the entry
    for transparency). Scores are never modified.
    """
    actions: list[str] = []
    missing = evaluation.get("missing_sections")
    if not isinstance(missing, list):
        return actions
    matching = {_section_name(s)
                for s in evaluation.get("matching_sections") or []}
    stage = str(evaluation.get("career_stage") or "").strip().lower()
    kept = []
    for item in missing:
        name = _section_name(item)
        if name == "academic projects" and "projects" in matching:
            actions.append(
                "Academic Projects not reported missing: Projects "
                "content is present (Academic Projects is a subtype "
                "of Projects).")
            continue
        if stage in _ENTRY_STAGES and name in _ENTRY_OPTIONAL_SECTIONS:
            actions.append(
                f"{item.get('section')} not reported missing: "
                "optional section for the candidate's career stage "
                f"({evaluation.get('career_stage')}).")
            continue
        kept.append(item)
    if actions:
        evaluation["missing_sections"] = kept
    return actions
