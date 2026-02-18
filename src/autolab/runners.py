from __future__ import annotations
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from autolab.constants import (
    DEFAULT_AGENT_RUNNER_NAME,
    DEFAULT_EXPERIMENT_TYPE,
    EXPERIMENT_TYPES,
    ITERATION_ID_SAFE_PATTERN,
)
from autolab.models import AgentRunnerEditScopeConfig, StageCheckError, StateError
from autolab.config import _load_agent_runner_config, _load_protected_files, _load_verifier_policy, _resolve_run_agent_mode
from autolab.state import (
    _load_state,
    _normalize_state,
    _resolve_iteration_directory,
)
from autolab.prompts import _render_stage_prompt, _resolve_stage_prompt_path
from autolab.utils import (
    _append_log,
    _collect_change_snapshot,
    _is_git_worktree,
    _snapshot_delta_paths,
)
from autolab.config import _load_meaningful_change_config
from autolab.state import _ensure_iteration_skeleton


def _compact_log_text(text: str, limit: int = 240) -> str:
    compact = " ".join(text.strip().split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}..."


SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password)\b\s*[:=]\s*([^\s]+)"),
    re.compile(r"(?i)\b(authorization:\s*bearer)\s+([^\s]+)"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bASIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b"),
)


def _redact_sensitive_text(text: str) -> str:
    redacted = str(text)
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(lambda match: f"{match.group(1)}=<redacted>" if match.groups() else "<redacted>", redacted)
    return redacted


_SHELL_META_PATTERN = re.compile(r"[|&;<>()$`]")


def _command_uses_shell_syntax(command: str) -> bool:
    return bool(_SHELL_META_PATTERN.search(command))


def _write_runner_execution_report(
    repo_root: Path,
    *,
    payload: dict[str, Any],
) -> None:
    report_path = repo_root / ".autolab" / "runner_execution_report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _collect_filesystem_snapshot(repo_root: Path) -> dict[str, tuple[float, int]]:
    """Walk the repo and collect (mtime, size) for every file.

    Used as a fallback when ``_is_git_worktree()`` is False so that scope
    violations can still be detected after agent execution.
    """
    snapshot: dict[str, tuple[float, int]] = {}
    root = repo_root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden dirs and __pycache__
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d != "__pycache__" and d != "node_modules"
        ]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            try:
                stat = fpath.stat()
                rel = fpath.relative_to(root).as_posix()
                snapshot[rel] = (stat.st_mtime, stat.st_size)
            except (OSError, ValueError):
                continue
    return snapshot


def _filesystem_snapshot_delta_paths(
    before: dict[str, tuple[float, int]],
    after: dict[str, tuple[float, int]],
) -> list[str]:
    """Compare two filesystem snapshots and return paths that changed."""
    changed: set[str] = set()
    for path, (mtime, size) in after.items():
        prev = before.get(path)
        if prev is None or prev != (mtime, size):
            changed.add(path)
    # Deleted files
    for path in before:
        if path not in after:
            changed.add(path)
    return sorted(changed)


def _is_within_scope(path: str, allowed_roots: tuple[str, ...]) -> bool:
    normalized = path.strip().replace("\\", "/")
    if not normalized:
        return True
    for root in allowed_roots:
        candidate = root.strip().strip("/")
        if not candidate:
            continue
        if normalized == candidate or normalized.startswith(f"{candidate}/"):
            return True
    return False


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
    experiment_id: str = "",
    ensure_iteration_dir: bool,
) -> Path:
    normalized_iteration_id = _normalize_workspace_iteration_id(iteration_id)
    experiments_root = _resolve_repo_relative_dir(
        repo_root,
        "experiments",
        field_name="runner experiments root",
    )

    workspace_dir, workspace_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=normalized_iteration_id,
        experiment_id=experiment_id,
        require_exists=not ensure_iteration_dir,
    )

    if workspace_dir.parent.parent != experiments_root or workspace_dir.parent.name not in EXPERIMENT_TYPES:
        raise StageCheckError(
            f"state.iteration_id must resolve within experiments/ for runner workspace scoping, got '{workspace_dir}'"
        )

    if ensure_iteration_dir and not workspace_dir.exists():
        created: list[Path] = []
        effective_type = workspace_type if workspace_type in EXPERIMENT_TYPES else DEFAULT_EXPERIMENT_TYPE
        _ensure_iteration_skeleton(
            repo_root,
            normalized_iteration_id,
            created,
            experiment_type=effective_type,
        )
        workspace_dir, _workspace_type = _resolve_iteration_directory(
            repo_root,
            iteration_id=normalized_iteration_id,
            experiment_id=experiment_id,
            require_exists=True,
        )
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
        "{stage}": shlex.quote(stage),
        "{prompt_path}": shlex.quote(str(prompt_path)),
        "{prompt_template_path}": shlex.quote(str(prompt_template_path)),
        "{prompt_context_path}": shlex.quote(str(prompt_context_path)),
        "{iteration_id}": shlex.quote(iteration_id),
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
    workspace_dir = _resolve_runner_workspace(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=str(prompt_state.get("experiment_id", "")).strip(),
        ensure_iteration_dir=runner.edit_scope.ensure_iteration_dir,
    )
    core_add_dirs, resolved_core_dirs = _build_core_add_dir_flags(
        repo_root,
        edit_scope=runner.edit_scope,
        runner=runner.runner,
    )
    runner_scope = {
        "mode": runner.edit_scope.mode,
        "workspace_dir": str(workspace_dir),
        "allowed_edit_dirs": [str(path.relative_to(repo_root)) for path in resolved_core_dirs],
    }
    prompt_bundle = _render_stage_prompt(
        repo_root,
        stage=stage,
        state=prompt_state,
        template_path=prompt_template_path,
        runner_scope=runner_scope,
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
    use_git_scope = _is_git_worktree(repo_root)
    baseline_snapshot = _collect_change_snapshot(repo_root) if use_git_scope else None
    fs_baseline_snapshot: dict[str, tuple[float, int]] | None = None
    if not use_git_scope:
        meaningful_config = _load_meaningful_change_config(repo_root)
        if meaningful_config.on_non_git_behavior == "fail":
            raise StageCheckError(
                "agent runner requires a git worktree for scope checking; "
                "set meaningful_change.on_non_git_behavior to 'warn_and_continue' to use filesystem snapshot fallback"
            )
        fs_baseline_snapshot = _collect_filesystem_snapshot(repo_root)
    workspace_rel = workspace_dir.relative_to(repo_root).as_posix()
    allowed_roots = tuple(
        sorted(
            {
                workspace_rel,
                *(path.relative_to(repo_root).as_posix() for path in resolved_core_dirs),
            }
        )
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
    run_report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "stage": stage,
        "runner": runner.runner,
        "workspace_dir": str(workspace_dir),
        "edit_scope_mode": runner.edit_scope.mode,
        "allowed_roots": list(allowed_roots),
        "timeout_seconds": runner.timeout_seconds,
        "status": "starting",
        "command_argv": [],
        "exit_code": None,
    }
    _append_log(
        repo_root,
        (
            f"agent runner start stage={stage} timeout_seconds={runner.timeout_seconds} "
            f"workspace_dir={workspace_dir} prompt_template={prompt_bundle.template_path} "
            f"prompt_rendered={prompt_bundle.rendered_path} command={_redact_sensitive_text(command)}"
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
        if _command_uses_shell_syntax(command):
            raise StageCheckError(
                "agent_runner.command contains shell metacharacters; "
                "configure an argv-safe command without pipes/subshell syntax"
            )
        try:
            popen_command = shlex.split(command)
        except ValueError as exc:
            raise StageCheckError(f"agent runner command could not be parsed: {exc}") from exc
        if not popen_command:
            raise StageCheckError("agent runner command resolved to empty arguments")
        run_report["command_argv"] = [
            _redact_sensitive_text(str(token))
            for token in popen_command
        ]
        _write_runner_execution_report(repo_root, payload=run_report)

        process = subprocess.Popen(
            popen_command,
            cwd=repo_root,
            shell=False,
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
            run_report["status"] = "timeout"
            run_report["exit_code"] = None
            _write_runner_execution_report(repo_root, payload=run_report)
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            return
    except Exception as exc:
        _append_log(repo_root, f"agent runner execution error stage={stage}: {exc}")
        run_report["status"] = "error"
        run_report["error"] = str(exc)
        run_report["exit_code"] = None
        _write_runner_execution_report(repo_root, payload=run_report)
        return
    finally:
        if stdout_thread is not None:
            stdout_thread.join(timeout=2)
        if stderr_thread is not None:
            stderr_thread.join(timeout=2)

    captured_stdout = "".join(captured_stdout_chunks).strip()
    captured_stderr = "".join(captured_stderr_chunks).strip()
    if captured_stdout:
        _append_log(
            repo_root,
            f"agent runner stdout stage={stage}: {_compact_log_text(_redact_sensitive_text(captured_stdout))}",
        )
    if captured_stderr:
        _append_log(
            repo_root,
            f"agent runner stderr stage={stage}: {_compact_log_text(_redact_sensitive_text(captured_stderr))}",
        )

    effective_delta_paths: list[str] = []
    if baseline_snapshot is not None:
        current_snapshot = _collect_change_snapshot(repo_root)
        delta_paths = _snapshot_delta_paths(baseline_snapshot, current_snapshot)
        effective_delta_paths = delta_paths
        out_of_scope = sorted(path for path in delta_paths if not _is_within_scope(path, allowed_roots))
        if out_of_scope:
            sample = ", ".join(out_of_scope[:8])
            _append_log(
                repo_root,
                (
                    "agent runner scope violation: changed paths outside allowed dirs "
                    f"allowed={allowed_roots} out_of_scope={sample}"
                ),
            )
            raise StageCheckError(
                "agent runner edited paths outside allowed edit scope; "
                f"out_of_scope={sample}"
            )
    elif fs_baseline_snapshot is not None:
        fs_current_snapshot = _collect_filesystem_snapshot(repo_root)
        fs_delta_paths = _filesystem_snapshot_delta_paths(fs_baseline_snapshot, fs_current_snapshot)
        effective_delta_paths = fs_delta_paths
        fs_out_of_scope = sorted(path for path in fs_delta_paths if not _is_within_scope(path, allowed_roots))
        if fs_out_of_scope:
            sample = ", ".join(fs_out_of_scope[:8])
            _append_log(
                repo_root,
                (
                    "agent runner scope violation (filesystem snapshot): changed paths outside allowed dirs "
                    f"allowed={allowed_roots} out_of_scope={sample}"
                ),
            )
            raise StageCheckError(
                "agent runner edited paths outside allowed edit scope (detected via filesystem snapshot); "
                f"out_of_scope={sample}"
            )

    # -- Protected files denylist check --
    if effective_delta_paths:
        policy = _load_verifier_policy(repo_root)
        protected_files = _load_protected_files(policy)
        if protected_files:
            protected_set = set(protected_files)
            violated = sorted(
                path for path in effective_delta_paths
                if path.replace("\\", "/") in protected_set
            )
            if violated:
                sample = ", ".join(violated[:8])
                _append_log(
                    repo_root,
                    (
                        f"agent runner modified protected file(s): {sample}. "
                        f"Protected files: {protected_files}"
                    ),
                )
                raise StageCheckError(
                    f"agent runner modified protected file(s): {sample}. "
                    f"Protected files: {protected_files}"
                )

    _append_log(repo_root, f"agent runner exit stage={stage} returncode={returncode}")
    run_report["status"] = "completed" if returncode == 0 else "failed"
    run_report["exit_code"] = int(returncode)
    _write_runner_execution_report(repo_root, payload=run_report)
    if returncode != 0:
        _append_log(repo_root, f"agent runner non-zero exit at stage={stage}; continuing with stage evaluation")
