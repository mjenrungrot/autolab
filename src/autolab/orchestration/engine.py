"""Orchestration engine that routes requests to mode adapters."""

from __future__ import annotations

from dataclasses import dataclass

from autolab.models import RunOutcome
from autolab.orchestration.adapters.assistant import AssistantModeAdapter
from autolab.orchestration.adapters.base import ModeAdapter
from autolab.orchestration.adapters.standard import StandardModeAdapter
from autolab.orchestration.models import LoopOutcome, LoopRequest, RunRequest


@dataclass
class OrchestrationEngine:
    standard_adapter: ModeAdapter = StandardModeAdapter()
    assistant_adapter: ModeAdapter = AssistantModeAdapter()

    def _adapter_for(self, request: RunRequest) -> ModeAdapter:
        return self.assistant_adapter if request.assistant else self.standard_adapter

    def run_once(self, request: RunRequest) -> RunOutcome:
        return self._adapter_for(request).run_once(request)

    def run_loop(self, request: LoopRequest) -> LoopOutcome:
        outcomes: list[RunOutcome] = []
        final_exit_code = 0
        for _index in range(request.max_iterations):
            outcome = self.run_once(
                RunRequest(
                    state_path=request.state_path,
                    decision=None,
                    run_agent_mode=request.run_agent_mode,
                    verify_before_evaluate=request.verify_before_evaluate,
                    assistant=request.assistant,
                    auto_mode=request.auto_mode,
                    auto_decision=request.auto_decision,
                    strict_implementation_progress=request.strict_implementation_progress,
                )
            )
            outcomes.append(outcome)
            final_exit_code = int(outcome.exit_code)
            if outcome.exit_code != 0:
                break
            if outcome.stage_after in {"stop", "human_review"}:
                return LoopOutcome(
                    outcomes=tuple(outcomes),
                    completed_iterations=len(outcomes),
                    final_exit_code=final_exit_code,
                    terminal_reason="terminal_stage_reached",
                )

        return LoopOutcome(
            outcomes=tuple(outcomes),
            completed_iterations=len(outcomes),
            final_exit_code=final_exit_code,
            terminal_reason="iteration_budget_reached",
        )
