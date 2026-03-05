"""Adapter protocol for mode-specific orchestration."""

from __future__ import annotations

from typing import Protocol

from autolab.models import RunOutcome
from autolab.orchestration.models import RunRequest


class ModeAdapter(Protocol):
    mode_name: str

    def run_once(self, request: RunRequest) -> RunOutcome: ...
