"""Extraction quality evaluation for the simple pipeline.

Deterministic, rule-based scoring only -- no LLM, no randomness.
Every successful extractor result is scored with the same criteria
and configurable (sum-normalised) weights. Length uses a saturating
function so the longest text does not automatically win, and
duplicated / corrupted text is penalised.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .config import DEFAULT_WEIGHTS

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
_SECTION_KEYWORDS: dict[str, list[str]] = {
    "skills": ["skills", "technical skills", "core competencies", "technologies"],
    "experience": ["experience", "work experience", "employment", "work history", "professional experience"],
    "education": ["education", "academic", "qualifications", "university", "degree"],
    "summary": ["summary", "profile", "objective", "about me", "professional summary"],
    "projects": ["projects", "project experience"],
    "certifications": ["certifications", "certificates", "certified"],
}

_HEADING_CANDIDATES = {
    "skills", "technical skills", "work experience", "experience",
    "education", "professional summary", "summary", "projects",
    "certifications", "contact", "contact information", "languages",
    "achievements", "profile", "objective",
}


def _meaningful_char_count(text: str) -> int:
    return sum(1 for c in text if not c.isspace() and c != "\ufffd")


def _control_char_count(text: str) -> int:
    return sum(1 for c in text if (ord(c) < 32 and c not in "\n\r\t") or ord(c) == 127)


def _readability_component(text: str, words: list[str]) -> float:
    if not words:
        return 0.0
    good = 0
    for w in words:
        token = w.strip(".,;:!?()[]{}\"'|/\\-•*")
        if not token:
            continue
        if "\ufffd" in token:
            continue
        # alphabetic-ish token of sane length
        alpha = sum(1 for c in token if c.isalpha())
        if alpha >= max(1, len(token) * 0.7) and 2 <= len(token) <= 20:
            good += 1
        elif token.isdigit():
            good += 1
    ratio = good / len(words) if words else 0.0
    # sentence punctuation bonus / penalty: some punctuation is healthy
    # avg word length sanity
    avg_len = sum(len(w) for w in words) / len(words) if words else 0
    if avg_len == 0:
        length_factor = 0.0
    elif 3 <= avg_len <= 8:
        length_factor = 1.0
    elif 2 <= avg_len < 3 or 8 < avg_len <= 12:
        length_factor = 0.7
    else:
        length_factor = 0.4
    return max(0.0, min(1.0, ratio * 0.8 + length_factor * 0.2))


def evaluate_extraction_quality(result: dict[str, Any], weights: dict[str, float] | None = None) -> dict[str, Any]:
    """Evaluate one unified extractor result dict.

    ``result`` is the dict produced by pipeline.extractors.run_extractors
    (keys: extractor, status, extracted_text, error, page_count, page_texts).
    Also tolerates ExtractionResult-like dicts with text/success keys.
    """
    extractor = result.get("extractor") or result.get("extractor_name") or "unknown"
    status_raw = result.get("status")
    if status_raw is None:
        status_raw = "success" if result.get("success") else "failed"
    text = result.get("extracted_text")
    if text is None:
        text = result.get("text") or ""
    if not isinstance(text, str):
        text = str(text)
    error = result.get("error")

    char_count = len(text)
    words = text.split() if text.strip() else []
    word_count = len(words)
    meaningful = _meaningful_char_count(text)

    page_texts = result.get("page_texts") or []
    page_count = result.get("page_count")
    if page_texts:
        total_pages = len(page_texts)
        empty_pages = sum(1 for p in page_texts if not (p or "").strip())
        empty_page_ratio = empty_pages / total_pages if total_pages else 0.0
    elif page_count:
        empty_page_ratio = 1.0 if not text.strip() else 0.0
    else:
        empty_page_ratio = 0.0 if text.strip() else (1.0 if status_raw == "success" else 1.0)

    replacement_count = text.count("\ufffd")
    replacement_ratio = replacement_count / char_count if char_count else 0.0
    control_count = _control_char_count(text)
    control_ratio = control_count / char_count if char_count else 0.0

    # Duplication: repeated non-empty lines
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if lines:
        counts = Counter(lines)
        duplicated_lines = sum(c - 1 for c in counts.values() if c > 1)
        duplication_ratio = duplicated_lines / len(lines)
        # also repeated word n-grams (bigrams) as a secondary signal
        # keep line-based as primary for determinism/simplicity
    else:
        duplication_ratio = 0.0

    readability = _readability_component(text, words)
    readability_score = round(readability * 100, 1)

    # Completeness
    lower = text.lower()
    has_email = bool(_EMAIL_RE.search(text))
    has_phone = bool(_PHONE_RE.search(text))
    sections_found: list[str] = []
    for section, keywords in _SECTION_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            sections_found.append(section)
    has_skills = "skills" in sections_found
    has_experience = "experience" in sections_found
    has_education = "education" in sections_found
    completeness_detail = {
        "has_email": has_email,
        "has_phone": has_phone,
        "has_skills_section": has_skills,
        "has_experience_section": has_experience,
        "has_education_section": has_education,
        "sections_found": sections_found,
    }
    completeness_indicators = [has_email, has_phone, has_skills, has_experience, has_education]
    completeness_component = sum(1 for v in completeness_indicators if v) / len(completeness_indicators) if completeness_indicators else 0.0

    # Structure
    raw_lines = text.splitlines()
    heading_count = 0
    bullet_count = 0
    for ln in raw_lines:
        stripped = ln.strip()
        if not stripped:
            continue
        if stripped.startswith(("-", "•", "*", "·", "–")) or re.match(r"^\d+[.)]\s", stripped):
            bullet_count += 1
        low = stripped.lower().rstrip(":")
        if low in _HEADING_CANDIDATES:
            heading_count += 1
        elif len(stripped) <= 60 and stripped == stripped.title() and len(stripped.split()) <= 5 and not stripped.endswith("."):
            # title-case short line could be a heading -- only count
            # if it contains a heading-like word to avoid overcounting names etc.
            # Be conservative: count only if at least one alpha word and no email/@
            if "@" not in stripped and any(w.lower() in _HEADING_CANDIDATES for w in stripped.lower().split()):
                heading_count += 1
    paragraphs = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    paragraph_count = len(paragraphs) if paragraphs else (1 if text.strip() else 0)
    # If no blank-line paragraphs, estimate from lines: groups
    if paragraph_count == 1 and len(lines) > 3:
        paragraph_count = min(len(lines), 5)  # line-structured resumes still have structure

    structure_component = 0.0
    if heading_count > 0:
        structure_component += 0.4
    elif paragraph_count >= 2:
        structure_component += 0.2
    structure_component += min(paragraph_count / 3, 1.0) * 0.3
    structure_component += min(bullet_count / 3, 1.0) * 0.3
    structure_component = max(0.0, min(1.0, structure_component))

    is_success = status_raw == "success"
    is_empty = meaningful == 0 or word_count == 0

    if not is_success or is_empty:
        return {
            "extractor": extractor,
            "quality_score": 0.0,
            "status": "failed",
            "word_count": word_count,
            "char_count": char_count,
            "meaningful_char_count": meaningful,
            "empty_page_ratio": round(float(empty_page_ratio), 4),
            "replacement_char_ratio": round(float(replacement_ratio), 4),
            "duplication_ratio": round(float(duplication_ratio), 4),
            "readability_score": 0.0 if is_empty else readability_score,
            "completeness": completeness_detail,
            "structure": {"heading_count": heading_count, "paragraph_count": paragraph_count, "bullet_count": bullet_count},
            "error": error,
        }

    # Components (0-1)
    availability_c = 1.0
    # Length adequacy uses UNIQUE meaningful content so duplicated
    # text cannot inflate the score by repeating itself.
    if lines:
        _unique_text = "\n".join(dict.fromkeys(lines))
        _effective_meaningful = _meaningful_char_count(_unique_text)
    else:
        _effective_meaningful = meaningful
    length_c = min(_effective_meaningful / 1500.0, 1.0) if _effective_meaningful else 0.0
    cleanliness_c = max(0.0, 1.0 - min(replacement_ratio * 20, 1.0) * 0.7 - min(control_ratio * 20, 1.0) * 0.3 - replacement_ratio * 5)
    # Simpler bounded cleanliness: heavy penalty for replacement chars
    cleanliness_c = max(0.0, 1.0 - min(replacement_ratio * 10, 1.0) - min(control_ratio * 10, 0.5))
    if replacement_ratio > 0.05:
        cleanliness_c = min(cleanliness_c, 0.2)
    non_dup_c = max(0.0, 1.0 - duplication_ratio)
    # Empty pages penalise via availability/length indirectly; fold into structure? keep separate small penalty
    # Apply empty page penalty to availability component
    if empty_page_ratio > 0:
        availability_c = max(0.0, availability_c - empty_page_ratio * 0.3)

    components = {
        "availability": availability_c,
        "length_adequacy": length_c,
        "readability": readability,
        "cleanliness": cleanliness_c,
        "non_duplication": non_dup_c,
        "completeness": completeness_component,
        "structure": structure_component,
    }

    w = dict(DEFAULT_WEIGHTS)
    if weights:
        for k, v in weights.items():
            try:
                w[str(k)] = float(v)
            except Exception:
                continue
    total_w = sum(w.get(k, 0.0) for k in components) or 1.0
    score = sum(components[k] * w.get(k, 0.0) for k in components) / total_w * 100.0
    score = round(max(0.0, min(100.0, score)), 1)

    return {
        "extractor": extractor,
        "quality_score": score,
        "status": "success",
        "word_count": word_count,
        "char_count": char_count,
        "meaningful_char_count": meaningful,
        "empty_page_ratio": round(float(empty_page_ratio), 4),
        "replacement_char_ratio": round(float(replacement_ratio), 4),
        "duplication_ratio": round(float(duplication_ratio), 4),
        "readability_score": readability_score,
        "completeness": completeness_detail,
        "structure": {"heading_count": heading_count, "paragraph_count": paragraph_count, "bullet_count": bullet_count},
        "error": error,
        "components": {k: round(v, 4) for k, v in components.items()},
    }
