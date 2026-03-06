"""Assistant workflow mode adapter."""

from __future__ import annotations

from autolab.models import RunOutcome
from autolab.orchestration.models import RunRequest
from autolab import run_assistant as _legacy_assistant


class AssistantModeAdapter:
    mode_name = "assistant"

    def run_once(self, request: RunRequest) -> RunOutcome:
        return _legacy_assistant._run_once_assistant(
            request.state_path,
            run_agent_mode=request.run_agent_mode,
            auto_mode=request.auto_mode,
            plan_only=request.plan_only,
            execute_approved_plan=request.execute_approved_plan,
        )
