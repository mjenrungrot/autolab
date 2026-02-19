from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import autolab.commands as commands_module


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
    # Replace python_bin and configure a passing dry-run command for tests
    # (the default scaffold dry-run command intentionally fails).
    for idx, line in enumerate(policy_lines):
        if line.strip().startswith("python_bin:"):
            policy_lines[idx] = f'python_bin: "{sys.executable}"'
            break
    for idx, line in enumerate(policy_lines):
        if line.strip().startswith("dry_run_command:"):
            policy_lines[idx] = (
                'dry_run_command: "{{python_bin}} -c \\"print(\'golden iteration dry-run: OK\')\\""'
            )
            break
    policy_path.write_text("\n".join(policy_lines) + "\n", encoding="utf-8")


def _copy_golden_iteration(repo: Path) -> None:
    golden_root = Path(__file__).resolve().parents[1] / "examples" / "golden_iteration"
    shutil.copytree(
        golden_root / "experiments", repo / "experiments", dirs_exist_ok=True
    )
    shutil.copytree(golden_root / "paper", repo / "paper", dirs_exist_ok=True)
    shutil.copy2(
        golden_root / ".autolab" / "state.json", repo / ".autolab" / "state.json"
    )
    shutil.copy2(
        golden_root / ".autolab" / "backlog.yaml", repo / ".autolab" / "backlog.yaml"
    )


def _write_agent_result(repo: Path) -> None:
    payload = {
        "status": "complete",
        "summary": "golden fixture",
        "changed_files": [],
        "completion_token_seen": True,
    }
    path = repo / ".autolab" / "agent_result.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def test_golden_iteration_verify_passes_across_stages(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _copy_golden_iteration(repo)
    _write_agent_result(repo)
    state_path = repo / ".autolab" / "state.json"

    stages = [
        "hypothesis",
        "design",
        "implementation",
        "implementation_review",
        "launch",
        "extract_results",
        "update_docs",
        "decide_repeat",
    ]
    for stage in stages:
        exit_code = commands_module.main(
            [
                "verify",
                "--state-file",
                str(state_path),
                "--stage",
                stage,
            ]
        )
        assert exit_code == 0, f"expected stage '{stage}' to pass verification"


def test_golden_iteration_negative_fixture_fails_with_clear_error(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _copy_golden_iteration(repo)
    _write_agent_result(repo)
    state_path = repo / ".autolab" / "state.json"

    # Break the design schema contract.
    design_path = repo / "experiments" / "plan" / "iter_golden" / "design.yaml"
    broken = design_path.read_text(encoding="utf-8").replace(
        'schema_version: "1.0"\n', ""
    )
    design_path.write_text(broken, encoding="utf-8")

    exit_code = commands_module.main(
        [
            "verify",
            "--state-file",
            str(state_path),
            "--stage",
            "design",
        ]
    )
    assert exit_code == 1
