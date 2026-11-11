"""Canonical AI Evaluation JSON — schema `ai-evaluation/1.1`.

A versioned, analytics-ready projection of one resume's stored
SATVIQ data (extraction + rule-based quality + AI evaluation),
built deterministically — this schema never calls an AI model.

Source separation (a hard rule of the design): every section is
sourced from exactly one layer, and `DataReliability` says which:
  - EXTRACTED  : information taken from the resume (resume_json)
  - AI         : Muse's semantic interpretation (evaluation)
  - DERIVED    : computed deterministically by the application
Nothing is fabricated: unsupported fields are null, and field
status uses the controlled vocabulary in `FieldStatus`.

Scores are 0-100 and classifications use the evaluation config's
bands. AI completeness/quality live ONLY in sections I/J; the
rule-based Quality Checker numbers are NOT part of this document
(they remain in the record/quality report, a separate system).
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

SCHEMA_VERSION = "ai-evaluation/1.1"
# Historical documents stored as ai-evaluation/1.0 remain readable:
# the only 1.1 change is the additive `recruiter_assessment` field.


class FieldStatus(str, Enum):
    AVAILABLE = "available"
    NOT_MENTIONED = "not_mentioned"
    NOT_DETECTED = "not_detected"
    CONFIRMED_ABSENT = "confirmed_absent"
    NOT_APPLICABLE = "not_applicable"
    UNCERTAIN = "uncertain"


class Source(str, Enum):
    EXTRACTED = "extracted"
    AI = "ai_interpretation"
    DERIVED = "derived"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Metadata(_Model):
    schema_version: str = SCHEMA_VERSION
    candidate_id: str
    resume_id: str
    evaluation_id: str | None = None
    evaluated_at: str | None = None
    model_name: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    evaluation_version: int | None = None
    evaluation_config_version: str | None = None
    yaml_version: str | None = None


class CandidateProfile(_Model):
    full_name: str | None = None
    professional_headline: str | None = None
    professional_summary: str | None = None
    career_stage: str | None = None
    total_experience_years: float | None = None
    professional_domain: str | None = None
    industry: str | None = None
    seniority: str | None = None
    location: str | None = None


class SkillEntry(_Model):
    skill_name: str
    category: str
    evidence: list[str] = Field(default_factory=list)
    source_section: str | None = None
    confidence: float | None = None

    @field_validator("confidence")
    @classmethod
    def _conf_range(cls, v):
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be within 0..1")
        return v


class SkillsIntelligence(_Model):
    technical_skills: list[SkillEntry] = Field(default_factory=list)
    programming_languages: list[SkillEntry] = Field(default_factory=list)
    frameworks: list[SkillEntry] = Field(default_factory=list)
    libraries: list[SkillEntry] = Field(default_factory=list)
    databases: list[SkillEntry] = Field(default_factory=list)
    cloud_platforms: list[SkillEntry] = Field(default_factory=list)
    devops_tools: list[SkillEntry] = Field(default_factory=list)
    ai_ml_skills: list[SkillEntry] = Field(default_factory=list)
    software_tools: list[SkillEntry] = Field(default_factory=list)
    domain_skills: list[SkillEntry] = Field(default_factory=list)
    soft_skills: list[SkillEntry] = Field(default_factory=list)
    uncategorized: list[SkillEntry] = Field(default_factory=list)


class ExperienceEntry(_Model):
    company: str | None = None
    designation: str | None = None
    duration: str | None = None
    duration_months: float | None = None
    is_current: bool | None = None
    # Populated ONLY when the source separates them (evaluation
    # evidence); never fabricated from merged highlights.
    responsibilities: list[str] | None = None
    achievements: list[str] | None = None
    highlights: list[str] = Field(default_factory=list)
    technologies: list[str] = Field(default_factory=list)
    industry: str | None = None
    supporting_evidence: list[str] = Field(default_factory=list)


class EducationEntry(_Model):
    institution: str | None = None
    degree: str | None = None
    specialization: str | None = None
    start_year: int | None = None
    end_year: int | None = None
    grade: str | None = None
    education_level: str | None = None


class ProjectEntry(_Model):
    project_name: str | None = None
    description: str | None = None
    technologies: list[str] = Field(default_factory=list)
    contributions: list[str] = Field(default_factory=list)
    outcomes: list[str] = Field(default_factory=list)
    related_skills: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class CertificationEntry(_Model):
    certification_name: str
    issuer: str | None = None
    issue_date: str | None = None
    expiry_date: str | None = None
    credential_url: str | None = None


class Observation(_Model):
    text: str
    evidence: list[str] = Field(default_factory=list)
    source: Source = Source.AI


class CandidateEvaluation(_Model):
    candidate_classification: str | None = None
    experience_classification: str | None = None
    professional_domain: str | None = None
    industry: str | None = None
    seniority: str | None = None
    strengths: list[Observation] = Field(default_factory=list)
    weaknesses: list[Observation] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)


def _score(v):
    if v is not None and not (0.0 <= float(v) <= 100.0):
        raise ValueError("score must be within 0..100")
    return v


class ResumeCompleteness(_Model):
    completeness_score: float | None = None
    classification: str | None = None
    present_sections: list[str] = Field(default_factory=list)
    missing_sections: list[str] = Field(default_factory=list)
    partial_sections: list[str] = Field(default_factory=list)
    section_level_observations: list[dict[str, Any]] = Field(
        default_factory=list)

    @field_validator("completeness_score")
    @classmethod
    def _range(cls, v):
        return _score(v)


class ResumeQuality(_Model):
    quality_score: float | None = None
    classification: str | None = None
    readability_observations: list[str] = Field(default_factory=list)
    structure_observations: list[str] = Field(default_factory=list)
    content_observations: list[str] = Field(default_factory=list)
    formatting_issues: list[str] = Field(default_factory=list)
    professional_presentation: str | None = None

    @field_validator("quality_score")
    @classmethod
    def _range(cls, v):
        return _score(v)


class GapsAndMissing(_Model):
    """Section K — kept strictly separate: what is not in the resume
    is NOT evidence the candidate lacks the skill."""
    missing_information: list[dict[str, Any]] = Field(default_factory=list)
    unverified_information: list[str] = Field(default_factory=list)
    extraction_uncertainty: list[str] = Field(default_factory=list)
    detected_skill_gaps: list[dict[str, Any]] = Field(default_factory=list)
    insufficient_evidence: list[str] = Field(default_factory=list)


class DataReliability(_Model):
    field_level_status: dict[str, FieldStatus] = Field(default_factory=dict)
    evidence_sources: dict[str, Any] = Field(default_factory=dict)
    extraction_warnings: list[str] = Field(default_factory=list)
    conflicting_information: list[str] = Field(default_factory=list)
    uncertain_fields: list[str] = Field(default_factory=list)


class AssessmentPointC(_Model):
    point: str = ""
    evidence: list[str] = Field(default_factory=list)


class VerifiedGapC(_Model):
    skill_or_area: str = ""
    basis: str = ""
    evidence: list[str] = Field(default_factory=list)


class UnmentionedAreaC(_Model):
    area: str = ""
    relevance_note: str = ""


class RecruiterAssessmentC(_Model):
    """Canonical copy of the evaluation's recruiter_assessment
    (ai-evaluation/1.1). Field names match the evaluation schema so
    the projection is a verbatim carry, never a reinterpretation."""

    summary: str = ""
    strengths: list[AssessmentPointC] = Field(default_factory=list)
    limitations: list[AssessmentPointC] = Field(default_factory=list)
    assessment_insights: list[AssessmentPointC] = Field(default_factory=list)
    verified_skill_gaps: list[VerifiedGapC] = Field(default_factory=list)
    unmentioned_areas: list[UnmentionedAreaC] = Field(default_factory=list)
    verification_points: list[str] = Field(default_factory=list)
    information_completeness_note: str = ""
    data_reliability_note: str = ""


class CanonicalEvaluation(_Model):
    """The complete canonical document (schema ai-evaluation/1.1)."""
    metadata: Metadata
    candidate_profile: CandidateProfile
    skills: SkillsIntelligence
    experience: list[ExperienceEntry] = Field(default_factory=list)
    education: list[EducationEntry] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
    certifications: list[CertificationEntry] = Field(default_factory=list)
    candidate_evaluation: CandidateEvaluation
    resume_completeness: ResumeCompleteness
    resume_quality: ResumeQuality
    gaps: GapsAndMissing
    data_reliability: DataReliability
    # Role suitability is preserved verbatim from the evaluation
    # (existing functionality, unchanged): list of role dicts.
    # Recruiter assessment, carried verbatim from the evaluation.
    # None when the source evaluation predates eval-prompt/2.0
    # (historical evaluations remain valid 1.0-shaped content under
    # the 1.1 envelope).
    recruiter_assessment: RecruiterAssessmentC | None = None
    role_suitability: list[dict[str, Any]] = Field(default_factory=list)
    suitable_roles: list[str] = Field(default_factory=list)
    # Candidate-improvement recommendations preserved (B2C capability),
    # kept apart from the recruiter-facing evaluation sections.
    recommendations: list[str] = Field(default_factory=list)


def validate_canonical(data: Any) -> CanonicalEvaluation:
    """Strict validation entry point (raises pydantic ValidationError)."""
    return CanonicalEvaluation.model_validate(data)
