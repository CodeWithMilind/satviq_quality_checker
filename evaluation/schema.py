"""Structured schema for Muse AI resume EVALUATIONS.

The evaluation is a judgement ABOUT a resume (career stage, section
intelligence, three independent scores, role suitability) — distinct
from ``muse_ai.schema.ResumeJSON``, which is the extraction of the
resume itself. Missing information uses the schema's empty
representations; the model is instructed never to invent details,
and validation never fills any in.

:func:`validate_evaluation` mirrors
``muse_ai.schema.validate_resume_json`` (Markdown-fence tolerant,
widest-brace fallback) and then applies DETERMINISTIC
post-validation that no model output can bypass:

- every score is clamped into 0-100;
- every classification is recomputed from the configured bands —
  the model's own classification strings are never trusted;
- ``suitable_roles`` is recomputed from the detailed
  ``role_suitability`` results (score >= the configured threshold,
  sorted by score descending) so the array ALWAYS matches the
  details;
- career-stage and importance strings are normalised onto the
  configured vocabularies when they match case-insensitively.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator

from . import config as eval_config


class CandidateInfo(BaseModel):
    """Factual candidate summary — only what the resume states."""

    name: str = ""
    headline: str = ""        # current title/role as stated, if any
    location: str = ""
    email: str = ""
    summary: str = ""         # brief factual overview of the candidate


class MatchingSection(BaseModel):
    """A standard section recognised in the resume (spec Part 3.1)."""

    section: str = ""                       # standardized section name
    original_heading: str = ""              # heading as written
    presence_status: str = "present"        # present | partial
    completeness_status: str = ""           # complete | partial | insufficient
    quality_assessment: str = ""
    summary: str = ""                       # extracted information summary
    evidence: list[str] = Field(default_factory=list)
    remarks: str = ""


class MissingSection(BaseModel):
    """A standard section that is missing or insufficient (Part 3.2)."""

    section: str = ""
    importance: str = "Medium"              # Critical | High | Medium | Low
    status: str = "missing"                 # missing | insufficient
    reason: str = ""
    impact: str = ""
    recommendation: str = ""
    evidence: list[str] = Field(default_factory=list)

    @field_validator("importance", mode="before")
    @classmethod
    def _importance_normalised(cls, value):
        if isinstance(value, str):
            for level in eval_config.IMPORTANCE_LEVELS:
                if value.strip().lower() == level.lower():
                    return level
        return "Medium"


class GoodToHaveSection(BaseModel):
    """An optional section (Part 3.3) — never penalised when absent."""

    section: str = ""
    present: bool = False
    relevance: str = ""
    potential_value: str = ""
    recommendation: str = ""


class ScoreExplanation(BaseModel):
    """Why a score is what it is (Part 5.4) — specific, never generic."""

    why_low: str = ""          # main reasons behind the score
    contributing_sections: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    improvements: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)  # do first


class ScoreBlock(BaseModel):
    """One independent score (Part 5). The classification is
    recomputed deterministically after validation."""

    score: float = 0
    classification: str = ""
    explanation: ScoreExplanation = Field(default_factory=ScoreExplanation)


class RoleSuitability(BaseModel):
    """One role's evidence-based suitability result (Part 6)."""

    role: str = ""
    score: float = 0
    matching_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    relevant_projects: list[str] = Field(default_factory=list)
    relevant_experience: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    explanation: str = ""


class Evaluation(BaseModel):
    """The full evaluation of one resume (spec Parts 2-6)."""

    candidate: CandidateInfo = Field(default_factory=CandidateInfo)
    career_stage: str = ""
    professional_domain: str = ""
    matching_sections: list[MatchingSection] = Field(default_factory=list)
    missing_sections: list[MissingSection] = Field(default_factory=list)
    good_to_have: list[GoodToHaveSection] = Field(default_factory=list)
    completeness: ScoreBlock = Field(default_factory=ScoreBlock)
    quality: ScoreBlock = Field(default_factory=ScoreBlock)
    role_suitability: list[RoleSuitability] = Field(default_factory=list)
    suitable_roles: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _clamp_score(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(min(100.0, max(0.0, number)), 1)


def _post_validate(evaluation: Evaluation,
                   config: dict[str, Any]) -> dict[str, Any]:
    """Deterministic corrections applied to EVERY parsed evaluation
    before it is stored or displayed (see module docstring)."""
    data = evaluation.model_dump()
    for key in ("completeness", "quality"):
        block = data[key]
        block["score"] = _clamp_score(block.get("score"))
        block["classification"] = eval_config.classification_for(
            block["score"], config)
    for role in data["role_suitability"]:
        role["score"] = _clamp_score(role.get("score"))
    threshold = config.get("suitable_role_min_score", 50)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = 50.0
    ranked = sorted(
        (r for r in data["role_suitability"] if r["score"] >= threshold),
        key=lambda r: (-r["score"], r["role"]))
    seen: list[str] = []
    for role in ranked:
        if role["role"] and role["role"] not in seen:
            seen.append(role["role"])
    data["suitable_roles"] = seen
    stage = str(data.get("career_stage") or "").strip()
    for known in eval_config.career_stage_names(config):
        if stage.lower() == known.lower():
            data["career_stage"] = known
            break
    return data


# --------------------------------------------------------------------- #
# Response parsing: staged, classified, and honest about truncation
# --------------------------------------------------------------------- #
#
# Failure categories (the prefix of every error string returned by
# validate_evaluation, so the service layer and the dashboard can
# distinguish them without parsing prose):
#   empty_response  — the model returned no visible text at all;
#   token_limit     — the text is JSON-shaped but cut off (the
#                     output budget, which reasoning tokens share,
#                     ran out before the object closed);
#   invalid_json    — text exists but no JSON object can be decoded;
#   schema_invalid  — JSON decoded, but the object does not fit the
#                     evaluation schema even after null/type repair.

_STR_LIST_FIELDS = {
    "evidence", "contributing_sections", "missing_information",
    "problems", "improvements", "priorities", "matching_skills",
    "missing_skills", "relevant_projects", "relevant_experience",
    "strengths", "weaknesses", "recommendations",
}
_STR_FIELDS = {
    "name", "headline", "location", "email", "summary", "section",
    "original_heading", "presence_status", "completeness_status",
    "quality_assessment", "remarks", "importance", "status", "reason",
    "impact", "recommendation", "relevance", "potential_value",
    "classification", "why_low", "career_stage", "professional_domain",
    "role", "explanation",
}


def _coerce(value: Any) -> Any:
    """Recursively repair NULL / mistyped leaves the model emits.

    Real Muse evaluation responses intermittently put ``null``
    where the schema wants a string or a list, or a bare string
    where it wants a list of strings. Pydantic (correctly) rejects
    those, and discarding an otherwise complete evaluation over a
    null ``evidence`` helps nobody. This pass repairs TYPES ONLY —
    null string -> "", null/bare-string list -> []/[value], numeric
    string -> number for score-like keys — and never invents
    content: no skill, evidence, score value or recommendation is
    ever created here."""
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if item is None and key in _STR_LIST_FIELDS:
                out[key] = []
            elif item is None and key in _STR_FIELDS:
                out[key] = ""
            elif isinstance(item, str) and key in _STR_LIST_FIELDS:
                out[key] = [item] if item.strip() else []
            elif isinstance(item, list) and key in _STR_FIELDS:
                out[key] = "; ".join(str(v) for v in item)
            elif (key in ("score",) and isinstance(item, str)
                    and item.strip()):
                try:
                    out[key] = float(item)
                except ValueError:
                    out[key] = item
            else:
                out[key] = _coerce(item)
        return out
    if isinstance(value, list):
        return [_coerce(item) for item in value]
    return value


# Top-level keys of the Evaluation model — a decoded object must
# carry at least one of them to be trusted as the evaluation itself
# (guards the prose-scan against accepting nested sub-objects).
_TOP_LEVEL_KEYS = frozenset({
    "candidate", "career_stage", "professional_domain",
    "matching_sections", "missing_sections", "good_to_have",
    "completeness", "quality", "role_suitability", "suitable_roles",
    "strengths", "weaknesses", "recommendations"})


def _decode_candidates(raw_text: str) -> tuple[dict | None, bool]:
    """Try to decode ONE JSON object from a model response.

    Returns ``(parsed_dict, saw_json_start)``. Strategies, in
    order: the whole text; the de-fenced text; a raw_decode scan
    from EVERY ``{`` (handles prose before/after the object and
    stray braces in surrounding text); each strategy retried with
    ``strict=False`` so literal control characters inside strings
    (a known model quirk) do not kill an otherwise valid object.
    ``saw_json_start`` reports whether the text contains a ``{``
    at all — used to tell truncation apart from plain non-JSON."""
    texts = [raw_text.strip(), _FENCE_RE.sub("", raw_text).strip()]
    decoders = (json.JSONDecoder(strict=True),
                json.JSONDecoder(strict=False))
    for text in texts:
        if not text:
            continue
        for strict in (True, False):
            try:
                parsed = json.loads(text, strict=strict)
                if isinstance(parsed, dict):
                    return parsed, True
            except (json.JSONDecodeError, TypeError):
                pass
        # Prose-wrapped: scan "{" positions, but ONLY trust an
        # object carrying the evaluation's own TOP-LEVEL keys. A
        # nested sub-object that happens to decode on its own (an
        # inner block inside a TRUNCATED response — a "candidate",
        # "explanation" or section object) decodes cleanly but its
        # keys are field names, never top-level keys, so it is
        # never mistaken for the evaluation itself; surrounding
        # prose after a genuine top-level object is harmless
        # because the scan finds that object first. NOTE:
        # raw_decode takes no strict argument — strictness lives
        # on the decoder instance.
        for match in re.finditer(r"\{", text):
            for decoder in decoders:
                try:
                    parsed, _ = decoder.raw_decode(text[match.start():])
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if isinstance(parsed, dict) \
                        and _TOP_LEVEL_KEYS & parsed.keys():
                    return parsed, True
    return None, "{" in raw_text


def diagnose_failure(raw_text: str, finish_reason: str | None,
                     reasoning_tokens: int | None,
                     completion_tokens: int | None) -> tuple[str, str]:
    """Classify a response that produced NO usable JSON.

    Returns ``(category, detail)`` — see the category list at the
    top of this section. Uses only response metadata + shape, never
    the resume content."""
    text = (raw_text or "").strip()
    if not text:
        detail = "the model returned no visible content"
        if finish_reason:
            detail += f" (finish_reason={finish_reason})"
        if (reasoning_tokens is not None and completion_tokens
                and reasoning_tokens >= completion_tokens):
            detail += (" — the entire output budget was spent on "
                       "reasoning tokens before any visible output")
        return "empty_response", detail
    if finish_reason == "length" or not text.endswith("}"):
        return ("token_limit",
                "the response appears truncated ("
                + (f"finish_reason={finish_reason}" if finish_reason
                   else "the JSON object never closes")
                + ") — the output token budget, which reasoning "
                "tokens share, was likely exhausted mid-response")
    return ("invalid_json",
            "no JSON object could be decoded from the response")


def validate_evaluation(
        raw_text: str,
        config: dict[str, Any] | None = None,
        ) -> tuple[dict | None, str | None]:
    """Parse + validate model output against :class:`Evaluation`.

    Returns ``(evaluation_dict, None)`` on success — with all
    deterministic post-validation applied — or ``(None, error)``
    where the error is prefixed with its failure category
    (``empty_response:`` / ``token_limit:`` / ``invalid_json:`` /
    ``schema_invalid:``). Markdown fences, surrounding prose and
    literal control characters are tolerated; null/mistyped leaves
    are type-repaired (never content-filled) before validation.
    Truncated or otherwise undecodable output is rejected — never
    partially accepted."""
    if not raw_text or not raw_text.strip():
        return None, "empty_response: Model returned no text."
    config = config or eval_config.load_config()
    parsed, saw_json = _decode_candidates(raw_text)
    if parsed is None:
        if saw_json and not raw_text.rstrip().endswith("}"):
            return None, ("token_limit: Model output was not valid "
                          "JSON — the response appears truncated "
                          "(the JSON object never closes).")
        return None, "invalid_json: Model output was not valid JSON."
    try:
        evaluation = Evaluation(**_coerce(parsed))
    except Exception as exc:  # pydantic ValidationError
        return None, f"schema_invalid: Schema validation failed: {exc}"
    return _post_validate(evaluation, config), None
