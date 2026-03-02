from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

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


def _write_state(repo: Path, *, stage: str = "design") -> Path:
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": stage,
        "stage_attempt": 0,
        "last_run_id": "",
        "pending_run_id": "",
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


def test_render_uses_current_stage_and_prints_prompt(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "stage: design" in captured.out.lower()


def test_render_stage_override_does_not_mutate_state(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)
    before = json.loads(state_path.read_text(encoding="utf-8"))

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--stage", "implementation"]
    )

    captured = capsys.readouterr()
    after = json.loads(state_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert captured.err == ""
    assert "stage: implementation" in captured.out.lower()
    assert after == before


def test_render_context_appends_separator_and_json(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--context"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    separator = "----- AUTOLAB CONTEXT JSON -----\n"
    assert separator in captured.out
    _, context_text = captured.out.split(separator, 1)
    context = json.loads(context_text)
    assert context["stage"] == "design"
    assert context["iteration_id"] == "iter1"


def test_render_does_not_write_rendered_artifacts(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    rendered_dir = repo / ".autolab" / "prompts" / "rendered"
    if rendered_dir.exists():
        shutil.rmtree(rendered_dir)

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    _captured = capsys.readouterr()
    assert exit_code == 0
    assert not (rendered_dir / "design.md").exists()
    assert not (rendered_dir / "design.context.json").exists()


def test_render_fails_when_prompt_template_missing(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)
    (repo / ".autolab" / "prompts" / "stage_design.md").unlink()

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "autolab render: ERROR" in captured.err
    assert "stage prompt is missing" in captured.err


def test_render_fails_when_stage_requires_missing_run_id(
    tmp_path: Path, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="launch")
    _write_backlog(repo)

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "autolab render: ERROR" in captured.err
    assert "requires a resolved run_id" in captured.err


def test_render_fails_for_invalid_stage_override(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--stage", "not_a_real_stage"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "autolab render: ERROR" in captured.err
    assert "no stage prompt mapping is defined" in captured.err


def test_render_fails_when_state_stage_is_invalid_without_override(
    tmp_path: Path, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_payload["stage"] = "not_a_real_stage"
    state_path.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "autolab render: ERROR" in captured.err
    assert "state.stage must be one of" in captured.err


def test_render_fails_when_state_file_is_missing(capsys, tmp_path: Path) -> None:
    missing_state_path = tmp_path / "missing" / "state.json"

    exit_code = commands_module.main(
        ["render", "--state-file", str(missing_state_path)]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "autolab render: ERROR" in captured.err
    assert "state file not found" in captured.err


def test_render_fails_when_state_file_is_malformed(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{bad json", encoding="utf-8")

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "autolab render: ERROR" in captured.err
    assert "state file is not valid JSON" in captured.err


def test_render_entrypoint_via_python_module(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    src_path = str(repo_root / "src")
    env["PYTHONPATH"] = (
        f"{src_path}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else src_path
    )

    result = subprocess.run(
        [sys.executable, "-m", "autolab", "render", "--state-file", str(state_path)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    assert "stage: design" in result.stdout.lower()
    assert result.stderr == ""
