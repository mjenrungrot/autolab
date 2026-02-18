from __future__ import annotations

import json
import shutil
from pathlib import Path

import re

import pytest
import yaml

from autolab.constants import PROMPT_TOKEN_PATTERN
from autolab.models import StageCheckError
from autolab.prompts import _render_stage_prompt

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAFFOLD_PROMPTS_DIR = _REPO_ROOT / "src" / "autolab" / "scaffold" / ".autolab" / "prompts"
_SKILLS_DIR = _REPO_ROOT / "src" / "autolab" / "skills"
_ALL_PROMPT_MDS = sorted(_SCAFFOLD_PROMPTS_DIR.rglob("*.md"))

_ASCII_ENFORCED_MDS: list[Path] = []
_ASCII_ENFORCED_MDS.extend(_ALL_PROMPT_MDS)
_ASCII_ENFORCED_MDS.extend(sorted(_SKILLS_DIR.rglob("*.md")))
_readme = _REPO_ROOT / "README.md"
if _readme.exists():
    _ASCII_ENFORCED_MDS.append(_readme)


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


@pytest.mark.parametrize(
    "stage",
    [
        "hypothesis",
        "design",
        "implementation",
        "implementation_review",
        "launch",
        "extract_results",
        "update_docs",
        "decide_repeat",
    ],
)
def test_render_scaffold_prompts_have_no_unresolved_tokens(tmp_path: Path, stage: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage=stage)
    if stage in {"extract_results", "update_docs"}:
        state["last_run_id"] = "run_001"
    _write_backlog(repo)

    template_path = repo / ".autolab" / "prompts" / f"stage_{stage}.md"
    bundle = _render_stage_prompt(repo, stage=stage, state=state, template_path=template_path, runner_scope={})

    unresolved_tokens = {match.group(1).strip() for match in PROMPT_TOKEN_PATTERN.finditer(bundle.prompt_text)}
    assert not unresolved_tokens
    assert "<ITERATION_ID>" not in bundle.prompt_text
    assert "## Runtime Stage Context" in bundle.prompt_text


_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")


@pytest.mark.parametrize(
    "md_path",
    _ASCII_ENFORCED_MDS,
    ids=[str(p.relative_to(_REPO_ROOT)) for p in _ASCII_ENFORCED_MDS],
)
def test_prompt_markdown_contains_only_ascii(md_path: Path) -> None:
    """All prompt, skill, and README markdown files must use standard ASCII only (no Unicode)."""
    content = md_path.read_text(encoding="utf-8")
    violations: list[str] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        for match in _NON_ASCII_RE.finditer(line):
            char = match.group()
            violations.append(
                f"  {md_path.name}:{lineno} col {match.start() + 1}: "
                f"U+{ord(char):04X} {repr(char)}"
            )
    assert not violations, (
        f"Non-ASCII characters found in {md_path.name}:\n" + "\n".join(violations)
    )
