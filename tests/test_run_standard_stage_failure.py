"""Direct tests for _handle_stage_failure retry/escalation behavior."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from autolab.run_standard import _handle_stage_failure


def _make_state(
    *,
    stage: str,
    stage_attempt: int,
    max_stage_attempts: int,
    history: list[dict[str, str]] | None = None,
) -> dict[str, object]:
    return {
        "stage": stage,
        "stage_attempt": stage_attempt,
        "max_stage_attempts": max_stage_attempts,
        "history": history or [],
    }


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_handle_stage_failure_retries_before_budget_exhaustion(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    state = _make_state(stage="hypothesis", stage_attempt=0, max_stage_attempts=3)

    with (
        mock.patch("autolab.run_standard._load_verifier_policy", return_value={}),
        mock.patch("autolab.run_standard._safe_todo_post_sync", return_value=([], "")),
    ):
        outcome = _handle_stage_failure(
            repo,
            state_path=state_path,
            state=state,
            stage_before="hypothesis",
            pre_sync_changed=[],
            detail="verification failed",
        )

    assert outcome.transitioned is False
    assert outcome.stage_after == "hypothesis"
    assert "retrying stage hypothesis (1/3)" in outcome.message

    persisted = _read_json(state_path)
    assert persisted["stage"] == "hypothesis"
    assert persisted["stage_attempt"] == 1

    escalation_packet_path = repo / ".autolab" / "escalation_packet.json"
    assert escalation_packet_path.exists() is False

    agent_result = _read_json(repo / ".autolab" / "agent_result.json")
    assert agent_result["status"] == "needs_retry"


def test_handle_stage_failure_exhaustion_writes_escalation_packet_with_stage_budget_override(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    history = [
        {
            "stage_before": "hypothesis",
            "stage_after": "design",
            "summary": "hypothesis accepted",
        },
        {
            "stage_before": "design",
            "stage_after": "implementation",
            "summary": "design approved",
        },
        {
            "stage_before": "implementation",
            "stage_after": "implementation_review",
            "summary": "implementation done",
        },
        {
            "stage_before": "implementation_review",
            "stage_after": "implementation",
            "summary": "needs retry",
        },
    ]
    state = _make_state(
        stage="design",
        stage_attempt=1,
        max_stage_attempts=5,
        history=history,
    )

    with (
        mock.patch(
            "autolab.run_standard._load_verifier_policy",
            return_value={"retry_policy_by_stage": {"design": {"max_retries": 2}}},
        ),
        mock.patch("autolab.run_standard._safe_todo_post_sync", return_value=([], "")),
        mock.patch(
            "autolab.run_standard._utc_now",
            return_value="2026-02-22T00:00:00Z",
        ),
    ):
        outcome = _handle_stage_failure(
            repo,
            state_path=state_path,
            state=state,
            stage_before="design",
            pre_sync_changed=[],
            detail="boom",
        )

    assert outcome.transitioned is True
    assert outcome.stage_after == "human_review"
    assert "retry budget exhausted (2/2), escalating to human_review" in outcome.message

    persisted = _read_json(state_path)
    assert persisted["stage"] == "human_review"
    assert persisted["stage_attempt"] == 2

    escalation_packet = _read_json(repo / ".autolab" / "escalation_packet.json")
    assert escalation_packet["stage"] == "design"
    assert escalation_packet["stage_attempt"] == 2
    assert escalation_packet["max_retries"] == 2
    assert escalation_packet["last_failures"] == ["boom"]
    assert escalation_packet["escalated_at"] == "2026-02-22T00:00:00Z"
    assert escalation_packet["history"] == [
        "design -> implementation: design approved",
        "implementation -> implementation_review: implementation done",
        "implementation_review -> implementation: needs retry",
    ]

    agent_result = _read_json(repo / ".autolab" / "agent_result.json")
    assert agent_result["status"] == "failed"


def test_handle_stage_failure_falls_back_to_global_budget_when_stage_override_missing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    state = _make_state(stage="hypothesis", stage_attempt=1, max_stage_attempts=2)

    with (
        mock.patch(
            "autolab.run_standard._load_verifier_policy",
            return_value={"retry_policy_by_stage": {"design": {"max_retries": 9}}},
        ),
        mock.patch("autolab.run_standard._safe_todo_post_sync", return_value=([], "")),
    ):
        outcome = _handle_stage_failure(
            repo,
            state_path=state_path,
            state=state,
            stage_before="hypothesis",
            pre_sync_changed=[],
            detail="verification failed",
        )

    assert outcome.stage_after == "human_review"
    assert "retry budget exhausted (2/2), escalating to human_review" in outcome.message

    escalation_packet = _read_json(repo / ".autolab" / "escalation_packet.json")
    assert escalation_packet["max_retries"] == 2
