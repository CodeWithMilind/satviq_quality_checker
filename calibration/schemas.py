"""Ground-truth annotation schema for calibration.

An annotation is a HUMAN-VERIFIED description of one resume: which
standard sections are actually present, which are actually missing,
the human completeness/quality scores, and (optionally) which roles
the candidate is genuinely suitable for. AI output must never be
saved as an annotation (spec Part L).

Section names use the evaluation engine's canonical vocabulary so an
annotation compares directly against ``evaluation.schema.Evaluation``
output without a translation layer.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# Canonical sections the evaluation engine reports on (spec Part 3.1).
CANONICAL_SECTIONS: tuple[str, ...] = (
    "Contact Information",
    "Professional Summary",
    "Career Objective",
    "Technical Skills",
    "Core Competencies",
    "Work Experience",
    "Internship Experience",
    "Projects",
    "Academic Projects",
    "Education",
    "Certifications",
    "Achievements",
    "Responsibilities",
    "Publications",
    "Research Experience",
)

# Optional sections (spec Part 3.3) — annotatable, never penalised.
GOOD_TO_HAVE_SECTIONS: tuple[str, ...] = (
    "Languages",
    "Interests & Hobbies",
    "Extracurricular Activities",
    "Volunteer Experience",
    "Portfolio Links",
    "Social Profiles",
    "Open Source Contributions",
    "Hackathons",
    "Workshops & Training",
    "References",
)

ALL_SECTIONS: tuple[str, ...] = CANONICAL_SECTIONS + GOOD_TO_HAVE_SECTIONS

# Sections whose misdetection hurts a recruiter most; used only to
# grade error severity in the metrics engine.
CORE_SECTIONS: frozenset[str] = frozenset({
    "Contact Information", "Education", "Technical Skills",
    "Work Experience",
})

ANNOTATION_STATUSES: tuple[str, ...] = (
    "pending", "in_progress", "completed", "needs_review",
)

SPLIT_NAMES: tuple[str, ...] = ("development", "validation", "holdout")

_FALLBACK_CAREER_STAGES: tuple[str, ...] = (
    "Student", "Fresher", "Entry-Level", "Experienced", "Senior",
)


def _key(name: str) -> str:
    return " ".join(str(name).split()).strip().lower()


_CANONICAL_BY_KEY: dict[str, str] = {_key(s): s for s in ALL_SECTIONS}


def canonical_section(name: Any) -> str | None:
    """Return the canonical display name for a section, or None when
    the name is not in the annotation vocabulary."""
    if not isinstance(name, str):
        return None
    return _CANONICAL_BY_KEY.get(_key(name))


def known_career_stages() -> list[str] | None:
    """Career-stage names from the live evaluation config, or None
    when the config cannot be loaded (validation then accepts any
    non-empty stage rather than inventing a list)."""
    try:
        from evaluation import config as eval_config

        config = eval_config.load_config()
        stages = config.get("career_stages") or []
        names = [s.get("name", "") for s in stages if isinstance(s, dict)]
        names = [n for n in names if n]
        return names or None
    except Exception:
        return None


def _normalise_sections(values: list[str], field: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        canonical = canonical_section(value)
        if canonical is None:
            raise ValueError(
                f"{field}: unknown section {value!r} — use the "
                "canonical section vocabulary")
        if canonical not in seen:
            seen.add(canonical)
            out.append(canonical)
    return out


class GroundTruthAnnotation(BaseModel):
    """One human-verified ground-truth annotation (spec Part D)."""

    expected_present_sections: list[str] = Field(default_factory=list)
    expected_missing_sections: list[str] = Field(default_factory=list)
    expected_completeness_score: float | None = None
    expected_quality_score: float | None = None
    # None = roles not labelled for this resume (role metrics skip
    # it); [] = labelled, and no role is genuinely suitable.
    expected_suitable_roles: list[str] | None = None
    career_stage: str = ""
    professional_domain: str = ""
    notes: str = ""

    @field_validator("expected_present_sections", mode="before")
    @classmethod
    def _present(cls, value):
        return _normalise_sections(list(value or []), "present sections")

    @field_validator("expected_missing_sections", mode="before")
    @classmethod
    def _missing(cls, value):
        return _normalise_sections(list(value or []), "missing sections")

    @field_validator(
        "expected_completeness_score", "expected_quality_score",
        mode="before")
    @classmethod
    def _score(cls, value):
        if value is None or value == "":
            return None
        number = float(value)
        if not 0.0 <= number <= 100.0:
            raise ValueError("expected scores must be within 0-100")
        return number

    @field_validator("expected_suitable_roles", mode="before")
    @classmethod
    def _roles(cls, value):
        if value is None:
            return None
        return [str(role).strip() for role in value if str(role).strip()]

    @field_validator("career_stage", mode="before")
    @classmethod
    def _stage(cls, value):
        stage = str(value or "").strip()
        if not stage:
            raise ValueError("career_stage is required for stratification")
        known = known_career_stages()
        if known is not None and stage not in known:
            raise ValueError(
                f"unknown career_stage {stage!r} — configured stages: "
                + ", ".join(known))
        return stage

    @field_validator("professional_domain", mode="before")
    @classmethod
    def _domain(cls, value):
        domain = str(value or "").strip()
        if not domain:
            raise ValueError(
                "professional_domain is required for stratification")
        return domain

    @model_validator(mode="after")
    def _no_overlap(self):
        overlap = (set(self.expected_present_sections)
                   & set(self.expected_missing_sections))
        if overlap:
            raise ValueError(
                "sections cannot be both present and missing: "
                + ", ".join(sorted(overlap)))
        return self


def validate_annotation(raw: dict[str, Any]) -> tuple[dict[str, Any] | None,
                                                      str | None]:
    """Validate one raw annotation dict. Returns (clean_dict, None)
    or (None, error_message) — importers collect errors per row
    instead of failing the whole file."""
    try:
        model = GroundTruthAnnotation(**(raw or {}))
    except Exception as exc:  # pydantic ValidationError and friends
        return None, str(exc)
    return model.model_dump(), None
