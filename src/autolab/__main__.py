from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import shutil
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from autolab.slurm_job_list import (
    append_entry_idempotent,
    canonical_slurm_job_bullet,
    is_slurm_manifest,
    ledger_contains_entry,
    ledger_contains_run_id,
    required_slurm_job_id,
)
from autolab.todo_sync import (
    mark_task_completed,
    select_decision_from_todo,
    select_open_task,
    sync_todo_post_run,
    sync_todo_pre_run,
)

try:
    import yaml
except Exception:  # pragma: no cover - environment may omit pyyaml
    yaml = None


ACTIVE_STAGES = (
    "hypothesis",
    "design",
    "implementation",
    "implementation_review",
    "launch",
    "extract_results",
    "update_docs",
)
TERMINAL_STAGES = ("human_review", "stop")
DECISION_STAGES = ("hypothesis", "design", "stop", "human_review")
ALL_STAGES = set(ACTIVE_STAGES + ("decide_repeat",) + TERMINAL_STAGES)
PACKAGE_SCAFFOLD_DIR = Path(__file__).resolve().parent / "scaffold" / ".autolab"

DEFAULT_BACKLOG_TEMPLATE = """hypotheses:
  - id: h1
    status: open
    title: "Bootstrap hypothesis"
    success_metric: "primary_metric"
    target_delta: 0.0
experiments:
  - id: e1
    hypothesis_id: h1
    status: open
    iteration_id: "{iteration_id}"
"""

DEFAULT_VERIFIER_POLICY = """test_command: "./venv/bin/python -m pytest"
dry_run_command: ""
require_tests: false
require_dry_run: false
require_env_smoke: false
require_docs_target_update: false
template_fill:
  enabled: true
  command: "./venv/bin/python .autolab/verifiers/template_fill.py"
  stages:
    hypothesis: true
    design: true
    implementation: true
    implementation_review: true
    launch: true
    extract_results: true
    update_docs: true
template_fill_by_stage:
  hypothesis: "./venv/bin/python .autolab/verifiers/template_fill.py --stage hypothesis"
  design: "./venv/bin/python .autolab/verifiers/template_fill.py --stage design"
  implementation: "./venv/bin/python .autolab/verifiers/template_fill.py --stage implementation"
  implementation_review: "./venv/bin/python .autolab/verifiers/template_fill.py --stage implementation_review"
  launch: "./venv/bin/python .autolab/verifiers/template_fill.py --stage launch"
  extract_results: "./venv/bin/python .autolab/verifiers/template_fill.py --stage extract_results"
  update_docs: "./venv/bin/python .autolab/verifiers/template_fill.py --stage update_docs"
requirements_by_stage:
  hypothesis:
    schema: true
  design:
    schema: true
  implementation:
    dry_run: true
    schema: true
  implementation_review:
    schema: true
    docs_target_update: true
    env_smoke: true
  launch:
    schema: true
    env_smoke: true
  extract_results:
    schema: true
    env_smoke: true
  update_docs:
    schema: true
    env_smoke: true
    docs_target_update: true
autorun:
  guardrails:
    max_same_decision_streak: 3
    max_no_progress_decisions: 2
    max_update_docs_cycles: 3
    max_generated_todo_tasks: 5
    on_breach: "human_review"
  auto_commit:
    mode: "meaningful_only"
  meaningful_change:
    require_implementation_progress: true
    require_git_for_progress: true
    on_non_git_behavior: "warn_and_continue"
    require_verification: true
    exclude_paths:
      - ".autolab/**"
      - "docs/todo.md"
      - "docs/wiki/**"
      - "experiments/*/docs_update.md"
agent_runner:
  enabled: true
  runner: codex  # Options: codex, claude, custom
  stages:
    - hypothesis
    - design
    - implementation
    - implementation_review
    - launch
    - extract_results
    - update_docs
  edit_scope:
    mode: "iteration_plus_core"
    core_dirs:
      - "src"
      - "scripts"
      - ".autolab"
      - "docs"
      - "paper"
      - "tests"
    ensure_iteration_dir: true
  timeout_seconds: 3600
"""

LOCK_STALE_SECONDS = 30 * 60
DEFAULT_MAX_HOURS = 8.0
AGENT_RUNNER_PRESETS: dict[str, str] = {
    "codex": "cat {prompt_path} | codex exec -s workspace-write -a never -C {workspace_dir} {core_add_dirs} -",
    "claude": "cat {prompt_path} | env -u CLAUDECODE claude -p --dangerously-skip-permissions --output-format text --verbose -",
}
DEFAULT_AGENT_RUNNER_NAME = "codex"
DEFAULT_AGENT_RUNNER_COMMAND = (
    # {prompt_path} resolves to rendered prompt content under .autolab/prompts/rendered/.
    AGENT_RUNNER_PRESETS[DEFAULT_AGENT_RUNNER_NAME]
)
DEFAULT_AGENT_RUNNER_STAGES = tuple(ACTIVE_STAGES)
DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS = 3600.0
DEFAULT_AGENT_RUNNER_EDIT_SCOPE_MODE = "iteration_plus_core"
AGENT_RUNNER_EDIT_SCOPE_MODES = ("iteration_only", "iteration_plus_core")
DEFAULT_AGENT_RUNNER_CORE_DIRS = ("src", "scripts", ".autolab", "docs", "paper", "tests")
DEFAULT_AGENT_RUNNER_ENSURE_ITERATION_DIR = True
DEFAULT_AUTO_COMMIT_MODE = "meaningful_only"
AUTO_COMMIT_MODES = ("meaningful_only", "always", "disabled")
DEFAULT_MEANINGFUL_EXCLUDE_PATHS = (
    ".autolab/**",
    "docs/todo.md",
    "docs/wiki/**",
    "experiments/*/docs_update.md",
)
ASSISTANT_CYCLE_STAGES = ("select", "implement", "verify", "review", "done")
ASSISTANT_CONTROL_COMMIT_PATHS = (
    ".autolab/agent_result.json",
    ".autolab/state.json",
    ".autolab/todo_state.json",
    "docs/todo.md",
)
BACKLOG_COMPLETED_STATUSES = {"done", "completed", "closed", "resolved"}
ITERATION_ID_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
RUN_ID_TIMESTAMP_PATTERN = re.compile(r"(20\d{6}T\d{6}Z)")
STAGE_PROMPT_FILES = {
    "hypothesis": "stage_hypothesis.md",
    "design": "stage_design.md",
    "implementation": "stage_implementation.md",
    "implementation_review": "stage_implementation_review.md",
    "launch": "stage_launch.md",
    "extract_results": "stage_extract_results.md",
    "update_docs": "stage_update_docs.md",
}
STAGE_PROMPT_FILES_LEGACY = {
    "extract_results": "stage_extract.md",
    "update_docs": "stage_docs.md",
}
SLURM_JOB_LIST_PATH = Path("docs/slurm_job_list.md")
PROMPT_TOKEN_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
PROMPT_LITERAL_TOKENS = ("<ITERATION_ID>", "<RUN_ID>")
PROMPT_SHARED_INCLUDE_PATTERN = re.compile(r"\{\{\s*shared:([A-Za-z0-9_.-]+)\s*\}\}")
PROMPT_REQUIRED_TOKENS_BY_STAGE = {
    "hypothesis": {"iteration_id", "hypothesis_id"},
    "design": {"iteration_id", "hypothesis_id"},
    "implementation": {"iteration_id"},
    "implementation_review": {"iteration_id"},
    "launch": {"iteration_id"},
    "extract_results": {"iteration_id", "run_id"},
    "update_docs": {"iteration_id", "run_id"},
}
REVIEW_RESULT_REQUIRED_CHECKS = (
    "tests",
    "dry_run",
    "schema",
    "env_smoke",
    "docs_target_update",
)
HOST_MODE_COMMAND_TIMEOUT_SECONDS = 2
VERIFIER_COMMAND_TIMEOUT_SECONDS = 120


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value) if value is not None else default


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _coerce_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


class StateError(RuntimeError):
    """Raised when state cannot be loaded or validated."""


class StageCheckError(RuntimeError):
    """Raised when a stage fails lightweight checks."""


@dataclass(frozen=True)
class RunOutcome:
    exit_code: int
    transitioned: bool
    stage_before: str
    stage_after: str
    message: str
    commit_allowed: bool = True
    commit_task_id: str = ""
    commit_cycle_stage: str = ""
    commit_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentRunnerConfig:
    runner: str
    enabled: bool
    command: str
    stages: tuple[str, ...]
    edit_scope: "AgentRunnerEditScopeConfig"
    timeout_seconds: float


@dataclass(frozen=True)
class AgentRunnerEditScopeConfig:
    mode: str
    core_dirs: tuple[str, ...]
    ensure_iteration_dir: bool


@dataclass(frozen=True)
class RenderedPromptBundle:
    template_path: Path
    rendered_path: Path
    context_path: Path
    prompt_text: str
    context_payload: dict[str, Any]


@dataclass(frozen=True)
class GuardrailConfig:
    max_same_decision_streak: int
    max_no_progress_decisions: int
    max_update_docs_cycles: int
    on_breach: str


@dataclass(frozen=True)
class MeaningfulChangeConfig:
    require_verification: bool
    require_implementation_progress: bool
    require_git_for_progress: bool
    on_non_git_behavior: str
    exclude_paths: tuple[str, ...]


@dataclass(frozen=True)
class AutoCommitConfig:
    mode: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _resolve_repo_root(state_path: Path) -> Path:
    if state_path.name == "state.json" and state_path.parent.name == ".autolab":
        return state_path.parent.parent
    return Path.cwd()


def _resolve_scaffold_source() -> Path:
    if PACKAGE_SCAFFOLD_DIR.exists():
        return PACKAGE_SCAFFOLD_DIR
    legacy = Path(__file__).resolve().parent.parent / "dotautolab" / ".autolab"
    if legacy.exists():
        return legacy
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
    return normalized


def _append_log(repo_root: Path, message: str) -> None:
    log_path = repo_root / ".autolab" / "logs" / "orchestrator.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(f"{_utc_now()} {message}\n")


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
        commit_message = f"{commit_message} [stage:{outcome.stage_before}->{outcome.stage_after}]"
    return commit_message


def _try_auto_commit(repo_root: Path, *, outcome: RunOutcome) -> str:
    if not outcome.commit_allowed:
        return "auto_commit: skipped (non-meaningful cycle)"
    inside = _run_git(repo_root, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return "auto_commit: skipped (not a git work tree)"

    conflicts = _run_git(repo_root, ["diff", "--name-only", "--diff-filter=U"])
    if conflicts.returncode != 0:
        detail = _compact_log_text((conflicts.stderr or conflicts.stdout or "unknown git error").strip())
        _append_log(repo_root, f"auto_commit probe failed: {detail}")
        return f"auto_commit: skipped (probe failed: {detail})"
    if conflicts.stdout.strip():
        _append_log(repo_root, "auto_commit skipped due to unresolved merge conflicts")
        return "auto_commit: skipped (unresolved merge conflicts)"

    scoped_paths = tuple(path for path in dict.fromkeys(outcome.commit_paths) if str(path).strip())
    if outcome.commit_paths and not scoped_paths:
        return "auto_commit: skipped (no scoped paths)"

    add_args = ["add", "--", *scoped_paths] if scoped_paths else ["add", "-A"]
    add = _run_git(repo_root, add_args)
    if add.returncode != 0:
        detail = _compact_log_text((add.stderr or add.stdout or "git add failed").strip())
        _append_log(repo_root, f"auto_commit add failed: {detail}")
        return f"auto_commit: failed (git add failed: {detail})"

    staged_args = ["diff", "--cached", "--quiet"]
    if scoped_paths:
        staged_args.extend(["--", *scoped_paths])
    staged = _run_git(repo_root, staged_args)
    if staged.returncode == 0:
        return "auto_commit: skipped (no changes)"
    if staged.returncode not in {0, 1}:
        detail = _compact_log_text((staged.stderr or staged.stdout or "git diff --cached failed").strip())
        _append_log(repo_root, f"auto_commit staged-check failed: {detail}")
        return f"auto_commit: failed (staged check failed: {detail})"

    staged_paths = _collect_staged_paths(repo_root, scoped_paths)
    commit_message = _build_auto_commit_message(outcome, staged_paths)
    commit_args = ["commit", "-m", commit_message]
    if scoped_paths:
        commit_args.extend(["--", *scoped_paths])
    commit = _run_git(repo_root, commit_args)
    if commit.returncode != 0:
        detail = _compact_log_text((commit.stderr or commit.stdout or "git commit failed").strip())
        _append_log(repo_root, f"auto_commit commit failed: {detail}")
        return f"auto_commit: failed ({detail})"

    head = _run_git(repo_root, ["rev-parse", "--short", "HEAD"])
    commit_id = head.stdout.strip() if head.returncode == 0 else "<unknown>"
    _append_log(repo_root, f"auto_commit created commit {commit_id}: {commit_message}")
    return f"auto_commit: committed {commit_id}"


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


def _resolve_autolab_dir(state_path: Path, repo_root: Path) -> Path:
    if state_path.name == "state.json" and state_path.parent.name == ".autolab":
        return state_path.parent
    return repo_root / ".autolab"


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
    return sum(1 for task in tasks.values() if isinstance(task, dict) and task.get("status") == "open")


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


def _default_agent_runner_edit_scope() -> AgentRunnerEditScopeConfig:
    return AgentRunnerEditScopeConfig(
        mode=DEFAULT_AGENT_RUNNER_EDIT_SCOPE_MODE,
        core_dirs=DEFAULT_AGENT_RUNNER_CORE_DIRS,
        ensure_iteration_dir=DEFAULT_AGENT_RUNNER_ENSURE_ITERATION_DIR,
    )


def _load_agent_runner_edit_scope(runner: dict[str, Any]) -> AgentRunnerEditScopeConfig:
    raw_edit_scope = runner.get("edit_scope")
    if raw_edit_scope is None:
        return _default_agent_runner_edit_scope()
    if not isinstance(raw_edit_scope, dict):
        raise StageCheckError("agent_runner.edit_scope must be a mapping")

    mode = str(raw_edit_scope.get("mode", DEFAULT_AGENT_RUNNER_EDIT_SCOPE_MODE)).strip().lower()
    if mode not in AGENT_RUNNER_EDIT_SCOPE_MODES:
        raise StageCheckError(
            f"agent_runner.edit_scope.mode must be one of {', '.join(AGENT_RUNNER_EDIT_SCOPE_MODES)}"
        )

    raw_core_dirs = raw_edit_scope.get("core_dirs", list(DEFAULT_AGENT_RUNNER_CORE_DIRS))
    if raw_core_dirs is None:
        raw_core_dirs = list(DEFAULT_AGENT_RUNNER_CORE_DIRS)
    if not isinstance(raw_core_dirs, list):
        raise StageCheckError("agent_runner.edit_scope.core_dirs must be a list of repo-relative directory paths")
    core_dirs: list[str] = []
    for raw_dir in raw_core_dirs:
        value = str(raw_dir).strip()
        if not value:
            raise StageCheckError("agent_runner.edit_scope.core_dirs entries must be non-empty")
        if value not in core_dirs:
            core_dirs.append(value)

    ensure_iteration_dir = bool(
        raw_edit_scope.get("ensure_iteration_dir", DEFAULT_AGENT_RUNNER_ENSURE_ITERATION_DIR)
    )
    if mode == "iteration_only":
        core_dirs = []

    return AgentRunnerEditScopeConfig(
        mode=mode,
        core_dirs=tuple(core_dirs),
        ensure_iteration_dir=ensure_iteration_dir,
    )


def _load_agent_runner_config(repo_root: Path) -> AgentRunnerConfig:
    policy_path = repo_root / ".autolab" / "verifier_policy.yaml"
    if yaml is None or not policy_path.exists():
        return AgentRunnerConfig(
            runner=DEFAULT_AGENT_RUNNER_NAME,
            enabled=False,
            command=DEFAULT_AGENT_RUNNER_COMMAND,
            stages=DEFAULT_AGENT_RUNNER_STAGES,
            edit_scope=_default_agent_runner_edit_scope(),
            timeout_seconds=DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS,
        )

    try:
        loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageCheckError(f"agent_runner policy could not be parsed at {policy_path}: {exc}") from exc

    if not isinstance(loaded, dict):
        return AgentRunnerConfig(
            runner=DEFAULT_AGENT_RUNNER_NAME,
            enabled=False,
            command=DEFAULT_AGENT_RUNNER_COMMAND,
            stages=DEFAULT_AGENT_RUNNER_STAGES,
            edit_scope=_default_agent_runner_edit_scope(),
            timeout_seconds=DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS,
        )

    runner_section = loaded.get("agent_runner")
    if runner_section is None:
        return AgentRunnerConfig(
            runner=DEFAULT_AGENT_RUNNER_NAME,
            enabled=False,
            command=DEFAULT_AGENT_RUNNER_COMMAND,
            stages=DEFAULT_AGENT_RUNNER_STAGES,
            edit_scope=_default_agent_runner_edit_scope(),
            timeout_seconds=DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS,
        )
    if not isinstance(runner_section, dict):
        raise StageCheckError("agent_runner policy must be a mapping")

    runner_name = str(runner_section.get("runner", DEFAULT_AGENT_RUNNER_NAME)).strip()
    valid_runners = set(AGENT_RUNNER_PRESETS) | {"custom"}
    if runner_name not in valid_runners:
        raise StageCheckError(
            f"agent_runner.runner must be one of {sorted(valid_runners)}, got '{runner_name}'"
        )

    enabled = bool(runner_section.get("enabled", False))
    raw_command = runner_section.get("command")
    if raw_command is not None:
        command = str(raw_command).strip()
    else:
        command = AGENT_RUNNER_PRESETS.get(runner_name, DEFAULT_AGENT_RUNNER_COMMAND)
    if enabled and not command:
        raise StageCheckError("agent_runner.command must be set when agent_runner.enabled is true")

    raw_stages = runner_section.get("stages")
    if raw_stages is None:
        stages = list(DEFAULT_AGENT_RUNNER_STAGES)
    else:
        if not isinstance(raw_stages, list):
            raise StageCheckError("agent_runner.stages must be a list of stage names")
        stages = []
        for raw_stage in raw_stages:
            stage = str(raw_stage).strip()
            if stage not in ACTIVE_STAGES:
                raise StageCheckError(f"agent_runner.stages includes unsupported stage '{stage}'")
            if stage not in stages:
                stages.append(stage)
    if enabled and not stages:
        raise StageCheckError("agent_runner.stages must include at least one active stage")

    raw_timeout = runner_section.get("timeout_seconds", DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS)
    try:
        timeout_seconds = float(raw_timeout)
    except Exception as exc:
        raise StageCheckError("agent_runner.timeout_seconds must be a non-negative number") from exc
    if timeout_seconds < 0:
        raise StageCheckError("agent_runner.timeout_seconds must be >= 0")
    if timeout_seconds == 0:
        timeout_seconds = DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS

    edit_scope = _load_agent_runner_edit_scope(runner_section)

    return AgentRunnerConfig(
        runner=runner_name,
        enabled=enabled,
        command=command,
        stages=tuple(stages),
        edit_scope=edit_scope,
        timeout_seconds=timeout_seconds,
    )


def _load_verifier_policy(repo_root: Path) -> dict[str, Any]:
    policy_path = repo_root / ".autolab" / "verifier_policy.yaml"
    if yaml is None or not policy_path.exists():
        return {}
    try:
        loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _load_guardrail_config(repo_root: Path) -> GuardrailConfig:
    policy = _load_verifier_policy(repo_root)
    autorun = policy.get("autorun")
    guardrails = autorun.get("guardrails") if isinstance(autorun, dict) else {}
    if not isinstance(guardrails, dict):
        guardrails = {}
    max_same = int(guardrails.get("max_same_decision_streak", 3) or 3)
    max_no_progress = int(guardrails.get("max_no_progress_decisions", 2) or 2)
    max_update_docs = int(guardrails.get("max_update_docs_cycles", 3) or 3)
    on_breach = str(guardrails.get("on_breach", "human_review")).strip() or "human_review"
    if on_breach not in TERMINAL_STAGES:
        on_breach = "human_review"
    if max_same < 1:
        max_same = 1
    if max_no_progress < 1:
        max_no_progress = 1
    if max_update_docs < 1:
        max_update_docs = 1
    return GuardrailConfig(
        max_same_decision_streak=max_same,
        max_no_progress_decisions=max_no_progress,
        max_update_docs_cycles=max_update_docs,
        on_breach=on_breach,
    )


def _load_meaningful_change_config(repo_root: Path) -> MeaningfulChangeConfig:
    policy = _load_verifier_policy(repo_root)
    autorun = policy.get("autorun")
    meaningful = autorun.get("meaningful_change") if isinstance(autorun, dict) else {}
    if not isinstance(meaningful, dict):
        meaningful = {}
    require_verification = bool(meaningful.get("require_verification", True))
    require_implementation_progress = bool(
        meaningful.get("require_implementation_progress", True)
    )
    require_git_for_progress = bool(meaningful.get("require_git_for_progress", True))
    on_non_git_behavior = str(
        meaningful.get("on_non_git_behavior", "warn_and_continue")
    ).strip().lower()
    if on_non_git_behavior not in {"warn_and_continue", "fail"}:
        on_non_git_behavior = "warn_and_continue"
    raw_patterns = meaningful.get("exclude_paths", list(DEFAULT_MEANINGFUL_EXCLUDE_PATHS))
    patterns: list[str] = []
    if isinstance(raw_patterns, list):
        for entry in raw_patterns:
            candidate = str(entry).strip()
            if candidate:
                patterns.append(candidate)
    if not patterns:
        patterns = list(DEFAULT_MEANINGFUL_EXCLUDE_PATHS)
    return MeaningfulChangeConfig(
        require_verification=require_verification,
        require_implementation_progress=require_implementation_progress,
        require_git_for_progress=require_git_for_progress,
        on_non_git_behavior=on_non_git_behavior,
        exclude_paths=tuple(patterns),
    )


def _load_auto_commit_config(repo_root: Path) -> AutoCommitConfig:
    policy = _load_verifier_policy(repo_root)
    autorun = policy.get("autorun")
    auto_commit = autorun.get("auto_commit") if isinstance(autorun, dict) else {}
    if not isinstance(auto_commit, dict):
        auto_commit = {}
    mode = str(auto_commit.get("mode", DEFAULT_AUTO_COMMIT_MODE)).strip().lower()
    if mode not in AUTO_COMMIT_MODES:
        mode = DEFAULT_AUTO_COMMIT_MODE
    return AutoCommitConfig(mode=mode)


def _resolve_run_agent_mode(mode_value: str | None) -> str:
    candidate = str(mode_value or "policy").strip().lower()
    if candidate in {"policy", "force_on", "force_off"}:
        return candidate
    return "policy"


def _probe_host_command(argv: list[str], *, timeout: float = HOST_MODE_COMMAND_TIMEOUT_SECONDS) -> tuple[bool, str]:
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
    return (proc.returncode == 0, "ok" if proc.returncode == 0 else f"exit_{proc.returncode}")


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
        if (has_sinfo and sinfo_ok) or (has_sbatch and sbatch_ok) or (has_squeue and squeue_ok):
            return ("slurm", probe)
        probe["note"] = "slurm environment detected but command probes incomplete"

    if has_sinfo and has_squeue and sinfo_ok and squeue_ok:
        return ("slurm", probe)
    if has_sbatch and (sinfo_ok or squeue_ok):
        return ("slurm", probe)
    return ("local", probe)


def _has_slurm_env() -> bool:
    return bool(
        os.environ.get("SLURM_CLUSTER_NAME")
        or os.environ.get("SLURM_JOB_ID")
        or os.environ.get("SLURM_JOB_NODELIST")
        or os.environ.get("SLURM_NNODES")
    )


def _is_command_available(command: str) -> bool:
    return shutil.which(command) is not None


def _detect_priority_host_mode() -> str:
    host_mode, _probe = _detect_host_mode_with_probe()
    return host_mode


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


def _snapshot_delta_paths(baseline_snapshot: dict[str, str], current_snapshot: dict[str, str]) -> list[str]:
    delta_paths = [path for path, signature in current_snapshot.items() if baseline_snapshot.get(path) != signature]
    return sorted(delta_paths)


def _assistant_commit_paths(delta_paths: list[str], meaningful_paths: list[str]) -> tuple[str, ...]:
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


def _evaluate_meaningful_change(
    repo_root: Path,
    config: MeaningfulChangeConfig,
    *,
    baseline_snapshot: dict[str, str] | None = None,
) -> tuple[bool, list[str], list[str], dict[str, str]]:
    current_snapshot = _collect_change_snapshot(repo_root)
    changed_paths = sorted(current_snapshot.keys())
    if baseline_snapshot is None:
        delta_paths = changed_paths
    else:
        delta_paths = _snapshot_delta_paths(baseline_snapshot, current_snapshot)
    meaningful_paths = [path for path in delta_paths if not _path_matches_any(path, config.exclude_paths)]
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
    meaningful, delta_paths, meaningful_paths, _current_snapshot = _evaluate_meaningful_change(
        repo_root,
        meaningful_config,
        baseline_snapshot=baseline_snapshot,
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


def _replace_iteration_placeholders(command: str, iteration_id: str) -> str:
    return command.replace("<ITERATION_ID>", iteration_id).replace("{{iteration_id}}", iteration_id)


def _run_verification_step(repo_root: Path, state: dict[str, Any]) -> tuple[bool, str]:
    policy = _load_verifier_policy(repo_root)
    iteration_id = str(state.get("iteration_id", "")).strip()
    stage = str(state.get("stage", "")).strip()

    stage_requirements: dict[str, bool] = {
        "tests": _coerce_bool(policy.get("require_tests", False)),
        "dry_run": _coerce_bool(policy.get("require_dry_run", False)),
        "env_smoke": _coerce_bool(policy.get("require_env_smoke", False)),
        "docs_target_update": _coerce_bool(policy.get("require_docs_target_update", False)),
    }
    requirements_by_stage = policy.get("requirements_by_stage", {})
    if isinstance(requirements_by_stage, dict):
        stage_section = requirements_by_stage.get(stage, {})
        if isinstance(stage_section, dict):
            stage_requirements["tests"] = _coerce_bool(
                stage_section.get("tests"),
                default=stage_requirements["tests"],
            )
            stage_requirements["dry_run"] = _coerce_bool(
                stage_section.get("dry_run"),
                default=stage_requirements["dry_run"],
            )
            stage_requirements["env_smoke"] = _coerce_bool(
                stage_section.get("env_smoke"),
                default=stage_requirements["env_smoke"],
            )
            stage_requirements["docs_target_update"] = _coerce_bool(
                stage_section.get("docs_target_update"),
                default=stage_requirements["docs_target_update"],
            )

    test_command = str(policy.get("test_command", "")).strip()
    dry_run_command = str(policy.get("dry_run_command", "")).strip()
    if not dry_run_command and stage_requirements.get("dry_run"):
        return (False, "verification dry-run command is required by policy but not configured")
    if not test_command and stage_requirements.get("tests"):
        return (False, "verification test command is required by policy but not configured")

    template_fill_section = policy.get("template_fill", {})
    template_fill_enabled = False
    template_fill_command = ""
    if isinstance(template_fill_section, dict):
        template_fill_enabled = bool(template_fill_section.get("enabled", False))
        if _coerce_bool(template_fill_section.get("enabled"), default=template_fill_enabled):
            raw_template_fill_command = str(template_fill_section.get("command", "")).strip()
            template_fill_command = raw_template_fill_command

    template_fill_by_stage = policy.get("template_fill_by_stage", {})
    if isinstance(template_fill_by_stage, dict):
        stage_template_fill = template_fill_by_stage.get(stage)
        if isinstance(stage_template_fill, str) and stage_template_fill.strip():
            template_fill_command = stage_template_fill.strip()

    command_specs: list[tuple[str, str]] = []

    commands: list[str] = []
    if stage_requirements["tests"] and test_command:
        commands.append(test_command)
    if stage_requirements["dry_run"] and dry_run_command:
        commands.append(_replace_iteration_placeholders(dry_run_command, iteration_id))
    if template_fill_enabled and template_fill_command:
        command_specs.append(("template_fill", template_fill_command))
    if stage_requirements["env_smoke"]:
        command_specs.append(("run_health", "./venv/bin/python .autolab/verifiers/run_health.py"))
        command_specs.append(("result_sanity", "./venv/bin/python .autolab/verifiers/result_sanity.py"))
    if stage_requirements["docs_target_update"] and stage in {"update_docs", "implementation_review"}:
        command_specs.append(
            ("docs_targets", "./venv/bin/python .autolab/verifiers/docs_targets.py")
        )
    command_specs.append(("schema_checks", "./venv/bin/python .autolab/verifiers/schema_checks.py"))

    for _name, command in command_specs:
        if command:
            commands.append(command)

    if not commands and stage_requirements["tests"]:
        return (
            False,
            "verification command list is empty after policy resolution; update verifier_policy requirements",
        )
    if not commands:
        commands.append("true")

    if not commands:
        return (False, "verification command not configured")

    for command in commands:
        if not command.strip():
            continue
        try:
            process = subprocess.run(
                command,
                cwd=repo_root,
                shell=True,
                text=True,
                capture_output=True,
                check=False,
                timeout=VERIFIER_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            detail = _compact_log_text(f"verification command timed out: {command}")
            _append_log(repo_root, f"assistant verification timeout command={command}")
            return (False, f"verification failed: {detail}")
        except OSError as exc:
            detail = _compact_log_text(f"verification command failed to start: {exc}")
            _append_log(repo_root, f"assistant verification failed command={command} detail={detail}")
            return (False, f"verification failed: {detail}")
        if process.returncode != 0:
            detail = _compact_log_text((process.stderr or process.stdout or "verification failed").strip())
            _append_log(repo_root, f"assistant verification failed command={command} detail={detail}")
            return (False, f"verification failed: {detail}")
    return (True, f"verification passed ({len(commands)} command(s))")


def _resolve_latest_run_state(iteration_dir: Path) -> tuple[str, str]:
    manifests = sorted(iteration_dir.glob("runs/*/run_manifest.json"))
    if not manifests:
        raise StageCheckError(f"launch did not produce run_manifest.json under {iteration_dir / 'runs'}")
    manifest_candidates: list[tuple[int, datetime, str, str, dict[str, Any]]] = []
    for manifest_path in manifests:
        payload = _load_dict_json(manifest_path, "runs/<run_id>/run_manifest.json")
        run_dir = manifest_path.parent
        run_id = str(payload.get("run_id", "")).strip() or run_dir.name
        parsed_timestamp = _manifest_timestamp(payload, run_id)
        timestamp = parsed_timestamp or datetime.min.replace(tzinfo=timezone.utc)
        has_timestamp = 1 if parsed_timestamp is not None else 0
        manifest_candidates.append((has_timestamp, timestamp, run_id, str(manifest_path), payload))
    _has_timestamp, _timestamp, run_id, _manifest_path, payload = max(
        manifest_candidates,
        key=lambda item: (item[0], item[1], item[2], item[3]),
    )

    sync_status = "completed"
    sync_payload = payload.get("artifact_sync_to_local")
    if not isinstance(sync_payload, dict):
        sync_root = payload.get("sync")
        if isinstance(sync_root, dict):
            nested_payload = sync_root.get("artifact_sync_to_local")
            if isinstance(nested_payload, dict):
                sync_payload = nested_payload
    if isinstance(sync_payload, dict):
        raw_sync = str(sync_payload.get("status", "")).strip().lower()
        if raw_sync in {"ok", "completed", "success"}:
            sync_status = "completed"
        elif raw_sync:
            sync_status = raw_sync
    launch_payload = payload.get("launch")
    nested_launch_mode = launch_payload.get("mode", "") if isinstance(launch_payload, dict) else ""
    launch_mode = str(
        payload.get("launch_mode")
        or payload.get("host_mode")
        or payload.get("detected_host_mode")
        or nested_launch_mode
    ).strip().lower()
    if launch_mode == "slurm" and sync_status not in {"completed", "ok", "success"}:
        raise StageCheckError(
            f"latest run {run_id} has incomplete artifact synchronization for slurm mode: {sync_status}"
        )
    return (run_id, sync_status)


def _validate_slurm_job_ledger_entry(
    repo_root: Path,
    *,
    manifest_path: Path,
    payload: dict[str, Any],
    stage: str,
) -> None:
    if not is_slurm_manifest(payload):
        return

    try:
        # Enforces required SLURM identifiers, including strict job_id presence.
        canonical_slurm_job_bullet(payload)
    except ValueError as exc:
        raise StageCheckError(f"{manifest_path} {exc}") from exc

    run_id = str(payload.get("run_id", "")).strip() or manifest_path.parent.name
    doc_path = repo_root / SLURM_JOB_LIST_PATH
    if not doc_path.exists():
        raise StageCheckError(
            f"{stage} requires {SLURM_JOB_LIST_PATH} for SLURM runs; missing at {doc_path}"
        )

    ledger_text = doc_path.read_text(encoding="utf-8")
    if not ledger_contains_run_id(ledger_text, run_id):
        raise StageCheckError(
            f"{stage} requires SLURM ledger entry run_id={run_id} in {SLURM_JOB_LIST_PATH}"
        )


def _resolve_stage_prompt_path(repo_root: Path, stage: str) -> Path:
    prompt_name = STAGE_PROMPT_FILES.get(stage)
    if prompt_name is None:
        raise StageCheckError(f"no stage prompt mapping is defined for '{stage}'")
    candidate = repo_root / ".autolab" / "prompts" / prompt_name
    if candidate.exists():
        return candidate
    legacy_prompt_name = STAGE_PROMPT_FILES_LEGACY.get(stage)
    if legacy_prompt_name:
        legacy_candidate = repo_root / ".autolab" / "prompts" / legacy_prompt_name
        if legacy_candidate.exists():
            return legacy_candidate
        raise StageCheckError(
            f"stage prompt is missing (new='{candidate.name}', legacy='{legacy_candidate.name}')"
        )
    raise StageCheckError(f"stage prompt is missing for '{stage}' ({candidate})")


def _resolve_prompt_shared_path(repo_root: Path, shared_name: str) -> Path:
    return repo_root / ".autolab" / "prompts" / "shared" / shared_name


def _render_prompt_includes(repo_root: Path, text: str, *, stage: str) -> str:
    """Render {{shared:...}} include directives."""
    rendered = text
    for _ in range(4):
        changed = False

        def _replace(match: re.Match[str]) -> str:
            nonlocal changed
            shared_name = match.group(1).strip()
            if not shared_name:
                return ""
            shared_path = _resolve_prompt_shared_path(repo_root, shared_name)
            if not shared_path.exists():
                raise StageCheckError(
                    f"prompt shared include '{shared_name}' is missing for stage '{stage}'"
                )
            include_text = shared_path.read_text(encoding="utf-8")
            changed = True
            return include_text

        rendered = PROMPT_SHARED_INCLUDE_PATTERN.sub(_replace, rendered)
        if not changed:
            break
    return rendered


def _compact_log_text(text: str, limit: int = 240) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}..."


def _default_stage_prompt_text(stage: str) -> str:
    title = stage.replace("_", " ").title()
    return (
        f"# {title} Stage Prompt\n\n"
        "This prompt was bootstrapped by `autolab init`.\n"
        "Update it with your project-specific instructions for this stage.\n\n"
        "## Hard Guardrails (Read First)\n"
        "- Do not modify experiments already marked completed in `.autolab/backlog.yaml` (including `done`, `completed`, `closed`, `resolved`) unless a human explicitly re-opens them.\n\n"
        "- If the mapped experiment is already `done`, `completed`, `closed`, or `resolved`, do not edit that experiment and wait for an explicit reopen.\n\n"
        "## Repository Path Scope\n"
        "- Required stage artifacts may be under `experiments/<ITERATION_ID>/...` and `.autolab/...` when specified.\n"
        "- Do not restrict analysis or edits to `experiments/` only.\n"
        "- `src/` contains core implementation that should work across multiple experiments or the broader codebase.\n"
        "- `experiments/` can contain experiment-specific implementation to prevent context flooding; move reusable logic to `src/` when multiple experiments need it.\n"
        "- `scripts/` contains useful miscellaneous task utilities.\n"
        "- `autolab/` is a valid target when task scope is orchestration, policy, prompt, or runner behavior.\n"
        "- Keep diffs minimal and avoid unrelated files.\n"
    )


def _resolve_repo_relative_dir(repo_root: Path, raw_dir: str, *, field_name: str) -> Path:
    value = str(raw_dir).strip()
    if not value:
        raise StageCheckError(f"{field_name} must be a non-empty repo-relative path")
    candidate = Path(value)
    if candidate.is_absolute():
        raise StageCheckError(f"{field_name} must be repo-relative, got absolute path '{value}'")
    if any(part == ".." for part in candidate.parts):
        raise StageCheckError(f"{field_name} must not traverse parent directories: '{value}'")

    root = repo_root.resolve()
    resolved = (repo_root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise StageCheckError(f"{field_name} escapes repo root: '{value}'") from exc
    return resolved


def _normalize_workspace_iteration_id(iteration_id: str) -> str:
    normalized = str(iteration_id).strip()
    if not normalized or normalized.startswith("<"):
        raise StageCheckError(
            "state.iteration_id must be set to a real identifier for runner workspace scoping"
        )
    if not ITERATION_ID_SAFE_PATTERN.fullmatch(normalized):
        raise StageCheckError(
            "state.iteration_id must be a single folder name using only [A-Za-z0-9._-] for runner workspace scoping"
        )
    return normalized


def _resolve_runner_workspace(
    repo_root: Path,
    *,
    iteration_id: str,
    ensure_iteration_dir: bool,
) -> Path:
    normalized_iteration_id = _normalize_workspace_iteration_id(iteration_id)
    experiments_root = _resolve_repo_relative_dir(
        repo_root,
        "experiments",
        field_name="runner experiments root",
    )
    workspace_dir = _resolve_repo_relative_dir(
        repo_root,
        f"experiments/{normalized_iteration_id}",
        field_name="agent_runner workspace_dir",
    )
    if workspace_dir.parent != experiments_root:
        raise StageCheckError(
            f"state.iteration_id must resolve to exactly one experiments/<iteration_id> folder, got '{workspace_dir}'"
        )

    if ensure_iteration_dir and not workspace_dir.exists():
        created: list[Path] = []
        _ensure_iteration_skeleton(repo_root, normalized_iteration_id, created)
        _append_log(
            repo_root,
            f"agent runner created iteration workspace {workspace_dir} (created={len(created)})",
        )

    if not workspace_dir.exists():
        raise StageCheckError(f"iteration workspace is missing at {workspace_dir}")
    if not workspace_dir.is_dir():
        raise StageCheckError(f"iteration workspace path is not a directory at {workspace_dir}")
    return workspace_dir


def _resolve_core_add_dirs(
    repo_root: Path,
    *,
    core_dirs: tuple[str, ...],
) -> tuple[Path, ...]:
    root = repo_root.resolve()
    resolved_dirs: list[Path] = []
    for raw_dir in core_dirs:
        resolved = _resolve_repo_relative_dir(
            repo_root,
            raw_dir,
            field_name="agent_runner.edit_scope.core_dirs",
        )
        if resolved == root:
            raise StageCheckError("agent_runner.edit_scope.core_dirs must not include repository root")
        if resolved not in resolved_dirs:
            resolved_dirs.append(resolved)
    return tuple(resolved_dirs)


def _build_core_add_dir_flags(
    repo_root: Path,
    *,
    edit_scope: AgentRunnerEditScopeConfig,
    runner: str = DEFAULT_AGENT_RUNNER_NAME,
) -> tuple[str, tuple[Path, ...]]:
    if edit_scope.mode == "iteration_only":
        return ("", ())

    resolved_dirs = _resolve_core_add_dirs(
        repo_root,
        core_dirs=edit_scope.core_dirs,
    )

    # Claude Code operates from repo root; scope is communicated via env vars + prompt.
    if runner == "claude":
        return ("", resolved_dirs)

    flags = " ".join(f"--add-dir {shlex.quote(str(path))}" for path in resolved_dirs)
    return (flags, resolved_dirs)


def _load_json_if_exists(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


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


def _extract_matching_lines(path: Path, *, keywords: tuple[str, ...], limit: int = 8) -> str:
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


def _extract_log_snippet(repo_root: Path, *, keywords: tuple[str, ...], limit: int = 8) -> str:
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


def _summarize_git_changes_for_prompt(repo_root: Path, *, limit: int = 12) -> tuple[str, list[str]]:
    entries = _collect_git_status_entries(repo_root)
    if not entries:
        return ("clean working tree", [])
    summarized = [f"{status_code.strip() or '??'} {path}" for path, status_code in entries[:limit]]
    summary = f"{len(entries)} changed path(s)"
    if len(entries) > limit:
        summary = f"{summary}; showing first {limit}"
    return (summary, summarized)


def _resolve_hypothesis_id(repo_root: Path, *, iteration_id: str, experiment_id: str) -> str:
    candidate = ""
    if yaml is not None and iteration_id and not iteration_id.startswith("<"):
        design_path = repo_root / "experiments" / iteration_id / "design.yaml"
        if design_path.exists():
            try:
                loaded = yaml.safe_load(design_path.read_text(encoding="utf-8"))
            except Exception:
                loaded = None
            if isinstance(loaded, dict):
                candidate = str(loaded.get("hypothesis_id", "")).strip()
                if candidate and not candidate.startswith("<"):
                    return candidate

    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    if yaml is not None and backlog_path.exists():
        try:
            loaded = yaml.safe_load(backlog_path.read_text(encoding="utf-8"))
        except Exception:
            loaded = None
        if isinstance(loaded, dict):
            experiments = loaded.get("experiments")
            if isinstance(experiments, list):
                for entry in experiments:
                    if not isinstance(entry, dict):
                        continue
                    entry_id = str(entry.get("id", "")).strip()
                    entry_iteration = str(entry.get("iteration_id", "")).strip()
                    if experiment_id and entry_id != experiment_id:
                        continue
                    if iteration_id and entry_iteration and entry_iteration != iteration_id:
                        continue
                    candidate = str(entry.get("hypothesis_id", "")).strip()
                    if candidate and not candidate.startswith("<"):
                        return candidate
            hypotheses = loaded.get("hypotheses")
            if isinstance(hypotheses, list):
                for entry in hypotheses:
                    if not isinstance(entry, dict):
                        continue
                    status = str(entry.get("status", "")).strip().lower()
                    if status in BACKLOG_COMPLETED_STATUSES:
                        continue
                    candidate = str(entry.get("id", "")).strip()
                    if candidate and not candidate.startswith("<"):
                        return candidate
    return "h1"


def _resolve_prompt_run_id(*, stage: str, state: dict[str, Any]) -> str:
    run_id = str(state.get("last_run_id", "")).strip()
    if run_id and not run_id.startswith("<"):
        return run_id
    if stage in {"extract_results", "update_docs"}:
        raise StageCheckError(
            f"prompt token '{{{{run_id}}}}' requires state.last_run_id for stage '{stage}'"
        )
    return "run_pending"


def _compact_json(value: Any, *, max_chars: int = 2000) -> str:
    try:
        rendered = json.dumps(value, indent=2, sort_keys=True)
    except Exception:
        rendered = str(value)
    compact = rendered.strip()
    if len(compact) <= max_chars:
        return compact
    return f"{compact[:max_chars]}..."


def _build_prompt_context(
    repo_root: Path,
    *,
    state: dict[str, Any],
    stage: str,
) -> dict[str, Any]:
    iteration_id = str(state.get("iteration_id", "")).strip()
    experiment_id = str(state.get("experiment_id", "")).strip()
    run_id = _resolve_prompt_run_id(stage=stage, state=state)
    hypothesis_id = _resolve_hypothesis_id(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
    )
    host_mode = _detect_priority_host_mode()
    launch_mode = host_mode

    iteration_dir = repo_root / "experiments" / iteration_id if iteration_id else Path()
    todo_focus_payload = _load_json_if_exists(repo_root / ".autolab" / "todo_focus.json")
    agent_result_payload = _load_json_if_exists(repo_root / ".autolab" / "agent_result.json")
    review_result_payload = _load_json_if_exists(iteration_dir / "review_result.json") if iteration_id else None
    state_excerpt = {
        "stage": str(state.get("stage", "")).strip(),
        "stage_attempt": int(state.get("stage_attempt", 0) or 0),
        "max_stage_attempts": int(state.get("max_stage_attempts", 0) or 0),
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "last_run_id": str(state.get("last_run_id", "")).strip(),
        "sync_status": str(state.get("sync_status", "")).strip(),
        "assistant_mode": str(state.get("assistant_mode", "")).strip(),
        "task_cycle_stage": str(state.get("task_cycle_stage", "")).strip(),
        "current_task_id": str(state.get("current_task_id", "")).strip(),
    }

    review_feedback = (
        _safe_read_text(iteration_dir / "implementation_review.md")
        if iteration_id and iteration_dir.exists()
        else ""
    )
    if not review_feedback:
        review_feedback = "unavailable: no implementation review feedback recorded for this iteration"

    dry_run_output = (
        _extract_matching_lines(
            iteration_dir / "implementation_plan.md",
            keywords=("dry-run", "dry run"),
            limit=8,
        )
        if iteration_id and iteration_dir.exists()
        else ""
    )
    if not dry_run_output:
        dry_run_output = "unavailable: no dry-run excerpt was found in implementation artifacts"

    verifier_outputs_parts: list[str] = []
    if isinstance(review_result_payload, dict):
        required_checks = review_result_payload.get("required_checks")
        if isinstance(required_checks, dict):
            verifier_outputs_parts.append(f"review_result.required_checks={_compact_json(required_checks, max_chars=400)}")
        status = str(review_result_payload.get("status", "")).strip()
        if status:
            verifier_outputs_parts.append(f"review_result.status={status}")
    template_fill_log = _extract_log_snippet(
        repo_root,
        keywords=("template_fill:", "docs_targets:", "result_sanity:", "run_health:", "schema_checks:"),
        limit=8,
    )
    if template_fill_log:
        verifier_outputs_parts.append(template_fill_log)
    verifier_outputs = "\n".join(verifier_outputs_parts).strip()
    if not verifier_outputs:
        verifier_outputs = "unavailable: no verifier output snippets detected in recent artifacts/logs"

    verifier_errors = _extract_log_snippet(
        repo_root,
        keywords=(
            "verification failed",
            "stagecheckerror",
            "run failure at",
            "template_fill: fail",
            "docs_targets: fail",
            "result_sanity: fail",
            "run_health: fail",
            "schema_checks: fail",
        ),
        limit=10,
    )
    if not verifier_errors:
        verifier_errors = "unavailable: no recent verifier error snippets found"

    git_summary, git_paths = _summarize_git_changes_for_prompt(repo_root, limit=12)
    diff_summary = f"{git_summary}\n" + ("\n".join(git_paths) if git_paths else "no changed paths")

    if todo_focus_payload is None:
        todo_focus_payload = {"note": "unavailable: .autolab/todo_focus.json is missing or unreadable"}
    if agent_result_payload is None:
        agent_result_payload = {"note": "unavailable: .autolab/agent_result.json is missing or unreadable"}

    return {
        "generated_at": _utc_now(),
        "stage": stage,
        "host_mode": host_mode,
        "launch_mode": launch_mode,
        "iteration_id": iteration_id,
        "run_id": run_id,
        "hypothesis_id": hypothesis_id,
        "state_snapshot": state_excerpt,
        "todo_focus": todo_focus_payload,
        "agent_result": agent_result_payload,
        "review_feedback": review_feedback,
        "verifier_errors": verifier_errors,
        "verifier_outputs": verifier_outputs,
        "dry_run_output": dry_run_output,
        "diff_summary": diff_summary,
        "git_changed_paths": git_paths,
    }


def _context_token_values(context: dict[str, Any]) -> dict[str, str]:
    def _to_text(value: Any, fallback_label: str) -> str:
        if isinstance(value, str):
            text = value.strip()
            return text if text else f"unavailable: {fallback_label}"
        if value is None:
            return f"unavailable: {fallback_label}"
        compact = _compact_json(value, max_chars=2000)
        return compact if compact else f"unavailable: {fallback_label}"

    return {
        "iteration_id": _to_text(context.get("iteration_id"), "iteration_id"),
        "stage": _to_text(context.get("stage"), "stage"),
        "stage_context": _to_text(context.get("stage_context"), "stage_context"),
        "run_id": _to_text(context.get("run_id"), "run_id"),
        "hypothesis_id": _to_text(context.get("hypothesis_id"), "hypothesis_id"),
        "review_feedback": _to_text(context.get("review_feedback"), "review_feedback"),
        "verifier_errors": _to_text(context.get("verifier_errors"), "verifier_errors"),
        "diff_summary": _to_text(context.get("diff_summary"), "diff_summary"),
        "verifier_outputs": _to_text(context.get("verifier_outputs"), "verifier_outputs"),
        "dry_run_output": _to_text(context.get("dry_run_output"), "dry_run_output"),
        "launch_mode": _to_text(context.get("launch_mode"), "launch_mode"),
    }


def _format_todo_focus_summary(todo_focus_payload: Any) -> str:
    if not isinstance(todo_focus_payload, dict):
        return "none"
    task_id = str(todo_focus_payload.get("task_id", "")).strip()
    title = str(todo_focus_payload.get("title", "")).strip()
    stage = str(todo_focus_payload.get("stage", "")).strip()
    if not any((task_id, title, stage)):
        return "none"
    parts = []
    if task_id:
        parts.append(f"task_id={task_id}")
    if stage:
        parts.append(f"stage={stage}")
    if title:
        parts.append(f"title={title}")
    return ", ".join(parts)


def _build_runtime_stage_context_block(context_payload: dict[str, Any]) -> str:
    state_snapshot = context_payload.get("state_snapshot")
    if not isinstance(state_snapshot, dict):
        state_snapshot = {}

    stage = str(context_payload.get("stage", "")).strip() or "unknown"
    iteration_id = str(context_payload.get("iteration_id", "")).strip() or "unknown"
    host_mode = str(context_payload.get("host_mode", "")).strip() or "unknown"
    stage_attempt = str(state_snapshot.get("stage_attempt", "")).strip() or "-"
    max_stage_attempts = str(state_snapshot.get("max_stage_attempts", "")).strip() or "-"
    assistant_mode = str(state_snapshot.get("assistant_mode", "")).strip() or "off"
    current_task_id = str(state_snapshot.get("current_task_id", "")).strip() or "none"
    last_run_id = str(state_snapshot.get("last_run_id", "")).strip() or "none"
    sync_status = str(state_snapshot.get("sync_status", "")).strip() or "unknown"
    todo_focus_summary = _format_todo_focus_summary(context_payload.get("todo_focus"))

    return (
        "## Runtime Stage Context\n"
        f"- stage: {stage}\n"
        f"- iteration_id: {iteration_id}\n"
        f"- detected_host_mode: {host_mode}\n"
        f"- stage_attempt: {stage_attempt}/{max_stage_attempts}\n"
        f"- assistant_mode: {assistant_mode}\n"
        f"- current_task_id: {current_task_id}\n"
        f"- last_run_id: {last_run_id}\n"
        f"- sync_status: {sync_status}\n"
        f"- todo_focus: {todo_focus_summary}\n"
    )


def _render_stage_prompt(
    repo_root: Path,
    *,
    stage: str,
    state: dict[str, Any],
    template_path: Path,
) -> RenderedPromptBundle:
    try:
        template_text = template_path.read_text(encoding="utf-8")
        template_text = _render_prompt_includes(repo_root, template_text, stage=stage)
    except Exception as exc:
        raise StageCheckError(f"agent runner prompt could not be read at {template_path}: {exc}") from exc

    context_payload = _build_prompt_context(
        repo_root,
        state=state,
        stage=stage,
    )
    context_payload["stage_context"] = _build_runtime_stage_context_block(context_payload)
    token_values = _context_token_values(context_payload)

    tokens_in_template = sorted({match.group(1).strip() for match in PROMPT_TOKEN_PATTERN.finditer(template_text)})
    unsupported_tokens = sorted(token for token in tokens_in_template if token not in token_values)
    if unsupported_tokens:
        _append_log(
            repo_root,
            f"prompt render unsupported tokens stage={stage} template={template_path} tokens={unsupported_tokens}",
        )
        raise StageCheckError(
            f"prompt template has unsupported token(s) for stage '{stage}': {', '.join(unsupported_tokens)}"
        )

    required_tokens = PROMPT_REQUIRED_TOKENS_BY_STAGE.get(stage, {"iteration_id"})
    required_values = {
        "iteration_id": str(context_payload.get("iteration_id", "")).strip(),
        "run_id": str(context_payload.get("run_id", "")).strip(),
    }
    missing_required = sorted(
        token
        for token in tokens_in_template
        if token in required_tokens and not required_values.get(token, "")
    )
    if missing_required:
        _append_log(
            repo_root,
            f"prompt render missing required tokens stage={stage} template={template_path} tokens={missing_required}",
        )
        raise StageCheckError(
            f"prompt template missing required value(s) for stage '{stage}': {', '.join(missing_required)}"
        )

    def _replace_token(match: re.Match[str]) -> str:
        token = match.group(1).strip()
        value = token_values.get(token, "")
        text = str(value).strip()
        if text:
            return text
        return f"unavailable: {token}"

    rendered_text = PROMPT_TOKEN_PATTERN.sub(_replace_token, template_text)
    rendered_text = rendered_text.replace("<ITERATION_ID>", token_values.get("iteration_id", "").strip())
    rendered_text = rendered_text.replace("<RUN_ID>", token_values.get("run_id", "").strip())
    if "{{stage_context}}" not in template_text:
        stage_context_block = token_values.get("stage_context", "").strip()
        if stage_context_block:
            rendered_text = f"{rendered_text.rstrip()}\n\n{stage_context_block}\n"

    unresolved_tokens = sorted({match.group(1).strip() for match in PROMPT_TOKEN_PATTERN.finditer(rendered_text)})
    unresolved_literals = [literal for literal in PROMPT_LITERAL_TOKENS if literal in rendered_text]
    if unresolved_tokens or unresolved_literals:
        _append_log(
            repo_root,
            (
                f"prompt render unresolved placeholders stage={stage} template={template_path} "
                f"tokens={unresolved_tokens} literals={unresolved_literals}"
            ),
        )
        unresolved_text = ", ".join([*unresolved_tokens, *unresolved_literals]) or "<unknown>"
        raise StageCheckError(
            f"rendered prompt contains unresolved placeholders for stage '{stage}': {unresolved_text}"
        )

    rendered_dir = repo_root / ".autolab" / "prompts" / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    rendered_path = rendered_dir / f"{stage}.md"
    context_path = rendered_dir / f"{stage}.context.json"
    rendered_path.write_text(rendered_text, encoding="utf-8")

    context_payload = {
        **context_payload,
        "template_path": str(template_path),
        "rendered_prompt_path": str(rendered_path),
        "rendered_context_path": str(context_path),
    }
    _write_json(context_path, context_payload)

    return RenderedPromptBundle(
        template_path=template_path,
        rendered_path=rendered_path,
        context_path=context_path,
        prompt_text=rendered_text,
        context_payload=context_payload,
    )


def _substitute_runner_command(
    template: str,
    *,
    stage: str,
    prompt_path: Path,
    prompt_template_path: Path,
    prompt_context_path: Path,
    iteration_id: str,
    workspace_dir: Path,
    core_add_dirs: str,
) -> str:
    command = str(template)
    replacements = {
        "{stage}": stage,
        "{prompt_path}": str(prompt_path),
        "{prompt_template_path}": str(prompt_template_path),
        "{prompt_context_path}": str(prompt_context_path),
        "{iteration_id}": iteration_id,
        "{workspace_dir}": shlex.quote(str(workspace_dir)),
        "{core_add_dirs}": core_add_dirs,
    }
    for token, value in replacements.items():
        command = command.replace(token, value)
    return command


def _invoke_agent_runner(
    repo_root: Path,
    *,
    state_path: Path,
    stage: str,
    iteration_id: str,
    run_agent_mode: str,
) -> None:
    runner = _load_agent_runner_config(repo_root)
    mode = _resolve_run_agent_mode(run_agent_mode)
    if stage not in runner.stages:
        if mode == "force_on":
            _append_log(repo_root, f"agent runner skipped by stage filter stage={stage}")
        return

    if mode == "force_off":
        return
    if mode == "policy" and not runner.enabled:
        return
    if mode == "force_on" and not runner.enabled:
        _append_log(repo_root, "agent runner forced by --run-agent (policy enabled=false)")
    if mode == "force_on" and not runner.command:
        raise StageCheckError(
            "agent_runner.command is empty; set agent_runner.command in .autolab/verifier_policy.yaml"
        )

    prompt_template_path = _resolve_stage_prompt_path(repo_root, stage)
    if not prompt_template_path.exists():
        raise StageCheckError(
            f"agent runner prompt is missing for stage '{stage}' at {prompt_template_path}"
        )

    try:
        prompt_state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        raise StageCheckError(f"prompt rendering requires valid state at {state_path}: {exc}") from exc
    prompt_bundle = _render_stage_prompt(
        repo_root,
        stage=stage,
        state=prompt_state,
        template_path=prompt_template_path,
    )

    workspace_dir = _resolve_runner_workspace(
        repo_root,
        iteration_id=iteration_id,
        ensure_iteration_dir=runner.edit_scope.ensure_iteration_dir,
    )
    core_add_dirs, resolved_core_dirs = _build_core_add_dir_flags(
        repo_root,
        edit_scope=runner.edit_scope,
        runner=runner.runner,
    )
    command = _substitute_runner_command(
        runner.command,
        stage=stage,
        prompt_path=prompt_bundle.rendered_path,
        prompt_template_path=prompt_bundle.template_path,
        prompt_context_path=prompt_bundle.context_path,
        iteration_id=iteration_id,
        workspace_dir=workspace_dir,
        core_add_dirs=core_add_dirs,
    )

    env = os.environ.copy()
    env["AUTOLAB_STAGE"] = stage
    env["AUTOLAB_ITERATION_ID"] = iteration_id
    env["AUTOLAB_PROMPT_PATH"] = str(prompt_bundle.rendered_path)
    env["AUTOLAB_PROMPT_TEMPLATE_PATH"] = str(prompt_bundle.template_path)
    env["AUTOLAB_PROMPT_CONTEXT_PATH"] = str(prompt_bundle.context_path)
    env["AUTOLAB_STATE_FILE"] = str(state_path)
    env["AUTOLAB_REPO_ROOT"] = str(repo_root)
    env["AUTOLAB_WORKSPACE_DIR"] = str(workspace_dir)
    env["AUTOLAB_CORE_ADD_DIRS"] = ",".join(str(path) for path in resolved_core_dirs)

    timeout: float | None = None if runner.timeout_seconds <= 0 else runner.timeout_seconds
    _append_log(
        repo_root,
        (
            f"agent runner start stage={stage} timeout_seconds={runner.timeout_seconds} "
            f"workspace_dir={workspace_dir} prompt_template={prompt_bundle.template_path} "
            f"prompt_rendered={prompt_bundle.rendered_path} command={command}"
        ),
    )

    process: subprocess.Popen[str] | None = None
    captured_stdout_chunks: list[str] = []
    captured_stderr_chunks: list[str] = []
    captured_stdout_len = [0]
    captured_stderr_len = [0]
    max_capture_chars = 2400

    def _pump_stream(
        stream: Any,
        sink: Any,
        captured_chunks: list[str],
        captured_len: list[int],
    ) -> None:
        if stream is None:
            return
        try:
            for line in iter(stream.readline, ""):
                sink.write(line)
                sink.flush()
                if captured_len[0] < max_capture_chars:
                    room = max_capture_chars - captured_len[0]
                    snippet = line[:room]
                    captured_chunks.append(snippet)
                    captured_len[0] += len(snippet)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    stdout_thread: threading.Thread | None = None
    stderr_thread: threading.Thread | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            shell=True,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            env=env,
        )
        if process.stdin is not None:
            try:
                process.stdin.write(prompt_bundle.prompt_text)
                process.stdin.flush()
            except BrokenPipeError:
                pass
            finally:
                process.stdin.close()

        stdout_thread = threading.Thread(
            target=_pump_stream,
            args=(process.stdout, sys.stdout, captured_stdout_chunks, captured_stdout_len),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_pump_stream,
            args=(process.stderr, sys.stderr, captured_stderr_chunks, captured_stderr_len),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            returncode = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _append_log(repo_root, f"agent runner timeout stage={stage} timeout_seconds={runner.timeout_seconds}")
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return
    except Exception as exc:
        _append_log(repo_root, f"agent runner execution error stage={stage}: {exc}")
        return
    finally:
        if stdout_thread is not None:
            stdout_thread.join(timeout=2)
        if stderr_thread is not None:
            stderr_thread.join(timeout=2)

    captured_stdout = "".join(captured_stdout_chunks).strip()
    captured_stderr = "".join(captured_stderr_chunks).strip()
    if captured_stdout:
        _append_log(repo_root, f"agent runner stdout stage={stage}: {_compact_log_text(captured_stdout)}")
    if captured_stderr:
        _append_log(repo_root, f"agent runner stderr stage={stage}: {_compact_log_text(captured_stderr)}")

    _append_log(repo_root, f"agent runner exit stage={stage} returncode={returncode}")
    if returncode != 0:
        _append_log(repo_root, f"agent runner non-zero exit at stage={stage}; continuing with stage evaluation")


def _acquire_lock(lock_path: Path, *, state_file: Path, command: str, stale_seconds: int) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    lock_payload: dict[str, Any] = {
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": _utc_now(),
        "last_heartbeat_at": _utc_now(),
        "command": command,
        "state_file": str(state_file),
    }
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    stale_replaced = False
    if lock_path.exists():
        existing: dict[str, Any] = {}
        try:
            loaded = json.loads(lock_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except Exception:
            existing = {}
        heartbeat = _parse_utc(str(existing.get("last_heartbeat_at", "")))
        if heartbeat is not None and now - heartbeat <= timedelta(seconds=stale_seconds):
            holder_pid = existing.get("pid", "<unknown>")
            holder_host = existing.get("host", "<unknown>")
            return (
                False,
                f"active lock exists at {lock_path} (pid={holder_pid}, host={holder_host})",
            )
        stale_replaced = True

    _write_json(lock_path, lock_payload)
    if stale_replaced:
        return (True, f"replaced stale lock at {lock_path}")
    return (True, f"lock acquired at {lock_path}")


def _heartbeat_lock(lock_path: Path) -> None:
    if not lock_path.exists():
        return
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    payload["last_heartbeat_at"] = _utc_now()
    _write_json(lock_path, payload)


def _release_lock(lock_path: Path) -> None:
    if not lock_path.exists():
        return
    try:
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if isinstance(payload, dict):
        holder_pid = int(payload.get("pid", -1)) if str(payload.get("pid", "")).isdigit() else -1
        if holder_pid not in {-1, os.getpid()}:
            return
    lock_path.unlink(missing_ok=True)


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
            lines.append(
                "| {i} | {before} | {after} | {transitioned} | {exit} | {decision} | {message} |".format(
                    i=row.get("index", ""),
                    before=str(row.get("stage_before", "")).replace("|", "/"),
                    after=str(row.get("stage_after", "")).replace("|", "/"),
                    transitioned=row.get("transitioned", ""),
                    exit=row.get("exit_code", ""),
                    decision=str(row.get("decision", "-")).replace("|", "/"),
                    message=str(row.get("message", "")).replace("|", "/"),
                )
            )
    else:
        lines.append("No iterations were executed.")

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return summary_path


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
    }


def _default_agent_result() -> dict[str, Any]:
    return {
        "status": "complete",
        "summary": "autolab bootstrap initialized",
        "changed_files": [],
        "completion_token_seen": True,
    }


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


def _normalize_space(value: Any) -> str:
    return str(value).strip()


def _normalize_backlog_status(value: Any) -> str:
    return _normalize_space(str(value)).lower()


def _is_backlog_status_completed(value: Any) -> bool:
    return _normalize_backlog_status(value) in BACKLOG_COMPLETED_STATUSES


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
    status = _normalize_backlog_status(entry.get("status"))
    if _is_backlog_status_completed(status):
        return (
            True,
            f"backlog experiment '{experiment_label}' is marked '{status}'",
        )
    return (
        False,
        f"backlog experiment '{experiment_label}' status is '{status or 'open'}'",
    )


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
        return (False, None, "state.experiment_id is unset")

    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    payload, load_error = _load_backlog_yaml(backlog_path)
    if payload is None:
        return (False, None, load_error)

    experiments = payload.get("experiments")
    if not isinstance(experiments, list):
        return (False, None, "backlog experiments list is missing")

    matches: list[dict[str, Any]] = []
    for entry in experiments:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("id", "")).strip() == normalized_experiment_id:
            matches.append(entry)

    if not matches:
        return (False, None, f"backlog experiment '{normalized_experiment_id}' was not found")
    if len(matches) > 1:
        return (False, None, f"backlog experiment id '{normalized_experiment_id}' is duplicated")

    target = matches[0]
    current_status = str(target.get("status", "")).strip().lower()
    if current_status == "completed":
        return (False, None, f"backlog experiment '{normalized_experiment_id}' is already completed")

    target["status"] = "completed"
    written, write_error = _write_backlog_yaml(backlog_path, payload)
    if write_error:
        return (False, None, write_error)
    if not written:
        return (False, None, f"no backlog changes were written for experiment '{normalized_experiment_id}'")
    return (True, backlog_path, f"marked backlog experiment '{normalized_experiment_id}' as completed")


def _ensure_iteration_skeleton(repo_root: Path, iteration_id: str, created: list[Path]) -> None:
    iteration_dir = repo_root / "experiments" / iteration_id
    _ensure_text_file(
        iteration_dir / "hypothesis.md",
        "# Hypothesis\n\n- metric: primary_metric\n- target_delta: 0.0\n",
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
            "  args: {}\n"
            "compute:\n"
            '  location: "local"\n'
            "metrics:\n"
            '  primary: "primary_metric"\n'
            "baselines:\n"
            '  - name: "baseline_current"\n'
            "    config_overrides: {}\n"
        ),
        created,
    )
    _ensure_text_file(
        iteration_dir / "implementation_plan.md",
        "# Implementation Plan\n\n- Implement the design requirements.\n",
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


def _require_non_empty(path: Path, label: str) -> None:
    if not path.exists():
        raise StageCheckError(f"{label} is missing at {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise StageCheckError(f"{label} is empty at {path}")


def _load_dict_json(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise StageCheckError(f"{label} is missing at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StageCheckError(f"{label} is not valid JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StageCheckError(f"{label} must contain a JSON object at {path}")
    return payload


def _validate_design(path: Path, iteration_id: str) -> None:
    if yaml is None:
        raise StageCheckError("design validation requires PyYAML")
    if not path.exists():
        raise StageCheckError(f"design.yaml is missing at {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageCheckError(f"design.yaml is not valid YAML at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StageCheckError("design.yaml must contain a mapping")

    required = {"id", "iteration_id", "hypothesis_id", "entrypoint", "compute", "metrics", "baselines"}
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise StageCheckError(f"design.yaml missing required keys: {missing}")

    if str(payload.get("iteration_id", "")).strip() != iteration_id:
        raise StageCheckError("design.yaml iteration_id does not match state.iteration_id")

    entrypoint = payload.get("entrypoint")
    if not isinstance(entrypoint, dict) or not str(entrypoint.get("module", "")).strip():
        raise StageCheckError("design.yaml entrypoint.module must be set")

    compute = payload.get("compute")
    if not isinstance(compute, dict) or not str(compute.get("location", "")).strip():
        raise StageCheckError("design.yaml compute.location must be set")

    baselines = payload.get("baselines")
    if not isinstance(baselines, list) or not baselines:
        raise StageCheckError("design.yaml baselines must be a non-empty list")


def _validate_review_result(path: Path) -> str:
    payload = _load_dict_json(path, "review_result.json")
    required = {"status", "blocking_findings", "required_checks", "reviewed_at"}
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise StageCheckError(f"review_result.json missing required keys: {missing}")

    status = str(payload.get("status", "")).strip()
    if status not in {"pass", "needs_retry", "failed"}:
        raise StageCheckError(f"review_result.json has invalid status '{status}'")
    return status


def _validate_launch(iteration_dir: Path) -> None:
    local_script = iteration_dir / "launch" / "run_local.sh"
    slurm_script = iteration_dir / "launch" / "run_slurm.sbatch"
    if not local_script.exists() and not slurm_script.exists():
        raise StageCheckError("launch requires run_local.sh or run_slurm.sbatch")
    if local_script.exists():
        _require_non_empty(local_script, "launch/run_local.sh")
    if slurm_script.exists():
        _require_non_empty(slurm_script, "launch/run_slurm.sbatch")


def _validate_extract(iteration_dir: Path, run_id: str) -> None:
    if not run_id or run_id.startswith("<"):
        raise StageCheckError("state.last_run_id must be set for extract_results")
    run_dir = iteration_dir / "runs" / run_id
    manifest = run_dir / "run_manifest.json"
    metrics = run_dir / "metrics.json"
    _require_non_empty(manifest, "runs/<run_id>/run_manifest.json")
    payload = _load_dict_json(metrics, "runs/<run_id>/metrics.json")
    if not payload:
        raise StageCheckError("runs/<run_id>/metrics.json must not be empty")


def _validate_update_docs(repo_root: Path, iteration_dir: Path, run_id: str) -> None:
    _require_non_empty(iteration_dir / "docs_update.md", "docs_update.md")
    _require_non_empty(iteration_dir / "analysis" / "summary.md", "analysis/summary.md")
    normalized_run_id = str(run_id).strip()
    if not normalized_run_id or normalized_run_id.startswith("<"):
        return
    manifest_path = iteration_dir / "runs" / normalized_run_id / "run_manifest.json"
    manifest_payload = _load_dict_json(manifest_path, "runs/<run_id>/run_manifest.json")
    _validate_slurm_job_ledger_entry(
        repo_root,
        manifest_path=manifest_path,
        payload=manifest_payload,
        stage="update_docs",
    )


def _evaluate_stage(repo_root: Path, state: dict[str, Any]) -> tuple[str, str, str]:
    stage = state["stage"]
    iteration_id = state["iteration_id"]
    iteration_dir = repo_root / "experiments" / iteration_id
    if not iteration_dir.exists():
        raise StageCheckError(f"iteration workspace is missing at {iteration_dir}")

    if stage == "hypothesis":
        _require_non_empty(iteration_dir / "hypothesis.md", "hypothesis.md")
        return ("design", "complete", "hypothesis checks passed")
    if stage == "design":
        _validate_design(iteration_dir / "design.yaml", iteration_id)
        return ("implementation", "complete", "design checks passed")
    if stage == "implementation":
        _require_non_empty(iteration_dir / "implementation_plan.md", "implementation_plan.md")
        return ("implementation_review", "complete", "implementation checks passed")
    if stage == "implementation_review":
        _require_non_empty(iteration_dir / "implementation_review.md", "implementation_review.md")
        review_status = _validate_review_result(iteration_dir / "review_result.json")
        if review_status == "pass":
            return ("launch", "complete", "implementation review passed")
        if review_status == "needs_retry":
            return ("implementation", "complete", "implementation review requested retry")
        return ("human_review", "failed", "implementation review failed")
    if stage == "launch":
        _validate_launch(iteration_dir)
        run_id, sync_status = _resolve_latest_run_state(iteration_dir)
        if run_id:
            state["last_run_id"] = run_id
            manifest_path = iteration_dir / "runs" / run_id / "run_manifest.json"
            manifest_payload = _load_dict_json(manifest_path, "runs/<run_id>/run_manifest.json")
            _validate_slurm_job_ledger_entry(
                repo_root,
                manifest_path=manifest_path,
                payload=manifest_payload,
                stage="launch",
            )
        state["sync_status"] = sync_status
        return ("extract_results", "complete", "launch checks passed")
    if stage == "extract_results":
        _validate_extract(iteration_dir, state["last_run_id"])
        return ("update_docs", "complete", "extract-results checks passed")
    if stage == "update_docs":
        _validate_update_docs(repo_root, iteration_dir, str(state.get("last_run_id", "")))
        return ("decide_repeat", "complete", "update-docs checks passed")
    raise StageCheckError(f"unsupported stage '{stage}'")


def _handle_stage_failure(
    repo_root: Path,
    *,
    state_path: Path,
    state: dict[str, Any],
    stage_before: str,
    pre_sync_changed: list[Path],
    detail: str,
) -> RunOutcome:
    state["stage_attempt"] = int(state["stage_attempt"]) + 1
    exhausted = state["stage_attempt"] >= int(state["max_stage_attempts"])
    if exhausted:
        state["stage"] = "human_review"
        agent_status = "failed"
        message = f"{detail}; retry budget exhausted, escalating to human_review"
    else:
        agent_status = "needs_retry"
        message = (
            f"{detail}; retrying stage {stage_before} "
            f"({state['stage_attempt']}/{state['max_stage_attempts']})"
        )
    _write_json(state_path, state)
    changed = [state_path]
    outcome = RunOutcome(
        exit_code=1,
        transitioned=state["stage"] != stage_before,
        stage_before=stage_before,
        stage_after=state["stage"],
        message=message,
    )
    post_sync_changed, post_sync_message = _safe_todo_post_sync(
        repo_root,
        state,
        run_outcome=_outcome_payload(outcome),
    )
    summary_with_todo = _append_todo_message(message, post_sync_message)
    _persist_agent_result(
        repo_root,
        status=agent_status,
        summary=summary_with_todo,
        changed_files=[*changed, *pre_sync_changed, *post_sync_changed],
    )
    _append_log(repo_root, f"run failure at {stage_before}: {message}")
    return RunOutcome(
        exit_code=outcome.exit_code,
        transitioned=outcome.transitioned,
        stage_before=outcome.stage_before,
        stage_after=outcome.stage_after,
        message=summary_with_todo,
    )


def _run_once_standard(
    state_path: Path,
    decision: str | None,
    *,
    run_agent_mode: str = "policy",
    auto_decision: bool = False,
    auto_mode: bool = False,
    commit_task_id: str = "",
    commit_cycle_stage: str = "",
    strict_implementation_progress: bool = True,
) -> RunOutcome:
    repo_root = _resolve_repo_root(state_path)
    pre_sync_changed: list[Path] = []
    state_bootstrap_changed: list[Path] = []
    detected_host_mode: str | None = None
    experiment_id_autofill_reason = ""
    try:
        raw_state = _load_state(state_path)
        state = _normalize_state(raw_state)
    except StateError as exc:
        message = f"invalid state: {exc}"
        pre_sync_changed, _ = _safe_todo_pre_sync(repo_root, None)
        post_sync_changed, post_sync_message = _safe_todo_post_sync(repo_root, None, run_outcome=None)
        summary = _append_todo_message(message, post_sync_message)
        _append_log(repo_root, f"run error: {message}")
        try:
            _persist_agent_result(
                repo_root,
                status="failed",
                summary=summary,
                changed_files=[*pre_sync_changed, *post_sync_changed],
            )
        except Exception:
            pass
        return RunOutcome(
            exit_code=1,
            transitioned=False,
            stage_before="<unknown>",
            stage_after="<unknown>",
            message=summary,
        )

    if not str(state.get("experiment_id", "")).strip():
        inferred_experiment_id, infer_reason = _infer_unique_experiment_id_from_backlog(
            repo_root,
            str(state.get("iteration_id", "")).strip(),
        )
        if inferred_experiment_id:
            state["experiment_id"] = inferred_experiment_id
            _write_json(state_path, state)
            state_bootstrap_changed.append(state_path)
            _append_log(repo_root, f"state.experiment_id auto-filled from backlog: {inferred_experiment_id}")
        else:
            experiment_id_autofill_reason = infer_reason

    detected_host_mode = _detect_priority_host_mode()
    active_completed, completion_summary = _is_active_experiment_completed(
        repo_root,
        state,
    )
    if active_completed and state["stage"] not in TERMINAL_STAGES:
        original_stage = state["stage"]
        state["stage"] = "stop"
        state["stage_attempt"] = 0
        state["current_task_id"] = ""
        state["task_cycle_stage"] = "done"
        state["task_change_baseline"] = {}
        _write_json(state_path, state)
        state_bootstrap_changed.append(state_path)
        pre_sync_changed, _ = _safe_todo_pre_sync(repo_root, state, host_mode=detected_host_mode)
        if state_bootstrap_changed:
            pre_sync_changed = [*state_bootstrap_changed, *pre_sync_changed]
        message = f"blocked completed experiment edits: {completion_summary}; re-open experiment in backlog to resume"
        outcome = RunOutcome(
            exit_code=0,
            transitioned=True,
            stage_before=original_stage,
            stage_after="stop",
            message=message,
        )
        post_sync_changed, post_sync_message = _safe_todo_post_sync(
            repo_root,
            state,
            run_outcome=_outcome_payload(outcome),
        )
        summary = _append_todo_message(message, post_sync_message)
        _persist_agent_result(
            repo_root,
            status="complete",
            summary=summary,
            changed_files=[*pre_sync_changed, *post_sync_changed],
        )
        _append_log(repo_root, f"run blocked completed experiment at stage {original_stage}")
        return RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=summary,
        )

    pre_sync_changed, _ = _safe_todo_pre_sync(repo_root, state, host_mode=detected_host_mode)
    if state_bootstrap_changed:
        pre_sync_changed = [*state_bootstrap_changed, *pre_sync_changed]
    standard_baseline_snapshot = _collect_change_snapshot(repo_root)

    stage_before = state["stage"]
    if stage_before in TERMINAL_STAGES:
        message = f"stage '{stage_before}' is terminal; nothing to do"
        outcome = RunOutcome(
            exit_code=0,
            transitioned=False,
            stage_before=stage_before,
            stage_after=stage_before,
            message=message,
        )
        post_sync_changed, post_sync_message = _safe_todo_post_sync(
            repo_root,
            state,
            run_outcome=_outcome_payload(outcome),
        )
        summary = _append_todo_message(message, post_sync_message)
        _persist_agent_result(
            repo_root,
            status="complete",
            summary=summary,
            changed_files=[*pre_sync_changed, *post_sync_changed],
        )
        _append_log(repo_root, f"run no-op at terminal stage {stage_before}")
        return RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=summary,
        )

    if stage_before == "decide_repeat":
        selected_decision = decision
        if selected_decision is None and auto_decision:
            selected_decision = select_decision_from_todo(
                repo_root,
                prioritize_implementation=(detected_host_mode == "local"),
            )
        if selected_decision is None and auto_decision and auto_mode:
            selected_decision = "stop"
        auto_selected = decision is None and selected_decision is not None

        if selected_decision is None:
            message = "stage 'decide_repeat' requires --decision (or --auto-decision) to transition"
            outcome = RunOutcome(
                exit_code=0,
                transitioned=False,
                stage_before=stage_before,
                stage_after=stage_before,
                message=message,
            )
            post_sync_changed, post_sync_message = _safe_todo_post_sync(
                repo_root,
                state,
                run_outcome=_outcome_payload(outcome),
            )
            summary = _append_todo_message(message, post_sync_message)
            _persist_agent_result(
                repo_root,
                status="complete",
                summary=summary,
                changed_files=[*pre_sync_changed, *post_sync_changed],
            )
            _append_log(repo_root, "run paused at decide_repeat (no decision)")
            return RunOutcome(
                exit_code=outcome.exit_code,
                transitioned=outcome.transitioned,
                stage_before=outcome.stage_before,
                stage_after=outcome.stage_after,
                message=summary,
            )

        guardrails = _load_guardrail_config(repo_root)
        repeat_guard = state.get("repeat_guard", {})
        open_count = _todo_open_count(repo_root)
        last_decision = str(repeat_guard.get("last_decision", ""))
        same_decision_streak = int(repeat_guard.get("same_decision_streak", 0))
        no_progress_decisions = int(repeat_guard.get("no_progress_decisions", 0))
        last_open_task_count = int(repeat_guard.get("last_open_task_count", -1))

        if auto_mode:
            if selected_decision == last_decision:
                same_decision_streak += 1
            else:
                same_decision_streak = 1
            if last_open_task_count >= 0 and open_count >= last_open_task_count:
                no_progress_decisions += 1
            else:
                no_progress_decisions = 0
            if (
                same_decision_streak > guardrails.max_same_decision_streak
                or no_progress_decisions >= guardrails.max_no_progress_decisions
            ):
                selected_decision = guardrails.on_breach
                same_decision_streak = 0
                no_progress_decisions = 0

        repeat_guard["last_decision"] = selected_decision
        repeat_guard["same_decision_streak"] = same_decision_streak
        repeat_guard["last_open_task_count"] = open_count
        repeat_guard["no_progress_decisions"] = no_progress_decisions
        if selected_decision not in TERMINAL_STAGES:
            repeat_guard["update_docs_cycle_count"] = 0
        state["repeat_guard"] = repeat_guard
        state["stage"] = selected_decision
        state["stage_attempt"] = 0
        _write_json(state_path, state)
        message = f"decision applied: decide_repeat -> {selected_decision}"
        if auto_selected:
            message = f"{message} (auto-selected from docs/todo.md)"
        changed = [state_path]
        if selected_decision == "stop":
            completed, backlog_path, completion_summary = _mark_backlog_experiment_completed(
                repo_root,
                str(state.get("experiment_id", "")).strip(),
            )
            if completed and backlog_path is not None:
                changed.append(backlog_path)
                _append_log(repo_root, completion_summary)
            else:
                if (
                    not str(state.get("experiment_id", "")).strip()
                    and experiment_id_autofill_reason
                ):
                    completion_summary = (
                        f"state.experiment_id is unset ({experiment_id_autofill_reason})"
                    )
                completion_summary = f"backlog completion skipped: {completion_summary}"
                _append_log(repo_root, completion_summary)
            message = f"{message}; {completion_summary}"
        outcome = RunOutcome(
            exit_code=0,
            transitioned=True,
            stage_before=stage_before,
            stage_after=selected_decision,
            message=message,
        )
        post_sync_changed, post_sync_message = _safe_todo_post_sync(
            repo_root,
            state,
            run_outcome=_outcome_payload(outcome),
        )
        summary = _append_todo_message(message, post_sync_message)
        _persist_agent_result(
            repo_root,
            status="complete",
            summary=summary,
            changed_files=[*changed, *pre_sync_changed, *post_sync_changed],
        )
        _append_log(repo_root, f"run transition {stage_before} -> {selected_decision}")
        return RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=summary,
            commit_task_id=commit_task_id,
            commit_cycle_stage=commit_cycle_stage,
        )

    if _resolve_run_agent_mode(run_agent_mode) != "force_off":
        open_todo_count = _todo_open_count(repo_root)
        if open_todo_count > 0 and not _has_open_stage_todo_task(repo_root, stage_before):
            _append_log(repo_root, f"agent runner skipped stage={stage_before} (no stage-focused todo tasks)")
        else:
            try:
                _invoke_agent_runner(
                    repo_root,
                    state_path=state_path,
                    stage=stage_before,
                    iteration_id=str(state["iteration_id"]),
                    run_agent_mode=run_agent_mode,
                )
            except StageCheckError as exc:
                return _handle_stage_failure(
                    repo_root,
                    state_path=state_path,
                    state=state,
                    stage_before=stage_before,
                    pre_sync_changed=pre_sync_changed,
                    detail=f"agent runner error: {exc}",
                )

    try:
        next_stage, agent_status, summary = _evaluate_stage(repo_root, state)
    except StageCheckError as exc:
        return _handle_stage_failure(
            repo_root,
            state_path=state_path,
            state=state,
            stage_before=stage_before,
            pre_sync_changed=pre_sync_changed,
            detail=str(exc),
        )

    if (
        strict_implementation_progress
        and stage_before == "implementation"
        and next_stage == "implementation_review"
    ):
        meaningful_config = _load_meaningful_change_config(repo_root)
        if not meaningful_config.require_implementation_progress:
            _append_log(
                repo_root,
                "implementation progress check skipped: require_implementation_progress=false",
            )
        else:
            non_git_required = bool(
                meaningful_config.require_git_for_progress and not _is_git_worktree(repo_root)
            )
            if non_git_required:
                if meaningful_config.on_non_git_behavior == "fail":
                    _append_log(
                        repo_root,
                        "implementation progress check failed: git worktree required but unavailable",
                    )
                    return _handle_stage_failure(
                        repo_root,
                        state_path=state_path,
                        state=state,
                        stage_before=stage_before,
                        pre_sync_changed=pre_sync_changed,
                        detail=(
                            "implementation progress check requires a git worktree; "
                            "set meaningful_change.require_git_for_progress=false to continue"
                        ),
                    )
                skip_message = (
                    "implementation progress check skipped: repository is not a git worktree; "
                    "continuing under policy"
                )
                _append_log(repo_root, skip_message)
                summary = f"{summary}; {skip_message}"
            else:
                implementation_progress, delta_paths, meaningful_paths, _current_snapshot = _evaluate_meaningful_change(
                    repo_root,
                    meaningful_config,
                    baseline_snapshot=standard_baseline_snapshot,
                )
                if not implementation_progress:
                    detail = (
                        "implementation produced no meaningful target changes beyond excluded paths "
                        f"({_meaningful_progress_detail(changed_paths=delta_paths, meaningful_paths=meaningful_paths)})"
                    )
                    _append_log(repo_root, f"implementation progress check failed: {detail}")
                    return _handle_stage_failure(
                        repo_root,
                        state_path=state_path,
                        state=state,
                        stage_before=stage_before,
                        pre_sync_changed=pre_sync_changed,
                        detail=detail,
                    )

    guardrail_stage_override = False
    if stage_before == "extract_results" and next_stage == "update_docs":
        guardrails = _load_guardrail_config(repo_root)
        repeat_guard = state.get("repeat_guard", {})
        if not isinstance(repeat_guard, dict):
            repeat_guard = {}
        update_docs_cycle_count = int(repeat_guard.get("update_docs_cycle_count", 0)) + 1
        repeat_guard["update_docs_cycle_count"] = update_docs_cycle_count
        state["repeat_guard"] = repeat_guard
        if update_docs_cycle_count > int(guardrails.max_update_docs_cycles):
            guardrail_stage_override = True
            state["stage"] = guardrails.on_breach
            state["stage_attempt"] = 0
            agent_status = "failed" if guardrails.on_breach == "human_review" else "complete"
            summary = (
                f"update_docs cycle limit exceeded ({update_docs_cycle_count}/{guardrails.max_update_docs_cycles}), "
                f"escalating to {guardrails.on_breach}"
            )

    if not guardrail_stage_override:
        state["stage"] = next_stage
        prior_attempt = int(state["stage_attempt"])
        max_stage_attempts = int(state["max_stage_attempts"])
        retry_cycle_increment = (
            stage_before == "implementation_review"
            and next_stage == "implementation"
            and summary == "implementation review requested retry"
        )
        retry_cycle_carry = (
            stage_before == "implementation"
            and next_stage == "implementation_review"
            and prior_attempt > 0
        )

        if retry_cycle_increment:
            state["stage_attempt"] = prior_attempt + 1
            if state["stage_attempt"] >= max_stage_attempts:
                state["stage"] = "human_review"
                agent_status = "failed"
                summary = (
                    f"implementation review retry budget exhausted "
                    f"({state['stage_attempt']}/{max_stage_attempts}), escalating to human_review"
                )
        elif retry_cycle_carry:
            state["stage_attempt"] = prior_attempt
        else:
            state["stage_attempt"] = 0

    _write_json(state_path, state)
    changed = [state_path]
    exit_code = 1 if agent_status == "failed" else 0
    stage_after = str(state["stage"])
    outcome = RunOutcome(
        exit_code=exit_code,
        transitioned=stage_after != stage_before,
        stage_before=stage_before,
        stage_after=stage_after,
        message=summary,
    )
    post_sync_changed, post_sync_message = _safe_todo_post_sync(
        repo_root,
        state,
        run_outcome=_outcome_payload(outcome),
    )
    summary_with_todo = _append_todo_message(summary, post_sync_message)
    _persist_agent_result(
        repo_root,
        status=agent_status,
        summary=summary_with_todo,
        changed_files=[*changed, *pre_sync_changed, *post_sync_changed],
    )
    _append_log(repo_root, f"run transition {stage_before} -> {stage_after} ({agent_status})")

    return RunOutcome(
        exit_code=outcome.exit_code,
        transitioned=outcome.transitioned,
        stage_before=outcome.stage_before,
        stage_after=outcome.stage_after,
        message=summary_with_todo,
        commit_task_id=commit_task_id,
        commit_cycle_stage=commit_cycle_stage,
    )


def _assistant_target_stage(task: dict[str, Any]) -> str:
    stage = str(task.get("stage", "")).strip()
    task_class = str(task.get("task_class", "unknown")).strip().lower()
    if stage in ACTIVE_STAGES:
        return stage
    if task_class == "docs":
        return "update_docs"
    if task_class == "experiment":
        return "design"
    return "implementation"


def _run_once_assistant(
    state_path: Path,
    *,
    run_agent_mode: str = "policy",
    auto_mode: bool = False,
) -> RunOutcome:
    repo_root = _resolve_repo_root(state_path)
    pre_sync_changed: list[Path] = []
    detected_host_mode = _detect_priority_host_mode()
    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        return _run_once_standard(
            state_path,
            None,
            run_agent_mode=run_agent_mode,
            auto_decision=False,
            auto_mode=auto_mode,
            strict_implementation_progress=False,
        )

    state["assistant_mode"] = "on"
    current_stage = str(state.get("stage", ""))
    completed_experiment, completion_summary = _is_active_experiment_completed(
        repo_root,
        state,
    )
    if completed_experiment and current_stage not in TERMINAL_STAGES:
        state["stage"] = "stop"
        state["current_task_id"] = ""
        state["task_cycle_stage"] = "done"
        state["task_change_baseline"] = {}
        state["stage_attempt"] = 0
        _write_json(state_path, state)
        pre_sync_changed, _ = _safe_todo_pre_sync(repo_root, state, host_mode=detected_host_mode)
        message = f"blocked completed experiment edits: {completion_summary}; re-open experiment in backlog to resume"
        outcome = RunOutcome(
            exit_code=0,
            transitioned=True,
            stage_before=current_stage,
            stage_after="stop",
            message=message,
        )
        post_sync_changed, post_sync_message = _safe_todo_post_sync(
            repo_root,
            state,
            run_outcome=_outcome_payload(outcome),
        )
        summary = _append_todo_message(message, post_sync_message)
        _persist_agent_result(
            repo_root,
            status="complete",
            summary=summary,
            changed_files=[state_path, *pre_sync_changed, *post_sync_changed],
        )
        _append_log(repo_root, f"assistant blocked completed experiment from stage {current_stage}")
        return RunOutcome(
            exit_code=0,
            transitioned=True,
            stage_before=current_stage,
            stage_after="stop",
            message=summary,
            commit_task_id="",
            commit_cycle_stage="done",
        )

    pre_sync_changed, _ = _safe_todo_pre_sync(repo_root, state, host_mode=detected_host_mode)
    stage_before = str(state.get("stage", ""))
    current_task_id = str(state.get("current_task_id", ""))
    cycle_stage = str(state.get("task_cycle_stage", "select"))
    force_task_selection = stage_before == "human_review"
    baseline_snapshot_raw = state.get("task_change_baseline", {})
    baseline_snapshot = baseline_snapshot_raw if isinstance(baseline_snapshot_raw, dict) else {}

    def _persist_simple(
        *,
        status: str,
        message: str,
        changed_files: list[Path],
        transitioned: bool,
        stage_after: str,
        exit_code: int = 0,
        commit_allowed: bool = False,
        commit_cycle_stage: str = "",
        commit_paths: tuple[str, ...] = (),
    ) -> RunOutcome:
        post_sync_changed, post_sync_message = _safe_todo_post_sync(
            repo_root,
            state,
            run_outcome={
                "exit_code": exit_code,
                "transitioned": transitioned,
                "stage_before": stage_before,
                "stage_after": stage_after,
            },
        )
        summary = _append_todo_message(message, post_sync_message)
        _persist_agent_result(
            repo_root,
            status=status,
            summary=summary,
            changed_files=[*changed_files, *pre_sync_changed, *post_sync_changed],
        )
        return RunOutcome(
            exit_code=exit_code,
            transitioned=transitioned,
            stage_before=stage_before,
            stage_after=stage_after,
            message=summary,
            commit_allowed=commit_allowed,
            commit_task_id=current_task_id,
            commit_cycle_stage=commit_cycle_stage,
            commit_paths=commit_paths,
        )

    if force_task_selection or cycle_stage in {"select", "done"} or not current_task_id:
        task = select_open_task(
            repo_root,
            prioritize_implementation=(detected_host_mode == "local"),
        )
        if task is None:
            state["current_task_id"] = ""
            state["task_cycle_stage"] = "done"
            state["task_change_baseline"] = {}
            state["stage"] = "stop"
            state["stage_attempt"] = 0
            _write_json(state_path, state)
            changed: list[Path] = [state_path]
            completion_msg = ""
            completed, backlog_path, completion_summary = _mark_backlog_experiment_completed(
                repo_root,
                str(state.get("experiment_id", "")).strip(),
            )
            if completed and backlog_path is not None:
                changed.append(backlog_path)
                completion_msg = f"; {completion_summary}"
            _append_log(repo_root, completion_summary)
            return _persist_simple(
                status="complete",
                message=f"assistant cycle complete: no actionable tasks remain{completion_msg}",
                changed_files=changed,
                transitioned=stage_before != "stop",
                stage_after="stop",
                commit_allowed=False,
                commit_cycle_stage="done",
            )

        current_task_id = str(task.get("task_id", "")).strip()
        state["current_task_id"] = current_task_id
        state["task_cycle_stage"] = "implement"
        state["task_change_baseline"] = _collect_change_snapshot(repo_root)
        target_stage = _assistant_target_stage(task)
        state["stage"] = target_stage
        state["stage_attempt"] = 0
        _write_json(state_path, state)
        return _persist_simple(
            status="complete",
            message=f"assistant selected task {current_task_id} ({task.get('task_class', 'unknown')}) -> {target_stage}",
            changed_files=[state_path],
            transitioned=target_stage != stage_before,
            stage_after=target_stage,
            commit_allowed=False,
            commit_cycle_stage="select",
        )

    if cycle_stage == "verify":
        verified, verify_message = _run_verification_step(repo_root, state)
        repeat_guard = dict(state.get("repeat_guard", {}))
        repeat_guard["last_verification_passed"] = verified
        state["repeat_guard"] = repeat_guard
        state["task_cycle_stage"] = "review" if verified else "implement"
        _write_json(state_path, state)
        return _persist_simple(
            status="complete" if verified else "needs_retry",
            message=f"assistant verification: {verify_message}",
            changed_files=[state_path],
            transitioned=False,
            stage_after=str(state.get("stage", stage_before)),
            exit_code=0,
            commit_allowed=False,
            commit_cycle_stage="verify",
        )

    if cycle_stage == "review":
        meaningful_config = _load_meaningful_change_config(repo_root)
        meaningful, changed_paths, meaningful_paths, _current_snapshot = _evaluate_meaningful_change(
            repo_root,
            meaningful_config,
            baseline_snapshot=baseline_snapshot,
        )
        repeat_guard = dict(state.get("repeat_guard", {}))
        verification_passed = bool(repeat_guard.get("last_verification_passed", False))
        passes_gate = meaningful and (not meaningful_config.require_verification or verification_passed)

        if passes_gate:
            mark_task_completed(repo_root, current_task_id)
            state["current_task_id"] = ""
            state["task_cycle_stage"] = "done"
            state["task_change_baseline"] = {}
            repeat_guard["no_progress_decisions"] = 0
            state["repeat_guard"] = repeat_guard
            scoped_commit_paths = _assistant_commit_paths(changed_paths, meaningful_paths)
            _write_json(state_path, state)
            return _persist_simple(
                status="complete",
                message=(
                    "assistant review passed meaningful-change gate: "
                    f"{len(meaningful_paths)} meaningful file(s) changed"
                ),
                changed_files=[state_path],
                transitioned=False,
                stage_after=str(state.get("stage", stage_before)),
                commit_allowed=True,
                commit_cycle_stage="review",
                commit_paths=scoped_commit_paths,
            )

        repeat_guard["no_progress_decisions"] = int(repeat_guard.get("no_progress_decisions", 0)) + 1
        state["repeat_guard"] = repeat_guard
        guardrails = _load_guardrail_config(repo_root)
        if auto_mode and int(repeat_guard["no_progress_decisions"]) >= int(guardrails.max_no_progress_decisions):
            state["task_cycle_stage"] = "done"
            state["current_task_id"] = ""
            state["task_change_baseline"] = {}
            state["stage"] = guardrails.on_breach
            _write_json(state_path, state)
            return _persist_simple(
                status="failed",
                message="assistant review guardrail breach: escalating to human_review",
                changed_files=[state_path],
                transitioned=stage_before != guardrails.on_breach,
                stage_after=guardrails.on_breach,
                exit_code=1,
                commit_allowed=False,
                commit_cycle_stage="review",
            )

        state["task_cycle_stage"] = "implement"
        _write_json(state_path, state)
        missing_verification = meaningful_config.require_verification and not verification_passed
        details: list[str] = []
        if not meaningful:
            details.append("no meaningful code/config/docs targets changed")
        if missing_verification:
            details.append("verification not passed")
        if not details:
            details.append("gate did not pass")
        return _persist_simple(
            status="needs_retry",
            message=f"assistant review blocked: {', '.join(details)}",
            changed_files=[state_path],
            transitioned=False,
            stage_after=str(state.get("stage", stage_before)),
            commit_allowed=False,
            commit_cycle_stage="review",
        )

    outcome = _run_once_standard(
        state_path,
        None,
        run_agent_mode=run_agent_mode,
        auto_decision=False,
        auto_mode=auto_mode,
        commit_task_id=current_task_id,
        commit_cycle_stage="implement",
        strict_implementation_progress=False,
    )

    try:
        refreshed = _normalize_state(_load_state(state_path))
    except StateError:
        refreshed = None
    if refreshed is not None and outcome.exit_code == 0 and refreshed.get("stage") not in TERMINAL_STAGES:
        refreshed["assistant_mode"] = "on"
        refreshed["task_cycle_stage"] = "verify"
        _write_json(state_path, refreshed)
        outcome = RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=f"{outcome.message}; assistant cycle -> verify",
            commit_allowed=False,
            commit_task_id=current_task_id,
            commit_cycle_stage="implement",
        )
    elif refreshed is not None:
        refreshed["assistant_mode"] = "on"
        if refreshed.get("stage") in TERMINAL_STAGES:
            refreshed["current_task_id"] = ""
            refreshed["task_cycle_stage"] = "done"
            refreshed["task_change_baseline"] = {}
        _write_json(state_path, refreshed)
        outcome = RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=outcome.message,
            commit_allowed=False,
            commit_task_id=current_task_id,
            commit_cycle_stage="implement",
        )
    return outcome


def _run_once(
    state_path: Path,
    decision: str | None,
    *,
    run_agent_mode: str = "policy",
    assistant: bool = False,
    auto_mode: bool = False,
    auto_decision: bool = False,
    strict_implementation_progress: bool = True,
) -> RunOutcome:
    if assistant:
        return _run_once_assistant(state_path, run_agent_mode=run_agent_mode, auto_mode=auto_mode)
    return _run_once_standard(
        state_path,
        decision,
        run_agent_mode=run_agent_mode,
        auto_decision=auto_decision,
        auto_mode=auto_mode,
        strict_implementation_progress=strict_implementation_progress,
    )


def _cmd_status(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    try:
        state = _load_state(state_path)
    except RuntimeError as exc:
        print(f"autolab status: ERROR {exc}", file=sys.stderr)
        return 1

    print("autolab status")
    print(f"state_file: {state_path}")
    for key in (
        "iteration_id",
        "experiment_id",
        "stage",
        "stage_attempt",
        "last_run_id",
        "sync_status",
        "assistant_mode",
        "current_task_id",
        "task_cycle_stage",
        "repeat_guard",
        "task_change_baseline",
        "max_stage_attempts",
        "max_total_iterations",
    ):
        value = state.get(key, "<missing>")
        print(f"{key}: {value}")
    return 0


def _cmd_sync_scaffold(args: argparse.Namespace) -> int:
    try:
        source_root = _resolve_scaffold_source()
    except RuntimeError as exc:
        print(f"autolab sync-scaffold: ERROR {exc}", file=sys.stderr)
        return 1

    destination = Path(args.dest).expanduser().resolve()
    copied, skipped = _sync_scaffold_bundle(
        source_root,
        destination,
        overwrite=bool(args.force),
    )
    print("autolab sync-scaffold")
    print(f"source: {source_root}")
    print(f"destination: {destination}")
    print(f"copied_files: {copied}")
    print(f"skipped_files: {skipped}")
    if not args.force and skipped and copied == 0:
        print("No files copied. Add --force to overwrite existing files.")
    return 0


def _cmd_slurm_job_list(args: argparse.Namespace) -> int:
    action = str(getattr(args, "action", "")).strip().lower()
    manifest_path = Path(args.manifest).expanduser()
    doc_path = Path(args.doc).expanduser()
    if action not in {"append", "verify"}:
        print(
            f"autolab slurm-job-list: invalid action '{action}' (expected append|verify)",
            file=sys.stderr,
        )
        return 1

    try:
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"autolab slurm-job-list: ERROR loading manifest {manifest_path}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(manifest_payload, dict):
        print(f"autolab slurm-job-list: ERROR manifest {manifest_path} must be a JSON object", file=sys.stderr)
        return 1

    if action == "append":
        try:
            if not is_slurm_manifest(manifest_payload):
                print(
                    f"autolab slurm-job-list: manifest is non-SLURM; append skipped for {manifest_path}"
                )
                return 0
            if doc_path.parent != manifest_path.parent:
                doc_path.parent.mkdir(parents=True, exist_ok=True)
            run_id = required_run_id(manifest_payload)
            canonical = canonical_slurm_job_bullet(manifest_payload)
            existing_text = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""
            next_text, updated = append_entry_idempotent(existing_text, canonical, run_id)
            if updated:
                doc_path.write_text(next_text, encoding="utf-8")
                print(f"autolab slurm-job-list: appended run_id={run_id} -> {doc_path}")
            else:
                print(f"autolab slurm-job-list: run_id={run_id} already present in {doc_path}")
            return 0
        except Exception as exc:
            print(f"autolab slurm-job-list: ERROR {exc}", file=sys.stderr)
            return 1

    try:
        if not is_slurm_manifest(manifest_payload):
            print(f"autolab slurm-job-list: manifest is non-SLURM; verify skipped for {manifest_path}")
            return 0
        run_id = required_run_id(manifest_payload)
        job_id = required_slurm_job_id(manifest_payload)
        expected = canonical_slurm_job_bullet(manifest_payload)
        ledger_text = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""
        if not ledger_contains_entry(ledger_text, expected):
            print(
                f"autolab slurm-job-list: FAIL run_id={run_id}, job_id={job_id}, missing ledger entry in {doc_path}"
            )
            return 1
        print(f"autolab slurm-job-list: PASS job_id={job_id}, run_id={run_id}")
        return 0
    except Exception as exc:
        print(f"autolab slurm-job-list: ERROR verifying {manifest_path}: {exc}", file=sys.stderr)
        return 1


def _cmd_init(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = repo_root / ".autolab"
    created: list[Path] = []

    for directory in (
        autolab_dir,
        autolab_dir / "logs",
        autolab_dir / "logs" / "iterations",
        autolab_dir / "prompts" / "shared",
        autolab_dir / "schemas",
        autolab_dir / "verifiers",
        repo_root / "experiments",
    ):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(directory)

    backlog_path = autolab_dir / "backlog.yaml"
    verifier_policy_path = autolab_dir / "verifier_policy.yaml"
    agent_result_path = autolab_dir / "agent_result.json"

    iteration_id = ""
    if state_path.exists():
        try:
            state = _normalize_state(_load_state(state_path))
        except StateError as exc:
            print(f"autolab init: ERROR {exc}", file=sys.stderr)
            return 1
        iteration_id = state["iteration_id"]
    else:
        iteration_id = _parse_iteration_from_backlog(backlog_path)
        if not iteration_id:
            iteration_id = _bootstrap_iteration_id()
        _ensure_json_file(state_path, _default_state(iteration_id), created)

    _ensure_text_file(backlog_path, DEFAULT_BACKLOG_TEMPLATE.format(iteration_id=iteration_id), created)
    _ensure_text_file(verifier_policy_path, DEFAULT_VERIFIER_POLICY, created)
    _ensure_json_file(agent_result_path, _default_agent_result(), created)
    for stage, prompt_file in STAGE_PROMPT_FILES.items():
        _ensure_text_file(
            autolab_dir / "prompts" / prompt_file,
            _default_stage_prompt_text(stage),
            created,
        )
    _ensure_iteration_skeleton(repo_root, iteration_id, created)
    try:
        init_state = _normalize_state(_load_state(state_path))
    except StateError:
        init_state = None
    todo_sync_changed, _ = _safe_todo_pre_sync(repo_root, init_state)
    for path in todo_sync_changed:
        if path not in created:
            created.append(path)

    _append_log(repo_root, f"init completed for iteration {iteration_id}; created={len(created)}")

    print("autolab init")
    print(f"state_file: {state_path}")
    print(f"iteration_id: {iteration_id}")
    print(f"created_entries: {len(created)}")
    for path in created:
        print(f"- {path}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    run_agent_mode = _resolve_run_agent_mode(getattr(args, "run_agent_mode", "policy"))
    assistant_mode = bool(getattr(args, "assistant", False))
    baseline_snapshot = _collect_change_snapshot(repo_root)
    outcome = _run_once(
        state_path,
        args.decision,
        run_agent_mode=run_agent_mode,
        assistant=assistant_mode,
        auto_mode=False,
        auto_decision=bool(getattr(args, "auto_decision", False)),
        strict_implementation_progress=bool(getattr(args, "strict_implementation_progress", True)),
    )
    commit_outcome = _prepare_standard_commit_outcome(
        repo_root,
        outcome,
        baseline_snapshot,
        assistant=assistant_mode,
        strict_implementation_progress=bool(getattr(args, "strict_implementation_progress", True)),
    )
    commit_summary = _try_auto_commit(repo_root, outcome=commit_outcome)
    print("autolab run")
    print(f"state_file: {state_path}")
    print(f"run_agent_mode: {run_agent_mode}")
    print(f"assistant: {bool(getattr(args, 'assistant', False))}")
    print(f"auto_decision: {bool(getattr(args, 'auto_decision', False))}")
    print(f"stage_before: {outcome.stage_before}")
    print(f"stage_after: {outcome.stage_after}")
    print(f"transitioned: {outcome.transitioned}")
    print(f"message: {outcome.message}")
    print(commit_summary)
    if outcome.exit_code != 0:
        print(f"autolab run: ERROR {outcome.message}", file=sys.stderr)
    return outcome.exit_code


def _cmd_loop(args: argparse.Namespace) -> int:
    if args.max_iterations <= 0:
        print("autolab loop: ERROR --max-iterations must be > 0", file=sys.stderr)
        return 2
    if args.auto and args.max_hours <= 0:
        print("autolab loop: ERROR --max-hours must be > 0 when --auto is enabled", file=sys.stderr)
        return 2

    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    lock_path = autolab_dir / "lock"
    max_hours = float(args.max_hours)
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    todo_open_before = _todo_open_count(repo_root)
    terminal_reason = "iteration_budget_reached"
    loop_rows: list[dict[str, Any]] = []
    auto_decision_count = 0
    retry_escalation_count = 0
    overall_exit_code = 0
    lock_acquired = False

    print("autolab loop")
    print(f"state_file: {state_path}")
    print(f"max_iterations: {args.max_iterations}")
    run_agent_mode = _resolve_run_agent_mode(getattr(args, "run_agent_mode", "policy"))
    auto_decision_enabled = bool(args.auto or run_agent_mode == "force_on")
    assistant_mode = bool(getattr(args, "assistant", False))
    print(f"run_agent_mode: {run_agent_mode}")
    print(f"assistant: {assistant_mode}")
    if args.auto:
        print("auto: true")
        print(f"max_hours: {max_hours}")
        lock_ok, lock_msg = _acquire_lock(
            lock_path,
            state_file=state_path,
            command=" ".join(sys.argv),
            stale_seconds=LOCK_STALE_SECONDS,
        )
        if not lock_ok:
            print(f"autolab loop: ERROR {lock_msg}", file=sys.stderr)
            return 1
        lock_acquired = True
        _append_log(repo_root, f"auto loop lock acquired: {lock_msg}")

    try:
        for index in range(1, args.max_iterations + 1):
            if args.auto and (time.monotonic() - started_monotonic) >= max_hours * 3600:
                terminal_reason = "time_budget_reached"
                print("autolab loop: stop (time budget reached)")
                break

            decision: str | None = None
            current_stage = ""
            if args.auto:
                try:
                    current_state = _normalize_state(_load_state(state_path))
                except StateError:
                    current_state = None
                if current_state is not None:
                    current_stage = str(current_state.get("stage", ""))
                if current_stage == "decide_repeat":
                    auto_decision_count += 1
                _heartbeat_lock(lock_path)

            baseline_snapshot = _collect_change_snapshot(repo_root)
            outcome = _run_once(
                state_path,
                decision if args.auto else None,
                run_agent_mode=run_agent_mode,
                assistant=assistant_mode,
                auto_mode=bool(args.auto),
                auto_decision=auto_decision_enabled,
                strict_implementation_progress=bool(getattr(args, "strict_implementation_progress", True)),
            )
            commit_outcome = _prepare_standard_commit_outcome(
                repo_root,
                outcome,
                baseline_snapshot,
                assistant=assistant_mode,
                strict_implementation_progress=bool(getattr(args, "strict_implementation_progress", True)),
            )
            commit_summary = _try_auto_commit(repo_root, outcome=commit_outcome)
            if "escalating to human_review" in outcome.message:
                retry_escalation_count += 1
            loop_rows.append(
                {
                    "index": index,
                    "stage_before": outcome.stage_before,
                    "stage_after": outcome.stage_after,
                    "transitioned": outcome.transitioned,
                    "exit_code": outcome.exit_code,
                    "decision": "auto" if args.auto and current_stage == "decide_repeat" else "-",
                    "message": outcome.message,
                }
            )
            print(
                f"iteration {index}: {outcome.stage_before} -> {outcome.stage_after} "
                f"(transitioned={outcome.transitioned}, exit={outcome.exit_code})"
            )
            print(f"iteration {index}: {commit_summary}")
            if outcome.exit_code != 0:
                print(f"autolab loop: ERROR {outcome.message}", file=sys.stderr)
                overall_exit_code = outcome.exit_code
                terminal_reason = "error"
                if outcome.stage_after == "human_review":
                    terminal_reason = "human_review"
                break
            if outcome.stage_after in TERMINAL_STAGES:
                terminal_reason = outcome.stage_after
                print(f"autolab loop: stop (terminal stage): {outcome.stage_after}")
                if args.auto and outcome.stage_after == "human_review":
                    overall_exit_code = 1
                break
            if not outcome.transitioned:
                if assistant_mode and outcome.exit_code == 0:
                    continue
                terminal_reason = "no_transition"
                print(f"autolab loop: stop (no transition): {outcome.message}")
                break
        else:
            terminal_reason = "iteration_budget_reached"

        final_stage = "<unknown>"
        try:
            final_state = _normalize_state(_load_state(state_path))
            final_stage = str(final_state["stage"])
        except StateError:
            pass

        if args.auto and final_stage == "human_review" and overall_exit_code == 0:
            overall_exit_code = 1
            terminal_reason = "human_review"

        print("autolab loop: complete")
        return overall_exit_code
    finally:
        ended_at = _utc_now()
        elapsed_seconds = time.monotonic() - started_monotonic
        if args.auto:
            final_stage = "<unknown>"
            try:
                final_state = _normalize_state(_load_state(state_path))
                final_stage = str(final_state["stage"])
            except StateError:
                pass
            todo_open_after = _todo_open_count(repo_root)
            try:
                _write_overnight_summary(
                    repo_root,
                    state_path=state_path,
                    started_at=started_at,
                    ended_at=ended_at,
                    elapsed_seconds=elapsed_seconds,
                    max_iterations=int(args.max_iterations),
                    max_hours=max_hours,
                    auto_decision_count=auto_decision_count,
                    retry_escalation_count=retry_escalation_count,
                    todo_open_before=todo_open_before,
                    todo_open_after=todo_open_after,
                    terminal_reason=terminal_reason,
                    final_stage=final_stage,
                    exit_code=overall_exit_code,
                    rows=loop_rows,
                )
            except Exception as exc:
                print(f"autolab loop: WARN failed to write overnight summary: {exc}", file=sys.stderr)
            if lock_acquired:
                _release_lock(lock_path)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="autolab command line interface")
    subparsers = parser.add_subparsers(dest="command")

    init = subparsers.add_parser("init", help="Initialize autolab scaffold and state files")
    init.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    init.set_defaults(handler=_cmd_init)

    run = subparsers.add_parser("run", help="Run one deterministic stage transition")
    run.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    run.add_argument(
        "--decision",
        choices=DECISION_STAGES,
        default=None,
        help="Manual decision target when current stage is decide_repeat",
    )
    run.add_argument(
        "--assistant",
        action="store_true",
        help="Enable engineer-assistant task cycle mode for this run.",
    )
    run.add_argument(
        "--auto-decision",
        action="store_true",
        help="Allow decide_repeat to auto-select from todo/backlog when --decision is not provided.",
    )
    run.add_argument(
        "--strict-implementation-progress",
        dest="strict_implementation_progress",
        action="store_true",
        help="Require meaningful implementation progress checks (default).",
    )
    run.add_argument(
        "--no-strict-implementation-progress",
        dest="strict_implementation_progress",
        action="store_false",
        help="Disable meaningful implementation progress checks.",
    )
    run_runner_group = run.add_mutually_exclusive_group()
    run_runner_group.add_argument(
        "--run-agent",
        dest="run_agent_mode",
        action="store_const",
        const="force_on",
        help="Force agent_runner invocation for eligible stages.",
    )
    run_runner_group.add_argument(
        "--no-run-agent",
        dest="run_agent_mode",
        action="store_const",
        const="force_off",
        help="Disable agent_runner invocation even if enabled in policy.",
    )
    run.set_defaults(run_agent_mode="policy")
    run.set_defaults(strict_implementation_progress=True)
    run.set_defaults(handler=_cmd_run)

    loop = subparsers.add_parser("loop", help="Run bounded stage transitions in sequence")
    loop.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    loop.add_argument(
        "--max-iterations",
        type=int,
        default=1,
        help="Maximum number of run iterations to execute (must be > 0)",
    )
    loop.add_argument(
        "--auto",
        action="store_true",
        help="Enable unattended loop mode with automatic decide_repeat decisions and lock enforcement.",
    )
    loop.add_argument(
        "--assistant",
        action="store_true",
        help="Enable engineer-assistant task cycle mode for unattended feature delivery.",
    )
    loop.add_argument(
        "--strict-implementation-progress",
        dest="strict_implementation_progress",
        action="store_true",
        help="Require meaningful implementation progress checks (default).",
    )
    loop.add_argument(
        "--no-strict-implementation-progress",
        dest="strict_implementation_progress",
        action="store_false",
        help="Disable meaningful implementation progress checks.",
    )
    loop.add_argument(
        "--max-hours",
        type=float,
        default=DEFAULT_MAX_HOURS,
        help="Maximum wall-clock runtime in hours for --auto mode (must be > 0).",
    )
    loop_runner_group = loop.add_mutually_exclusive_group()
    loop_runner_group.add_argument(
        "--run-agent",
        dest="run_agent_mode",
        action="store_const",
        const="force_on",
        help="Force agent_runner invocation for eligible stages.",
    )
    loop_runner_group.add_argument(
        "--no-run-agent",
        dest="run_agent_mode",
        action="store_const",
        const="force_off",
        help="Disable agent_runner invocation even if enabled in policy.",
    )
    loop.set_defaults(run_agent_mode="policy")
    loop.set_defaults(strict_implementation_progress=True)
    loop.set_defaults(handler=_cmd_loop)

    status = subparsers.add_parser("status", help="Show current .autolab state")
    status.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    status.set_defaults(handler=_cmd_status)

    sync_scaffold = subparsers.add_parser(
        "sync-scaffold",
        help="Sync bundled autolab scaffold files into the repository",
    )
    sync_scaffold.add_argument(
        "--dest",
        default=".autolab",
        help="Target directory for scaffold files (default: .autolab)",
    )
    sync_scaffold.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing scaffold files.",
    )
    sync_scaffold.set_defaults(handler=_cmd_sync_scaffold)

    slurm_job_list = subparsers.add_parser(
        "slurm-job-list",
        help="Maintain or verify docs/slurm_job_list.md ledger entries for run manifests.",
    )
    slurm_job_list.add_argument(
        "action",
        choices=("append", "verify"),
        help="Action to perform against a run manifest.",
    )
    slurm_job_list.add_argument(
        "--manifest",
        required=True,
        help="Path to experiments/<iteration_id>/runs/<run_id>/run_manifest.json",
    )
    slurm_job_list.add_argument(
        "--doc",
        required=True,
        help="Path to docs/slurm_job_list.md.",
    )
    slurm_job_list.set_defaults(handler=_cmd_slurm_job_list)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
