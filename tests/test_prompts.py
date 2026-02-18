from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from autolab.models import StageCheckError
from autolab.prompts import _render_stage_prompt


def _copy_scaffold(repo: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "autolab" / "scaffold" / ".autolab"
    target = repo / ".autolab"
    shutil.copytree(source, target, dirs_exist_ok=True)


def _write_state(repo: Path, *, stage: str) -> dict[str, object]:
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": stage,
        "stage_attempt": 0,
        "last_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


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


def test_render_design_prompt_accepts_required_hypothesis_id(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="design")
    _write_backlog(repo)

    template_path = repo / ".autolab" / "prompts" / "stage_design.md"
    bundle = _render_stage_prompt(repo, stage="design", state=state, template_path=template_path, runner_scope={})

    assert "{{hypothesis_id}}" not in bundle.prompt_text
    assert "hypothesis_id: h1" in bundle.prompt_text
    assert "python3 .autolab/verifiers/template_fill.py --stage design" in bundle.prompt_text


def test_render_prompt_rejects_legacy_literal_placeholder_tokens(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="hypothesis")
    _write_backlog(repo)

    template_path = repo / ".autolab" / "prompts" / "custom_legacy.md"
    template_path.write_text("# bad\nlegacy token: <ITERATION_ID>\n", encoding="utf-8")

    with pytest.raises(StageCheckError, match="unresolved placeholders"):
        _render_stage_prompt(repo, stage="hypothesis", state=state, template_path=template_path, runner_scope={})
