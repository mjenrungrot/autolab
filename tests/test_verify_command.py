from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

import yaml

import autolab.commands as commands_module
from autolab.validators import (
    _build_verification_command_specs,
    _run_verification_step_detailed,
)


def _copy_scaffold(repo: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "autolab"
        / "scaffold"
        / ".autolab"
    )
    target = repo / ".autolab"
    shutil.copytree(source, target, dirs_exist_ok=True)
    policy_path = target / "verifier_policy.yaml"
    policy_lines = policy_path.read_text(encoding="utf-8").splitlines()
    for idx, line in enumerate(policy_lines):
        if line.strip().startswith("python_bin:"):
            policy_lines[idx] = f'python_bin: "{sys.executable}"'
            break
    policy_path.write_text("\n".join(policy_lines) + "\n", encoding="utf-8")


def _write_state(repo: Path) -> Path:
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "design",
        "stage_attempt": 0,
        "last_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    path = repo / ".autolab" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


def _write_backlog(repo: Path) -> None:
    backlog = {
        "hypotheses": [
            {
                "id": "h1",
                "status": "open",
                "title": "hypothesis",
                "success_metric": "accuracy",
                "target_delta": 0.1,
            }
        ],
        "experiments": [
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "iteration_id": "iter1",
            }
        ],
    }
    path = repo / ".autolab" / "backlog.yaml"
    path.write_text(yaml.safe_dump(backlog, sort_keys=False), encoding="utf-8")


def _write_agent_result(repo: Path) -> None:
    payload = {
        "status": "complete",
        "summary": "ok",
        "changed_files": [],
        "completion_token_seen": True,
    }
    path = repo / ".autolab" / "agent_result.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_design(repo: Path) -> None:
    payload = {
        "schema_version": "1.0",
        "id": "e1",
        "iteration_id": "iter1",
        "hypothesis_id": "h1",
        "entrypoint": {"module": "pkg.train", "args": {}},
        "compute": {"location": "local", "gpu_count": 0},
        "metrics": {
            "primary": {"name": "accuracy", "unit": "%", "mode": "maximize"},
            "secondary": [],
            "success_delta": "+1.0%",
            "aggregation": "mean",
            "baseline_comparison": "vs baseline",
        },
        "baselines": [{"name": "baseline", "description": "existing"}],
        "implementation_requirements": [
            {
                "requirement_id": "R1",
                "description": "Implement baseline training path",
                "scope_kind": "experiment",
                "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
            }
        ],
        "extract_parser": {
            "kind": "command",
            "command": "python3 -m scripts.extract_results --run-id {run_id} --iteration-path {iteration_path}",
        },
        "variants": [{"name": "proposed", "changes": {}}],
    }
    path = repo / "experiments" / "plan" / "iter1" / "design.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _seed_verification_summaries(repo: Path, *, count: int) -> list[str]:
    logs_dir = repo / ".autolab" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for idx in range(count):
        name = f"verification_00000000000000{idx:03d}_design.json"
        (logs_dir / name).write_text(
            json.dumps({"seed_index": idx}, indent=2) + "\n", encoding="utf-8"
        )
        names.append(name)
    return names


def _write_lock(repo: Path, *, state_path: Path, command: str = "autolab run") -> Path:
    now = commands_module._utc_now()
    payload = {
        "pid": 99999,
        "host": "test-host",
        "owner_uuid": "owner-test",
        "started_at": now,
        "last_heartbeat_at": now,
        "command": command,
        "state_file": str(state_path),
    }
    lock_path = repo / ".autolab" / "lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return lock_path


def test_verify_command_writes_summary_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)

    exit_code = commands_module.main(["verify", "--state-file", str(state_path)])

    assert exit_code == 0
    summaries = sorted((repo / ".autolab" / "logs").glob("verification_*.json"))
    assert summaries, "expected verification summary artifact"
    latest = json.loads(summaries[-1].read_text(encoding="utf-8"))
    assert latest["passed"] is True
    assert latest["stage_effective"] == "design"
    canonical = json.loads(
        (repo / ".autolab" / "verification_result.json").read_text(encoding="utf-8")
    )
    assert canonical["passed"] is True
    assert canonical["stage_effective"] == "design"
    handoff_payload = json.loads(
        (repo / ".autolab" / "handoff.json").read_text(encoding="utf-8")
    )
    assert handoff_payload["current_stage"] == "design"
    handoff_md_path = Path(handoff_payload["handoff_markdown_path"])
    assert handoff_md_path.exists()


def test_verify_command_prunes_old_summary_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)
    seeded_names = _seed_verification_summaries(repo, count=205)

    exit_code = commands_module.main(["verify", "--state-file", str(state_path)])

    assert exit_code == 0
    summaries = sorted((repo / ".autolab" / "logs").glob("verification_*.json"))
    assert len(summaries) == 200
    remaining_names = {path.name for path in summaries}
    assert all(name not in remaining_names for name in seeded_names[:6])
    assert all(name in remaining_names for name in seeded_names[6:])
    generated_names = [name for name in remaining_names if name not in seeded_names]
    assert len(generated_names) == 1


def test_verify_command_keeps_all_when_within_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)
    seeded_names = _seed_verification_summaries(repo, count=199)

    exit_code = commands_module.main(["verify", "--state-file", str(state_path)])

    assert exit_code == 0
    summaries = sorted((repo / ".autolab" / "logs").glob("verification_*.json"))
    assert len(summaries) == 200
    remaining_names = {path.name for path in summaries}
    assert all(name in remaining_names for name in seeded_names)
    generated_names = [name for name in remaining_names if name not in seeded_names]
    assert len(generated_names) == 1


def test_verify_command_continues_when_summary_prune_delete_fails(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)
    seeded_names = _seed_verification_summaries(repo, count=205)
    blocked_name = seeded_names[0]
    original_unlink = Path.unlink

    def _patched_unlink(path: Path, *args, **kwargs) -> None:
        if path.name == blocked_name:
            raise OSError("simulated unlink failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _patched_unlink)

    exit_code = commands_module.main(["verify", "--state-file", str(state_path)])

    assert exit_code == 0
    summaries = sorted((repo / ".autolab" / "logs").glob("verification_*.json"))
    assert len(summaries) == 201
    assert (repo / ".autolab" / "logs" / blocked_name).exists()


def test_run_with_verify_blocks_stage_transition_on_verification_failure(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    # Intentionally skip design.yaml to force verifier failure.

    exit_code = commands_module.main(
        [
            "run",
            "--state-file",
            str(state_path),
            "--verify",
            "--no-run-agent",
        ]
    )

    assert exit_code == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stage"] == "design"
    assert state["stage_attempt"] == 1


def test_run_fails_when_active_lock_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)
    lock_path = _write_lock(repo, state_path=state_path, command="autolab loop --auto")

    exit_code = commands_module.main(
        [
            "run",
            "--state-file",
            str(state_path),
            "--no-run-agent",
        ]
    )

    assert exit_code == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stage"] == "design"
    assert state["stage_attempt"] == 0
    assert lock_path.exists()


def test_loop_without_auto_fails_when_active_lock_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)
    lock_path = _write_lock(repo, state_path=state_path, command="autolab run")

    exit_code = commands_module.main(
        [
            "loop",
            "--state-file",
            str(state_path),
            "--max-iterations",
            "1",
            "--no-run-agent",
        ]
    )

    assert exit_code == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stage"] == "design"
    assert state["stage_attempt"] == 0
    assert lock_path.exists()


def test_loop_auto_continues_after_successful_non_terminal_implementation_wave(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_payload["stage"] = "implementation"
    state_path.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")

    call_count = 0

    def _scripted_run_once(
        _state_path: Path,
        _decision: str | None,
        *,
        run_agent_mode: str = "policy",
        verify_before_evaluate: bool = False,
        assistant: bool = False,
        auto_mode: bool = False,
        auto_decision: bool = False,
        strict_implementation_progress: bool = True,
    ) -> commands_module.RunOutcome:
        nonlocal call_count
        del (
            run_agent_mode,
            verify_before_evaluate,
            assistant,
            auto_mode,
            auto_decision,
            strict_implementation_progress,
        )
        call_count += 1
        if call_count == 1:
            return commands_module.RunOutcome(
                exit_code=0,
                transitioned=False,
                stage_before="implementation",
                stage_after="implementation",
                message="implementation wave 1/2 completed; next wave is 2",
            )
        return commands_module.RunOutcome(
            exit_code=0,
            transitioned=True,
            stage_before="implementation",
            stage_after="implementation_review",
            message="implementation checks passed",
        )

    monkeypatch.setattr(commands_module, "_run_once", _scripted_run_once)
    monkeypatch.setattr(
        commands_module,
        "_prepare_standard_commit_outcome",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        commands_module,
        "_try_auto_commit",
        lambda *_args, **_kwargs: "autocommit skipped",
    )

    exit_code = commands_module.main(
        [
            "loop",
            "--state-file",
            str(state_path),
            "--auto",
            "--max-hours",
            "1",
            "--max-iterations",
            "2",
            "--no-run-agent",
        ]
    )

    assert exit_code == 0
    assert call_count == 2


def test_skip_fails_when_active_lock_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)
    lock_path = _write_lock(repo, state_path=state_path, command="autolab run")

    exit_code = commands_module.main(
        [
            "skip",
            "--state-file",
            str(state_path),
            "--stage",
            "implementation",
            "--reason",
            "test",
        ]
    )

    assert exit_code == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stage"] == "design"
    assert state["stage_attempt"] == 0
    assert lock_path.exists()


def test_run_heartbeats_lock_during_long_execution(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)

    heartbeat_calls = 0
    original_heartbeat_lock = commands_module._heartbeat_lock

    def _counting_heartbeat(lock_path: Path) -> None:
        nonlocal heartbeat_calls
        heartbeat_calls += 1
        original_heartbeat_lock(lock_path)

    def _slow_run_once(
        _state_path: Path,
        _decision: str | None,
        *,
        run_agent_mode: str = "policy",
        verify_before_evaluate: bool = False,
        assistant: bool = False,
        auto_mode: bool = False,
        auto_decision: bool = False,
        strict_implementation_progress: bool = True,
    ) -> commands_module.RunOutcome:
        del (
            run_agent_mode,
            verify_before_evaluate,
            assistant,
            auto_mode,
            auto_decision,
            strict_implementation_progress,
        )
        time.sleep(0.05)
        return commands_module.RunOutcome(
            exit_code=0,
            transitioned=False,
            stage_before="design",
            stage_after="design",
            message="ok",
        )

    monkeypatch.setattr(commands_module, "_heartbeat_lock", _counting_heartbeat)
    monkeypatch.setattr(commands_module, "_run_once", _slow_run_once)
    monkeypatch.setattr(commands_module, "RUN_LOCK_HEARTBEAT_INTERVAL_SECONDS", 0.01)

    exit_code = commands_module.main(
        [
            "run",
            "--state-file",
            str(state_path),
            "--no-run-agent",
        ]
    )

    assert exit_code == 0
    assert heartbeat_calls >= 3


def test_run_blocks_on_stage_readiness_when_run_id_missing(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = repo / ".autolab" / "state.json"
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "extract_results",
        "stage_attempt": 0,
        "last_run_id": "",
        "pending_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    _write_backlog(repo)
    _write_agent_result(repo)

    exit_code = commands_module.main(
        [
            "run",
            "--state-file",
            str(state_path),
            "--no-run-agent",
        ]
    )

    assert exit_code == 1
    next_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert next_state["stage"] == "extract_results"
    assert next_state["stage_attempt"] == 1


def test_lock_status_reads_runtime_lock(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_lock(repo, state_path=state_path)

    exit_code = commands_module.main(
        ["lock", "status", "--state-file", str(state_path)]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert "autolab lock: active" in captured.out
    assert "pid: 99999" in captured.out


def test_lock_break_removes_runtime_lock(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    lock_path = _write_lock(repo, state_path=state_path)

    exit_code = commands_module.main(
        [
            "lock",
            "break",
            "--state-file",
            str(state_path),
            "--reason",
            "test",
        ]
    )

    assert exit_code == 0
    assert not lock_path.exists()


def test_verification_specs_skip_result_sanity_for_implementation_review(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "implementation_review",
        "stage_attempt": 0,
        "last_run_id": "",
        "pending_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }

    _stage, _requirements, command_specs = _build_verification_command_specs(
        repo,
        state,
        stage_override="implementation_review",
    )
    command_names = [name for name, _command in command_specs]
    assert "run_health" in command_names
    assert "result_sanity" not in command_names


def test_verification_specs_include_result_sanity_for_extract_results(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "extract_results",
        "stage_attempt": 0,
        "last_run_id": "run_001",
        "pending_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }

    _stage, _requirements, command_specs = _build_verification_command_specs(
        repo,
        state,
        stage_override="extract_results",
    )
    command_names = [name for name, _command in command_specs]
    assert "run_health" in command_names
    assert "result_sanity" in command_names


def test_verification_dry_run_iteration_placeholder_blocks_shell_injection(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)

    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert isinstance(policy, dict)
    policy["dry_run_command"] = (
        "{{python_bin}} -c \"print('dry-run-ok')\" --iteration <ITERATION_ID>"
    )
    policy["template_fill"] = {"enabled": False}
    policy["template_fill_by_stage"] = {}
    policy["requirements_by_stage"] = {
        "implementation": {
            "tests": False,
            "dry_run": True,
            "schema": False,
            "prompt_lint": False,
            "consistency": False,
            "env_smoke": False,
            "docs_target_update": False,
        }
    }
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    for verifier in (
        "registry_consistency.py",
        "prompt_registry_contract.py",
        "closed_experiment_guard.py",
        "implementation_plan_lint.py",
        "implementation_plan_contract.py",
    ):
        verifier_path = repo / ".autolab" / "verifiers" / verifier
        if verifier_path.exists():
            verifier_path.unlink()

    state = {
        "iteration_id": "iter1; touch autolab_poc #",
        "experiment_id": "e1",
        "stage": "implementation",
        "stage_attempt": 0,
        "last_run_id": "",
        "pending_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }

    passed, message, details = _run_verification_step_detailed(
        repo,
        state,
        stage_override="implementation",
    )

    assert passed is True, message
    assert "verification passed" in message
    assert (repo / "autolab_poc").exists() is False
    commands = details.get("commands", [])
    assert isinstance(commands, list)
    assert commands
    assert commands[0].get("name") == "dry_run"
