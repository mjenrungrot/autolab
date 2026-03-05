"""Standard workflow mode adapter."""

from __future__ import annotations

from autolab.models import RunOutcome
from autolab.orchestration.models import RunRequest
from autolab import run_standard as _legacy_standard


class StandardModeAdapter:
    mode_name = "standard"

    def run_once(self, request: RunRequest) -> RunOutcome:
        return _legacy_standard._run_once_standard(
            request.state_path,
            request.decision,
            run_agent_mode=request.run_agent_mode,
            verify_before_evaluate=request.verify_before_evaluate,
            auto_decision=request.auto_decision,
            auto_mode=request.auto_mode,
            strict_implementation_progress=request.strict_implementation_progress,
            plan_only=request.plan_only,
            execute_approved_plan=request.execute_approved_plan,
        )
