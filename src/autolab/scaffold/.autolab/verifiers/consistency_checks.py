#!/usr/bin/env python3
"""Cross-artifact consistency verifier for iteration-level handoff contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from invariant_checks import (
    check_design_manifest_host_mode,
    check_manifest_sync_status,
    check_metric_name_match,
    check_state_run_scoped_fields,
)
from verifier_lib import (
    RUN_MANIFEST_STATUSES,
    load_json,
    load_yaml,
    load_state,
    make_result,
    print_result,
    resolve_iteration_dir,
)


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return load_json(path)
    except Exception:
        return None


def _read_optional_yaml(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return load_yaml(path)
    except Exception:
        return None


def _check_review_gate(iteration_dir: Path, *, stage: str) -> list[str]:
    if stage not in {"launch", "extract_results", "update_docs", "decide_repeat"}:
        return []
    review_path = iteration_dir / "review_result.json"
    review_payload = _read_optional_json(review_path)
    if review_payload is None:
        return [
            f"{review_path} is missing or invalid; review_result.status=pass is required before launch"
        ]
    review_status = str(review_payload.get("status", "")).strip().lower()
    if review_status != "pass":
        return [
            f"{review_path} status must be 'pass' before launch/extract/docs, got '{review_status or '<missing>'}'"
        ]
    return []


def _check_design_manifest_consistency(
    design_payload: dict[str, Any] | None,
    manifest_payload: dict[str, Any] | None,
) -> list[str]:
    return check_design_manifest_host_mode(design_payload, manifest_payload)


def _check_metric_name_consistency(
    design_payload: dict[str, Any] | None,
    metrics_payload: dict[str, Any] | None,
) -> list[str]:
    return check_metric_name_match(design_payload, metrics_payload)


def _check_run_scoped_fields(
    *,
    stage: str,
    state: dict[str, Any],
    manifest_payload: dict[str, Any] | None,
    metrics_payload: dict[str, Any] | None,
) -> list[str]:
    failures = check_state_run_scoped_fields(
        state=state,
        manifest_payload=manifest_payload,
        metrics_payload=metrics_payload,
    )
    require_sync_success = stage in {"extract_results", "update_docs", "decide_repeat"}
    failures.extend(
        check_manifest_sync_status(
            manifest_payload,
            require_success=require_sync_success,
            context=stage,
        )
    )
    return failures


def _check_manifest_status_canonical(
    manifest_payload: dict[str, Any] | None,
) -> list[str]:
    """Verify run_manifest.status is in the canonical enum."""
    if not isinstance(manifest_payload, dict):
        return []
    raw_status = str(manifest_payload.get("status", "")).strip().lower()
    if raw_status and raw_status not in RUN_MANIFEST_STATUSES:
        return [
            f"run_manifest.status='{raw_status}' is not a canonical status "
            f"(expected one of: {', '.join(sorted(RUN_MANIFEST_STATUSES))})"
        ]
    return []


def _check_decision_evidence_pointers(iteration_dir: Path) -> list[str]:
    """Verify decision_result.json evidence pointers reference existing files."""
    decision_path = iteration_dir / "decision_result.json"
    decision_payload = _read_optional_json(decision_path)
    if not isinstance(decision_payload, dict):
        return []
    evidence = decision_payload.get("evidence")
    if not isinstance(evidence, list):
        return []
    failures: list[str] = []
    for entry in evidence:
        if not isinstance(entry, dict):
            continue
        pointer = str(entry.get("pointer", "")).strip()
        if not pointer:
            continue
        # Skip pattern paths and absolute paths
        if "<" in pointer or "{{" in pointer:
            continue
        candidate = iteration_dir / pointer
        if not candidate.exists():
            # Also try from repo root
            from verifier_lib import REPO_ROOT

            alt = REPO_ROOT / pointer
            if not alt.exists():
                failures.append(
                    f"decision_result.json evidence pointer '{pointer}' does not reference an existing file"
                )
    return failures


def _check_iteration_id_chain(iteration_dir: Path, run_id: str) -> list[str]:
    """Check iteration_id consistency across all artifacts in the iteration dir."""
    failures: list[str] = []
    iteration_id_from_dir = iteration_dir.name

    artifacts: list[tuple[str, dict[str, Any] | None]] = []
    design = _read_optional_yaml(iteration_dir / "design.yaml")
    if design is not None:
        artifacts.append(("design.yaml", design))
    review = _read_optional_json(iteration_dir / "review_result.json")
    if review is not None:
        artifacts.append(("review_result.json", review))
    decision = _read_optional_json(iteration_dir / "decision_result.json")
    if decision is not None:
        artifacts.append(("decision_result.json", decision))
    if run_id:
        manifest = _read_optional_json(
            iteration_dir / "runs" / run_id / "run_manifest.json"
        )
        if manifest is not None:
            artifacts.append((f"runs/{run_id}/run_manifest.json", manifest))
        metrics = _read_optional_json(iteration_dir / "runs" / run_id / "metrics.json")
        if metrics is not None:
            artifacts.append((f"runs/{run_id}/metrics.json", metrics))

    for name, payload in artifacts:
        if not isinstance(payload, dict):
            continue
        art_iter_id = str(payload.get("iteration_id", "")).strip()
        if art_iter_id and art_iter_id != iteration_id_from_dir:
            failures.append(
                f"{name} iteration_id='{art_iter_id}' does not match iteration directory name '{iteration_id_from_dir}'"
            )
    return failures


def _normalized_run_group(state: dict[str, Any]) -> list[str]:
    raw_group = state.get("run_group", [])
    if not isinstance(raw_group, list):
        return []
    normalized: list[str] = []
    for raw_run_id in raw_group:
        run_id = str(raw_run_id).strip()
        if run_id and run_id not in normalized:
            normalized.append(run_id)
    return normalized


def _check_replicate_manifests(iteration_dir: Path, state: dict[str, Any]) -> list[str]:
    run_group = _normalized_run_group(state)
    if len(run_group) <= 1:
        return []

    failures: list[str] = []
    iteration_id = str(state.get("iteration_id", "")).strip()
    for run_id in run_group:
        manifest_path = iteration_dir / "runs" / run_id / "run_manifest.json"
        payload = _read_optional_json(manifest_path)
        if not isinstance(payload, dict):
            failures.append(
                f"{manifest_path} is required for multi-run replicate manifests"
            )
            continue
        manifest_run_id = str(payload.get("run_id", "")).strip()
        if manifest_run_id and manifest_run_id != run_id:
            failures.append(
                f"{manifest_path} run_id='{manifest_run_id}' must match replicate id '{run_id}'"
            )
        manifest_iteration_id = str(payload.get("iteration_id", "")).strip()
        if (
            iteration_id
            and manifest_iteration_id
            and manifest_iteration_id != iteration_id
        ):
            failures.append(
                f"{manifest_path} iteration_id='{manifest_iteration_id}' does not match state.iteration_id '{iteration_id}'"
            )
    return failures


def _check_replicate_metrics_contract(
    iteration_dir: Path, state: dict[str, Any], *, stage: str
) -> list[str]:
    if stage not in {"extract_results", "update_docs", "decide_repeat"}:
        return []

    run_group = _normalized_run_group(state)
    if len(run_group) <= 1:
        return []

    failures: list[str] = []
    for run_id in run_group:
        metrics_path = iteration_dir / "runs" / run_id / "metrics.json"
        payload = _read_optional_json(metrics_path)
        if not isinstance(payload, dict):
            failures.append(
                f"{metrics_path} is required for each replicate in multi-run mode"
            )

    base_run_id = str(state.get("last_run_id", "")).strip()
    if not base_run_id:
        failures.append(
            "state.last_run_id is required to validate aggregated multi-run metrics"
        )
        return failures

    aggregate_metrics_path = iteration_dir / "runs" / base_run_id / "metrics.json"
    aggregate_payload = _read_optional_json(aggregate_metrics_path)
    if not isinstance(aggregate_payload, dict):
        failures.append(
            f"{aggregate_metrics_path} is required as aggregate metrics artifact for multi-run mode"
        )
        return failures

    per_run_metrics = aggregate_payload.get("per_run_metrics")
    if not isinstance(per_run_metrics, list):
        failures.append(
            f"{aggregate_metrics_path} must include per_run_metrics array for multi-run mode"
        )
    else:
        seen_ids = {
            str(entry.get("run_id", "")).strip()
            for entry in per_run_metrics
            if isinstance(entry, dict)
        }
        missing = [run_id for run_id in run_group if run_id not in seen_ids]
        if missing:
            failures.append(
                f"{aggregate_metrics_path} per_run_metrics is missing replicate run_id(s): {', '.join(missing)}"
            )

    aggregation_method = str(aggregate_payload.get("aggregation_method", "")).strip()
    if not aggregation_method:
        failures.append(
            f"{aggregate_metrics_path} must include aggregation_method for multi-run mode"
        )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", default=None, help="Optional stage override for gating behavior"
    )
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
        result = make_result(
            "consistency_checks", str(args.stage or ""), [], [str(exc)]
        )
        print_result(result, as_json=args.json)
        return 1

    stage = str(args.stage or state.get("stage", "")).strip()
    iteration_dir = resolve_iteration_dir(str(state.get("iteration_id", "")))
    run_id = str(state.get("last_run_id", "")).strip()

    design_payload = _read_optional_yaml(iteration_dir / "design.yaml")
    manifest_payload = (
        _read_optional_json(iteration_dir / "runs" / run_id / "run_manifest.json")
        if run_id
        else None
    )
    metrics_payload = (
        _read_optional_json(iteration_dir / "runs" / run_id / "metrics.json")
        if run_id
        else None
    )

    failures: list[str] = []
    failures.extend(_check_review_gate(iteration_dir, stage=stage))
    failures.extend(
        _check_design_manifest_consistency(design_payload, manifest_payload)
    )
    failures.extend(_check_metric_name_consistency(design_payload, metrics_payload))
    failures.extend(
        _check_run_scoped_fields(
            stage=stage,
            state=state,
            manifest_payload=manifest_payload,
            metrics_payload=metrics_payload,
        )
    )
    failures.extend(_check_manifest_status_canonical(manifest_payload))
    failures.extend(_check_decision_evidence_pointers(iteration_dir))
    failures.extend(_check_iteration_id_chain(iteration_dir, run_id))
    failures.extend(_check_replicate_manifests(iteration_dir, state))
    failures.extend(
        _check_replicate_metrics_contract(iteration_dir, state, stage=stage)
    )

    checks = [{"name": issue, "status": "fail", "detail": issue} for issue in failures]
    if not failures:
        checks = [
            {
                "name": "consistency_checks",
                "status": "pass",
                "detail": "cross-artifact consistency checks passed",
            }
        ]
    result = make_result("consistency_checks", stage, checks, failures)
    print_result(result, as_json=args.json)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
