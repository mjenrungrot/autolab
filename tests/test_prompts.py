from __future__ import annotations

import json
import shutil
from pathlib import Path

import re

import pytest
import yaml

from autolab.constants import PROMPT_TOKEN_PATTERN
from autolab.models import StageCheckError
from autolab.prompts import (
    _extract_hypothesis_target_delta,
    _parse_signed_delta,
    _suggest_decision_from_metrics,
    _target_comparison_text,
    _render_stage_prompt,
)

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


# ---------------------------------------------------------------------------
# _parse_signed_delta tests (Item 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("80 +5.0%", 5.0),
        ("2.0 -0.3", -0.3),
        ("5.0", 5.0),
        ("+10", 10.0),
        ("-2.5%", -2.5),
        ("", None),
    ],
)
def test_parse_signed_delta(text: str, expected: float | None) -> None:
    result = _parse_signed_delta(text)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert abs(result - expected) < 1e-6


# ---------------------------------------------------------------------------
# _suggest_decision_from_metrics tests (Item 1)
# ---------------------------------------------------------------------------


def _setup_metrics_repo(
    tmp_path: Path,
    *,
    hypothesis_target_delta: str = "5.0",
    metrics_delta: float = 6.0,
    metric_mode: str = "maximize",
) -> tuple[Path, dict[str, object]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "decide_repeat",
        "stage_attempt": 0,
        "last_run_id": "run_001",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    backlog = {
        "experiments": [{"id": "e1", "hypothesis_id": "h1", "status": "open", "iteration_id": "iter1"}],
    }
    (repo / ".autolab" / "backlog.yaml").write_text(yaml.safe_dump(backlog), encoding="utf-8")

    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "hypothesis.md").write_text(
        f"# Hypothesis\n- target_delta: {hypothesis_target_delta}\n",
        encoding="utf-8",
    )
    design = {
        "schema_version": "1.0",
        "id": "e1",
        "iteration_id": "iter1",
        "hypothesis_id": "h1",
        "entrypoint": {"module": "x"},
        "compute": {"location": "local"},
        "metrics": {
            "primary": {"name": "accuracy", "unit": "%", "mode": metric_mode},
            "secondary": [],
            "success_delta": "+5.0",
        },
        "baselines": [{"name": "b", "description": "b"}],
    }
    (iteration_dir / "design.yaml").write_text(yaml.safe_dump(design), encoding="utf-8")
    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = {
        "status": "complete",
        "primary_metric": {
            "name": "accuracy",
            "value": 90.0,
            "delta_vs_baseline": metrics_delta,
        },
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    return repo, state


def test_suggest_decision_from_metrics_returns_stop_when_target_met(tmp_path: Path) -> None:
    repo, state = _setup_metrics_repo(tmp_path, hypothesis_target_delta="5.0", metrics_delta=6.0)
    result, evidence = _suggest_decision_from_metrics(repo, state)
    assert result == "stop"
    assert isinstance(evidence, dict)
    assert "comparison" in evidence


def test_suggest_decision_from_metrics_returns_design_when_target_not_met(tmp_path: Path) -> None:
    repo, state = _setup_metrics_repo(tmp_path, hypothesis_target_delta="10.0", metrics_delta=6.0)
    result, evidence = _suggest_decision_from_metrics(repo, state)
    assert result == "design"
    assert isinstance(evidence, dict)
    assert "comparison" in evidence


# ---------------------------------------------------------------------------
# _target_comparison_text with metric_mode tests (Item 4)
# ---------------------------------------------------------------------------


def test_target_comparison_maximize_met() -> None:
    payload = {"primary_metric": {"name": "accuracy", "delta_vs_baseline": 6.0}}
    _comp, suggestion = _target_comparison_text(
        metrics_payload=payload, hypothesis_target_delta=5.0,
        design_target_delta="", run_id="run_001", metric_mode="maximize",
    )
    assert "stop" in suggestion


def test_target_comparison_maximize_not_met() -> None:
    payload = {"primary_metric": {"name": "accuracy", "delta_vs_baseline": 3.0}}
    _comp, suggestion = _target_comparison_text(
        metrics_payload=payload, hypothesis_target_delta=5.0,
        design_target_delta="", run_id="run_001", metric_mode="maximize",
    )
    assert "design" in suggestion


def test_target_comparison_minimize_met() -> None:
    payload = {"primary_metric": {"name": "loss", "delta_vs_baseline": -3.0}}
    _comp, suggestion = _target_comparison_text(
        metrics_payload=payload, hypothesis_target_delta=-2.0,
        design_target_delta="", run_id="run_001", metric_mode="minimize",
    )
    assert "stop" in suggestion


def test_target_comparison_minimize_not_met() -> None:
    payload = {"primary_metric": {"name": "loss", "delta_vs_baseline": -1.0}}
    _comp, suggestion = _target_comparison_text(
        metrics_payload=payload, hypothesis_target_delta=-2.0,
        design_target_delta="", run_id="run_001", metric_mode="minimize",
    )
    assert "design" in suggestion


def test_suggest_decision_from_metrics_minimize_mode(tmp_path: Path) -> None:
    repo, state = _setup_metrics_repo(
        tmp_path, hypothesis_target_delta="-2.0", metrics_delta=-3.0, metric_mode="minimize",
    )
    result, evidence = _suggest_decision_from_metrics(repo, state)
    assert result == "stop"
    assert isinstance(evidence, dict)
    assert evidence.get("metric_mode") == "minimize"
