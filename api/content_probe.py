"""Deterministic content probes for direct-LLM routing.

When a resume dead-ends at the quality gate, the usual cause is a
DETECTION gap, not missing content: the completeness filter works
from headings, so a typo'd or glued heading (``WORKEXPRIENCE``,
``HANDLINGEQUIPMENT``, ``ACADAMICQUALIFICATION``) makes a section
"missing" even though its content is fully present in the extracted
text. Discarding such a resume wastes a readable document — the LLM
text stage reads the normalized text itself and never needed the
heading verdict.

This module answers one question per section: *is this section's
content detectably present in the text?* Probes are typo-tolerant
stems (case-insensitive substrings, also matched space-free so glued
headings hit) and regular expressions, configured in the
``content_probes`` block of
``quality_checker/config/completeness.yaml``. The Quality Checker
never reads that block — probing is a routing concern, and probe
results never change any quality score.

Used by :func:`api.pipeline._apply_direct_llm_routing`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

_CONFIG_PATH = (Path(__file__).resolve().parents[1]
                / "quality_checker" / "config" / "completeness.yaml")

_cache: dict[str, dict[str, list[str]]] | None = None


def load_probes(*, reload: bool = False,
                path: Path | None = None) -> dict[str, dict[str, list[str]]]:
    """The ``content_probes`` mapping from completeness.yaml:
    ``{section: {"stems": [...], "patterns": [...]}}``. Cached;
    returns {} when the block or file is unavailable (probing then
    simply finds nothing and routing stays off)."""
    global _cache
    if _cache is not None and not reload and path is None:
        return _cache
    probes: dict[str, dict[str, list[str]]] = {}
    try:
        data = yaml.safe_load(
            (path or _CONFIG_PATH).read_text(encoding="utf-8")) or {}
        raw = data.get("content_probes") or {}
        for section, block in raw.items():
            block = block or {}
            probes[str(section)] = {
                "stems": [str(s) for s in (block.get("stems") or [])],
                "patterns": [str(p) for p in (block.get("patterns") or [])],
            }
    except Exception:
        probes = {}
    if path is None:
        _cache = probes
    return probes


def probe_section(text: str, section: str,
                  probes: dict[str, dict[str, list[str]]] | None = None,
                  ) -> dict[str, Any]:
    """Probe one section against ``text``.

    Returns ``{"present": bool, "evidence": [{"type", "signal",
    "match"}, ...]}`` — present when at least one stem or pattern
    hits. Evidence carries the matched text so the routing decision
    stays auditable in the record."""
    block = (probes if probes is not None else load_probes()).get(section)
    if not block or not text:
        return {"present": False, "evidence": []}
    lowered = text.lower()
    squashed = lowered.replace(" ", "")
    evidence: list[dict[str, str]] = []
    for stem in block["stems"]:
        needle = stem.lower()
        hit = needle in lowered or needle.replace(" ", "") in squashed
        if hit:
            evidence.append({"type": "stem", "signal": stem,
                             "match": stem})
    for pattern in block["patterns"]:
        try:
            match = re.search(pattern, text)
        except re.error:
            continue
        if match:
            evidence.append({"type": "pattern", "signal": pattern,
                             "match": match.group(0)[:80]})
    return {"present": bool(evidence), "evidence": evidence}


def probe_sections(text: str, sections: list[str],
                   probes: dict[str, dict[str, list[str]]] | None = None,
                   ) -> dict[str, dict[str, Any]]:
    """Probe several sections; ``{section: probe_section(...)}``."""
    resolved = probes if probes is not None else load_probes()
    return {section: probe_section(text, section, resolved)
            for section in sections}
