"""Snapshot assertion helpers for characterization tests.

Usage:
    assert_matches_snapshot(actual_dict, Path("tests/fixtures/snapshots/foo.json"))

Set UPDATE_SNAPSHOTS=1 to regenerate committed baselines.
"""

import json
import os
from pathlib import Path
from typing import Any

VOLATILE_KEYS: frozenset[str] = frozenset(
    {
        "repo_path",  # absolute path differs per machine
        "output_path",
        "csv_path",
    }
)

FLOAT_PRECISION: int = 4


def normalize(obj: Any) -> Any:
    """Strip VOLATILE_KEYS, sort list-of-dicts by first stable key, round floats."""
    if isinstance(obj, dict):
        result = {}
        for k, v in obj.items():
            if k in VOLATILE_KEYS:
                continue
            result[k] = normalize(v)
        return result
    if isinstance(obj, list):
        normalized = [normalize(item) for item in obj]
        if normalized:
            if all(isinstance(item, dict) for item in normalized):
                # Sort list-of-dicts by the first key's value so insertion-order
                # differences don't break comparisons.
                try:
                    first_keys = [next(iter(item)) for item in normalized if item]
                    if first_keys:
                        key = first_keys[0]
                        normalized.sort(key=lambda d: str(d.get(key, "")))
                except (StopIteration, TypeError):
                    pass
            elif all(isinstance(item, (str, int, float, bool)) for item in normalized):
                # Sort lists of primitives for determinism (e.g. pr_unique_dates from
                # a set, rejection-reason keys, etc.)
                try:
                    normalized.sort(key=str)
                except TypeError:
                    pass
        return normalized
    if isinstance(obj, float):
        return round(obj, FLOAT_PRECISION)
    return obj


def assert_matches_snapshot(actual: Any, snapshot_path: Path) -> None:
    """Compare normalised *actual* to the committed snapshot at *snapshot_path*.

    Pass UPDATE_SNAPSHOTS=1 to write/overwrite the snapshot and pass the test.
    """
    normalised = normalize(actual)

    if os.environ.get("UPDATE_SNAPSHOTS") == "1":
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        snapshot_path.write_text(json.dumps(normalised, indent=2, sort_keys=True))
        return

    assert snapshot_path.exists(), (
        f"Snapshot {snapshot_path} does not exist. "
        "Run with UPDATE_SNAPSHOTS=1 to create it."
    )
    committed = json.loads(snapshot_path.read_text())
    assert normalised == committed, (
        f"Snapshot mismatch for {snapshot_path.name}.\n"
        "If this is an intentional change, regenerate with UPDATE_SNAPSHOTS=1 "
        "and explain in the commit message."
    )
