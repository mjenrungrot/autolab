#!/usr/bin/env python3
"""Result sanity checks for metrics artifacts."""

from __future__ import annotations

import json
import math
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / ".autolab" / "state.json"
PLACEHOLDER_TOKENS = {"<iteration_id>", "<run_id>", "template"}


def _load_state() -> dict:
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("state.json must contain an object")
    return data


def _has_placeholders(payload) -> bool:
    if isinstance(payload, str):
        normalized = payload.lower()
        return any(token in normalized for token in PLACEHOLDER_TOKENS)
    return False


def _validate_metrics(path: Path, failures: list[str]) -> None:
    if not path.exists():
        failures.append(f"{path} is missing")
        return

    try:
        metrics = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        failures.append(f"{path} is not valid JSON: {exc}")
        return

    if not isinstance(metrics, dict):
        failures.append(f"{path} must contain a JSON object")
        return

    if not metrics:
        failures.append(f"{path} is empty")
        return

    # Explicit placeholder guard
    normalized_dump = json.dumps(metrics).lower()
    if _has_placeholders(normalized_dump):
        failures.append(f"{path} appears to contain placeholder content")

    # Reject empty numeric states and NaN-like values.
    def walk(obj, key_prefix=""):
        if isinstance(obj, dict):
            for key, value in obj.items():
                walk(value, f"{key_prefix}.{key}" if key_prefix else str(key))
        elif isinstance(obj, list):
            for idx, value in enumerate(obj):
                walk(value, f"{key_prefix}[{idx}]")
        elif isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                failures.append(f"{path} has invalid numeric value at {key_prefix}: {obj}")
        elif isinstance(obj, str):
            if _has_placeholders(obj):
                failures.append(f"{path} has placeholder-like string at {key_prefix}: {obj}")

    walk(metrics)

    # Require at least one informative key for extract-results stage.
    has_info = any(
        key not in {"status", "iteration_id", "run_id"}
        for key in metrics.keys()
    )
    if not has_info:
        failures.append(f"{path} does not include metric payload beyond status metadata")


def main() -> int:
    failures: list[str] = []

    try:
        state = _load_state()
    except Exception as exc:
        print(f"result_sanity: ERROR {exc}")
        return 1

    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    stage = str(state.get("stage", "")).strip()
    if not iteration_id or not run_id:
        print("result_sanity: ERROR missing iteration_id/last_run_id in state")
        return 1

    if stage != "extract_results":
        print("result_sanity: PASS")
        return 0

    metrics_path = REPO_ROOT / "experiments" / iteration_id / "runs" / run_id / "metrics.json"
    _validate_metrics(metrics_path, failures)

    if failures:
        print("result_sanity: FAIL")
        for reason in failures:
            print(reason)
        return 1

    print("result_sanity: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
