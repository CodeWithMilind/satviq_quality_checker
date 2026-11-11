"""Compact, low-token prompt construction for YAML Editor AI.

Token discipline (the module's core constraint):
- ONE compact instruction — no long system prompt, no history, no
  resume summaries, no repeated explanations;
- the YAML side of the prompt contains ONLY the alias lists of the
  relevant categories (the sections the completeness checker missed
  or found uncertain) — never the whole configuration file;
- the resume text is sent ONCE, truncated to a configured cap;
- the model must answer with strict JSON grouped by the EXISTING
  YAML category names, which are listed explicitly.
"""

from __future__ import annotations

from typing import Any

#: The single compact instruction sent as the developer message.
INSTRUCTION = (
    "Identify missing resume section headings and relevant keyword "
    "variations absent from the existing YAML. Return only genuinely "
    "useful additions as strict JSON grouped by existing YAML "
    "categories. Never suggest duplicates or invented content.")


def relevant_categories(config: dict[str, Any],
                        missing_sections: list[str] | None) -> list[str]:
    """Categories worth sending: the sections completeness missed.
    Falls back to every scored section when the caller cannot say
    which sections were missed (still bounded — alias lists only)."""
    sections = config.get("sections") or {}
    if missing_sections:
        wanted = [name for name in missing_sections if name in sections]
        if wanted:
            return wanted
    return list(sections.keys())


def build_prompt(config: dict[str, Any], resume_text: str, *,
                 missing_sections: list[str] | None = None,
                 max_resume_chars: int = 6000) -> tuple[str, str, list[str]]:
    """Return ``(instruction, user_text, categories_sent)``.

    The user text carries, in order: the category catalogue with the
    EXISTING aliases for the relevant categories only, then the
    resume text (once, capped)."""
    sections = config.get("sections") or {}
    categories = relevant_categories(config, missing_sections)
    lines = ["Existing YAML categories and their current keywords:"]
    for name in categories:
        section = sections.get(name) or {}
        aliases = [str(a) for a in (section.get("aliases") or [])]
        aliases += [str(a) for a in (section.get("conditional_aliases")
                                     or [])]
        display = section.get("display_name") or name
        lines.append(f"- {name} ({display}): " + ", ".join(aliases))
    lines.append("")
    lines.append("Reply with strict JSON only, keyed ONLY by the "
                 "category names above, e.g. "
                 '{"skills": ["technical skills"]}. '
                 "Use {} when nothing is missing.")
    lines.append("")
    lines.append("Resume text:")
    lines.append(resume_text[:max_resume_chars])
    return INSTRUCTION, "\n".join(lines), categories
