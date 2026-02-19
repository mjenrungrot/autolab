from __future__ import annotations

from pathlib import Path

import yaml

from autolab.config import (
    _load_guardrail_config,
    _load_launch_execute_policy,
    _load_protected_files,
    _load_slurm_lifecycle_strict_policy,
    _load_strict_mode_config,
)


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
