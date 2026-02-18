#!/usr/bin/env python3
"""Cross-artifact consistency verifier for iteration-level handoff contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = REPO_ROOT / ".autolab" / "state.json"
EXPERIMENT_TYPES = ("plan", "in_progress", "done")
DEFAULT_EXPERIMENT_TYPE = "plan"
SYNC_SUCCESS_STATUSES = {"ok", "completed", "success", "passed"}


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return payload


def _load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required for consistency checks")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping")
    return payload


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        raise RuntimeError(f"missing state file: {STATE_PATH}")
    return _load_json(STATE_PATH)


def _resolve_iteration_dir(iteration_id: str) -> Path:
    normalized = str(iteration_id).strip()
    if not normalized:
        raise RuntimeError("state.iteration_id is required")
    experiments_root = REPO_ROOT / "experiments"
    for experiment_type in EXPERIMENT_TYPES:
        candidate = experiments_root / experiment_type / normalized
        if candidate.exists():
            return candidate
    return experiments_root / DEFAULT_EXPERIMENT_TYPE / normalized


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return _load_json(path)
    except Exception:
        return None


def _read_optional_yaml(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return _load_yaml(path)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=None, help="Optional stage override for gating behavior")
    parser.add_argument("--json", action="store_true", default=False, help="Output machine-readable JSON envelope")
    args = parser.parse_args()

    try:
        state = _load_state()
    except Exception as exc:
        if args.json:
            envelope = {
                "status": "fail",
                "verifier": "consistency_checks",
                "stage": str(args.stage or ""),
                "checks": [],
                "errors": [str(exc)],
            }
            print(json.dumps(envelope))
        else:
            print(f"consistency_checks: ERROR {exc}")
        return 1

    stage = str(args.stage or state.get("stage", "")).strip()
    iteration_dir = _resolve_iteration_dir(str(state.get("iteration_id", "")))
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

    passed = not failures
    if args.json:
        checks = [{"name": issue, "status": "fail", "detail": issue} for issue in failures]
        if passed:
            checks = [
                {
                    "name": "consistency_checks",
                    "status": "pass",
                    "detail": "cross-artifact consistency checks passed",
                }
            ]
        envelope = {
            "status": "pass" if passed else "fail",
            "verifier": "consistency_checks",
            "stage": stage,
            "checks": checks,
            "errors": failures,
        }
        print(json.dumps(envelope))
    else:
        if passed:
            print("consistency_checks: PASS")
        else:
            print("consistency_checks: FAIL")
            for issue in failures:
                print(issue)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
