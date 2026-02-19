#!/usr/bin/env python3
"""Verifier for documentation target updates."""

from __future__ import annotations

import argparse
import math
import re
from pathlib import Path

from verifier_lib import (
    REPO_ROOT,
    load_json,
    load_state,
    make_result,
    print_result,
    resolve_iteration_dir,
)

PLACEHOLDER_PATTERNS = (
    re.compile(r"\{\{[^{}\n]+\}\}"),
    re.compile(r"<[^>\n]+>"),
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bTBD\b", re.IGNORECASE),
    re.compile(r"\bFIXME\b", re.IGNORECASE),
    re.compile(r"\bplaceholder\b", re.IGNORECASE),
)


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
        raise RuntimeError(f"{path} is missing")
    return path.read_text(encoding="utf-8")


def _contains_placeholder_text(text: str) -> bool:
    return any(pattern.search(text) for pattern in PLACEHOLDER_PATTERNS)


def _iter_reference(iteration_id: str, run_id: str, targets: list[Path]) -> bool:
    for path in targets:
        try:
            text = _load_text(path).lower()
        except Exception:
            continue
        if iteration_id.lower() in text:
            return True
        if run_id and run_id.lower() in text:
            return True
    return False


def _load_metrics(iteration_dir: Path, run_id: str) -> dict:
    """Attempt to load metrics.json for the given run."""
    if not run_id:
        return {}
    metrics_path = iteration_dir / "runs" / run_id / "metrics.json"
    if not metrics_path.exists():
        return {}
    try:
        return load_json(metrics_path)
    except Exception:
        return {}


def _coerce_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except Exception:
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _contains_numeric_match(
    text: str,
    expected: float,
    *,
    rel_tol: float = 0.005,
    abs_tol: float = 0.001,
) -> bool:
    candidates = re.findall(r"[-+]?\d+(?:\.\d+)?", text)
    for raw in candidates:
        parsed = _coerce_float(raw)
        if parsed is None:
            continue
        if math.isclose(parsed, expected, rel_tol=rel_tol, abs_tol=abs_tol):
            return True
    return False


def _extract_primary_metric(metrics: dict) -> tuple[str, float | None, float | None]:
    primary_metric = metrics.get("primary_metric")
    if not isinstance(primary_metric, dict):
        return ("", None, None)
    metric_name = str(primary_metric.get("name", "")).strip()
    metric_value = _coerce_float(primary_metric.get("value"))
    metric_delta = _coerce_float(primary_metric.get("delta_vs_baseline"))
    return (metric_name, metric_value, metric_delta)


def _validate_primary_metric_mentions(
    *,
    docs_text: str,
    docs_path: Path,
    metrics: dict,
) -> list[str]:
    if not metrics:
        return []
    metric_name, metric_value, metric_delta = _extract_primary_metric(metrics)
    if not metric_name:
        return []

    failures: list[str] = []
    lowered = docs_text.lower()
    if metric_name.lower() not in lowered:
        failures.append(
            f"{docs_path} must mention primary metric name '{metric_name}' from metrics.json"
        )
    if metric_value is not None and not _contains_numeric_match(
        docs_text, metric_value
    ):
        failures.append(
            f"{docs_path} must include primary metric value {metric_value} (rounding tolerance allowed)"
        )
    if metric_delta is not None and not _contains_numeric_match(
        docs_text, metric_delta
    ):
        failures.append(
            f"{docs_path} must include primary metric delta_vs_baseline {metric_delta} (rounding tolerance allowed)"
        )
    return failures


def _require_run_artifact_references(
    text: str, *, iteration_dir: Path, run_id: str
) -> list[str]:
    if not run_id:
        return []
    try:
        iteration_rel = iteration_dir.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        iteration_rel = iteration_dir.as_posix()

    metrics_rel = f"{iteration_rel}/runs/{run_id}/metrics.json"
    manifest_rel = f"{iteration_rel}/runs/{run_id}/run_manifest.json"
    failures: list[str] = []
    if metrics_rel not in text:
        failures.append(
            f"docs_update.md must reference exact metrics artifact path '{metrics_rel}'"
        )
    if manifest_rel not in text:
        failures.append(
            f"docs_update.md must reference exact manifest artifact path '{manifest_rel}'"
        )
    return failures


def _validate_docs_update(
    docs_update_path: Path,
    *,
    iteration_dir: Path,
    iteration_id: str,
    run_id: str,
    targets: list[Path],
    metrics: dict,
) -> list[str]:
    failures: list[str] = []
    text = _load_text(docs_update_path)
    lowered = text.lower()

    if _contains_placeholder_text(text):
        failures.append(f"{docs_update_path} contains placeholder text")
    if not lowered.strip():
        failures.append(f"{docs_update_path} is empty")

    if not targets:
        if "no target configured" in lowered or "no targets configured" in lowered:
            return failures
        failures.append(
            "state.paper_targets is not configured; docs_update.md must include explicit"
            " 'No target configured' rationale."
        )
        return failures

    if "no changes needed" in lowered:
        return failures

    if not _iter_reference(iteration_id, run_id, targets):
        failures.append(
            "paper targets do not contain this iteration or run reference and no no-change rationale is provided"
        )

    failures.extend(
        _validate_primary_metric_mentions(
            docs_text=text,
            docs_path=docs_update_path,
            metrics=metrics,
        )
    )
    failures.extend(
        _require_run_artifact_references(
            text,
            iteration_dir=iteration_dir,
            run_id=run_id,
        )
    )

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON envelope",
    )
    args = parser.parse_args()

    try:
        state = load_state()
    except Exception as exc:
        result = make_result("docs_targets", "", [], [str(exc)])
        print_result(result, as_json=args.json)
        return 1

    stage = str(state.get("stage", "")).strip()
    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    targets = _iter_targets(state.get("paper_targets"))

    if stage != "update_docs":
        result = make_result(
            "docs_targets",
            stage,
            [
                {
                    "name": "docs_targets",
                    "status": "pass",
                    "detail": f"skipped for stage={stage}",
                }
            ],
            [],
        )
        print_result(result, as_json=args.json)
        return 0

    iteration_dir = resolve_iteration_dir(iteration_id)
    docs_update_path = iteration_dir / "docs_update.md"
    metrics = _load_metrics(iteration_dir, run_id)

    failures: list[str] = []
    for path in targets:
        if not path.exists():
            failures.append(f"{path} is missing")

    if not failures:
        failures = _validate_docs_update(
            docs_update_path,
            iteration_dir=iteration_dir,
            iteration_id=iteration_id,
            run_id=run_id,
            targets=targets,
            metrics=metrics,
        )

    passed = not failures

    checks = [{"name": f, "status": "fail", "detail": f} for f in failures]
    if passed:
        checks = [
            {
                "name": "docs_targets",
                "status": "pass",
                "detail": "all docs target checks passed",
            }
        ]
    result = make_result("docs_targets", stage, checks, failures)
    print_result(result, as_json=args.json)

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
