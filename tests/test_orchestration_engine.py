from __future__ import annotations

from pathlib import Path
from unittest import mock

from autolab.models import RunOutcome
from autolab.orchestration.adapters.assistant import AssistantModeAdapter
from autolab.orchestration.engine import OrchestrationEngine
from autolab.orchestration.models import LoopRequest, RunRequest


class _CapturingAdapter:
    mode_name = "capturing"

    def __init__(self) -> None:
        self.requests: list[RunRequest] = []

    def run_once(self, request: RunRequest) -> RunOutcome:
        self.requests.append(request)
        return RunOutcome(
            exit_code=0,
            transitioned=False,
            stage_before="implementation",
            stage_after="human_review",
            message="paused for review",
        )


def test_run_loop_forwards_plan_execution_flags() -> None:
    adapter = _CapturingAdapter()
    engine = OrchestrationEngine(
        standard_adapter=adapter,
        assistant_adapter=_CapturingAdapter(),
    )

    outcome = engine.run_loop(
        LoopRequest(
            state_path=Path("state.json"),
            max_iterations=2,
            plan_only=True,
            execute_approved_plan=True,
        )
    )

    assert len(adapter.requests) == 1
    assert adapter.requests[0].plan_only is True
    assert adapter.requests[0].execute_approved_plan is True
    assert outcome.completed_iterations == 1
    assert outcome.terminal_reason == "terminal_stage_reached"


def test_assistant_adapter_forwards_plan_execution_flags() -> None:
    adapter = AssistantModeAdapter()
    request = RunRequest(
        state_path=Path("state.json"),
        assistant=True,
        plan_only=True,
        execute_approved_plan=True,
    )
    outcome = RunOutcome(
        exit_code=0,
        transitioned=False,
        stage_before="implementation",
        stage_after="implementation",
        message="paused",
        pause_reason="plan_approval_required",
    )

    with mock.patch(
        "autolab.orchestration.adapters.assistant._legacy_assistant._run_once_assistant",
        return_value=outcome,
    ) as mocked:
        result = adapter.run_once(request)

    assert result.pause_reason == "plan_approval_required"
    assert mocked.call_args.kwargs["plan_only"] is True
    assert mocked.call_args.kwargs["execute_approved_plan"] is True
