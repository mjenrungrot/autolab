from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autolab.config import _load_plan_execution_config
from autolab.models import StageCheckError
from autolab.plan_contract import PlanContractError, check_implementation_plan_contract
from autolab.runners import _invoke_agent_runner
from autolab.scope import _resolve_project_wide_root
from autolab.state import _resolve_iteration_directory
from autolab.utils import (
    _append_log,
    _collect_change_snapshot,
    _snapshot_delta_paths,
    _utc_now,
    _write_json,
)


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
        "wave_retry_counts": {str(row["wave"]): 0 for row in wave_rows},
        "wave_status": {str(row["wave"]): "pending" for row in wave_rows},
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
        raw_map = state.get(key)
        normalized: dict[str, Any] = {}
        if isinstance(raw_map, dict):
            for task_id in sorted(tasks):
                normalized[task_id] = raw_map.get(task_id, default_value)
        else:
            for task_id in sorted(tasks):
                normalized[task_id] = default_value
        state[key] = normalized
        return normalized

    _ensure_status_map("task_status", "pending")
    _ensure_status_map("task_attempt_counts", 0)
    _ensure_status_map("task_retry_counts", 0)
    _ensure_status_map("task_last_error", "")
    _ensure_status_map("task_files_changed", [])

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
    missing: list[str] = []
    raw_artifacts = task.get("expected_artifacts", [])
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
    task: dict[str, Any],
    iteration_id: str,
    iteration_path: str,
    scope_root: str,
) -> tuple[bool, str]:
    task_id = str(task.get("task_id", "")).strip() or "task"
    commands = task.get("verification_commands", [])
    if not isinstance(commands, list):
        return (False, f"{task_id}: verification_commands must be a list")
    for raw_command in commands:
        command = _substitute_task_command(
            str(raw_command).strip(),
            iteration_id=iteration_id,
            iteration_path=iteration_path,
            task_id=task_id,
            scope_root=scope_root,
        )
        if not command:
            continue
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
    manual_only_rationale = str(task.get("manual_only_rationale", "")).strip()
    attempts = 0
    last_error = ""
    last_files_changed: list[str] = []
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
                },
                report_name=f"runner_execution_report.{task_id}.json",
            )
        except subprocess.TimeoutExpired as exc:
            last_error = f"{task_id}: runner execution timed out: {exc}"
            continue
        except Exception as exc:
            last_error = f"{task_id}: runner execution error: {exc}"
            continue
        last_files_changed = [
            _normalize_path(path) for path in runner_result.get("changed_paths", [])
        ]
        runner_status = str(runner_result.get("status", "")).strip().lower()
        runner_exit = _coerce_exit_code(runner_result.get("exit_code"), default=1)
        if runner_status != "completed" or runner_exit != 0:
            last_error = (
                f"{task_id}: runner execution failed "
                f"(status={runner_status or 'unknown'}, exit_code={runner_exit})"
            )
            continue

        verification_commands = task.get("verification_commands", [])
        if isinstance(verification_commands, list) and verification_commands:
            try:
                verified, verify_error = _run_task_verification_commands(
                    repo_root=repo_root,
                    task=task,
                    iteration_id=iteration_id,
                    iteration_path=iteration_path,
                    scope_root=task_scope_root.as_posix(),
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
                continue
        elif require_verification_commands and not manual_only_rationale:
            last_error = f"{task_id}: verification_commands required by policy when manual_only_rationale is absent"
            continue

        return {
            "task_id": task_id,
            "status": "completed",
            "attempts": attempts,
            "retries_used": max(0, attempts - 1),
            "error": "",
            "files_changed": sorted(set(last_files_changed)),
        }

    return {
        "task_id": task_id,
        "status": "failed",
        "attempts": attempts,
        "retries_used": max(0, attempts - 1),
        "error": last_error or f"{task_id}: execution failed",
        "files_changed": sorted(set(last_files_changed)),
    }


def _build_execution_summary(
    *,
    iteration_id: str,
    plan_file: str,
    contract_hash: str,
    wave_rows: list[dict[str, Any]],
    state_payload: dict[str, Any],
    task_map: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    task_status = state_payload.get("task_status", {})
    task_attempt_counts = state_payload.get("task_attempt_counts", {})
    task_retry_counts = state_payload.get("task_retry_counts", {})
    task_last_error = state_payload.get("task_last_error", {})
    task_files_changed = state_payload.get("task_files_changed", {})
    wave_status = state_payload.get("wave_status", {})
    wave_retry_counts = state_payload.get("wave_retry_counts", {})

    completed = 0
    failed = 0
    blocked = 0
    details: list[dict[str, Any]] = []
    for row in wave_rows:
        wave = int(row["wave"])
        for task_id in row["tasks"]:
            status = str(task_status.get(task_id, "pending")).strip() or "pending"
            if status == "completed":
                completed += 1
            elif status == "failed":
                failed += 1
            elif status == "blocked":
                blocked += 1
            details.append(
                {
                    "task_id": task_id,
                    "status": status,
                    "wave": wave,
                    "attempts": int(task_attempt_counts.get(task_id, 0) or 0),
                    "retries_used": int(task_retry_counts.get(task_id, 0) or 0),
                    "last_error": str(task_last_error.get(task_id, "")).strip(),
                    "files_changed": task_files_changed.get(task_id, []),
                    "scope_kind": str(
                        task_map.get(task_id, {}).get("scope_kind", "")
                    ).strip(),
                }
            )

    total = len(task_map)
    pending = max(0, total - completed - failed - blocked)
    waves_executed = 0
    wave_details: list[dict[str, Any]] = []
    for row in wave_rows:
        wave = int(row["wave"])
        key = str(wave)
        status = str(wave_status.get(key, "pending")).strip() or "pending"
        if status == "completed":
            waves_executed = max(waves_executed, wave)
        wave_details.append(
            {
                "wave": wave,
                "status": status,
                "attempts": int(wave_retry_counts.get(key, 0) or 0),
                "tasks": list(row["tasks"]),
            }
        )

    return {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "stage": "implementation",
        "iteration_id": iteration_id,
        "plan_file": plan_file,
        "contract_hash": contract_hash,
        "run_unit": "wave",
        "tasks_total": total,
        "tasks_completed": completed,
        "tasks_failed": failed,
        "tasks_blocked": blocked,
        "tasks_pending": pending,
        "waves_total": len(wave_rows),
        "waves_executed": waves_executed,
        "wave_details": wave_details,
        "task_details": details,
    }


def execute_implementation_plan_step(
    repo_root: Path,
    *,
    state_path: Path,
    state: dict[str, Any],
    run_agent_mode: str,
    auto_mode: bool = False,
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

    execution_state_path = _execution_state_path(iteration_dir)
    summary_path = _execution_summary_path(iteration_dir)
    raw_state: dict[str, Any] | None = None
    if execution_state_path.exists():
        loaded = _load_json_dict(execution_state_path)
        raw_state = loaded

    if raw_state is None:
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
        contract_passed, contract_message, _details = (
            check_implementation_plan_contract(
                repo_root,
                state,
                stage_override="implementation",
                write_outputs=True,
            )
        )
    except PlanContractError as exc:
        raise StageCheckError(str(exc)) from exc
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

    contract_path = repo_root / ".autolab" / "plan_contract.json"
    graph_path = repo_root / ".autolab" / "plan_graph.json"
    if not contract_path.exists():
        raise StageCheckError(
            "missing .autolab/plan_contract.json after contract check"
        )
    if not graph_path.exists():
        raise StageCheckError("missing .autolab/plan_graph.json after contract check")

    contract = _load_json_dict(contract_path)
    graph = _load_json_dict(graph_path)
    contract_hash = _json_hash(contract)
    task_map = _task_map(contract)
    wave_rows = _wave_rows(graph)

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
            iteration_id=iteration_id,
            plan_file=f"{iteration_path}/implementation_plan.md",
            contract_hash=contract_hash,
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

    ready_tasks: list[dict[str, Any]] = []
    for task in wave_tasks:
        task_id = str(task.get("task_id", "")).strip()
        if str(task_status.get(task_id, "pending")).strip() == "completed":
            continue
        if not _task_ready(task=task, task_status=task_status):
            task_status[task_id] = "blocked"
            state_payload["task_status"] = task_status
            continue
        ready_tasks.append(task)

    if not ready_tasks:
        state_payload["wave_status"][str(wave_number)] = "blocked"
        state_payload["updated_at"] = _utc_now()
        summary_payload = _build_execution_summary(
            iteration_id=iteration_id,
            plan_file=f"{iteration_path}/implementation_plan.md",
            contract_hash=contract_hash,
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
                }
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_task_id = {
                executor.submit(
                    _execute_task,
                    repo_root,
                    state_path=state_path,
                    iteration_id=iteration_id,
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
                task_result_status = "failed"
        if task_success:
            state_payload["task_status"][task_id] = "completed"
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
        for task_id in wave_task_ids:
            if (
                str(state_payload["task_status"].get(task_id, "")).strip()
                == "completed"
            ):
                state_payload["task_status"][task_id] = "failed"
                state_payload["task_last_error"][task_id] = (
                    "out-of-contract edits detected in wave delta"
                )

    wave_failed = any(
        str(state_payload["task_status"].get(task_id, "")).strip() != "completed"
        for task_id in wave_task_ids
    )
    wave_key = str(wave_number)
    if wave_failed:
        retry_count = int(state_payload["wave_retry_counts"].get(wave_key, 0) or 0) + 1
        state_payload["wave_retry_counts"][wave_key] = retry_count
        state_payload["wave_status"][wave_key] = "failed"
        if retry_count <= impl_cfg.wave_retry_max:
            for task_id in wave_task_ids:
                if (
                    str(state_payload["task_status"].get(task_id, "")).strip()
                    == "failed"
                ):
                    state_payload["task_status"][task_id] = "pending"
            state_payload["current_wave"] = wave_number
            state_payload["updated_at"] = _utc_now()
            summary_payload = _build_execution_summary(
                iteration_id=iteration_id,
                plan_file=f"{iteration_path}/implementation_plan.md",
                contract_hash=contract_hash,
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
                    f"retrying wave ({retry_count}/{impl_cfg.wave_retry_max})"
                ),
                changed_files=tuple(changed_files),
            )

        state_payload["updated_at"] = _utc_now()
        summary_payload = _build_execution_summary(
            iteration_id=iteration_id,
            plan_file=f"{iteration_path}/implementation_plan.md",
            contract_hash=contract_hash,
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
                f"({retry_count}/{impl_cfg.wave_retry_max}); escalating to {impl_cfg.on_wave_retry_exhausted}"
            ),
            changed_files=tuple(changed_files),
            next_stage=impl_cfg.on_wave_retry_exhausted,
        )

    state_payload["wave_status"][wave_key] = "completed"
    state_payload["current_wave"] = wave_number + 1
    state_payload["updated_at"] = _utc_now()
    summary_payload = _build_execution_summary(
        iteration_id=iteration_id,
        plan_file=f"{iteration_path}/implementation_plan.md",
        contract_hash=contract_hash,
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
