"""YAML access + deterministic keyword validation for YAML Editor AI.

Responsibilities:
- locate and seed the versioned YAML store (``yaml files/``), whose
  ``active/completeness.yaml`` is the single source of truth the
  Quality Checker loads (see quality_checker/filters/_config.py);
- read the current configuration + section catalogue;
- validate LLM suggestions deterministically (structure, known
  categories, length, duplicates — case/space-insensitive — and
  presence of the keyword in the resume text, so nothing invented
  can be appended);
- append accepted keywords to the ``aliases`` list of their section
  by TEXT editing, preserving the file's comments, ordering and
  formatting; the result is re-parsed and verified to be a superset
  of the previous configuration before it is returned.

The LLM never touches files — this module (via version_manager) is
the only writer, and it is append-only: existing keywords are never
deleted or modified.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

#: The bundled completeness YAML the Quality Checker shipped with.
#: It seeds the versioned store on first use and remains as the
#: fallback when no active file exists.
BUNDLED_COMPLETENESS = (
    Path(__file__).resolve().parent.parent
    / "quality_checker" / "config" / "completeness.yaml")


def project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def normalize_keyword(value: str) -> str:
    """Canonical comparison form: lowercase, single spaces, trimmed."""
    return re.sub(r"\s+", " ", value.strip().lower())


def compact_form(value: str) -> str:
    """Space-free comparison form (catches glued duplicates like
    'technicalskills' vs 'technical skills')."""
    return re.sub(r"[^a-z0-9]+", "", value.lower())


class YamlStore:
    """Paths + seeding for the versioned completeness YAML store."""

    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.yaml_dir = self.root / "yaml files"
        self.active_dir = self.yaml_dir / "active"
        self.versions_dir = self.yaml_dir / "versions"
        self.changelog_dir = self.yaml_dir / "changelog"
        self.active_path = self.active_dir / "completeness.yaml"
        self.changelog_path = self.changelog_dir / "changes.json"

    # -- seeding ---------------------------------------------------------
    def ensure_seeded(self) -> None:
        """Create the store from the bundled YAML if it does not yet
        exist. The active file starts byte-identical to the bundled
        configuration, and version 1 records exactly that content, so
        no existing keyword can be lost by the migration."""
        self.active_dir.mkdir(parents=True, exist_ok=True)
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        self.changelog_dir.mkdir(parents=True, exist_ok=True)
        if not self.active_path.is_file():
            shutil.copyfile(BUNDLED_COMPLETENESS, self.active_path)
            logger.info("YAML Editor AI: seeded active completeness "
                        "YAML from %s", BUNDLED_COMPLETENESS)
        v1 = self.versions_dir / "completeness_v001.yaml"
        if not v1.is_file():
            shutil.copyfile(self.active_path, v1)

    # -- reading ---------------------------------------------------------
    def read_active_text(self) -> str:
        return self.active_path.read_text(encoding="utf-8")

    def read_active(self) -> dict[str, Any]:
        data = yaml.safe_load(self.read_active_text()) or {}
        if not isinstance(data, dict):
            raise ValueError("Active completeness YAML is not a mapping.")
        return data

    def sections(self) -> dict[str, Any]:
        return self.read_active().get("sections") or {}

    def version_numbers(self) -> list[int]:
        numbers = []
        for path in self.versions_dir.glob("completeness_v*.yaml"):
            match = re.fullmatch(r"completeness_v(\d+)\.yaml", path.name)
            if match:
                numbers.append(int(match.group(1)))
        return sorted(numbers)

    def current_version(self) -> int:
        numbers = self.version_numbers()
        return numbers[-1] if numbers else 1


def existing_keyword_forms(config: dict[str, Any]) -> dict[str, set[str]]:
    """Per-section set of comparison forms already configured
    (aliases + conditional_aliases, in both spaced and glued form)."""
    forms: dict[str, set[str]] = {}
    for name, section in (config.get("sections") or {}).items():
        seen: set[str] = set()
        for key in ("aliases", "conditional_aliases"):
            for alias in (section or {}).get(key) or []:
                seen.add(normalize_keyword(str(alias)))
                seen.add(compact_form(str(alias)))
        forms[name] = seen
    return forms


def validate_suggestions(raw: Any, *, config: dict[str, Any],
                         resume_text: str, max_keyword_chars: int = 60,
                         max_per_category: int = 5,
                         max_total: int = 12) -> tuple[dict[str, list[str]], list[dict[str, str]]]:
    """Deterministically validate raw LLM suggestions.

    Returns ``(accepted, rejected)`` where accepted maps existing
    section names to genuinely new alias strings, and rejected lists
    ``{"category", "keyword", "reason"}`` for everything else:

    - response is not a JSON object / category value is not a list;
    - category does not exist in the current YAML (no new categories
      are ever created by the agent);
    - keyword empty, over-long, or not a string;
    - keyword duplicates an existing alias or another suggestion
      (case-insensitive and space-insensitive);
    - keyword does not literally occur in the resume text (spaced or
      glued) — invented content is rejected.
    """
    accepted: dict[str, list[str]] = {}
    rejected: list[dict[str, str]] = []
    if not isinstance(raw, dict):
        return {}, [{"category": "", "keyword": "",
                     "reason": "response_not_an_object"}]
    sections = config.get("sections") or {}
    forms = existing_keyword_forms(config)
    text_norm = normalize_keyword(resume_text)
    text_compact = compact_form(resume_text)
    total = 0
    for category, values in raw.items():
        if category not in sections:
            items = values if isinstance(values, list) else [values]
            for item in items:
                rejected.append({"category": str(category),
                                 "keyword": str(item),
                                 "reason": "unknown_category"})
            continue
        if not isinstance(values, list):
            rejected.append({"category": category, "keyword": str(values),
                             "reason": "category_value_not_a_list"})
            continue
        for item in values:
            keyword = item if isinstance(item, str) else ""
            cleaned = re.sub(r"\s+", " ", keyword.strip())
            if not cleaned:
                rejected.append({"category": category, "keyword": str(item),
                                 "reason": "empty_or_not_a_string"})
                continue
            if len(cleaned) > max_keyword_chars:
                rejected.append({"category": category, "keyword": cleaned,
                                 "reason": "too_long"})
                continue
            norm = normalize_keyword(cleaned)
            comp = compact_form(cleaned)
            if norm in forms[category] or comp in forms[category]:
                rejected.append({"category": category, "keyword": cleaned,
                                 "reason": "duplicate"})
                continue
            if norm not in text_norm and comp not in text_compact:
                rejected.append({"category": category, "keyword": cleaned,
                                 "reason": "not_in_resume_text"})
                continue
            if len(accepted.get(category, [])) >= max_per_category \
                    or total >= max_total:
                rejected.append({"category": category, "keyword": cleaned,
                                 "reason": "over_limit"})
                continue
            accepted.setdefault(category, []).append(cleaned.lower())
            forms[category].add(norm)
            forms[category].add(comp)
            total += 1
    return accepted, rejected


def append_aliases_to_text(yaml_text: str,
                           additions: dict[str, list[str]]) -> str:
    """Append aliases to each section's ``aliases:`` list IN TEXT.

    Text editing (instead of a YAML round-trip) preserves comments,
    evidence annotations, ordering and formatting. Each section's
    list is located by its exact indentation; new items are appended
    after the list's last item with matching indentation and a
    ``# [yaml-editor-ai]`` provenance comment.
    """
    lines = yaml_text.splitlines()
    # Locate sections: two-space indented keys under `sections:`.
    section_starts: dict[str, int] = {}
    in_sections = False
    for idx, line in enumerate(lines):
        if re.match(r"^sections:\s*$", line):
            in_sections = True
            continue
        if in_sections:
            if line and not line.startswith((" ", "#")):
                break  # next top-level key ends the sections block
            match = re.match(r"^  ([a-z0-9_]+):\s*$", line)
            if match:
                section_starts[match.group(1)] = idx
    insertions: list[tuple[int, list[str]]] = []
    for section, keywords in additions.items():
        start = section_starts.get(section)
        if start is None:
            raise ValueError(f"Section {section!r} not found in YAML text.")
        # Find this section's `aliases:` line before the next section.
        following = [i for i in section_starts.values() if i > start]
        end = min(following) if following else len(lines)
        alias_idx = None
        for idx in range(start, end):
            if re.match(r"^    aliases:\s*$", lines[idx]):
                alias_idx = idx
                break
        if alias_idx is None:
            raise ValueError(
                f"Section {section!r} has no aliases list to append to.")
        # Last item of the aliases block (items are 6-space list
        # rows). Comment lines INSIDE the list belong to the NEXT
        # block when they introduce it (e.g. a comment directly above
        # `conditional_aliases:`), so insertion happens before any
        # trailing comment run — never between a block's introductory
        # comment and its key line.
        last_item = alias_idx
        indent = "      "
        pending_comments: list[int] = []
        for idx in range(alias_idx + 1, end):
            row = lines[idx]
            if re.match(r"^      - ", row):
                last_item = idx
                indent = row[:len(row) - len(row.lstrip())]
                pending_comments = []
            elif row.strip() == "":
                pending_comments = []
            elif row.lstrip().startswith("#"):
                pending_comments.append(idx)
            else:
                break
        if pending_comments:
            last_item = pending_comments[0] - 1
        new_rows = [f'{indent}- "{kw}"'.ljust(40) + "# [yaml-editor-ai]"
                    for kw in keywords]
        insertions.append((last_item + 1, new_rows))
    for position, rows in sorted(insertions, reverse=True):
        lines[position:position] = rows
    return "\n".join(lines) + "\n"


def build_updated_config(current_text: str, current_config: dict[str, Any],
                         additions: dict[str, list[str]]) -> str:
    """Produce the updated YAML text and VERIFY it before returning:
    it must parse, keep every previously configured keyword, and add
    exactly the accepted additions — nothing else may change."""
    updated_text = append_aliases_to_text(current_text, additions)
    updated = yaml.safe_load(updated_text)
    if not isinstance(updated, dict) or "sections" not in updated:
        raise ValueError("Updated YAML failed to parse as a config.")
    for name, section in (current_config.get("sections") or {}).items():
        before = set((section or {}).get("aliases") or [])
        after = set(((updated["sections"].get(name) or {})
                     .get("aliases") or []))
        if not before <= after:
            raise ValueError(
                f"Update would lose existing aliases for {name!r}.")
    for name, keywords in additions.items():
        after = set(((updated["sections"].get(name) or {})
                     .get("aliases") or []))
        if not set(keywords) <= after:
            raise ValueError(f"Additions for {name!r} missing after edit.")
    return updated_text
