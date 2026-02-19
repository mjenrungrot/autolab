#!/usr/bin/env python3
"""Verifier for documentation target updates."""

from __future__ import annotations

import argparse
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


def _has_concrete_metric_value(text: str, metrics: dict) -> bool:
    """Check that docs_update.md contains at least one concrete metric value from metrics.json."""
    if not metrics:
        return True  # No metrics to validate against
    for key, value in metrics.items():
        if isinstance(value, (int, float)) and str(value) in text:
            return True
        if isinstance(value, str) and value.strip() and value.strip() in text:
            return True
    return False


RUN_EVIDENCE_PATTERNS = (
    re.compile(r"run[_ ]evidence", re.IGNORECASE),
    re.compile(r"runs/[A-Za-z0-9_-]+/", re.IGNORECASE),
    re.compile(r"metrics\.json", re.IGNORECASE),
)


def _has_run_evidence_block(text: str) -> bool:
    """Check that docs_update.md contains a Run Evidence block with file paths."""
    return any(pattern.search(text) for pattern in RUN_EVIDENCE_PATTERNS)


def _validate_docs_update(
    docs_update_path: Path,
    *,
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

    # Check for concrete metric values from metrics.json
    if metrics and not _has_concrete_metric_value(text, metrics):
        failures.append(
            f"{docs_update_path} does not contain any concrete metric values from metrics.json"
        )

    # Check for Run Evidence block with file paths
    if run_id and not _has_run_evidence_block(text):
        failures.append(
            f"{docs_update_path} should contain a Run Evidence block referencing run artifact file paths"
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
