from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import autolab.commands as commands_module
import pytest
from autolab.update import UpdateResult


def _load_toml(path: Path) -> dict:
    payload: dict
    if sys.version_info >= (3, 11):
        import tomllib

        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    else:  # pragma: no cover
        import tomli  # type: ignore

        payload = tomli.loads(path.read_text(encoding="utf-8"))
    return payload


def test_status_docs_generate_and_policy_doctor_smoke(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    assert commands_module.main(["status", "--state-file", str(state_path)]) == 0
    assert (
        commands_module.main(["docs", "generate", "--state-file", str(state_path)]) == 0
    )
    assert (
        commands_module.main(["policy", "doctor", "--state-file", str(state_path)]) == 0
    )


def test_update_command_routes_to_handler(
    monkeypatch,
) -> None:
    captured: dict[str, Path] = {}

    def _fake_run_update(cwd: Path) -> UpdateResult:
        captured["cwd"] = cwd
        return UpdateResult(
            current_version="1.1.0",
            latest_tag="v1.1.1",
            upgraded=True,
            synced_scaffold=False,
            sync_skipped_reason="outside repo",
        )

    monkeypatch.setattr(commands_module, "run_update", _fake_run_update)

    exit_code = commands_module.main(["update"])

    assert exit_code == 0
    assert captured["cwd"] == Path.cwd()


def test_update_command_propagates_failure_exit_code(
    monkeypatch,
) -> None:
    def _raise_error(_cwd: Path) -> UpdateResult:
        raise RuntimeError("simulated update failure")

    monkeypatch.setattr(commands_module, "run_update", _raise_error)

    exit_code = commands_module.main(["update"])

    assert exit_code == 1


def test_package_data_contract_includes_registry_and_golden_fixtures() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = _load_toml(pyproject_path)

    package_data = (
        pyproject.get("tool", {})
        .get("setuptools", {})
        .get("package-data", {})
        .get("autolab", [])
    )
    assert isinstance(package_data, list)

    assert "scaffold/.autolab/workflow.yaml" in package_data
    assert "example_golden_iterations/README.md" in package_data
    assert (
        "example_golden_iterations/experiments/plan/iter_golden/runs/*/*.json"
        in package_data
    )


def test_console_script_contract_points_to_main_entrypoint() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = _load_toml(pyproject_path)
    scripts = pyproject.get("project", {}).get("scripts", {})
    assert isinstance(scripts, dict)
    assert scripts.get("autolab") == "autolab.__main__:main"


def test_installed_console_script_can_run_render(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    venv_dir = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    python_bin = bin_dir / "python"
    autolab_bin = bin_dir / "autolab"
    if os.name == "nt":
        autolab_bin = bin_dir / "autolab.exe"

    install_result = subprocess.run(
        [str(python_bin), "-m", "pip", "install", "--no-deps", "-e", str(repo_root)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert install_result.returncode == 0, install_result.stderr

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = workspace / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "iteration_id": "iter1",
                "experiment_id": "e1",
                "stage": "implementation",
                "stage_attempt": 0,
                "last_run_id": "",
                "pending_run_id": "",
                "sync_status": "na",
                "max_stage_attempts": 3,
                "max_total_iterations": 20,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    prompt_path = workspace / ".autolab" / "prompts" / "stage_implementation_runner.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(
        "# Stage: implementation\n\nstage: {{stage}}\niteration_id: {{iteration_id}}\n",
        encoding="utf-8",
    )
    audit_prompt_path = workspace / ".autolab" / "prompts" / "stage_implementation.md"
    audit_prompt_path.write_text(
        "# Stage: implementation (audit)\n\nstage: {{stage}}\niteration_id: {{iteration_id}}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(autolab_bin), "render", "--state-file", str(state_path)],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "stage: implementation" in result.stdout.lower()


def test_top_level_help_groups_commands_for_onboarding() -> None:
    help_text = commands_module._build_parser().format_help()
    assert "positional arguments:" in help_text
    assert "COMMAND" in help_text
    assert "  Getting started:" in help_text
    assert "  Run workflow:" in help_text
    assert "  Backlog steering:" in help_text
    assert "  Safety and policy:" in help_text
    assert "  Maintenance:" in help_text
    assert "init" in help_text
    assert "configure" in help_text
    assert "run" in help_text
    assert "loop" in help_text
    assert "tui" in help_text
    assert "render" in help_text
    assert "todo" in help_text
    assert "policy" in help_text
    assert "update" in help_text
    assert "report" in help_text
    assert "Record a human review decision" in help_text
    assert "Recommended onboarding flow:" in help_text


def test_review_subcommand_help_uses_human_review_terminology(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = commands_module._build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["review", "--help"])

    assert int(exc_info.value.code) == 0
    captured = capsys.readouterr()
    assert "--status {pass,retry,stop}" in captured.out
    assert "Human review decision:" in captured.out


def test_experiment_subcommand_help_includes_create(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = commands_module._build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["experiment", "--help"])

    assert int(exc_info.value.code) == 0
    captured = capsys.readouterr()
    assert "create" in captured.out
    assert "move" in captured.out


def test_experiment_create_help_shows_required_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = commands_module._build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["experiment", "create", "--help"])

    assert int(exc_info.value.code) == 0
    captured = capsys.readouterr()
    assert "--experiment-id EXPERIMENT_ID" in captured.out
    assert "--iteration-id ITERATION_ID" in captured.out
    assert "--hypothesis-id HYPOTHESIS_ID" in captured.out


def test_status_human_review_banner_mentions_human_review_decision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["stage"] = "human_review"
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    assert commands_module.main(["status", "--state-file", str(state_path)]) == 0
    captured = capsys.readouterr()
    assert "*** HUMAN REVIEW REQUIRED ***" in captured.out
    assert "record the human review decision" in captured.out


def test_packaged_golden_iteration_fixture_contract() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    packaged_root = repo_root / "src" / "autolab" / "example_golden_iterations"
    assert packaged_root.is_dir()

    packaged_files = sorted(
        path.relative_to(packaged_root)
        for path in packaged_root.rglob("*")
        if path.is_file()
    )
    expected_files = sorted(
        [
            Path(".autolab/backlog.yaml"),
            Path(".autolab/state.json"),
            Path("README.md"),
            Path("experiments/plan/iter_golden/analysis/summary.md"),
            Path("experiments/plan/iter_golden/decision_result.json"),
            Path("experiments/plan/iter_golden/design.yaml"),
            Path("experiments/plan/iter_golden/docs_update.md"),
            Path("experiments/plan/iter_golden/hypothesis.md"),
            Path("experiments/plan/iter_golden/implementation_plan.md"),
            Path("experiments/plan/iter_golden/implementation_review.md"),
            Path("experiments/plan/iter_golden/launch/run_local.sh"),
            Path("experiments/plan/iter_golden/review_result.json"),
            Path(
                "experiments/plan/iter_golden/runs/20260201T120000Z_demo/metrics.json"
            ),
            Path(
                "experiments/plan/iter_golden/runs/20260201T120000Z_demo/run_manifest.json"
            ),
            Path("paper/results.md"),
        ]
    )

    assert packaged_files == expected_files
