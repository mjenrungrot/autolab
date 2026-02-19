#!/usr/bin/env python3
"""Result sanity checks for metrics artifacts."""

from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path

from verifier_lib import load_state, make_result, print_result, resolve_iteration_dir

PLACEHOLDER_TOKENS = {"<iteration_id>", "<run_id>", "<TODO>", "placeholder"}
PLACEHOLDER_PATTERNS = (
    re.compile(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}"),
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bTBD\b", re.IGNORECASE),
)


def _has_placeholders(payload) -> bool:
    if isinstance(payload, str):
        lowered = payload.lower()
        if any(token in lowered for token in PLACEHOLDER_TOKENS):
            return True
        return any(pattern.search(lowered) for pattern in PLACEHOLDER_PATTERNS)
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

    if _has_placeholders(json.dumps(metrics)):
        failures.append(f"{path} appears to contain placeholder content")

    for key in {"iteration_id", "run_id", "status", "primary_metric"}:
        if key not in metrics:
            failures.append(f"{path} missing required key '{key}'")

    status = str(metrics.get("status", "")).strip().lower()
    if status not in {"completed", "partial", "failed"}:
        failures.append(f"{path} status must be one of completed|partial|failed")

    def walk(obj, key_prefix=""):
        if isinstance(obj, dict):
            for key, value in obj.items():
                walk(value, f"{key_prefix}.{key}" if key_prefix else str(key))
        elif isinstance(obj, list):
            for idx, value in enumerate(obj):
                walk(value, f"{key_prefix}[{idx}]")
        elif isinstance(obj, float):
            if math.isnan(obj) or math.isinf(obj):
                failures.append(
                    f"{path} has invalid numeric value at {key_prefix}: {obj}"
                )
        elif isinstance(obj, str):
            if _has_placeholders(obj):
                failures.append(
                    f"{path} has placeholder-like string at {key_prefix}: {obj}"
                )

    walk(metrics)

    primary_metric = metrics.get("primary_metric")
    if not isinstance(primary_metric, dict):
        failures.append(f"{path} primary_metric must be a mapping")
    elif not {"name", "value", "delta_vs_baseline"} <= set(primary_metric.keys()):
        failures.append(f"{path} primary_metric requires name/value/delta_vs_baseline")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON envelope",
    )
    args = parser.parse_args()

    failures: list[str] = []

    try:
        state = load_state()
    except Exception as exc:
        result = make_result("result_sanity", "", [], [str(exc)])
        print_result(result, as_json=args.json)
        return 1

    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    stage = str(state.get("stage", "")).strip()
    if not iteration_id or not run_id:
        result = make_result(
            "result_sanity", stage, [], ["missing iteration_id/last_run_id in state"]
        )
        print_result(result, as_json=args.json)
        return 1

    if stage != "extract_results":
        result = make_result(
            "result_sanity",
            stage,
            [
                {
                    "name": "result_sanity",
                    "status": "pass",
                    "detail": f"skipped for stage={stage}",
                }
            ],
            [],
        )
        print_result(result, as_json=args.json)
        return 0

    metrics_path = (
        resolve_iteration_dir(iteration_id) / "runs" / run_id / "metrics.json"
    )
    _validate_metrics(metrics_path, failures)

    passed = not failures

    checks = [{"name": f, "status": "fail", "detail": f} for f in failures]
    if passed:
        checks = [
            {
                "name": "result_sanity",
                "status": "pass",
                "detail": "all result sanity checks passed",
            }
        ]
    result = make_result("result_sanity", stage, checks, failures)
    print_result(result, as_json=args.json)

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
