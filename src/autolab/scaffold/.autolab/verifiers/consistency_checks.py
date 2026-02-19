#!/usr/bin/env python3
"""Cross-artifact consistency verifier for iteration-level handoff contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from verifier_lib import (
    RUN_MANIFEST_STATUSES,
    SYNC_SUCCESS_STATUSES,
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
        return [f"{review_path} is missing or invalid; review_result.status=pass is required before launch"]
    review_status = str(review_payload.get("status", "")).strip().lower()
    if review_status != "pass":
        return [f"{review_path} status must be 'pass' before launch/extract/docs, got '{review_status or '<missing>'}'"]
    return []


def _check_design_manifest_consistency(
    design_payload: dict[str, Any] | None,
    manifest_payload: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(design_payload, dict) or not isinstance(manifest_payload, dict):
        return []
    failures: list[str] = []
    design_location = (
        str((design_payload.get("compute") or {}).get("location", "")).strip().lower()
        if isinstance(design_payload.get("compute"), dict)
        else ""
    )
    manifest_host_mode = str(
        manifest_payload.get("host_mode")
        or manifest_payload.get("launch_mode")
        or (manifest_payload.get("launch") or {}).get("mode")
    ).strip().lower()
    if design_location and manifest_host_mode and design_location != manifest_host_mode:
        failures.append(
            f"design.compute.location='{design_location}' does not match run_manifest host/launch mode '{manifest_host_mode}'"
        )
    return failures


def _check_metric_name_consistency(
    design_payload: dict[str, Any] | None,
    metrics_payload: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(design_payload, dict) or not isinstance(metrics_payload, dict):
        return []
    failures: list[str] = []
    design_metric_name = ""
    design_metrics = design_payload.get("metrics")
    if isinstance(design_metrics, dict):
        primary = design_metrics.get("primary")
        if isinstance(primary, dict):
            design_metric_name = str(primary.get("name", "")).strip()
    metrics_metric_name = ""
    primary_metric = metrics_payload.get("primary_metric")
    if isinstance(primary_metric, dict):
        metrics_metric_name = str(primary_metric.get("name", "")).strip()
    if design_metric_name and metrics_metric_name and design_metric_name != metrics_metric_name:
        failures.append(
            f"metrics.primary_metric.name='{metrics_metric_name}' does not match design.metrics.primary.name='{design_metric_name}'"
        )
    return failures


def _check_run_scoped_fields(
    *,
    state: dict[str, Any],
    manifest_payload: dict[str, Any] | None,
    metrics_payload: dict[str, Any] | None,
) -> list[str]:
    failures: list[str] = []
    state_iteration_id = str(state.get("iteration_id", "")).strip()
    state_run_id = str(state.get("last_run_id", "")).strip()

    if isinstance(manifest_payload, dict):
        manifest_iteration_id = str(manifest_payload.get("iteration_id", "")).strip()
        if state_iteration_id and manifest_iteration_id and state_iteration_id != manifest_iteration_id:
            failures.append(
                f"run_manifest iteration_id '{manifest_iteration_id}' does not match state.iteration_id '{state_iteration_id}'"
            )
        manifest_run_id = str(manifest_payload.get("run_id", "")).strip()
        if state_run_id and manifest_run_id and state_run_id != manifest_run_id:
            failures.append(
                f"run_manifest run_id '{manifest_run_id}' does not match state.last_run_id '{state_run_id}'"
            )
        sync = manifest_payload.get("artifact_sync_to_local", {})
        if isinstance(sync, dict):
            sync_status = str(sync.get("status", "")).strip().lower()
            if sync_status and sync_status not in SYNC_SUCCESS_STATUSES:
                failures.append(
                    f"run_manifest artifact_sync_to_local.status='{sync_status}' is not success-like"
                )

    if isinstance(metrics_payload, dict):
        metrics_iteration_id = str(metrics_payload.get("iteration_id", "")).strip()
        if state_iteration_id and metrics_iteration_id and state_iteration_id != metrics_iteration_id:
            failures.append(
                f"metrics iteration_id '{metrics_iteration_id}' does not match state.iteration_id '{state_iteration_id}'"
            )
        metrics_run_id = str(metrics_payload.get("run_id", "")).strip()
        if state_run_id and metrics_run_id and state_run_id != metrics_run_id:
            failures.append(
                f"metrics run_id '{metrics_run_id}' does not match state.last_run_id '{state_run_id}'"
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
        manifest = _read_optional_json(iteration_dir / "runs" / run_id / "run_manifest.json")
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=None, help="Optional stage override for gating behavior")
    parser.add_argument("--json", action="store_true", default=False, help="Output machine-readable JSON envelope")
    args = parser.parse_args()

    try:
        state = load_state()
    except Exception as exc:
        result = make_result("consistency_checks", str(args.stage or ""), [], [str(exc)])
        print_result(result, as_json=args.json)
        return 1

    stage = str(args.stage or state.get("stage", "")).strip()
    iteration_dir = resolve_iteration_dir(str(state.get("iteration_id", "")))
    run_id = str(state.get("last_run_id", "")).strip()

    design_payload = _read_optional_yaml(iteration_dir / "design.yaml")
    manifest_payload = _read_optional_json(iteration_dir / "runs" / run_id / "run_manifest.json") if run_id else None
    metrics_payload = _read_optional_json(iteration_dir / "runs" / run_id / "metrics.json") if run_id else None

    failures: list[str] = []
    failures.extend(_check_review_gate(iteration_dir, stage=stage))
    failures.extend(_check_design_manifest_consistency(design_payload, manifest_payload))
    failures.extend(_check_metric_name_consistency(design_payload, metrics_payload))
    failures.extend(
        _check_run_scoped_fields(
            state=state,
            manifest_payload=manifest_payload,
            metrics_payload=metrics_payload,
        )
    )
    failures.extend(_check_manifest_status_canonical(manifest_payload))
    failures.extend(_check_decision_evidence_pointers(iteration_dir))
    failures.extend(_check_iteration_id_chain(iteration_dir, run_id))

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
