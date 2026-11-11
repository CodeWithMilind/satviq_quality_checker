"""The project's structured Resume JSON schema and its validation.

Both Muse paths — text → JSON generation and the direct image/PDF fallback —
must return JSON matching this schema. Missing information uses the
schema's empty representations (``""``, ``[]``, ``None``): models are
instructed never to invent details, and validation never fills any in.

``skills`` is a categorized :class:`Skills` object (Muse returns it
that way); the flat ``list[str]`` form used before categorization is
still accepted and converted, so older outputs keep validating.
"""

from __future__ import annotations

import json
import re

from pydantic import BaseModel, Field, field_validator, model_validator


class Skills(BaseModel):
    """Categorized skills, as returned by Muse AI.

    Every category defaults to an empty list, so a response naming
    only some categories still validates. Two model variances are
    normalized without losing information: a category given as a
    single string becomes a one-item list, and any unrecognized
    category's items are preserved under ``other_skills`` instead
    of being dropped.
    """

    technical_skills: list[str] = Field(default_factory=list)
    soft_skills: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    taxonomy_skills: list[str] = Field(default_factory=list)
    other_skills: list[str] = Field(default_factory=list)

    @field_validator("technical_skills", "soft_skills", "tools",
                     "taxonomy_skills", "other_skills", mode="before")
    @classmethod
    def _category_as_list(cls, value):
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        return value

    @model_validator(mode="before")
    @classmethod
    def _preserve_unknown_categories(cls, value):
        if isinstance(value, dict):
            known = {"technical_skills", "soft_skills", "tools",
                     "taxonomy_skills", "other_skills"}
            extra: list[str] = []
            for key, item in value.items():
                if key not in known and isinstance(item, list):
                    extra.extend(str(entry) for entry in item)
            if extra:
                value = dict(value)
                existing = value.get("other_skills") or []
                if isinstance(existing, str):
                    existing = [existing]
                value["other_skills"] = list(existing) + extra
        return value


class ExperienceItem(BaseModel):
    title: str = ""
    company: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""
    highlights: list[str] = Field(default_factory=list)


class EducationItem(BaseModel):
    degree: str = ""
    institution: str = ""
    location: str = ""
    start_date: str = ""
    end_date: str = ""


class ResumeJSON(BaseModel):
    @field_validator("skills", mode="before")
    @classmethod
    def _skills_backward_compatible(cls, value):
        """Accept the pre-categorization flat list form.

        Older model responses (and every stored record produced
        before this change) carry ``skills`` as a simple list. The
        list is converted into the categorized structure with every
        item preserved under ``technical_skills`` — the primary
        category — so no skill information is lost. ``None`` and a
        bare string are handled the same way.
        """
        if value is None:
            return {}
        if isinstance(value, list):
            return {"technical_skills": list(value)}
        if isinstance(value, str):
            return {"technical_skills": [value]} if value.strip() else {}
        return value

    full_name: str = ""
    email: str = ""
    phone: str = ""
    location: str = ""

    @field_validator("phone", mode="before")
    @classmethod
    def _phone_normalized(cls, value):
        """Standardize the phone field to ``+91 XXXXXXXXXX``.

        Deterministic normalization (api.phone_numbers) applied to
        the model's extracted value BEFORE it becomes the stored
        Resume JSON: valid Indian mobiles are canonicalized, invalid
        or ambiguous values pass through unchanged, and the original
        extracted TEXT is never touched — only this structured
        field. The import is local so this module stays importable
        in isolation (the standalone schema tests load it without
        the api package)."""
        if value is None:
            return ""
        try:
            from api.phone_numbers import normalize_phone_field
            return normalize_phone_field(value)
        except Exception:
            return value
    summary: str = ""
    skills: Skills = Field(default_factory=Skills)
    experience: list[ExperienceItem] = Field(default_factory=list)
    education: list[EducationItem] = Field(default_factory=list)
    certifications: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    links: list[str] = Field(default_factory=list)


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def validate_resume_json(raw_text: str) -> tuple[dict | None, str | None]:
    """Parse + validate model output against :class:`ResumeJSON`.

    Returns ``(resume_dict, None)`` on success or ``(None, error)``.
    Tolerates Markdown code fences and surrounding prose by first
    trying the whole text, then the widest ``{...}`` span.
    """
    if not raw_text or not raw_text.strip():
        return None, "Model returned no text."
    candidates = [raw_text.strip(), _FENCE_RE.sub("", raw_text).strip()]
    start, end = raw_text.find("{"), raw_text.rfind("}")
    if start != -1 and end > start:
        candidates.append(raw_text[start:end + 1])
    last_error: str | None = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(parsed, dict):
            last_error = "Model JSON was not an object."
            continue
        try:
            return ResumeJSON(**parsed).model_dump(), None
        except Exception as exc:  # pydantic ValidationError
            last_error = f"Schema validation failed: {exc}"
    return None, last_error or "Model output was not valid JSON."
