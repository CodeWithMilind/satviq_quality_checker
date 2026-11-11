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
"""

from __future__ import annotations

import json
from typing import Any

from . import config as eval_config
from .schema import Evaluation

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
    """The evaluation instructions (developer message)."""
    config = config or eval_config.load_config()
    sections = "; ".join(
        f"{s['name']} ({s['importance']})"
        for s in config["standard_sections"])
    good_to_have = "; ".join(config["good_to_have_sections"])
    stages = "\n".join(
        f"- {stage['name']}: judge mainly on "
        + ", ".join(stage.get("evaluation_focus") or [])
        for stage in config["career_stages"])
    roles = "\n".join(
        f"- {role['name']} (also: "
        + (", ".join(role.get("alt_titles") or []) or "—")
        + ") — required: "
        + ", ".join(role.get("required_skills") or [])
        + "; preferred: "
        + ", ".join(role.get("preferred_skills") or [])
        for role in config["role_taxonomy"])
    examples = "; ".join(
        f'"{heading}" means {standard}'
        for heading, standard in _SECTION_EXAMPLES)
    bands = ", ".join(
        f"{band['min_score']}-100 {band['label']}"
        if band is config["classification_bands"][0]
        else f"{band['min_score']}+ {band['label']}"
        for band in config["classification_bands"])
    schema = json.dumps(Evaluation.model_json_schema())
    return (
        "You are the resume EVALUATION engine of SATVIQ. You judge a "
        "resume; you do not rewrite it and you do not extract it "
        "again. Reply with ONLY a JSON object matching this schema:\n"
        f"{schema}\n\n"
        "EVIDENCE RULES (absolute):\n"
        "- Base every statement on the resume text provided. Never "
        "invent candidate information, skills, experience, dates or "
        "achievements that are not in the text.\n"
        "- Every strength, weakness, missing section and role "
        "recommendation must cite supporting evidence from the text "
        "(or, for something missing, the absence you verified).\n"
        "- Do not use protected personal characteristics (age, "
        "gender, religion, caste, nationality, marital status, "
        "photograph, etc.) for scoring in any way.\n\n"
        "SECTIONS:\n"
        f"- Standard sections to look for: {sections}.\n"
        "- Recognise sections by MEANING, not by exact heading text. "
        f"Examples of equivalent headings: {examples}. These are "
        "examples, not a fixed dictionary.\n"
        "- NEVER classify a section as missing simply because its "
        "heading is unfamiliar — read what the content actually is. "
        "For each recognised section report the standardized name, "
        "the original heading as written, whether it is complete, "
        "partial or insufficient, a short summary of what it "
        "contains, evidence, and remarks.\n"
        "- For each standard section that is missing or contains "
        "insufficient information, report its importance (Critical / "
        "High / Medium / Low), why, the impact on the resume, and a "
        "concrete improvement recommendation.\n"
        "- CONTENT counts, not just headings — two sections in "
        "particular: RESPONSIBILITIES are usually documented as the "
        "duty bullets inside Work Experience / Professional "
        "Experience entries, with no dedicated heading; when duty "
        "statements exist there, Responsibilities is PRESENT "
        "(report it under matching sections with the original "
        "heading noted as embedded in Work Experience). ACHIEVEMENTS "
        "are measurable outcomes — percentages, growth figures, "
        "scaled results, awards — which usually live inside the "
        "summary and job bullets; when such outcome evidence "
        "exists, Achievements is PRESENT even without a dedicated "
        "heading. For both: only report the section missing when "
        "the CONTENT is absent from the whole resume; a dedicated "
        "heading may be suggested as a presentation improvement in "
        "remarks, never as missing content.\n"
        f"- Good-to-have sections: {good_to_have}. Report whether "
        "each is present and what it would add. NEVER penalise any "
        "score for a missing good-to-have section.\n\n"
        "CAREER STAGE:\n"
        "- First identify the candidate's career stage, then judge "
        "the resume against THAT stage's expectations:\n"
        f"{stages}\n"
        "- A fresher or student is NEVER penalised for missing "
        "professional work experience; judge them on projects, "
        "internships, skills, certifications and coursework. A "
        "career changer is judged on transferable skills and "
        "demonstrated practical knowledge of the target field, not "
        "on missing direct experience.\n\n"
        "THE THREE SCORES (each 0-100, each INDEPENDENT — never "
        "compute or imply a combined/overall score):\n"
        "1. completeness.score — important section presence, "
        "information completeness, relevant candidate details, "
        "measured against the candidate's career stage.\n"
        "2. quality.score — readability, clarity, professional "
        "presentation, relevance, summary quality, skills "
        "presentation, project and experience descriptions, "
        "measurable achievements, organisation, consistency, "
        "repetition and irrelevant information.\n"
        "3. role_suitability[].score — per role: matching skills, "
        "relevant projects, work experience, education, "
        "certifications and practical knowledge.\n"
        f"Classification bands (for your understanding): {bands}. "
        "For each of completeness and quality, the explanation must "
        "say specifically why the score is what it is: which "
        "sections contributed, what information is missing, what "
        "problems were found, what to improve, and which "
        "improvements to prioritise first. Avoid generic "
        "explanations.\n\n"
        "JOB ROLES:\n"
        "- Recommend roles ONLY from the taxonomy below (closely "
        "matching additional roles are allowed only when the resume "
        "evidence clearly supports them). No job description is "
        "provided — judge from the resume alone.\n"
        "- A skill keyword match alone is NOT suitability: weigh "
        "projects, experience, education and demonstrated practical "
        "knowledge. Never recommend a role without supporting "
        "evidence, and list the required skills the candidate is "
        "missing for each role.\n"
        f"{roles}\n\n"
        "Also provide strengths, weaknesses and prioritised "
        "improvement recommendations for the candidate. Keep every "
        "string concise and factual."
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
