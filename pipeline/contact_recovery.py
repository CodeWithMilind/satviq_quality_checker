"""Contact Information Recovery — a targeted post-normalization
stage of the simple pipeline.

Problem (real case, ``download_12_.pdf``): the selected extractor's
text can lose the resume's contact header — that PDF's text is
stored as vector outlines, so the text-layer extractors see almost
nothing, Docling's output contains the body but not the phone /
email / location line, and the Quality Checker then reports
Contact Information missing even though the information is plainly
on page 1 of the PDF.

This stage runs AFTER selection + normalization and BEFORE the LLM
stage, on the selected extractor's normalized text only:

1. Detect which of phone / email / location are missing from the
   selected normalized text.
2. If any are missing, look for them — in order — in (a) the other
   extractors' already-extracted outputs, (b) the original PDF's
   first-page text layer (PyMuPDF, read directly), and (c) OCR of
   the original file (the existing OCR extractor; its first page).
3. Validate every candidate (email shape; phone digit count;
   location must carry a PIN code or be a header City, State line).
4. Merge ONLY the recovered fields into the normalized text as a
   clearly headed ``Contact Information`` block. The selected
   extractor is never replaced, its original text is never touched,
   nothing is duplicated, and nothing is invented: a field that no
   source yields stays missing and is reported as such.

The returned block records full provenance: which fields were
already present, which were recovered from which source by which
method, which sources were tried, and — when the text was
corrected — the pre-recovery normalized text.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

FIELDS = ("phone", "email", "location")

# The same shapes the Quality Checker's completeness patterns use,
# so "present" here means what the checker means.
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_CANDIDATE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
PIN_RE = re.compile(r"\b[1-9]\d{5}\b")
# "Pune, Maharashtra 411068" — City, State PIN.
LOCATION_PIN_RE = re.compile(
    r"[A-Za-z][A-Za-z .]{1,40}?,\s*[A-Za-z][A-Za-z .]{1,40}?\s+"
    r"[1-9]\d{5}\b")
# "Pune, Maharashtra" / "Pune, India" as a standalone header line.
LOCATION_LINE_RE = re.compile(
    r"^\s*[A-Z][A-Za-z. ]{1,30},\s*[A-Z][A-Za-z. ]{1,30}\s*$")

SOURCE_LABELS = {"tika": "Tika", "pymupdf": "PyMuPDF",
                 "docling": "Docling", "ocr": "OCR"}


# -- validation / extraction -----------------------------------------

def _valid_email(value: str) -> bool:
    return bool(value) and len(value) <= 254 and \
        EMAIL_RE.fullmatch(value) is not None


def _valid_phone(candidate: str) -> bool:
    digits = re.sub(r"\D", "", candidate or "")
    if not (10 <= len(digits) <= 15):
        return False
    if len(set(digits)) == 1:
        return False
    return True


def _find_email(text: str) -> str | None:
    for match in EMAIL_RE.finditer(text or ""):
        if _valid_email(match.group(0)):
            return match.group(0)
    return None


def _find_phone(text: str) -> str | None:
    for match in PHONE_CANDIDATE_RE.finditer(text or ""):
        candidate = match.group(0).strip(" |·•-")
        if _valid_phone(candidate):
            return candidate
    return None


def _find_location(text: str) -> str | None:
    match = LOCATION_PIN_RE.search(text or "")
    if match:
        return match.group(0).strip(" |·•-")
    # Fallback: a standalone "City, State" header line.
    for line in (text or "").splitlines()[:15]:
        if LOCATION_LINE_RE.match(line):
            return line.strip()
    return None


_FINDERS: dict[str, Callable[[str], str | None]] = {
    "phone": _find_phone, "email": _find_email,
    "location": _find_location,
}


def detect_present(text: str) -> dict[str, bool]:
    """Which contact fields are detectably present in ``text`` —
    the same evidence the Completeness checker accepts."""
    return {field: _FINDERS[field](text) is not None for field in FIELDS}


def extract_fields(text: str, wanted: list[str]) -> dict[str, str]:
    """Extract validated values for the ``wanted`` fields from a
    candidate text. Fields with no valid value are simply absent
    from the result — never guessed."""
    found: dict[str, str] = {}
    for field in wanted:
        value = _FINDERS[field](text or "")
        if value is not None:
            found[field] = value
    return found


# -- default recovery sources ------------------------------------------

def _first_page_text_layer(path: Path) -> str:
    """The original PDF's first-page text layer, read directly with
    PyMuPDF (independent of what the PyMuPDF extractor attempt
    produced). Empty string when there is no usable text layer."""
    import pymupdf

    document = pymupdf.open(path)
    try:
        if document.page_count == 0:
            return ""
        return document[0].get_text() or ""
    finally:
        document.close()


def _ocr_first_page_text(path: Path) -> tuple[str, str | None]:
    """OCR the original file with the existing OCR extractor and
    return its FIRST page's text (contact headers live on page 1).
    Returns (text, error) — error is the extractor's own message
    when OCR is unavailable or fails; nothing is fabricated."""
    from extractor.extractors.ocr import OCRExtractor
    from extractor.manager import load_manager_config

    config = load_manager_config()
    per_extractor = config.get("extractors", {}) or {}
    extractor = OCRExtractor(config=per_extractor.get("ocr") or {})
    result = extractor.extract(str(path))
    if not result.success:
        return "", result.error or "OCR extraction failed."
    if result.page_texts:
        return result.page_texts[0] or "", None
    return result.text or "", None


# -- the stage -----------------------------------------------------------

def _merge_block(recovered: dict[str, dict[str, Any]]) -> str:
    lines = ["Contact Information"]
    labels = {"phone": "Phone", "email": "Email", "location": "Location"}
    for field in FIELDS:
        if field in recovered:
            lines.append(f"{labels[field]}: {recovered[field]['value']}")
    return "\n".join(lines)


def recover_contact_information(
    *,
    normalized_text: str,
    selected_extractor: str | None,
    results: list[dict[str, Any]],
    file_path: str | Path,
    text_layer_reader: Callable[[Path], str] | None = None,
    ocr_runner: Callable[[Path], tuple[str, str | None]] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run contact recovery over the selected normalized text.

    Returns ``(text_to_send_onward, recovery_block)``. The text is
    the input unchanged unless validated fields were recovered, in
    which case a ``Contact Information`` block carrying ONLY the
    recovered fields is prepended. The original extraction attempts
    and the selected extractor are never modified.
    """
    path = Path(file_path)
    present = detect_present(normalized_text)
    missing = [f for f in FIELDS if not present[f]]
    block: dict[str, Any] = {
        "status": "not_needed",
        "selected_extractor": selected_extractor,
        "present_before": [f for f in FIELDS if present[f]],
        "missing_before": missing,
        "recovered": {},
        "still_missing": list(missing),
        "sources_tried": [],
        "applied": False,
        "pre_recovery_normalized_text": None,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if not missing:
        return normalized_text, block

    recovered: dict[str, dict[str, Any]] = {}

    def _harvest(source_label: str, method: str, text: str,
                 error: str | None = None) -> None:
        wanted = [f for f in missing if f not in recovered]
        found = extract_fields(text, wanted) if text and wanted else {}
        for field, value in found.items():
            recovered[field] = {"value": value, "source": source_label,
                                "method": method}
        block["sources_tried"].append({
            "source": source_label, "method": method,
            "found": sorted(found), "error": error})

    # (a) The extractors' own already-extracted outputs.
    for res in results or []:
        if len(recovered) == len(missing):
            break
        name = res.get("extractor") or ""
        text = res.get("extracted_text") or res.get("text") or ""
        if not text:
            continue
        _harvest(SOURCE_LABELS.get(name, name or "extractor"),
                 "extractor_output", text)

    # (b) The original PDF's first-page text layer.
    if len(recovered) < len(missing) and path.suffix.lower() == ".pdf":
        reader = text_layer_reader or _first_page_text_layer
        try:
            layer_text = reader(path)
            _harvest("PyMuPDF", "first_page_text_layer", layer_text)
        except Exception as exc:
            block["sources_tried"].append({
                "source": "PyMuPDF", "method": "first_page_text_layer",
                "found": [], "error": str(exc)})

    # (c) OCR of the original file (first page).
    if len(recovered) < len(missing):
        runner = ocr_runner or _ocr_first_page_text
        try:
            ocr_text, ocr_error = runner(path)
            _harvest("OCR", "ocr_first_page", ocr_text, error=ocr_error)
        except Exception as exc:
            block["sources_tried"].append({
                "source": "OCR", "method": "ocr_first_page",
                "found": [], "error": str(exc)})

    block["recovered"] = recovered
    block["still_missing"] = [f for f in missing if f not in recovered]
    if not recovered:
        block["status"] = "not_recovered"
        return normalized_text, block

    block["status"] = "recovered" if not block["still_missing"] else "partial"
    block["applied"] = True
    block["pre_recovery_normalized_text"] = normalized_text
    corrected = _merge_block(recovered) + "\n\n" + normalized_text
    logger.info(
        "Contact recovery for %s: %s recovered (%s); still missing: %s",
        path.name, sorted(recovered),
        ", ".join(f"{f}<-{recovered[f]['source']}" for f in recovered),
        block["still_missing"])
    return corrected, block
