"""Completeness filter.

Detects whether expected resume sections appear to be present, using
configurable section names / aliases and optional regex patterns.

Important semantics (from the spec):
- A missing section is a *possible omission*, not proof that extraction
  failed. Reasons are worded that way.
- Not every resume must contain every section — importance levels
  (required / recommended / optional) control how much a missing
  section costs, and optional sections cost very little.
- No specific keyword is required in every resume.

Deterministic logic only, no LLM. Analysis uses a normalised *copy*;
the original text is never mutated.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from ..models import CompletenessFilterResult
from ._config import invalid_input_result, load_config

logger = logging.getLogger(__name__)

_HEADING_DECORATION = re.compile(r"^[\s#>*\-=_•·]+|[\s:|\-=_•·]+$")
_NUMBERING = re.compile(r"^\s*(?:\d{1,3}\s*[.)-]?\s+|\d{1,3}\s*[.)]\s*)")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _normalise_heading(line: str) -> str:
    """Normalise one line for heading comparison (on a copy)."""
    cleaned = _NUMBERING.sub("", line.strip().lower())
    cleaned = cleaned.replace("&", " and ")
    cleaned = _HEADING_DECORATION.sub("", cleaned)
    return _NON_WORD.sub(" ", cleaned).strip()


class CompletenessFilter:
    """Detect expected resume sections in extracted text."""

    name = "completeness"

    def __init__(self, config: dict[str, Any] | None = None,
                 config_path: str | Path | None = None) -> None:
        self.config = load_config("completeness.yaml", config=config, config_path=config_path)
        self.sections: dict[str, Any] = self.config.get("sections", {})
        self.penalties: dict[str, float] = self.config.get(
            "penalties_by_importance", {"required": 25, "recommended": 12, "optional": 3}
        )
        # Pre-compile patterns once; a bad regex in config is an error
        # the operator should see at construction time, not per-document.
        self._patterns: dict[str, list[re.Pattern[str]]] = {}
        for key, spec in self.sections.items():
            compiled = []
            for pattern in spec.get("patterns", []) or []:
                try:
                    compiled.append(re.compile(pattern))
                except re.error:
                    logger.exception("Invalid regex for section '%s': %s", key, pattern)
                    raise
            self._patterns[key] = compiled

    # -- public API -----------------------------------------------------
    def evaluate(self, text: object, *, extraction_quality: float | None = None) -> CompletenessFilterResult:
        if not isinstance(text, str):
            logger.warning("CompletenessFilter received non-string input: %s", type(text).__name__)
            return CompletenessFilterResult(
                score=0.0, flags=["INVALID_INPUT"],
                reasons=[f"Expected extracted text as str, got {type(text).__name__}."],
                metrics=invalid_input_result(self.name, text),
                detected_sections=[], missing_sections=self._all_display_names(),
                missing_required=self._display_names_by_importance("required"),
                missing_recommended=self._display_names_by_importance("recommended"),
                missing_optional=self._display_names_by_importance("optional"),
            )

        if not text.strip():
            extraction_unknown = (
                extraction_quality is not None
                and extraction_quality <= float(self.config.get("uncertainty_text_health_score", 40))
            )
            missing = [] if extraction_unknown else self._all_display_names()
            missing_required = [] if extraction_unknown else self._display_names_by_importance("required")
            missing_recommended = [] if extraction_unknown else self._display_names_by_importance("recommended")
            missing_optional = [] if extraction_unknown else self._display_names_by_importance("optional")
            uncertain = self._all_display_names() if extraction_unknown else []
            flags = ["EMPTY_TEXT", "NO_SECTIONS_DETECTED"]
            reasons = ["Extracted text is empty, so no resume sections can be detected."]
            if extraction_unknown:
                flags.append("SECTION_PRESENCE_UNCERTAIN")
                reasons.append("Text extraction quality is too low to classify undetected sections as missing.")
            return CompletenessFilterResult(
                score=0.0, flags=flags, reasons=reasons,
                metrics={"char_count": len(text), "word_count": 0,
                         "detected_count": 0, "missing_count": len(missing),
                         "configured_section_count": len(self.sections),
                         "detected_sections": [], "missing_sections": missing,
                         "missing_required": missing_required,
                         "missing_recommended": missing_recommended,
                         "missing_optional": missing_optional,
                         "uncertain_sections": uncertain,
                         "section_status": ({name: "missing" for name in missing}
                                            | {name: "uncertain" for name in uncertain}),
                         "section_evidence": {}, "matched_aliases": {},
                         "extraction_quality": extraction_quality},
                detected_sections=[], missing_sections=missing,
                missing_required=missing_required,
                missing_recommended=missing_recommended,
                missing_optional=missing_optional,
            )

        # Normalised copy for analysis — the original is left untouched.
        raw_lines = [
            (line_number, line.strip())
            for line_number, line in enumerate(text.splitlines(), start=1)
            if line.strip()
        ]
        candidates: list[tuple[str, str, int, str]] = []
        for index, (line_number, raw) in enumerate(raw_lines):
            normalised = _normalise_heading(raw)
            candidates.append((normalised, raw, line_number, "heading"))
            # PDF extraction may split a short heading over adjacent lines.
            if index + 1 < len(raw_lines) and len(raw) < 45 and len(raw_lines[index + 1][1]) < 45:
                next_raw = raw_lines[index + 1][1]
                joined = _normalise_heading(f"{raw} {next_raw}")
                candidates.append((joined, f"{raw} {next_raw}", line_number, "joined_lines"))

        detected: list[str] = []
        missing: list[str] = []
        missing_by_importance: dict[str, list[str]] = {"required": [], "recommended": [], "optional": []}
        matched_aliases: dict[str, str] = {}
        section_evidence: dict[str, dict[str, Any]] = {}
        uncertain: list[str] = []
        threshold = float(self.config.get("uncertainty_text_health_score", 40))
        poor_extraction = extraction_quality is not None and extraction_quality <= threshold

        for key, spec in self.sections.items():
            display = spec.get("display_name", key)
            importance = str(spec.get("importance", "optional")).lower()
            hit: str | None = None

            for alias in spec.get("aliases", []) or []:
                alias_norm = _normalise_heading(str(alias))
                for line, raw, line_number, method in candidates:
                    # Exact heading or explicit heading delimiter + content. Do not treat
                    # ordinary prose such as “Experience in Python” as a section title.
                    raw_heading = _NUMBERING.sub("", raw.lower())
                    if line == alias_norm or re.match(
                        rf"^{re.escape(alias_norm)}\s*[:|]\s*\S", raw_heading
                    ):
                        hit = str(alias)
                        section_evidence[display] = {
                            "canonical_name": display, "original_heading": raw,
                            "detection_method": method, "line_number": line_number,
                            "evidence": raw[:160], "confidence": 0.98 if method == "heading" else 0.9,
                        }
                        break
                if hit is not None:
                    break
            if hit is None:
                for pattern in self._patterns.get(key, []):
                    if pattern.search(text):
                        hit = f"pattern:{pattern.pattern[:40]}"
                        match = pattern.search(text)
                        section_evidence[display] = {
                            "canonical_name": display, "original_heading": None,
                            "detection_method": "pattern", "line_number": None,
                            "evidence": match.group(0)[:160] if match else None,
                            "confidence": 0.8,
                        }
                        break

            if hit is not None:
                detected.append(display)
                matched_aliases[display] = hit
            else:
                if poor_extraction:
                    uncertain.append(display)
                else:
                    missing.append(display)
                    if importance in missing_by_importance:
                        missing_by_importance[importance].append(display)
                    else:
                        missing_by_importance["optional"].append(display)

        score = 100.0
        flags: list[str] = []
        reasons: list[str] = []
        for importance, names in missing_by_importance.items():
            if not names:
                continue
            score -= self.penalties.get(importance, 0) * len(names)
            flags.append(f"MISSING_{importance.upper()}_SECTION")
            for name in names:
                reasons.append(
                    f"{importance.capitalize()} section not detected: {name}. "
                    "This may be a genuine omission in the resume or an extraction gap — "
                    "it is not by itself proof that extraction failed."
                )
        if not detected:
            flags.append("NO_SECTIONS_DETECTED")
            reasons.append("No configured resume sections were detected in the extracted text.")
        if uncertain:
            flags.append("SECTION_PRESENCE_UNCERTAIN")
            reasons.append("Text extraction quality is too low to classify undetected sections as missing.")

        metrics: dict[str, Any] = {
            "char_count": len(text),
            "word_count": len(text.split()),
            "configured_section_count": len(self.sections),
            "detected_count": len(detected),
            "missing_count": len(missing),
            "detected_sections": detected,
            "missing_sections": missing,
            "missing_required": missing_by_importance["required"],
            "missing_recommended": missing_by_importance["recommended"],
            "missing_optional": missing_by_importance["optional"],
            "matched_aliases": matched_aliases,
            "section_evidence": section_evidence,
            "uncertain_sections": uncertain,
            "section_status": (
                {name: "detected" for name in detected}
                | {name: "missing" for name in missing}
                | {name: "uncertain" for name in uncertain}
            ),
            "extraction_quality": extraction_quality,
        }
        logger.debug("CompletenessFilter score=%.2f detected=%s missing=%s",
                     max(0.0, score), detected, missing)
        return CompletenessFilterResult(
            score=max(0.0, score), flags=flags, reasons=reasons, metrics=metrics,
            detected_sections=detected, missing_sections=missing,
            missing_required=missing_by_importance["required"],
            missing_recommended=missing_by_importance["recommended"],
            missing_optional=missing_by_importance["optional"],
        )

    # -- helpers --------------------------------------------------------
    def _all_display_names(self) -> list[str]:
        return [spec.get("display_name", key) for key, spec in self.sections.items()]

    def _display_names_by_importance(self, importance: str) -> list[str]:
        return [spec.get("display_name", key) for key, spec in self.sections.items()
                if str(spec.get("importance", "optional")).lower() == importance]


def evaluate_completeness(text: object, **kwargs: Any) -> CompletenessFilterResult:
    """Convenience function so the filter is usable without a class instance."""
    return CompletenessFilter(**kwargs).evaluate(text)
