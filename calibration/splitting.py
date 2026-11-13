"""Deterministic stratified dataset splitting (spec Part E).

70% development / 15% validation / 15% holdout by default, stratified
by (career_stage, professional_domain) so every split mirrors the
dataset's composition. The same dataset + seed always produces the
same assignment, on any platform: ordering is fully sorted and the
per-stratum shuffle is seeded from a SHA-256 of the inputs, never
from Python's salted ``hash()``.

Leakage prevention: resumes sharing a content hash are one unit —
the same file can never land in two splits.
"""

from __future__ import annotations

import hashlib
import random
from typing import Any

from .schemas import SPLIT_NAMES

DEFAULT_RATIOS: tuple[float, float, float] = (0.70, 0.15, 0.15)
DEFAULT_SEED = 42


def _stratum_seed(seed: int, stage: str, domain: str) -> int:
    digest = hashlib.sha256(
        f"{seed}|{stage}|{domain}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _split_counts(n: int,
                  ratios: tuple[float, float, float]) -> tuple[int, int, int]:
    """Largest-remainder apportionment of n units across the splits,
    deterministic on ties (development, then validation, then
    holdout)."""
    raw = [n * ratio for ratio in ratios]
    counts = [int(value) for value in raw]
    remaining = n - sum(counts)
    order = sorted(range(3), key=lambda i: (-(raw[i] - counts[i]), i))
    for i in order[:remaining]:
        counts[i] += 1
    return counts[0], counts[1], counts[2]


def assign_splits(
    items: list[dict[str, Any]],
    seed: int = DEFAULT_SEED,
    ratios: tuple[float, float, float] = DEFAULT_RATIOS,
) -> dict[str, str]:
    """Assign each item to a split. Items are dicts with ``id``,
    ``file_hash``, ``career_stage`` and ``professional_domain`` keys
    (stage/domain come from the item's ground-truth annotation).
    Returns ``{item_id: split_name}`` covering every item."""
    if len(ratios) != 3 or any(r < 0 for r in ratios) \
            or abs(sum(ratios) - 1.0) > 1e-9:
        raise ValueError("ratios must be three non-negative numbers "
                         "summing to 1")

    # Group by content hash — the group is the indivisible unit.
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        groups.setdefault(str(item.get("file_hash") or item["id"]),
                          []).append(item)

    # One stratum per group, taken from its lowest-id member so the
    # choice never depends on input order.
    strata: dict[tuple[str, str], list[str]] = {}
    for file_hash, members in groups.items():
        members_sorted = sorted(members, key=lambda m: str(m["id"]))
        first = members_sorted[0]
        stratum = (str(first.get("career_stage") or ""),
                   str(first.get("professional_domain") or ""))
        strata.setdefault(stratum, []).append(file_hash)

    assignment: dict[str, str] = {}
    for stratum in sorted(strata):
        hashes = sorted(strata[stratum])
        rng = random.Random(_stratum_seed(seed, stratum[0], stratum[1]))
        rng.shuffle(hashes)
        n_dev, n_val, _n_hold = _split_counts(len(hashes), ratios)
        for index, file_hash in enumerate(hashes):
            if index < n_dev:
                split = SPLIT_NAMES[0]
            elif index < n_dev + n_val:
                split = SPLIT_NAMES[1]
            else:
                split = SPLIT_NAMES[2]
            for member in groups[file_hash]:
                assignment[str(member["id"])] = split
    return assignment


def split_summary(assignment: dict[str, str]) -> dict[str, int]:
    """Counts per split name (all three keys always present)."""
    summary = {name: 0 for name in SPLIT_NAMES}
    for split in assignment.values():
        summary[split] = summary.get(split, 0) + 1
    return summary
