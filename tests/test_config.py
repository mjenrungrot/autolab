from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from autolab.config import (
    _load_agent_runner_config,
    _load_guardrail_config,
    _load_launch_execute_policy,
    _load_launch_runtime_config,
    _load_meaningful_change_config,
    _load_plan_execution_config,
    _load_protected_files,
    _resolve_policy_python_bin,
    _load_slurm_lifecycle_strict_policy,
    _load_strict_mode_config,
)
from autolab.models import StageCheckError


def test_load_guardrail_config_reads_max_generated_todo_tasks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy = {
        "autorun": {
            "guardrails": {
                "max_same_decision_streak": 3,
                "max_no_progress_decisions": 2,
                "max_update_docs_cycles": 3,
                "max_generated_todo_tasks": 11,
                "on_breach": "human_review",
            }
        }
    }
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    guardrails = _load_guardrail_config(repo)

    assert guardrails.max_generated_todo_tasks == 11


def test_load_guardrail_config_reads_stalled_blocker_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy = {
        "autorun": {
            "guardrails": {
                "max_stalled_blocker_cycles": 7,
            }
        }
    }
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    guardrails = _load_guardrail_config(repo)
    assert guardrails.max_stalled_blocker_cycles == 7


def test_load_meaningful_change_config_defaults_include_review_artifact_filter(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    config = _load_meaningful_change_config(repo)

    assert config.require_non_review_progress_in_implementation_cycle is True
    assert "**/implementation_review.md" in config.implementation_cycle_exclude_paths
    assert "**/review_result.json" in config.implementation_cycle_exclude_paths


def test_load_meaningful_change_config_reads_custom_cycle_filters(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy = {
        "autorun": {
            "meaningful_change": {
                "require_non_review_progress_in_implementation_cycle": False,
                "implementation_cycle_exclude_paths": [
                    "a.md",
                    "b.json",
                ],
            }
        }
    }
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    config = _load_meaningful_change_config(repo)
    assert config.require_non_review_progress_in_implementation_cycle is False
    assert config.implementation_cycle_exclude_paths == ("a.md", "b.json")


def test_load_protected_files_applies_safe_profile() -> None:
    policy = {
        "protected_files": [".autolab/state.json"],
        "safe_automation_protected_files": True,
        "protected_file_profiles": {
            "safe_automation": [
                ".autolab/prompts/**",
                ".autolab/schemas/**",
            ]
        },
    }
    protected = _load_protected_files(policy)
    assert ".autolab/state.json" in protected
    assert ".autolab/prompts/**" in protected
    assert ".autolab/schemas/**" in protected


def test_load_protected_files_auto_mode_applies_safe_profile_even_when_toggle_false() -> (
    None
):
    policy = {
        "protected_files": [".autolab/state.json"],
        "safe_automation_protected_files": False,
        "protected_file_profiles": {
            "safe_automation": [
                ".autolab/prompts/**",
                ".autolab/schemas/**",
                ".autolab/verifiers/**",
            ]
        },
    }

    protected = _load_protected_files(policy, auto_mode=True)

    assert ".autolab/state.json" in protected
    assert ".autolab/prompts/**" in protected
    assert ".autolab/schemas/**" in protected
    assert ".autolab/verifiers/**" in protected


def test_load_strict_mode_config_auto_mode_defaults_human_review_for_stop_when_unset(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy = {
        "autorun": {
            "strict_mode": {
                "forbid_auto_stop": False,
            }
        }
    }
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    strict = _load_strict_mode_config(repo, auto_mode=True)

    assert strict.require_human_review_for_stop is True


def test_load_strict_mode_config_auto_mode_respects_explicit_override(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy = {
        "autorun": {
            "strict_mode": {
                "forbid_auto_stop": False,
                "require_human_review_for_stop": False,
            }
        }
    }
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    strict = _load_strict_mode_config(repo, auto_mode=True)

    assert strict.require_human_review_for_stop is False


def test_load_launch_execute_policy_defaults_true(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _load_launch_execute_policy(repo) is True


def test_load_launch_execute_policy_reads_false(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text("launch:\n  execute: false\n", encoding="utf-8")

    assert _load_launch_execute_policy(repo) is False


def test_load_launch_runtime_config_defaults(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    config = _load_launch_runtime_config(repo)

    assert config.execute is True
    assert config.local_timeout_seconds == 900.0
    assert config.slurm_submit_timeout_seconds == 30.0


def test_load_launch_runtime_config_reads_custom_values(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        (
            "launch:\n"
            "  execute: true\n"
            "  local_timeout_seconds: 120\n"
            "  slurm_submit_timeout_seconds: 45\n"
        ),
        encoding="utf-8",
    )

    config = _load_launch_runtime_config(repo)

    assert config.execute is True
    assert config.local_timeout_seconds == 120.0
    assert config.slurm_submit_timeout_seconds == 45.0


def test_load_launch_runtime_config_non_positive_timeout_falls_back_to_defaults(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        (
            "launch:\n"
            "  execute: true\n"
            "  local_timeout_seconds: 0\n"
            "  slurm_submit_timeout_seconds: -1\n"
        ),
        encoding="utf-8",
    )

    config = _load_launch_runtime_config(repo)

    assert config.local_timeout_seconds == 900.0
    assert config.slurm_submit_timeout_seconds == 30.0


def test_load_slurm_lifecycle_strict_policy_defaults_true(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _load_slurm_lifecycle_strict_policy(repo) is True


def test_load_slurm_lifecycle_strict_policy_reads_false(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text("slurm:\n  lifecycle_strict: false\n", encoding="utf-8")

    assert _load_slurm_lifecycle_strict_policy(repo) is False


def test_resolve_policy_python_bin_defaults_to_current_interpreter() -> None:
    assert _resolve_policy_python_bin({}) == sys.executable


def test_resolve_policy_python_bin_normalizes_generic_python_binaries() -> None:
    assert _resolve_policy_python_bin({"python_bin": "python3"}) == sys.executable
    assert _resolve_policy_python_bin({"python_bin": "python"}) == sys.executable


def test_resolve_policy_python_bin_respects_explicit_custom_binary() -> None:
    assert (
        _resolve_policy_python_bin({"python_bin": "/usr/bin/python3"})
        == "/usr/bin/python3"
    )


def test_load_agent_runner_config_defaults_to_codex_dangerous(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    runner = _load_agent_runner_config(repo)

    assert runner.runner == "codex"
    assert "--dangerously-bypass-approvals-and-sandbox" in runner.command
    assert runner.codex_dangerously_bypass_approvals_and_sandbox is True


def test_load_agent_runner_config_uses_codex_dangerous_preset_from_policy(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy = {
        "agent_runner": {
            "enabled": True,
            "runner": "codex",
            "codex_dangerously_bypass_approvals_and_sandbox": True,
        }
    }
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    runner = _load_agent_runner_config(repo)

    assert runner.runner == "codex"
    assert "--dangerously-bypass-approvals-and-sandbox" in runner.command
    assert runner.codex_dangerously_bypass_approvals_and_sandbox is True


def test_load_agent_runner_config_uses_codex_dangerous_preset_from_env(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("AUTOLAB_CODEX_ALLOW_DANGEROUS", "true")
    repo = tmp_path / "repo"
    repo.mkdir()
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy = {"agent_runner": {"enabled": True, "runner": "codex"}}
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    runner = _load_agent_runner_config(repo)

    assert runner.runner == "codex"
    assert "--dangerously-bypass-approvals-and-sandbox" in runner.command
    assert runner.codex_dangerously_bypass_approvals_and_sandbox is True


def _write_plan_execution_policy(repo: Path, implementation: dict[str, object]) -> None:
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy = {"plan_execution": {"implementation": implementation}}
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")


def test_load_plan_execution_config_preserves_explicit_zero_retry_values(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_plan_execution_policy(
        repo,
        {
            "enabled": True,
            "run_unit": "wave",
            "max_parallel_tasks": 1,
            "task_retry_max": 0,
            "wave_retry_max": 0,
            "failure_mode": "finish_wave_then_stop",
            "on_wave_retry_exhausted": "human_review",
            "require_verification_commands": True,
        },
    )

    implementation = _load_plan_execution_config(repo).implementation

    assert implementation.max_parallel_tasks == 1
    assert implementation.task_retry_max == 0
    assert implementation.wave_retry_max == 0


def test_load_plan_execution_config_rejects_zero_max_parallel_tasks(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_plan_execution_policy(
        repo,
        {
            "enabled": True,
            "run_unit": "wave",
            "max_parallel_tasks": 0,
            "task_retry_max": 0,
            "wave_retry_max": 0,
            "failure_mode": "finish_wave_then_stop",
            "on_wave_retry_exhausted": "human_review",
            "require_verification_commands": True,
        },
    )

    with pytest.raises(
        StageCheckError,
        match="plan_execution\\.implementation\\.max_parallel_tasks must be >= 1",
    ):
        _load_plan_execution_config(repo)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("max_parallel_tasks", "oops"),
        ("task_retry_max", "oops"),
        ("wave_retry_max", "oops"),
    ),
)
def test_load_plan_execution_config_invalid_integer_fields_raise_clean_error(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    implementation: dict[str, object] = {
        "enabled": True,
        "run_unit": "wave",
        "max_parallel_tasks": 1,
        "task_retry_max": 0,
        "wave_retry_max": 0,
        "failure_mode": "finish_wave_then_stop",
        "on_wave_retry_exhausted": "human_review",
        "require_verification_commands": True,
    }
    implementation[field] = value
    _write_plan_execution_policy(repo, implementation)

    with pytest.raises(
        StageCheckError,
        match=rf"plan_execution\.implementation\.{field} must be an integer",
    ):
        _load_plan_execution_config(repo)
