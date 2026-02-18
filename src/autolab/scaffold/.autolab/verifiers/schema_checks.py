#!/usr/bin/env python3
"""Schema-oriented artifact validation for autolab stage handoffs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, List

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / ".autolab" / "state.json"
SCHEMA_DIR = REPO_ROOT / ".autolab" / "schemas"
SCHEMAS: dict[str, str] = {
    "design": "design.schema.json",
    "agent_result": "agent_result.schema.json",
    "review_result": "review_result.schema.json",
    "run_manifest": "run_manifest.schema.json",
    "metrics": "metrics.schema.json",
}

DESIGN_REQUIRED_CHECKS = {"tests", "dry_run", "schema", "env_smoke", "docs_target_update"}


def _load_json(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return data


def _load_yaml(path: Path) -> dict:
    if yaml is None:
        raise RuntimeError("PyYAML is not available")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping")
    return payload


def _load_state() -> dict:
    return _load_json(STATE_FILE)


def _load_schema(schema_key: str) -> dict:
    path = SCHEMA_DIR / SCHEMAS[schema_key]
    return _load_json(path)


def _require_keys(payload: dict, *, required: Iterable[str], path: str, label: str) -> list[str]:
    missing = sorted(set(required) - set(payload.keys()))
    if not missing:
        return []
    return [f"{path} {label} missing required keys: {missing}"]


def _validate_agent_result() -> list[str]:
    path = REPO_ROOT / ".autolab" / "agent_result.json"
    failures: list[str] = []
    try:
        payload = _load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]

    failures.extend(_require_keys(payload, required={"status", "summary", "changed_files", "completion_token_seen"}, path=str(path), label=""))
    schema = _load_schema("agent_result")
    required = schema.get("required", [])
    failures.extend(_require_keys(payload, required=required, path=str(path), label="schema"))
    if payload.get("status") not in {"complete", "needs_retry", "failed"}:
        failures.append(f"{path} status must be one of complete|needs_retry|failed")
    return failures


def _validate_design(state: dict) -> list[str]:
    stage = str(state.get("stage", "")).strip()
    if stage not in {"design", "implementation", "implementation_review", "launch", "extract_results", "update_docs", "hypothesis"}:
        return []

    iteration_id = str(state.get("iteration_id", "")).strip()
    iteration_dir = REPO_ROOT / "experiments" / iteration_id
    design_path = iteration_dir / "design.yaml"
    failures: list[str] = []

    if not design_path.exists():
        return [f"{design_path} is missing"]

    try:
        payload = _load_yaml(design_path)
    except Exception as exc:
        return [f"{design_path} is invalid YAML: {exc}"]

    schema = _load_schema("design")
    failures.extend(_require_keys(payload, required=set(schema.get("required", [])), path=str(design_path), label="schema"))
    if payload.get("iteration_id") not in {"", None} and str(payload.get("iteration_id")) != iteration_id:
        failures.append(f"{design_path} iteration_id mismatch with state")

    if not isinstance(payload.get("id"), str) or not payload["id"]:
        failures.append(f"{design_path} id must be a non-empty string")
    if not isinstance(payload.get("hypothesis_id"), str) or not payload["hypothesis_id"]:
        failures.append(f"{design_path} hypothesis_id must be a non-empty string")

    entrypoint = payload.get("entrypoint")
    if not isinstance(entrypoint, dict) or not isinstance(entrypoint.get("module"), str) or not entrypoint.get("module"):
        failures.append(f"{design_path} entrypoint.module must be a non-empty string")

    compute = payload.get("compute")
    if not isinstance(compute, dict) or not compute.get("location"):
        failures.append(f"{design_path} compute.location is required")

    metrics = payload.get("metrics")
    if not isinstance(metrics, dict):
        failures.append(f"{design_path} metrics must be a mapping")
    else:
        if "primary" not in metrics:
            failures.append(f"{design_path} metrics.primary is required")
        if "success_delta" not in metrics:
            failures.append(f"{design_path} metrics.success_delta is required")
        if "aggregation" not in metrics:
            failures.append(f"{design_path} metrics.aggregation is required")

    if not isinstance(payload.get("baselines"), list) or not payload.get("baselines"):
        failures.append(f"{design_path} baselines must be a non-empty list")

    return failures


def _latest_run_dir(state: dict) -> Path | None:
    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    if not iteration_id or not run_id:
        return None
    return REPO_ROOT / "experiments" / iteration_id / "runs" / run_id


def _validate_review_result(state: dict) -> list[str]:
    stage = str(state.get("stage", "")).strip()
    if stage not in {"implementation_review", "launch", "extract_results", "update_docs"}:
        return []
    path = REPO_ROOT / "experiments" / str(state.get("iteration_id", "")).strip() / "review_result.json"
    if not path.exists():
        return [f"{path} is missing"]

    try:
        payload = _load_json(path)
    except Exception as exc:
        return [f"{path} is invalid JSON: {exc}"]

    schema = _load_schema("review_result")
    failures: list[str] = []
    failures.extend(_require_keys(payload, required=set(schema.get("required", [])), path=str(path), label="schema"))
    if payload.get("status") not in {"pass", "needs_retry", "failed"}:
        failures.append(f"{path} status must be one of pass|needs_retry|failed")

    required_checks = payload.get("required_checks")
    if not isinstance(required_checks, dict):
        return failures + [f"{path} required_checks must be a mapping"]

    for check in DESIGN_REQUIRED_CHECKS:
        if check not in required_checks:
            failures.append(f"{path} required_checks is missing '{check}'")
        elif not isinstance(required_checks.get(check), str):
            failures.append(f"{path} required_checks['{check}'] must be a string status")
    return failures


def _validate_run_manifest(state: dict) -> list[str]:
    stage = str(state.get("stage", "")).strip()
    if stage not in {"launch", "extract_results", "update_docs"}:
        return []

    run_dir = _latest_run_dir(state)
    if not run_dir:
        return [".autolab/state.json missing last_run_id for run-manifest validation"]
    manifest_path = run_dir / "run_manifest.json"
    if not manifest_path.exists():
        return [f"{manifest_path} is missing"]

    try:
        payload = _load_json(manifest_path)
    except Exception as exc:
        return [f"{manifest_path} is invalid JSON: {exc}"]

    failures: list[str] = []
    schema = _load_schema("run_manifest")
    failures.extend(_require_keys(payload, required=set(schema.get("required", [])), path=str(manifest_path), label="schema"))
    if str(payload.get("iteration_id", "")).strip() and str(payload.get("iteration_id")).strip() != str(
        state.get("iteration_id", "")
    ):
        failures.append(f"{manifest_path} iteration_id mismatch")
    if str(payload.get("run_id", "")).strip() and str(payload.get("run_id")) != str(state.get("last_run_id", "")):
        failures.append(f"{manifest_path} run_id mismatch")

    artifact_sync = payload.get("artifact_sync_to_local")
    if not isinstance(artifact_sync, dict):
        failures.append(f"{manifest_path} artifact_sync_to_local must be a mapping")
    else:
        status = str(artifact_sync.get("status", "")).strip().lower()
        if status not in {"ok", "completed", "success", "pending", "running", ""}:
            failures.append(f"{manifest_path} artifact_sync_to_local.status is invalid")

    return failures


def _validate_metrics(state: dict) -> list[str]:
    stage = str(state.get("stage", "")).strip()
    if stage != "extract_results":
        return []

    run_dir = _latest_run_dir(state)
    if not run_dir:
        return [".autolab/state.json missing last_run_id for metrics validation"]
    path = run_dir / "metrics.json"
    if not path.exists():
        return [f"{path} is missing"]

    try:
        payload = _load_json(path)
    except Exception as exc:
        return [f"{path} is invalid JSON: {exc}"]

    failures: list[str] = []
    failures.extend(_require_keys(payload, required={"iteration_id", "run_id", "status"}, path=str(path), label="required"))
    if payload.get("status") not in {"completed", "partial", "failed"}:
        failures.append(f"{path} status must be one of completed|partial|failed")

    if str(payload.get("iteration_id", "")).strip() and str(payload.get("iteration_id")) != str(state.get("iteration_id", "")):
        failures.append(f"{path} iteration_id mismatch")
    if str(payload.get("run_id", "")).strip() and str(payload.get("run_id")) != str(state.get("last_run_id", "")):
        failures.append(f"{path} run_id mismatch")

    primary_metric = payload.get("primary_metric")
    if not isinstance(primary_metric, dict):
        failures.append(f"{path} primary_metric must be a mapping")
    else:
        for key in {"name", "value", "delta_vs_baseline"}:
            if key not in primary_metric:
                failures.append(f"{path} primary_metric missing required field '{key}'")
    return failures


def main() -> int:
    failures: list[str] = []
    try:
        state = _load_state()
    except Exception as exc:
        print(f"schema_checks: ERROR {exc}")
        return 1

    failures.extend(_validate_design(state))
    failures.extend(_validate_agent_result())
    failures.extend(_validate_review_result(state))
    failures.extend(_validate_run_manifest(state))
    failures.extend(_validate_metrics(state))

    if failures:
        print("schema_checks: FAIL")
        for reason in failures:
            print(reason)
        return 1

    print("schema_checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
