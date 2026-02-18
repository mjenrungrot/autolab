"""Autolab state — persistence, locking, backlog helpers, and iteration scaffolding."""

from __future__ import annotations

import json
import os
import shutil
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from autolab.constants import (
    ALL_STAGES,
    ASSISTANT_CYCLE_STAGES,
    BACKLOG_COMPLETED_STATUSES,
    DEFAULT_EXPERIMENT_TYPE,
    EXPERIMENT_TYPES,
    PACKAGE_SCAFFOLD_DIR,
)
from autolab.models import StageCheckError, StateError
from autolab.utils import (
    _ensure_json_file,
    _ensure_text_file,
    _is_backlog_status_completed,
    _is_experiment_type_locked,
    _normalize_backlog_status,
    _normalize_experiment_type,
    _normalize_space,
    _parse_utc,
    _read_json,
    _utc_now,
    _write_json,
)


# ---------------------------------------------------------------------------
# State loading / normalisation
# ---------------------------------------------------------------------------


def _load_state(path: Path) -> dict[str, object]:
    payload = _read_json(path)
    return payload


def _normalize_state(state: dict[str, Any]) -> dict[str, Any]:
    required = (
        "iteration_id",
        "stage",
        "stage_attempt",
        "last_run_id",
        "sync_status",
        "max_stage_attempts",
        "max_total_iterations",
    )
    missing = [key for key in required if key not in state]
    if missing:
        raise StateError(f"state file missing required keys: {missing}")

    normalized = dict(state)
    iteration_id = str(normalized.get("iteration_id", "")).strip()
    if not iteration_id or iteration_id.startswith("<"):
        raise StateError("state.iteration_id must be set to a real identifier")
    normalized["iteration_id"] = iteration_id

    stage = str(normalized.get("stage", "")).strip()
    if stage not in ALL_STAGES:
        raise StateError(f"state.stage must be one of {sorted(ALL_STAGES)}, got '{stage}'")
    normalized["stage"] = stage

    for key in ("stage_attempt", "max_stage_attempts", "max_total_iterations"):
        try:
            value = int(normalized.get(key))
        except Exception as exc:
            raise StateError(f"state.{key} must be an integer") from exc
        if value < 0:
            raise StateError(f"state.{key} must be >= 0")
        normalized[key] = value

    if normalized["max_stage_attempts"] <= 0:
        raise StateError("state.max_stage_attempts must be > 0")
    if normalized["max_total_iterations"] <= 0:
        raise StateError("state.max_total_iterations must be > 0")

    normalized["last_run_id"] = str(normalized.get("last_run_id", "")).strip()
    normalized["sync_status"] = str(normalized.get("sync_status", "")).strip()
    normalized["experiment_id"] = str(normalized.get("experiment_id", "")).strip()
    assistant_mode = str(normalized.get("assistant_mode", "off")).strip().lower()
    if assistant_mode not in {"off", "on"}:
        assistant_mode = "off"
    normalized["assistant_mode"] = assistant_mode
    normalized["current_task_id"] = str(normalized.get("current_task_id", "")).strip()
    task_cycle_stage = str(normalized.get("task_cycle_stage", "select")).strip().lower()
    if task_cycle_stage not in ASSISTANT_CYCLE_STAGES:
        task_cycle_stage = "select"
    normalized["task_cycle_stage"] = task_cycle_stage
    repeat_guard_raw = normalized.get("repeat_guard", {})
    if not isinstance(repeat_guard_raw, dict):
        repeat_guard_raw = {}
    repeat_guard = {
        "last_decision": str(repeat_guard_raw.get("last_decision", "")).strip(),
        "same_decision_streak": 0,
        "last_open_task_count": -1,
        "no_progress_decisions": 0,
        "update_docs_cycle_count": 0,
        "last_verification_passed": bool(repeat_guard_raw.get("last_verification_passed", False)),
    }
    for key in ("same_decision_streak", "last_open_task_count", "no_progress_decisions", "update_docs_cycle_count"):
        try:
            value = int(repeat_guard_raw.get(key, repeat_guard[key]))
        except Exception:
            value = repeat_guard[key]
        repeat_guard[key] = value
    normalized["repeat_guard"] = repeat_guard
    baseline_raw = normalized.get("task_change_baseline", {})
    if not isinstance(baseline_raw, dict):
        baseline_raw = {}
    baseline_snapshot: dict[str, str] = {}
    for raw_path, raw_signature in baseline_raw.items():
        path = str(raw_path).strip()
        signature = str(raw_signature).strip()
        if path and signature:
            baseline_snapshot[path] = signature
    normalized["task_change_baseline"] = baseline_snapshot

    history_raw = normalized.get("history", [])
    history: list[dict[str, Any]] = []
    if isinstance(history_raw, list):
        for entry in history_raw[-200:]:
            if not isinstance(entry, dict):
                continue
            serialized: dict[str, Any] = {}
            for key, value in entry.items():
                serialized[str(key)] = value
            history.append(serialized)
    normalized["history"] = history
    return normalized


# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------


def _resolve_repo_root(state_path: Path) -> Path:
    if state_path.name == "state.json" and state_path.parent.name == ".autolab":
        return state_path.parent.parent
    return Path.cwd()


def _resolve_autolab_dir(state_path: Path, repo_root: Path) -> Path:
    if state_path.name == "state.json" and state_path.parent.name == ".autolab":
        return state_path.parent
    return repo_root / ".autolab"


def _resolve_scaffold_source() -> Path:
    if PACKAGE_SCAFFOLD_DIR.exists():
        return PACKAGE_SCAFFOLD_DIR
    raise RuntimeError("bundled autolab scaffold is unavailable in this installation")


def _sync_scaffold_bundle(
    source_root: Path,
    destination_root: Path,
    *,
    overwrite: bool,
) -> tuple[int, int]:
    copied = 0
    skipped = 0
    for source in source_root.rglob("*"):
        relative = source.relative_to(source_root)
        destination = destination_root / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and not overwrite:
            skipped += 1
            continue
        shutil.copy2(source, destination)
        copied += 1
    return copied, skipped


# ---------------------------------------------------------------------------
# Iteration directory resolution
# ---------------------------------------------------------------------------


def _resolve_iteration_directory(
    repo_root: Path,
    *,
    iteration_id: str,
    experiment_id: str = "",
    require_exists: bool = False,
) -> tuple[Path, str]:
    normalized_iteration_id = _normalize_space(iteration_id)
    if not normalized_iteration_id:
        raise StageCheckError("state.iteration_id must be set to a real identifier")

    experiments_root = repo_root / "experiments"
    preferred_type = _resolve_experiment_type_from_backlog(
        repo_root,
        iteration_id=normalized_iteration_id,
        experiment_id=experiment_id,
    )

    candidates: list[tuple[Path, str]] = []
    if preferred_type:
        candidates.append((experiments_root / preferred_type / normalized_iteration_id, preferred_type))
    for experiment_type in EXPERIMENT_TYPES:
        candidate = experiments_root / experiment_type / normalized_iteration_id
        if all(existing_path != candidate for existing_path, _ in candidates):
            candidates.append((candidate, experiment_type))

    for candidate_path, candidate_type in candidates:
        if candidate_path.exists():
            return (candidate_path, candidate_type)

    if require_exists:
        searched = ", ".join(str(path) for path, _ in candidates)
        raise StageCheckError(
            f"iteration workspace is missing for iteration_id '{normalized_iteration_id}' (searched: {searched})"
        )

    resolved_type = preferred_type or DEFAULT_EXPERIMENT_TYPE
    return (experiments_root / resolved_type / normalized_iteration_id, resolved_type)


# ---------------------------------------------------------------------------
# Backlog helpers (YAML)
# ---------------------------------------------------------------------------


def _resolve_experiment_type_from_backlog(
    repo_root: Path,
    *,
    iteration_id: str,
    experiment_id: str = "",
) -> str:
    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    payload, _load_error = _load_backlog_yaml(backlog_path)
    if payload is None:
        return ""

    entry, _resolve_error = _find_backlog_experiment_entry(
        payload,
        experiment_id=_normalize_space(experiment_id),
        iteration_id=_normalize_space(iteration_id),
    )
    if entry is None:
        return ""

    explicit_type = _normalize_experiment_type(entry.get("type"))
    if explicit_type:
        return explicit_type
    if _is_backlog_status_completed(entry.get("status")):
        return "done"
    return ""


def _find_backlog_experiment_entry(
    backlog_payload: dict[str, Any],
    *,
    experiment_id: str,
    iteration_id: str,
) -> tuple[dict[str, Any] | None, str]:
    experiments = backlog_payload.get("experiments")
    if not isinstance(experiments, list):
        return (None, "backlog experiments list is missing")

    normalized_experiment_id = _normalize_space(experiment_id)
    normalized_iteration_id = _normalize_space(iteration_id)

    if normalized_experiment_id:
        matches: list[dict[str, Any]] = []
        for entry in experiments:
            if not isinstance(entry, dict):
                continue
            if _normalize_space(str(entry.get("id", ""))) == normalized_experiment_id:
                matches.append(entry)
        if not matches:
            return (None, f"backlog experiment '{normalized_experiment_id}' was not found")
        if len(matches) > 1:
            return (None, f"backlog experiment id '{normalized_experiment_id}' is duplicated")
        return (matches[0], "")

    if not normalized_iteration_id:
        return (None, "state.iteration_id is unset")

    matches: list[dict[str, Any]] = []
    for entry in experiments:
        if not isinstance(entry, dict):
            continue
        if _normalize_space(str(entry.get("iteration_id", ""))) == normalized_iteration_id:
            matches.append(entry)
    if not matches:
        return (None, f"no backlog experiment matches iteration_id '{normalized_iteration_id}'")
    if len(matches) > 1:
        duplicates: list[str] = []
        for match in matches:
            entry_id = _normalize_space(str(match.get("id", "")))
            if entry_id:
                duplicates.append(entry_id)
        if not duplicates:
            duplicates = ["<unidentified>"]
        return (None, f"multiple backlog experiments match iteration_id '{normalized_iteration_id}': {', '.join(duplicates)}")
    return (matches[0], "")


def _is_active_experiment_completed(
    repo_root: Path,
    state: dict[str, Any],
) -> tuple[bool, str]:
    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    payload, load_error = _load_backlog_yaml(backlog_path)
    if payload is None:
        return (False, load_error)

    experiment_id = _normalize_space(str(state.get("experiment_id", "")))
    iteration_id = _normalize_space(str(state.get("iteration_id", "")))
    entry, resolve_error = _find_backlog_experiment_entry(
        payload,
        experiment_id=experiment_id,
        iteration_id=iteration_id,
    )
    if entry is None:
        return (False, resolve_error)

    experiment_label = _normalize_space(str(entry.get("id", ""))) or experiment_id or iteration_id
    experiment_type = _normalize_experiment_type(entry.get("type"))
    if _is_experiment_type_locked(experiment_type):
        return (
            True,
            f"backlog experiment '{experiment_label}' is type '{experiment_type}'",
        )

    status = _normalize_backlog_status(entry.get("status"))
    if _is_backlog_status_completed(status):
        return (
            True,
            f"backlog experiment '{experiment_label}' is marked '{status}'",
        )
    return (
        False,
        f"backlog experiment '{experiment_label}' type is '{experiment_type or DEFAULT_EXPERIMENT_TYPE}' and status is '{status or 'open'}'",
    )


def _load_backlog_yaml(path: Path) -> tuple[dict[str, Any] | None, str]:
    if yaml is None:
        return (None, "PyYAML is unavailable")
    if not path.exists():
        return (None, f"backlog file is missing at {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return (None, f"backlog file could not be parsed: {exc}")
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        return (None, f"backlog file must contain a mapping at {path}")
    return (loaded, "")


def _write_backlog_yaml(path: Path, payload: dict[str, Any]) -> tuple[bool, str]:
    if yaml is None:
        return (False, "PyYAML is unavailable")
    try:
        rendered = yaml.safe_dump(payload, sort_keys=False)
    except Exception as exc:
        return (False, f"backlog file could not be serialized: {exc}")
    if not rendered.endswith("\n"):
        rendered = f"{rendered}\n"
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current == rendered:
        return (False, "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(rendered, encoding="utf-8")
    return (True, "")


def _infer_unique_experiment_id_from_backlog(repo_root: Path, iteration_id: str) -> tuple[str, str]:
    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    payload, load_error = _load_backlog_yaml(backlog_path)
    if payload is None:
        return ("", load_error)
    experiments = payload.get("experiments")
    if not isinstance(experiments, list):
        return ("", "backlog experiments list is missing")

    matches: list[str] = []
    seen: set[str] = set()
    for entry in experiments:
        if not isinstance(entry, dict):
            continue
        entry_iteration_id = str(entry.get("iteration_id", "")).strip()
        if entry_iteration_id != iteration_id:
            continue
        experiment_id = str(entry.get("id", "")).strip()
        if not experiment_id or experiment_id in seen:
            continue
        seen.add(experiment_id)
        matches.append(experiment_id)

    if not matches:
        return ("", f"no backlog experiment matches iteration_id '{iteration_id}'")
    if len(matches) > 1:
        return ("", f"multiple backlog experiments match iteration_id '{iteration_id}': {', '.join(matches)}")
    return (matches[0], "")


def _mark_backlog_experiment_completed(
    repo_root: Path,
    experiment_id: str,
) -> tuple[bool, Path | None, str]:
    normalized_experiment_id = str(experiment_id).strip()
    if not normalized_experiment_id:
        return (
            False,
            None,
            "state.experiment_id is unset; backlog completion could not be applied",
        )

    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    payload, load_error = _load_backlog_yaml(backlog_path)
    if payload is None:
        return (False, None, load_error)

    entry, resolve_error = _find_backlog_experiment_entry(
        payload,
        experiment_id=normalized_experiment_id,
        iteration_id="",
    )
    if entry is None:
        return (False, None, resolve_error)

    status = _normalize_backlog_status(entry.get("status"))
    experiment_type = _normalize_experiment_type(entry.get("type"))
    already_completed = _is_backlog_status_completed(status) and experiment_type == "done"
    if already_completed:
        return (
            False,
            None,
            f"backlog experiment '{normalized_experiment_id}' is already completed",
        )

    entry["status"] = "completed"
    entry["type"] = "done"
    changed, write_error = _write_backlog_yaml(backlog_path, payload)
    if write_error:
        return (False, None, write_error)
    if not changed:
        return (
            False,
            None,
            f"backlog experiment '{normalized_experiment_id}' completion was already up to date",
        )
    return (
        True,
        backlog_path,
        f"marked backlog experiment '{normalized_experiment_id}' as completed (type=done)",
    )


def _parse_iteration_from_backlog(path: Path) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        try:
            loaded = yaml.safe_load(text)
        except Exception:
            loaded = None
        if isinstance(loaded, dict):
            experiments = loaded.get("experiments")
            if isinstance(experiments, list):
                for entry in experiments:
                    if isinstance(entry, dict):
                        candidate = str(entry.get("iteration_id", "")).strip()
                        if candidate and not candidate.startswith("<"):
                            return candidate
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("iteration_id:"):
            candidate = stripped.split(":", 1)[1].strip().strip("'\"")
            if candidate and not candidate.startswith("<"):
                return candidate
    return ""


# ---------------------------------------------------------------------------
# Bootstrap defaults
# ---------------------------------------------------------------------------


def _bootstrap_iteration_id() -> str:
    return "bootstrap_iteration"


def _default_state(iteration_id: str) -> dict[str, Any]:
    return {
        "iteration_id": iteration_id,
        "experiment_id": "",
        "stage": "hypothesis",
        "stage_attempt": 0,
        "last_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 5,
        "max_total_iterations": 50,
        "assistant_mode": "off",
        "current_task_id": "",
        "task_cycle_stage": "select",
        "repeat_guard": {
            "last_decision": "",
            "same_decision_streak": 0,
            "last_open_task_count": -1,
            "no_progress_decisions": 0,
            "update_docs_cycle_count": 0,
            "last_verification_passed": False,
        },
        "task_change_baseline": {},
        "history": [],
    }


def _default_agent_result() -> dict[str, Any]:
    return {
        "status": "complete",
        "summary": "autolab bootstrap initialized",
        "changed_files": [],
        "completion_token_seen": True,
    }


def _append_state_history(
    state: dict[str, Any],
    *,
    stage_before: str,
    stage_after: str,
    status: str,
    summary: str,
    decision: str = "",
    verification: dict[str, Any] | None = None,
    max_entries: int = 200,
) -> None:
    history_raw = state.get("history", [])
    history: list[dict[str, Any]]
    if isinstance(history_raw, list):
        history = [entry for entry in history_raw if isinstance(entry, dict)]
    else:
        history = []
    entry: dict[str, Any] = {
        "timestamp_utc": _utc_now(),
        "stage_before": str(stage_before).strip(),
        "stage_after": str(stage_after).strip(),
        "status": str(status).strip(),
        "summary": str(summary).strip(),
        "stage_attempt": int(state.get("stage_attempt", 0) or 0),
    }
    normalized_decision = str(decision).strip()
    if normalized_decision:
        entry["decision"] = normalized_decision
    if isinstance(verification, dict):
        entry["verification"] = verification
    history.append(entry)
    if len(history) > max_entries:
        history = history[-max_entries:]
    state["history"] = history


# ---------------------------------------------------------------------------
# Iteration skeleton
# ---------------------------------------------------------------------------


def _ensure_iteration_skeleton(
    repo_root: Path,
    iteration_id: str,
    created: list[Path],
    experiment_type: str = DEFAULT_EXPERIMENT_TYPE,
) -> None:
    normalized_type = _normalize_experiment_type(experiment_type) or DEFAULT_EXPERIMENT_TYPE
    iteration_dir = repo_root / "experiments" / normalized_type / iteration_id
    _ensure_text_file(
        iteration_dir / "hypothesis.md",
        (
            "# Hypothesis Statement\n\n"
            "## Primary Metric\n"
            "PrimaryMetric: primary_metric; Unit: unit; Success: baseline +0.0\n\n"
            "- metric: primary_metric\n"
            "- target_delta: 0.0\n"
            "- criteria: define operational success criteria for design stage\n"
        ),
        created,
    )
    _ensure_text_file(
        iteration_dir / "design.yaml",
        (
            f'id: "e1"\n'
            f'iteration_id: "{iteration_id}"\n'
            'hypothesis_id: "h1"\n'
            "entrypoint:\n"
            '  module: "tinydesk_v4.train"\n'
            "  args:\n"
            '    config: "TODO: set config path"\n'
            "compute:\n"
            '  location: "local"\n'
            '  walltime_estimate: "00:30:00"\n'
            '  memory_estimate: "64GB"\n'
            "  gpu_count: 0\n"
            "metrics:\n"
            "  primary:\n"
            '    name: "primary_metric"\n'
            '    unit: "unit"\n'
            '    mode: "maximize"\n'
            "  secondary: []\n"
            '  success_delta: "TODO: define target delta"\n'
            '  aggregation: "mean"\n'
            '  baseline_comparison: "TODO: define baseline comparison"\n'
            "baselines:\n"
            '  - name: "baseline_current"\n'
            '    description: "TODO: describe current baseline"\n'
        ),
        created,
    )
    _ensure_text_file(
        iteration_dir / "implementation_plan.md",
        "# Implementation Plan\n\n- Implement the design requirements.\n",
        created,
    )
    _ensure_text_file(
        iteration_dir / "implementation" / "README.md",
        (
            "# Experiment Implementation\n\n"
            "Store experiment-specific implementation artifacts in this directory.\n"
            "Notebook and other per-iteration code artifacts should stay here by default.\n"
        ),
        created,
    )
    _ensure_text_file(
        iteration_dir / "implementation_review.md",
        "# Implementation Review\n\nReview notes.\n",
        created,
    )
    _ensure_json_file(
        iteration_dir / "review_result.json",
        {
            "status": "pass",
            "blocking_findings": [],
            "required_checks": {
                "tests": "skip",
                "dry_run": "skip",
                "schema": "pass",
                "env_smoke": "skip",
                "docs_target_update": "skip",
            },
            "reviewed_at": "1970-01-01T00:00:00Z",
        },
        created,
    )
    _ensure_text_file(
        iteration_dir / "launch" / "run_local.sh",
        "#!/usr/bin/env bash\nset -euo pipefail\n# local launch placeholder\n",
        created,
    )
    _ensure_text_file(
        iteration_dir / "launch" / "run_slurm.sbatch",
        "#!/usr/bin/env bash\nset -euo pipefail\n# slurm launch placeholder\n",
        created,
    )
    _ensure_text_file(
        iteration_dir / "analysis" / "summary.md",
        "# Analysis Summary\n\nInitial summary.\n",
        created,
    )
    _ensure_text_file(
        iteration_dir / "docs_update.md",
        "# Documentation Update\n\nNo changes needed.\n",
        created,
    )
    runs_dir = iteration_dir / "runs"
    if not runs_dir.exists():
        runs_dir.mkdir(parents=True, exist_ok=True)
        created.append(runs_dir)


# ---------------------------------------------------------------------------
# Locking
# ---------------------------------------------------------------------------


def _read_lock_payload(lock_path: Path) -> dict[str, Any]:
    if not lock_path.exists():
        return {}
    try:
        loaded = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _lock_age_seconds(existing: dict[str, Any], *, now: datetime) -> float | None:
    heartbeat = _parse_utc(str(existing.get("last_heartbeat_at", "")))
    if heartbeat is None:
        return None
    return max(0.0, (now - heartbeat).total_seconds())


def _write_lock_payload_exclusive(lock_path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, indent=2) + "\n"
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(rendered)


def _acquire_lock(lock_path: Path, *, state_file: Path, command: str, stale_seconds: int) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    started_at = _utc_now()
    monotonic_now = time.monotonic()
    owner_uuid = uuid.uuid4().hex
    lock_payload: dict[str, Any] = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "owner_uuid": owner_uuid,
        "started_at": started_at,
        "last_heartbeat_at": started_at,
        "started_monotonic": monotonic_now,
        "last_heartbeat_monotonic": monotonic_now,
        "command": command,
        "state_file": str(state_file),
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    stale_replaced = False
    for _ in range(3):
        try:
            _write_lock_payload_exclusive(lock_path, lock_payload)
            if stale_replaced:
                return (True, f"replaced stale lock at {lock_path}")
            return (True, f"lock acquired at {lock_path}")
        except FileExistsError:
            existing = _read_lock_payload(lock_path)
            age_seconds = _lock_age_seconds(existing, now=now)
            holder_pid = existing.get("pid", "<unknown>")
            holder_host = existing.get("host", "<unknown>")
            holder_owner = existing.get("owner_uuid", "<unknown>")
            holder_state = existing.get("state_file", "<unknown>")
            holder_command = existing.get("command", "<unknown>")
            heartbeat = _parse_utc(str(existing.get("last_heartbeat_at", "")))
            if heartbeat is not None and now - heartbeat <= timedelta(seconds=stale_seconds):
                age_text = f"{age_seconds:.0f}s" if age_seconds is not None else "unknown"
                return (
                    False,
                    (
                        f"active lock exists at {lock_path} "
                        f"(pid={holder_pid}, host={holder_host}, owner_uuid={holder_owner}, "
                        f"age={age_text}, state_file={holder_state}, command={holder_command})"
                    ),
                )

            stale_path = lock_path.with_suffix(f"{lock_path.suffix}.stale.{owner_uuid[:8]}")
            try:
                os.replace(lock_path, stale_path)
            except FileNotFoundError:
                continue
            except OSError:
                return (False, f"failed to replace stale lock at {lock_path}")
            stale_replaced = True
            continue
        except OSError as exc:
            return (False, f"failed to acquire lock at {lock_path}: {exc}")
    return (False, f"failed to acquire lock at {lock_path} after retries")


def _heartbeat_lock(lock_path: Path) -> None:
    if not lock_path.exists():
        return
    payload = _read_lock_payload(lock_path)
    if not payload:
        return
    payload["last_heartbeat_at"] = _utc_now()
    payload["last_heartbeat_monotonic"] = time.monotonic()
    _write_json(lock_path, payload)


def _release_lock(lock_path: Path) -> None:
    if not lock_path.exists():
        return
    payload = _read_lock_payload(lock_path)
    if isinstance(payload, dict):
        holder_pid = int(payload.get("pid", -1)) if str(payload.get("pid", "")).isdigit() else -1
        if holder_pid not in {-1, os.getpid()}:
            return
    lock_path.unlink(missing_ok=True)


def _inspect_lock(lock_path: Path) -> dict[str, Any] | None:
    """Return lock payload with computed age, or None if no lock exists."""
    if not lock_path.exists():
        return None
    payload = _read_lock_payload(lock_path)
    if not payload:
        return None
    now = datetime.now(timezone.utc)
    age = _lock_age_seconds(payload, now=now)
    result = dict(payload)
    result["age_seconds"] = age
    return result


def _force_break_lock(lock_path: Path, *, reason: str) -> str:
    """Forcibly remove a lock file and return an audit message."""
    if not lock_path.exists():
        return "no lock to break"
    payload = _read_lock_payload(lock_path)
    holder_pid = payload.get("pid", "<unknown>")
    holder_host = payload.get("host", "<unknown>")
    started_at = payload.get("started_at", "<unknown>")
    lock_path.unlink(missing_ok=True)
    return (
        f"lock broken: pid={holder_pid}, host={holder_host}, "
        f"started_at={started_at}, reason={reason}"
    )
