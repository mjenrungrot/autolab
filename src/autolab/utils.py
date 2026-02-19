"""Autolab utility functions — extracted from __main__ for reuse."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from autolab.constants import (
    ACTIVE_STAGES,
    ASSISTANT_CONTROL_COMMIT_PATHS,
    BACKLOG_COMPLETED_STATUSES,
    EXPERIMENT_LOCKED_TYPES,
    EXPERIMENT_TYPES,
    HOST_MODE_COMMAND_TIMEOUT_SECONDS,
    RUN_ID_TIMESTAMP_PATTERN,
)
from autolab.models import (
    AutoCommitConfig,
    MeaningfulChangeConfig,
    RunOutcome,
    StateError,
)
from autolab.todo_sync import sync_todo_pre_run, sync_todo_post_run


# ---------------------------------------------------------------------------
# Host mode detection
# ---------------------------------------------------------------------------


def _probe_host_command(
    argv: list[str], *, timeout: float = HOST_MODE_COMMAND_TIMEOUT_SECONDS
) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            argv,
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError):
        return (False, "missing")
    except subprocess.TimeoutExpired:
        return (False, "timeout")
    return (
        proc.returncode == 0,
        "ok" if proc.returncode == 0 else f"exit_{proc.returncode}",
    )


def _is_command_available(command: str) -> bool:
    return shutil.which(command) is not None


def _has_slurm_env() -> bool:
    return bool(
        os.environ.get("SLURM_CLUSTER_NAME")
        or os.environ.get("SLURM_JOB_ID")
        or os.environ.get("SLURM_JOB_NODELIST")
        or os.environ.get("SLURM_NNODES")
    )


def _detect_host_mode_with_probe() -> tuple[str, dict[str, str]]:
    has_sinfo = _is_command_available("sinfo")
    has_squeue = _is_command_available("squeue")
    has_sbatch = _is_command_available("sbatch")
    probe = {
        "has_sinfo": str(has_sinfo).lower(),
        "has_squeue": str(has_squeue).lower(),
        "has_sbatch": str(has_sbatch).lower(),
        "has_slurm_env": str(_has_slurm_env()).lower(),
    }

    sinfo_ok = squeue_ok = sbatch_ok = False
    if has_sinfo:
        sinfo_ok, sinfo_status = _probe_host_command(["sinfo", "-V"])
        probe["sinfo"] = sinfo_status
    if has_squeue:
        squeue_ok, squeue_status = _probe_host_command(["squeue", "-V"])
        probe["squeue"] = squeue_status
    if has_sbatch:
        sbatch_ok, sbatch_status = _probe_host_command(["sbatch", "--version"])
        probe["sbatch"] = sbatch_status

    if _has_slurm_env():
        if (
            (has_sinfo and sinfo_ok)
            or (has_sbatch and sbatch_ok)
            or (has_squeue and squeue_ok)
        ):
            return ("slurm", probe)
        probe["note"] = "slurm environment detected but command probes incomplete"

    if has_sinfo and has_squeue and sinfo_ok and squeue_ok:
        return ("slurm", probe)
    if has_sbatch and (sinfo_ok or squeue_ok):
        return ("slurm", probe)
    return ("local", probe)


def _detect_priority_host_mode() -> str:
    host_mode, _probe = _detect_host_mode_with_probe()
    return host_mode


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _generate_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex[:6]
    return f"{timestamp}_{suffix}"


def _parse_utc(value: str) -> datetime | None:
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_run_id_timestamp(run_id: str) -> datetime | None:
    match = RUN_ID_TIMESTAMP_PATTERN.search(str(run_id))
    if not match:
        return None
    try:
        parsed = datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc)


def _manifest_timestamp(payload: dict[str, Any], run_id: str) -> datetime | None:
    candidates: list[Any] = [
        payload.get("timestamp_utc"),
        payload.get("timestamp"),
        payload.get("completed_at"),
        payload.get("finished_at"),
        payload.get("started_at"),
        payload.get("created_at"),
    ]
    timestamps = payload.get("timestamps")
    if isinstance(timestamps, dict):
        candidates.extend(
            [
                timestamps.get("timestamp_utc"),
                timestamps.get("completed_at"),
                timestamps.get("finished_at"),
                timestamps.get("started_at"),
                timestamps.get("created_at"),
            ]
        )
    for nested_key in ("launch", "execution", "sync"):
        nested = payload.get(nested_key)
        if not isinstance(nested, dict):
            continue
        candidates.extend(
            [
                nested.get("timestamp_utc"),
                nested.get("completed_at"),
                nested.get("finished_at"),
                nested.get("started_at"),
                nested.get("created_at"),
            ]
        )
    for raw_value in candidates:
        parsed = _parse_utc(str(raw_value))
        if parsed is not None:
            return parsed
    return _parse_run_id_timestamp(run_id)


# ---------------------------------------------------------------------------
# JSON I/O helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise StateError(f"state file not found: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StateError(f"state file is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StateError(f"state file must contain an object: {path}")
    return payload


def _load_json_if_exists(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _compact_json(value: Any, *, max_chars: int = 2000) -> str:
    try:
        rendered = json.dumps(value, indent=2, sort_keys=True)
    except Exception:
        rendered = str(value)
    compact = rendered.strip()
    if len(compact) <= max_chars:
        return compact
    return f"{compact[:max_chars]}..."


# ---------------------------------------------------------------------------
# Text / file helpers
# ---------------------------------------------------------------------------


def _safe_read_text(path: Path, *, max_chars: int = 2000) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return ""
    compact = text.strip()
    if not compact:
        return ""
    if len(compact) <= max_chars:
        return compact
    return f"{compact[:max_chars]}..."


def _extract_matching_lines(
    path: Path, *, keywords: tuple[str, ...], limit: int = 8
) -> str:
    if not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    matched: list[str] = []
    lowered_keywords = tuple(keyword.lower() for keyword in keywords)
    for line in lines:
        compact = " ".join(line.strip().split())
        if not compact:
            continue
        lowered = compact.lower()
        if any(keyword in lowered for keyword in lowered_keywords):
            matched.append(compact)
    if not matched:
        return ""
    return "\n".join(matched[:limit])


def _extract_log_snippet(
    repo_root: Path, *, keywords: tuple[str, ...], limit: int = 8
) -> str:
    log_path = repo_root / ".autolab" / "logs" / "orchestrator.log"
    if not log_path.exists():
        return ""
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return ""
    lowered_keywords = tuple(keyword.lower() for keyword in keywords)
    matched: list[str] = []
    for line in reversed(lines):
        lowered = line.lower()
        if any(keyword in lowered for keyword in lowered_keywords):
            matched.append(_compact_log_text(line, limit=320))
        if len(matched) >= limit:
            break
    if not matched:
        return ""
    matched.reverse()
    return "\n".join(matched)


def _compact_log_text(text: str, limit: int = 240) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}..."


def _normalize_space(value: Any) -> str:
    return str(value).strip()


def _ensure_text_file(path: Path, content: str, created: list[Path]) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    created.append(path)


def _ensure_json_file(path: Path, payload: dict[str, Any], created: list[Path]) -> None:
    if path.exists():
        return
    _write_json(path, payload)
    created.append(path)


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _append_log(repo_root: Path, message: str) -> None:
    log_path = repo_root / ".autolab" / "logs" / "orchestrator.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{_utc_now()} {message}\n")


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _run_git(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    command = ["git", "-C", str(repo_root), *args]
    try:
        return subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
        )
    except FileNotFoundError as exc:
        return subprocess.CompletedProcess(command, 127, "", f"git not found: {exc}")
    except OSError as exc:
        return subprocess.CompletedProcess(command, 1, "", str(exc))


def _is_git_worktree(repo_root: Path) -> bool:
    check = _run_git(repo_root, ["rev-parse", "--is-inside-work-tree"])
    return check.returncode == 0 and check.stdout.strip() == "true"


def _collect_staged_paths(repo_root: Path, scoped_paths: tuple[str, ...]) -> list[str]:
    args = ["diff", "--cached", "--name-only"]
    if scoped_paths:
        args.extend(["--", *scoped_paths])
    staged = _run_git(repo_root, args)
    if staged.returncode != 0:
        return [path for path in scoped_paths if path]
    paths: list[str] = []
    seen: set[str] = set()
    for raw_line in staged.stdout.splitlines():
        path = raw_line.strip()
        if not path or path in seen:
            continue
        seen.add(path)
        paths.append(path)
    return paths


def _is_docs_path(path: str) -> bool:
    normalized = path.replace("\\", "/")
    return (
        normalized.startswith("docs/")
        or normalized.startswith("paper/")
        or normalized.endswith(".md")
    )


def _infer_auto_commit_type(paths: list[str]) -> str:
    if not paths:
        return "feat"
    normalized = [path.replace("\\", "/") for path in paths]
    if all(path.startswith("tests/") for path in normalized):
        return "test"
    if all(_is_docs_path(path) for path in normalized):
        return "docs"
    if any(path.startswith("tests/") for path in normalized):
        return "test"
    if any(_is_docs_path(path) for path in normalized):
        return "docs"
    return "feat"


def _summarize_commit_paths(paths: list[str], *, max_items: int = 2) -> str:
    if not paths:
        return "repository updates"
    unique_paths = list(dict.fromkeys(paths))
    shown = unique_paths[:max_items]
    if len(unique_paths) <= max_items:
        return ", ".join(shown)
    return f"{', '.join(shown)} +{len(unique_paths) - max_items} files"


def _build_auto_commit_message(outcome: RunOutcome, staged_paths: list[str]) -> str:
    commit_type = _infer_auto_commit_type(staged_paths)
    summary = _summarize_commit_paths(staged_paths)
    commit_message = f"{commit_type}(autolab): update {summary}"
    if outcome.commit_task_id:
        commit_message = f"{commit_message} [task:{outcome.commit_task_id}]"
    if outcome.commit_cycle_stage:
        commit_message = f"{commit_message} [cycle:{outcome.commit_cycle_stage}]"
    if outcome.transitioned:
        commit_message = (
            f"{commit_message} [stage:{outcome.stage_before}->{outcome.stage_after}]"
        )
    return commit_message


def _try_auto_commit(repo_root: Path, *, outcome: RunOutcome) -> str:
    if not outcome.commit_allowed:
        return "auto_commit: skipped (non-meaningful cycle)"
    inside = _run_git(repo_root, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return "auto_commit: skipped (not a git work tree)"

    conflicts = _run_git(repo_root, ["diff", "--name-only", "--diff-filter=U"])
    if conflicts.returncode != 0:
        detail = _compact_log_text(
            (conflicts.stderr or conflicts.stdout or "unknown git error").strip()
        )
        _append_log(repo_root, f"auto_commit probe failed: {detail}")
        return f"auto_commit: skipped (probe failed: {detail})"
    if conflicts.stdout.strip():
        _append_log(repo_root, "auto_commit skipped due to unresolved merge conflicts")
        return "auto_commit: skipped (unresolved merge conflicts)"

    scoped_paths = tuple(
        path for path in dict.fromkeys(outcome.commit_paths) if str(path).strip()
    )
    if outcome.commit_paths and not scoped_paths:
        return "auto_commit: skipped (no scoped paths)"

    add_args = ["add", "--", *scoped_paths] if scoped_paths else ["add", "-A"]
    add = _run_git(repo_root, add_args)
    if add.returncode != 0:
        detail = _compact_log_text(
            (add.stderr or add.stdout or "git add failed").strip()
        )
        _append_log(repo_root, f"auto_commit add failed: {detail}")
        return f"auto_commit: failed (git add failed: {detail})"

    staged_args = ["diff", "--cached", "--quiet"]
    if scoped_paths:
        staged_args.extend(["--", *scoped_paths])
    staged = _run_git(repo_root, staged_args)
    if staged.returncode == 0:
        return "auto_commit: skipped (no changes)"
    if staged.returncode not in {0, 1}:
        detail = _compact_log_text(
            (staged.stderr or staged.stdout or "git diff --cached failed").strip()
        )
        _append_log(repo_root, f"auto_commit staged-check failed: {detail}")
        return f"auto_commit: failed (staged check failed: {detail})"

    staged_paths = _collect_staged_paths(repo_root, scoped_paths)
    commit_message = _build_auto_commit_message(outcome, staged_paths)
    commit_args = ["commit", "-m", commit_message]
    if scoped_paths:
        commit_args.extend(["--", *scoped_paths])
    commit = _run_git(repo_root, commit_args)
    if commit.returncode != 0:
        detail = _compact_log_text(
            (commit.stderr or commit.stdout or "git commit failed").strip()
        )
        _append_log(repo_root, f"auto_commit commit failed: {detail}")
        return f"auto_commit: failed ({detail})"

    head = _run_git(repo_root, ["rev-parse", "--short", "HEAD"])
    commit_id = head.stdout.strip() if head.returncode == 0 else "<unknown>"
    _append_log(repo_root, f"auto_commit created commit {commit_id}: {commit_message}")
    return f"auto_commit: committed {commit_id}"


def _collect_git_status_entries(repo_root: Path) -> list[tuple[str, str]]:
    status = _run_git(repo_root, ["status", "--porcelain", "--untracked-files=all"])
    if status.returncode != 0:
        return []
    entries: list[tuple[str, str]] = []
    for raw_line in status.stdout.splitlines():
        line = raw_line.rstrip("\n")
        if len(line) < 4:
            continue
        status_code = line[:2]
        payload = line[3:].strip()
        if " -> " in payload:
            payload = payload.split(" -> ", 1)[1].strip()
        if payload:
            entries.append((payload, status_code))
    return entries


def _collect_changed_paths(repo_root: Path) -> list[str]:
    entries = _collect_git_status_entries(repo_root)
    changed: list[str] = []
    seen: set[str] = set()
    for path, _status_code in entries:
        if path in seen:
            continue
        seen.add(path)
        changed.append(path)
    return changed


def _summarize_git_changes_for_prompt(
    repo_root: Path, *, limit: int = 12
) -> tuple[str, list[str]]:
    entries = _collect_git_status_entries(repo_root)
    if not entries:
        return ("clean working tree", [])
    summarized = [
        f"{status_code.strip() or '??'} {path}" for path, status_code in entries[:limit]
    ]
    summary = f"{len(entries)} changed path(s)"
    if len(entries) > limit:
        summary = f"{summary}; showing first {limit}"
    return (summary, summarized)


# ---------------------------------------------------------------------------
# Change tracking / fingerprinting
# ---------------------------------------------------------------------------


def _path_fingerprint(repo_root: Path, relative_path: str) -> str:
    path = repo_root / relative_path
    if path.exists() and path.is_file():
        try:
            digest = hashlib.sha1(path.read_bytes()).hexdigest()
        except OSError:
            return "<unreadable>"
        return digest
    if path.exists() and path.is_dir():
        return "<dir>"
    return "<missing>"


def _collect_change_snapshot(repo_root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path, status_code in _collect_git_status_entries(repo_root):
        snapshot[path] = f"{status_code}:{_path_fingerprint(repo_root, path)}"
    return snapshot


def _snapshot_delta_paths(
    baseline_snapshot: dict[str, str], current_snapshot: dict[str, str]
) -> list[str]:
    delta_paths = [
        path
        for path, signature in current_snapshot.items()
        if baseline_snapshot.get(path) != signature
    ]
    return sorted(delta_paths)


def _assistant_commit_paths(
    delta_paths: list[str], meaningful_paths: list[str]
) -> tuple[str, ...]:
    meaningful_set = set(meaningful_paths)
    scoped: list[str] = []
    seen: set[str] = set()
    for path in delta_paths:
        include_path = path in meaningful_set or path in ASSISTANT_CONTROL_COMMIT_PATHS
        if not include_path or path in seen:
            continue
        seen.add(path)
        scoped.append(path)
    return tuple(scoped)


def _path_matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    target = path.strip()
    if not target:
        return False
    normalized_target = target.replace("\\", "/")
    for pattern in patterns:
        if fnmatch.fnmatch(normalized_target, pattern):
            return True
    return False


def _resolve_meaningful_exclude_paths(
    config: MeaningfulChangeConfig,
    *,
    stage: str | None,
) -> tuple[str, ...]:
    patterns = list(config.exclude_paths)
    normalized_stage = str(stage or "").strip().lower()
    if (
        config.require_non_review_progress_in_implementation_cycle
        and normalized_stage in {"implementation", "implementation_review"}
    ):
        for pattern in config.implementation_cycle_exclude_paths:
            if pattern not in patterns:
                patterns.append(pattern)
    return tuple(patterns)


def _evaluate_meaningful_change(
    repo_root: Path,
    config: MeaningfulChangeConfig,
    *,
    baseline_snapshot: dict[str, str] | None = None,
    stage: str | None = None,
) -> tuple[bool, list[str], list[str], dict[str, str]]:
    current_snapshot = _collect_change_snapshot(repo_root)
    changed_paths = sorted(current_snapshot.keys())
    if baseline_snapshot is None:
        delta_paths = changed_paths
    else:
        delta_paths = _snapshot_delta_paths(baseline_snapshot, current_snapshot)
    exclude_patterns = _resolve_meaningful_exclude_paths(config, stage=stage)
    meaningful_paths = [
        path for path in delta_paths if not _path_matches_any(path, exclude_patterns)
    ]
    return (bool(meaningful_paths), delta_paths, meaningful_paths, current_snapshot)


def _meaningful_progress_detail(
    *,
    changed_paths: list[str],
    meaningful_paths: list[str],
    limit: int = 5,
) -> str:
    if not changed_paths:
        return "no files changed"
    if meaningful_paths:
        sample = ", ".join(meaningful_paths[:limit])
    else:
        sample = ", ".join(changed_paths[:limit])
    return (
        f"changed_paths={len(changed_paths)}, meaningful_paths={len(meaningful_paths)} "
        f"sample={sample}"
    )


def _prepare_standard_commit_outcome(
    repo_root: Path,
    outcome: RunOutcome,
    baseline_snapshot: dict[str, str],
    *,
    assistant: bool,
    strict_implementation_progress: bool = True,
) -> RunOutcome:
    if assistant:
        return outcome

    # Late import to avoid circular dependency — these config loaders remain
    # in __main__ alongside the verifier-policy infrastructure they depend on.
    from autolab.config import _load_auto_commit_config, _load_meaningful_change_config

    auto_commit_config = _load_auto_commit_config(repo_root)
    if auto_commit_config.mode == "always":
        return outcome

    if auto_commit_config.mode == "disabled":
        return RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=outcome.message,
            commit_allowed=False,
            commit_task_id=outcome.commit_task_id,
            commit_cycle_stage=outcome.commit_cycle_stage,
            commit_paths=(),
        )

    meaningful_config = _load_meaningful_change_config(repo_root)
    require_progress_gate = (
        strict_implementation_progress
        and meaningful_config.require_implementation_progress
    )
    non_git_check = bool(
        meaningful_config.require_git_for_progress and not _is_git_worktree(repo_root)
    )
    meaningful, delta_paths, meaningful_paths, _current_snapshot = (
        _evaluate_meaningful_change(
            repo_root,
            meaningful_config,
            baseline_snapshot=baseline_snapshot,
            stage=outcome.stage_before,
        )
    )

    if require_progress_gate and non_git_check:
        if meaningful_config.on_non_git_behavior == "warn_and_continue":
            return RunOutcome(
                exit_code=outcome.exit_code,
                transitioned=outcome.transitioned,
                stage_before=outcome.stage_before,
                stage_after=outcome.stage_after,
                message=outcome.message,
                commit_allowed=True,
                commit_task_id=outcome.commit_task_id,
                commit_cycle_stage=outcome.commit_cycle_stage,
                commit_paths=tuple(delta_paths),
            )
        return RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=outcome.message,
            commit_allowed=False,
            commit_task_id=outcome.commit_task_id,
            commit_cycle_stage=outcome.commit_cycle_stage,
            commit_paths=(),
        )

    if not require_progress_gate:
        return RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=outcome.message,
            commit_allowed=True,
            commit_task_id=outcome.commit_task_id,
            commit_cycle_stage=outcome.commit_cycle_stage,
            commit_paths=tuple(delta_paths),
        )

    if not meaningful:
        return RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=outcome.message,
            commit_allowed=False,
            commit_task_id=outcome.commit_task_id,
            commit_cycle_stage=outcome.commit_cycle_stage,
            commit_paths=(),
        )
    return RunOutcome(
        exit_code=outcome.exit_code,
        transitioned=outcome.transitioned,
        stage_before=outcome.stage_before,
        stage_after=outcome.stage_after,
        message=outcome.message,
        commit_allowed=True,
        commit_task_id=outcome.commit_task_id,
        commit_cycle_stage=outcome.commit_cycle_stage,
        commit_paths=tuple(meaningful_paths),
    )


# ---------------------------------------------------------------------------
# Todo helpers
# ---------------------------------------------------------------------------


def _todo_open_count(repo_root: Path) -> int:
    todo_state_path = repo_root / ".autolab" / "todo_state.json"
    if not todo_state_path.exists():
        return 0
    try:
        payload = json.loads(todo_state_path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    tasks = payload.get("tasks", {})
    if not isinstance(tasks, dict):
        return 0
    return sum(
        1
        for task in tasks.values()
        if isinstance(task, dict) and task.get("status") == "open"
    )


def _has_open_stage_todo_task(repo_root: Path, stage: str) -> bool:
    normalized_stage = str(stage).strip()
    if normalized_stage not in ACTIVE_STAGES:
        return False
    todo_state_path = repo_root / ".autolab" / "todo_state.json"
    if not todo_state_path.exists():
        return False
    try:
        payload = json.loads(todo_state_path.read_text(encoding="utf-8"))
    except Exception:
        return False
    tasks = payload.get("tasks", {})
    if not isinstance(tasks, dict):
        return False
    for task in tasks.values():
        if not isinstance(task, dict):
            continue
        if task.get("status") != "open":
            continue
        if str(task.get("stage", "")).strip() == normalized_stage:
            return True
    return False


def _safe_todo_pre_sync(
    repo_root: Path,
    state: dict[str, Any] | None,
    *,
    host_mode: str | None = None,
) -> tuple[list[Path], str]:
    try:
        result = sync_todo_pre_run(repo_root, state, host_mode=host_mode)
    except Exception as exc:
        _append_log(repo_root, f"todo_sync pre-run error: {exc}")
        return ([], "todo_sync pre-run failed")
    return (result.changed_files, result.message)


def _safe_todo_post_sync(
    repo_root: Path,
    state: dict[str, Any] | None,
    *,
    run_outcome: dict[str, Any] | None,
) -> tuple[list[Path], str]:
    try:
        result = sync_todo_post_run(repo_root, state, run_outcome=run_outcome)
    except Exception as exc:
        _append_log(repo_root, f"todo_sync post-run error: {exc}")
        return ([], "todo_sync post-run failed")
    return (result.changed_files, result.message)


def _outcome_payload(outcome: RunOutcome) -> dict[str, Any]:
    return {
        "exit_code": outcome.exit_code,
        "transitioned": outcome.transitioned,
        "stage_before": outcome.stage_before,
        "stage_after": outcome.stage_after,
    }


def _append_todo_message(base_message: str, todo_message: str) -> str:
    suffix = todo_message.strip()
    if not suffix:
        return base_message
    return f"{base_message}; {suffix}"


# ---------------------------------------------------------------------------
# Backlog / experiment type helpers
# ---------------------------------------------------------------------------


def _normalize_backlog_status(value: Any) -> str:
    return _normalize_space(str(value)).lower()


def _is_backlog_status_completed(value: Any) -> bool:
    return _normalize_backlog_status(value) in BACKLOG_COMPLETED_STATUSES


def _normalize_experiment_type(value: Any) -> str:
    normalized = _normalize_space(str(value)).lower()
    if normalized in EXPERIMENT_TYPES:
        return normalized
    return ""


def _is_experiment_type_locked(value: Any) -> bool:
    return _normalize_experiment_type(value) in EXPERIMENT_LOCKED_TYPES


# ---------------------------------------------------------------------------
# Agent result persistence
# ---------------------------------------------------------------------------


def _write_block_reason(
    repo_root: Path,
    *,
    reason: str,
    stage_at_block: str,
    action_required: str,
    iteration_id: str = "",
    guardrail_rule: str = "",
) -> Path:
    """Write .autolab/block_reason.json when an experiment is blocked."""
    block_path = repo_root / ".autolab" / "block_reason.json"
    _write_json(
        block_path,
        {
            "blocked_at": _utc_now(),
            "reason": reason,
            "stage_at_block": stage_at_block,
            "action_required": action_required,
        },
    )
    # Also write a human-readable blocked.md alongside the JSON
    if iteration_id:
        md_dir = repo_root / "experiments"
        # Find the iteration directory by searching experiment type dirs
        for exp_type in ("plan", "in_progress", "done"):
            candidate = md_dir / exp_type / iteration_id
            if candidate.is_dir():
                md_dir = candidate
                break
        else:
            md_dir = md_dir / "in_progress" / iteration_id
        md_dir.mkdir(parents=True, exist_ok=True)
    else:
        md_dir = repo_root / ".autolab"
    blocked_md_path = md_dir / "blocked.md"
    lines = [
        "# Experiment Blocked\n",
        f"\n**Reason:** {reason}\n",
        f"\n**Stage at block:** {stage_at_block}\n",
        f"\n**Action required:** {action_required}\n",
    ]
    if guardrail_rule:
        lines.append(f"\n**Guardrail rule:** {guardrail_rule}\n")
    lines.append(
        "\n## How to Reopen\n\n"
        "1. Address the blocking reason above\n"
        "2. Update the backlog entry status (e.g., change `type` from `done` back to `in_progress`)\n"
        "3. Run `autolab run` to resume the workflow\n"
    )
    blocked_md_path.write_text("".join(lines), encoding="utf-8")
    return block_path


def _write_guardrail_breach(
    repo_root: Path,
    *,
    rule: str,
    counters: dict[str, Any],
    stage: str,
    remediation: str,
) -> Path:
    """Write .autolab/guardrail_breach.json when a guardrail threshold is exceeded."""
    breach_path = repo_root / ".autolab" / "guardrail_breach.json"
    _write_json(
        breach_path,
        {
            "breached_at": _utc_now(),
            "rule": rule,
            "counters": counters,
            "stage": stage,
            "remediation": remediation,
        },
    )
    return breach_path


def _persist_agent_result(
    repo_root: Path,
    *,
    status: str,
    summary: str,
    changed_files: list[Path],
) -> Path:
    agent_path = repo_root / ".autolab" / "agent_result.json"
    resolved: list[str] = []
    seen: set[str] = set()
    for candidate in [*changed_files, agent_path]:
        value = str(candidate.resolve())
        if value not in seen:
            seen.add(value)
            resolved.append(value)
    _write_json(
        agent_path,
        {
            "status": status,
            "summary": summary,
            "changed_files": resolved,
            "completion_token_seen": True,
        },
    )
    return agent_path
