"""Version management + changelog for the completeness YAML store.

Every accepted update follows the same safe sequence, all under the
caller's lock:

1. the CURRENT active content is already preserved as
   ``versions/completeness_vNNN.yaml`` (seeding guarantees v001;
   every update writes its own version before becoming active);
2. the proposed updated YAML is validated by the caller
   (yaml_manager.build_updated_config);
3. the new version file is written;
4. the active file is swapped ATOMICALLY (temp file + os.replace),
   so it is never left partially written, even across a crash;
5. the change is appended to ``changelog/changes.json`` (also
   written atomically).

If anything fails before step 4, the active YAML is untouched. If no
keywords were accepted, no version is created at all.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .yaml_manager import YamlStore


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                    prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def version_label(number: int) -> str:
    return f"v{number:03d}"


def read_changelog(store: YamlStore) -> list[dict[str, Any]]:
    try:
        data = json.loads(store.changelog_path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def write_changelog(store: YamlStore, entries: list[dict[str, Any]]) -> None:
    _atomic_write(store.changelog_path,
                  json.dumps(entries, indent=2, ensure_ascii=False) + "\n")


def version_file(store: YamlStore, number: int) -> Path:
    return store.versions_dir / f"completeness_v{number:03d}.yaml"


def apply_update(store: YamlStore, updated_text: str, *,
                 additions: dict[str, list[str]],
                 resume_id: str, filename: str,
                 input_tokens: int | None, output_tokens: int | None,
                 model: str, duration_ms: float,
                 status: str = "success") -> dict[str, Any]:
    """Write the next version, atomically activate it, and record the
    changelog entry. Returns the entry that was recorded."""
    previous = store.current_version()
    new_version = previous + 1
    _atomic_write(version_file(store, new_version), updated_text)
    _atomic_write(store.active_path, updated_text)
    entry = {
        "version": version_label(new_version),
        "previous_version": version_label(previous),
        "new_version": version_label(new_version),
        "resume_id": resume_id,
        "filename": filename,
        "timestamp": _now_iso(),
        "keywords_added": {k: list(v) for k, v in additions.items()},
        "keywords_added_count": sum(len(v) for v in additions.values()),
        "categories_affected": sorted(additions.keys()),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "model": model,
        "duration_ms": round(float(duration_ms), 2),
        "status": status,
    }
    entries = read_changelog(store)
    entries.append(entry)
    write_changelog(store, entries)
    return entry


def record_initialization(store: YamlStore) -> dict[str, Any] | None:
    """One-time changelog entry documenting the seeding of the store
    from the bundled quality_checker/config/completeness.yaml."""
    entries = read_changelog(store)
    if any(e.get("status") == "initialized" for e in entries):
        return None
    entry = {
        "version": version_label(1),
        "previous_version": None,
        "new_version": version_label(1),
        "resume_id": None,
        "filename": None,
        "timestamp": _now_iso(),
        "keywords_added": {},
        "keywords_added_count": 0,
        "categories_affected": [],
        "input_tokens": None,
        "output_tokens": None,
        "model": None,
        "duration_ms": 0.0,
        "status": "initialized",
        "note": "Seeded byte-identically from "
                "quality_checker/config/completeness.yaml; the active "
                "file is now the single source of truth.",
    }
    entries.append(entry)
    write_changelog(store, entries)
    return entry
