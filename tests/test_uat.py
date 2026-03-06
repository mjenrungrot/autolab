from __future__ import annotations

import json
from pathlib import Path

import pytest

from autolab.models import StageCheckError
from autolab.uat import parse_uat_markdown
from autolab.validators import _validate_review_result


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _review_result_payload() -> dict[str, object]:
    return {
        "status": "pass",
        "blocking_findings": [],
        "required_checks": {
            "tests": "pass",
            "dry_run": "pass",
            "schema": "pass",
            "env_smoke": "pass",
            "docs_target_update": "pass",
        },
        "reviewed_at": "2026-03-05T00:00:00Z",
    }


def _plan_approval_payload() -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "generated_at": "2026-03-05T00:00:00Z",
        "iteration_id": "iter1",
        "status": "approved",
        "requires_approval": True,
        "plan_hash": "plan-hash-1",
        "risk_fingerprint": "risk-fingerprint-1",
        "trigger_reasons": ["project_wide_tasks_present"],
        "counts": {
            "tasks_total": 1,
            "waves_total": 1,
            "project_wide_tasks": 1,
            "project_wide_unique_paths": 1,
            "observed_retries": 0,
            "stage_attempt": 0,
        },
        "reviewed_by": "reviewer",
        "reviewed_at": "2026-03-05T00:00:30Z",
        "notes": "",
        "source_paths": {
            "plan_contract": ".autolab/plan_contract.json",
            "plan_graph": ".autolab/plan_graph.json",
            "plan_check_result": ".autolab/plan_check_result.json",
        },
        "uat": {
            "policy_required": False,
            "effective_required": True,
            "required_by": "manual",
        },
    }


def test_parse_uat_markdown_rejects_missing_required_fields(tmp_path: Path) -> None:
    path = tmp_path / "uat.md"
    path.write_text(
        "# User Acceptance Test\n\n"
        "UATStatus: pass\n\n"
        "## Checks\n\n"
        "### Check 1 - bootstrap\n"
        "- command: ./scripts/bootstrap_venv.sh\n"
        "- expected: exits 0\n"
        "- result: pass\n",
        encoding="utf-8",
    )

    parsed = parse_uat_markdown(path)

    assert parsed["status"] == "invalid"
    assert any("missing field(s): observed" in error for error in parsed["errors"])


def test_validate_review_result_requires_passing_uat_when_required(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    review_path = iteration_dir / "review_result.json"
    approval_payload = _plan_approval_payload()
    _write_json(review_path, _review_result_payload())
    _write_json(iteration_dir / "plan_approval.json", approval_payload)

    with pytest.raises(
        StageCheckError, match="uat.md is required before implementation_review"
    ):
        _validate_review_result(
            review_path,
            policy_requirements={},
            repo_root=repo,
            iteration_dir=iteration_dir,
            stage_label="implementation_review",
            plan_approval_payload=approval_payload,
        )


def test_validate_review_result_accepts_passing_required_uat(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    review_path = iteration_dir / "review_result.json"
    approval_payload = _plan_approval_payload()
    _write_json(review_path, _review_result_payload())
    _write_json(iteration_dir / "plan_approval.json", approval_payload)
    (iteration_dir / "uat.md").write_text(
        "# User Acceptance Test\n\n"
        "UATStatus: pass\n\n"
        "## Scope\n"
        "- iteration_id: iter1\n"
        "- scope_kind: project_wide\n"
        "- required_by: manual\n\n"
        "## Preconditions\n"
        "- revision_label: v0.0.0\n"
        "- host_mode: local\n"
        "- remote_profile: none\n\n"
        "## Checks\n\n"
        "### Check 1 - bootstrap\n"
        "- command: ./scripts/bootstrap_venv.sh\n"
        "- expected: exits 0 and creates ./venv\n"
        "- observed: exited 0\n"
        "- result: pass\n\n"
        "## Follow-ups\n"
        "- none\n",
        encoding="utf-8",
    )

    status = _validate_review_result(
        review_path,
        policy_requirements={},
        repo_root=repo,
        iteration_dir=iteration_dir,
        stage_label="implementation_review",
        plan_approval_payload=approval_payload,
    )

    assert status == "pass"
