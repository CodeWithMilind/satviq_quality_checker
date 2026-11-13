"""Calibration Phase 1: deterministic stratified splitting."""

from __future__ import annotations

from calibration.splitting import assign_splits, split_summary


def _items(n, stage="Student", domain="Engineering", prefix="r"):
    return [{"id": f"{prefix}{i}", "file_hash": f"hash-{prefix}{i}",
             "career_stage": stage, "professional_domain": domain}
            for i in range(n)]


def test_exact_ratios_on_round_numbers():
    assignment = assign_splits(_items(100), seed=42)
    assert split_summary(assignment) == {
        "development": 70, "validation": 15, "holdout": 15}


def test_deterministic_same_seed_same_result():
    items = _items(60)
    assert assign_splits(items, seed=7) == assign_splits(items, seed=7)


def test_input_order_does_not_change_assignment():
    items = _items(50)
    forward = assign_splits(items, seed=3)
    backward = assign_splits(list(reversed(items)), seed=3)
    assert forward == backward


def test_every_item_assigned_exactly_once():
    items = _items(37)
    assignment = assign_splits(items, seed=1)
    assert set(assignment) == {item["id"] for item in items}
    assert set(assignment.values()) <= {
        "development", "validation", "holdout"}


def test_duplicate_hash_never_leaks_across_splits():
    items = _items(40)
    items.append({"id": "dupe", "file_hash": "hash-r5",
                  "career_stage": "Student",
                  "professional_domain": "Engineering"})
    assignment = assign_splits(items, seed=42)
    assert assignment["dupe"] == assignment["r5"]


def test_stratification_keeps_stage_mix():
    items = _items(100, stage="Student", prefix="s")
    items += _items(100, stage="Fresher", prefix="f")
    assignment = assign_splits(items, seed=42)
    for prefix in ("s", "f"):
        splits = split_summary(
            {k: v for k, v in assignment.items() if k.startswith(prefix)})
        assert splits == {"development": 70, "validation": 15,
                          "holdout": 15}


def test_small_dataset_lands_in_development_first():
    assignment = assign_splits(_items(1), seed=42)
    assert assignment == {"r0": "development"}


def test_bad_ratios_rejected():
    import pytest
    with pytest.raises(ValueError):
        assign_splits(_items(10), seed=1, ratios=(0.5, 0.5, 0.5))
