"""Strict loading for human-labelled JSON audit fixtures."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def load_json_fixture(path: str | Path) -> Any:
    """Load a fixture while rejecting duplicate keys hidden by stdlib JSON."""
    with Path(path).open(encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=_unique_json_object)
