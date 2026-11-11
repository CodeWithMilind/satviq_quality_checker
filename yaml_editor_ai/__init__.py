"""YAML Editor AI — a self-improving completeness keyword engine.

An independent, low-token agent that analyses LOW-completeness
resumes after the normal extraction pipeline has finished, asks Muse
Spark for genuinely missing section-heading / keyword variations, and
— after strict deterministic validation — appends only new keywords
to the versioned completeness YAML under ``yaml files/``.

Independence guarantees:
- own configuration (``yaml_editor_ai/config.yaml`` + YAML_EDITOR_*
  environment variables), own Muse client, own timeout;
- own token tracking (``token_usage.json``) — never mixed with the
  main pipeline's ``record["token_usage"]``;
- own analytics endpoints (``/api/yaml-editor/*``);
- the LLM NEVER writes YAML directly — a deterministic Python
  validation layer decides what is appended, and every accepted
  update is versioned + changelogged;
- a resume's already-completed quality score is NEVER modified —
  new keywords apply only to future evaluations.
"""

from __future__ import annotations

from .agent import (YamlEditorAgent, get_default_agent,
                    maybe_improve_from_record, record_yaml_editor_block,
                    yaml_editor_block_for_record)

__all__ = ["YamlEditorAgent", "get_default_agent",
           "maybe_improve_from_record", "record_yaml_editor_block",
           "yaml_editor_block_for_record"]
