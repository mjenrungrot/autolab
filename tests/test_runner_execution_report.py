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


class _CaptureStdin:
    def __init__(self) -> None:
        self._chunks: list[str] = []
        self.closed = False

    def write(self, text: str) -> int:
        self._chunks.append(str(text))
        return len(text)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    @property
    def text(self) -> str:
        return "".join(self._chunks)


class _CaptureProcess:
    launched: list["_CaptureProcess"] = []

    def __init__(self, *args, **kwargs) -> None:
        self.command = list(args[0]) if args else []
        self.env = dict(kwargs.get("env", {}))
        self.stdin = _CaptureStdin()
        self.stdout = _StaticStream([])
        self.stderr = _StaticStream([])
        self.pid = 999999
        type(self).launched.append(self)

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
            mode="scope_root_only",
            core_dirs=(),
            ensure_iteration_dir=False,
        ),
        timeout_seconds=60.0,
        codex_dangerously_bypass_approvals_and_sandbox=False,
    )
    monkeypatch.setattr(
        runners, "_load_agent_runner_config", lambda _root: runner_config
    )
    monkeypatch.setattr(
        runners,
        "_resolve_stage_prompt_path",
        lambda _root, _stage, **_kwargs: template_path,
    )
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
    monkeypatch.setattr(
        runners, "_load_protected_files", lambda _policy, auto_mode=False: []
    )
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
            mode="scope_root_plus_core",
            core_dirs=(".autolab",),
            ensure_iteration_dir=False,
        ),
        timeout_seconds=60.0,
        codex_dangerously_bypass_approvals_and_sandbox=False,
    )
    monkeypatch.setattr(
        runners, "_load_agent_runner_config", lambda _root: runner_config
    )
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


def test_substitute_runner_command_supports_audit_brief_and_human_tokens() -> None:
    rendered = runners._substitute_runner_command(
        (
            "runner --prompt {prompt_runner_path} "
            "--prompt-legacy {prompt_path} "
            "--audit {prompt_audit_path} "
            "--brief {prompt_brief_path} "
            "--brief-legacy {prompt_retry_brief_path} "
            "--human {prompt_human_path} "
            "--scope-root {scope_root}"
        ),
        stage="implementation",
        prompt_runner_path=Path("/tmp/implementation.runner.md"),
        prompt_template_path=Path("/tmp/stage_implementation_runner.md"),
        prompt_context_path=Path("/tmp/implementation.context.json"),
        prompt_audit_path=Path("/tmp/implementation.audit.md"),
        prompt_brief_path=Path("/tmp/implementation.brief.md"),
        prompt_human_path=Path("/tmp/implementation.human.md"),
        iteration_id="iter1",
        workspace_dir=Path("/tmp/workspace"),
        scope_root=Path("/tmp/scope_root"),
        core_add_dirs="",
    )
    assert "--audit /tmp/implementation.audit.md" in rendered
    assert "--brief /tmp/implementation.brief.md" in rendered
    assert "--prompt-legacy /tmp/implementation.runner.md" in rendered
    assert "--brief-legacy /tmp/implementation.brief.md" in rendered
    assert "--human /tmp/implementation.human.md" in rendered
    assert "--scope-root /tmp/scope_root" in rendered


def test_task_packet_mode_uses_minimal_isolated_prompt_artifacts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = _configure_runner_environment(
        monkeypatch,
        repo_root,
        process_factory=_CaptureProcess,
    )
    _CaptureProcess.launched.clear()
    runner_config = AgentRunnerConfig(
        runner="codex",
        enabled=True,
        command=(
            "fake-runner "
            "--runner {prompt_runner_path} "
            "--context {prompt_context_path} "
            "--audit {prompt_audit_path} "
            "--brief {prompt_brief_path} "
            "--human {prompt_human_path}"
        ),
        stages=("implementation",),
        edit_scope=AgentRunnerEditScopeConfig(
            mode="scope_root_only",
            core_dirs=(),
            ensure_iteration_dir=False,
        ),
        timeout_seconds=60.0,
        codex_dangerously_bypass_approvals_and_sandbox=False,
    )
    monkeypatch.setattr(
        runners, "_load_agent_runner_config", lambda _root: runner_config
    )
    monkeypatch.setattr(
        runners,
        "_render_stage_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("task packet mode should not render full stage prompt packs")
        ),
    )

    task_packet = {
        "task_id": "T1",
        "objective": "Implement minimal task packet execution.",
        "scope_kind": "experiment",
        "depends_on": [],
        "reads": ["experiments/plan/iter1/design.yaml"],
        "writes": ["src/foo.py"],
        "touches": ["src/foo.py"],
        "expected_artifacts": ["src/foo.py"],
        "verification_commands": ["pytest -q tests/test_runner_execution_report.py"],
    }
    result = runners._invoke_agent_runner(
        repo_root,
        state_path=state_path,
        stage="implementation",
        iteration_id="iter1",
        run_agent_mode="policy",
        task_packet=task_packet,
        task_context={
            "wave": 1,
            "attempt": 1,
            "max_attempts": 2,
            "agent_surface": {"primary_role": "planner"},
            "stage_context": "should not be copied into task packets",
            "sidecar_context": {
                "context_inputs": ["project_wide:research:findings:pw-research"],
                "resolved_inputs": [
                    "project_wide:research:findings:pw-research: Project-wide research summary"
                ],
                "compact_summary": "Project-wide research summary",
                "extra": "should be dropped",
            },
        },
    )

    assert result["status"] == "completed"
    assert result["exit_code"] == 0
    assert len(_CaptureProcess.launched) == 1
    launched = _CaptureProcess.launched[0]

    runner_path = Path(launched.env["AUTOLAB_PROMPT_RUNNER_PATH"])
    context_path = Path(launched.env["AUTOLAB_PROMPT_CONTEXT_PATH"])
    audit_path = Path(launched.env["AUTOLAB_PROMPT_AUDIT_PATH"])
    brief_path = Path(launched.env["AUTOLAB_PROMPT_BRIEF_PATH"])
    human_path = Path(launched.env["AUTOLAB_PROMPT_HUMAN_PATH"])

    assert ".task_" in runner_path.name
    assert ".task_" in context_path.name
    assert ".task_" in audit_path.name
    assert ".task_" in brief_path.name
    assert ".task_" in human_path.name

    assert runner_path.exists()
    assert context_path.exists()
    assert audit_path.exists()
    assert brief_path.exists()
    assert human_path.exists()
    assert not (
        repo_root / ".autolab" / "prompts" / "rendered" / "implementation.runner.md"
    ).exists()

    runner_prompt_text = runner_path.read_text(encoding="utf-8")
    assert "Stage: implementation (runner task)" in runner_prompt_text
    assert "Implementation Auditor" not in runner_prompt_text
    assert (
        "verification_commands: pytest -q tests/test_runner_execution_report.py"
        in runner_prompt_text
    )

    task_context_payload = json.loads(context_path.read_text(encoding="utf-8"))
    assert task_context_payload["task"]["task_id"] == "T1"
    assert task_context_payload["task_context"]["wave"] == 1
    assert task_context_payload["task_context"]["max_attempts"] == 2
    assert task_context_payload["task_context"]["sidecar_context"] == {
        "context_inputs": ["project_wide:research:findings:pw-research"],
        "resolved_inputs": [
            "project_wide:research:findings:pw-research: Project-wide research summary"
        ],
        "compact_summary": "Project-wide research summary",
    }
    assert "allowed_edit_dirs" in task_context_payload["runner_scope"]
    assert task_context_payload["runner_scope"]["scope_kind"] == "experiment"
    assert task_context_payload["runner_scope"]["scope_root"] == str(
        repo_root / "experiments" / "plan" / "iter1"
    )
    assert "agent_surface" not in task_context_payload
    assert "agent_surface" not in task_context_payload["task_context"]
    assert "stage_context" not in task_context_payload["task_context"]

    assert "Task packet mode is active." in audit_path.read_text(encoding="utf-8")
    assert "Task packet mode is active." in brief_path.read_text(encoding="utf-8")
    assert "Task packet mode is active." in human_path.read_text(encoding="utf-8")

    argv = launched.command
    assert argv[0] == "fake-runner"
    assert argv[argv.index("--runner") + 1] == str(runner_path)
    assert argv[argv.index("--context") + 1] == str(context_path)
    assert argv[argv.index("--audit") + 1] == str(audit_path)
    assert argv[argv.index("--brief") + 1] == str(brief_path)
    assert argv[argv.index("--human") + 1] == str(human_path)
    assert "Stage: implementation (runner task)" in launched.stdin.text
    assert launched.env["AUTOLAB_SCOPE_ROOT"] == str(
        repo_root / "experiments" / "plan" / "iter1"
    )


def test_task_packet_rendered_paths_are_unique_per_invocation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = _configure_runner_environment(
        monkeypatch,
        repo_root,
        process_factory=_CaptureProcess,
    )
    _CaptureProcess.launched.clear()
    monkeypatch.setattr(
        runners,
        "_render_stage_prompt",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("task packet mode should not render full stage prompt packs")
        ),
    )

    task_packet = {
        "task_id": "T1",
        "objective": "Unique task packet artifacts per invocation.",
        "scope_kind": "experiment",
        "depends_on": [],
        "reads": [],
        "writes": ["src/foo.py"],
        "touches": ["src/foo.py"],
        "expected_artifacts": ["src/foo.py"],
        "verification_commands": [],
    }
    runners._invoke_agent_runner(
        repo_root,
        state_path=state_path,
        stage="implementation",
        iteration_id="iter1",
        run_agent_mode="policy",
        task_packet=task_packet,
        task_context={"wave": 1, "attempt": 1},
    )
    runners._invoke_agent_runner(
        repo_root,
        state_path=state_path,
        stage="implementation",
        iteration_id="iter1",
        run_agent_mode="policy",
        task_packet=task_packet,
        task_context={"wave": 1, "attempt": 1},
    )

    assert len(_CaptureProcess.launched) == 2
    first = _CaptureProcess.launched[0].env
    second = _CaptureProcess.launched[1].env
    for key in (
        "AUTOLAB_PROMPT_RUNNER_PATH",
        "AUTOLAB_PROMPT_CONTEXT_PATH",
        "AUTOLAB_PROMPT_AUDIT_PATH",
        "AUTOLAB_PROMPT_BRIEF_PATH",
        "AUTOLAB_PROMPT_HUMAN_PATH",
    ):
        assert first[key] != second[key]


def test_project_wide_task_uses_configured_scope_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "src").mkdir(parents=True, exist_ok=True)
    policy_path = repo_root / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        "scope_roots:\n  project_wide_root: src\n",
        encoding="utf-8",
    )
    state_path = _configure_runner_environment(
        monkeypatch,
        repo_root,
        process_factory=_CaptureProcess,
    )
    _CaptureProcess.launched.clear()
    monkeypatch.setattr(
        runners,
        "_resolve_runner_workspace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("project_wide tasks should not resolve iteration workspace")
        ),
    )

    task_packet = {
        "task_id": "Tpw",
        "objective": "Project-wide update under configured scope root.",
        "scope_kind": "project_wide",
        "depends_on": [],
        "reads": ["src/config.yaml"],
        "writes": ["src/config.yaml"],
        "touches": ["src/config.yaml"],
        "expected_artifacts": ["src/config.yaml"],
        "verification_commands": [],
    }
    result = runners._invoke_agent_runner(
        repo_root,
        state_path=state_path,
        stage="implementation",
        iteration_id="iter1",
        run_agent_mode="policy",
        task_packet=task_packet,
    )

    assert result["status"] == "completed"
    launched = _CaptureProcess.launched[0]
    assert launched.env["AUTOLAB_SCOPE_ROOT"] == str(repo_root / "src")
    assert launched.env["AUTOLAB_WORKSPACE_DIR"] == str(repo_root / "src")
    context_path = Path(launched.env["AUTOLAB_PROMPT_CONTEXT_PATH"])
    context_payload = json.loads(context_path.read_text(encoding="utf-8"))
    assert context_payload["runner_scope"]["scope_kind"] == "project_wide"
    assert context_payload["runner_scope"]["scope_root"] == str(repo_root / "src")


def test_project_wide_task_enforces_scope_violation_detection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "src").mkdir(parents=True, exist_ok=True)
    policy_path = repo_root / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        "scope_roots:\n  project_wide_root: src\n",
        encoding="utf-8",
    )
    state_path = _configure_runner_environment(
        monkeypatch,
        repo_root,
        process_factory=_ExitZeroProcess,
        delta_paths=["docs/outside.md"],
    )
    monkeypatch.setattr(
        runners,
        "_resolve_runner_workspace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("project_wide tasks should not resolve iteration workspace")
        ),
    )

    task_packet = {
        "task_id": "Tpw2",
        "objective": "Project-wide update under configured scope root.",
        "scope_kind": "project_wide",
        "depends_on": [],
        "reads": ["src/config.yaml"],
        "writes": ["src/config.yaml"],
        "touches": ["src/config.yaml"],
        "expected_artifacts": ["src/config.yaml"],
        "verification_commands": [],
    }
    with pytest.raises(StageCheckError, match="out_of_scope"):
        runners._invoke_agent_runner(
            repo_root,
            state_path=state_path,
            stage="implementation",
            iteration_id="iter1",
            run_agent_mode="policy",
            task_packet=task_packet,
        )
