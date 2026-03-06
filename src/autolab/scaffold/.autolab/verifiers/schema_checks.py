#!/usr/bin/env python3
"""Schema-oriented artifact validation for autolab stage handoffs.

Responsibility boundary
-----------------------
**Owns (schema_checks)**:
  - JSON Schema validation of all structured artifacts against .schema.json
    files (state, backlog, design, agent_result, review_result, run_manifest,
    metrics, decision_result, todo_state, todo_focus, plan_metadata,
    plan_execution_summary, discuss_sidecar, research_sidecar) using Draft
    2020-12 via jsonschema library
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

import pathlib
import sys

_VERIFIER_DIR = pathlib.Path(__file__).resolve().parent
if str(_VERIFIER_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFIER_DIR))

import argparse
import json
from datetime import datetime
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
    from jsonschema import Draft202012Validator, FormatChecker
except Exception:  # pragma: no cover
    Draft202012Validator = None
    FormatChecker = None

try:
    from autolab.config import (
        _resolve_stage_requirements as _shared_resolve_stage_requirements,
    )
except Exception:  # pragma: no cover
    _shared_resolve_stage_requirements = None  # type: ignore[assignment]

try:
    from autolab.scope import _resolve_project_wide_root as _shared_project_wide_root
except Exception:  # pragma: no cover
    _shared_project_wide_root = None  # type: ignore[assignment]

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

try:
    from autolab.validators import _validate_design as _shared_validate_design
except Exception:  # pragma: no cover
    _shared_validate_design = None  # type: ignore[assignment]

try:
    from autolab.uat import ensure_uat_pass as _shared_ensure_uat_pass
    from autolab.uat import resolve_uat_requirement as _shared_resolve_uat_requirement
except Exception:  # pragma: no cover
    _shared_ensure_uat_pass = None  # type: ignore[assignment]
    _shared_resolve_uat_requirement = None  # type: ignore[assignment]

try:
    from autolab.utils import _path_fingerprint as _shared_path_fingerprint
except Exception:  # pragma: no cover
    _shared_path_fingerprint = None  # type: ignore[assignment]

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
    "parser_capabilities": "parser_capabilities.schema.json",
    "parser_capabilities_index": "parser_capabilities_index.schema.json",
    "agent_result": "agent_result.schema.json",
    "review_result": "review_result.schema.json",
    "run_manifest": "run_manifest.schema.json",
    "metrics": "metrics.schema.json",
    "decision_result": "decision_result.schema.json",
    "todo_state": "todo_state.schema.json",
    "todo_focus": "todo_focus.schema.json",
    "plan_metadata": "plan_metadata.schema.json",
    "plan_execution_summary": "plan_execution_summary.schema.json",
    "plan_execution_state": "plan_execution_state.schema.json",
    "traceability_coverage": "traceability_coverage.schema.json",
    "traceability_latest": "traceability_latest.schema.json",
    "handoff": "handoff.schema.json",
    "plan_contract": "plan_contract.schema.json",
    "plan_check_result": "plan_check_result.schema.json",
    "plan_approval": "plan_approval.schema.json",
    "plan_graph": "plan_graph.schema.json",
    "codebase_project_map": "codebase_project_map.schema.json",
    "codebase_experiment_delta": "codebase_experiment_delta.schema.json",
    "codebase_context_bundle": "codebase_context_bundle.schema.json",
    "discuss_sidecar": "discuss_sidecar.schema.json",
    "research_sidecar": "research_sidecar.schema.json",
    "design_context_quality": "design_context_quality.schema.json",
}

OBSERVABILITY_REASON_CODES = {
    "",
    "completed",
    "dependency_blocked",
    "fail_fast_skipped",
    "wave_retry_pending",
    "runner_failed",
    "verification_failed",
    "expected_artifacts_missing",
    "out_of_contract_edits",
    "task_exception",
    "missing_task_result",
    "pending",
    "unknown",
}

TASK_KEYED_PLAN_EXECUTION_STATE_FIELDS = (
    "task_status",
    "task_attempt_counts",
    "task_retry_counts",
    "task_last_error",
    "task_files_changed",
    "task_started_at",
    "task_completed_at",
    "task_duration_seconds",
    "task_reason_code",
    "task_reason_detail",
    "task_runner_report_path",
    "task_verification_status",
    "task_verification_commands",
    "task_expected_artifacts_missing",
    "task_blocked_by",
)


def _normalize_metric_names(raw_value: object) -> list[str]:
    if not isinstance(raw_value, list):
        return []
    normalized: list[str] = []
    for item in raw_value:
        metric_name = str(item).strip()
        if metric_name and metric_name not in normalized:
            normalized.append(metric_name)
    return normalized


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


def _is_traceability_schema_strict(policy: dict[str, Any]) -> bool:
    """Return whether traceability schema issues should fail verification."""
    schema_validation = policy.get("schema_validation")
    if not isinstance(schema_validation, dict):
        return False
    if "traceability_artifacts_strict" not in schema_validation:
        return False
    return bool(schema_validation.get("traceability_artifacts_strict"))


def _schema_validate(payload: Any, *, schema_key: str, path: Path) -> list[str]:
    if Draft202012Validator is None:
        return ["jsonschema dependency is required (install: pip install jsonschema)"]
    schema = _load_schema(schema_key)
    if _is_strict_schema_mode():
        schema = _patch_strict_additional_properties(schema)
    validator_kwargs: dict[str, Any] = {}
    if FormatChecker is not None:
        validator_kwargs["format_checker"] = FormatChecker()
    validator = Draft202012Validator(schema, **validator_kwargs)
    failures: list[str] = []
    for error in sorted(
        validator.iter_errors(payload), key=lambda item: _format_error_path(item.path)
    ):
        location = _format_error_path(error.path)
        failures.append(f"{path} schema violation at {location}: {error.message}")
    return failures


def _load_plan_contract_task_ids(state: dict[str, Any]) -> set[str]:
    iteration_dir = _iteration_dir(state)
    task_ids: set[str] = set()
    for path in (
        iteration_dir / "plan_contract.json",
        REPO_ROOT / ".autolab" / "plan_contract.json",
    ):
        if not path.exists():
            continue
        try:
            payload = load_json(path)
        except Exception:
            continue
        tasks = payload.get("tasks")
        if not isinstance(tasks, list):
            continue
        for row in tasks:
            if not isinstance(row, dict):
                continue
            task_id = str(row.get("task_id", "")).strip()
            if task_id:
                task_ids.add(task_id)
    return task_ids


def _validate_reason_code(
    raw_value: Any,
    *,
    path_label: str,
    allow_empty: bool,
) -> list[str]:
    reason_code = str(raw_value or "").strip().lower()
    if not reason_code and allow_empty:
        return []
    if reason_code not in OBSERVABILITY_REASON_CODES:
        allowed = ", ".join(
            sorted(code or "<empty>" for code in OBSERVABILITY_REASON_CODES)
        )
        return [f"{path_label} must be one of [{allowed}]"]
    return []


def _validate_known_task_ids(
    mapping: Any,
    *,
    path: Path,
    field_name: str,
    known_task_ids: set[str],
) -> list[str]:
    if not known_task_ids or not isinstance(mapping, dict):
        return []
    failures: list[str] = []
    for raw_task_id in mapping.keys():
        task_id = str(raw_task_id or "").strip()
        if task_id and task_id not in known_task_ids:
            failures.append(f"{path} {field_name} contains unknown task_id '{task_id}'")
    return failures


def _is_valid_timestamp(raw_value: Any) -> bool:
    text = str(raw_value or "").strip()
    if not text:
        return False
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def _validate_timestamp(
    raw_value: Any,
    *,
    path_label: str,
    allow_empty: bool = False,
) -> list[str]:
    text = str(raw_value or "").strip()
    if not text and allow_empty:
        return []
    if not _is_valid_timestamp(text):
        return [f"{path_label} must be a valid date-time string"]
    return []


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
    if _shared_validate_design is not None:
        try:
            _shared_validate_design(
                path,
                iteration_id,
                repo_root=REPO_ROOT,
                experiment_id=str(state.get("experiment_id", "")).strip(),
            )
        except Exception as exc:
            failures.append(f"{path} {exc}")
    return failures


def _validate_parser_capabilities(
    state: dict[str, Any], policy: dict[str, Any], *, stage: str
) -> list[str]:
    if stage not in {
        "design",
        "implementation",
        "implementation_review",
        "launch",
        "extract_results",
        "update_docs",
        "decide_repeat",
    }:
        return []

    extract = policy.get("extract_results")
    if not isinstance(extract, dict):
        extract = {}
    parser_policy = extract.get("parser")
    if not isinstance(parser_policy, dict):
        parser_policy = {}
    require_manifest = bool(parser_policy.get("require_capability_manifest", False))
    require_index = bool(parser_policy.get("require_capability_index", False))

    iteration_dir = _iteration_dir(state)
    design_path = iteration_dir / "design.yaml"
    manifest_path = iteration_dir / "parser_capabilities.json"
    index_path = REPO_ROOT / ".autolab" / "parser_capabilities.json"

    if (
        not require_manifest
        and not require_index
        and not manifest_path.exists()
        and not index_path.exists()
    ):
        return []

    failures: list[str] = []
    try:
        design_payload = load_yaml(design_path)
    except Exception as exc:
        return [f"{design_path} {exc}"]

    parser_block = design_payload.get("extract_parser")
    design_parser_kind = ""
    if isinstance(parser_block, dict):
        design_parser_kind = str(parser_block.get("kind", "")).strip().lower()
    primary_metric_name = ""
    metrics_block = design_payload.get("metrics")
    if isinstance(metrics_block, dict):
        primary_block = metrics_block.get("primary")
        if isinstance(primary_block, dict):
            primary_metric_name = str(primary_block.get("name", "")).strip()

    manifest_payload: dict[str, Any] | None = None
    if not manifest_path.exists():
        if require_manifest:
            failures.append(
                f"{manifest_path} is required by verifier_policy extract_results.parser.require_capability_manifest=true"
            )
    else:
        try:
            manifest_payload = load_json(manifest_path)
        except Exception as exc:
            failures.append(f"{manifest_path} {exc}")
        else:
            failures.extend(
                _schema_validate(
                    manifest_payload,
                    schema_key="parser_capabilities",
                    path=manifest_path,
                )
            )

    manifest_parser_kind = ""
    manifest_supported_metrics: list[str] = []
    if isinstance(manifest_payload, dict):
        manifest_parser = manifest_payload.get("parser")
        if isinstance(manifest_parser, dict):
            manifest_parser_kind = str(manifest_parser.get("kind", "")).strip().lower()
        manifest_supported_metrics = _normalize_metric_names(
            manifest_payload.get("supported_metrics")
        )

    if (
        design_parser_kind
        and manifest_parser_kind
        and (design_parser_kind != manifest_parser_kind)
    ):
        failures.append(
            "parser capability mismatch: "
            f"design.extract_parser.kind='{design_parser_kind}' does not match "
            f"parser_capabilities.parser.kind='{manifest_parser_kind}'"
        )

    if primary_metric_name and not manifest_supported_metrics:
        failures.append(
            "parser capability mismatch: parser_capabilities.supported_metrics must "
            f"include design primary metric '{primary_metric_name}'"
        )
    elif (
        primary_metric_name
        and manifest_supported_metrics
        and primary_metric_name not in manifest_supported_metrics
    ):
        failures.append(
            "parser capability mismatch: "
            f"design.metrics.primary.name='{primary_metric_name}' is not listed in "
            "parser_capabilities.supported_metrics"
        )

    if not index_path.exists():
        if require_index:
            failures.append(
                f"{index_path} is required by verifier_policy extract_results.parser.require_capability_index=true"
            )
        return failures

    index_payload: dict[str, Any] | None = None
    try:
        index_payload = load_json(index_path)
    except Exception as exc:
        failures.append(f"{index_path} {exc}")
    else:
        failures.extend(
            _schema_validate(
                index_payload,
                schema_key="parser_capabilities_index",
                path=index_path,
            )
        )

    if not isinstance(index_payload, dict):
        return failures
    iterations_map = index_payload.get("iterations")
    if not isinstance(iterations_map, dict):
        failures.append(f"{index_path} iterations must be a mapping")
        return failures

    iteration_id = str(state.get("iteration_id", "")).strip()
    entry = iterations_map.get(iteration_id)
    if not isinstance(entry, dict):
        failures.append(f"{index_path} iterations is missing key '{iteration_id}'")
        return failures

    entry_manifest_path = str(entry.get("manifest_path", "")).strip()
    if entry_manifest_path:
        expected_manifest_path = (
            manifest_path.relative_to(REPO_ROOT).as_posix()
            if manifest_path.is_relative_to(REPO_ROOT)
            else str(manifest_path)
        )
        if entry_manifest_path != expected_manifest_path:
            failures.append(
                "parser capability mismatch: "
                f"index manifest_path '{entry_manifest_path}' does not match "
                f"'{expected_manifest_path}'"
            )

    entry_parser_kind = str(entry.get("parser_kind", "")).strip().lower()
    if (
        manifest_parser_kind
        and entry_parser_kind
        and (entry_parser_kind != manifest_parser_kind)
    ):
        failures.append(
            "parser capability mismatch: "
            f"index parser_kind '{entry_parser_kind}' does not match "
            f"manifest parser kind '{manifest_parser_kind}'"
        )

    entry_supported = _normalize_metric_names(entry.get("supported_metrics"))
    if (
        primary_metric_name
        and entry_supported
        and primary_metric_name not in entry_supported
    ):
        failures.append(
            "parser capability mismatch: "
            f"index supported_metrics does not include design primary metric '{primary_metric_name}'"
        )

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
    failures = _schema_validate(payload, schema_key="plan_execution_summary", path=path)
    failures.extend(
        _validate_timestamp(
            payload.get("generated_at"),
            path_label=f"{path} generated_at",
        )
    )
    known_task_ids = _load_plan_contract_task_ids(state)
    task_details = payload.get("task_details")
    if isinstance(task_details, list):
        for index, row in enumerate(task_details):
            if not isinstance(row, dict):
                continue
            failures.extend(
                _validate_timestamp(
                    row.get("started_at"),
                    path_label=f"{path} task_details[{index}].started_at",
                )
            )
            failures.extend(
                _validate_timestamp(
                    row.get("completed_at"),
                    path_label=f"{path} task_details[{index}].completed_at",
                )
            )
            task_id = str(row.get("task_id", "")).strip()
            if known_task_ids and task_id and task_id not in known_task_ids:
                failures.append(
                    f"{path} task_details[{index}].task_id '{task_id}' is not declared in plan_contract.json"
                )
            failures.extend(
                _validate_reason_code(
                    row.get("reason_code"),
                    path_label=f"{path} task_details[{index}].reason_code",
                    allow_empty=False,
                )
            )
    wave_details = payload.get("wave_details")
    if isinstance(wave_details, list):
        for wave_index, row in enumerate(wave_details):
            if not isinstance(row, dict):
                continue
            failures.extend(
                _validate_timestamp(
                    row.get("started_at"),
                    path_label=f"{path} wave_details[{wave_index}].started_at",
                )
            )
            failures.extend(
                _validate_timestamp(
                    row.get("completed_at"),
                    path_label=f"{path} wave_details[{wave_index}].completed_at",
                )
            )
            attempt_history = row.get("attempt_history")
            if isinstance(attempt_history, list):
                for attempt_index, attempt in enumerate(attempt_history):
                    if not isinstance(attempt, dict):
                        continue
                    failures.extend(
                        _validate_timestamp(
                            attempt.get("started_at"),
                            path_label=(
                                f"{path} wave_details[{wave_index}].attempt_history[{attempt_index}].started_at"
                            ),
                        )
                    )
                    failures.extend(
                        _validate_timestamp(
                            attempt.get("completed_at"),
                            path_label=(
                                f"{path} wave_details[{wave_index}].attempt_history[{attempt_index}].completed_at"
                            ),
                        )
                    )
    critical_path = payload.get("critical_path")
    if isinstance(critical_path, dict) and known_task_ids:
        for task_id in critical_path.get("task_ids", []):
            normalized = str(task_id or "").strip()
            if normalized and normalized not in known_task_ids:
                failures.append(
                    f"{path} critical_path.task_ids contains unknown task_id '{normalized}'"
                )
    return failures


def _validate_plan_execution_state(state: dict[str, Any]) -> list[str]:
    """Validate plan_execution_state.json if it exists (optional artifact)."""
    iteration_dir = _iteration_dir(state)
    path = iteration_dir / "plan_execution_state.json"
    if not path.exists():
        return []
    try:
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    failures = _schema_validate(payload, schema_key="plan_execution_state", path=path)
    failures.extend(
        _validate_timestamp(
            payload.get("generated_at"),
            path_label=f"{path} generated_at",
        )
    )
    failures.extend(
        _validate_timestamp(
            payload.get("updated_at"),
            path_label=f"{path} updated_at",
        )
    )
    known_task_ids = _load_plan_contract_task_ids(state)
    for field_name in TASK_KEYED_PLAN_EXECUTION_STATE_FIELDS:
        failures.extend(
            _validate_known_task_ids(
                payload.get(field_name),
                path=path,
                field_name=field_name,
                known_task_ids=known_task_ids,
            )
        )
    reason_codes = payload.get("task_reason_code")
    if isinstance(reason_codes, dict):
        for task_id, raw_reason_code in reason_codes.items():
            task_label = str(task_id or "").strip() or "<empty>"
            failures.extend(
                _validate_reason_code(
                    raw_reason_code,
                    path_label=f"{path} task_reason_code['{task_label}']",
                    allow_empty=True,
                )
            )
    for field_name in (
        "task_started_at",
        "task_completed_at",
        "wave_started_at",
        "wave_completed_at",
    ):
        mapping = payload.get(field_name)
        if not isinstance(mapping, dict):
            continue
        for key, raw_value in mapping.items():
            label = str(key or "").strip() or "<empty>"
            failures.extend(
                _validate_timestamp(
                    raw_value,
                    path_label=f"{path} {field_name}['{label}']",
                )
            )
    wave_attempt_history = payload.get("wave_attempt_history")
    if isinstance(wave_attempt_history, dict):
        for wave_key, attempts in wave_attempt_history.items():
            if not isinstance(attempts, list):
                continue
            wave_label = str(wave_key or "").strip() or "<empty>"
            for attempt_index, attempt in enumerate(attempts):
                if not isinstance(attempt, dict):
                    continue
                failures.extend(
                    _validate_timestamp(
                        attempt.get("started_at"),
                        path_label=(
                            f"{path} wave_attempt_history['{wave_label}'][{attempt_index}].started_at"
                        ),
                    )
                )
                failures.extend(
                    _validate_timestamp(
                        attempt.get("completed_at"),
                        path_label=(
                            f"{path} wave_attempt_history['{wave_label}'][{attempt_index}].completed_at"
                        ),
                    )
                )
    return failures


def _validate_traceability_coverage(state: dict[str, Any]) -> list[str]:
    """Validate traceability_coverage.json when present (optional artifact)."""
    iteration_dir = _iteration_dir(state)
    path = iteration_dir / "traceability_coverage.json"
    if not path.exists():
        return []
    try:
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    failures = _schema_validate(payload, schema_key="traceability_coverage", path=path)
    state_iteration_id = str(state.get("iteration_id", "")).strip()
    payload_iteration_id = str(payload.get("iteration_id", "")).strip()
    if (
        state_iteration_id
        and payload_iteration_id
        and payload_iteration_id != state_iteration_id
    ):
        failures.append(f"{path} iteration_id mismatch")
    return failures


def _validate_traceability_latest(state: dict[str, Any]) -> list[str]:
    """Validate .autolab/traceability_latest.json when present (optional artifact)."""
    path = REPO_ROOT / ".autolab" / "traceability_latest.json"
    if not path.exists():
        return []
    try:
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    failures = _schema_validate(payload, schema_key="traceability_latest", path=path)
    state_iteration_id = str(state.get("iteration_id", "")).strip()
    payload_iteration_id = str(payload.get("iteration_id", "")).strip()
    if (
        state_iteration_id
        and payload_iteration_id
        and payload_iteration_id != state_iteration_id
    ):
        failures.append(f"{path} iteration_id mismatch")
    return failures


def _validate_handoff(state: dict[str, Any]) -> list[str]:
    """Validate .autolab/handoff.json when present (optional artifact)."""
    path = REPO_ROOT / ".autolab" / "handoff.json"
    if not path.exists():
        return []
    try:
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    failures = _schema_validate(payload, schema_key="handoff", path=path)
    failures.extend(
        _validate_timestamp(
            payload.get("generated_at"),
            path_label=f"{path} generated_at",
        )
    )
    state_iteration_id = str(state.get("iteration_id", "")).strip()
    payload_iteration_id = str(payload.get("iteration_id", "")).strip()
    if (
        state_iteration_id
        and payload_iteration_id
        and payload_iteration_id != state_iteration_id
    ):
        failures.append(f"{path} iteration_id mismatch")
    known_task_ids = _load_plan_contract_task_ids(state)
    wave_observability = payload.get("wave_observability")
    if isinstance(wave_observability, dict):
        latest_verifier = payload.get("latest_verifier_summary")
        if isinstance(latest_verifier, dict):
            failures.extend(
                _validate_timestamp(
                    latest_verifier.get("generated_at"),
                    path_label=f"{path} latest_verifier_summary.generated_at",
                )
            )
        task_summary = wave_observability.get("task_summary")
        if isinstance(task_summary, dict):
            task_details = task_summary.get("task_details")
            if isinstance(task_details, list):
                for index, row in enumerate(task_details):
                    if not isinstance(row, dict):
                        continue
                    failures.extend(
                        _validate_timestamp(
                            row.get("started_at"),
                            path_label=(
                                f"{path} wave_observability.task_summary.task_details[{index}].started_at"
                            ),
                            allow_empty=True,
                        )
                    )
                    failures.extend(
                        _validate_timestamp(
                            row.get("completed_at"),
                            path_label=(
                                f"{path} wave_observability.task_summary.task_details[{index}].completed_at"
                            ),
                            allow_empty=True,
                        )
                    )
                    task_id = str(row.get("task_id", "")).strip()
                    if known_task_ids and task_id and task_id not in known_task_ids:
                        failures.append(
                            f"{path} wave_observability.task_summary.task_details[{index}].task_id '{task_id}' is not declared in plan_contract.json"
                        )
                    failures.extend(
                        _validate_reason_code(
                            row.get("reason_code"),
                            path_label=(
                                f"{path} wave_observability.task_summary.task_details[{index}].reason_code"
                            ),
                            allow_empty=False,
                        )
                    )
        waves = wave_observability.get("waves")
        if isinstance(waves, list):
            for wave_index, row in enumerate(waves):
                if not isinstance(row, dict):
                    continue
                failures.extend(
                    _validate_timestamp(
                        row.get("started_at"),
                        path_label=f"{path} wave_observability.waves[{wave_index}].started_at",
                        allow_empty=True,
                    )
                )
                failures.extend(
                    _validate_timestamp(
                        row.get("completed_at"),
                        path_label=f"{path} wave_observability.waves[{wave_index}].completed_at",
                        allow_empty=True,
                    )
                )
                attempt_history = row.get("attempt_history")
                if isinstance(attempt_history, list):
                    for attempt_index, attempt in enumerate(attempt_history):
                        if not isinstance(attempt, dict):
                            continue
                        failures.extend(
                            _validate_timestamp(
                                attempt.get("started_at"),
                                path_label=(
                                    f"{path} wave_observability.waves[{wave_index}].attempt_history[{attempt_index}].started_at"
                                ),
                            )
                        )
                        failures.extend(
                            _validate_timestamp(
                                attempt.get("completed_at"),
                                path_label=(
                                    f"{path} wave_observability.waves[{wave_index}].attempt_history[{attempt_index}].completed_at"
                                ),
                            )
                        )
    return failures


def _iter_context_sidecar_specs(
    state: dict[str, Any],
) -> tuple[tuple[str, str, Path, str], ...]:
    iteration_dir = _iteration_dir(state)
    return (
        (
            "project_wide",
            "discuss",
            REPO_ROOT
            / ".autolab"
            / "context"
            / "sidecars"
            / "project_wide"
            / "discuss.json",
            "discuss_sidecar",
        ),
        (
            "project_wide",
            "research",
            REPO_ROOT
            / ".autolab"
            / "context"
            / "sidecars"
            / "project_wide"
            / "research.json",
            "research_sidecar",
        ),
        (
            "experiment",
            "discuss",
            iteration_dir / "context" / "sidecars" / "discuss.json",
            "discuss_sidecar",
        ),
        (
            "experiment",
            "research",
            iteration_dir / "context" / "sidecars" / "research.json",
            "research_sidecar",
        ),
    )


def _resolve_repo_contained_path(raw_path: Any) -> Path | None:
    candidate = str(raw_path or "").strip()
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    resolved_repo_root = REPO_ROOT.resolve()
    resolved_path = path.resolve(strict=False)
    try:
        resolved_path.relative_to(resolved_repo_root)
    except ValueError:
        return None
    return resolved_path


def _expected_sidecar_scope_root(
    *,
    expected_scope_kind: str,
    state: dict[str, Any],
) -> Path | None:
    if expected_scope_kind == "experiment":
        return _iteration_dir(state).resolve()
    if _shared_project_wide_root is None:
        return None
    try:
        return _shared_project_wide_root(REPO_ROOT).resolve()
    except Exception:
        return None


def _validate_sidecar_contract(path: Path, payload: dict[str, Any]) -> list[str]:
    failures: list[str] = []

    def _collect_ids(collection_name: str) -> set[str]:
        raw_entries = payload.get(collection_name)
        if not isinstance(raw_entries, list):
            return set()
        return {
            str(entry.get("id", "")).strip()
            for entry in raw_entries
            if isinstance(entry, dict) and str(entry.get("id", "")).strip()
        }

    sidecar_kind = str(payload.get("sidecar_kind", "")).strip()
    if sidecar_kind != "research":
        return failures

    question_ids = _collect_ids("questions")
    finding_ids = _collect_ids("findings")
    source_ids = _collect_ids("sources")

    raw_findings = payload.get("findings")
    if isinstance(raw_findings, list):
        for index, entry in enumerate(raw_findings):
            if not isinstance(entry, dict):
                continue
            for ref in entry.get("question_ids", []):
                ref_text = str(ref).strip()
                if ref_text and ref_text not in question_ids:
                    failures.append(
                        f"{path} findings[{index}].question_ids references unknown question id '{ref_text}'"
                    )
            for ref in entry.get("source_ids", []):
                ref_text = str(ref).strip()
                if ref_text and ref_text not in source_ids:
                    failures.append(
                        f"{path} findings[{index}].source_ids references unknown source id '{ref_text}'"
                    )

    raw_recommendations = payload.get("recommendations")
    if isinstance(raw_recommendations, list):
        for index, entry in enumerate(raw_recommendations):
            if not isinstance(entry, dict):
                continue
            for field, known_ids in (
                ("question_ids", question_ids),
                ("finding_ids", finding_ids),
                ("source_ids", source_ids),
            ):
                for ref in entry.get(field, []):
                    ref_text = str(ref).strip()
                    if ref_text and ref_text not in known_ids:
                        failures.append(
                            f"{path} recommendations[{index}].{field} references unknown id '{ref_text}'"
                        )

    raw_sources = payload.get("sources")
    if isinstance(raw_sources, list):
        for index, entry in enumerate(raw_sources):
            if not isinstance(entry, dict):
                continue
            source_path = str(entry.get("path", "")).strip()
            source_fingerprint = str(entry.get("fingerprint", "")).strip()
            if not source_path and not source_fingerprint:
                continue
            resolved_path = _resolve_repo_contained_path(source_path)
            if resolved_path is None:
                failures.append(
                    f"{path} sources[{index}].path must resolve inside repo root"
                )
                continue
            if not resolved_path.exists() or not resolved_path.is_file():
                failures.append(
                    f"{path} sources[{index}].path does not exist as a file"
                )
                continue
            if not source_fingerprint:
                failures.append(
                    f"{path} sources[{index}].fingerprint is required when path is set"
                )
                continue
            if _shared_path_fingerprint is not None:
                actual_fingerprint = _shared_path_fingerprint(
                    REPO_ROOT,
                    resolved_path.relative_to(REPO_ROOT).as_posix(),
                )
                if actual_fingerprint != source_fingerprint:
                    failures.append(
                        f"{path} sources[{index}].fingerprint does not match {resolved_path.relative_to(REPO_ROOT).as_posix()}"
                    )
    return failures


def _validate_context_sidecars(state: dict[str, Any]) -> list[str]:
    """Validate optional discuss/research sidecars when present."""
    failures: list[str] = []
    state_iteration_id = str(state.get("iteration_id", "")).strip()
    state_experiment_id = str(state.get("experiment_id", "")).strip()

    for (
        expected_scope_kind,
        _sidecar_kind,
        path,
        schema_key,
    ) in _iter_context_sidecar_specs(state):
        if not path.exists():
            continue
        try:
            payload = load_json(path)
        except Exception as exc:
            failures.append(f"{path} {exc}")
            continue

        failures.extend(_schema_validate(payload, schema_key=schema_key, path=path))
        failures.extend(
            _validate_timestamp(
                payload.get("generated_at"),
                path_label=f"{path} generated_at",
            )
        )
        failures.extend(_validate_sidecar_contract(path, payload))

        payload_scope_kind = str(payload.get("scope_kind", "")).strip()
        if payload_scope_kind and payload_scope_kind != expected_scope_kind:
            failures.append(
                f"{path} scope_kind '{payload_scope_kind}' does not match {expected_scope_kind} sidecar location"
            )

        resolved_scope_root = _resolve_repo_contained_path(payload.get("scope_root"))
        expected_scope_root = _expected_sidecar_scope_root(
            expected_scope_kind=expected_scope_kind,
            state=state,
        )
        if payload.get("scope_root") and resolved_scope_root is None:
            failures.append(f"{path} scope_root must resolve inside repo root")
        elif expected_scope_root is not None and resolved_scope_root is not None:
            if resolved_scope_root != expected_scope_root:
                failures.append(
                    f"{path} scope_root mismatch ({resolved_scope_root} != {expected_scope_root})"
                )

        if expected_scope_kind != "experiment":
            continue
        payload_iteration_id = str(payload.get("iteration_id", "")).strip()
        if (
            state_iteration_id
            and payload_iteration_id
            and payload_iteration_id != state_iteration_id
        ):
            failures.append(f"{path} iteration_id mismatch")
        payload_experiment_id = str(payload.get("experiment_id", "")).strip()
        if (
            state_experiment_id
            and payload_experiment_id
            and payload_experiment_id != state_experiment_id
        ):
            failures.append(f"{path} experiment_id mismatch")

    return failures


def _resolve_bundle_path(raw_path: Any) -> Path | None:
    candidate = str(raw_path or "").strip()
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path


def _validate_codebase_context_maps(_state: dict[str, Any]) -> list[str]:
    """Validate optional brownfield context artifacts when present."""
    bundle_path = REPO_ROOT / ".autolab" / "context" / "bundle.json"
    if not bundle_path.exists():
        return []

    try:
        bundle_payload = load_json(bundle_path)
    except Exception as exc:
        return [f"{bundle_path} {exc}"]
    failures = _schema_validate(
        bundle_payload,
        schema_key="codebase_context_bundle",
        path=bundle_path,
    )

    project_map_path = _resolve_bundle_path(bundle_payload.get("project_map_path"))
    if project_map_path is not None:
        try:
            project_payload = load_json(project_map_path)
        except Exception as exc:
            failures.append(f"{project_map_path} {exc}")
            project_payload = None
        if isinstance(project_payload, dict):
            failures.extend(
                _schema_validate(
                    project_payload,
                    schema_key="codebase_project_map",
                    path=project_map_path,
                )
            )

    delta_paths: list[Path] = []
    selected_delta_path = _resolve_bundle_path(
        bundle_payload.get("selected_experiment_delta_path")
    )
    if selected_delta_path is not None:
        delta_paths.append(selected_delta_path)
    raw_delta_maps = bundle_payload.get("experiment_delta_maps")
    if isinstance(raw_delta_maps, list):
        for entry in raw_delta_maps:
            if not isinstance(entry, dict):
                continue
            resolved = _resolve_bundle_path(entry.get("path"))
            if resolved is not None:
                delta_paths.append(resolved)

    deduped_paths: list[Path] = []
    seen: set[str] = set()
    for path in delta_paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped_paths.append(path)

    for delta_path in deduped_paths:
        try:
            delta_payload = load_json(delta_path)
        except Exception as exc:
            failures.append(f"{delta_path} {exc}")
            continue
        failures.extend(
            _schema_validate(
                delta_payload,
                schema_key="codebase_experiment_delta",
                path=delta_path,
            )
        )
    return failures


def _validate_plan_contract(state: dict[str, Any], *, stage: str) -> list[str]:
    """Validate implementation plan contract artifacts."""
    if stage not in {"implementation", "implementation_review"}:
        return []
    iteration_dir = _iteration_dir(state)
    errors: list[str] = []

    canonical_path = REPO_ROOT / ".autolab" / "plan_contract.json"
    snapshot_path = iteration_dir / "plan_contract.json"

    for path in (canonical_path, snapshot_path):
        try:
            payload = load_json(path)
        except Exception as exc:
            errors.append(f"{path} {exc}")
            continue
        errors.extend(_schema_validate(payload, schema_key="plan_contract", path=path))
        if (
            str(payload.get("iteration_id", "")).strip()
            and str(payload.get("iteration_id", "")).strip()
            != str(state.get("iteration_id", "")).strip()
        ):
            errors.append(f"{path} iteration_id mismatch")
        if str(payload.get("stage", "")).strip() not in {"", "implementation"}:
            errors.append(f"{path} stage must be 'implementation'")
    return errors


def _validate_plan_checker_outputs(state: dict[str, Any], *, stage: str) -> list[str]:
    """Validate checker outputs when they exist (or are required in implementation stage)."""
    if stage not in {"implementation", "implementation_review"}:
        return []
    errors: list[str] = []
    require_artifacts = stage == "implementation"
    for schema_key, path in (
        ("plan_check_result", REPO_ROOT / ".autolab" / "plan_check_result.json"),
        ("plan_graph", REPO_ROOT / ".autolab" / "plan_graph.json"),
    ):
        if not path.exists():
            if require_artifacts:
                errors.append(f"{path} [Errno 2] No such file or directory")
            continue
        try:
            payload = load_json(path)
        except Exception as exc:
            errors.append(f"{path} {exc}")
            continue
        errors.extend(_schema_validate(payload, schema_key=schema_key, path=path))
        if (
            str(payload.get("iteration_id", "")).strip()
            and str(payload.get("iteration_id", "")).strip()
            != str(state.get("iteration_id", "")).strip()
        ):
            errors.append(f"{path} iteration_id mismatch")
    return errors


def _validate_plan_approval(state: dict[str, Any], *, stage: str) -> list[str]:
    if stage not in {"implementation", "implementation_review"}:
        return []
    iteration_dir = _iteration_dir(state)
    path = iteration_dir / "plan_approval.json"
    if not path.exists():
        return []
    try:
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    failures = _schema_validate(payload, schema_key="plan_approval", path=path)
    failures.extend(
        _validate_timestamp(
            payload.get("generated_at"),
            path_label=f"{path} generated_at",
        )
    )
    reviewed_at = str(payload.get("reviewed_at", "")).strip()
    if reviewed_at:
        failures.extend(
            _validate_timestamp(
                reviewed_at,
                path_label=f"{path} reviewed_at",
            )
        )
    if (
        str(payload.get("iteration_id", "")).strip()
        and str(payload.get("iteration_id", "")).strip()
        != str(state.get("iteration_id", "")).strip()
    ):
        failures.append(f"{path} iteration_id mismatch")
    return failures


def _validate_uat(state: dict[str, Any], *, stage: str) -> list[str]:
    if stage not in {"implementation_review", "launch"}:
        return []
    if _shared_resolve_uat_requirement is None:
        return []
    iteration_dir = _iteration_dir(state)
    repo_root = REPO_ROOT
    plan_approval_payload: dict[str, Any] | None = None
    plan_approval_path = iteration_dir / "plan_approval.json"
    if plan_approval_path.exists():
        try:
            loaded = load_json(plan_approval_path)
        except Exception as exc:
            return [f"{plan_approval_path} {exc}"]
        if isinstance(loaded, dict):
            plan_approval_payload = loaded

    summary = _shared_resolve_uat_requirement(
        repo_root,
        iteration_dir,
        plan_approval_payload=plan_approval_payload,
    )
    if not bool(summary.get("effective_required", False)):
        return []

    failures: list[str] = []
    path = Path(str(summary.get("artifact_path", iteration_dir / "uat.md")))
    status = str(summary.get("status", "")).strip().lower()
    if status == "missing":
        return [f"{path} is required but missing"]
    if status == "invalid":
        for error in summary.get("errors", []):
            detail = str(error).strip()
            if detail:
                failures.append(f"{path} invalid: {detail}")
        return failures or [f"{path} is invalid"]

    if stage in {"implementation_review", "launch"}:
        if _shared_ensure_uat_pass is None:
            return []
        try:
            _shared_ensure_uat_pass(
                repo_root,
                iteration_dir,
                stage_label=stage,
                plan_approval_payload=plan_approval_payload,
            )
        except Exception as exc:
            failures.append(str(exc))
    return failures


def _validate_design_context_quality(state: dict[str, Any], *, stage: str) -> list[str]:
    if stage != "design":
        return []
    path = _iteration_dir(state) / "design_context_quality.json"
    if not path.exists():
        return []
    try:
        payload = load_json(path)
    except Exception as exc:
        return [f"{path} {exc}"]
    failures = _schema_validate(
        payload,
        schema_key="design_context_quality",
        path=path,
    )
    failures.extend(
        _validate_timestamp(
            payload.get("generated_at"),
            path_label=f"{path} generated_at",
        )
    )
    if (
        str(payload.get("iteration_id", "")).strip()
        and str(payload.get("iteration_id", "")).strip()
        != str(state.get("iteration_id", "")).strip()
    ):
        failures.append(f"{path} iteration_id mismatch")
    if (
        str(payload.get("experiment_id", "")).strip()
        and str(payload.get("experiment_id", "")).strip()
        != str(state.get("experiment_id", "")).strip()
    ):
        failures.append(f"{path} experiment_id mismatch")
    return failures


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
    failures.extend(_validate_parser_capabilities(state, policy, stage=stage))
    failures.extend(_validate_review_result(state, policy, stage=stage))
    failures.extend(_validate_run_manifest(state, stage=stage))
    failures.extend(_validate_replicate_run_manifests(state, stage=stage))
    failures.extend(_validate_metrics(state, stage=stage))
    failures.extend(_validate_decision_result(state, stage=stage))
    failures.extend(_validate_todo_state(state))
    failures.extend(_validate_todo_focus(state))
    failures.extend(_validate_plan_metadata(state))
    failures.extend(_validate_plan_execution_summary(state))
    failures.extend(_validate_plan_execution_state(state))
    failures.extend(_validate_handoff(state))
    failures.extend(_validate_context_sidecars(state))
    failures.extend(_validate_codebase_context_maps(state))
    failures.extend(_validate_plan_contract(state, stage=stage))
    failures.extend(_validate_plan_checker_outputs(state, stage=stage))
    failures.extend(_validate_plan_approval(state, stage=stage))
    failures.extend(_validate_uat(state, stage=stage))
    failures.extend(_validate_design_context_quality(state, stage=stage))

    warnings: list[str] = []
    traceability_issues = []
    traceability_issues.extend(_validate_traceability_coverage(state))
    traceability_issues.extend(_validate_traceability_latest(state))
    if _is_traceability_schema_strict(policy):
        failures.extend(traceability_issues)
    else:
        warnings.extend(traceability_issues)

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
            "warnings": warnings,
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
                    "handoff": "Regenerate via `autolab handoff` or verify required handoff fields in .autolab/handoff.json",
                    "sidecars/": "Ensure optional discuss/research sidecar metadata matches scope and every collection item includes id and summary",
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
            if warnings:
                print("\nAdvisory warnings:")
                for warning in warnings:
                    print(f"- {warning}")
        else:
            if warnings:
                print("schema_checks: PASS (with advisory warnings)")
                for warning in warnings:
                    print(f"- {warning}")
            else:
                print("schema_checks: PASS")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
