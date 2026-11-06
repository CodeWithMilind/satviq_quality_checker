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
_URL_RE = re.compile(r"(?:https?://|www\.)\S+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(r"\+?\d[\d\s().-]{6,}\d")
# A "link item" line — a label/handle/URL pointing elsewhere, never
# section content: "Website: x.com", "Instagram: @handle", a bare URL.
_LINK_ITEM_RE = re.compile(
    r"(?:https?://|www\.|mailto:|@[\w.]+|\b[\w-]+\.(?:com|in|io|dev|site|net|org|co)\b)",
    re.IGNORECASE)
# Evidence that a content window holds work records, not prose: a
# bullet, a year, or a "Title: ..." entry line.
_WORK_MARKER_RE = re.compile(r"(?:[\u2022\u25cf\u25aa]|\b(?:19|20)\d{2}\b|^\s*[-*]\s|\S+\s*[:|]\s*\S)")
_LINK_LABEL_RE = re.compile(
    r"^(?:portfolio|website|instagram|linkedin|github|twitter|facebook|"
    r"behance|dribbble|links?)\s*[:|]", re.IGNORECASE)
_NUMBERING = re.compile(r"^\s*(?:\d{1,3}\s*[.)-]?\s+|\d{1,3}\s*[.)]\s*)")
# Numbering glued to the heading word, e.g. "01EXPERIENCE" / "02EDUCATION"
# (seen in real resumes whose PDF text layer drops the space).
_NUMBERING_GLUED = re.compile(r"^\d{1,3}(?=[A-Za-z])")
_NON_WORD = re.compile(r"[^a-z0-9]+")


def _normalise_heading(line: str) -> str:
    """Normalise one line for heading comparison (on a copy)."""
    cleaned = _NUMBERING_GLUED.sub("", _NUMBERING.sub("", line.strip().lower()))
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
        # Conditional (content-gated) aliases and the gate thresholds.
        gate_cfg = self.config.get("conditional_content", {}) or {}
        self._gate_min_words = int(gate_cfg.get("min_words", 8))
        self._gate_max_lines = int(gate_cfg.get("max_window_lines", 15))
        # Every recognised heading form, used to find where a
        # conditional section's content window ends.
        self._known_headings: set[str] = set()
        for spec in self.sections.values():
            for group in ("aliases", "conditional_aliases"):
                for alias in spec.get(group, []) or []:
                    self._known_headings.add(_normalise_heading(str(alias)))
        # Observed-but-unscored sections (languages, achievements, ...)
        # are not detected, but their headings still END a conditional
        # content window — otherwise their prose could be mistaken for
        # the conditional section's own content.
        for spec in (self.config.get("observed_unscored_sections", {}) or {}).values():
            for alias in (spec or {}).get("aliases", []) or []:
                self._known_headings.add(_normalise_heading(str(alias)))

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

        conditional_rejected: list[dict[str, Any]] = []

        def find_heading(alias_norm: str):
            """First candidate matching an alias: the whole normalised
            line equals the alias, or the raw line is the alias plus an
            explicit ':' / '|' delimiter and inline content. Ordinary
            prose such as "Experience in Python" never matches. The raw
            line gets the same '&' -> 'and' normalisation as aliases,
            so "Projects & Achievements: ..." matches its alias just
            like the "and" spelling does."""
            alias_squashed = alias_norm.replace(" ", "")
            for line, raw, line_number, method in candidates:
                # Exact match, or space-insensitive match: real PDF
                # text layers produce glued headings
                # ("ACADEMICACHIEVEMENTS", "CAREEROBJECTIVE") and
                # letter-spaced headings ("E D U C A T I O N") whose
                # letters are exactly the alias's letters.
                if line == alias_norm or line.replace(" ", "") == alias_squashed:
                    return raw, line_number, method
                raw_heading = re.sub(
                    r"\s+", " ",
                    _NUMBERING.sub("", raw.lower()).replace("&", " and ")).strip()
                # Bullet glyphs from PDF symbol fonts (private-use
                # characters) can precede the heading on the raw line;
                # they are not part of the heading.
                raw_heading = re.sub(r"^[^a-z0-9]+", "", raw_heading)
                if re.match(rf"^{re.escape(alias_norm)}\s*[:|]\s*\S", raw_heading):
                    return raw, line_number, method
            return None

        _CONNECTORS = {"at", "with", "in", "of", "for"}
        _DURATION_WORDS = {"years", "year", "yrs", "yr", "months", "month", "mos", "mo"}

        def find_embedded(alias_norm: str):
            """Heading embedded in a heading-like line, in the strict
            shapes real resumes use (all observed in the CV dataset):

            A. "<x> <alias> at <Employer>" — e.g. "OJT /Work Experience
               at TATA MOTORS LTD, Pantnagar :": at most 2 words before
               the alias, the suffix starts with a connector, is at
               most 7 words, and names a capitalised employer/place.
            B. "<alias> and <words>" — combined headings such as
               "Professional Experience & Training" (suffix <= 4 words).
            C. "<alias> <duration>" — "Working Experience (2.5+ Years)"
               (suffix is only digits and duration words).
            D. a long digit run glued to the heading by extraction
               ("2289936201423Work Experience") and nothing else.

            Ordinary prose never satisfies these shapes: it has long
            prefixes, long suffixes, or no connector. Single-word
            aliases are excluded (too prose-prone)."""
            if len(alias_norm.split()) < 2:
                return None
            phrase = re.compile(rf"(?<!\w){re.escape(alias_norm)}(?!\w)")
            for line, raw, line_number, method in candidates:
                if method != "heading":
                    continue
                probe = re.sub(r"^\d{4,}(?=[a-z])", "", line)
                match = phrase.search(probe)
                if match is None:
                    continue
                prefix = probe[:match.start()].split()
                suffix = probe[match.end():].split()
                if len(prefix) > 2 or len(probe.split()) > 12:
                    continue
                ok = False
                if suffix and suffix[0] in _CONNECTORS and len(suffix) <= 7:
                    # The employer/place after the connector is
                    # capitalised in the raw line ("TATA MOTORS",
                    # "Tata Motors"); prose objects are not.
                    tail = raw[re.search(rf"\b{suffix[0]}\b", raw, re.I).end():] \
                        if re.search(rf"\b{suffix[0]}\b", raw, re.I) else ""
                    ok = bool(re.search(r"[A-Z]{2,}|[A-Z][a-z]", tail))
                elif not prefix and suffix and suffix[0] == "and" and len(suffix) <= 4:
                    ok = True
                elif (not prefix and suffix and len(suffix) <= 6
                        and any(w.isdigit() for w in suffix)
                        and all(w.isdigit() or w in _DURATION_WORDS for w in suffix)):
                    ok = True
                elif probe != line and not prefix and not suffix:
                    ok = True
                if ok:
                    return raw, line_number, "heading_embedded"
            return None

        for key, spec in self.sections.items():
            display = spec.get("display_name", key)
            importance = str(spec.get("importance", "optional")).lower()
            hit: str | None = None

            for alias in spec.get("aliases", []) or []:
                found = find_heading(_normalise_heading(str(alias)))
                if found is not None:
                    raw, line_number, method = found
                    hit = str(alias)
                    section_evidence[display] = {
                        "canonical_name": display, "original_heading": raw,
                        "detection_method": method, "line_number": line_number,
                        "evidence": raw[:160], "confidence": 0.98 if method == "heading" else 0.9,
                    }
                    break
            if hit is None:
                for alias in spec.get("aliases", []) or []:
                    found = find_embedded(_normalise_heading(str(alias)))
                    if found is not None:
                        raw, line_number, method = found
                        hit = str(alias)
                        section_evidence[display] = {
                            "canonical_name": display, "original_heading": raw,
                            "detection_method": "heading_embedded",
                            "line_number": line_number,
                            "evidence": raw[:160], "confidence": 0.9,
                        }
                        break
            if hit is None:
                # Conditional aliases: the heading alone is not enough
                # (it may be a bare label, e.g. "Portfolio" above one
                # URL) — the content under it must be substantive.
                for alias in spec.get("conditional_aliases", []) or []:
                    alias_norm = _normalise_heading(str(alias))
                    found = find_heading(alias_norm)
                    if found is None:
                        continue
                    raw, line_number, method = found
                    if self._content_gate_passes(raw_lines, line_number, raw, alias_norm):
                        hit = str(alias)
                        section_evidence[display] = {
                            "canonical_name": display, "original_heading": raw,
                            "detection_method": "heading_conditional",
                            "line_number": line_number,
                            "evidence": raw[:160], "confidence": 0.85,
                        }
                    else:
                        conditional_rejected.append({
                            "section": display, "alias": str(alias),
                            "original_heading": raw, "line_number": line_number,
                        })
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
            "conditional_rejected": conditional_rejected,
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

    # -- conditional content gate ------------------------------------
    def _content_gate_passes(self, raw_lines, line_number, raw_heading_line,
                             alias_norm: str) -> bool:
        """True when substantive content follows a conditional heading.

        The window is the lines after the heading, stopping at the
        next recognised section heading or after the configured
        number of lines. The heading line's own inline remainder is
        NOT counted — for a label like "Portfolio: Google Drive
        (link on request)" that remainder is a link description, not
        content. Link-item lines (URLs, handles, "Website: ..." ...)
        are excluded, and URLs, emails and phone numbers are removed
        before counting words, so a bare link block never passes.
        If the first line after the heading is itself a link item,
        the heading is a link label and the window must additionally
        contain work-record evidence (a bullet, a year, or a titled
        entry) — this stops sidebar link labels in two-column
        resumes from borrowing the next column's prose.
        """
        chunks: list[str] = []
        started = False
        taken = 0
        first_line_is_link = False
        seen_first_line = False
        for ln, raw in raw_lines:
            if not started:
                if ln == line_number:
                    started = True
                continue
            if taken >= self._gate_max_lines:
                break
            taken += 1
            if _normalise_heading(raw) in self._known_headings:
                break
            is_link = bool(_LINK_ITEM_RE.search(raw)
                           or _LINK_LABEL_RE.match(raw.strip()))
            if not seen_first_line:
                seen_first_line = True
                first_line_is_link = is_link
            if is_link:
                continue
            chunks.append(raw)
        text = _URL_RE.sub(" ", " ".join(chunks))
        text = _EMAIL_RE.sub(" ", text)
        text = _PHONE_RE.sub(" ", text)
        words = re.findall(r"[A-Za-z][A-Za-z'\-]+", text)
        if len(words) < self._gate_min_words:
            return False
        if first_line_is_link:
            # The heading is acting as a link label (e.g. "PORTFOLIO"
            # above a Behance URL in a sidebar). Two-column extraction
            # can leak the following column's prose into the window,
            # so require actual work-record evidence as well.
            return any(_WORK_MARKER_RE.search(c) for c in chunks)
        return True

    # -- helpers --------------------------------------------------------
    def _all_display_names(self) -> list[str]:
        return [spec.get("display_name", key) for key, spec in self.sections.items()]

    def _display_names_by_importance(self, importance: str) -> list[str]:
        return [spec.get("display_name", key) for key, spec in self.sections.items()
                if str(spec.get("importance", "optional")).lower() == importance]


def evaluate_completeness(text: object, **kwargs: Any) -> CompletenessFilterResult:
    """Convenience function so the filter is usable without a class instance."""
    return CompletenessFilter(**kwargs).evaluate(text)
