"""Shared CLI imports, constants, formatter, and helper utilities."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import importlib.metadata as importlib_metadata
import importlib.resources as importlib_resources
import json
import os
import platform
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml_mod
except Exception:
    _yaml_mod = None

from autolab.constants import (
    ACTIVE_STAGES,
    ALL_STAGES,
    DECISION_STAGES,
    DEFAULT_BACKLOG_TEMPLATE,
    DEFAULT_EXPERIMENT_TYPE,
    EXPERIMENT_TYPES,
    DEFAULT_MAX_HOURS,
    DEFAULT_VERIFIER_POLICY,
    ITERATION_ID_SAFE_PATTERN,
    LOCK_STALE_SECONDS,
    STAGE_BRIEF_PROMPT_FILES,
    STAGE_HUMAN_PROMPT_FILES,
    STAGE_PROMPT_FILES,
    STAGE_RUNNER_PROMPT_FILES,
    TERMINAL_STAGES,
)
from autolab.registry import load_registry, StageSpec
from autolab.models import RunOutcome, StageCheckError, StateError
from autolab.orchestration.engine import OrchestrationEngine
from autolab.orchestration.models import RunRequest
from autolab.config import (
    _load_guardrail_config,
    _load_meaningful_change_config,
    _load_verifier_policy,
    _resolve_policy_python_bin,
    _resolve_run_agent_mode,
)
from autolab.run_standard import _run_once_standard
from autolab.run_assistant import _run_once_assistant
from autolab.handoff import refresh_handoff
from autolab.state import (
    _acquire_lock,
    _append_state_history,
    _bootstrap_iteration_id,
    _default_agent_result,
    _default_state,
    _find_backlog_experiment_entry,
    _force_break_lock,
    _heartbeat_lock,
    _inspect_lock,
    _load_backlog_yaml,
    _load_state,
    _mark_backlog_experiment_completed,
    _normalize_state,
    _parse_iteration_from_backlog,
    _read_lock_payload,
    _release_lock,
    _resolve_autolab_dir,
    _resolve_iteration_directory,
    _resolve_repo_root,
    _resolve_scaffold_source,
    _sync_scaffold_bundle,
    _resolve_experiment_type_from_backlog,
    _write_backlog_yaml,
    _ensure_iteration_skeleton,
)
from autolab.todo_sync import list_open_tasks, mark_task_completed, mark_task_removed
from autolab.update import run_update
from autolab.brownfield_bootstrap import run_brownfield_bootstrap
from autolab.utils import (
    _append_log,
    _collect_change_snapshot,
    _ensure_json_file,
    _ensure_text_file,
    _is_backlog_status_completed,
    _load_json_if_exists,
    _normalize_experiment_type,
    _normalize_space,
    _outcome_payload,
    _persist_agent_result,
    _prepare_standard_commit_outcome,
    _safe_todo_pre_sync,
    _todo_open_count,
    _try_auto_commit,
    _utc_now,
    _write_json,
)
from autolab.prompts import (
    _default_stage_prompt_text,
    _render_stage_prompt,
    _resolve_stage_prompt_path,
)
from autolab.validators import _run_verification_step_detailed
from autolab.slurm_job_list import (
    append_entry_idempotent,
    canonical_slurm_job_bullet,
    is_slurm_manifest,
    ledger_contains_entry,
    ledger_contains_run_id,
    required_run_id,
    required_slurm_job_id,
)

POLICY_PRESET_NAMES = ("local_dev", "ci_strict", "slurm")
SUPPORTED_SKILL_PROVIDERS = ("codex", "claude")
SKILL_INSTALL_ROOT_BY_PROVIDER = {
    "codex": ".codex",
    "claude": ".claude",
}
VERIFICATION_SUMMARY_RETENTION_LIMIT = 200
RUN_LOCK_HEARTBEAT_INTERVAL_SECONDS = 60.0

TOP_LEVEL_COMMAND_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Getting started",
        ("init", "configure", "status", "progress", "docs", "explain"),
    ),
    (
        "Run workflow",
        (
            "run",
            "loop",
            "trace",
            "tui",
            "render",
            "verify",
            "verify-golden",
            "lint",
            "review",
            "skip",
            "handoff",
            "resume",
        ),
    ),
    ("Backlog steering", ("focus", "todo", "experiment")),
    ("Safety and policy", ("policy", "guardrails", "lock", "unlock")),
    (
        "Maintenance",
        (
            "sync-scaffold",
            "update",
            "install-skill",
            "slurm-job-list",
            "report",
            "reset",
        ),
    ),
)


class _AutolabHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Render top-level commands grouped by onboarding categories."""

    def _format_action(self, action: argparse.Action) -> str:
        subparsers_action = getattr(argparse, "_SubParsersAction", None)
        if (
            subparsers_action is not None
            and isinstance(action, subparsers_action)
            and action.dest == "command"
        ):
            return self._format_top_level_subparsers(action)
        return super()._format_action(action)

    def _format_top_level_subparsers(self, action: argparse.Action) -> str:
        subactions = list(action._get_subactions())  # type: ignore[attr-defined]
        help_by_command: dict[str, str] = {}
        for subaction in subactions:
            command = str(getattr(subaction, "dest", "")).strip()
            if not command:
                continue
            help_by_command[command] = str(getattr(subaction, "help", "") or "").strip()

        choices = getattr(action, "choices", {})
        commands_in_order = [str(name).strip() for name in choices]

        indent = " " * self._current_indent
        invocation = self._format_action_invocation(action)
        lines = [f"{indent}{invocation}"]

        rendered_commands: set[str] = set()
        command_width = max((len(name) for name in help_by_command), default=0)
        command_width = max(10, min(command_width, 24))

        for group_name, group_commands in TOP_LEVEL_COMMAND_GROUPS:
            available = [cmd for cmd in group_commands if cmd in help_by_command]
            if not available:
                continue
            lines.append(f"{indent}  {group_name}:")
            for command in available:
                rendered_commands.add(command)
                summary = help_by_command.get(command, "")
                lines.append(
                    f"{indent}    {command.ljust(command_width)}  {summary}".rstrip()
                )

        leftovers = [
            command
            for command in commands_in_order
            if command in help_by_command and command not in rendered_commands
        ]
        if leftovers:
            lines.append(f"{indent}  Other commands:")
            for command in leftovers:
                summary = help_by_command.get(command, "")
                lines.append(
                    f"{indent}    {command.ljust(command_width)}  {summary}".rstrip()
                )

        return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Skill installer helpers
# ---------------------------------------------------------------------------


def _normalize_skill_provider(provider: str) -> str:
    normalized_provider = str(provider).strip().lower()
    if normalized_provider not in SUPPORTED_SKILL_PROVIDERS:
        raise RuntimeError(
            f"unsupported skill provider '{provider}' (expected one of: {', '.join(SUPPORTED_SKILL_PROVIDERS)})"
        )
    return normalized_provider


def _skill_install_root(project_root: Path, provider: str) -> Path:
    normalized_provider = _normalize_skill_provider(provider)
    provider_root = SKILL_INSTALL_ROOT_BY_PROVIDER[normalized_provider]
    return project_root / provider_root / "skills"


def _list_bundled_skills(provider: str) -> list[str]:
    normalized_provider = _normalize_skill_provider(provider)
    skills_root = importlib_resources.files("autolab").joinpath(
        "skills", normalized_provider
    )
    if not skills_root.is_dir():
        raise RuntimeError(
            f"bundled skills directory is unavailable at package://autolab/skills/{normalized_provider}"
        )
    found: list[str] = []
    for child in skills_root.iterdir():
        if child.joinpath("SKILL.md").is_file():
            found.append(child.name)
    return sorted(found)


def _load_packaged_skill_template_text(provider: str, skill_name: str) -> str:
    normalized_provider = _normalize_skill_provider(provider)

    resource = importlib_resources.files("autolab").joinpath(
        "skills",
        normalized_provider,
        skill_name,
        "SKILL.md",
    )
    if not resource.is_file():
        raise RuntimeError(
            f"bundled skill template is unavailable at package://autolab/skills/{normalized_provider}/{skill_name}/SKILL.md"
        )
    return resource.read_text(encoding="utf-8")


def _deep_merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(dict(merged[key]), value)  # type: ignore[arg-type]
        else:
            merged[key] = value
    return merged


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if _yaml_mod is None:
        raise RuntimeError("PyYAML is required for policy preset operations")
    if not path.exists():
        return {}
    payload = _yaml_mod.safe_load(path.read_text(encoding="utf-8"))
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping")
    return payload


# ---------------------------------------------------------------------------
# Internal helper: dispatch a single run to standard or assistant runner
# ---------------------------------------------------------------------------


def _run_once(
    state_path: Path,
    decision: str | None,
    *,
    run_agent_mode: str = "policy",
    verify_before_evaluate: bool = False,
    assistant: bool = False,
    auto_mode: bool = False,
    auto_decision: bool = False,
    strict_implementation_progress: bool = True,
) -> RunOutcome:
    engine = OrchestrationEngine()
    return engine.run_once(
        RunRequest(
            state_path=state_path,
            decision=decision,
            run_agent_mode=run_agent_mode,
            verify_before_evaluate=verify_before_evaluate,
            assistant=assistant,
            auto_mode=auto_mode,
            auto_decision=auto_decision,
            strict_implementation_progress=strict_implementation_progress,
        )
    )


@contextmanager
def _periodic_run_lock_heartbeat(lock_path: Path):
    """Keep run lock heartbeat fresh while a single run execution is active."""
    stop_event = threading.Event()

    def _heartbeat_loop() -> None:
        while not stop_event.wait(RUN_LOCK_HEARTBEAT_INTERVAL_SECONDS):
            _heartbeat_lock(lock_path)

    _heartbeat_lock(lock_path)
    heartbeat_thread = threading.Thread(
        target=_heartbeat_loop,
        name="autolab-run-lock-heartbeat",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        yield
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=RUN_LOCK_HEARTBEAT_INTERVAL_SECONDS + 1.0)
        _heartbeat_lock(lock_path)


# ---------------------------------------------------------------------------
# Overnight summary helper (used by _cmd_loop)
# ---------------------------------------------------------------------------


def _write_overnight_summary(
    repo_root: Path,
    *,
    state_path: Path,
    started_at: str,
    ended_at: str,
    elapsed_seconds: float,
    max_iterations: int,
    max_hours: float,
    auto_decision_count: int,
    retry_escalation_count: int,
    recoverable_error_count: int,
    consecutive_errors_at_exit: int,
    todo_open_before: int,
    todo_open_after: int,
    terminal_reason: str,
    final_stage: str,
    exit_code: int,
    rows: list[dict[str, Any]],
) -> Path:
    summary_path = repo_root / ".autolab" / "logs" / "overnight_summary.md"
    lines = [
        "# Overnight Autolab Summary",
        "",
        f"- started_at: `{started_at}`",
        f"- ended_at: `{ended_at}`",
        f"- elapsed_seconds: `{elapsed_seconds:.2f}`",
        f"- state_file: `{state_path}`",
        f"- max_iterations: `{max_iterations}`",
        f"- max_hours: `{max_hours}`",
        f"- auto_decisions: `{auto_decision_count}`",
        f"- retry_escalations: `{retry_escalation_count}`",
        f"- recoverable_errors: `{recoverable_error_count}`",
        f"- consecutive_errors_at_exit: `{consecutive_errors_at_exit}`",
        f"- todo_open_before: `{todo_open_before}`",
        f"- todo_open_after: `{todo_open_after}`",
        f"- terminal_reason: `{terminal_reason}`",
        f"- final_stage: `{final_stage}`",
        f"- exit_code: `{exit_code}`",
        "",
        "## Iterations",
    ]
    if rows:
        lines.extend(
            [
                "| i | before | after | transitioned | exit | decision | message |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for row in rows:
            _msg = str(row.get("message", "")).replace("|", "/")
            if row.get("recoverable"):
                _msg = f"[recoverable] {_msg}"
            lines.append(
                "| {i} | {before} | {after} | {transitioned} | {exit} | {decision} | {message} |".format(
                    i=row.get("index", ""),
                    before=str(row.get("stage_before", "")).replace("|", "/"),
                    after=str(row.get("stage_after", "")).replace("|", "/"),
                    transitioned=row.get("transitioned", ""),
                    exit=row.get("exit_code", ""),
                    decision=str(row.get("decision", "-")).replace("|", "/"),
                    message=_msg,
                )
            )
    else:
        lines.append("No iterations were executed.")

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return summary_path


def _prune_verification_summary_logs(
    repo_root: Path, *, keep_latest: int = VERIFICATION_SUMMARY_RETENTION_LIMIT
) -> tuple[int, int, int]:
    """Prune old verification summary artifacts under .autolab/logs."""
    logs_dir = repo_root / ".autolab" / "logs"
    if keep_latest < 0:
        keep_latest = 0

    summary_paths = sorted(logs_dir.glob("verification_*.json"), key=lambda p: p.name)
    before = len(summary_paths)
    if before <= keep_latest:
        return (before, 0, before)

    deleted = 0
    prune_count = before - keep_latest
    for summary_path in summary_paths[:prune_count]:
        try:
            summary_path.unlink()
            deleted += 1
        except FileNotFoundError:
            continue
        except OSError as exc:
            try:
                relative_path = summary_path.relative_to(repo_root).as_posix()
            except ValueError:
                relative_path = summary_path.as_posix()
            _append_log(
                repo_root,
                f"verify log-retention warning path={relative_path} detail={exc}",
            )

    after = len(sorted(logs_dir.glob("verification_*.json"), key=lambda p: p.name))
    return (before, deleted, after)


# ---------------------------------------------------------------------------
# Manual control helpers (focus / todo / experiment move)
# ---------------------------------------------------------------------------


def _default_repeat_guard_payload() -> dict[str, Any]:
    return {
        "last_decision": "",
        "same_decision_streak": 0,
        "last_open_task_count": -1,
        "no_progress_decisions": 0,
        "update_docs_cycle_count": 0,
        "last_verification_passed": False,
    }


def _reset_state_for_manual_handoff(state: dict[str, Any], *, stage: str) -> None:
    state["stage"] = stage
    state["stage_attempt"] = 0
    state["last_run_id"] = ""
    state["pending_run_id"] = ""
    state["run_group"] = []
    state["sync_status"] = "na"
    state["assistant_mode"] = "off"
    state["current_task_id"] = ""
    state["task_cycle_stage"] = "select"
    state["task_change_baseline"] = {}
    state["repeat_guard"] = _default_repeat_guard_payload()


def _ensure_no_active_lock(lock_path: Path) -> str:
    info = _inspect_lock(lock_path)
    if info is None:
        return ""
    age_raw = info.get("age_seconds")
    if isinstance(age_raw, (int, float)) and age_raw > LOCK_STALE_SECONDS:
        return ""
    age_text = f"{age_raw:.0f}s" if isinstance(age_raw, (int, float)) else "unknown"
    return (
        f"active lock exists at {lock_path} "
        f"(pid={info.get('pid', '<unknown>')}, host={info.get('host', '<unknown>')}, age={age_text})"
    )


def _validate_target_identifiers(iteration_id: str, experiment_id: str) -> str:
    normalized_iteration_id = _normalize_space(iteration_id)
    normalized_experiment_id = _normalize_space(experiment_id)
    if not normalized_iteration_id or normalized_iteration_id.startswith("<"):
        return "iteration_id must be a real identifier"
    if not ITERATION_ID_SAFE_PATTERN.fullmatch(normalized_iteration_id):
        return "iteration_id must match [A-Za-z0-9._-] so it can map to experiments/<type>/<iteration_id>"
    if not normalized_experiment_id or normalized_experiment_id.startswith("<"):
        return "experiment_id must be a real identifier"
    return ""


def _resolve_backlog_target_entry(
    repo_root: Path,
    *,
    iteration_id: str,
    experiment_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    payload, load_error = _load_backlog_yaml(backlog_path)
    if payload is None:
        return (None, None, load_error)

    experiments = payload.get("experiments")
    if not isinstance(experiments, list):
        return (None, None, "backlog experiments list is missing")

    normalized_iteration_id = _normalize_space(iteration_id)
    normalized_experiment_id = _normalize_space(experiment_id)
    if not normalized_iteration_id and not normalized_experiment_id:
        return (
            None,
            None,
            "target experiment is ambiguous; set --iteration-id and/or --experiment-id",
        )

    if normalized_experiment_id:
        matches = [
            entry
            for entry in experiments
            if isinstance(entry, dict)
            and _normalize_space(str(entry.get("id", ""))) == normalized_experiment_id
        ]
        if not matches:
            return (
                None,
                None,
                f"backlog experiment '{normalized_experiment_id}' was not found",
            )
        if len(matches) > 1:
            return (
                None,
                None,
                f"backlog experiment id '{normalized_experiment_id}' is duplicated",
            )
        entry = matches[0]
        if normalized_iteration_id:
            entry_iteration_id = _normalize_space(str(entry.get("iteration_id", "")))
            if entry_iteration_id != normalized_iteration_id:
                return (
                    None,
                    None,
                    (
                        f"experiment '{normalized_experiment_id}' is mapped to iteration_id "
                        f"'{entry_iteration_id}', not '{normalized_iteration_id}'"
                    ),
                )
        return (payload, entry, "")

    matches = [
        entry
        for entry in experiments
        if isinstance(entry, dict)
        and _normalize_space(str(entry.get("iteration_id", "")))
        == normalized_iteration_id
    ]
    if not matches:
        return (
            None,
            None,
            f"no backlog experiment matches iteration_id '{normalized_iteration_id}'",
        )
    if len(matches) > 1:
        duplicate_ids = [
            _normalize_space(str(entry.get("id", ""))) or "<unidentified>"
            for entry in matches
        ]
        return (
            None,
            None,
            (
                f"multiple backlog experiments match iteration_id '{normalized_iteration_id}': "
                f"{', '.join(duplicate_ids)}"
            ),
        )
    return (payload, matches[0], "")


def _resolve_create_hypothesis_id(
    backlog_payload: dict[str, Any], *, hypothesis_id: str
) -> tuple[str, str]:
    hypotheses = backlog_payload.get("hypotheses")
    if not isinstance(hypotheses, list):
        return ("", "backlog hypotheses list is missing")

    requested_hypothesis_id = _normalize_space(hypothesis_id)
    if requested_hypothesis_id:
        matches = [
            entry
            for entry in hypotheses
            if isinstance(entry, dict)
            and _normalize_space(str(entry.get("id", ""))) == requested_hypothesis_id
        ]
        if not matches:
            return (
                "",
                f"backlog hypothesis '{requested_hypothesis_id}' was not found",
            )
        if len(matches) > 1:
            return (
                "",
                f"backlog hypothesis id '{requested_hypothesis_id}' is duplicated",
            )
        matched_entry = matches[0]
        status = _normalize_space(str(matched_entry.get("status", ""))).lower()
        if _is_backlog_status_completed(status):
            return (
                "",
                (
                    f"backlog hypothesis '{requested_hypothesis_id}' is completed "
                    f"(status='{status or 'completed'}')"
                ),
            )
        return (requested_hypothesis_id, "")

    for entry in hypotheses:
        if not isinstance(entry, dict):
            continue
        resolved_hypothesis_id = _normalize_space(str(entry.get("id", "")))
        if not resolved_hypothesis_id:
            continue
        status = _normalize_space(str(entry.get("status", ""))).lower()
        if _is_backlog_status_completed(status):
            continue
        return (resolved_hypothesis_id, "")

    return (
        "",
        "no open hypothesis available in backlog; set --hypothesis-id to a non-completed hypothesis",
    )


def _validate_experiment_create_uniqueness(
    repo_root: Path,
    backlog_payload: dict[str, Any],
    *,
    experiment_id: str,
    iteration_id: str,
) -> str:
    experiments = backlog_payload.get("experiments")
    if not isinstance(experiments, list):
        return "backlog experiments list is missing"

    for entry in experiments:
        if not isinstance(entry, dict):
            continue
        entry_experiment_id = _normalize_space(str(entry.get("id", "")))
        if entry_experiment_id == experiment_id:
            return f"backlog experiment '{experiment_id}' already exists"

    for entry in experiments:
        if not isinstance(entry, dict):
            continue
        entry_iteration_id = _normalize_space(str(entry.get("iteration_id", "")))
        if entry_iteration_id != iteration_id:
            continue
        existing_experiment = _normalize_space(str(entry.get("id", ""))) or "<unknown>"
        return (
            f"backlog iteration_id '{iteration_id}' already exists "
            f"(experiment_id='{existing_experiment}')"
        )

    for experiment_type in EXPERIMENT_TYPES:
        candidate = repo_root / "experiments" / experiment_type / iteration_id
        if candidate.exists():
            return f"iteration directory already exists: {candidate}"

    return ""


def _is_entry_completed(entry: dict[str, Any]) -> bool:
    experiment_type = _normalize_experiment_type(entry.get("type"))
    if experiment_type == "done":
        return True
    return _is_backlog_status_completed(entry.get("status"))


def _normalize_experiment_stage(value: str) -> tuple[str, str]:
    normalized = _normalize_space(value).lower()
    if normalized == "planned":
        normalized = "plan"
    if normalized not in EXPERIMENT_TYPES:
        return (
            "",
            f"unsupported --to value '{value}' (expected one of: planned, plan, in_progress, done)",
        )
    return (normalized, "")


def _mapped_backlog_status_for_type(experiment_type: str) -> str:
    if experiment_type == "done":
        return "completed"
    if experiment_type == "in_progress":
        return "in_progress"
    return "open"


def _rewrite_iteration_prefix_scoped(
    repo_root: Path,
    *,
    iteration_dir: Path,
    old_prefix: str,
    new_prefix: str,
) -> tuple[list[Path], str]:
    if old_prefix == new_prefix:
        return ([], "")

    changed: list[Path] = []
    original_texts: dict[Path, str] = {}
    candidates: list[Path] = []

    for path in sorted(iteration_dir.rglob("*")):
        if path.is_file():
            candidates.append(path)

    autolab_dir = repo_root / ".autolab"
    for path in sorted(autolab_dir.glob("*.json")):
        if path.is_file():
            candidates.append(path)

    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        except Exception:
            continue
        if old_prefix not in text:
            continue
        updated = text.replace(old_prefix, new_prefix)
        if updated == text:
            continue
        original_texts[path] = text
        try:
            path.write_text(updated, encoding="utf-8")
        except Exception as exc:
            rollback_errors: list[str] = []
            for rollback_path, original_text in reversed(list(original_texts.items())):
                try:
                    rollback_path.write_text(original_text, encoding="utf-8")
                except Exception as rollback_exc:
                    rollback_errors.append(f"{rollback_path}: {rollback_exc}")
            rollback_suffix = (
                f"; rollback failures: {' | '.join(rollback_errors)}"
                if rollback_errors
                else ""
            )
            return ([], f"failed rewriting '{path}': {exc}{rollback_suffix}")
        changed.append(path)
    return (changed, "")


def _insert_todo_task_line(todo_path: Path, *, line: str) -> None:
    if todo_path.exists():
        lines = todo_path.read_text(encoding="utf-8").splitlines()
    else:
        lines = [
            "# TODO",
            "",
            "## Tasks",
            "<!-- Add one bullet per task. Optional stage tag: [stage:design]. -->",
            "",
            "## Notes",
            "Write non-task notes here. Bullets in this section are ignored by autolab steering.",
        ]

    tasks_idx = -1
    notes_idx = -1
    for idx, raw_line in enumerate(lines):
        lowered = raw_line.strip().lower()
        if lowered == "## tasks" and tasks_idx < 0:
            tasks_idx = idx
        if lowered == "## notes" and notes_idx < 0:
            notes_idx = idx

    if tasks_idx < 0:
        lines.extend(["", "## Tasks"])
        tasks_idx = len(lines) - 1
    if notes_idx < 0:
        lines.extend(["", "## Notes"])
        notes_idx = len(lines) - 1

    insert_at = notes_idx
    while insert_at > tasks_idx + 1 and not lines[insert_at - 1].strip():
        insert_at -= 1
    lines.insert(insert_at, line)

    # Keep one blank line before notes section for readability.
    notes_idx = next(
        (
            idx
            for idx, raw_line in enumerate(lines)
            if raw_line.strip().lower() == "## notes"
        ),
        -1,
    )
    if notes_idx > 0 and lines[notes_idx - 1].strip():
        lines.insert(notes_idx, "")

    todo_path.parent.mkdir(parents=True, exist_ok=True)
    todo_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _resolve_todo_selector(
    open_tasks: list[dict[str, Any]], selector: str
) -> tuple[str, str]:
    normalized = _normalize_space(selector)
    if not normalized:
        return ("", "task selector is empty")

    for task in open_tasks:
        task_id = _normalize_space(str(task.get("task_id", "")))
        if task_id == normalized:
            return (task_id, "")

    if normalized.isdigit():
        index = int(normalized)
        if 1 <= index <= len(open_tasks):
            task_id = _normalize_space(str(open_tasks[index - 1].get("task_id", "")))
            if task_id:
                return (task_id, "")
        return (
            "",
            f"task index {index} is out of range (open tasks: {len(open_tasks)})",
        )

    return ("", f"task selector '{normalized}' did not match any open task")


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------


# Re-export all helper names (including underscored internals) for handler modules.
__all__ = [name for name in globals() if not name.startswith("__")]
