"""LLM artifact robustness tests.

Systematically tests what happens when the LLM writes **malformed** content
(wrong types, invalid enums, missing nested fields, semantic contradictions,
null values, etc.) at every pipeline stage.

~75 parametrized test cases verify every validator gracefully handles every
category of LLM mistake — returning retry (exit_code=1, stage_attempt+1)
or block (for decide_repeat) rather than crashing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml

from autolab.__main__ import (
    _run_once,
)
from autolab.launch_runtime import LaunchExecutionResult


# ---------------------------------------------------------------------------
# Helpers (replicated from test_pipeline_coverage.py — module-private there)
# ---------------------------------------------------------------------------


def _make_state(
    *,
    stage: str = "hypothesis",
    stage_attempt: int = 0,
    max_stage_attempts: int = 3,
    iteration_id: str = "iter_test_001",
    experiment_id: str = "e1",
    assistant_mode: str = "off",
    current_task_id: str = "",
    task_cycle_stage: str = "select",
    repeat_guard: dict[str, Any] | None = None,
    last_run_id: str = "",
) -> dict[str, Any]:
    return {
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "stage": stage,
        "stage_attempt": stage_attempt,
        "last_run_id": last_run_id,
        "pending_run_id": "",
        "sync_status": "",
        "max_stage_attempts": max_stage_attempts,
        "max_total_iterations": 10,
        "assistant_mode": assistant_mode,
        "current_task_id": current_task_id,
        "task_cycle_stage": task_cycle_stage,
        "repeat_guard": repeat_guard
        or {
            "last_decision": "",
            "same_decision_streak": 0,
            "last_open_task_count": -1,
            "no_progress_decisions": 0,
            "update_docs_cycle_count": 0,
            "last_verification_passed": False,
        },
        "task_change_baseline": {},
    }


def _write_state(repo: Path, state: dict[str, Any]) -> Path:
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state_path


def _read_state(repo: Path) -> dict[str, Any]:
    state_path = repo / ".autolab" / "state.json"
    return json.loads(state_path.read_text(encoding="utf-8"))


def _write_backlog(
    repo: Path,
    *,
    experiment_id: str = "e1",
    iteration_id: str = "iter_test_001",
    status: str = "open",
    hypothesis_status: str = "open",
) -> None:
    backlog = {
        "hypotheses": [
            {
                "id": "h1",
                "status": hypothesis_status,
                "title": "Test hypothesis",
                "success_metric": "metric",
                "target_delta": 0.0,
            },
        ],
        "experiments": [
            {
                "id": experiment_id,
                "hypothesis_id": "h1",
                "status": status,
                "iteration_id": iteration_id,
            },
        ],
    }
    path = repo / ".autolab" / "backlog.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(backlog, sort_keys=False), encoding="utf-8")


def _write_policy(
    repo: Path,
    *,
    guardrails: dict[str, Any] | None = None,
    launch: dict[str, Any] | None = None,
    require_dry_run: bool = False,
    require_tests: bool = False,
) -> None:
    policy: dict[str, Any] = {
        "test_command": "true",
        "dry_run_command": "true",
        "require_tests": require_tests,
        "require_dry_run": require_dry_run,
        "require_env_smoke": False,
        "require_docs_target_update": False,
        "launch": launch
        or {
            "execute": True,
            "local_timeout_seconds": 900,
            "slurm_submit_timeout_seconds": 30,
        },
        "template_fill": {"enabled": False},
        "agent_runner": {"enabled": False, "stages": []},
        "autorun": {
            "guardrails": guardrails
            or {
                "max_same_decision_streak": 3,
                "max_no_progress_decisions": 2,
                "max_update_docs_cycles": 3,
                "on_breach": "human_review",
            },
            "auto_commit": {"mode": "off"},
            "meaningful_change": {
                "require_implementation_progress": False,
                "require_git_for_progress": False,
                "on_non_git_behavior": "warn_and_continue",
                "require_verification": False,
                "exclude_paths": [],
            },
        },
    }
    path = repo / ".autolab" / "verifier_policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")


def _write_todo_state(repo: Path, tasks: dict[str, Any] | None = None) -> None:
    payload = {"version": 1, "next_order": 1, "tasks": tasks or {}}
    path = repo / ".autolab" / "todo_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_todo_md(repo: Path, content: str = "") -> None:
    path = repo / "docs" / "todo.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_iteration(repo: Path, iteration_id: str = "iter_test_001") -> Path:
    iteration_dir = repo / "experiments" / "plan" / iteration_id
    iteration_dir.mkdir(parents=True, exist_ok=True)
    return iteration_dir


# --- Per-stage artifact seeders ---


def _seed_hypothesis(iteration_dir: Path) -> None:
    (iteration_dir / "hypothesis.md").write_text(
        (
            "# Hypothesis Statement\n\n"
            "## Primary Metric\n"
            "PrimaryMetric: accuracy; Unit: %; Success: baseline +2.0\n\n"
            "- metric: accuracy\n"
            "- metric_mode: maximize\n"
            "- target_delta: 2.0\n"
            "- criteria: improve top-1 accuracy by at least 2.0 points\n"
        ),
        encoding="utf-8",
    )


def _seed_design(iteration_dir: Path, iteration_id: str = "iter_test_001") -> None:
    design = {
        "id": "d1",
        "iteration_id": iteration_id,
        "hypothesis_id": "h1",
        "entrypoint": {"module": "train", "args": {}},
        "compute": {"location": "local", "gpus": 0},
        "metrics": ["loss"],
        "baselines": [{"name": "baseline1", "value": 1.0}],
    }
    (iteration_dir / "design.yaml").write_text(
        yaml.safe_dump(design, sort_keys=False), encoding="utf-8"
    )


def _seed_implementation(iteration_dir: Path) -> None:
    (iteration_dir / "implementation_plan.md").write_text(
        "# Implementation\nStep 1.", encoding="utf-8"
    )


def _seed_review_pass(iteration_dir: Path) -> None:
    (iteration_dir / "implementation_review.md").write_text(
        "# Review\nLGTM.", encoding="utf-8"
    )
    review = {
        "status": "pass",
        "blocking_findings": [],
        "required_checks": {
            "tests": "pass",
            "dry_run": "skip",
            "schema": "pass",
            "env_smoke": "skip",
            "docs_target_update": "skip",
        },
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    (iteration_dir / "review_result.json").write_text(
        json.dumps(review, indent=2), encoding="utf-8"
    )


def _seed_review_retry(iteration_dir: Path) -> None:
    (iteration_dir / "implementation_review.md").write_text(
        "# Review\nNeeds work.", encoding="utf-8"
    )
    review = {
        "status": "needs_retry",
        "blocking_findings": ["issue1"],
        "required_checks": {
            "tests": "fail",
            "dry_run": "skip",
            "schema": "pass",
            "env_smoke": "skip",
            "docs_target_update": "skip",
        },
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    (iteration_dir / "review_result.json").write_text(
        json.dumps(review, indent=2), encoding="utf-8"
    )


def _seed_launch(iteration_dir: Path, run_id: str = "run_001") -> None:
    if not (iteration_dir / "design.yaml").exists():
        _seed_design(iteration_dir, iteration_id=iteration_dir.name)
    if not (iteration_dir / "review_result.json").exists():
        _seed_review_pass(iteration_dir)
    launch_dir = iteration_dir / "launch"
    launch_dir.mkdir(parents=True, exist_ok=True)
    (launch_dir / "run_local.sh").write_text(
        '#!/bin/bash\nmkdir -p "runs/$AUTOLAB_RUN_ID"\necho \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\necho run',
        encoding="utf-8",
    )
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "iteration_id": iteration_dir.name,
        "launch_mode": "local",
        "host_mode": "local",
        "command": "bash launch/run_local.sh",
        "resource_request": {},
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:05:00Z",
        },
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _seed_extract(iteration_dir: Path, run_id: str = "run_001") -> None:
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "iteration_id": iteration_dir.name,
        "launch_mode": "local",
        "host_mode": "local",
        "command": "bash launch/run_local.sh",
        "resource_request": {},
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:05:00Z",
        },
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    metrics = {
        "iteration_id": iteration_dir.name,
        "run_id": run_id,
        "status": "completed",
        "primary_metric": {
            "name": "loss",
            "value": 0.5,
            "delta_vs_baseline": 0.0,
        },
        "baseline_results": [],
        "variant_results": [],
    }
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )


def _seed_update_docs(iteration_dir: Path) -> None:
    (iteration_dir / "docs_update.md").write_text(
        (
            "# Docs Update\n\n"
            "- run_id: run_001\n"
            "- metrics artifact: runs/run_001/metrics.json\n"
            "- manifest artifact: runs/run_001/run_manifest.json\n"
        ),
        encoding="utf-8",
    )
    analysis_dir = iteration_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "summary.md").write_text("# Summary\nResults.", encoding="utf-8")


def _seed_decision_result(
    iteration_dir: Path,
    *,
    decision: str = "design",
    rationale: str = "More refinement needed.",
    evidence: list[dict[str, str]] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "decision": decision,
        "rationale": rationale,
        "evidence": evidence
        or [
            {
                "source": "metrics",
                "pointer": "runs/run_001/metrics.json",
                "summary": "Target not met",
            }
        ],
        "risks": [],
    }
    (iteration_dir / "decision_result.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


# --- Repo scaffolding ---


def _setup_repo(
    tmp_path: Path,
    *,
    hypothesis_status: str = "open",
    backlog_experiment_status: str = "open",
    require_dry_run: bool = False,
    require_tests: bool = False,
    **state_kwargs: Any,
) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = _make_state(**state_kwargs)
    state_path = _write_state(repo, state)
    _write_backlog(
        repo,
        experiment_id=state.get("experiment_id", "e1"),
        iteration_id=state.get("iteration_id", "iter_test_001"),
        status=backlog_experiment_status,
        hypothesis_status=hypothesis_status,
    )
    _write_policy(repo, require_dry_run=require_dry_run, require_tests=require_tests)
    _write_todo_state(repo)
    _write_todo_md(repo)
    iteration_dir = _seed_iteration(repo, state.get("iteration_id", "iter_test_001"))
    return repo, state_path, iteration_dir


def _run(state_path: Path, **kwargs: Any):
    kwargs.setdefault("run_agent_mode", "force_off")
    kwargs.setdefault("strict_implementation_progress", False)
    with mock.patch("autolab.run_standard._generate_run_id", return_value="run_001"):
        return _run_once(state_path, kwargs.pop("decision", None), **kwargs)


# --- Builder helpers ---


def _make_valid_design(**overrides: Any) -> dict[str, Any]:
    design: dict[str, Any] = {
        "id": "d1",
        "iteration_id": "iter_test_001",
        "hypothesis_id": "h1",
        "entrypoint": {"module": "train", "args": {}},
        "compute": {"location": "local", "gpus": 0},
        "metrics": ["loss"],
        "baselines": [{"name": "baseline1", "value": 1.0}],
    }
    design.update(overrides)
    return design


def _make_valid_review(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": "pass",
        "blocking_findings": [],
        "required_checks": {
            "tests": "pass",
            "dry_run": "skip",
            "schema": "pass",
            "env_smoke": "skip",
            "docs_target_update": "skip",
        },
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    base.update(overrides)
    return base


def _make_valid_decision(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "schema_version": "1.0",
        "decision": "design",
        "rationale": "More refinement needed.",
        "evidence": [
            {
                "source": "metrics",
                "pointer": "runs/run_001/metrics.json",
                "summary": "Target not met",
            }
        ],
        "risks": [],
    }
    base.update(overrides)
    return base


def _make_valid_manifest(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "run_id": "run_001",
        "iteration_id": "iter_test_001",
        "launch_mode": "local",
        "host_mode": "local",
        "command": "bash launch/run_local.sh",
        "resource_request": {},
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:05:00Z",
        },
    }
    base.update(overrides)
    return base


def _assert_retry(outcome: Any, repo: Path, stage: str) -> None:
    """Assert the pipeline retried (exit_code=1, stage unchanged, attempt+1)."""
    assert outcome.exit_code == 1, (
        f"Expected retry exit_code=1, got {outcome.exit_code}: {outcome.message}"
    )
    persisted = _read_state(repo)
    assert persisted["stage"] == stage, (
        f"Expected stage={stage}, got {persisted['stage']}"
    )
    assert persisted["stage_attempt"] >= 1, (
        f"Expected stage_attempt>=1, got {persisted['stage_attempt']}"
    )


def _assert_block(outcome: Any, stage: str = "decide_repeat") -> None:
    """Assert the pipeline blocked (exit_code=0, no transition, stays at stage)."""
    assert outcome.exit_code == 0, (
        f"Expected block exit_code=0, got {outcome.exit_code}"
    )
    assert not outcome.transitioned, "Expected no transition"
    assert outcome.stage_after == stage, (
        f"Expected stage_after={stage}, got {outcome.stage_after}"
    )


# ---------------------------------------------------------------------------
# 1. TestHypothesisMalformedArtifacts
# ---------------------------------------------------------------------------


class TestHypothesisMalformedArtifacts:
    """Malformed hypothesis.md → StageCheckError → retry."""

    @pytest.mark.parametrize(
        "case_id, content",
        [
            (
                "missing_metric",
                "# Hypothesis\n- target_delta: 2.0\n- criteria: improve accuracy\n- metric_mode: maximize\n",
            ),
            (
                "missing_target_delta",
                "# Hypothesis\n- metric: accuracy\n- criteria: improve accuracy\n- metric_mode: maximize\n",
            ),
            (
                "missing_criteria",
                "# Hypothesis\n- metric: accuracy\n- target_delta: 2.0\n- metric_mode: maximize\n",
            ),
            (
                "invalid_metric_mode",
                "# Hypothesis\n- metric: accuracy\n- target_delta: 2.0\n- criteria: improve accuracy\n- metric_mode: supersize\n",
            ),
            (
                "missing_metric_mode",
                "# Hypothesis\n- metric: accuracy\n- target_delta: 2.0\n- criteria: improve accuracy\n",
            ),
            (
                "non_numeric_delta",
                "# Hypothesis\n- metric: accuracy\n- target_delta: big improvement\n- criteria: improve accuracy\n- metric_mode: maximize\n",
            ),
            (
                "positive_delta_minimize",
                "# Hypothesis\n- metric: loss\n- target_delta: 5.0\n- criteria: reduce loss\n- metric_mode: minimize\n",
            ),
            (
                "negative_delta_maximize",
                "# Hypothesis\n- metric: accuracy\n- target_delta: -3.0\n- criteria: improve accuracy\n- metric_mode: maximize\n",
            ),
        ],
        ids=[
            "missing_metric",
            "missing_target_delta",
            "missing_criteria",
            "invalid_metric_mode",
            "missing_metric_mode",
            "non_numeric_delta",
            "positive_delta_minimize",
            "negative_delta_maximize",
        ],
    )
    def test_hypothesis_malformed(
        self, tmp_path: Path, case_id: str, content: str
    ) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="hypothesis")
        (it_dir / "hypothesis.md").write_text(content, encoding="utf-8")

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "hypothesis")


# ---------------------------------------------------------------------------
# 2. TestDesignMalformedArtifacts
# ---------------------------------------------------------------------------


class TestDesignMalformedArtifacts:
    """Malformed design.yaml → StageCheckError → retry."""

    @pytest.mark.parametrize(
        "case_id, write_fn",
        [
            (
                "yaml_parse_error",
                lambda it: (it / "design.yaml").write_text(
                    "{not: [valid yaml", encoding="utf-8"
                ),
            ),
            (
                "not_a_mapping",
                lambda it: (it / "design.yaml").write_text(
                    "- item1\n- item2\n", encoding="utf-8"
                ),
            ),
            (
                "missing_required_keys",
                lambda it: (it / "design.yaml").write_text(
                    yaml.safe_dump(
                        {"id": "d1", "iteration_id": "iter_test_001"}, sort_keys=False
                    ),
                    encoding="utf-8",
                ),
            ),
            (
                "iteration_id_mismatch",
                lambda it: (it / "design.yaml").write_text(
                    yaml.safe_dump(
                        _make_valid_design(iteration_id="wrong_iter"), sort_keys=False
                    ),
                    encoding="utf-8",
                ),
            ),
            (
                "entrypoint_string",
                lambda it: (it / "design.yaml").write_text(
                    yaml.safe_dump(
                        _make_valid_design(entrypoint="train.py"), sort_keys=False
                    ),
                    encoding="utf-8",
                ),
            ),
            (
                "entrypoint_empty_module",
                lambda it: (it / "design.yaml").write_text(
                    yaml.safe_dump(
                        _make_valid_design(entrypoint={"module": "", "args": {}}),
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                ),
            ),
            (
                "entrypoint_missing_module_key",
                lambda it: (it / "design.yaml").write_text(
                    yaml.safe_dump(
                        _make_valid_design(entrypoint={"args": {"lr": 0.01}}),
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                ),
            ),
            (
                "compute_string",
                lambda it: (it / "design.yaml").write_text(
                    yaml.safe_dump(
                        _make_valid_design(compute="local"), sort_keys=False
                    ),
                    encoding="utf-8",
                ),
            ),
            (
                "compute_empty_location",
                lambda it: (it / "design.yaml").write_text(
                    yaml.safe_dump(
                        _make_valid_design(compute={"location": ""}),
                        sort_keys=False,
                    ),
                    encoding="utf-8",
                ),
            ),
            (
                "baselines_empty_list",
                lambda it: (it / "design.yaml").write_text(
                    yaml.safe_dump(_make_valid_design(baselines=[]), sort_keys=False),
                    encoding="utf-8",
                ),
            ),
        ],
        ids=[
            "yaml_parse_error",
            "not_a_mapping",
            "missing_required_keys",
            "iteration_id_mismatch",
            "entrypoint_string",
            "entrypoint_empty_module",
            "entrypoint_missing_module_key",
            "compute_string",
            "compute_empty_location",
            "baselines_empty_list",
        ],
    )
    def test_design_malformed(
        self, tmp_path: Path, case_id: str, write_fn: Any
    ) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="design")
        write_fn(it_dir)

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "design")


# ---------------------------------------------------------------------------
# 3. TestImplementationMalformedArtifacts
# ---------------------------------------------------------------------------


class TestImplementationMalformedArtifacts:
    """Malformed implementation_plan.md → StageCheckError → retry."""

    @pytest.mark.parametrize(
        "case_id, content, need_dry_run",
        [
            ("empty_file", "", False),
            ("whitespace_only", "   \n  \n  ", False),
            (
                "missing_dry_run_heading",
                "# Implementation Plan\nStep 1: do things.\nStep 2: verify.",
                True,
            ),
            (
                "wrong_heading_level",
                "# Implementation Plan\n# Dry Run\nRun locally first.",
                True,
            ),
        ],
        ids=[
            "empty_file",
            "whitespace_only",
            "missing_dry_run_heading",
            "wrong_heading_level",
        ],
    )
    def test_implementation_malformed(
        self,
        tmp_path: Path,
        case_id: str,
        content: str,
        need_dry_run: bool,
    ) -> None:
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="implementation", require_dry_run=need_dry_run
        )
        (it_dir / "implementation_plan.md").write_text(content, encoding="utf-8")

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "implementation")


# ---------------------------------------------------------------------------
# 4. TestReviewResultMalformedArtifacts
# ---------------------------------------------------------------------------


class TestReviewResultMalformedArtifacts:
    """Malformed review artifacts → StageCheckError → retry."""

    @pytest.mark.parametrize(
        "case_id, review_md, review_json, need_require_tests",
        [
            # 1. implementation_review.md is empty
            ("review_md_empty", "", None, False),
            # 2. Invalid JSON in review_result.json
            (
                "json_parse_error",
                "# Review\nOK.",
                "{bad json",
                False,
            ),
            # 3. JSON is not a dict
            (
                "json_not_dict",
                "# Review\nOK.",
                "[1, 2, 3]",
                False,
            ),
            # 4. Missing status key
            (
                "missing_status",
                "# Review\nOK.",
                json.dumps(
                    {
                        "blocking_findings": [],
                        "required_checks": {
                            "tests": "pass",
                            "dry_run": "skip",
                            "schema": "pass",
                            "env_smoke": "skip",
                            "docs_target_update": "skip",
                        },
                        "reviewed_at": "2026-01-01T00:00:00Z",
                    }
                ),
                False,
            ),
            # 5. Missing required_checks key
            (
                "missing_required_checks",
                "# Review\nOK.",
                json.dumps(
                    {
                        "status": "pass",
                        "blocking_findings": [],
                        "reviewed_at": "2026-01-01T00:00:00Z",
                    }
                ),
                False,
            ),
            # 6. Missing reviewed_at key
            (
                "missing_reviewed_at",
                "# Review\nOK.",
                json.dumps(
                    {
                        "status": "pass",
                        "blocking_findings": [],
                        "required_checks": {
                            "tests": "pass",
                            "dry_run": "skip",
                            "schema": "pass",
                            "env_smoke": "skip",
                            "docs_target_update": "skip",
                        },
                    }
                ),
                False,
            ),
            # 7. Invalid status enum
            (
                "invalid_status_enum",
                "# Review\nOK.",
                json.dumps(_make_valid_review(status="approved")),
                False,
            ),
            # 8. Null status → coerced to "None"
            (
                "null_status",
                "# Review\nOK.",
                json.dumps(_make_valid_review(status=None)),
                False,
            ),
            # 9. required_checks is not a dict
            (
                "checks_not_dict",
                "# Review\nOK.",
                json.dumps(_make_valid_review(required_checks="all pass")),
                False,
            ),
            # 10. required_checks missing sub-keys
            (
                "checks_missing_sub_keys",
                "# Review\nOK.",
                json.dumps(_make_valid_review(required_checks={"tests": "pass"})),
                False,
            ),
            # 11. Individual check has invalid status
            (
                "check_invalid_status",
                "# Review\nOK.",
                json.dumps(
                    _make_valid_review(
                        required_checks={
                            "tests": "passed",
                            "dry_run": "skip",
                            "schema": "pass",
                            "env_smoke": "skip",
                            "docs_target_update": "skip",
                        }
                    )
                ),
                False,
            ),
            # 12. status=pass but tests=fail (when policy requires tests)
            (
                "pass_with_fail_check",
                "# Review\nOK.",
                json.dumps(
                    _make_valid_review(
                        required_checks={
                            "tests": "fail",
                            "dry_run": "skip",
                            "schema": "pass",
                            "env_smoke": "skip",
                            "docs_target_update": "skip",
                        }
                    )
                ),
                True,
            ),
        ],
        ids=[
            "review_md_empty",
            "json_parse_error",
            "json_not_dict",
            "missing_status",
            "missing_required_checks",
            "missing_reviewed_at",
            "invalid_status_enum",
            "null_status",
            "checks_not_dict",
            "checks_missing_sub_keys",
            "check_invalid_status",
            "pass_with_fail_check",
        ],
    )
    def test_review_malformed(
        self,
        tmp_path: Path,
        case_id: str,
        review_md: str,
        review_json: str | None,
        need_require_tests: bool,
    ) -> None:
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="implementation_review",
            require_tests=need_require_tests,
        )
        (it_dir / "implementation_review.md").write_text(review_md, encoding="utf-8")
        if review_json is not None:
            (it_dir / "review_result.json").write_text(review_json, encoding="utf-8")

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "implementation_review")


# ---------------------------------------------------------------------------
# 5. TestLaunchMalformedArtifacts
# ---------------------------------------------------------------------------


class TestLaunchMalformedArtifacts:
    """Malformed launch artifacts → StageCheckError → retry."""

    def test_no_launch_scripts(self, tmp_path: Path) -> None:
        """Neither run_local.sh nor run_slurm.sbatch."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_design(it_dir)
        _seed_review_pass(it_dir)

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "launch")

    def test_empty_local_script(self, tmp_path: Path) -> None:
        """run_local.sh exists but is empty."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_design(it_dir)
        _seed_review_pass(it_dir)
        launch_dir = it_dir / "launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        (launch_dir / "run_local.sh").write_text("", encoding="utf-8")

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "launch")

    def test_empty_slurm_script(self, tmp_path: Path) -> None:
        """run_slurm.sbatch exists but empty, no local script."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_design(it_dir, iteration_id="iter_test_001")
        _seed_review_pass(it_dir)
        launch_dir = it_dir / "launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        (launch_dir / "run_slurm.sbatch").write_text("", encoding="utf-8")

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "launch")

    def test_review_not_pass(self, tmp_path: Path) -> None:
        """review_result.json exists with status=needs_retry."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_design(it_dir)
        # Working launch script so runtime succeeds
        launch_dir = it_dir / "launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        (launch_dir / "run_local.sh").write_text(
            '#!/bin/bash\nmkdir -p "runs/$AUTOLAB_RUN_ID"\n'
            'echo \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\n',
            encoding="utf-8",
        )
        # Review with needs_retry
        _seed_review_retry(it_dir)

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "launch")

    def test_design_unparseable(self, tmp_path: Path) -> None:
        """design.yaml is invalid YAML."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_review_pass(it_dir)
        (it_dir / "design.yaml").write_text("{not: [valid yaml", encoding="utf-8")
        launch_dir = it_dir / "launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        (launch_dir / "run_local.sh").write_text(
            "#!/bin/bash\necho ok", encoding="utf-8"
        )

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "launch")

    def test_host_mode_mismatch(self, tmp_path: Path) -> None:
        """design says slurm, manifest says local → mismatch error."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        # Design says slurm
        design = _make_valid_design(compute={"location": "slurm", "gpus": 0})
        (it_dir / "design.yaml").write_text(
            yaml.safe_dump(design, sort_keys=False), encoding="utf-8"
        )
        _seed_review_pass(it_dir)
        # Launch scripts (both present to pass validation)
        launch_dir = it_dir / "launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        (launch_dir / "run_local.sh").write_text(
            "#!/bin/bash\necho ok", encoding="utf-8"
        )
        (launch_dir / "run_slurm.sbatch").write_text(
            "#!/bin/bash\necho ok", encoding="utf-8"
        )
        # Pre-seed manifest with host_mode=local
        run_dir = it_dir / "runs" / "run_001"
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = _make_valid_manifest(host_mode="local", launch_mode="local")
        (run_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        # Mock the runtime to succeed without modifying the manifest
        def fake_launch_runtime(
            repo_root: Path, *, state: dict[str, Any]
        ) -> LaunchExecutionResult:
            state["last_run_id"] = "run_001"
            state["sync_status"] = "completed"
            return LaunchExecutionResult(
                run_id="run_001", sync_status="completed", changed_files=()
            )

        with mock.patch(
            "autolab.run_standard._execute_launch_runtime",
            side_effect=fake_launch_runtime,
        ):
            outcome = _run(state_path)

        _assert_retry(outcome, repo, "launch")


# ---------------------------------------------------------------------------
# 6. TestSlurmMonitorMalformedArtifacts
# ---------------------------------------------------------------------------


class TestSlurmMonitorMalformedArtifacts:
    """Malformed run_manifest.json at slurm_monitor → StageCheckError → retry."""

    @pytest.mark.parametrize(
        "case_id, manifest_content",
        [
            ("manifest_invalid_json", "{bad"),
            ("manifest_not_dict", "[1, 2]"),
        ],
        ids=["manifest_invalid_json", "manifest_not_dict"],
    )
    def test_slurm_monitor_manifest_malformed(
        self,
        tmp_path: Path,
        case_id: str,
        manifest_content: str,
    ) -> None:
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="slurm_monitor", last_run_id="run_001"
        )
        run_dir = it_dir / "runs" / "run_001"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_manifest.json").write_text(manifest_content, encoding="utf-8")

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "slurm_monitor")


# ---------------------------------------------------------------------------
# 7. TestExtractResultsMalformedArtifacts
# ---------------------------------------------------------------------------


class TestExtractResultsMalformedArtifacts:
    """Malformed extract artifacts → StageCheckError → retry."""

    @pytest.mark.parametrize(
        "case_id, manifest_content, metrics_content",
        [
            # 1. Manifest is invalid JSON
            ("manifest_invalid_json", "{bad", None),
            # 2. Manifest is not a dict
            ("manifest_not_dict", "[1, 2]", None),
            # 3. Manifest missing artifact_sync_to_local
            (
                "manifest_missing_sync",
                json.dumps({"run_id": "run_001", "iteration_id": "iter_test_001"}),
                None,
            ),
            # 4. artifact_sync_to_local is not a dict
            (
                "sync_not_dict",
                json.dumps(
                    {
                        "run_id": "run_001",
                        "iteration_id": "iter_test_001",
                        "artifact_sync_to_local": "ok",
                    }
                ),
                None,
            ),
            # 5. sync status is not success-like
            (
                "sync_status_invalid",
                json.dumps(
                    {
                        "run_id": "run_001",
                        "iteration_id": "iter_test_001",
                        "artifact_sync_to_local": {"status": "downloading"},
                    }
                ),
                None,
            ),
            # 6. Metrics is invalid JSON (valid manifest)
            (
                "metrics_invalid_json",
                json.dumps(_make_valid_manifest()),
                "not json",
            ),
            # 7. Metrics is not a dict (valid manifest)
            (
                "metrics_not_dict",
                json.dumps(_make_valid_manifest()),
                "[1, 2]",
            ),
            # 8. Metrics is empty dict (valid manifest)
            (
                "metrics_empty_dict",
                json.dumps(_make_valid_manifest()),
                "{}",
            ),
        ],
        ids=[
            "manifest_invalid_json",
            "manifest_not_dict",
            "manifest_missing_sync",
            "sync_not_dict",
            "sync_status_invalid",
            "metrics_invalid_json",
            "metrics_not_dict",
            "metrics_empty_dict",
        ],
    )
    def test_extract_malformed(
        self,
        tmp_path: Path,
        case_id: str,
        manifest_content: str,
        metrics_content: str | None,
    ) -> None:
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="extract_results", last_run_id="run_001"
        )
        run_dir = it_dir / "runs" / "run_001"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_manifest.json").write_text(manifest_content, encoding="utf-8")
        if metrics_content is not None:
            (run_dir / "metrics.json").write_text(metrics_content, encoding="utf-8")

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "extract_results")


# ---------------------------------------------------------------------------
# 8. TestUpdateDocsMalformedArtifacts
# ---------------------------------------------------------------------------


class TestUpdateDocsMalformedArtifacts:
    """Malformed update_docs artifacts → StageCheckError → retry."""

    def test_docs_update_missing(self, tmp_path: Path) -> None:
        """No docs_update.md file."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="update_docs", last_run_id="run_001"
        )
        # Seed valid analysis/summary.md
        analysis_dir = it_dir / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        (analysis_dir / "summary.md").write_text("# Summary\nOK.", encoding="utf-8")
        # Seed valid run manifest (for later checks)
        _seed_extract(it_dir)

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "update_docs")

    def test_docs_update_empty(self, tmp_path: Path) -> None:
        """docs_update.md is empty."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="update_docs", last_run_id="run_001"
        )
        (it_dir / "docs_update.md").write_text("", encoding="utf-8")
        analysis_dir = it_dir / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        (analysis_dir / "summary.md").write_text("# Summary\nOK.", encoding="utf-8")
        _seed_extract(it_dir)

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "update_docs")

    def test_summary_missing(self, tmp_path: Path) -> None:
        """No analysis/summary.md."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="update_docs", last_run_id="run_001"
        )
        (it_dir / "docs_update.md").write_text(
            "# Docs\nSome content.", encoding="utf-8"
        )
        _seed_extract(it_dir)

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "update_docs")

    def test_summary_empty(self, tmp_path: Path) -> None:
        """analysis/summary.md is empty."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="update_docs", last_run_id="run_001"
        )
        (it_dir / "docs_update.md").write_text(
            "# Docs\nSome content.", encoding="utf-8"
        )
        analysis_dir = it_dir / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        (analysis_dir / "summary.md").write_text("", encoding="utf-8")
        _seed_extract(it_dir)

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "update_docs")

    def test_missing_run_id_ref(self, tmp_path: Path) -> None:
        """docs_update.md doesn't contain the run_id."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="update_docs", last_run_id="run_001"
        )
        (it_dir / "docs_update.md").write_text(
            "# Docs Update\nSome content without run reference.", encoding="utf-8"
        )
        analysis_dir = it_dir / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        (analysis_dir / "summary.md").write_text("# Summary\nOK.", encoding="utf-8")
        _seed_extract(it_dir)

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "update_docs")

    def test_missing_metrics_ref(self, tmp_path: Path) -> None:
        """Has run_id but no metrics.json mention."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="update_docs", last_run_id="run_001"
        )
        (it_dir / "docs_update.md").write_text(
            "# Docs Update\nResults for run_001 are good.", encoding="utf-8"
        )
        analysis_dir = it_dir / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        (analysis_dir / "summary.md").write_text("# Summary\nOK.", encoding="utf-8")
        _seed_extract(it_dir)

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "update_docs")

    def test_missing_manifest_ref(self, tmp_path: Path) -> None:
        """Has run_id + metrics.json but no run_manifest.json mention."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="update_docs", last_run_id="run_001"
        )
        (it_dir / "docs_update.md").write_text(
            "# Docs Update\nResults for run_001 in metrics.json.", encoding="utf-8"
        )
        analysis_dir = it_dir / "analysis"
        analysis_dir.mkdir(parents=True, exist_ok=True)
        (analysis_dir / "summary.md").write_text("# Summary\nOK.", encoding="utf-8")
        _seed_extract(it_dir)

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "update_docs")

    @pytest.mark.parametrize(
        "case_id, manifest_content",
        [
            ("manifest_invalid_json", "{bad"),
            ("manifest_not_dict", "[1, 2]"),
        ],
        ids=["manifest_invalid_json", "manifest_not_dict"],
    )
    def test_manifest_malformed(
        self,
        tmp_path: Path,
        case_id: str,
        manifest_content: str,
    ) -> None:
        """run_manifest.json exists but cannot be parsed as an object."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="update_docs", last_run_id="run_001"
        )
        _seed_update_docs(it_dir)
        run_dir = it_dir / "runs" / "run_001"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run_manifest.json").write_text(manifest_content, encoding="utf-8")

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "update_docs")


# ---------------------------------------------------------------------------
# 9. TestDecideRepeatMalformedArtifacts
# ---------------------------------------------------------------------------


class TestDecideRepeatMalformedArtifacts:
    """Malformed decision_result.json → _decision_from_artifact returns (None, error)
    → pipeline blocks (exit_code=0, transitioned=False, stage stays decide_repeat).
    """

    @pytest.mark.parametrize(
        "case_id, content, error_fragment",
        [
            # 1. Invalid JSON
            ("invalid_json", "{bad json", "not valid JSON"),
            # 2. Not a dict
            ("not_dict", '"just a string"', "must contain a JSON object"),
            # 3. Missing decision key
            (
                "missing_decision",
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "rationale": "Some reason.",
                        "evidence": [
                            {
                                "source": "metrics",
                                "pointer": "x",
                                "summary": "y",
                            }
                        ],
                    }
                ),
                "decision must be one of",
            ),
            # 4. Invalid decision enum
            (
                "invalid_decision_enum",
                json.dumps(_make_valid_decision(decision="implementation")),
                "decision must be one of",
            ),
            # 5. Empty decision string
            (
                "empty_decision",
                json.dumps(_make_valid_decision(decision="")),
                "decision must be one of",
            ),
            # 6. Null decision
            (
                "null_decision",
                json.dumps(_make_valid_decision(decision=None)),
                "decision must be one of",
            ),
            # 7. Missing rationale
            (
                "missing_rationale",
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "decision": "design",
                        "evidence": [
                            {
                                "source": "metrics",
                                "pointer": "x",
                                "summary": "y",
                            }
                        ],
                    }
                ),
                "non-empty rationale",
            ),
            # 8. Empty rationale
            (
                "empty_rationale",
                json.dumps(_make_valid_decision(rationale="")),
                "non-empty rationale",
            ),
            # 9. Empty evidence list
            (
                "empty_evidence",
                json.dumps(_make_valid_decision(evidence=[])),
                "non-empty 'evidence'",
            ),
            # 10. Evidence item is wrong type (int not dict)
            (
                "evidence_wrong_type",
                json.dumps(_make_valid_decision(evidence=[42])),
                "evidence[0] must be a dict",
            ),
            # 11. Evidence item missing 'source'
            (
                "evidence_missing_source",
                json.dumps(
                    _make_valid_decision(evidence=[{"pointer": "x", "summary": "y"}])
                ),
                "non-empty string 'source'",
            ),
            # 12. Evidence item empty pointer
            (
                "evidence_empty_pointer",
                json.dumps(
                    _make_valid_decision(
                        evidence=[{"source": "m", "pointer": "", "summary": "y"}]
                    )
                ),
                "non-empty string 'pointer'",
            ),
            # 13. Evidence item null summary
            (
                "evidence_null_summary",
                json.dumps(
                    _make_valid_decision(
                        evidence=[{"source": "m", "pointer": "x", "summary": None}]
                    )
                ),
                "non-empty string 'summary'",
            ),
            # 14. Evidence is not a list
            (
                "evidence_not_list",
                json.dumps(_make_valid_decision(evidence="some text")),
                "non-empty 'evidence' list",
            ),
        ],
        ids=[
            "invalid_json",
            "not_dict",
            "missing_decision",
            "invalid_decision_enum",
            "empty_decision",
            "null_decision",
            "missing_rationale",
            "empty_rationale",
            "empty_evidence",
            "evidence_wrong_type",
            "evidence_missing_source",
            "evidence_empty_pointer",
            "evidence_null_summary",
            "evidence_not_list",
        ],
    )
    def test_decide_repeat_malformed(
        self,
        tmp_path: Path,
        case_id: str,
        content: str,
        error_fragment: str,
    ) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")
        (it_dir / "decision_result.json").write_text(content, encoding="utf-8")

        outcome = _run(state_path)

        _assert_block(outcome, "decide_repeat")
        # Verify the error message contains the expected fragment
        assert error_fragment in outcome.message, (
            f"Expected '{error_fragment}' in message: {outcome.message}"
        )


# ---------------------------------------------------------------------------
# 10. TestGracefulHandlingNoCrash
# ---------------------------------------------------------------------------


class TestGracefulHandlingNoCrash:
    """Cross-stage edge cases — no unhandled exceptions."""

    def test_null_bytes_hypothesis(self, tmp_path: Path) -> None:
        """Null bytes in hypothesis.md → retry (not crash)."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="hypothesis")
        (it_dir / "hypothesis.md").write_bytes(
            b"# Hypothesis\x00\n- metric: accuracy\n\x00- target_delta: 2.0\n"
        )

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "hypothesis")

    def test_bom_in_design(self, tmp_path: Path) -> None:
        """UTF-8 BOM prefix in YAML → retry or pass (not crash)."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="design")
        valid_design = yaml.safe_dump(_make_valid_design(), sort_keys=False)
        # Prepend UTF-8 BOM
        (it_dir / "design.yaml").write_text("\ufeff" + valid_design, encoding="utf-8")

        outcome = _run(state_path)

        # Either passes (BOM handled) or retries (BOM breaks parsing); never crashes
        if outcome.exit_code == 0:
            assert outcome.stage_after == "implementation"
        else:
            assert outcome.exit_code == 1
            persisted = _read_state(repo)
            assert persisted["stage"] == "design"

    def test_very_long_json(self, tmp_path: Path) -> None:
        """100KB review_result.json → retry (not crash)."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="implementation_review")
        (it_dir / "implementation_review.md").write_text(
            "# Review\nOK.", encoding="utf-8"
        )
        # Intentionally invalid status so validation fails (not crashes)
        huge_review = _make_valid_review(status="x" * 100_000)
        (it_dir / "review_result.json").write_text(
            json.dumps(huge_review), encoding="utf-8"
        )

        outcome = _run(state_path)

        _assert_retry(outcome, repo, "implementation_review")

    def test_extra_fields_design(self, tmp_path: Path) -> None:
        """Valid design + hallucinated fields → PASS (extra fields ignored)."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="design")
        design = _make_valid_design(
            hallucinated_field="value",
            extra_data={"nested": True},
            llm_note="This field doesn't exist in the schema",
        )
        (it_dir / "design.yaml").write_text(
            yaml.safe_dump(design, sort_keys=False), encoding="utf-8"
        )

        outcome = _run(state_path)

        assert outcome.exit_code == 0
        assert outcome.stage_after == "implementation"

    def test_extra_fields_review(self, tmp_path: Path) -> None:
        """Valid review + hallucinated fields → PASS (extra fields ignored)."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="implementation_review")
        (it_dir / "implementation_review.md").write_text(
            "# Review\nLGTM.", encoding="utf-8"
        )
        review = _make_valid_review(
            confidence_score=0.95,
            llm_reasoning="Everything looks good",
            extra_nested={"important": False},
        )
        (it_dir / "review_result.json").write_text(
            json.dumps(review, indent=2), encoding="utf-8"
        )

        outcome = _run(state_path)

        assert outcome.exit_code == 0
        assert outcome.stage_after == "launch"

    def test_extra_fields_decision(self, tmp_path: Path) -> None:
        """Valid decision + hallucinated fields → PASS (transitions)."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")
        decision = _make_valid_decision(
            confidence=0.9,
            alternative_considered="hypothesis",
            llm_note="Extra field",
        )
        (it_dir / "decision_result.json").write_text(
            json.dumps(decision, indent=2), encoding="utf-8"
        )

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "design"
