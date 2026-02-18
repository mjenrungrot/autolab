#!/usr/bin/env python3
"""Schema-oriented artifact validation for autolab stage handoffs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

try:
    from jsonschema import Draft202012Validator
except Exception:  # pragma: no cover
    Draft202012Validator = None

try:
    from autolab.config import _resolve_stage_requirements as _shared_resolve_stage_requirements
except Exception:  # pragma: no cover
    _shared_resolve_stage_requirements = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / ".autolab" / "state.json"
SCHEMA_DIR = REPO_ROOT / ".autolab" / "schemas"
EXPERIMENT_TYPES = ("plan", "in_progress", "done")
DEFAULT_EXPERIMENT_TYPE = "plan"
SCHEMAS: dict[str, str] = {
    "state": "state.schema.json",
    "backlog": "backlog.schema.json",
    "design": "design.schema.json",
    "agent_result": "agent_result.schema.json",
    "review_result": "review_result.schema.json",
    "run_manifest": "run_manifest.schema.json",
    "metrics": "metrics.schema.json",
}
REVIEW_RESULT_REQUIRED_CHECKS = (
    "tests",
    "dry_run",
    "schema",
    "env_smoke",
    "docs_target_update",
)
REVIEW_RESULT_CHECK_STATUSES = {"pass", "skip", "fail"}
SYNC_SUCCESS_STATUSES = {"ok", "completed", "success", "passed"}


def _load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return payload


def _load_yaml(path: Path) -> dict:
    if yaml is None:
        raise RuntimeError("PyYAML is not available")
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping")
    return payload


def _load_state() -> dict:
    return _load_json(STATE_FILE)


def _load_policy() -> dict:
    policy_path = REPO_ROOT / ".autolab" / "verifier_policy.yaml"
    if not policy_path.exists():
        return {}
    if yaml is None:
        return {}
    try:
        payload = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_schema(schema_key: str) -> dict:
    path = SCHEMA_DIR / SCHEMAS[schema_key]
    return _load_json(path)


def _format_error_path(error_path: Iterable[Any]) -> str:
    pieces = ["$"]
    for part in error_path:
        if isinstance(part, int):
            pieces.append(f"[{part}]")
        else:
            pieces.append(f".{part}")
    return "".join(pieces)


def _schema_validate(payload: Any, *, schema_key: str, path: Path) -> list[str]:
    if Draft202012Validator is None:
        return ["jsonschema dependency is required (install: pip install jsonschema)"]
    schema = _load_schema(schema_key)
    validator = Draft202012Validator(schema)
    failures: list[str] = []
    for error in sorted(validator.iter_errors(payload), key=lambda item: _format_error_path(item.path)):
        location = _format_error_path(error.path)
        failures.append(f"{path} schema violation at {location}: {error.message}")
    return failures


def _stage_requirements(policy: dict[str, Any], stage: str) -> dict[str, bool]:
    if _shared_resolve_stage_requirements is not None:
        try:
            shared = _shared_resolve_stage_requirements(policy, stage)
        except Exception:
            shared = None
        if isinstance(shared, dict):
            return {
                "tests": bool(shared.get("tests", False)),
                "dry_run": bool(shared.get("dry_run", False)),
                "schema": bool(shared.get("schema", False)),
                "env_smoke": bool(shared.get("env_smoke", False)),
                "docs_target_update": bool(shared.get("docs_target_update", False)),
            }

    output: dict[str, bool] = {
        "tests": False,
        "dry_run": False,
        "schema": False,
        "env_smoke": False,
        "docs_target_update": False,
    }
    legacy_map = {
        "tests": "require_tests",
        "dry_run": "require_dry_run",
        "schema": "require_schema",
        "env_smoke": "require_env_smoke",
        "docs_target_update": "require_docs_target_update",
    }
    for key, legacy_key in legacy_map.items():
        if legacy_key in policy:
            output[key] = bool(policy.get(legacy_key))
    requirements_by_stage = policy.get("requirements_by_stage")
    if isinstance(requirements_by_stage, dict):
        stage_section = requirements_by_stage.get(stage)
        if isinstance(stage_section, dict):
            for key in output:
                if key in stage_section:
                    output[key] = bool(stage_section.get(key))
    return output


def _resolve_iteration_dir(iteration_id: str) -> Path:
    normalized_iteration = iteration_id.strip()
    experiments_root = REPO_ROOT / "experiments"
    candidates = [experiments_root / experiment_type / normalized_iteration for experiment_type in EXPERIMENT_TYPES]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return experiments_root / DEFAULT_EXPERIMENT_TYPE / normalized_iteration


def _iteration_dir(state: dict[str, Any]) -> Path:
    iteration_id = str(state.get("iteration_id", "")).strip()
    return _resolve_iteration_dir(iteration_id)


def _latest_run_dir(state: dict[str, Any]) -> Path | None:
    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    if not iteration_id or not run_id:
        return None
    return _resolve_iteration_dir(iteration_id) / "runs" / run_id


def _validate_state_schema() -> list[str]:
    try:
        payload = _load_json(STATE_FILE)
    except Exception as exc:
        return [f"{STATE_FILE} {exc}"]
    return _schema_validate(payload, schema_key="state", path=STATE_FILE)


def _validate_backlog_schema() -> list[str]:
    path = REPO_ROOT / ".autolab" / "backlog.yaml"
    try:
        payload = _load_yaml(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    return _schema_validate(payload, schema_key="backlog", path=path)


def _validate_agent_result() -> list[str]:
    path = REPO_ROOT / ".autolab" / "agent_result.json"
    try:
        payload = _load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    failures = _schema_validate(payload, schema_key="agent_result", path=path)
    status = str(payload.get("status", "")).strip()
    if status not in {"complete", "needs_retry", "failed"}:
        failures.append(f"{path} status must be one of complete|needs_retry|failed")
    return failures


def _validate_design(state: dict[str, Any], *, stage: str) -> list[str]:
    if stage not in {
        "design",
        "implementation",
        "implementation_review",
        "launch",
        "extract_results",
        "update_docs",
        "decide_repeat",
        "hypothesis",
    }:
        return []

    iteration_id = str(state.get("iteration_id", "")).strip()
    path = _iteration_dir(state) / "design.yaml"
    try:
        payload = _load_yaml(path)
    except Exception as exc:
        return [f"{path} {exc}"]

    failures = _schema_validate(payload, schema_key="design", path=path)
    if str(payload.get("iteration_id", "")).strip() and str(payload.get("iteration_id")).strip() != iteration_id:
        failures.append(f"{path} iteration_id mismatch with state")
    return failures


def _validate_review_result(state: dict[str, Any], policy: dict[str, Any], *, stage: str) -> list[str]:
    if stage not in {"implementation_review", "launch", "extract_results", "update_docs", "decide_repeat"}:
        return []

    path = _iteration_dir(state) / "review_result.json"
    try:
        payload = _load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]

    failures = _schema_validate(payload, schema_key="review_result", path=path)
    status = str(payload.get("status", "")).strip().lower()
    if status not in {"pass", "needs_retry", "failed"}:
        failures.append(f"{path} status must be one of pass|needs_retry|failed")

    required_checks = payload.get("required_checks")
    if not isinstance(required_checks, dict):
        return failures + [f"{path} required_checks must be a mapping"]

    for check_name in REVIEW_RESULT_REQUIRED_CHECKS:
        if check_name not in required_checks:
            failures.append(f"{path} required_checks missing key '{check_name}'")
            continue
        check_status = str(required_checks.get(check_name, "")).strip().lower()
        if check_status not in REVIEW_RESULT_CHECK_STATUSES:
            failures.append(
                f"{path} required_checks['{check_name}'] must be one of {sorted(REVIEW_RESULT_CHECK_STATUSES)}"
            )

    policy_requirements = _stage_requirements(policy, "implementation_review")
    for check_name in REVIEW_RESULT_REQUIRED_CHECKS:
        if not policy_requirements.get(check_name, False):
            continue
        check_status = str(required_checks.get(check_name, "")).strip().lower()
        if status == "pass" and check_status != "pass":
            failures.append(
                f"{path} status=pass requires required_checks['{check_name}']='pass' (policy requirement)"
            )

    return failures


def _validate_run_manifest(state: dict[str, Any], *, stage: str) -> list[str]:
    if stage not in {"launch", "extract_results", "update_docs", "decide_repeat"}:
        return []

    run_dir = _latest_run_dir(state)
    if run_dir is None:
        return []

    path = run_dir / "run_manifest.json"
    try:
        payload = _load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]

    failures = _schema_validate(payload, schema_key="run_manifest", path=path)
    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    if str(payload.get("iteration_id", "")).strip() and str(payload.get("iteration_id", "")).strip() != iteration_id:
        failures.append(f"{path} iteration_id mismatch")
    if str(payload.get("run_id", "")).strip() and str(payload.get("run_id", "")).strip() != run_id:
        failures.append(f"{path} run_id mismatch")

    if stage == "launch":
        review_path = _iteration_dir(state) / "review_result.json"
        try:
            review_payload = _load_json(review_path)
        except Exception as exc:
            failures.append(f"{review_path} {exc}")
            review_payload = {}
        review_status = str(review_payload.get("status", "")).strip().lower()
        if review_status != "pass":
            failures.append(f"{review_path} status must be 'pass' before launch")

        design_path = _iteration_dir(state) / "design.yaml"
        try:
            design_payload = _load_yaml(design_path)
        except Exception as exc:
            failures.append(f"{design_path} {exc}")
            design_payload = {}
        design_compute = design_payload.get("compute") if isinstance(design_payload, dict) else {}
        design_location = str((design_compute or {}).get("location", "")).strip().lower()
        host_mode = str(payload.get("host_mode", payload.get("launch_mode", ""))).strip().lower()
        if design_location and host_mode and design_location != host_mode:
            failures.append(
                f"{path} host mode '{host_mode}' does not match design.compute.location '{design_location}'"
            )

    if stage == "extract_results":
        sync = payload.get("artifact_sync_to_local")
        if not isinstance(sync, dict):
            failures.append(f"{path} artifact_sync_to_local must be a mapping")
        else:
            sync_status = str(sync.get("status", "")).strip().lower()
            if sync_status not in SYNC_SUCCESS_STATUSES:
                failures.append(
                    f"{path} artifact_sync_to_local.status must be success-like for extract_results"
                )

    return failures


def _validate_metrics(state: dict[str, Any], *, stage: str) -> list[str]:
    if stage not in {"extract_results", "update_docs", "decide_repeat"}:
        return []

    run_dir = _latest_run_dir(state)
    if run_dir is None:
        return []

    path = run_dir / "metrics.json"
    try:
        payload = _load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]

    failures = _schema_validate(payload, schema_key="metrics", path=path)
    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    if str(payload.get("iteration_id", "")).strip() and str(payload.get("iteration_id", "")).strip() != iteration_id:
        failures.append(f"{path} iteration_id mismatch")
    if str(payload.get("run_id", "")).strip() and str(payload.get("run_id", "")).strip() != run_id:
        failures.append(f"{path} run_id mismatch")
    return failures


def _resolve_stage(state: dict[str, Any], stage_override: str | None) -> str:
    if stage_override:
        return stage_override.strip()
    return str(state.get("stage", "")).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=None, help="Override stage from .autolab/state.json")
    args = parser.parse_args()

    failures: list[str] = []

    try:
        state = _load_state()
    except Exception as exc:
        print(f"schema_checks: ERROR {exc}")
        return 1
    stage = _resolve_stage(state, args.stage)
    if not stage:
        print("schema_checks: ERROR state stage is missing")
        return 1

    policy = _load_policy()

    failures.extend(_validate_state_schema())
    failures.extend(_validate_backlog_schema())
    failures.extend(_validate_agent_result())
    failures.extend(_validate_design(state, stage=stage))
    failures.extend(_validate_review_result(state, policy, stage=stage))
    failures.extend(_validate_run_manifest(state, stage=stage))
    failures.extend(_validate_metrics(state, stage=stage))

    if failures:
        print("schema_checks: FAIL")
        for reason in failures:
            print(reason)
        return 1

    print("schema_checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
