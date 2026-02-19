#!/usr/bin/env python3
"""Schema-oriented artifact validation for autolab stage handoffs.

Responsibility boundary
-----------------------
**Owns (schema_checks)**:
  - JSON Schema validation of all structured artifacts against .schema.json
    files (state, backlog, design, agent_result, review_result, run_manifest,
    metrics, decision_result, todo_state, todo_focus, plan_metadata,
    plan_execution_summary) using Draft 2020-12 via jsonschema library
  - Cross-artifact consistency:
      * iteration_id / run_id match across state, design, run_manifest, metrics
      * primary_metric.name in metrics matches design.metrics.primary.name
      * host_mode in run_manifest matches design.compute.location
  - Stage-gating invariants:
      * review_result.status must be 'pass' before launch
      * Policy-required checks (tests, dry_run, schema, env_smoke,
        docs_target_update) must be 'pass' when review status is 'pass'
      * artifact_sync_to_local.status must be success-like for extract_results
  - Required field presence and type enforcement (via JSON Schema)
  - Optional strict additionalProperties mode (via verifier_policy.yaml)

**Does NOT own** (these belong to template_fill):
  - Placeholder / template detection ({{...}}, <...>, TODO, TBD, FIXME, ...)
  - File size budgets (line / character / byte limits)
  - Content triviality detection (empty scripts, comment-only files)
  - Template-identity detection (content == bootstrap template verbatim)
  - Hypothesis PrimaryMetric format validation

**Known boundary overlap** with template_fill:
  template_fill performs lightweight structural checks (required keys, enum
  values, schema_version) as a fast pre-flight that partially overlaps with
  the full JSON Schema validation here.  This duplication is intentional
  defence-in-depth; see template_fill.py docstring for details.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterable

from invariant_checks import (
    check_design_manifest_host_mode,
    check_manifest_sync_status,
    check_metric_name_match,
)

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

try:
    from jsonschema import Draft202012Validator
except Exception:  # pragma: no cover
    Draft202012Validator = None

try:
    from autolab.config import (
        _resolve_stage_requirements as _shared_resolve_stage_requirements,
    )
except Exception:  # pragma: no cover
    _shared_resolve_stage_requirements = None  # type: ignore[assignment]

try:
    from autolab.constants import (
        REVIEW_RESULT_REQUIRED_CHECKS,
        REVIEW_RESULT_CHECK_STATUSES,
    )
except Exception:  # pragma: no cover
    REVIEW_RESULT_REQUIRED_CHECKS = (  # type: ignore[misc]
        "tests",
        "dry_run",
        "schema",
        "env_smoke",
        "docs_target_update",
    )
    REVIEW_RESULT_CHECK_STATUSES = {"pass", "skip", "fail"}  # type: ignore[misc]

from verifier_lib import (
    REPO_ROOT,
    STATE_FILE,
    EXPERIMENT_TYPES,
    DEFAULT_EXPERIMENT_TYPE,
    load_json,
    load_yaml,
    load_state,
    resolve_iteration_dir,
    suggest_fix_hints,
)

SCHEMA_DIR = REPO_ROOT / ".autolab" / "schemas"
SCHEMAS: dict[str, str] = {
    "state": "state.schema.json",
    "backlog": "backlog.schema.json",
    "design": "design.schema.json",
    "agent_result": "agent_result.schema.json",
    "review_result": "review_result.schema.json",
    "run_manifest": "run_manifest.schema.json",
    "metrics": "metrics.schema.json",
    "decision_result": "decision_result.schema.json",
    "todo_state": "todo_state.schema.json",
    "todo_focus": "todo_focus.schema.json",
    "plan_metadata": "plan_metadata.schema.json",
    "plan_execution_summary": "plan_execution_summary.schema.json",
}


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
    return load_json(path)


def _format_error_path(error_path: Iterable[Any]) -> str:
    pieces = ["$"]
    for part in error_path:
        if isinstance(part, int):
            pieces.append(f"[{part}]")
        else:
            pieces.append(f".{part}")
    return "".join(pieces)


def _patch_strict_additional_properties(schema: dict[str, Any]) -> dict[str, Any]:
    """Recursively set additionalProperties: false on all object schemas."""
    import copy

    patched = copy.deepcopy(schema)
    _patch_object_schema(patched)
    return patched


def _patch_object_schema(node: Any) -> None:
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" and "additionalProperties" not in node:
        node["additionalProperties"] = False
    if "properties" in node and isinstance(node["properties"], dict):
        for _key, prop_schema in node["properties"].items():
            _patch_object_schema(prop_schema)
    if "items" in node:
        _patch_object_schema(node["items"])


def _is_strict_schema_mode() -> bool:
    policy = _load_policy()
    schema_validation = policy.get("schema_validation")
    if not isinstance(schema_validation, dict):
        return False
    return bool(schema_validation.get("strict_additional_properties", False))


def _schema_validate(payload: Any, *, schema_key: str, path: Path) -> list[str]:
    if Draft202012Validator is None:
        return ["jsonschema dependency is required (install: pip install jsonschema)"]
    schema = _load_schema(schema_key)
    if _is_strict_schema_mode():
        schema = _patch_strict_additional_properties(schema)
    validator = Draft202012Validator(schema)
    failures: list[str] = []
    for error in sorted(
        validator.iter_errors(payload), key=lambda item: _format_error_path(item.path)
    ):
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
                "prompt_lint": bool(shared.get("prompt_lint", False)),
                "consistency": bool(shared.get("consistency", False)),
                "env_smoke": bool(shared.get("env_smoke", False)),
                "docs_target_update": bool(shared.get("docs_target_update", False)),
            }

    output: dict[str, bool] = {
        "tests": False,
        "dry_run": False,
        "schema": False,
        "prompt_lint": False,
        "consistency": False,
        "env_smoke": False,
        "docs_target_update": False,
    }
    legacy_map = {
        "tests": "require_tests",
        "dry_run": "require_dry_run",
        "schema": "require_schema",
        "prompt_lint": "require_prompt_lint",
        "consistency": "require_consistency",
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


def _iteration_dir(state: dict[str, Any]) -> Path:
    iteration_id = str(state.get("iteration_id", "")).strip()
    return resolve_iteration_dir(iteration_id)


def _latest_run_dir(state: dict[str, Any]) -> Path | None:
    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    if not iteration_id or not run_id:
        return None
    return resolve_iteration_dir(iteration_id) / "runs" / run_id


def _validate_state_schema() -> list[str]:
    try:
        payload = load_json(STATE_FILE)
    except Exception as exc:
        return [f"{STATE_FILE} {exc}"]
    return _schema_validate(payload, schema_key="state", path=STATE_FILE)


def _validate_backlog_schema() -> list[str]:
    path = REPO_ROOT / ".autolab" / "backlog.yaml"
    try:
        payload = load_yaml(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    return _schema_validate(payload, schema_key="backlog", path=path)


def _validate_agent_result() -> list[str]:
    path = REPO_ROOT / ".autolab" / "agent_result.json"
    try:
        payload = load_json(path)
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
        payload = load_yaml(path)
    except Exception as exc:
        return [f"{path} {exc}"]

    failures = _schema_validate(payload, schema_key="design", path=path)
    if (
        str(payload.get("iteration_id", "")).strip()
        and str(payload.get("iteration_id")).strip() != iteration_id
    ):
        failures.append(f"{path} iteration_id mismatch with state")
    return failures


def _validate_review_result(
    state: dict[str, Any], policy: dict[str, Any], *, stage: str
) -> list[str]:
    if stage not in {
        "implementation_review",
        "launch",
        "extract_results",
        "update_docs",
        "decide_repeat",
    }:
        return []

    path = _iteration_dir(state) / "review_result.json"
    try:
        payload = load_json(path)
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
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]

    failures = _schema_validate(payload, schema_key="run_manifest", path=path)
    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    if (
        str(payload.get("iteration_id", "")).strip()
        and str(payload.get("iteration_id", "")).strip() != iteration_id
    ):
        failures.append(f"{path} iteration_id mismatch")
    if (
        str(payload.get("run_id", "")).strip()
        and str(payload.get("run_id", "")).strip() != run_id
    ):
        failures.append(f"{path} run_id mismatch")

    if stage == "launch":
        review_path = _iteration_dir(state) / "review_result.json"
        try:
            review_payload = load_json(review_path)
        except Exception as exc:
            failures.append(f"{review_path} {exc}")
            review_payload = {}
        review_status = str(review_payload.get("status", "")).strip().lower()
        if review_status != "pass":
            failures.append(f"{review_path} status must be 'pass' before launch")

        design_path = _iteration_dir(state) / "design.yaml"
        try:
            design_payload = load_yaml(design_path)
        except Exception as exc:
            failures.append(f"{design_path} {exc}")
            design_payload = {}
        mode_failures = check_design_manifest_host_mode(design_payload, payload)
        for failure in mode_failures:
            failures.append(f"{path} {failure}")

    if stage in {"launch", "extract_results", "update_docs", "decide_repeat"}:
        failures.extend(
            check_manifest_sync_status(
                payload,
                require_success=stage == "extract_results",
                context=stage,
            )
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


def _validate_replicate_run_manifests(
    state: dict[str, Any], *, stage: str
) -> list[str]:
    if stage not in {"launch", "extract_results", "update_docs", "decide_repeat"}:
        return []

    run_group = _normalized_run_group(state)
    if len(run_group) <= 1:
        return []

    failures: list[str] = []
    iteration_id = str(state.get("iteration_id", "")).strip()
    iteration_dir = _iteration_dir(state)

    for replicate_run_id in run_group:
        path = iteration_dir / "runs" / replicate_run_id / "run_manifest.json"
        try:
            payload = load_json(path)
        except Exception as exc:
            failures.append(f"{path} {exc}")
            continue
        failures.extend(_schema_validate(payload, schema_key="run_manifest", path=path))

        manifest_run_id = str(payload.get("run_id", "")).strip()
        if manifest_run_id and manifest_run_id != replicate_run_id:
            failures.append(
                f"{path} run_id '{manifest_run_id}' does not match replicate id '{replicate_run_id}'"
            )
        manifest_iteration_id = str(payload.get("iteration_id", "")).strip()
        if (
            iteration_id
            and manifest_iteration_id
            and manifest_iteration_id != iteration_id
        ):
            failures.append(
                f"{path} iteration_id '{manifest_iteration_id}' does not match state.iteration_id '{iteration_id}'"
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
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]

    failures = _schema_validate(payload, schema_key="metrics", path=path)
    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    if (
        str(payload.get("iteration_id", "")).strip()
        and str(payload.get("iteration_id", "")).strip() != iteration_id
    ):
        failures.append(f"{path} iteration_id mismatch")
    if (
        str(payload.get("run_id", "")).strip()
        and str(payload.get("run_id", "")).strip() != run_id
    ):
        failures.append(f"{path} run_id mismatch")

    design_path = _iteration_dir(state) / "design.yaml"
    try:
        design_payload = load_yaml(design_path)
    except Exception:
        design_payload = {}
    metric_failures = check_metric_name_match(design_payload, payload)
    for failure in metric_failures:
        failures.append(f"{path} {failure}")
    return failures


def _validate_decision_result(state: dict[str, Any], *, stage: str) -> list[str]:
    if stage != "decide_repeat":
        return []

    path = _iteration_dir(state) / "decision_result.json"
    try:
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]

    failures = _schema_validate(payload, schema_key="decision_result", path=path)
    return failures


def _resolve_stage(state: dict[str, Any], stage_override: str | None) -> str:
    if stage_override:
        return stage_override.strip()
    return str(state.get("stage", "")).strip()


def _validate_todo_state(state: dict[str, Any]) -> list[str]:
    path = REPO_ROOT / ".autolab" / "todo_state.json"
    assistant_mode = str(state.get("assistant_mode", "")).strip().lower() == "on"
    if not path.exists():
        if assistant_mode:
            return [f"{path} is required when assistant_mode=on"]
        return []
    try:
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    return _schema_validate(payload, schema_key="todo_state", path=path)


def _validate_todo_focus(state: dict[str, Any]) -> list[str]:
    path = REPO_ROOT / ".autolab" / "todo_focus.json"
    assistant_mode = str(state.get("assistant_mode", "")).strip().lower() == "on"
    if not path.exists():
        if assistant_mode:
            return [f"{path} is required when assistant_mode=on"]
        return []
    try:
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    return _schema_validate(payload, schema_key="todo_focus", path=path)


def _validate_plan_metadata(state: dict[str, Any]) -> list[str]:
    """Validate plan_metadata.json if it exists (optional artifact)."""
    iteration_dir = _iteration_dir(state)
    path = iteration_dir / "plan_metadata.json"
    if not path.exists():
        return []
    try:
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    return _schema_validate(payload, schema_key="plan_metadata", path=path)


def _validate_plan_execution_summary(state: dict[str, Any]) -> list[str]:
    """Validate plan_execution_summary.json if it exists (optional artifact)."""
    iteration_dir = _iteration_dir(state)
    path = iteration_dir / "plan_execution_summary.json"
    if not path.exists():
        return []
    try:
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    return _schema_validate(payload, schema_key="plan_execution_summary", path=path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", default=None, help="Override stage from .autolab/state.json"
    )
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
        if args.json:
            envelope = {
                "status": "fail",
                "verifier": "schema_checks",
                "stage": "",
                "checks": [],
                "errors": [str(exc)],
            }
            print(json.dumps(envelope))
        else:
            print(f"schema_checks: ERROR {exc}")
        return 1
    stage = _resolve_stage(state, args.stage)
    if not stage:
        if args.json:
            envelope = {
                "status": "fail",
                "verifier": "schema_checks",
                "stage": "",
                "checks": [],
                "errors": ["state stage is missing"],
            }
            print(json.dumps(envelope))
        else:
            print("schema_checks: ERROR state stage is missing")
        return 1

    policy = _load_policy()

    failures.extend(_validate_state_schema())
    failures.extend(_validate_backlog_schema())
    failures.extend(_validate_agent_result())
    failures.extend(_validate_design(state, stage=stage))
    failures.extend(_validate_review_result(state, policy, stage=stage))
    failures.extend(_validate_run_manifest(state, stage=stage))
    failures.extend(_validate_replicate_run_manifests(state, stage=stage))
    failures.extend(_validate_metrics(state, stage=stage))
    failures.extend(_validate_decision_result(state, stage=stage))
    failures.extend(_validate_todo_state(state))
    failures.extend(_validate_todo_focus(state))
    failures.extend(_validate_plan_metadata(state))
    failures.extend(_validate_plan_execution_summary(state))

    passed = not failures

    if args.json:
        checks = [{"name": f, "status": "fail", "detail": f} for f in failures]
        if passed:
            checks = [
                {
                    "name": "schema_checks",
                    "status": "pass",
                    "detail": "all schema checks passed",
                }
            ]
        envelope = {
            "status": "pass" if passed else "fail",
            "verifier": "schema_checks",
            "stage": stage,
            "checks": checks,
            "errors": failures,
        }
        print(json.dumps(envelope))
    else:
        if failures:
            print("schema_checks: FAIL")
            for reason in failures:
                print(reason)
            top_n = failures[:3]
            if top_n:
                print("\n--- Top failures summary ---")
                schema_hints: dict[str, str] = {
                    "state": "Check .autolab/state.json keys match state.schema.json",
                    "backlog": "Verify .autolab/backlog.yaml experiments list structure",
                    "design": "Ensure design.yaml has schema_version, id, iteration_id, metrics, baselines",
                    "review_result": "Confirm review_result.json has all required_checks keys with valid statuses",
                    "agent_result": "Check .autolab/agent_result.json status is complete|needs_retry|failed",
                    "todo_state": "Validate .autolab/todo_state.json version field is an integer",
                    "metrics": "Ensure metrics.json has schema_version and primary_metric",
                }
                for i, failure_text in enumerate(top_n, 1):
                    hint = ""
                    for schema_key, schema_hint in schema_hints.items():
                        if schema_key in failure_text.lower():
                            hint = f" Hint: {schema_hint}"
                            break
                    print(f"  {i}. {failure_text}{hint}")
                print(
                    f"\nNext steps: fix the above issues and rerun `autolab verify --stage {stage}`"
                )
            hint_texts = suggest_fix_hints(
                failures,
                stage=stage,
                verifier="schema_checks",
            )
            if hint_texts:
                print("\nMost likely fixes:")
                for hint in hint_texts:
                    print(f"- {hint}")
        else:
            print("schema_checks: PASS")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
