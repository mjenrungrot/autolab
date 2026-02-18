from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


pytest.importorskip("jsonschema")


def _copy_scaffold(repo: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "autolab" / "scaffold" / ".autolab"
    target = repo / ".autolab"
    shutil.copytree(source, target, dirs_exist_ok=True)


def _write_state(repo: Path, *, stage: str = "implementation_review") -> None:
    state = {
        "iteration_id": "iter1",
        "stage": stage,
        "stage_attempt": 0,
        "last_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    path = repo / ".autolab" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


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


def _write_agent_result(repo: Path) -> None:
    payload = {
        "status": "complete",
        "summary": "ok",
        "changed_files": [],
        "completion_token_seen": True,
    }
    path = repo / ".autolab" / "agent_result.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_design(repo: Path) -> None:
    payload = {
        "id": "d1",
        "iteration_id": "iter1",
        "hypothesis_id": "h1",
        "entrypoint": {"module": "pkg.train", "args": {}},
        "compute": {"location": "local", "gpu_count": 0},
        "metrics": {
            "primary": {"name": "accuracy", "unit": "%", "mode": "maximize"},
            "secondary": [],
            "success_delta": "+1.0%",
            "aggregation": "mean",
            "baseline_comparison": "vs baseline",
        },
        "baselines": [{"name": "baseline", "description": "existing"}],
        "variants": [{"name": "proposed", "changes": {}}],
    }
    path = repo / "experiments" / "iter1" / "design.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_review_result(repo: Path, *, include_docs_check: bool = True) -> None:
    required_checks = {
        "tests": "pass",
        "dry_run": "pass",
        "schema": "pass",
        "env_smoke": "pass",
    }
    if include_docs_check:
        required_checks["docs_target_update"] = "pass"
    payload = {
        "status": "pass",
        "blocking_findings": [],
        "required_checks": required_checks,
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    path = repo / "experiments" / "iter1" / "review_result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _run_schema_checks(repo: Path) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "schema_checks.py"
    return subprocess.run(
        [sys.executable, str(verifier)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def _setup_review_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)
    return repo


def test_schema_checks_pass_for_valid_review_payload(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_schema_checks_fail_when_required_check_key_missing(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=False)

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "docs_target_update" in result.stdout
