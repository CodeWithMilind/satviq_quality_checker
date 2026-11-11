"""Deterministic builder for the canonical AI Evaluation JSON.

build_canonical(record) projects ONE stored SATVIQ record into the
`ai-evaluation/1.1` document. It performs NO AI calls and invents
NOTHING: every value comes from the record's extraction output
(resume_json), its latest successful AI evaluation, or a documented
deterministic derivation (durations from dates, education level from
degree text, skill categories from the curated dictionary).

Source separation: extraction data feeds profile/skills/experience/
education/projects/certifications; AI interpretation feeds
candidate_evaluation/completeness/quality/recommendations; derived
values are limited to the mechanical transforms above and are
identifiable in `data_reliability.evidence_sources`.
"""

from __future__ import annotations

import re
from typing import Any

from .categories import categorize
from .schema import SCHEMA_VERSION

_YEAR_RE = re.compile(r"(19|20)\d{2}")

_LEVELS = (
    ("doctorate", ("phd", "ph.d", "doctorate", "d.sc")),
    ("masters", ("mca", "m.tech", "mtech", "mba", "m.sc", "msc",
                 "master", "m.e.", "m.com", "post graduate",
                 "postgraduate", "pgdm", "pg diploma")),
    ("bachelors", ("bca", "b.tech", "btech", "b.e.", "b.sc", "bsc",
                   "bachelor", "b.com", "bba", "b.a.", "ug")),
    ("diploma", ("diploma", "iti", "polytechnic")),
    ("certificate", ("certificate", "certification course")),
    ("school", ("12th", "hsc", "intermediate", "10th", "ssc",
                "higher secondary")),
)


def _clean(v: Any) -> str | None:
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def _list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _years_in(text: Any) -> int | None:
    if not text:
        return None
    m = _YEAR_RE.search(str(text))
    return int(m.group(0)) if m else None


def _education_level(degree: str | None) -> str | None:
    if not degree:
        return None
    low = f" {degree.lower()} "
    for level, needles in _LEVELS:
        if any(n in low for n in needles):
            return level
    return None


def _latest_success(record: dict) -> dict | None:
    analysis = record.get("analysis")
    if isinstance(analysis, dict) and analysis.get("status") == "success":
        return analysis
    for entry in reversed(_list(record.get("analyses"))):
        if isinstance(entry, dict) and entry.get("status") == "success":
            return entry
    return None


def _duration_helpers():
    from api.db import _duration_months, _total_experience_years
    return _duration_months, _total_experience_years


def build_metadata(record: dict, entry: dict | None) -> dict:
    meta = {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": record.get("resume_id"),
        "resume_id": record.get("resume_id"),
        "evaluation_id": None, "evaluated_at": None,
        "model_name": None, "model_version": None,
        "prompt_version": None, "evaluation_version": None,
        "evaluation_config_version": None, "yaml_version": None,
    }
    if entry:
        meta.update({
            "evaluation_id": entry.get("analysis_id"),
            "evaluated_at": entry.get("created_at"),
            "model_name": entry.get("model"),
            "evaluation_version": entry.get("analysis_version"),
            "evaluation_config_version": entry.get("config_version"),
            "yaml_version": entry.get("yaml_version"),
        })
    return meta


def build_profile(record: dict, ev: dict, rj: dict,
                  total_years: float | None) -> dict:
    cand = ev.get("candidate") or {}
    stage = _clean(ev.get("career_stage"))
    return {
        "full_name": _clean(cand.get("name")) or _clean(rj.get("full_name")),
        "professional_headline": _clean(cand.get("headline")),
        "professional_summary": _clean(cand.get("summary"))
        or _clean(rj.get("summary")),
        "career_stage": stage,
        "total_experience_years": total_years,
        "professional_domain": _clean(ev.get("professional_domain")),
        "industry": None,  # no stored source separates industry
        "seniority": stage,  # derived: stage IS the seniority class
        "location": _clean(cand.get("location"))
        or _clean(rj.get("location")),
    }


def build_skills(rj: dict, ev: dict) -> dict:
    buckets = rj.get("skills") or {}
    if isinstance(buckets, list):
        buckets = {"technical_skills": buckets}
    out: dict[str, list] = {k: [] for k in (
        "technical_skills", "programming_languages", "frameworks",
        "libraries", "databases", "cloud_platforms", "devops_tools",
        "ai_ml_skills", "software_tools", "domain_skills",
        "soft_skills", "uncategorized")}
    seen: set[tuple[str, str]] = set()

    def push(name: str, category: str, target: str) -> None:
        key = (name.lower(), target)
        if key in seen or not name.strip():
            return
        seen.add(key)
        out[target].append({
            "skill_name": name.strip(), "category": category,
            "evidence": [], "source_section": None, "confidence": None})

    for bucket, names in buckets.items():
        if not isinstance(names, list):
            continue
        for name in names:
            if not isinstance(name, str):
                continue
            if bucket == "soft_skills":
                push(name, "soft_skills", "soft_skills")
                continue
            category = categorize(name, bucket)
            push(name, category, category)
            if bucket == "technical_skills":
                push(name, category, "technical_skills")
            if bucket == "tools":
                push(name, category, "software_tools")
    # Domain skills attested by the AI evaluation's domain label are
    # NOT skills; domain_skills only fills from the dictionary above.
    return out


def build_experience(rj: dict) -> list[dict]:
    duration_months, _ = _duration_helpers()
    out = []
    for item in _list(rj.get("experience")):
        if not isinstance(item, dict):
            continue
        start, end = item.get("start_date"), item.get("end_date")
        duration = None
        if start or end:
            duration = " – ".join(
                str(x) for x in (start, end) if x) or None
        out.append({
            "company": _clean(item.get("company")),
            "designation": _clean(item.get("title")),
            "duration": duration,
            "duration_months": duration_months(
                start, end, item.get("is_current")),
            "is_current": item.get("is_current")
            if isinstance(item.get("is_current"), bool) else None,
            # Only when the source itself separates them — the
            # extraction's merged highlights are never split by guess.
            "responsibilities": item.get("responsibilities")
            if isinstance(item.get("responsibilities"), list) else None,
            "achievements": item.get("achievements")
            if isinstance(item.get("achievements"), list) else None,
            "highlights": [str(h) for h in _list(item.get("highlights"))],
            "technologies": [str(t) for t in _list(
                item.get("technologies") or item.get("tech_stack"))],
            "industry": _clean(item.get("industry")),
            "supporting_evidence": [str(e) for e in _list(
                item.get("evidence"))],
        })
    return out


def build_education(rj: dict) -> list[dict]:
    out = []
    for item in _list(rj.get("education")):
        if not isinstance(item, dict):
            continue
        degree = _clean(item.get("degree"))
        out.append({
            "institution": _clean(item.get("institution")),
            "degree": degree,
            "specialization": _clean(item.get("field_of_study")),
            "start_year": _years_in(item.get("start_date")),
            "end_year": _years_in(item.get("end_date")),
            "grade": _clean(item.get("score")) or _clean(item.get("grade")),
            "education_level": _education_level(degree),
        })
    return out


def build_projects(rj: dict, ev: dict) -> list[dict]:
    out = []
    seen: set[str] = set()
    for item in _list(rj.get("projects")):
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name") or item.get("title"))
        out.append({
            "project_name": name,
            "description": _clean(item.get("description")),
            "technologies": [str(t) for t in _list(
                item.get("tech_stack") or item.get("technologies"))],
            "contributions": [], "outcomes": [],
            "related_skills": [],
            "evidence": [str(e) for e in _list(item.get("evidence"))],
        })
        if name:
            seen.add(name.lower())
    # Project names attested by the AI evaluation (role suitability
    # evidence) — real names read from the resume by the model, not
    # invented structure; descriptions stay null.
    for role in _list(ev.get("role_suitability")):
        if not isinstance(role, dict):
            continue
        for name in _list(role.get("relevant_projects")):
            key = str(name).strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append({
                    "project_name": str(name).strip(),
                    "description": None, "technologies": [],
                    "contributions": [], "outcomes": [],
                    "related_skills": [],
                    "evidence": [f"role_suitability:{role.get('role')}"],
                })
    return out


def build_certifications(rj: dict) -> list[dict]:
    return [{
        "certification_name": str(name).strip(), "issuer": None,
        "issue_date": None, "expiry_date": None, "credential_url": None,
    } for name in _list(rj.get("certifications"))
        if isinstance(name, str) and name.strip()]


def build_evaluation_block(ev: dict, total_years: float | None) -> dict:
    stage = _clean(ev.get("career_stage"))
    if total_years is not None:
        exp_class = f"{stage or 'Unspecified'} — {total_years} years"
    else:
        exp_class = stage
    observations = []
    for s in _list(ev.get("matching_sections")):
        if isinstance(s, dict) and s.get("quality_assessment"):
            observations.append({
                "text": f"{s.get('section')}: {s['quality_assessment']}",
                "evidence": [str(e) for e in _list(s.get("evidence"))],
                "source": "ai_interpretation"})
    ra = ev.get("recruiter_assessment") or {}
    strengths = [{"text": str(p.get("point") or ""),
                  "evidence": [str(e) for e in _list(p.get("evidence"))],
                  "source": "ai_interpretation"}
                 for p in _list(ra.get("strengths"))
                 if isinstance(p, dict)]
    if not strengths:  # historical evaluations: legacy plain lists
        strengths = [{"text": str(s), "evidence": [],
                      "source": "ai_interpretation"}
                     for s in _list(ev.get("strengths"))]
    weaknesses = [{"text": str(p.get("point") or ""),
                   "evidence": [str(e) for e in _list(p.get("evidence"))],
                   "source": "ai_interpretation"}
                  for p in _list(ra.get("limitations"))
                  if isinstance(p, dict)]
    if not weaknesses:
        weaknesses = [{"text": str(s), "evidence": [],
                       "source": "ai_interpretation"}
                      for s in _list(ev.get("weaknesses"))]
    return {
        "candidate_classification": stage,
        "experience_classification": exp_class,
        "professional_domain": _clean(ev.get("professional_domain")),
        "industry": None,
        "seniority": stage,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "observations": observations,
    }


def build_completeness(ev: dict) -> dict:
    comp = ev.get("completeness") or {}
    partial, observations = [], []
    for s in _list(ev.get("matching_sections")):
        if not isinstance(s, dict):
            continue
        if s.get("completeness_status") in ("partial", "insufficient"):
            partial.append(s.get("section"))
        observations.append({
            "section": s.get("section"), "status": "present",
            "completeness_status": s.get("completeness_status"),
            "summary": s.get("summary"), "remarks": s.get("remarks")})
    for s in _list(ev.get("missing_sections")):
        if isinstance(s, dict):
            observations.append({
                "section": s.get("section"), "status": "missing",
                "importance": s.get("importance"),
                "reason": s.get("reason"), "impact": s.get("impact")})
    return {
        "completeness_score": comp.get("score"),
        "classification": _clean(comp.get("classification")),
        "present_sections": [s.get("section") for s in
                             _list(ev.get("matching_sections"))
                             if isinstance(s, dict)],
        "missing_sections": [s.get("section") for s in
                             _list(ev.get("missing_sections"))
                             if isinstance(s, dict)],
        "partial_sections": partial,
        "section_level_observations": observations,
    }


def build_quality(ev: dict) -> dict:
    qual = ev.get("quality") or {}
    expl = qual.get("explanation") or {}
    content_obs = []
    if expl.get("why_low"):
        content_obs.append(str(expl["why_low"]))
    content_obs += [str(p) for p in _list(expl.get("problems"))]
    return {
        "quality_score": qual.get("score"),
        "classification": _clean(qual.get("classification")),
        "readability_observations": [],
        "structure_observations": [str(s) for s in _list(
            expl.get("contributing_sections"))],
        "content_observations": content_obs,
        "formatting_issues": [],
        "professional_presentation": None,
    }


def build_gaps(record: dict, ev: dict, rj: dict) -> dict:
    role_gaps = []
    suitable = set(_list(ev.get("suitable_roles")))
    for role in _list(ev.get("role_suitability")):
        if isinstance(role, dict) and role.get("role") in suitable:
            missing = [str(s) for s in _list(role.get("missing_skills"))]
            if missing:
                role_gaps.append({
                    "role": role.get("role"),
                    "missing_skills": missing,
                    "basis": "role_suitability",
                    "note": "Gap is relative to this role's requirements"
                            " as judged from the resume; the resume not"
                            " mentioning a skill is not proof the"
                            " candidate lacks it."})
    ra = ev.get("recruiter_assessment") or {}
    for gap in _list(ra.get("verified_skill_gaps")):
        if isinstance(gap, dict):
            role_gaps.append({
                "skill_or_area": gap.get("skill_or_area"),
                "basis": gap.get("basis"),
                "evidence": [str(e) for e in _list(gap.get("evidence"))],
                "source": "recruiter_assessment",
                "note": "Verified gap: evidenced by the resume "
                        "itself, per the assessment."})
    unmentioned = [str(u.get("area")) for u in
                   _list(ra.get("unmentioned_areas"))
                   if isinstance(u, dict) and u.get("area")]
    insufficient = [s.get("section") for s in
                    _list(ev.get("matching_sections"))
                    if isinstance(s, dict)
                    and s.get("completeness_status") == "insufficient"]
    uncertainty = [str(w) for w in _list(record.get("warnings"))]
    recovery = record.get("contact_recovery") or {}
    if isinstance(recovery, dict) and recovery.get("status"):
        uncertainty.append(
            f"contact_recovery:{recovery.get('status')}")
    analysis = _latest_success(record)
    if analysis and analysis.get("recovery"):
        uncertainty.append("evaluation_recovery:json_correction_retry")
    return {
        "missing_information": [s for s in
                                _list(ev.get("missing_sections"))
                                if isinstance(s, dict)],
        "unverified_information": unmentioned,
        "extraction_uncertainty": uncertainty,
        "detected_skill_gaps": role_gaps,
        "insufficient_evidence": [str(s) for s in insufficient if s],
    }


def build_reliability(record: dict, ev: dict, rj: dict,
                      doc: dict) -> dict:
    stage = _clean(ev.get("career_stage"))
    has_exp = bool(doc["experience"])
    status: dict[str, str] = {}

    def st(field: str, present: bool, empty_status: str) -> None:
        status[field] = "available" if present else empty_status

    st("full_name", bool(doc["candidate_profile"]["full_name"]),
       "not_detected")
    st("email", bool(_clean(rj.get("email"))), "not_detected")
    st("phone", bool(_clean(rj.get("phone"))), "not_mentioned")
    st("location", bool(doc["candidate_profile"]["location"]),
       "not_mentioned")
    st("summary", bool(doc["candidate_profile"]["professional_summary"]),
       "not_mentioned")
    any_skill = any(doc["skills"][k] for k in doc["skills"])
    st("skills", any_skill, "not_mentioned")
    if has_exp:
        status["experience"] = "available"
    elif stage in ("Student", "Fresher"):
        # For students/freshers, jobs are not expected — not a gap.
        status["experience"] = "not_applicable"
    else:
        status["experience"] = "not_mentioned"
    st("education", bool(doc["education"]), "not_mentioned")
    st("certifications", bool(doc["certifications"]), "not_mentioned")
    st("projects", bool(doc["projects"]), "not_mentioned")
    if not ev:
        status["ai_evaluation"] = "not_detected"
    else:
        status["ai_evaluation"] = "available"

    conflicts = []
    rj_name = _clean(rj.get("full_name"))
    ev_name = _clean((ev.get("candidate") or {}).get("name"))
    if rj_name and ev_name and rj_name.lower() != ev_name.lower():
        conflicts.append(
            f"name differs: extraction '{rj_name}' vs evaluation "
            f"'{ev_name}'")
    rj_email = _clean(rj.get("email"))
    ev_email = _clean((ev.get("candidate") or {}).get("email"))
    if rj_email and ev_email and rj_email.lower() != ev_email.lower():
        conflicts.append(
            f"email differs: extraction '{rj_email}' vs evaluation "
            f"'{ev_email}'")

    uncertain = [f for f, s in status.items()
                 if s in ("uncertain", "not_detected")]
    return {
        "field_level_status": status,
        "evidence_sources": {
            "profile_and_skills": "extracted (resume_json)",
            "experience_education": "extracted (resume_json)",
            "evaluation_sections": "ai_interpretation (evaluation)",
            "durations_and_levels": "derived (deterministic)",
            "skill_categories": "derived (curated dictionary; "
                                "unknown skills stay uncategorized)",
            "final_extractor": record.get("selected_extractor")
            or record.get("extractor_used"),
        },
        "extraction_warnings": [str(w) for w in
                                _list(record.get("warnings"))],
        "conflicting_information": conflicts,
        "uncertain_fields": uncertain,
    }


def _canonical_ra(ev: dict) -> dict | None:
    """The recruiter assessment carried verbatim (None when the
    evaluation predates it). Only known keys are carried, with
    types normalized — content is never reinterpreted."""
    ra = ev.get("recruiter_assessment")
    if not isinstance(ra, dict):
        return None

    def points(items):
        return [{"point": str(p.get("point") or ""),
                 "evidence": [str(e) for e in _list(p.get("evidence"))]}
                for p in _list(items) if isinstance(p, dict)]

    return {
        "summary": str(ra.get("summary") or ""),
        "strengths": points(ra.get("strengths")),
        "limitations": points(ra.get("limitations")),
        "assessment_insights": points(ra.get("assessment_insights")),
        "verified_skill_gaps": [
            {"skill_or_area": str(g.get("skill_or_area") or ""),
             "basis": str(g.get("basis") or ""),
             "evidence": [str(e) for e in _list(g.get("evidence"))]}
            for g in _list(ra.get("verified_skill_gaps"))
            if isinstance(g, dict)],
        "unmentioned_areas": [
            {"area": str(u.get("area") or ""),
             "relevance_note": str(u.get("relevance_note") or "")}
            for u in _list(ra.get("unmentioned_areas"))
            if isinstance(u, dict)],
        "verification_points": [str(v) for v in
                                _list(ra.get("verification_points"))],
        "information_completeness_note": str(
            ra.get("information_completeness_note") or ""),
        "data_reliability_note": str(
            ra.get("data_reliability_note") or ""),
    }


def build_canonical(record: dict) -> dict:
    """Project one stored record into the canonical document.

    Returns a plain dict validated by callers via
    candidate_intelligence.validators.validate_canonical.
    """
    _, total_years_fn = _duration_helpers()
    rj = record.get("resume_json") or record.get("resume_data") or {}
    if not isinstance(rj, dict):
        rj = {}
    entry = _latest_success(record)
    ev = (entry.get("evaluation") if entry else None) or {}
    if not isinstance(ev, dict):
        ev = {}
    experience = [e for e in _list(rj.get("experience"))
                  if isinstance(e, dict)]
    total_years = total_years_fn(experience)
    doc: dict[str, Any] = {
        "metadata": build_metadata(record, entry),
        "candidate_profile": build_profile(record, ev, rj, total_years),
        "skills": build_skills(rj, ev),
        "experience": build_experience(rj),
        "education": build_education(rj),
        "projects": build_projects(rj, ev),
        "certifications": build_certifications(rj),
        "candidate_evaluation": build_evaluation_block(ev, total_years),
        "resume_completeness": build_completeness(ev),
        "resume_quality": build_quality(ev),
        "gaps": build_gaps(record, ev, rj),
        "recruiter_assessment": _canonical_ra(ev),
        "role_suitability": [r for r in _list(ev.get("role_suitability"))
                             if isinstance(r, dict)],
        "suitable_roles": [str(s) for s in _list(ev.get("suitable_roles"))],
        "recommendations": [str(s) for s in _list(ev.get("recommendations"))],
    }
    doc["data_reliability"] = build_reliability(record, ev, rj, doc)
    return doc
