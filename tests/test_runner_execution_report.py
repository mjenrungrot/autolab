from __future__ import annotations

import io
import json
import subprocess
import time
from pathlib import Path

import pytest

import autolab.runners as runners
from autolab.models import (
    AgentRunnerConfig,
    AgentRunnerEditScopeConfig,
    RenderedPromptBundle,
    StageCheckError,
)


class _StaticStream:
    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def readline(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        time.sleep(0.005)
        return ""

    def close(self) -> None:
        return None


class _DeadChannelProcess:
    def __init__(self, *_args, **_kwargs) -> None:
        self.stdin = io.StringIO()
        self.stdout = _StaticStream(["failed to queue rollout items: channel closed\n"])
        self.stderr = _StaticStream([])
        self.pid = 999999
        self._terminated = False

    def wait(self, timeout: float | None = None) -> int:
        if self._terminated:
            return 143
        raise subprocess.TimeoutExpired(cmd="fake-runner", timeout=timeout or 0.0)

    def poll(self) -> int | None:
        return 143 if self._terminated else None

    def terminate(self) -> None:
        self._terminated = True

    def kill(self) -> None:
        self._terminated = True


class _ExitZeroProcess:
    def __init__(self, *_args, **_kwargs) -> None:
        self.stdin = io.StringIO()
        self.stdout = _StaticStream([])
        self.stderr = _StaticStream([])
        self.pid = 999999

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def poll(self) -> int | None:
        return 0

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        return None


def _configure_runner_environment(
    monkeypatch: pytest.MonkeyPatch,
    repo_root: Path,
    *,
    process_factory,
    delta_paths: list[str] | None = None,
) -> Path:
    state_path = repo_root / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_payload = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "implementation",
        "stage_attempt": 0,
        "last_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    state_path.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")

    workspace_dir = repo_root / "experiments" / "plan" / "iter1"
    workspace_dir.mkdir(parents=True, exist_ok=True)

    prompt_dir = repo_root / ".autolab" / "prompts"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    template_path = prompt_dir / "stage_implementation.md"
    rendered_path = prompt_dir / "stage_implementation.rendered.md"
    context_path = prompt_dir / "stage_implementation.context.json"
    template_path.write_text("# template\n", encoding="utf-8")
    rendered_path.write_text("# rendered\n", encoding="utf-8")
    context_path.write_text("{}", encoding="utf-8")

    runner_config = AgentRunnerConfig(
        runner="codex",
        enabled=True,
        command="fake-runner",
        stages=("implementation",),
        edit_scope=AgentRunnerEditScopeConfig(
            mode="iteration_only",
            core_dirs=(),
            ensure_iteration_dir=False,
        ),
        timeout_seconds=60.0,
        codex_dangerously_bypass_approvals_and_sandbox=False,
    )
    monkeypatch.setattr(runners, "_load_agent_runner_config", lambda _root: runner_config)
    monkeypatch.setattr(runners, "_resolve_stage_prompt_path", lambda _root, _stage: template_path)
    monkeypatch.setattr(runners, "_load_state", lambda _state_path: dict(state_payload))
    monkeypatch.setattr(runners, "_normalize_state", lambda raw_state: raw_state)
    monkeypatch.setattr(
        runners,
        "_resolve_runner_workspace",
        lambda _repo_root, **_kwargs: workspace_dir,
    )
    monkeypatch.setattr(
        runners,
        "_build_core_add_dir_flags",
        lambda _repo_root, **_kwargs: ("", ()),
    )
    monkeypatch.setattr(
        runners,
        "_render_stage_prompt",
        lambda _repo_root, **_kwargs: RenderedPromptBundle(
            template_path=template_path,
            rendered_path=rendered_path,
            context_path=context_path,
            prompt_text="test prompt\n",
            context_payload={},
        ),
    )
    monkeypatch.setattr(runners, "_is_git_worktree", lambda _repo_root: True)
    monkeypatch.setattr(runners, "_collect_change_snapshot", lambda _repo_root: {})
    monkeypatch.setattr(
        runners,
        "_snapshot_delta_paths",
        lambda _before, _after: list(delta_paths or []),
    )
    monkeypatch.setattr(runners, "_load_verifier_policy", lambda _repo_root: {})
    monkeypatch.setattr(runners, "_load_protected_files", lambda _policy, auto_mode=False: [])
    monkeypatch.setattr(runners.subprocess, "Popen", process_factory)
    return state_path


def test_dead_channel_forces_runner_finalization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = _configure_runner_environment(
        monkeypatch, repo_root, process_factory=_DeadChannelProcess
    )
    monkeypatch.setattr(runners, "RUNNER_WAIT_SLICE_SECONDS", 0.01)
    monkeypatch.setattr(runners, "RUNNER_DEAD_CHANNEL_GRACE_SECONDS", 0.05)

    started = time.monotonic()
    runners._invoke_agent_runner(
        repo_root,
        state_path=state_path,
        stage="implementation",
        iteration_id="iter1",
        run_agent_mode="policy",
    )
    elapsed = time.monotonic() - started

    report_path = repo_root / ".autolab" / "runner_execution_report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert elapsed < 1.0
    assert payload["status"] == "failed"
    assert payload["termination_reason"] == "dead_channel"
    assert payload["exit_code"] == 143
    assert payload.get("finalized_at")


def test_scope_error_still_writes_terminal_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = _configure_runner_environment(
        monkeypatch,
        repo_root,
        process_factory=_ExitZeroProcess,
        delta_paths=["outside.txt"],
    )

    with pytest.raises(StageCheckError, match="out_of_scope"):
        runners._invoke_agent_runner(
            repo_root,
            state_path=state_path,
            stage="implementation",
            iteration_id="iter1",
            run_agent_mode="policy",
        )

    report_path = repo_root / ".autolab" / "runner_execution_report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["termination_reason"] == "post_run_validation"
    assert payload["exit_code"] == 0
    assert payload.get("finalized_at")
    assert "out_of_scope" in payload.get("error", "")


def test_protected_file_violation_includes_remediation_hint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = _configure_runner_environment(
        monkeypatch,
        repo_root,
        process_factory=_ExitZeroProcess,
        delta_paths=[".autolab/verifier_policy.yaml"],
    )

    runner_config = AgentRunnerConfig(
        runner="codex",
        enabled=True,
        command="fake-runner",
        stages=("implementation",),
        edit_scope=AgentRunnerEditScopeConfig(
            mode="iteration_plus_core",
            core_dirs=(".autolab",),
            ensure_iteration_dir=False,
        ),
        timeout_seconds=60.0,
        codex_dangerously_bypass_approvals_and_sandbox=False,
    )
    monkeypatch.setattr(runners, "_load_agent_runner_config", lambda _root: runner_config)
    monkeypatch.setattr(
        runners,
        "_build_core_add_dir_flags",
        lambda _repo_root, **_kwargs: (
            "--add-dir fake",
            (repo_root / ".autolab",),
        ),
    )
    monkeypatch.setattr(
        runners,
        "_load_protected_files",
        lambda _policy, auto_mode=False: [".autolab/verifier_policy.yaml"],
    )

    with pytest.raises(StageCheckError, match="modified protected file\\(s\\)"):
        runners._invoke_agent_runner(
            repo_root,
            state_path=state_path,
            stage="implementation",
            iteration_id="iter1",
            run_agent_mode="policy",
        )

    report_path = repo_root / ".autolab" / "runner_execution_report.json"
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["termination_reason"] == "post_run_validation"
    assert payload["exit_code"] == 0
    assert payload.get("finalized_at")
    assert "modified protected file(s)" in payload.get("error", "")
    assert "Remediation:" in payload.get("error", "")
