from __future__ import annotations

import re
from collections import deque
from pathlib import Path
from typing import Any

from autolab.utils import _load_json_if_exists

_REASON_CODE_COMPLETED = "completed"
_REASON_CODE_DEPENDENCY_BLOCKED = "dependency_blocked"
_REASON_CODE_FAIL_FAST_SKIPPED = "fail_fast_skipped"
_REASON_CODE_WAVE_RETRY_PENDING = "wave_retry_pending"
_REASON_CODE_RUNNER_FAILED = "runner_failed"
_REASON_CODE_VERIFICATION_FAILED = "verification_failed"
_REASON_CODE_EXPECTED_ARTIFACTS_MISSING = "expected_artifacts_missing"
_REASON_CODE_OUT_OF_CONTRACT_EDITS = "out_of_contract_edits"
_REASON_CODE_TASK_EXCEPTION = "task_exception"
_REASON_CODE_MISSING_TASK_RESULT = "missing_task_result"

_REASON_CODES = {
    _REASON_CODE_COMPLETED,
    _REASON_CODE_DEPENDENCY_BLOCKED,
    _REASON_CODE_FAIL_FAST_SKIPPED,
    _REASON_CODE_WAVE_RETRY_PENDING,
    _REASON_CODE_RUNNER_FAILED,
    _REASON_CODE_VERIFICATION_FAILED,
    _REASON_CODE_EXPECTED_ARTIFACTS_MISSING,
    _REASON_CODE_OUT_OF_CONTRACT_EDITS,
    _REASON_CODE_TASK_EXCEPTION,
    _REASON_CODE_MISSING_TASK_RESULT,
}

_CONFLICT_KIND_WRITE = "same_wave_write_conflict"
_CONFLICT_KIND_GROUP = "same_wave_conflict_group_collision"
_CONFLICT_KIND_OUT_OF_CONTRACT = "out_of_contract_edits"

_WRITE_CONFLICT_RE = re.compile(
    r"same-wave write conflict: tasks (?P<left>\S+) and (?P<right>\S+) overlap in writes/touches"
)
_GROUP_CONFLICT_RE = re.compile(
    r"same-wave conflict_group collision: tasks (?P<left>\S+) and (?P<right>\S+) share '(?P<group>[^']+)'"
)


def _safe_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _safe_string(value: Any) -> str:
    return str(value or "").strip()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        try:
            return int(default)
        except Exception:
            return 0


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        try:
            return float(default)
        except Exception:
            return 0.0


def _normalize_path(value: Any) -> str:
    return str(value or "").replace("\\", "/").strip().strip("/")


def _path_within_surface(path: str, surface: str) -> bool:
    normalized_path = _normalize_path(path)
    normalized_surface = _normalize_path(surface)
    if not normalized_path or not normalized_surface:
        return False
    return normalized_path == normalized_surface or normalized_path.startswith(
        f"{normalized_surface}/"
    )


def _non_empty_strings(value: Any) -> list[str]:
    output: list[str] = []
    if not isinstance(value, list):
        return output
    for item in value:
        text = _safe_string(item)
        if text:
            output.append(text)
    return output


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in values:
        text = _safe_string(item)
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _payload_iteration_id(payload: Any) -> str:
    return _safe_string(_safe_dict(payload).get("iteration_id"))


def _expected_iteration_id(
    *,
    iteration_dir: Path | None,
    execution_state_payload: dict[str, Any],
    execution_summary_payload: dict[str, Any],
    graph_payload: dict[str, Any],
    plan_check_payload: dict[str, Any],
) -> str:
    candidates = [
        iteration_dir.name if iteration_dir is not None else "",
        _payload_iteration_id(execution_state_payload),
        _payload_iteration_id(execution_summary_payload),
        _payload_iteration_id(graph_payload),
        _payload_iteration_id(plan_check_payload),
    ]
    for candidate in candidates:
        text = _safe_string(candidate)
        if text:
            return text
    return ""


def _filter_payload_for_iteration(
    payload: dict[str, Any],
    *,
    label: str,
    expected_iteration_id: str,
    diagnostics: list[str],
) -> dict[str, Any]:
    if not payload:
        return {}
    actual_iteration_id = _payload_iteration_id(payload)
    if (
        expected_iteration_id
        and actual_iteration_id
        and actual_iteration_id != expected_iteration_id
    ):
        diagnostics.append(
            f"{label} ignored because iteration_id={actual_iteration_id} does not match {expected_iteration_id}"
        )
        return {}
    return payload


def _expand_retry_reasons(values: list[str]) -> list[str]:
    expanded: list[str] = []
    for value in values:
        for raw_part in str(value).split(","):
            part = _safe_string(raw_part)
            if part:
                expanded.append(part)
    return expanded


def _repo_relative(repo_root: Path, path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return str(path)


def _resolve_relative_path(repo_root: Path, raw_path: str) -> Path | None:
    text = _safe_string(raw_path)
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = (repo_root / candidate).resolve(strict=False)
    else:
        candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(repo_root)
    except ValueError:
        return None
    return candidate


def _load_optional_mapping(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    payload = _load_json_if_exists(path)
    return payload if isinstance(payload, dict) else None


def _load_contract_payload(
    repo_root: Path,
    *,
    iteration_dir: Path | None,
    execution_state_payload: dict[str, Any],
    expected_iteration_id: str,
    diagnostics: list[str],
    contract_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(contract_payload, dict):
        return _filter_payload_for_iteration(
            contract_payload,
            label="plan_contract payload",
            expected_iteration_id=expected_iteration_id,
            diagnostics=diagnostics,
        )

    candidate_paths: list[Path] = []
    if iteration_dir is not None:
        candidate_paths.append(iteration_dir / "plan_contract.json")
    state_contract_path = _resolve_relative_path(
        repo_root,
        _safe_string(execution_state_payload.get("contract_path")),
    )
    if state_contract_path is not None:
        candidate_paths.append(state_contract_path)
    candidate_paths.append(repo_root / ".autolab" / "plan_contract.json")

    seen: set[Path] = set()
    for path in candidate_paths:
        if path in seen:
            continue
        seen.add(path)
        payload = _load_optional_mapping(path)
        if not isinstance(payload, dict):
            continue
        filtered = _filter_payload_for_iteration(
            payload,
            label=f"{_repo_relative(repo_root, path) or path.as_posix()}",
            expected_iteration_id=expected_iteration_id,
            diagnostics=diagnostics,
        )
        if filtered:
            return filtered
    return {}


def _task_contract_details(
    contract_payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    details: dict[str, dict[str, Any]] = {}
    for raw_task in _safe_list(contract_payload.get("tasks")):
        if not isinstance(raw_task, dict):
            continue
        task_id = _safe_string(raw_task.get("task_id"))
        if not task_id:
            continue
        surfaces = _dedupe_strings(
            [
                _normalize_path(path)
                for field in ("writes", "touches")
                for path in _safe_list(raw_task.get(field))
                if _normalize_path(path)
            ]
        )
        details[task_id] = {
            "surfaces": surfaces,
            "conflict_group": _safe_string(raw_task.get("conflict_group")),
        }
    return details


def _surface_overlap_path(left: str, right: str) -> str:
    normalized_left = _normalize_path(left)
    normalized_right = _normalize_path(right)
    if not normalized_left or not normalized_right:
        return ""
    if _path_within_surface(normalized_left, normalized_right):
        return normalized_left
    if _path_within_surface(normalized_right, normalized_left):
        return normalized_right
    return ""


def _declared_overlap_paths(
    *,
    left_task_id: str,
    right_task_id: str,
    contract_task_details: dict[str, dict[str, Any]],
) -> list[str]:
    left_surfaces = _non_empty_strings(
        _safe_dict(contract_task_details.get(left_task_id)).get("surfaces")
    )
    right_surfaces = _non_empty_strings(
        _safe_dict(contract_task_details.get(right_task_id)).get("surfaces")
    )
    overlaps: list[str] = []
    for left_surface in left_surfaces:
        for right_surface in right_surfaces:
            overlap = _surface_overlap_path(left_surface, right_surface)
            if overlap:
                overlaps.append(overlap)
    return _dedupe_strings(overlaps)


def _observed_overlap_paths(
    *,
    left_task_id: str,
    right_task_id: str,
    task_lookup: dict[str, dict[str, Any]],
) -> list[str]:
    left_files = set(
        _non_empty_strings(
            _safe_dict(task_lookup.get(left_task_id)).get("files_changed")
        )
    )
    right_files = set(
        _non_empty_strings(
            _safe_dict(task_lookup.get(right_task_id)).get("files_changed")
        )
    )
    return sorted(left_files & right_files)


def _task_ids_for_paths(
    *,
    task_ids: list[str],
    task_lookup: dict[str, dict[str, Any]],
    paths: list[str],
) -> list[str]:
    if not paths:
        return task_ids
    matched: list[str] = []
    for task_id in task_ids:
        files_changed = _non_empty_strings(
            _safe_dict(task_lookup.get(task_id)).get("files_changed")
        )
        if any(
            _path_within_surface(file_path, conflict_path)
            or _path_within_surface(conflict_path, file_path)
            for file_path in files_changed
            for conflict_path in paths
        ):
            matched.append(task_id)
    return matched or task_ids


def _timing_available(
    *, started_at: str, completed_at: str, duration_seconds: float
) -> bool:
    return bool(started_at or completed_at or duration_seconds > 0)


def _default_reason_code(*, status: str, last_error: str) -> str:
    normalized_status = _safe_string(status).lower()
    if normalized_status == "completed":
        return _REASON_CODE_COMPLETED
    if normalized_status == "blocked":
        return _REASON_CODE_DEPENDENCY_BLOCKED
    if normalized_status == "failed":
        if last_error:
            return _REASON_CODE_RUNNER_FAILED
        return _REASON_CODE_TASK_EXCEPTION
    if normalized_status == "pending":
        return _REASON_CODE_WAVE_RETRY_PENDING if last_error else "pending"
    return normalized_status or "unknown"


def _normalize_reason_code(raw_value: Any, *, status: str, last_error: str) -> str:
    reason_code = _safe_string(raw_value).lower()
    if reason_code in _REASON_CODES:
        return reason_code
    fallback = _default_reason_code(status=status, last_error=last_error)
    return fallback if fallback in _REASON_CODES else fallback


def _sanitize_attempt_history(value: Any) -> list[dict[str, Any]]:
    history: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return history
    for raw_entry in value:
        if not isinstance(raw_entry, dict):
            continue
        duration_seconds = round(
            _safe_float(raw_entry.get("duration_seconds"), 0.0),
            3,
        )
        history.append(
            {
                "attempt": max(1, _safe_int(raw_entry.get("attempt"), 1)),
                "status": _safe_string(raw_entry.get("status")) or "unknown",
                "started_at": _safe_string(raw_entry.get("started_at")),
                "completed_at": _safe_string(raw_entry.get("completed_at")),
                "duration_seconds": max(0.0, duration_seconds),
                "retry_reason": _safe_string(raw_entry.get("retry_reason")),
                "detail": _safe_string(raw_entry.get("detail")),
            }
        )
    history.sort(key=lambda row: row["attempt"])
    return history


def _build_task_rows(
    repo_root: Path,
    *,
    task_to_wave: dict[str, int],
    execution_state_payload: dict[str, Any],
    execution_summary_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    summary_rows = _safe_list(execution_summary_payload.get("task_details"))
    summary_by_task: dict[str, dict[str, Any]] = {}
    for row in summary_rows:
        if not isinstance(row, dict):
            continue
        task_id = _safe_string(row.get("task_id"))
        if task_id:
            summary_by_task[task_id] = row

    state_status = _safe_dict(execution_state_payload.get("task_status"))
    task_ids = sorted(
        {
            *task_to_wave.keys(),
            *summary_by_task.keys(),
            *(task_id for task_id in state_status.keys() if _safe_string(task_id)),
        }
    )
    task_rows: list[dict[str, Any]] = []

    for task_id in task_ids:
        detail = _safe_dict(summary_by_task.get(task_id))
        status = (
            _safe_string(detail.get("status"))
            or _safe_string(state_status.get(task_id))
            or "pending"
        )
        wave = _safe_int(detail.get("wave"), task_to_wave.get(task_id, 0))
        attempts = _safe_int(
            detail.get("attempts"),
            _safe_int(
                _safe_dict(execution_state_payload.get("task_attempt_counts")).get(
                    task_id
                ),
                0,
            ),
        )
        retries_used = _safe_int(
            detail.get("retries_used"),
            _safe_int(
                _safe_dict(execution_state_payload.get("task_retry_counts")).get(
                    task_id
                ),
                0,
            ),
        )
        last_error = _safe_string(detail.get("last_error")) or _safe_string(
            _safe_dict(execution_state_payload.get("task_last_error")).get(task_id)
        )
        started_at = _safe_string(detail.get("started_at")) or _safe_string(
            _safe_dict(execution_state_payload.get("task_started_at")).get(task_id)
        )
        completed_at = _safe_string(detail.get("completed_at")) or _safe_string(
            _safe_dict(execution_state_payload.get("task_completed_at")).get(task_id)
        )
        duration_seconds = max(
            0.0,
            round(
                _safe_float(
                    detail.get("duration_seconds"),
                    _safe_dict(
                        execution_state_payload.get("task_duration_seconds")
                    ).get(task_id),
                ),
                3,
            ),
        )
        blocked_by = _non_empty_strings(detail.get("blocked_by")) or _non_empty_strings(
            _safe_dict(execution_state_payload.get("task_blocked_by")).get(task_id)
        )
        files_changed = _non_empty_strings(
            detail.get("files_changed")
        ) or _non_empty_strings(
            _safe_dict(execution_state_payload.get("task_files_changed")).get(task_id)
        )
        scope_kind = _safe_string(detail.get("scope_kind"))
        runner_report_path = _safe_string(
            detail.get("runner_report_path")
        ) or _safe_string(
            _safe_dict(execution_state_payload.get("task_runner_report_path")).get(
                task_id
            )
        )
        verification_status = _safe_string(
            detail.get("verification_status")
        ) or _safe_string(
            _safe_dict(execution_state_payload.get("task_verification_status")).get(
                task_id
            )
        )
        if not verification_status:
            verification_status = "not_run"
        verification_commands = _non_empty_strings(
            detail.get("verification_commands")
        ) or _non_empty_strings(
            _safe_dict(execution_state_payload.get("task_verification_commands")).get(
                task_id
            )
        )
        expected_artifacts_missing = _non_empty_strings(
            detail.get("expected_artifacts_missing")
        ) or _non_empty_strings(
            _safe_dict(
                execution_state_payload.get("task_expected_artifacts_missing")
            ).get(task_id)
        )
        reason_detail = (
            _safe_string(detail.get("reason_detail"))
            or _safe_string(
                _safe_dict(execution_state_payload.get("task_reason_detail")).get(
                    task_id
                )
            )
            or last_error
        )
        reason_code = _normalize_reason_code(
            detail.get("reason_code")
            or _safe_dict(execution_state_payload.get("task_reason_code")).get(task_id),
            status=status,
            last_error=last_error,
        )

        runner_status = _safe_string(detail.get("runner_status"))
        runner_exit_code = detail.get("runner_exit_code")
        runner_report_full_path = _resolve_relative_path(repo_root, runner_report_path)
        runner_report_payload = _load_optional_mapping(runner_report_full_path)
        if isinstance(runner_report_payload, dict):
            if not runner_status:
                runner_status = _safe_string(runner_report_payload.get("status"))
            if runner_exit_code is None:
                runner_exit_code = runner_report_payload.get("exit_code")

        timing_available = _timing_available(
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
        )
        evidence_summary = _safe_dict(detail.get("evidence_summary"))
        if not evidence_summary:
            evidence_summary = {
                "runner_status": runner_status or "unavailable",
                "runner_exit_code": None
                if runner_exit_code is None
                else _safe_int(runner_exit_code, 0),
                "verification_status": verification_status,
                "files_changed_count": len(files_changed),
                "expected_artifacts_missing_count": len(expected_artifacts_missing),
                "text": (
                    f"runner={runner_status or 'unavailable'} "
                    f"verify={verification_status} files={len(files_changed)} "
                    f"missing_artifacts={len(expected_artifacts_missing)}"
                ),
            }
        else:
            evidence_summary = {
                **evidence_summary,
                "text": _safe_string(evidence_summary.get("text"))
                or (
                    f"runner={_safe_string(evidence_summary.get('runner_status')) or runner_status or 'unavailable'} "
                    f"verify={_safe_string(evidence_summary.get('verification_status')) or verification_status} "
                    f"files={_safe_int(evidence_summary.get('files_changed_count'), len(files_changed))} "
                    f"missing_artifacts={_safe_int(evidence_summary.get('expected_artifacts_missing_count'), len(expected_artifacts_missing))}"
                ),
            }

        task_rows.append(
            {
                "task_id": task_id,
                "status": status,
                "wave": wave,
                "attempts": max(0, attempts),
                "retries_used": max(0, retries_used),
                "last_error": last_error,
                "scope_kind": scope_kind,
                "files_changed": files_changed,
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_seconds": duration_seconds,
                "timing_available": timing_available,
                "reason_code": reason_code,
                "reason_detail": reason_detail,
                "blocked_by": blocked_by,
                "runner_report_path": runner_report_path,
                "runner_status": runner_status,
                "runner_exit_code": None
                if runner_exit_code is None
                else _safe_int(runner_exit_code, 0),
                "verification_status": verification_status,
                "verification_commands": verification_commands,
                "expected_artifacts_missing": expected_artifacts_missing,
                "evidence_summary": evidence_summary,
            }
        )

    task_rows.sort(key=lambda row: (row["wave"], row["task_id"]))
    return task_rows


def _build_task_lookup(task_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(row["task_id"]): row
        for row in task_rows
        if _safe_string(row.get("task_id"))
    }


def _build_wave_rows(
    *,
    graph_payload: dict[str, Any],
    execution_state_payload: dict[str, Any],
    execution_summary_payload: dict[str, Any],
    task_rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int], dict[int, list[str]]]:
    wave_to_tasks: dict[int, list[str]] = {}
    task_to_wave: dict[str, int] = {}

    for row in _safe_list(graph_payload.get("waves")):
        if not isinstance(row, dict):
            continue
        wave = _safe_int(row.get("wave"), 0)
        tasks = _dedupe_strings(_non_empty_strings(row.get("tasks")))
        if wave <= 0 or not tasks:
            continue
        wave_to_tasks[wave] = tasks
        for task_id in tasks:
            task_to_wave[task_id] = wave

    if not wave_to_tasks:
        for row in _safe_list(execution_summary_payload.get("wave_details")):
            if not isinstance(row, dict):
                continue
            wave = _safe_int(row.get("wave"), 0)
            tasks = _dedupe_strings(_non_empty_strings(row.get("tasks")))
            if wave <= 0 or not tasks:
                continue
            wave_to_tasks[wave] = tasks
            for task_id in tasks:
                task_to_wave[task_id] = wave

    for row in task_rows:
        task_id = _safe_string(row.get("task_id"))
        wave = _safe_int(row.get("wave"), 0)
        if not task_id or wave <= 0:
            continue
        wave_to_tasks.setdefault(wave, [])
        if task_id not in wave_to_tasks[wave]:
            wave_to_tasks[wave].append(task_id)
        task_to_wave[task_id] = wave

    summary_wave_details: dict[int, dict[str, Any]] = {}
    for row in _safe_list(execution_summary_payload.get("wave_details")):
        if not isinstance(row, dict):
            continue
        wave = _safe_int(row.get("wave"), 0)
        if wave > 0:
            summary_wave_details[wave] = row

    task_lookup = _build_task_lookup(task_rows)
    wave_rows: list[dict[str, Any]] = []
    for wave in sorted(wave_to_tasks):
        detail = _safe_dict(summary_wave_details.get(wave))
        tasks = list(wave_to_tasks.get(wave, []))
        state_wave_key = str(wave)
        attempt_history = _sanitize_attempt_history(
            detail.get("attempt_history")
            or _safe_dict(execution_state_payload.get("wave_attempt_history")).get(
                state_wave_key
            )
        )
        if attempt_history:
            attempts = len(attempt_history)
            retries_used = max(0, attempts - 1)
        else:
            retries_used = _safe_int(
                detail.get("retries_used"),
                _safe_int(
                    _safe_dict(execution_state_payload.get("wave_retry_counts")).get(
                        state_wave_key
                    ),
                    0,
                ),
            )
            attempts = _safe_int(detail.get("attempts"), 0)
            if attempts == 0 and retries_used > 0:
                attempts = retries_used + 1
        status = (
            _safe_string(detail.get("status"))
            or _safe_string(
                _safe_dict(execution_state_payload.get("wave_status")).get(
                    state_wave_key
                )
            )
            or "pending"
        )
        started_at = _safe_string(detail.get("started_at")) or _safe_string(
            _safe_dict(execution_state_payload.get("wave_started_at")).get(
                state_wave_key
            )
        )
        completed_at = _safe_string(detail.get("completed_at")) or _safe_string(
            _safe_dict(execution_state_payload.get("wave_completed_at")).get(
                state_wave_key
            )
        )
        duration_seconds = _safe_float(
            detail.get("duration_seconds"),
            _safe_dict(execution_state_payload.get("wave_duration_seconds")).get(
                state_wave_key
            ),
        )
        if duration_seconds <= 0 and attempt_history:
            duration_seconds = sum(
                max(0.0, _safe_float(entry.get("duration_seconds"), 0.0))
                for entry in attempt_history
            )
        duration_seconds = max(0.0, round(duration_seconds, 3))
        last_attempt_duration_seconds = round(
            max(
                0.0,
                _safe_float(
                    detail.get("last_attempt_duration_seconds"),
                    attempt_history[-1].get("duration_seconds")
                    if attempt_history
                    else 0.0,
                ),
            ),
            3,
        )
        retry_reasons = _dedupe_strings(
            _expand_retry_reasons(
                _non_empty_strings(detail.get("retry_reasons"))
                or _non_empty_strings(
                    _safe_dict(execution_state_payload.get("wave_retry_reasons")).get(
                        state_wave_key
                    )
                )
                or [
                    _safe_string(entry.get("retry_reason"))
                    for entry in attempt_history
                    if _safe_string(entry.get("retry_reason"))
                ]
            )
        )
        out_of_contract_paths = _dedupe_strings(
            _non_empty_strings(detail.get("out_of_contract_paths"))
            or _non_empty_strings(
                _safe_dict(
                    execution_state_payload.get("wave_out_of_contract_paths")
                ).get(state_wave_key)
            )
        )
        blocked_task_ids: list[str] = []
        skipped_task_ids: list[str] = []
        deferred_task_ids: list[str] = []
        completed_task_ids: list[str] = []
        failed_task_ids: list[str] = []
        pending_task_ids: list[str] = []
        for task_id in tasks:
            task_row = _safe_dict(task_lookup.get(task_id))
            task_status = _safe_string(task_row.get("status"))
            reason_code = _safe_string(task_row.get("reason_code"))
            if task_status == "completed":
                completed_task_ids.append(task_id)
            elif task_status == "failed":
                failed_task_ids.append(task_id)
            elif (
                task_status == "blocked"
                or reason_code == _REASON_CODE_DEPENDENCY_BLOCKED
            ):
                blocked_task_ids.append(task_id)
            elif reason_code == _REASON_CODE_FAIL_FAST_SKIPPED:
                skipped_task_ids.append(task_id)
                pending_task_ids.append(task_id)
            elif reason_code == _REASON_CODE_WAVE_RETRY_PENDING:
                deferred_task_ids.append(task_id)
                pending_task_ids.append(task_id)
            elif task_status == "pending":
                pending_task_ids.append(task_id)
        timing_available = _timing_available(
            started_at=started_at,
            completed_at=completed_at,
            duration_seconds=duration_seconds,
        ) or bool(attempt_history)
        retry_pending = bool(deferred_task_ids)
        wave_rows.append(
            {
                "wave": wave,
                "status": status,
                "attempts": max(attempts, len(attempt_history), 0),
                "retries_used": max(0, retries_used),
                "tasks": tasks,
                "started_at": started_at,
                "completed_at": completed_at,
                "duration_seconds": duration_seconds,
                "last_attempt_duration_seconds": last_attempt_duration_seconds,
                "timing_available": timing_available,
                "attempt_history": attempt_history,
                "retry_reasons": retry_reasons,
                "current_retry_reasons": list(retry_reasons) if retry_pending else [],
                "out_of_contract_paths": out_of_contract_paths,
                "completed_task_ids": completed_task_ids,
                "failed_task_ids": failed_task_ids,
                "blocked_task_ids": blocked_task_ids,
                "skipped_task_ids": skipped_task_ids,
                "deferred_task_ids": deferred_task_ids,
                "pending_task_ids": pending_task_ids,
                "retry_pending": retry_pending,
            }
        )
    return (wave_rows, task_to_wave, wave_to_tasks)


def _topological_order(nodes: list[str], edges: list[tuple[str, str]]) -> list[str]:
    indegree = {node: 0 for node in nodes}
    outgoing: dict[str, list[str]] = {node: [] for node in nodes}
    for left, right in edges:
        if left not in indegree or right not in indegree:
            continue
        outgoing[left].append(right)
        indegree[right] += 1
    queue = deque(sorted(node for node, degree in indegree.items() if degree == 0))
    ordered: list[str] = []
    while queue:
        node = queue.popleft()
        ordered.append(node)
        for nxt in sorted(outgoing.get(node, [])):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if len(ordered) != len(nodes):
        return []
    return ordered


def _build_critical_path(
    *,
    graph_payload: dict[str, Any],
    task_rows: list[dict[str, Any]],
    task_to_wave: dict[str, int],
    wave_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    task_lookup = _build_task_lookup(task_rows)
    node_ids: list[str] = []
    for row in _safe_list(graph_payload.get("nodes")):
        if not isinstance(row, dict):
            continue
        task_id = _safe_string(row.get("task_id"))
        if task_id:
            node_ids.append(task_id)
    node_ids.extend(
        str(row["task_id"]) for row in task_rows if _safe_string(row.get("task_id"))
    )
    for wave_row in wave_rows:
        node_ids.extend(_non_empty_strings(wave_row.get("tasks")))
    node_ids = sorted(set(node_ids))
    if not node_ids:
        return {
            "status": "unavailable",
            "mode": "unavailable",
            "task_ids": [],
            "wave_ids": [],
            "duration_seconds": 0.0,
            "weight": 0.0,
            "basis_note": "plan graph unavailable",
        }

    edges: list[tuple[str, str]] = []
    for row in _safe_list(graph_payload.get("edges")):
        if not isinstance(row, dict):
            continue
        left = _safe_string(row.get("from"))
        right = _safe_string(row.get("to"))
        if left and right:
            edges.append((left, right))
    ordered_waves = sorted(
        (
            (
                _safe_int(wave_row.get("wave"), 0),
                _dedupe_strings(_non_empty_strings(wave_row.get("tasks"))),
            )
            for wave_row in wave_rows
            if _safe_int(wave_row.get("wave"), 0) > 0
        ),
        key=lambda item: item[0],
    )
    for index in range(len(ordered_waves) - 1):
        left_wave_tasks = ordered_waves[index][1]
        right_wave_tasks = ordered_waves[index + 1][1]
        for left_task_id in left_wave_tasks:
            for right_task_id in right_wave_tasks:
                edges.append((left_task_id, right_task_id))
    edges = sorted(set(edges))
    ordered = _topological_order(node_ids, edges)
    if not ordered:
        return {
            "status": "unavailable",
            "mode": "unavailable",
            "task_ids": [],
            "wave_ids": [],
            "duration_seconds": 0.0,
            "weight": 0.0,
            "basis_note": "dependency graph is cyclic or invalid",
        }

    weights: dict[str, float] = {}
    measured_flags: dict[str, bool] = {}
    for task_id in node_ids:
        task_row = _safe_dict(task_lookup.get(task_id))
        duration_seconds = max(0.0, _safe_float(task_row.get("duration_seconds"), 0.0))
        timing_available = bool(task_row.get("timing_available", False))
        if timing_available:
            weights[task_id] = duration_seconds if duration_seconds > 0 else 0.001
            measured_flags[task_id] = True
        else:
            weights[task_id] = 1.0
            measured_flags[task_id] = False

    predecessors: dict[str, list[str]] = {node: [] for node in node_ids}
    for left, right in edges:
        if left in predecessors and right in predecessors:
            predecessors[right].append(left)

    best_score: dict[str, float] = {}
    best_prev: dict[str, str] = {}
    for node in ordered:
        base_weight = weights.get(node, 1.0)
        best_score[node] = base_weight
        for prev in predecessors.get(node, []):
            candidate = best_score.get(prev, 0.0) + base_weight
            if candidate > best_score[node]:
                best_score[node] = candidate
                best_prev[node] = prev

    if not best_score:
        return {
            "status": "unavailable",
            "mode": "unavailable",
            "task_ids": [],
            "wave_ids": [],
            "duration_seconds": 0.0,
            "weight": 0.0,
            "basis_note": "critical path could not be computed",
        }

    terminal = max(sorted(best_score), key=lambda task_id: best_score[task_id])
    task_ids: list[str] = []
    cursor = terminal
    while cursor:
        task_ids.append(cursor)
        cursor = best_prev.get(cursor, "")
    task_ids.reverse()

    measured_count = sum(
        1 for task_id in task_ids if measured_flags.get(task_id, False)
    )
    if measured_count == 0:
        mode = "structural"
        duration_seconds = 0.0
        basis_note = "structural path from dependency graph with wave-barrier edges and unit task weights"
    elif measured_count == len(task_ids):
        mode = "measured_complete"
        duration_seconds = round(
            sum(
                max(
                    0.0,
                    _safe_float(
                        task_lookup.get(task_id, {}).get("duration_seconds"), 0.0
                    ),
                )
                for task_id in task_ids
            ),
            3,
        )
        basis_note = (
            "measured path using recorded task durations with wave-barrier edges"
        )
    else:
        mode = "measured_partial"
        duration_seconds = round(
            sum(
                max(
                    0.0,
                    _safe_float(
                        task_lookup.get(task_id, {}).get("duration_seconds"), 0.0
                    ),
                )
                if measured_flags.get(task_id, False)
                else 0.0
                for task_id in task_ids
            ),
            3,
        )
        basis_note = "measured path with structural fallback for tasks lacking timing and wave-barrier edges"

    wave_ids = sorted(
        {
            task_to_wave.get(task_id, 0)
            for task_id in task_ids
            if task_to_wave.get(task_id, 0) > 0
        }
    )
    return {
        "status": "available",
        "mode": mode,
        "task_ids": task_ids,
        "wave_ids": wave_ids,
        "duration_seconds": duration_seconds,
        "weight": round(best_score.get(terminal, 0.0), 3),
        "basis_note": basis_note,
    }


def _parse_file_conflicts(
    *,
    plan_check_payload: dict[str, Any],
    task_to_wave: dict[str, int],
    wave_rows: list[dict[str, Any]],
    task_rows: list[dict[str, Any]],
    contract_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    conflicts: list[dict[str, Any]] = []
    task_lookup = _build_task_lookup(task_rows)
    contract_task_details = _task_contract_details(contract_payload)
    raw_errors = _safe_list(plan_check_payload.get("errors"))
    for raw_error in raw_errors:
        message = _safe_string(raw_error)
        if not message:
            continue
        write_match = _WRITE_CONFLICT_RE.search(message)
        if write_match:
            left = write_match.group("left")
            right = write_match.group("right")
            left_wave = task_to_wave.get(left, 0)
            right_wave = task_to_wave.get(right, 0)
            wave = left_wave if left_wave and left_wave == right_wave else 0
            declared_paths = _declared_overlap_paths(
                left_task_id=left,
                right_task_id=right,
                contract_task_details=contract_task_details,
            )
            observed_paths = _observed_overlap_paths(
                left_task_id=left,
                right_task_id=right,
                task_lookup=task_lookup,
            )
            paths = _dedupe_strings(declared_paths + observed_paths)
            detail = message
            if paths:
                detail = f"{message} (overlap: {', '.join(paths)})"
            conflicts.append(
                {
                    "kind": _CONFLICT_KIND_WRITE,
                    "wave": wave,
                    "tasks": [left, right],
                    "paths": paths,
                    "conflict_group": "",
                    "detail": detail,
                }
            )
            continue
        group_match = _GROUP_CONFLICT_RE.search(message)
        if group_match:
            left = group_match.group("left")
            right = group_match.group("right")
            group = group_match.group("group")
            left_wave = task_to_wave.get(left, 0)
            right_wave = task_to_wave.get(right, 0)
            wave = left_wave if left_wave and left_wave == right_wave else 0
            paths = _observed_overlap_paths(
                left_task_id=left,
                right_task_id=right,
                task_lookup=task_lookup,
            )
            detail = message
            if paths:
                detail = f"{message} (observed overlap: {', '.join(paths)})"
            conflicts.append(
                {
                    "kind": _CONFLICT_KIND_GROUP,
                    "wave": wave,
                    "tasks": [left, right],
                    "paths": paths,
                    "conflict_group": group,
                    "detail": detail,
                }
            )
    for row in wave_rows:
        out_of_contract_paths = _non_empty_strings(row.get("out_of_contract_paths"))
        if not out_of_contract_paths:
            continue
        wave_task_ids = _task_ids_for_paths(
            task_ids=list(_non_empty_strings(row.get("tasks"))),
            task_lookup=task_lookup,
            paths=out_of_contract_paths,
        )
        conflicts.append(
            {
                "kind": _CONFLICT_KIND_OUT_OF_CONTRACT,
                "wave": _safe_int(row.get("wave"), 0),
                "tasks": wave_task_ids,
                "paths": out_of_contract_paths,
                "conflict_group": "",
                "detail": (
                    f"out-of-contract edits detected in wave {_safe_int(row.get('wave'), 0)}: "
                    f"{', '.join(out_of_contract_paths)}"
                ),
            }
        )
    conflicts.sort(
        key=lambda row: (
            _safe_int(row.get("wave"), 0),
            _safe_string(row.get("kind")),
            tuple(_non_empty_strings(row.get("tasks"))),
        )
    )
    return conflicts


def _build_wave_summary(
    *,
    execution_summary_payload: dict[str, Any],
    wave_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    waves_total = _safe_int(
        execution_summary_payload.get("waves_total"), len(wave_rows)
    )
    if waves_total <= 0:
        waves_total = len(wave_rows)
    waves_executed = _safe_int(execution_summary_payload.get("waves_executed"), 0)
    if waves_executed <= 0 and wave_rows:
        waves_executed = len(
            [
                row
                for row in wave_rows
                if _safe_string(row.get("status")) in {"completed", "failed", "blocked"}
                or _safe_int(row.get("attempts"), 0) > 0
                or bool(row.get("timing_available"))
            ]
        )
    current_wave: int | None = None
    for row in wave_rows:
        status = _safe_string(row.get("status")).lower()
        if status in {"pending", "failed", "blocked"}:
            current_wave = _safe_int(row.get("wave"), 0) or None
            break
    if current_wave is None and wave_rows:
        current_wave = _safe_int(wave_rows[-1].get("wave"), 0) or None
    return {
        "status": "available" if wave_rows else "unavailable",
        "current": current_wave,
        "executed": max(0, waves_executed),
        "total": max(0, waves_total),
    }


def _build_task_summary(
    *,
    execution_summary_payload: dict[str, Any],
    task_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    if task_rows:
        total = len(task_rows)
        completed = len(
            [row for row in task_rows if _safe_string(row.get("status")) == "completed"]
        )
        failed = len(
            [row for row in task_rows if _safe_string(row.get("status")) == "failed"]
        )
        blocked = len(
            [row for row in task_rows if _safe_string(row.get("status")) == "blocked"]
        )
        pending = len(
            [row for row in task_rows if _safe_string(row.get("status")) == "pending"]
        )
        skipped = len(
            [
                row
                for row in task_rows
                if _safe_string(row.get("reason_code"))
                == _REASON_CODE_FAIL_FAST_SKIPPED
            ]
        )
        deferred = len(
            [
                row
                for row in task_rows
                if _safe_string(row.get("reason_code"))
                == _REASON_CODE_WAVE_RETRY_PENDING
            ]
        )
    else:
        total = _safe_int(execution_summary_payload.get("tasks_total"), 0)
        completed = _safe_int(execution_summary_payload.get("tasks_completed"), 0)
        failed = _safe_int(execution_summary_payload.get("tasks_failed"), 0)
        blocked = _safe_int(execution_summary_payload.get("tasks_blocked"), 0)
        pending = _safe_int(execution_summary_payload.get("tasks_pending"), 0)
        skipped = _safe_int(execution_summary_payload.get("tasks_skipped"), 0)
        deferred = _safe_int(execution_summary_payload.get("tasks_deferred"), 0)
    return {
        "status": "available" if task_rows or total > 0 else "unavailable",
        "total": total,
        "completed": completed,
        "failed": failed,
        "blocked": blocked,
        "pending": pending,
        "skipped": skipped,
        "deferred": deferred,
        "task_details": task_rows,
    }


def build_wave_observability(
    repo_root: Path,
    *,
    iteration_dir: Path | None,
    contract_payload: dict[str, Any] | None = None,
    graph_payload: dict[str, Any] | None = None,
    plan_check_payload: dict[str, Any] | None = None,
    execution_state_payload: dict[str, Any] | None = None,
    execution_summary_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_paths = {
        "plan_graph_path": _repo_relative(
            repo_root, repo_root / ".autolab" / "plan_graph.json"
        ),
        "plan_check_result_path": _repo_relative(
            repo_root, repo_root / ".autolab" / "plan_check_result.json"
        ),
        "plan_execution_state_path": _repo_relative(
            repo_root,
            iteration_dir / "plan_execution_state.json"
            if iteration_dir is not None
            else None,
        ),
        "plan_execution_summary_path": _repo_relative(
            repo_root,
            iteration_dir / "plan_execution_summary.json"
            if iteration_dir is not None
            else None,
        ),
    }
    diagnostics: list[str] = []
    if iteration_dir is None:
        diagnostics.append("iteration directory unavailable for wave observability")
        return {
            "status": "unavailable",
            "wave_summary": {
                "status": "unavailable",
                "current": None,
                "executed": 0,
                "total": 0,
            },
            "task_summary": {
                "status": "unavailable",
                "total": 0,
                "completed": 0,
                "failed": 0,
                "blocked": 0,
                "pending": 0,
                "skipped": 0,
                "deferred": 0,
                "task_details": [],
            },
            "summary": {
                "waves_total": 0,
                "waves_executed": 0,
                "tasks_total": 0,
                "tasks_completed": 0,
                "tasks_failed": 0,
                "tasks_blocked": 0,
                "tasks_pending": 0,
                "tasks_skipped": 0,
                "tasks_deferred": 0,
                "retrying_waves": 0,
                "conflict_count": 0,
            },
            "critical_path": {
                "status": "unavailable",
                "mode": "unavailable",
                "task_ids": [],
                "wave_ids": [],
                "duration_seconds": 0.0,
                "weight": 0.0,
                "basis_note": "iteration directory unavailable",
            },
            "file_conflicts": [],
            "waves": [],
            "tasks": [],
            "diagnostics": diagnostics,
            "source_paths": source_paths,
        }

    if graph_payload is None:
        graph_payload = _load_optional_mapping(
            repo_root / ".autolab" / "plan_graph.json"
        )
    if plan_check_payload is None:
        plan_check_payload = _load_optional_mapping(
            repo_root / ".autolab" / "plan_check_result.json"
        )
    if execution_state_payload is None:
        execution_state_payload = _load_optional_mapping(
            iteration_dir / "plan_execution_state.json"
        )
    if execution_summary_payload is None:
        execution_summary_payload = _load_optional_mapping(
            iteration_dir / "plan_execution_summary.json"
        )

    graph_payload = _safe_dict(graph_payload)
    plan_check_payload = _safe_dict(plan_check_payload)
    execution_state_payload = _safe_dict(execution_state_payload)
    execution_summary_payload = _safe_dict(execution_summary_payload)

    expected_iteration_id = _expected_iteration_id(
        iteration_dir=iteration_dir,
        execution_state_payload=execution_state_payload,
        execution_summary_payload=execution_summary_payload,
        graph_payload=graph_payload,
        plan_check_payload=plan_check_payload,
    )
    execution_state_payload = _filter_payload_for_iteration(
        execution_state_payload,
        label="plan_execution_state payload",
        expected_iteration_id=expected_iteration_id,
        diagnostics=diagnostics,
    )
    execution_summary_payload = _filter_payload_for_iteration(
        execution_summary_payload,
        label="plan_execution_summary payload",
        expected_iteration_id=expected_iteration_id,
        diagnostics=diagnostics,
    )
    graph_payload = _filter_payload_for_iteration(
        graph_payload,
        label="plan_graph payload",
        expected_iteration_id=expected_iteration_id,
        diagnostics=diagnostics,
    )
    plan_check_payload = _filter_payload_for_iteration(
        plan_check_payload,
        label="plan_check_result payload",
        expected_iteration_id=expected_iteration_id,
        diagnostics=diagnostics,
    )
    contract_payload = _load_contract_payload(
        repo_root,
        iteration_dir=iteration_dir,
        execution_state_payload=execution_state_payload,
        expected_iteration_id=expected_iteration_id,
        diagnostics=diagnostics,
        contract_payload=contract_payload,
    )

    if not graph_payload:
        diagnostics.append("plan_graph.json unavailable")
    if not execution_summary_payload:
        diagnostics.append("plan_execution_summary.json unavailable")
    if not execution_state_payload:
        diagnostics.append("plan_execution_state.json unavailable")
    if not plan_check_payload:
        diagnostics.append("plan_check_result.json unavailable")

    task_to_wave_seed: dict[str, int] = {}
    for row in _safe_list(graph_payload.get("waves")):
        if not isinstance(row, dict):
            continue
        wave = _safe_int(row.get("wave"), 0)
        for task_id in _non_empty_strings(row.get("tasks")):
            if wave > 0:
                task_to_wave_seed[task_id] = wave
    task_rows = _build_task_rows(
        repo_root,
        task_to_wave=task_to_wave_seed,
        execution_state_payload=execution_state_payload,
        execution_summary_payload=execution_summary_payload,
    )
    wave_rows, task_to_wave, _wave_to_tasks = _build_wave_rows(
        graph_payload=graph_payload,
        execution_state_payload=execution_state_payload,
        execution_summary_payload=execution_summary_payload,
        task_rows=task_rows,
    )
    critical_path = _build_critical_path(
        graph_payload=graph_payload,
        task_rows=task_rows,
        task_to_wave=task_to_wave,
        wave_rows=wave_rows,
    )
    critical_task_ids = set(_non_empty_strings(critical_path.get("task_ids")))
    critical_wave_ids = {
        _safe_int(wave, 0)
        for wave in _safe_list(critical_path.get("wave_ids"))
        if _safe_int(wave, 0) > 0
    }
    for row in wave_rows:
        row["critical_path"] = _safe_int(row.get("wave"), 0) in critical_wave_ids
    for row in task_rows:
        row["critical_path"] = _safe_string(row.get("task_id")) in critical_task_ids

    file_conflicts = _parse_file_conflicts(
        plan_check_payload=plan_check_payload,
        task_to_wave=task_to_wave,
        wave_rows=wave_rows,
        task_rows=task_rows,
        contract_payload=contract_payload,
    )
    wave_summary = _build_wave_summary(
        execution_summary_payload=execution_summary_payload,
        wave_rows=wave_rows,
    )
    task_summary = _build_task_summary(
        execution_summary_payload=execution_summary_payload,
        task_rows=task_rows,
    )
    summary = {
        "waves_total": _safe_int(wave_summary.get("total"), 0),
        "waves_executed": _safe_int(wave_summary.get("executed"), 0),
        "tasks_total": _safe_int(task_summary.get("total"), 0),
        "tasks_completed": _safe_int(task_summary.get("completed"), 0),
        "tasks_failed": _safe_int(task_summary.get("failed"), 0),
        "tasks_blocked": _safe_int(task_summary.get("blocked"), 0),
        "tasks_pending": _safe_int(task_summary.get("pending"), 0),
        "tasks_skipped": _safe_int(task_summary.get("skipped"), 0),
        "tasks_deferred": _safe_int(task_summary.get("deferred"), 0),
        "retrying_waves": len([row for row in wave_rows if row.get("retry_pending")]),
        "conflict_count": len(file_conflicts),
    }
    return {
        "status": "available"
        if (
            task_rows
            or wave_rows
            or file_conflicts
            or critical_path.get("status") == "available"
        )
        else "unavailable",
        "wave_summary": wave_summary,
        "task_summary": task_summary,
        "summary": summary,
        "critical_path": critical_path,
        "file_conflicts": file_conflicts,
        "waves": wave_rows,
        "tasks": task_rows,
        "diagnostics": diagnostics,
        "source_paths": source_paths,
    }


__all__ = ["build_wave_observability"]
