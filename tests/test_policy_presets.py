from __future__ import annotations

from pathlib import Path

import yaml

import autolab.commands as commands_module


def _load_policy(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_init_sets_dry_run_disabled_when_unconfigured(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    exit_code = commands_module.main(
        ["init", "--state-file", str(state_path), "--no-interactive"]
    )

    assert exit_code == 0
    policy = _load_policy(repo / ".autolab" / "verifier_policy.yaml")
    req = policy.get("requirements_by_stage", {})
    assert isinstance(req, dict)
    implementation = req.get("implementation", {})
    implementation_review = req.get("implementation_review", {})
    assert isinstance(implementation, dict)
    assert isinstance(implementation_review, dict)
    assert implementation.get("dry_run") is False
    assert implementation_review.get("dry_run") is False


def test_policy_apply_preset_local_dev(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )

    exit_code = commands_module.main(
        ["policy", "apply", "preset", "local_dev", "--state-file", str(state_path)]
    )

    assert exit_code == 0
    policy = _load_policy(repo / ".autolab" / "verifier_policy.yaml")
    resolution = policy.get("policy_resolution", {})
    assert isinstance(resolution, dict)
    assert resolution.get("default_preset") == "local_dev"
    assert policy.get("safe_automation_protected_files") is False
    prompt_lint = policy.get("prompt_lint", {})
    assert isinstance(prompt_lint, dict)
    assert prompt_lint.get("mode") == "warn"


def test_policy_apply_preset_ci_strict(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )

    exit_code = commands_module.main(
        ["policy", "apply", "preset", "ci_strict", "--state-file", str(state_path)]
    )

    assert exit_code == 0
    policy = _load_policy(repo / ".autolab" / "verifier_policy.yaml")
    resolution = policy.get("policy_resolution", {})
    assert isinstance(resolution, dict)
    assert resolution.get("default_preset") == "ci_strict"
    assert policy.get("safe_automation_protected_files") is True
    prompt_lint = policy.get("prompt_lint", {})
    assert isinstance(prompt_lint, dict)
    assert prompt_lint.get("mode") == "enforce"
    req = policy.get("requirements_by_stage", {})
    assert isinstance(req, dict)
    implementation = req.get("implementation", {})
    assert isinstance(implementation, dict)
    assert implementation.get("tests") is True
    assert implementation.get("dry_run") is True


def test_policy_apply_preset_experiment_search(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )

    exit_code = commands_module.main(
        [
            "policy",
            "apply",
            "preset",
            "experiment_search",
            "--state-file",
            str(state_path),
        ]
    )

    assert exit_code == 0
    policy = _load_policy(repo / ".autolab" / "verifier_policy.yaml")
    resolution = policy.get("policy_resolution", {})
    assert isinstance(resolution, dict)
    assert resolution.get("default_preset") == "experiment_search"
    assert policy.get("safe_automation_protected_files") is False
    assert policy.get("uat_surface_patterns") == []
    prompt_lint = policy.get("prompt_lint", {})
    assert isinstance(prompt_lint, dict)
    assert prompt_lint.get("mode") == "warn"
    autorun = policy.get("autorun", {})
    assert isinstance(autorun, dict)
    campaign = autorun.get("campaign", {})
    assert isinstance(campaign, dict)
    assert campaign.get("max_timeout_factor") == 3.0
    assert campaign.get("max_no_improvement_streak") == 6
    assert campaign.get("max_crash_streak_before_rethink") == 3


def test_policy_apply_preset_integration_change(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )

    exit_code = commands_module.main(
        [
            "policy",
            "apply",
            "preset",
            "integration_change",
            "--state-file",
            str(state_path),
        ]
    )

    assert exit_code == 0
    policy = _load_policy(repo / ".autolab" / "verifier_policy.yaml")
    resolution = policy.get("policy_resolution", {})
    assert isinstance(resolution, dict)
    assert resolution.get("default_preset") == "integration_change"
    assert policy.get("safe_automation_protected_files") is True
    prompt_lint = policy.get("prompt_lint", {})
    assert isinstance(prompt_lint, dict)
    assert prompt_lint.get("mode") == "enforce"
    autorun = policy.get("autorun", {})
    assert isinstance(autorun, dict)
    campaign = autorun.get("campaign", {})
    assert isinstance(campaign, dict)
    assert campaign.get("max_timeout_factor") == 2.0
    assert campaign.get("max_no_improvement_streak") == 3
    guardrails = autorun.get("guardrails", {})
    assert isinstance(guardrails, dict)
    assert guardrails.get("max_same_decision_streak") == 4
