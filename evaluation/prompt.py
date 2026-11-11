"""Prompt construction for the Muse AI resume EVALUATION call.

The evaluation receives exactly three inputs, all derived from the
record the existing pipeline already produced — extraction is never
repeated and no other extractor's text is ever sent:

1. The record's Selected Normalized Text (the same text the
   extraction stage sent to Muse), falling back to the final /
   extracted text when no normalization block exists.
2. A clearly labelled DETERMINISTIC Quality Checker context block:
   the selected attempt's filter scores and detected/missing
   sections. The checker matches known headings deterministically
   and can miss sections written under nonstandard headings, so the
   block is context for cross-checking, never a verdict the model
   must copy.
3. A compact summary of the already-extracted Resume JSON, when one
   exists, so the evaluation stays consistent with the extraction.

The instructions encode the spec's hard rules: evidence only, never
invent; career-stage-aware criteria; three INDEPENDENT scores and
no combined score; section equivalence by meaning, not by heading
text; roles only with supporting evidence.

Prompt version eval-prompt/2.0 (B2B transformation): the audience is
a recruiter/assessor, not the candidate. The response gains a
`recruiter_assessment` object (evidenced strengths/limitations,
verified gaps strictly separated from unmentioned areas,
verification points, and business-language notes for the two
scores); the candidate-facing fields (recommendations, top-level
strengths/weaknesses, explanation improvements/priorities) remain in
the schema for historical compatibility and are instructed EMPTY.
The instruction text was also tightened for token efficiency —
rules are stated once, in compact form, without changing any
scoring semantics.
"""

from __future__ import annotations

import json
from typing import Any

from . import config as eval_config
from .schema import Evaluation

PROMPT_VERSION = "eval-prompt/2.1"

# Section-equivalence examples from the spec (Part 3.1). These are
# EXAMPLES of equivalent headings, not a fixed dictionary: the model
# must judge equivalence by meaning.
_SECTION_EXAMPLES = (
    ("Candidate Snapshot", "Professional Summary"),
    ("Skill Inventory", "Technical Skills"),
    ("Professional Journey", "Work Experience"),
    ("Build Showcase", "Projects"),
    ("Credential Vault", "Certifications"),
    ("Academic Background", "Education"),
)


def selected_text(record: dict[str, Any]) -> str:
    """The one text an evaluation is allowed to be based on."""
    block = record.get("text_normalization") or {}
    if isinstance(block, dict):
        text = block.get("normalized_text")
        if text and str(text).strip():
            return str(text)
    for key in ("final_extracted_text", "extracted_text"):
        text = record.get(key)
        if text and str(text).strip():
            return str(text)
    return ""


def _selected_quality_report(record: dict[str, Any]) -> dict[str, Any] | None:
    """The selected attempt's Quality Checker report, if stored."""
    quality = record.get("quality")
    if isinstance(quality, dict) and quality:
        return quality
    for attempt in record.get("attempts") or []:
        if not isinstance(attempt, dict):
            continue
        if attempt.get("is_final_selected") or attempt.get("selected"):
            report = attempt.get("quality_checker_report")
            if isinstance(report, dict):
                return report
    return None


def quality_checker_context(record: dict[str, Any]) -> str:
    """The deterministic-checker context block ("" when no report)."""
    report = _selected_quality_report(record)
    if not report:
        return ""
    lines = [
        "DETERMINISTIC QUALITY CHECKER CONTEXT (produced by SATVIQ's "
        "deterministic Quality Checker, NOT by an AI — use it to "
        "cross-check your section findings, never copy it blindly):"]
    extractor = (report.get("source_tool")
                 or record.get("extractor_used") or "")
    if extractor:
        lines.append(f"Selected extractor: {extractor}")
    scores = []
    for key, label in (("text_health", "Text Health"),
                       ("completeness", "Completeness"),
                       ("reading_order", "Reading Order")):
        part = report.get(key)
        if isinstance(part, dict) and isinstance(part.get("score"), (int, float)):
            scores.append(f"{label}: {part['score']}/100")
    if scores:
        lines.append("Filter scores — " + " · ".join(scores))
    completeness = report.get("completeness")
    if isinstance(completeness, dict):
        metrics = completeness.get("metrics") or {}
        detected = metrics.get("detected_sections") or []
        missing = metrics.get("missing_sections") or []
        if detected:
            lines.append("Sections the checker detected: "
                         + ", ".join(str(s) for s in detected))
        if missing:
            lines.append("Sections the checker did NOT detect: "
                         + ", ".join(str(s) for s in missing))
    lines.append(
        "Important: the checker matches a fixed set of known headings "
        "deterministically, so it can miss a section that IS present "
        "under a nonstandard heading, and it can only see the "
        "extracted text. Verify every section against the resume "
        "text itself before calling it missing.")
    return "\n".join(lines)


def _resume_json_summary(record: dict[str, Any]) -> str:
    """A compact digest of the stored Resume JSON ("" when absent)."""
    data = record.get("resume_json") or record.get("resume_data")
    llm = record.get("llm") or {}
    if not isinstance(data, dict) and isinstance(llm, dict):
        data = llm.get("resume_json")
    if not isinstance(data, dict) or not data:
        return ""
    skills = data.get("skills") or {}
    flat_skills: list[str] = []
    if isinstance(skills, dict):
        for values in skills.values():
            if isinstance(values, list):
                flat_skills.extend(str(v) for v in values)
    elif isinstance(skills, list):
        flat_skills = [str(v) for v in skills]
    digest = {
        "name": data.get("full_name") or "",
        "email_present": bool(data.get("email")),
        "location": data.get("location") or "",
        "skills": flat_skills[:40],
        "experience": [
            {"title": item.get("title") or "",
             "company": item.get("company") or ""}
            for item in (data.get("experience") or [])
            if isinstance(item, dict)][:10],
        "education": [
            {"degree": item.get("degree") or "",
             "institution": item.get("institution") or ""}
            for item in (data.get("education") or [])
            if isinstance(item, dict)][:6],
        "certifications": [str(c) for c in
                           (data.get("certifications") or [])][:15],
        "languages": [str(v) for v in (data.get("languages") or [])][:10],
    }
    return ("ALREADY-EXTRACTED RESUME JSON (compact summary of what "
            "the extraction stage produced — stay consistent with "
            "it, and prefer the resume text where they conflict):\n"
            + json.dumps(digest, ensure_ascii=False))


def build_instructions(config: dict[str, Any] | None = None) -> str:
    """The evaluation instructions (developer message).

    Token-optimized: dynamic lists use compact separators and every
    rule is stated exactly once. Scoring semantics are identical to
    eval-prompt/1.x; only wording, audience and the recruiter
    assessment block changed.
    """
    config = config or eval_config.load_config()
    sections = "; ".join(
        f"{s['name']} ({s['importance']})"
        for s in config["standard_sections"])
    good_to_have = "; ".join(config["good_to_have_sections"])
    stages = "\n".join(
        f"- {stage['name']}: "
        + ", ".join(stage.get("evaluation_focus") or [])
        for stage in config["career_stages"])
    roles = "\n".join(
        f"- {role['name']} ["
        + (", ".join(role.get("alt_titles") or []) or "-")
        + "] req: "
        + ", ".join(role.get("required_skills") or [])
        + "; pref: "
        + ", ".join(role.get("preferred_skills") or [])
        for role in config["role_taxonomy"])
    examples = "; ".join(
        f'"{heading}"={standard}'
        for heading, standard in _SECTION_EXAMPLES)
    bands = ", ".join(
        f"{band['min_score']}+ {band['label']}"
        for band in config["classification_bands"])
    schema = json.dumps(Evaluation.model_json_schema(),
                        separators=(",", ":"))
    return (
        "You are SATVIQ's candidate ASSESSMENT engine. Your reader is "
        "a recruiter assessing this candidate for business use — not "
        "the candidate. Assess only; do not rewrite, coach or "
        "re-extract. Reply with ONLY a JSON object matching:\n"
        f"{schema}\n\n"
        "EVIDENCE (absolute): base every statement on the resume "
        "text; never invent information, skills, dates or "
        "achievements. Every finding cites evidence from the text "
        "(for something missing, the absence you verified). Never "
        "use protected characteristics (age, gender, religion, "
        "caste, nationality, marital status, photograph) in scoring. "
        "Write for a recruiter deciding whether and how to progress "
        "this candidate — factual, specific, no advice addressed to "
        "the candidate.\n\n"
        "SECTIONS:\n"
        f"- Standard: {sections}.\n"
        "- Recognise sections by MEANING, not heading text. "
        f"Equivalences (examples, not a dictionary): {examples}. "
        "Never mark a section missing for an unfamiliar heading — "
        "judge its content. NEVER classify a section as missing for "
        "an unfamiliar heading. For each recognised section: standard "
        "name, original heading, complete/partial/insufficient, "
        "summary, evidence, remarks.\n"
        "- For each missing/insufficient standard section: "
        "importance (Critical/High/Medium/Low), what "
        "decision-relevant information is unavailable to an "
        "assessor, and the impact on assessing this candidate. Its "
        "`recommendation` field states what an assessor should note "
        "or verify (may be empty) — never candidate advice.\n"
        "- CONTENT over headings: RESPONSIBILITIES are the duty "
        "bullets inside Work Experience entries (no dedicated "
        "heading needed); ACHIEVEMENTS are measurable outcomes "
        "(percentages, growth, scale, awards) anywhere in the text. "
        "Report each as missing ONLY when its content is absent "
        "from the whole resume; a dedicated heading may be noted in "
        "remarks as presentation-only.\n"
        "- APPLICABILITY: Academic Projects is a subtype of "
        "Projects — never report it missing when Projects content "
        "exists. Publications and Research Experience are OPTIONAL "
        "for Student/Fresher/Entry-Level candidates: do not report "
        "them missing or let them affect any score for those stages "
        "unless the candidate's domain clearly requires them.\n"
        f"- Good-to-have: {good_to_have}. Report presence/value; "
        "never penalise any score for a missing one.\n\n"
        "CAREER STAGE: identify it first, then judge against that "
        "stage's expectations:\n"
        f"{stages}\n"
        "Freshers/students are NEVER penalised for missing "
        "professional experience (judge projects, internships, "
        "skills, certifications, coursework). Career changers are "
        "judged on transferable skills and demonstrated knowledge "
        "of the target field.\n\n"
        "THREE SCORES (0-100 each, INDEPENDENT — never compute or "
        "imply a combined/overall score):\n"
        "1. completeness = CANDIDATE INFORMATION COMPLETENESS: how "
        "much decision-relevant information the resume gives an "
        "assessor, vs the candidate's stage. Low = the business has "
        "little to decide with; NOT an ability judgement.\n"
        "2. quality = RESUME DATA RELIABILITY: readability, "
        "clarity, consistency, organisation, specificity, "
        "measurable achievements, relevance. Low = claims need "
        "verification before reliance; NOT a qualification "
        "judgement.\n"
        "3. role_suitability[] = per-role fit (skills, projects, "
        "experience, education, certifications, practical "
        "knowledge).\n"
        f"Bands: {bands}. Each score explanation states why: "
        "contributing sections, missing information, problems — "
        "phrased as what the score means for assessing this "
        "candidate. Leave explanation.improvements and "
        "explanation.priorities EMPTY.\n\n"
        "ROLES: recommend ONLY from this taxonomy (a close extra "
        "role only when evidence clearly supports it). Judge from "
        "the resume alone — no job description exists. A keyword "
        "match alone is NOT suitability; never recommend without "
        "evidence; list each role's missing required skills.\n"
        f"{roles}\n\n"
        "RECRUITER ASSESSMENT (recruiter_assessment):\n"
        "- summary: 2-4 sentences, who this candidate is "
        "professionally, for a recruiter.\n"
        "- strengths / limitations / assessment_insights: each "
        "{point, evidence[]}. Limitations must be evidenced IN the "
        "resume (thin descriptions, unquantified claims, narrow "
        "scope, inconsistencies) — NEVER mere absences. Insights: "
        "fit signals, trajectory, consistency between sections, "
        "stage-appropriate strength of evidence.\n"
        "- NOT verified gaps: missing metrics or quantified "
        "outcomes, dataset sizes, deployment/production evidence "
        "and project outcomes are DOCUMENTATION limitations "
        "(record them under limitations) — they say the resume "
        "under-documents the work, not that the candidate lacks a "
        "competency — unless the resume explicitly demonstrates an "
        "actual competency shortfall.\n"
        "- verified_skill_gaps vs unmentioned_areas — keep "
        "strictly separate. A gap {skill_or_area, basis, "
        "evidence[]} requires the resume itself to evidence the "
        "shortfall (claimed expertise contradicted by described "
        "work; duties described without the role's standard "
        "competency). unmentioned_areas {area, relevance_note} are "
        "plausibly relevant but simply absent — absence of evidence "
        "is NOT evidence of absence, never a deficiency. A role's "
        "missing_skills entry (e.g. Docker for Python Developer) "
        "is unmentioned unless independently evidenced as a gap.\n"
        "- verification_points: what a recruiter should confirm in "
        "screening/interview (unverified claims, chronology gaps, "
        "skills without evidence, certifications without issuer).\n"
        "- information_completeness_note / data_reliability_note: "
        "1-2 sentences each, for THIS candidate, on what the two "
        "scores mean in practice (see score definitions).\n"
        "Leave EMPTY: top-level recommendations, strengths, "
        "weaknesses (their content lives, with evidence, in "
        "recruiter_assessment). Do not repeat the same evidence "
        "verbatim across fields. Keep every string concise and "
        "factual."
    )


def build_prompt(record: dict[str, Any],
                 config: dict[str, Any] | None = None,
                 ) -> tuple[str, str]:
    """(instructions, user_text) for one record's evaluation call."""
    config = config or eval_config.load_config()
    parts = ["Evaluate the following resume.\n"]
    context = quality_checker_context(record)
    if context:
        parts.append(context + "\n")
    digest = _resume_json_summary(record)
    if digest:
        parts.append(digest + "\n")
    parts.append("RESUME TEXT (the selected normalized text — the "
                 "only resume content to evaluate):\n"
                 + selected_text(record))
    return build_instructions(config), "\n".join(parts)
