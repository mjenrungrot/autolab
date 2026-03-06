from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from autolab.config import _load_plan_execution_config
from autolab.models import StageCheckError
from autolab.plan_approval import (
    approval_is_current,
    build_plan_hash,
    build_risk_fingerprint,
    load_plan_approval,
    write_plan_approval,
)
from autolab.plan_contract import PlanContractError, check_implementation_plan_contract
from autolab.runners import _invoke_agent_runner
from autolab.sidecar_tools import build_task_context_guidance
from autolab.scope import _resolve_project_wide_root
from autolab.state import _resolve_iteration_directory
from autolab.utils import (
    _append_log,
    _collect_change_snapshot,
    _snapshot_delta_paths,
    _utc_now,
    _write_json,
)
from autolab.wave_observability import build_wave_observability


_SYSTEM_ALLOWED_PREFIXES: tuple[str, ...] = (
    ".autolab/runner_execution_report",
    ".autolab/prompts/rendered/",
    ".autolab/prompts/rendered",
    ".autolab/plan_check_result.json",
    ".autolab/plan_graph.json",
)


@dataclass(frozen=True)
class ImplementationExecutionStepResult:
    handled: bool
    proceed_to_evaluate: bool
    agent_status: str
    exit_code: int
    summary: str
    changed_files: tuple[Path, ...]
    next_stage: str | None = None
    pause_reason: str = ""


def _normalize_path(value: str) -> str:
    return str(value).replace("\\", "/").strip().strip("/")


def _coerce_exit_code(value: Any, *, default: int = 1) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _task_failure_policy(task: dict[str, Any]) -> str:
    policy = str(task.get("failure_policy", "")).strip().lower()
    return policy or "fail_fast"


def _json_hash(payload: dict[str, Any]) -> str:
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _derive_approval_risk_fallback(
    *,
    task_map: dict[str, dict[str, Any]],
    wave_rows: list[dict[str, Any]],
    state: dict[str, Any],
    raw_execution_state: dict[str, Any] | None,
    impl_cfg: Any,
) -> dict[str, Any]:
    approval_cfg = getattr(impl_cfg, "approval", None)
    project_wide_task_ids = sorted(
        task_id
        for task_id, task in task_map.items()
        if str(task.get("scope_kind", "")).strip().lower() == "project_wide"
    )
    project_wide_unique_paths = sorted(
        {
            _normalize_path(path)
            for task in task_map.values()
            if str(task.get("scope_kind", "")).strip().lower() == "project_wide"
            for field in ("writes", "touches")
            for path in (
                task.get(field, []) if isinstance(task.get(field, []), list) else []
            )
            if _normalize_path(path)
        }
    )
    observed_retries = 0
    if isinstance(raw_execution_state, dict):
        for field in ("task_retry_counts", "wave_retry_counts"):
            raw_counts = raw_execution_state.get(field)
            if not isinstance(raw_counts, dict):
                continue
            for value in raw_counts.values():
                try:
                    observed_retries += max(int(value or 0), 0)
                except Exception:
                    continue
    try:
        stage_attempt = max(int(state.get("stage_attempt", 0) or 0), 0)
    except Exception:
        stage_attempt = 0
    trigger_reasons: list[str] = []
    if approval_cfg is not None and getattr(approval_cfg, "enabled", False):
        if (
            getattr(approval_cfg, "require_for_project_wide_tasks", False)
            and project_wide_task_ids
        ):
            trigger_reasons.append("project_wide_tasks_present")
        if len(task_map) > int(getattr(approval_cfg, "max_tasks_without_approval", 6)):
            trigger_reasons.append("task_count_exceeds_threshold")
        if len(wave_rows) > int(getattr(approval_cfg, "max_waves_without_approval", 2)):
            trigger_reasons.append("wave_count_exceeds_threshold")
        if len(project_wide_unique_paths) > int(
            getattr(approval_cfg, "max_project_wide_paths_without_approval", 3)
        ):
            trigger_reasons.append("project_wide_blast_radius_exceeds_threshold")
        if getattr(approval_cfg, "require_after_retries", True) and (
            observed_retries > 0 or stage_attempt > 0
        ):
            trigger_reasons.append("prior_retries_observed")
    approval_risk = {
        "requires_approval": bool(trigger_reasons),
        "trigger_reasons": trigger_reasons,
        "counts": {
            "tasks_total": len(task_map),
            "waves_total": len(wave_rows),
            "project_wide_tasks": len(project_wide_task_ids),
            "project_wide_unique_paths": len(project_wide_unique_paths),
            "observed_retries": observed_retries,
            "stage_attempt": stage_attempt,
        },
        "project_wide_task_ids": project_wide_task_ids,
        "project_wide_unique_paths": project_wide_unique_paths,
        "policy": {
            "enabled": bool(getattr(approval_cfg, "enabled", False)),
            "require_for_project_wide_tasks": bool(
                getattr(approval_cfg, "require_for_project_wide_tasks", False)
            ),
            "max_tasks_without_approval": int(
                getattr(approval_cfg, "max_tasks_without_approval", 6)
            ),
            "max_waves_without_approval": int(
                getattr(approval_cfg, "max_waves_without_approval", 2)
            ),
            "max_project_wide_paths_without_approval": int(
                getattr(approval_cfg, "max_project_wide_paths_without_approval", 3)
            ),
            "require_after_retries": bool(
                getattr(approval_cfg, "require_after_retries", True)
            ),
        },
    }
    approval_risk["risk_fingerprint"] = build_risk_fingerprint(approval_risk)
    return approval_risk


def _load_design_yaml_mapping(iteration_dir: Path) -> dict[str, Any]:
    if yaml is None:
        return {}
    design_path = iteration_dir / "design.yaml"
    if not design_path.exists():
        return {}
    try:
        payload = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _load_json_dict(path: Path) -> dict[str, Any]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageCheckError(f"invalid JSON at {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise StageCheckError(f"expected JSON object at {path}")
    return loaded


def _task_surfaces(task: dict[str, Any]) -> list[str]:
    surfaces: list[str] = []
    for field in ("writes", "touches"):
        raw_values = task.get(field, [])
        if not isinstance(raw_values, list):
            continue
        for raw in raw_values:
            normalized = _normalize_path(str(raw))
            if normalized and normalized not in surfaces:
                surfaces.append(normalized)
    return surfaces


def _path_within_surface(path: str, surface: str) -> bool:
    normalized_path = _normalize_path(path)
    normalized_surface = _normalize_path(surface)
    if not normalized_path or not normalized_surface:
        return False
    return normalized_path == normalized_surface or normalized_path.startswith(
        f"{normalized_surface}/"
    )


def _path_allowed_system(path: str) -> bool:
    normalized = _normalize_path(path)
    if not normalized:
        return True
    for prefix in _SYSTEM_ALLOWED_PREFIXES:
        normalized_prefix = _normalize_path(prefix)
        if normalized.startswith(normalized_prefix):
            return True
    return False


def _paths_within_declared_surfaces(
    paths: list[str], declared_surfaces: list[str]
) -> list[str]:
    violations: list[str] = []
    for path in paths:
        normalized = _normalize_path(path)
        if not normalized:
            continue
        if _path_allowed_system(normalized):
            continue
        if any(
            _path_within_surface(normalized, surface) for surface in declared_surfaces
        ):
            continue
        violations.append(normalized)
    return sorted(set(violations))


def _task_map(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    tasks_raw = contract.get("tasks")
    if not isinstance(tasks_raw, list) or not tasks_raw:
        raise StageCheckError("plan_contract.json must contain non-empty tasks list")
    mapped: dict[str, dict[str, Any]] = {}
    for entry in tasks_raw:
        if not isinstance(entry, dict):
            continue
        task_id = str(entry.get("task_id", "")).strip()
        if not task_id:
            continue
        mapped[task_id] = entry
    if not mapped:
        raise StageCheckError(
            "plan_contract.json does not contain valid task_id entries"
        )
    return mapped


def _wave_rows(graph: dict[str, Any]) -> list[dict[str, Any]]:
    raw_waves = graph.get("waves")
    if not isinstance(raw_waves, list) or not raw_waves:
        raise StageCheckError("plan_graph.json must contain non-empty waves list")
    rows: list[dict[str, Any]] = []
    for row in raw_waves:
        if not isinstance(row, dict):
            continue
        wave = int(row.get("wave", 0) or 0)
        tasks = row.get("tasks")
        if wave <= 0 or not isinstance(tasks, list):
            continue
        task_ids = [str(task_id).strip() for task_id in tasks if str(task_id).strip()]
        if not task_ids:
            continue
        rows.append({"wave": wave, "tasks": task_ids})
    if not rows:
        raise StageCheckError("plan_graph.json does not contain valid wave rows")
    rows.sort(key=lambda item: int(item["wave"]))
    return rows


def _execution_state_path(iteration_dir: Path) -> Path:
    return iteration_dir / "plan_execution_state.json"


def _execution_summary_path(iteration_dir: Path) -> Path:
    return iteration_dir / "plan_execution_summary.json"


def _initial_execution_state(
    *,
    iteration_id: str,
    contract_hash: str,
    contract_path: str,
    plan_file: str,
    wave_rows: list[dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    run_unit: str,
) -> dict[str, Any]:
    now = _utc_now()
    return {
        "schema_version": "1.0",
        "generated_at": now,
        "updated_at": now,
        "stage": "implementation",
        "iteration_id": iteration_id,
        "contract_path": contract_path,
        "contract_hash": contract_hash,
        "plan_file": plan_file,
        "run_unit": run_unit,
        "waves_total": len(wave_rows),
        "task_status": {task_id: "pending" for task_id in sorted(tasks)},
        "task_attempt_counts": {task_id: 0 for task_id in sorted(tasks)},
        "task_retry_counts": {task_id: 0 for task_id in sorted(tasks)},
        "task_last_error": {task_id: "" for task_id in sorted(tasks)},
        "task_files_changed": {task_id: [] for task_id in sorted(tasks)},
        "task_started_at": {task_id: "" for task_id in sorted(tasks)},
        "task_completed_at": {task_id: "" for task_id in sorted(tasks)},
        "task_duration_seconds": {task_id: 0.0 for task_id in sorted(tasks)},
        "task_reason_code": {task_id: "" for task_id in sorted(tasks)},
        "task_reason_detail": {task_id: "" for task_id in sorted(tasks)},
        "task_runner_report_path": {task_id: "" for task_id in sorted(tasks)},
        "task_verification_status": {task_id: "" for task_id in sorted(tasks)},
        "task_verification_commands": {task_id: [] for task_id in sorted(tasks)},
        "task_expected_artifacts_missing": {task_id: [] for task_id in sorted(tasks)},
        "task_blocked_by": {task_id: [] for task_id in sorted(tasks)},
        "wave_retry_counts": {str(row["wave"]): 0 for row in wave_rows},
        "wave_status": {str(row["wave"]): "pending" for row in wave_rows},
        "wave_started_at": {str(row["wave"]): "" for row in wave_rows},
        "wave_completed_at": {str(row["wave"]): "" for row in wave_rows},
        "wave_duration_seconds": {str(row["wave"]): 0.0 for row in wave_rows},
        "wave_attempt_history": {str(row["wave"]): [] for row in wave_rows},
        "wave_retry_reasons": {str(row["wave"]): [] for row in wave_rows},
        "wave_out_of_contract_paths": {str(row["wave"]): [] for row in wave_rows},
        "current_wave": 1,
    }


def _coerce_execution_state(
    *,
    iteration_id: str,
    contract_hash: str,
    contract_path: str,
    plan_file: str,
    wave_rows: list[dict[str, Any]],
    tasks: dict[str, dict[str, Any]],
    run_unit: str,
    raw_state: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(raw_state, dict):
        return _initial_execution_state(
            iteration_id=iteration_id,
            contract_hash=contract_hash,
            contract_path=contract_path,
            plan_file=plan_file,
            wave_rows=wave_rows,
            tasks=tasks,
            run_unit=run_unit,
        )
    if str(raw_state.get("contract_hash", "")).strip() != contract_hash:
        return _initial_execution_state(
            iteration_id=iteration_id,
            contract_hash=contract_hash,
            contract_path=contract_path,
            plan_file=plan_file,
            wave_rows=wave_rows,
            tasks=tasks,
            run_unit=run_unit,
        )

    state = dict(raw_state)
    state["schema_version"] = "1.0"
    state["stage"] = "implementation"
    state["iteration_id"] = iteration_id
    state["contract_hash"] = contract_hash
    state["contract_path"] = contract_path
    state["plan_file"] = plan_file
    state["run_unit"] = run_unit
    state["waves_total"] = len(wave_rows)
    state["updated_at"] = _utc_now()

    def _ensure_status_map(key: str, default_value: Any) -> dict[str, Any]:
        def _clone_default() -> Any:
            if isinstance(default_value, list):
                return list(default_value)
            if isinstance(default_value, dict):
                return dict(default_value)
            return default_value

        raw_map = state.get(key)
        normalized: dict[str, Any] = {}
        if isinstance(raw_map, dict):
            for task_id in sorted(tasks):
                value = raw_map.get(task_id, _clone_default())
                if isinstance(value, list):
                    value = list(value)
                elif isinstance(value, dict):
                    value = dict(value)
                normalized[task_id] = value
        else:
            for task_id in sorted(tasks):
                normalized[task_id] = _clone_default()
        state[key] = normalized
        return normalized

    _ensure_status_map("task_status", "pending")
    _ensure_status_map("task_attempt_counts", 0)
    _ensure_status_map("task_retry_counts", 0)
    _ensure_status_map("task_last_error", "")
    _ensure_status_map("task_files_changed", [])
    _ensure_status_map("task_started_at", "")
    _ensure_status_map("task_completed_at", "")
    _ensure_status_map("task_duration_seconds", 0.0)
    _ensure_status_map("task_reason_code", "")
    _ensure_status_map("task_reason_detail", "")
    _ensure_status_map("task_runner_report_path", "")
    _ensure_status_map("task_verification_status", "")
    _ensure_status_map("task_verification_commands", [])
    _ensure_status_map("task_expected_artifacts_missing", [])
    _ensure_status_map("task_blocked_by", [])

    raw_wave_retry = state.get("wave_retry_counts")
    wave_retry_counts: dict[str, int] = {}
    if isinstance(raw_wave_retry, dict):
        for row in wave_rows:
            key = str(row["wave"])
            try:
                wave_retry_counts[key] = int(raw_wave_retry.get(key, 0) or 0)
            except Exception:
                wave_retry_counts[key] = 0
    else:
        for row in wave_rows:
            wave_retry_counts[str(row["wave"])] = 0
    state["wave_retry_counts"] = wave_retry_counts

    raw_wave_status = state.get("wave_status")
    wave_status: dict[str, str] = {}
    if isinstance(raw_wave_status, dict):
        for row in wave_rows:
            key = str(row["wave"])
            wave_status[key] = (
                str(raw_wave_status.get(key, "pending")).strip() or "pending"
            )
    else:
        for row in wave_rows:
            wave_status[str(row["wave"])] = "pending"
    state["wave_status"] = wave_status

    def _ensure_wave_map(key: str, default_value: Any) -> dict[str, Any]:
        def _clone_default() -> Any:
            if isinstance(default_value, list):
                return list(default_value)
            if isinstance(default_value, dict):
                return dict(default_value)
            return default_value

        raw_map = state.get(key)
        normalized: dict[str, Any] = {}
        if isinstance(raw_map, dict):
            for row in wave_rows:
                wave_key = str(row["wave"])
                value = raw_map.get(wave_key, _clone_default())
                if isinstance(value, list):
                    value = list(value)
                elif isinstance(value, dict):
                    value = dict(value)
                normalized[wave_key] = value
        else:
            for row in wave_rows:
                wave_key = str(row["wave"])
                normalized[wave_key] = _clone_default()
        state[key] = normalized
        return normalized

    _ensure_wave_map("wave_started_at", "")
    _ensure_wave_map("wave_completed_at", "")
    _ensure_wave_map("wave_duration_seconds", 0.0)
    _ensure_wave_map("wave_attempt_history", [])
    _ensure_wave_map("wave_retry_reasons", [])
    _ensure_wave_map("wave_out_of_contract_paths", [])
    return state


def _next_wave(
    *,
    wave_rows: list[dict[str, Any]],
    task_status: dict[str, Any],
) -> dict[str, Any] | None:
    for row in wave_rows:
        task_ids = row["tasks"]
        if any(
            str(task_status.get(task_id, "pending")) != "completed"
            for task_id in task_ids
        ):
            return row
    return None


def _task_ready(
    *,
    task: dict[str, Any],
    task_status: dict[str, Any],
) -> bool:
    depends_on = task.get("depends_on", [])
    if not isinstance(depends_on, list):
        return False
    for dependency in depends_on:
        dep_id = str(dependency).strip()
        if not dep_id:
            continue
        if str(task_status.get(dep_id, "")) != "completed":
            return False
    return True


def _task_expected_artifacts_missing(
    *,
    repo_root: Path,
    iteration_dir: Path,
    scope_root: Path,
    task: dict[str, Any],
) -> list[str]:
    scope_root_resolved = scope_root.resolve()

    def _within_scope_root(path: Path) -> bool:
        try:
            path.resolve().relative_to(scope_root_resolved)
            return True
        except ValueError:
            return False

    missing: list[str] = []
    raw_artifacts = task.get("expected_artifacts", [])
    scope_kind = str(task.get("scope_kind", "")).strip().lower()
    if not isinstance(raw_artifacts, list):
        return missing
    for raw_artifact in raw_artifacts:
        artifact = str(raw_artifact).strip()
        if not artifact:
            continue
        artifact_path = Path(artifact)
        candidates: list[Path] = []
        if artifact_path.is_absolute():
            candidates.append(artifact_path)
        else:
            candidates.extend(
                [
                    scope_root / artifact_path,
                    iteration_dir / artifact_path,
                    repo_root / artifact_path,
                ]
            )
        if scope_kind == "project_wide":
            candidates = [
                candidate for candidate in candidates if _within_scope_root(candidate)
            ]
        if any(candidate.exists() for candidate in candidates):
            continue
        missing.append(artifact)
    return missing


def _substitute_task_command(
    command: str,
    *,
    iteration_id: str,
    iteration_path: str,
    task_id: str,
    scope_root: str,
) -> str:
    substituted = str(command)
    replacements = {
        "{{iteration_id}}": iteration_id,
        "{iteration_id}": iteration_id,
        "<ITERATION_ID>": iteration_id,
        "{{iteration_path}}": iteration_path,
        "{iteration_path}": iteration_path,
        "<ITERATION_PATH>": iteration_path,
        "{{task_id}}": task_id,
        "{task_id}": task_id,
        "<TASK_ID>": task_id,
        "{{scope_root}}": scope_root,
        "{scope_root}": scope_root,
        "<SCOPE_ROOT>": scope_root,
    }
    for token, value in replacements.items():
        substituted = substituted.replace(token, value)
    return substituted


def _run_task_verification_commands(
    *,
    repo_root: Path,
    task_id: str,
    commands: list[str],
) -> tuple[bool, str]:
    for command in commands:
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return (False, f"{task_id}: verification command parse failed: {exc}")
        if not argv:
            continue
        try:
            result = subprocess.run(
                argv,
                cwd=repo_root,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return (
                False,
                f"{task_id}: verification failed (`{command}`): command timed out after 300s",
            )
        except Exception as exc:
            return (
                False,
                f"{task_id}: verification failed (`{command}`): {exc}",
            )
        if result.returncode != 0:
            stderr_text = " ".join(str(result.stderr or "").split())
            stdout_text = " ".join(str(result.stdout or "").split())
            detail = stderr_text or stdout_text or "verification command failed"
            return (
                False,
                f"{task_id}: verification failed (`{command}`): {detail[:320]}",
            )
    return (True, "")


def _execute_task(
    repo_root: Path,
    *,
    state_path: Path,
    iteration_id: str,
    experiment_id: str,
    iteration_path: str,
    iteration_dir: Path,
    project_wide_root: Path,
    wave: int,
    task: dict[str, Any],
    task_retry_max: int,
    require_verification_commands: bool,
) -> dict[str, Any]:
    task_id = str(task.get("task_id", "")).strip() or "task"
    task_scope_kind = str(task.get("scope_kind", "")).strip().lower()
    task_scope_root = (
        project_wide_root if task_scope_kind == "project_wide" else iteration_dir
    )
    design_payload = _load_design_yaml_mapping(iteration_dir)
    task_sidecar_guidance = build_task_context_guidance(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        task_packet=task,
        design_payload=design_payload,
    )
    manual_only_rationale = str(task.get("manual_only_rationale", "")).strip()
    raw_verification_commands = task.get("verification_commands", [])
    rendered_verification_commands: list[str] = []
    if isinstance(raw_verification_commands, list):
        for raw_command in raw_verification_commands:
            command = _substitute_task_command(
                str(raw_command).strip(),
                iteration_id=iteration_id,
                iteration_path=iteration_path,
                task_id=task_id,
                scope_root=task_scope_root.as_posix(),
            )
            if command:
                rendered_verification_commands.append(command)
    attempts = 0
    last_error = ""
    last_files_changed: list[str] = []
    verification_status = "not_run"
    runner_report_path = f".autolab/runner_execution_report.{task_id}.json"
    runner_status = "skipped"
    runner_exit_code: int | None = 0
    reason_code = ""
    reason_detail = ""
    started_at = _utc_now()
    started_at_monotonic = time.monotonic()
    max_attempts = max(1, task_retry_max + 1)
    while attempts < max_attempts:
        attempts += 1
        try:
            runner_result = _invoke_agent_runner(
                repo_root,
                state_path=state_path,
                stage="implementation",
                iteration_id=iteration_id,
                run_agent_mode="force_on",
                auto_mode=False,
                task_packet=task,
                task_context={
                    "wave": wave,
                    "attempt": attempts,
                    "max_attempts": max_attempts,
                    "sidecar_context": task_sidecar_guidance,
                },
                report_name=Path(runner_report_path).name,
            )
        except subprocess.TimeoutExpired as exc:
            last_error = f"{task_id}: runner execution timed out: {exc}"
            runner_status = "timeout"
            runner_exit_code = None
            reason_code = "runner_failed"
            reason_detail = last_error
            continue
        except Exception as exc:
            last_error = f"{task_id}: runner execution error: {exc}"
            runner_status = "error"
            runner_exit_code = None
            reason_code = "runner_failed"
            reason_detail = last_error
            continue
        last_files_changed = [
            _normalize_path(path) for path in runner_result.get("changed_paths", [])
        ]
        runner_status = str(runner_result.get("status", "")).strip().lower()
        raw_runner_exit = runner_result.get("exit_code")
        runner_exit_code = int(raw_runner_exit) if raw_runner_exit is not None else None
        runner_exit = _coerce_exit_code(raw_runner_exit, default=1)
        if runner_status != "completed" or runner_exit != 0:
            last_error = (
                f"{task_id}: runner execution failed "
                f"(status={runner_status or 'unknown'}, exit_code={runner_exit})"
            )
            reason_code = "runner_failed"
            reason_detail = last_error
            continue

        if rendered_verification_commands:
            try:
                verified, verify_error = _run_task_verification_commands(
                    repo_root=repo_root,
                    task_id=task_id,
                    commands=rendered_verification_commands,
                )
            except subprocess.TimeoutExpired as exc:
                verified, verify_error = (
                    False,
                    f"{task_id}: verification timed out: {exc}",
                )
            except Exception as exc:
                verified, verify_error = (
                    False,
                    f"{task_id}: verification error: {exc}",
                )
            if not verified:
                last_error = verify_error
                verification_status = "failed"
                reason_code = "verification_failed"
                reason_detail = verify_error
                continue
            verification_status = "passed"
        elif require_verification_commands and not manual_only_rationale:
            last_error = f"{task_id}: verification_commands required by policy when manual_only_rationale is absent"
            verification_status = "required_missing"
            reason_code = "verification_failed"
            reason_detail = last_error
            continue
        else:
            verification_status = "not_run"

        completed_at = _utc_now()
        duration_seconds = round(time.monotonic() - started_at_monotonic, 3)
        return {
            "task_id": task_id,
            "status": "completed",
            "attempts": attempts,
            "retries_used": max(0, attempts - 1),
            "error": "",
            "files_changed": sorted(set(last_files_changed)),
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": max(0.0, duration_seconds),
            "reason_code": "completed",
            "reason_detail": "",
            "runner_report_path": runner_report_path,
            "runner_status": runner_status,
            "runner_exit_code": runner_exit_code,
            "verification_status": verification_status,
            "verification_commands": list(rendered_verification_commands),
            "expected_artifacts_missing": [],
            "blocked_by": [],
        }

    completed_at = _utc_now()
    duration_seconds = round(time.monotonic() - started_at_monotonic, 3)
    return {
        "task_id": task_id,
        "status": "failed",
        "attempts": attempts,
        "retries_used": max(0, attempts - 1),
        "error": last_error or f"{task_id}: execution failed",
        "files_changed": sorted(set(last_files_changed)),
        "started_at": started_at,
        "completed_at": completed_at,
        "duration_seconds": max(0.0, duration_seconds),
        "reason_code": reason_code or "task_exception",
        "reason_detail": reason_detail or last_error or f"{task_id}: execution failed",
        "runner_report_path": runner_report_path,
        "runner_status": runner_status,
        "runner_exit_code": runner_exit_code,
        "verification_status": verification_status,
        "verification_commands": list(rendered_verification_commands),
        "expected_artifacts_missing": [],
        "blocked_by": [],
    }


def _append_wave_attempt(
    state_payload: dict[str, Any],
    *,
    wave_key: str,
    started_at: str,
    completed_at: str,
    duration_seconds: float,
    status: str,
    retry_reason: str = "",
    detail: str = "",
) -> None:
    raw_history = state_payload.get("wave_attempt_history")
    if not isinstance(raw_history, dict):
        raw_history = {}
        state_payload["wave_attempt_history"] = raw_history
    attempts = raw_history.get(wave_key)
    if not isinstance(attempts, list):
        attempts = []
        raw_history[wave_key] = attempts
    attempt_number = len(attempts) + 1
    attempts.append(
        {
            "attempt": attempt_number,
            "status": status,
            "started_at": started_at,
            "completed_at": completed_at,
            "duration_seconds": round(max(0.0, duration_seconds), 3),
            "retry_reason": retry_reason,
            "detail": detail,
        }
    )
    wave_started_map = state_payload.get("wave_started_at")
    if (
        isinstance(wave_started_map, dict)
        and not str(wave_started_map.get(wave_key, "")).strip()
    ):
        wave_started_map[wave_key] = started_at
    wave_completed_map = state_payload.get("wave_completed_at")
    if isinstance(wave_completed_map, dict):
        wave_completed_map[wave_key] = completed_at
    wave_duration_map = state_payload.get("wave_duration_seconds")
    if isinstance(wave_duration_map, dict):
        prior = 0.0
        try:
            prior = float(wave_duration_map.get(wave_key, 0.0) or 0.0)
        except Exception:
            prior = 0.0
        wave_duration_map[wave_key] = round(prior + max(0.0, duration_seconds), 3)
    wave_retry_counts = state_payload.get("wave_retry_counts")
    if isinstance(wave_retry_counts, dict):
        wave_retry_counts[wave_key] = max(0, len(attempts) - 1)


def _wave_retry_reasons_for_tasks(
    *,
    wave_task_ids: list[str],
    state_payload: dict[str, Any],
) -> list[str]:
    reason_codes = state_payload.get("task_reason_code")
    if not isinstance(reason_codes, dict):
        return []
    reasons: list[str] = []
    for task_id in wave_task_ids:
        reason_code = str(reason_codes.get(task_id, "")).strip()
        if not reason_code or reason_code == "completed":
            continue
        if reason_code not in reasons:
            reasons.append(reason_code)
    return reasons


def _build_execution_summary(
    *,
    repo_root: Path,
    iteration_dir: Path,
    iteration_id: str,
    plan_file: str,
    contract_hash: str,
    contract_payload: dict[str, Any],
    graph_payload: dict[str, Any],
    plan_check_payload: dict[str, Any],
    wave_rows: list[dict[str, Any]],
    state_payload: dict[str, Any],
    task_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    base_summary = {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "stage": "implementation",
        "iteration_id": iteration_id,
        "plan_file": plan_file,
        "contract_hash": contract_hash,
        "run_unit": "wave",
        "tasks_total": len(task_map),
        "tasks_completed": 0,
        "tasks_failed": 0,
        "tasks_blocked": 0,
        "tasks_pending": 0,
        "waves_total": len(wave_rows),
        "waves_executed": 0,
        "wave_details": [],
        "task_details": [],
    }
    observability = build_wave_observability(
        repo_root,
        iteration_dir=iteration_dir,
        contract_payload=contract_payload,
        graph_payload=graph_payload,
        plan_check_payload=plan_check_payload,
        execution_state_payload=state_payload,
        execution_summary_payload=base_summary,
    )
    summary = observability.get("summary", {})
    if not isinstance(summary, dict):
        summary = {}
    wave_summary = observability.get("wave_summary", {})
    if not isinstance(wave_summary, dict):
        wave_summary = {}
    task_summary = observability.get("task_summary", {})
    if not isinstance(task_summary, dict):
        task_summary = {}
    base_summary.update(
        {
            "tasks_total": int(
                task_summary.get("total", len(task_map)) or len(task_map)
            ),
            "tasks_completed": int(task_summary.get("completed", 0) or 0),
            "tasks_failed": int(task_summary.get("failed", 0) or 0),
            "tasks_blocked": int(task_summary.get("blocked", 0) or 0),
            "tasks_pending": int(task_summary.get("pending", 0) or 0),
            "tasks_skipped": int(task_summary.get("skipped", 0) or 0),
            "tasks_deferred": int(task_summary.get("deferred", 0) or 0),
            "waves_total": int(
                wave_summary.get("total", len(wave_rows)) or len(wave_rows)
            ),
            "waves_executed": int(wave_summary.get("executed", 0) or 0),
            "wave_details": observability.get("waves", []),
            "task_details": observability.get("tasks", []),
            "critical_path": observability.get("critical_path", {}),
            "file_conflicts": observability.get("file_conflicts", []),
            "diagnostics": observability.get("diagnostics", []),
            "observability_summary": summary,
        }
    )
    return base_summary


def execute_implementation_plan_step(
    repo_root: Path,
    *,
    state_path: Path,
    state: dict[str, Any],
    run_agent_mode: str,
    auto_mode: bool = False,
    plan_only: bool = False,
    execute_approved_plan: bool = False,
) -> ImplementationExecutionStepResult:
    plan_execution = _load_plan_execution_config(repo_root)
    impl_cfg = plan_execution.implementation
    if not impl_cfg.enabled:
        return ImplementationExecutionStepResult(
            handled=False,
            proceed_to_evaluate=True,
            agent_status="complete",
            exit_code=0,
            summary="plan execution disabled by policy",
            changed_files=(),
        )

    iteration_id = str(state.get("iteration_id", "")).strip()
    if not iteration_id:
        raise StageCheckError("state.iteration_id is required for plan execution")
    experiment_id = str(state.get("experiment_id", "")).strip()
    iteration_dir, _ = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        require_exists=False,
    )
    try:
        iteration_path = iteration_dir.relative_to(repo_root).as_posix()
    except ValueError:
        iteration_path = iteration_dir.as_posix()
    project_wide_root = _resolve_project_wide_root(repo_root)

    changed_files: list[Path] = []

    contract_path = repo_root / ".autolab" / "plan_contract.json"
    graph_path = repo_root / ".autolab" / "plan_graph.json"
    plan_check_path = repo_root / ".autolab" / "plan_check_result.json"
    execution_state_path = _execution_state_path(iteration_dir)
    summary_path = _execution_summary_path(iteration_dir)
    approval_json_path = iteration_dir / "plan_approval.json"
    approval_md_path = iteration_dir / "plan_approval.md"
    raw_state: dict[str, Any] | None = None
    if execution_state_path.exists():
        loaded = _load_json_dict(execution_state_path)
        raw_state = loaded

    existing_approval = load_plan_approval(iteration_dir)
    planning_artifacts_exist = (
        contract_path.exists() and graph_path.exists() and plan_check_path.exists()
    )
    if execute_approved_plan:
        missing_artifacts = [
            path
            for path in (
                ".autolab/plan_contract.json" if not contract_path.exists() else "",
                ".autolab/plan_graph.json" if not graph_path.exists() else "",
                ".autolab/plan_check_result.json"
                if not plan_check_path.exists()
                else "",
            )
            if path
        ]
        if missing_artifacts:
            missing_summary = ", ".join(missing_artifacts)
            return ImplementationExecutionStepResult(
                handled=True,
                proceed_to_evaluate=False,
                agent_status="failed",
                exit_code=1,
                summary=(
                    "`autolab run --execute-approved-plan` requires current "
                    f"planning artifacts ({missing_summary}); rerun "
                    "`autolab run --plan-only` or `autolab run`"
                ),
                changed_files=tuple(changed_files),
            )
        if not existing_approval:
            return ImplementationExecutionStepResult(
                handled=True,
                proceed_to_evaluate=False,
                agent_status="failed",
                exit_code=1,
                summary=(
                    "`autolab run --execute-approved-plan` requires a current "
                    "`plan_approval.json`; rerun `autolab run --plan-only`"
                ),
                changed_files=tuple(changed_files),
            )
    approval_retry_requested = (
        str(existing_approval.get("status", "")).strip().lower() == "retry"
        and not execute_approved_plan
    )
    planning_needed = not planning_artifacts_exist
    if approval_retry_requested:
        planning_needed = True
        raw_state = None

    if planning_needed:
        planning_result = _invoke_agent_runner(
            repo_root,
            state_path=state_path,
            stage="implementation",
            iteration_id=iteration_id,
            run_agent_mode=run_agent_mode,
            auto_mode=auto_mode,
            report_name="runner_execution_report.plan.json",
        )
        changed_files.append(
            repo_root
            / ".autolab"
            / planning_result.get("report_name", "runner_execution_report.plan.json")
        )
        planning_status = str(planning_result.get("status", "")).strip().lower()
        planning_exit = _coerce_exit_code(planning_result.get("exit_code"), default=1)
        if planning_status != "completed" or planning_exit != 0:
            return ImplementationExecutionStepResult(
                handled=True,
                proceed_to_evaluate=False,
                agent_status="needs_retry",
                exit_code=1,
                summary=(
                    "implementation planning pass failed; "
                    "rerun stage after fixing planner output"
                ),
                changed_files=tuple(changed_files),
            )

    try:
        contract_passed, contract_message, contract_details = (
            check_implementation_plan_contract(
                repo_root,
                state,
                stage_override="implementation",
                write_outputs=not execute_approved_plan,
            )
        )
    except PlanContractError as exc:
        raise StageCheckError(str(exc)) from exc
    if not execute_approved_plan:
        changed_files.extend(
            [
                repo_root / ".autolab" / "plan_check_result.json",
                repo_root / ".autolab" / "plan_graph.json",
            ]
        )
    if not contract_passed:
        return ImplementationExecutionStepResult(
            handled=True,
            proceed_to_evaluate=False,
            agent_status="needs_retry",
            exit_code=1,
            summary=contract_message,
            changed_files=tuple(changed_files),
        )

    if not contract_path.exists():
        raise StageCheckError(
            "missing .autolab/plan_contract.json after contract check"
        )
    if not graph_path.exists():
        raise StageCheckError("missing .autolab/plan_graph.json after contract check")
    if not plan_check_path.exists():
        raise StageCheckError(
            "missing .autolab/plan_check_result.json after contract check"
        )

    contract = _load_json_dict(contract_path)
    graph = _load_json_dict(graph_path)
    plan_check = _load_json_dict(plan_check_path)
    contract_hash = _json_hash(contract)
    task_map = _task_map(contract)
    wave_rows = _wave_rows(graph)
    approval_risk = plan_check.get("approval_risk")
    plan_hash = str(plan_check.get("plan_hash", "")).strip() or build_plan_hash(
        contract_payload=contract,
        graph_payload=graph,
    )
    if execute_approved_plan:
        if not isinstance(approval_risk, dict):
            return ImplementationExecutionStepResult(
                handled=True,
                proceed_to_evaluate=False,
                agent_status="failed",
                exit_code=1,
                summary=(
                    "`autolab run --execute-approved-plan` requires a current "
                    "approval risk in `.autolab/plan_check_result.json`; rerun "
                    "`autolab run --plan-only`"
                ),
                changed_files=tuple(changed_files),
            )
        current_approval_risk = contract_details.get("approval_risk")
        current_plan_hash = str(contract_details.get("plan_hash", "")).strip()
        if not isinstance(current_approval_risk, dict):
            return ImplementationExecutionStepResult(
                handled=True,
                proceed_to_evaluate=False,
                agent_status="failed",
                exit_code=1,
                summary=(
                    "`autolab run --execute-approved-plan` could not validate the "
                    "current approval risk; rerun `autolab run --plan-only`"
                ),
                changed_files=tuple(changed_files),
            )
        if not current_plan_hash:
            current_graph = contract_details.get("plan_graph")
            if isinstance(current_graph, dict):
                current_plan_hash = build_plan_hash(
                    contract_payload=contract,
                    graph_payload=current_graph,
                )
        if not current_plan_hash:
            return ImplementationExecutionStepResult(
                handled=True,
                proceed_to_evaluate=False,
                agent_status="failed",
                exit_code=1,
                summary=(
                    "`autolab run --execute-approved-plan` could not validate the "
                    "current plan hash; rerun `autolab run --plan-only`"
                ),
                changed_files=tuple(changed_files),
            )
        current_risk_fingerprint = build_risk_fingerprint(current_approval_risk)
        stored_risk_fingerprint = build_risk_fingerprint(approval_risk)
        if (
            current_plan_hash != plan_hash
            or current_risk_fingerprint != stored_risk_fingerprint
        ):
            return ImplementationExecutionStepResult(
                handled=True,
                proceed_to_evaluate=False,
                agent_status="failed",
                exit_code=1,
                summary=(
                    "current planning artifacts no longer match the approved plan; "
                    "rerun `autolab run --plan-only` or `autolab run`"
                ),
                changed_files=tuple(changed_files),
            )
        approval_payload = existing_approval
        approval_required = bool(approval_risk.get("requires_approval", False))
        approval_current = approval_is_current(
            approval_payload,
            plan_hash=plan_hash,
            risk_fingerprint=stored_risk_fingerprint,
            require_approved=approval_required,
        )
        if not approval_current:
            approval_status = str(approval_payload.get("status", "")).strip().lower()
            if approval_required and approval_status in {"pending", "retry", "stop"}:
                summary = (
                    "current implementation plan is not approved; "
                    "run `autolab approve-plan --status approve` or rerun "
                    "`autolab run --plan-only`"
                )
            else:
                summary = (
                    "`autolab run --execute-approved-plan` requires a current "
                    "`plan_approval.json` matching the existing planning artifacts; "
                    "rerun `autolab run --plan-only`"
                )
            return ImplementationExecutionStepResult(
                handled=True,
                proceed_to_evaluate=False,
                agent_status="failed",
                exit_code=1,
                summary=summary,
                changed_files=tuple(changed_files),
            )
    else:
        if not isinstance(approval_risk, dict):
            approval_risk = _derive_approval_risk_fallback(
                task_map=task_map,
                wave_rows=wave_rows,
                state=state,
                raw_execution_state=raw_state,
                impl_cfg=impl_cfg,
            )
        approval_payload = write_plan_approval(
            iteration_dir,
            iteration_id=iteration_id,
            approval_risk=approval_risk,
            plan_hash=plan_hash,
        )
        changed_files.extend([approval_json_path, approval_md_path])

        approval_required = bool(approval_payload.get("requires_approval", False))
        approval_current = approval_is_current(
            approval_payload,
            plan_hash=plan_hash,
            risk_fingerprint=str(approval_payload.get("risk_fingerprint", "")).strip(),
            require_approved=True,
        )
    if plan_only:
        approval_summary = (
            "approval required" if approval_required else "approval not required"
        )
        return ImplementationExecutionStepResult(
            handled=True,
            proceed_to_evaluate=False,
            agent_status="complete",
            exit_code=0,
            summary=(
                f"implementation plan prepared ({approval_summary}); "
                "review with `autolab approve-plan --status approve|retry|stop` "
                "or continue with `autolab run`"
            ),
            changed_files=tuple(changed_files),
            pause_reason="plan_only",
        )
    if execute_approved_plan and approval_required and not approval_current:
        return ImplementationExecutionStepResult(
            handled=True,
            proceed_to_evaluate=False,
            agent_status="failed",
            exit_code=1,
            summary=(
                "current implementation plan is not approved; "
                "run `autolab approve-plan --status approve` or rerun `autolab run --plan-only`"
            ),
            changed_files=tuple(changed_files),
        )
    if approval_required and not approval_current:
        return ImplementationExecutionStepResult(
            handled=True,
            proceed_to_evaluate=False,
            agent_status="pending_approval",
            exit_code=0,
            summary=(
                "implementation plan approval required before execution; "
                "run `autolab approve-plan --status approve`, "
                "`autolab approve-plan --status retry`, or "
                "`autolab run --execute-approved-plan` after approval"
            ),
            changed_files=tuple(changed_files),
            pause_reason="plan_approval_required",
        )

    state_payload = _coerce_execution_state(
        iteration_id=iteration_id,
        contract_hash=contract_hash,
        contract_path=".autolab/plan_contract.json",
        plan_file=f"{iteration_path}/implementation_plan.md",
        wave_rows=wave_rows,
        tasks=task_map,
        run_unit=impl_cfg.run_unit,
        raw_state=raw_state,
    )

    task_status = state_payload.get("task_status", {})
    if not isinstance(task_status, dict):
        raise StageCheckError("plan_execution_state.task_status must be a mapping")

    next_wave = _next_wave(wave_rows=wave_rows, task_status=task_status)
    if next_wave is None:
        summary_payload = _build_execution_summary(
            repo_root=repo_root,
            iteration_dir=iteration_dir,
            iteration_id=iteration_id,
            plan_file=f"{iteration_path}/implementation_plan.md",
            contract_hash=contract_hash,
            contract_payload=contract,
            graph_payload=graph,
            plan_check_payload=plan_check,
            wave_rows=wave_rows,
            state_payload=state_payload,
            task_map=task_map,
        )
        _write_json(execution_state_path, state_payload)
        _write_json(summary_path, summary_payload)
        changed_files.extend([execution_state_path, summary_path])
        return ImplementationExecutionStepResult(
            handled=True,
            proceed_to_evaluate=True,
            agent_status="complete",
            exit_code=0,
            summary="implementation plan execution complete",
            changed_files=tuple(changed_files),
        )

    wave_number = int(next_wave["wave"])
    wave_task_ids = [
        str(task_id).strip() for task_id in next_wave["tasks"] if str(task_id).strip()
    ]
    wave_tasks: list[dict[str, Any]] = [
        task_map[task_id] for task_id in wave_task_ids if task_id in task_map
    ]
    if not wave_tasks:
        raise StageCheckError(f"wave {wave_number} has no valid tasks")

    wave_key = str(wave_number)
    wave_attempt_started_at = _utc_now()
    wave_attempt_started_at_monotonic = time.monotonic()
    state_payload["wave_out_of_contract_paths"][wave_key] = []

    ready_tasks: list[dict[str, Any]] = []
    for task in wave_tasks:
        task_id = str(task.get("task_id", "")).strip()
        if str(task_status.get(task_id, "pending")).strip() == "completed":
            continue
        if not _task_ready(task=task, task_status=task_status):
            depends_on = task.get("depends_on", [])
            unresolved_dependencies: list[str] = []
            if isinstance(depends_on, list):
                for dependency in depends_on:
                    dep_id = str(dependency).strip()
                    if not dep_id:
                        continue
                    if str(task_status.get(dep_id, "")).strip() != "completed":
                        unresolved_dependencies.append(dep_id)
            task_status[task_id] = "blocked"
            state_payload["task_status"] = task_status
            state_payload["task_reason_code"][task_id] = "dependency_blocked"
            state_payload["task_reason_detail"][task_id] = (
                f"{task_id}: blocked by unresolved dependencies "
                f"({', '.join(sorted(unresolved_dependencies))})"
            )
            state_payload["task_blocked_by"][task_id] = unresolved_dependencies
            continue
        ready_tasks.append(task)

    if not ready_tasks:
        state_payload["wave_status"][str(wave_number)] = "blocked"
        wave_attempt_completed_at = _utc_now()
        wave_attempt_duration_seconds = round(
            time.monotonic() - wave_attempt_started_at_monotonic,
            3,
        )
        _append_wave_attempt(
            state_payload,
            wave_key=wave_key,
            started_at=wave_attempt_started_at,
            completed_at=wave_attempt_completed_at,
            duration_seconds=wave_attempt_duration_seconds,
            status="blocked",
            detail="wave blocked by unresolved dependencies",
        )
        state_payload["updated_at"] = _utc_now()
        summary_payload = _build_execution_summary(
            repo_root=repo_root,
            iteration_dir=iteration_dir,
            iteration_id=iteration_id,
            plan_file=f"{iteration_path}/implementation_plan.md",
            contract_hash=contract_hash,
            contract_payload=contract,
            graph_payload=graph,
            plan_check_payload=plan_check,
            wave_rows=wave_rows,
            state_payload=state_payload,
            task_map=task_map,
        )
        _write_json(execution_state_path, state_payload)
        _write_json(summary_path, summary_payload)
        changed_files.extend([execution_state_path, summary_path])
        return ImplementationExecutionStepResult(
            handled=True,
            proceed_to_evaluate=False,
            agent_status="failed",
            exit_code=1,
            summary=f"wave {wave_number} is blocked by unresolved dependencies",
            changed_files=tuple(changed_files),
            next_stage=impl_cfg.on_wave_retry_exhausted,
        )

    wave_baseline = _collect_change_snapshot(repo_root)

    task_results: dict[str, dict[str, Any]] = {}
    failure_mode_fail_fast = impl_cfg.failure_mode == "fail_fast"
    max_workers = min(len(ready_tasks), max(1, impl_cfg.max_parallel_tasks))
    if max_workers <= 1 or failure_mode_fail_fast:
        halted_by_fail_fast = ""
        halted_task_id = ""
        for task in ready_tasks:
            task_result = _execute_task(
                repo_root,
                state_path=state_path,
                iteration_id=iteration_id,
                experiment_id=experiment_id,
                iteration_path=iteration_path,
                iteration_dir=iteration_dir,
                project_wide_root=project_wide_root,
                wave=wave_number,
                task=task,
                task_retry_max=impl_cfg.task_retry_max,
                require_verification_commands=impl_cfg.require_verification_commands,
            )
            task_id = str(task_result["task_id"]).strip()
            task_results[task_id] = task_result
            task_failed = str(task_result.get("status", "")).strip() != "completed"
            if (
                task_failed
                and failure_mode_fail_fast
                and _task_failure_policy(task) == "fail_fast"
            ):
                halted_by_fail_fast = (
                    f"{task_id}: fail_fast policy halted remaining wave tasks"
                )
                halted_task_id = task_id
                break
        if halted_by_fail_fast:
            for task in ready_tasks:
                task_id = str(task.get("task_id", "")).strip()
                if not task_id or task_id in task_results:
                    continue
                task_results[task_id] = {
                    "task_id": task_id,
                    "status": "pending",
                    "attempts": 0,
                    "retries_used": 0,
                    "error": (
                        f"{task_id}: skipped because fail_fast halted wave after "
                        f"{halted_task_id} failed"
                    ),
                    "files_changed": [],
                    "started_at": "",
                    "completed_at": "",
                    "duration_seconds": 0.0,
                    "reason_code": "fail_fast_skipped",
                    "reason_detail": (
                        f"{task_id}: skipped because fail_fast halted wave after "
                        f"{halted_task_id} failed"
                    ),
                    "runner_report_path": "",
                    "runner_status": "not_run",
                    "runner_exit_code": None,
                    "verification_status": "not_run",
                    "verification_commands": [],
                    "expected_artifacts_missing": [],
                    "blocked_by": [],
                }
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task_id = {
                executor.submit(
                    _execute_task,
                    repo_root,
                    state_path=state_path,
                    iteration_id=iteration_id,
                    experiment_id=experiment_id,
                    iteration_path=iteration_path,
                    iteration_dir=iteration_dir,
                    project_wide_root=project_wide_root,
                    wave=wave_number,
                    task=task,
                    task_retry_max=impl_cfg.task_retry_max,
                    require_verification_commands=impl_cfg.require_verification_commands,
                ): str(task.get("task_id", "")).strip()
                for task in ready_tasks
            }
            for future in as_completed(future_to_task_id):
                task_id = future_to_task_id[future]
                try:
                    task_result = future.result()
                except Exception as exc:
                    task_result = {
                        "task_id": task_id,
                        "status": "failed",
                        "attempts": 1,
                        "retries_used": 0,
                        "error": f"{task_id}: task execution error: {exc}",
                        "files_changed": [],
                        "started_at": "",
                        "completed_at": "",
                        "duration_seconds": 0.0,
                        "reason_code": "task_exception",
                        "reason_detail": f"{task_id}: task execution error: {exc}",
                        "runner_report_path": f".autolab/runner_execution_report.{task_id}.json",
                        "runner_status": "error",
                        "runner_exit_code": None,
                        "verification_status": "not_run",
                        "verification_commands": [],
                        "expected_artifacts_missing": [],
                        "blocked_by": [],
                    }
                task_results[task_id] = task_result

    for task in ready_tasks:
        task_id = str(task.get("task_id", "")).strip()
        task_result = task_results.get(task_id)
        if not isinstance(task_result, dict):
            task_result = {
                "task_id": task_id,
                "status": "failed",
                "attempts": 1,
                "retries_used": 0,
                "error": f"{task_id}: missing task result",
                "files_changed": [],
                "started_at": "",
                "completed_at": "",
                "duration_seconds": 0.0,
                "reason_code": "missing_task_result",
                "reason_detail": f"{task_id}: missing task result",
                "runner_report_path": f".autolab/runner_execution_report.{task_id}.json",
                "runner_status": "missing",
                "runner_exit_code": None,
                "verification_status": "not_run",
                "verification_commands": [],
                "expected_artifacts_missing": [],
                "blocked_by": [],
            }
        state_payload["task_attempt_counts"][task_id] = int(
            task_result.get("attempts", 0) or 0
        )
        state_payload["task_retry_counts"][task_id] = int(
            task_result.get("retries_used", 0) or 0
        )
        state_payload["task_last_error"][task_id] = str(
            task_result.get("error", "")
        ).strip()
        state_payload["task_files_changed"][task_id] = list(
            task_result.get("files_changed", [])
        )
        state_payload["task_started_at"][task_id] = str(
            task_result.get("started_at", "")
        ).strip()
        state_payload["task_completed_at"][task_id] = str(
            task_result.get("completed_at", "")
        ).strip()
        try:
            state_payload["task_duration_seconds"][task_id] = round(
                float(task_result.get("duration_seconds", 0.0) or 0.0),
                3,
            )
        except Exception:
            state_payload["task_duration_seconds"][task_id] = 0.0
        state_payload["task_reason_code"][task_id] = str(
            task_result.get("reason_code", "")
        ).strip()
        state_payload["task_reason_detail"][task_id] = str(
            task_result.get("reason_detail", "")
        ).strip()
        state_payload["task_runner_report_path"][task_id] = str(
            task_result.get("runner_report_path", "")
        ).strip()
        state_payload["task_verification_status"][task_id] = str(
            task_result.get("verification_status", "")
        ).strip()
        state_payload["task_verification_commands"][task_id] = list(
            task_result.get("verification_commands", [])
        )
        state_payload["task_expected_artifacts_missing"][task_id] = list(
            task_result.get("expected_artifacts_missing", [])
        )
        state_payload["task_blocked_by"][task_id] = list(
            task_result.get("blocked_by", [])
        )
        task_result_status = str(task_result.get("status", "")).strip()
        task_success = task_result_status == "completed"
        if task_success:
            missing_artifacts = _task_expected_artifacts_missing(
                repo_root=repo_root,
                iteration_dir=iteration_dir,
                scope_root=(
                    project_wide_root
                    if str(task.get("scope_kind", "")).strip().lower() == "project_wide"
                    else iteration_dir
                ),
                task=task,
            )
            if missing_artifacts:
                task_success = False
                error_text = f"{task_id}: expected_artifacts missing ({', '.join(sorted(missing_artifacts))})"
                state_payload["task_last_error"][task_id] = error_text
                state_payload["task_reason_code"][task_id] = (
                    "expected_artifacts_missing"
                )
                state_payload["task_reason_detail"][task_id] = error_text
                state_payload["task_expected_artifacts_missing"][task_id] = list(
                    missing_artifacts
                )
                task_result_status = "failed"
        if task_success:
            state_payload["task_status"][task_id] = "completed"
            state_payload["task_reason_code"][task_id] = "completed"
            state_payload["task_reason_detail"][task_id] = ""
        elif task_result_status == "pending":
            state_payload["task_status"][task_id] = "pending"
        else:
            state_payload["task_status"][task_id] = "failed"

    wave_delta = _snapshot_delta_paths(
        wave_baseline, _collect_change_snapshot(repo_root)
    )
    wave_declared_surfaces: list[str] = []
    for task in wave_tasks:
        for surface in _task_surfaces(task):
            if surface not in wave_declared_surfaces:
                wave_declared_surfaces.append(surface)
    out_of_contract = _paths_within_declared_surfaces(
        wave_delta,
        wave_declared_surfaces,
    )
    if out_of_contract:
        preview = ", ".join(out_of_contract[:8])
        _append_log(
            repo_root,
            f"implementation wave {wave_number}: out-of-contract edits detected ({preview})",
        )
        state_payload["wave_out_of_contract_paths"][wave_key] = list(out_of_contract)
        for task_id in wave_task_ids:
            if (
                str(state_payload["task_status"].get(task_id, "")).strip()
                == "completed"
            ):
                state_payload["task_status"][task_id] = "failed"
                state_payload["task_last_error"][task_id] = (
                    "out-of-contract edits detected in wave delta"
                )
                state_payload["task_reason_code"][task_id] = "out_of_contract_edits"
                state_payload["task_reason_detail"][task_id] = (
                    "out-of-contract edits detected in wave delta"
                )

    wave_failed = any(
        str(state_payload["task_status"].get(task_id, "")).strip() != "completed"
        for task_id in wave_task_ids
    )
    wave_attempt_completed_at = _utc_now()
    wave_attempt_duration_seconds = round(
        time.monotonic() - wave_attempt_started_at_monotonic,
        3,
    )
    if wave_failed:
        retry_reasons = _wave_retry_reasons_for_tasks(
            wave_task_ids=wave_task_ids,
            state_payload=state_payload,
        )
        state_payload["wave_status"][wave_key] = "failed"
        existing_retry_reasons = state_payload["wave_retry_reasons"].get(wave_key, [])
        if not isinstance(existing_retry_reasons, list):
            existing_retry_reasons = []
        for reason_code in retry_reasons:
            if reason_code not in existing_retry_reasons:
                existing_retry_reasons.append(reason_code)
        state_payload["wave_retry_reasons"][wave_key] = existing_retry_reasons
        _append_wave_attempt(
            state_payload,
            wave_key=wave_key,
            started_at=wave_attempt_started_at,
            completed_at=wave_attempt_completed_at,
            duration_seconds=wave_attempt_duration_seconds,
            status="failed",
            retry_reason=", ".join(retry_reasons),
            detail=f"wave {wave_number} failed",
        )
        retries_used = int(state_payload["wave_retry_counts"].get(wave_key, 0) or 0)
        next_retry_number = retries_used + 1
        if retries_used < impl_cfg.wave_retry_max:
            for task_id in wave_task_ids:
                if (
                    str(state_payload["task_status"].get(task_id, "")).strip()
                    == "failed"
                ):
                    prior_reason_code = str(
                        state_payload["task_reason_code"].get(task_id, "")
                    ).strip()
                    prior_reason_detail = (
                        str(
                            state_payload["task_reason_detail"].get(task_id, "")
                        ).strip()
                        or str(
                            state_payload["task_last_error"].get(task_id, "")
                        ).strip()
                    )
                    state_payload["task_status"][task_id] = "pending"
                    state_payload["task_reason_code"][task_id] = "wave_retry_pending"
                    state_payload["task_reason_detail"][task_id] = (
                        f"retry scheduled after {prior_reason_code or 'failure'}: "
                        f"{prior_reason_detail}"
                    )
            state_payload["current_wave"] = wave_number
            state_payload["updated_at"] = _utc_now()
            summary_payload = _build_execution_summary(
                repo_root=repo_root,
                iteration_dir=iteration_dir,
                iteration_id=iteration_id,
                plan_file=f"{iteration_path}/implementation_plan.md",
                contract_hash=contract_hash,
                contract_payload=contract,
                graph_payload=graph,
                plan_check_payload=plan_check,
                wave_rows=wave_rows,
                state_payload=state_payload,
                task_map=task_map,
            )
            _write_json(execution_state_path, state_payload)
            _write_json(summary_path, summary_payload)
            changed_files.extend([execution_state_path, summary_path])
            return ImplementationExecutionStepResult(
                handled=True,
                proceed_to_evaluate=False,
                agent_status="needs_retry",
                exit_code=1,
                summary=(
                    f"implementation wave {wave_number}/{len(wave_rows)} failed; "
                    f"retrying wave ({next_retry_number}/{impl_cfg.wave_retry_max})"
                ),
                changed_files=tuple(changed_files),
            )

        state_payload["updated_at"] = _utc_now()
        summary_payload = _build_execution_summary(
            repo_root=repo_root,
            iteration_dir=iteration_dir,
            iteration_id=iteration_id,
            plan_file=f"{iteration_path}/implementation_plan.md",
            contract_hash=contract_hash,
            contract_payload=contract,
            graph_payload=graph,
            plan_check_payload=plan_check,
            wave_rows=wave_rows,
            state_payload=state_payload,
            task_map=task_map,
        )
        _write_json(execution_state_path, state_payload)
        _write_json(summary_path, summary_payload)
        changed_files.extend([execution_state_path, summary_path])
        return ImplementationExecutionStepResult(
            handled=True,
            proceed_to_evaluate=False,
            agent_status="failed",
            exit_code=1,
            summary=(
                f"implementation wave {wave_number}/{len(wave_rows)} retry budget exhausted "
                f"({retries_used}/{impl_cfg.wave_retry_max}); escalating to {impl_cfg.on_wave_retry_exhausted}"
            ),
            changed_files=tuple(changed_files),
            next_stage=impl_cfg.on_wave_retry_exhausted,
        )

    state_payload["wave_status"][wave_key] = "completed"
    _append_wave_attempt(
        state_payload,
        wave_key=wave_key,
        started_at=wave_attempt_started_at,
        completed_at=wave_attempt_completed_at,
        duration_seconds=wave_attempt_duration_seconds,
        status="completed",
        detail=f"wave {wave_number} completed",
    )
    state_payload["current_wave"] = wave_number + 1
    state_payload["updated_at"] = _utc_now()
    summary_payload = _build_execution_summary(
        repo_root=repo_root,
        iteration_dir=iteration_dir,
        iteration_id=iteration_id,
        plan_file=f"{iteration_path}/implementation_plan.md",
        contract_hash=contract_hash,
        contract_payload=contract,
        graph_payload=graph,
        plan_check_payload=plan_check,
        wave_rows=wave_rows,
        state_payload=state_payload,
        task_map=task_map,
    )
    _write_json(execution_state_path, state_payload)
    _write_json(summary_path, summary_payload)
    changed_files.extend([execution_state_path, summary_path])

    next_pending_wave = _next_wave(
        wave_rows=wave_rows,
        task_status=state_payload.get("task_status", {}),
    )
    if next_pending_wave is None:
        return ImplementationExecutionStepResult(
            handled=True,
            proceed_to_evaluate=True,
            agent_status="complete",
            exit_code=0,
            summary="implementation plan execution complete",
            changed_files=tuple(changed_files),
        )
    return ImplementationExecutionStepResult(
        handled=True,
        proceed_to_evaluate=False,
        agent_status="complete",
        exit_code=0,
        summary=(
            f"implementation wave {wave_number}/{len(wave_rows)} completed; "
            f"next wave is {int(next_pending_wave['wave'])}"
        ),
        changed_files=tuple(changed_files),
    )
