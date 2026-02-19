#!/usr/bin/env python3
"""Verifier for documentation-metric drift detection.

Responsibility: Reads metrics.json and scans docs_update.md / paper targets
for matching primary metric values. Fails if key numbers are absent or
contradictory (with rounding tolerance).
"""

from __future__ import annotations

import pathlib
import sys

_VERIFIER_DIR = pathlib.Path(__file__).resolve().parent
if str(_VERIFIER_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFIER_DIR))

import argparse
import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from verifier_lib import (
    REPO_ROOT,
    load_state,
    make_result,
    print_result,
    resolve_iteration_dir,
)

TOLERANCE = 0.05


def _iter_targets(paper_targets: object) -> list[Path]:
    if isinstance(paper_targets, str):
        value = paper_targets.strip()
        return [REPO_ROOT / value] if value else []
    if isinstance(paper_targets, list):
        paths: list[Path] = []
        for item in paper_targets:
            if isinstance(item, str):
                value = item.strip()
                if value:
                    paths.append(REPO_ROOT / value)
        return paths
    return []


def _load_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _number_representations(value: float) -> list[str]:
    """Generate string representations of a number for matching purposes."""
    representations: list[str] = []
    # Integer representation (e.g., 90)
    if value == int(value):
        representations.append(str(int(value)))
    # One decimal (e.g., 90.0)
    representations.append(f"{value:.1f}")
    # Two decimals (e.g., 90.00)
    representations.append(f"{value:.2f}")
    # Three decimals (e.g., 90.000)
    representations.append(f"{value:.3f}")
    # General float repr
    plain = str(value)
    if plain not in representations:
        representations.append(plain)
    return representations


def _number_appears_in_text(value: float, text: str) -> bool:
    """Check whether a numeric value (within tolerance) appears in text."""
    for rep in _number_representations(value):
        if rep in text:
            return True
    # Also check rounded variants within tolerance
    for offset in (-TOLERANCE, TOLERANCE):
        rounded = value + offset
        for rep in _number_representations(rounded):
            if rep in text:
                return True
    return False


# Pattern to find numbers in text (integers and decimals)
_NUMBER_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")


def _find_contradictory_mention(
    metric_name: str,
    expected_value: float,
    text: str,
) -> str | None:
    """Check if the metric name is mentioned near a contradictory number.

    Returns a description of the contradiction, or None if no contradiction found.
    """
    lowered_name = metric_name.lower()
    lowered_text = text.lower()

    # Find all positions where the metric name appears
    start = 0
    while True:
        idx = lowered_text.find(lowered_name, start)
        if idx == -1:
            break
        # Look in a window around the metric name mention (200 chars after)
        window_start = idx
        window_end = min(len(text), idx + len(metric_name) + 200)
        window = text[window_start:window_end]

        for match in _NUMBER_PATTERN.finditer(window):
            try:
                found_value = float(match.group(0))
            except ValueError:
                continue
            # Skip if value is close to expected (within tolerance)
            if abs(found_value - expected_value) <= TOLERANCE:
                continue
            # Skip trivially small numbers that are likely unrelated (e.g., indices)
            if abs(found_value) < 1.0 and abs(expected_value) >= 1.0:
                continue
            # Found a number near the metric name that differs from expected
            return (
                f"metric '{metric_name}' mentioned with value {found_value} "
                f"near position {idx}, expected ~{expected_value}"
            )
        start = idx + 1

    return None


def _check_drift(
    metrics: dict[str, Any],
    docs_text: str,
    target_texts: list[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run drift checks between metrics and documentation texts.

    Returns (checks, errors) where checks is a list of check dicts and
    errors is a list of failure descriptions.
    """
    checks: list[dict[str, Any]] = []
    errors: list[str] = []

    primary_metric = metrics.get("primary_metric")
    if not isinstance(primary_metric, dict):
        errors.append("metrics.json primary_metric is not a mapping")
        checks.append({"name": "primary_metric_structure", "passed": False})
        return checks, errors

    metric_name = str(primary_metric.get("name", "")).strip()
    metric_value = primary_metric.get("value")
    metric_delta = primary_metric.get("delta_vs_baseline")

    if not metric_name:
        errors.append("metrics.json primary_metric.name is empty")
        checks.append({"name": "primary_metric_name", "passed": False})
        return checks, errors

    # Combine all doc texts for searching
    all_texts = [docs_text] + target_texts

    # Check 1: primary metric value appears in docs_update.md
    if metric_value is not None:
        try:
            value_float = float(metric_value)
        except (TypeError, ValueError):
            value_float = None

        if value_float is not None:
            found_in_docs = _number_appears_in_text(value_float, docs_text)
            checks.append(
                {
                    "name": "value_in_docs_update",
                    "passed": found_in_docs,
                    "metric_name": metric_name,
                    "expected_value": value_float,
                }
            )
            if not found_in_docs:
                errors.append(
                    f"docs_update.md does not reference primary metric "
                    f"'{metric_name}' value {value_float}"
                )

    # Check 2: delta_vs_baseline appears in docs_update.md (informational, not a hard fail)
    if metric_delta is not None:
        try:
            delta_float = float(metric_delta)
        except (TypeError, ValueError):
            delta_float = None

        if delta_float is not None:
            found_delta = _number_appears_in_text(delta_float, docs_text)
            checks.append(
                {
                    "name": "delta_in_docs_update",
                    "passed": found_delta,
                    "metric_name": metric_name,
                    "expected_delta": delta_float,
                }
            )
            # Delta missing is a soft signal, not a hard error

    # Check 3: contradictory numbers in all doc texts
    if metric_value is not None:
        try:
            value_float = float(metric_value)
        except (TypeError, ValueError):
            value_float = None

        if value_float is not None:
            for i, doc_text in enumerate(all_texts):
                if not doc_text.strip():
                    continue
                label = "docs_update.md" if i == 0 else f"paper_target[{i - 1}]"
                contradiction = _find_contradictory_mention(
                    metric_name, value_float, doc_text
                )
                passed = contradiction is None
                checks.append(
                    {
                        "name": f"no_contradiction_{label}",
                        "passed": passed,
                    }
                )
                if not passed:
                    errors.append(f"contradictory value in {label}: {contradiction}")

    return checks, errors


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=None, help="Override current stage")
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON envelope",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        state = load_state()
    except Exception as exc:
        result = make_result("docs_drift", "", [], [str(exc)])
        print_result(result, as_json=args.json)
        return 1

    stage = args.stage or str(state.get("stage", "")).strip()

    # Only run for update_docs stage
    if stage != "update_docs":
        result = make_result("docs_drift", stage, [], [])
        print_result(result, as_json=args.json)
        return 0

    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    paper_targets = state.get("paper_targets")

    if not iteration_id or not run_id:
        msg = "missing iteration_id or last_run_id in state"
        result = make_result("docs_drift", stage, [], [msg])
        print_result(result, as_json=args.json)
        return 1

    iteration_dir = resolve_iteration_dir(iteration_id)
    metrics_path = iteration_dir / "runs" / run_id / "metrics.json"

    if not metrics_path.exists():
        msg = f"metrics.json not found at {metrics_path}"
        result = make_result("docs_drift", stage, [], [msg])
        print_result(result, as_json=args.json)
        return 1

    try:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except Exception as exc:
        msg = f"metrics.json is not valid JSON: {exc}"
        result = make_result("docs_drift", stage, [], [msg])
        print_result(result, as_json=args.json)
        return 1

    if not isinstance(metrics, dict):
        msg = "metrics.json must contain a JSON object"
        result = make_result("docs_drift", stage, [], [msg])
        print_result(result, as_json=args.json)
        return 1

    # Load docs_update.md
    docs_update_path = iteration_dir / "docs_update.md"
    docs_text = _load_text(docs_update_path)

    # Load paper target files
    targets = _iter_targets(paper_targets)
    target_texts: list[str] = []
    for target_path in targets:
        target_texts.append(_load_text(target_path))

    checks, errors = _check_drift(metrics, docs_text, target_texts)

    result = make_result("docs_drift", stage, checks, errors)
    print_result(result, as_json=args.json)

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
