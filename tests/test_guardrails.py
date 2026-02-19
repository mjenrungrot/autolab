"""Item 27: Guardrail regression tests.

Tests the guardrail detection, configuration loading, and breach artifact
writing that protect against infinite loops in auto-mode runs.

Covered guardrails:
  - same-decision-streak escalation
  - no-progress escalation
  - update-docs-cycle escalation
  - guardrail config loading & defaults
  - guardrail breach artifact structure
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
import pytest

from autolab.config import _load_guardrail_config
from autolab.models import GuardrailConfig
from autolab.utils import _write_guardrail_breach, _write_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_policy(repo_root: Path, policy: dict[str, Any]) -> Path:
    """Write a verifier_policy.yaml and return its path."""
    policy_path = repo_root / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")
    return policy_path


def _make_repo(tmp_path: Path, guardrails: dict[str, Any] | None = None) -> Path:
    """Create a minimal repo directory with optional guardrail policy."""
    repo = tmp_path / "repo"
    repo.mkdir()
    if guardrails is not None:
        _write_policy(repo, {"autorun": {"guardrails": guardrails}})
    return repo


# ===================================================================
# 1. Guardrail config loading & defaults
# ===================================================================


class TestLoadGuardrailConfigDefaults:
    """_load_guardrail_config returns sensible defaults when the policy
    file is absent or has no guardrails section."""

    def test_defaults_when_no_policy_file(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        config = _load_guardrail_config(repo)
        assert isinstance(config, GuardrailConfig)
        assert config.max_same_decision_streak == 3
        assert config.max_no_progress_decisions == 2
        assert config.max_update_docs_cycles == 3
        assert config.max_generated_todo_tasks == 5
        assert config.on_breach == "human_review"

    def test_defaults_when_empty_policy(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _write_policy(repo, {})
        config = _load_guardrail_config(repo)
        assert config.max_same_decision_streak == 3
        assert config.max_no_progress_decisions == 2
        assert config.on_breach == "human_review"

    def test_defaults_when_autorun_has_no_guardrails_key(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _write_policy(repo, {"autorun": {}})
        config = _load_guardrail_config(repo)
        assert config.max_same_decision_streak == 3
        assert config.max_no_progress_decisions == 2
        assert config.max_update_docs_cycles == 3

    def test_defaults_when_guardrails_not_a_dict(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _write_policy(repo, {"autorun": {"guardrails": "invalid"}})
        config = _load_guardrail_config(repo)
        assert config.max_same_decision_streak == 3
        assert config.on_breach == "human_review"


class TestLoadGuardrailConfigCustomValues:
    """_load_guardrail_config correctly parses custom policy values."""

    def test_all_fields_customized(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            guardrails={
                "max_same_decision_streak": 5,
                "max_no_progress_decisions": 4,
                "max_update_docs_cycles": 6,
                "max_generated_todo_tasks": 10,
                "on_breach": "stop",
            },
        )
        config = _load_guardrail_config(repo)
        assert config.max_same_decision_streak == 5
        assert config.max_no_progress_decisions == 4
        assert config.max_update_docs_cycles == 6
        assert config.max_generated_todo_tasks == 10
        assert config.on_breach == "stop"

    def test_partial_override(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            guardrails={
                "max_same_decision_streak": 7,
            },
        )
        config = _load_guardrail_config(repo)
        assert config.max_same_decision_streak == 7
        # Other fields fall back to defaults.
        assert config.max_no_progress_decisions == 2
        assert config.max_update_docs_cycles == 3
        assert config.max_generated_todo_tasks == 5
        assert config.on_breach == "human_review"

    def test_on_breach_invalid_falls_back_to_human_review(self, tmp_path: Path) -> None:
        """on_breach must be one of TERMINAL_STAGES; invalid values default
        to 'human_review'."""
        repo = _make_repo(
            tmp_path,
            guardrails={
                "on_breach": "implementation",  # not a terminal stage
            },
        )
        config = _load_guardrail_config(repo)
        assert config.on_breach == "human_review"

    def test_on_breach_empty_string_falls_back(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            guardrails={
                "on_breach": "",
            },
        )
        config = _load_guardrail_config(repo)
        assert config.on_breach == "human_review"

    def test_on_breach_accepts_stop(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            guardrails={
                "on_breach": "stop",
            },
        )
        config = _load_guardrail_config(repo)
        assert config.on_breach == "stop"


class TestLoadGuardrailConfigMinClamp:
    """Values below 1 are clamped.

    The loader uses ``int(guardrails.get(key, default) or default)`` which
    means zero (falsy) falls through to the default value before the clamp
    check.  Only genuinely negative values reach the ``< 1`` clamp.
    """

    def test_zero_max_same_decision_streak_coerced_to_default(
        self, tmp_path: Path
    ) -> None:
        """0 is falsy so ``int(0 or 3)`` produces 3 (the default)."""
        repo = _make_repo(tmp_path, guardrails={"max_same_decision_streak": 0})
        config = _load_guardrail_config(repo)
        assert config.max_same_decision_streak == 3

    def test_negative_max_no_progress_clamps_to_one(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, guardrails={"max_no_progress_decisions": -5})
        config = _load_guardrail_config(repo)
        assert config.max_no_progress_decisions == 1

    def test_zero_max_update_docs_cycles_coerced_to_default(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(tmp_path, guardrails={"max_update_docs_cycles": 0})
        config = _load_guardrail_config(repo)
        assert config.max_update_docs_cycles == 3

    def test_zero_max_generated_todo_tasks_coerced_to_default(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(tmp_path, guardrails={"max_generated_todo_tasks": 0})
        config = _load_guardrail_config(repo)
        assert config.max_generated_todo_tasks == 5

    def test_negative_max_same_decision_streak_clamps_to_one(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(tmp_path, guardrails={"max_same_decision_streak": -2})
        config = _load_guardrail_config(repo)
        assert config.max_same_decision_streak == 1

    def test_negative_max_update_docs_cycles_clamps_to_one(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(tmp_path, guardrails={"max_update_docs_cycles": -1})
        config = _load_guardrail_config(repo)
        assert config.max_update_docs_cycles == 1

    def test_negative_max_generated_todo_tasks_clamps_to_one(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(tmp_path, guardrails={"max_generated_todo_tasks": -3})
        config = _load_guardrail_config(repo)
        assert config.max_generated_todo_tasks == 1


class TestLoadGuardrailConfigNoneCoercion:
    """None/null values in YAML fall back to defaults via the `or N` coercion."""

    def test_none_max_same_decision_streak(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, guardrails={"max_same_decision_streak": None})
        config = _load_guardrail_config(repo)
        assert config.max_same_decision_streak == 3

    def test_none_max_no_progress_decisions(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, guardrails={"max_no_progress_decisions": None})
        config = _load_guardrail_config(repo)
        assert config.max_no_progress_decisions == 2

    def test_none_max_update_docs_cycles(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, guardrails={"max_update_docs_cycles": None})
        config = _load_guardrail_config(repo)
        assert config.max_update_docs_cycles == 3

    def test_none_on_breach(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path, guardrails={"on_breach": None})
        config = _load_guardrail_config(repo)
        assert config.on_breach == "human_review"


# ===================================================================
# 2. Guardrail breach artifact (_write_guardrail_breach)
# ===================================================================


class TestWriteGuardrailBreach:
    """_write_guardrail_breach writes .autolab/guardrail_breach.json with
    the expected schema."""

    def test_writes_expected_json_structure(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        breach_path = _write_guardrail_breach(
            repo,
            rule="same_decision_streak",
            counters={"same_decision_streak": 4, "max_same_decision_streak": 3},
            stage="decide_repeat",
            remediation="Escalated to human_review.",
        )
        assert breach_path == repo / ".autolab" / "guardrail_breach.json"
        assert breach_path.exists()

        payload = json.loads(breach_path.read_text(encoding="utf-8"))
        assert payload["rule"] == "same_decision_streak"
        assert payload["stage"] == "decide_repeat"
        assert payload["remediation"] == "Escalated to human_review."
        assert payload["counters"]["same_decision_streak"] == 4
        assert payload["counters"]["max_same_decision_streak"] == 3
        assert "breached_at" in payload

    def test_breach_timestamp_is_iso_utc(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_guardrail_breach(
            repo,
            rule="no_progress",
            counters={},
            stage="decide_repeat",
            remediation="test",
        )
        payload = json.loads(
            (repo / ".autolab" / "guardrail_breach.json").read_text(encoding="utf-8")
        )
        ts = payload["breached_at"]
        assert isinstance(ts, str)
        assert ts.endswith("Z")

    def test_overwrites_previous_breach(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_guardrail_breach(
            repo,
            rule="first_rule",
            counters={"a": 1},
            stage="decide_repeat",
            remediation="first",
        )
        _write_guardrail_breach(
            repo,
            rule="second_rule",
            counters={"b": 2},
            stage="extract_results",
            remediation="second",
        )
        payload = json.loads(
            (repo / ".autolab" / "guardrail_breach.json").read_text(encoding="utf-8")
        )
        assert payload["rule"] == "second_rule"
        assert payload["stage"] == "extract_results"

    def test_creates_autolab_directory_if_missing(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        # .autolab does not exist yet
        breach_path = _write_guardrail_breach(
            repo,
            rule="update_docs_cycle",
            counters={"update_docs_cycle_count": 4, "max_update_docs_cycles": 3},
            stage="extract_results",
            remediation="Escalated.",
        )
        assert breach_path.exists()

    def test_returns_path(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        breach_path = _write_guardrail_breach(
            repo,
            rule="test",
            counters={},
            stage="decide_repeat",
            remediation="test",
        )
        assert isinstance(breach_path, Path)
        assert breach_path.name == "guardrail_breach.json"


# ===================================================================
# 3. Same-decision-streak escalation logic
# ===================================================================


class TestSameDecisionStreakEscalation:
    """Verify the same-decision-streak counter logic used in the
    decide_repeat path of _run_once_standard.

    The logic is:
      if selected_decision == last_decision:
          same_decision_streak += 1
      else:
          same_decision_streak = 1
      ...
      if same_decision_streak > guardrails.max_same_decision_streak:
          -> escalate

    We test this counter arithmetic in isolation using the same algorithm.
    """

    @staticmethod
    def _simulate_streak(
        decisions: list[str],
        max_same: int = 3,
    ) -> tuple[bool, int]:
        """Replay a sequence of decisions and return (breached, final_streak).

        Mirrors the counter logic from _run_once_standard's decide_repeat
        section.
        """
        same_decision_streak = 0
        last_decision = ""
        breached = False
        for decision in decisions:
            if decision == last_decision:
                same_decision_streak += 1
            else:
                same_decision_streak = 1
            if same_decision_streak > max_same:
                breached = True
                break
            last_decision = decision
        return (breached, same_decision_streak)

    def test_no_breach_within_limit(self) -> None:
        breached, streak = self._simulate_streak(
            ["hypothesis", "hypothesis", "hypothesis"],
            max_same=3,
        )
        assert not breached
        assert streak == 3

    def test_breach_on_exceeding_limit(self) -> None:
        breached, streak = self._simulate_streak(
            ["hypothesis", "hypothesis", "hypothesis", "hypothesis"],
            max_same=3,
        )
        assert breached
        assert streak == 4

    def test_streak_resets_on_different_decision(self) -> None:
        breached, streak = self._simulate_streak(
            ["hypothesis", "hypothesis", "design", "hypothesis"],
            max_same=3,
        )
        assert not breached
        assert streak == 1

    def test_streak_with_max_one(self) -> None:
        """max_same_decision_streak=1 means the second identical decision
        triggers the breach."""
        breached, streak = self._simulate_streak(
            ["hypothesis", "hypothesis"],
            max_same=1,
        )
        assert breached
        assert streak == 2

    def test_single_decision_never_breaches(self) -> None:
        breached, streak = self._simulate_streak(
            ["hypothesis"],
            max_same=1,
        )
        assert not breached
        assert streak == 1

    def test_empty_decisions_never_breaches(self) -> None:
        breached, streak = self._simulate_streak([], max_same=1)
        assert not breached
        assert streak == 0

    def test_alternating_decisions_never_breach(self) -> None:
        breached, _ = self._simulate_streak(
            ["hypothesis", "design"] * 10,
            max_same=1,
        )
        assert not breached


# ===================================================================
# 4. No-progress escalation logic
# ===================================================================


class TestNoProgressEscalation:
    """Verify the no-progress counter logic from the decide_repeat path.

    The logic is:
      if last_open_task_count >= 0 and open_count >= last_open_task_count:
          no_progress_decisions += 1
      else:
          no_progress_decisions = 0

      if meaningful_changed:
          no_progress_decisions = 0

      if no_progress_decisions >= guardrails.max_no_progress_decisions:
          -> escalate
    """

    @staticmethod
    def _simulate_no_progress(
        open_counts: list[int],
        meaningful_flags: list[bool],
        max_no_progress: int = 2,
    ) -> tuple[bool, int]:
        """Replay open-count / meaningful-change signals and return
        (breached, final_counter).

        Mirrors the counter logic from _run_once_standard.
        """
        no_progress_decisions = 0
        last_open_task_count = -1
        breached = False
        for open_count, meaningful_changed in zip(open_counts, meaningful_flags):
            if last_open_task_count >= 0 and open_count >= last_open_task_count:
                no_progress_decisions += 1
            else:
                no_progress_decisions = 0
            if meaningful_changed:
                no_progress_decisions = 0
            if no_progress_decisions >= max_no_progress:
                breached = True
                break
            last_open_task_count = open_count
        return (breached, no_progress_decisions)

    def test_no_breach_when_task_count_decreases(self) -> None:
        breached, counter = self._simulate_no_progress(
            open_counts=[5, 4, 3],
            meaningful_flags=[False, False, False],
            max_no_progress=2,
        )
        assert not breached
        assert counter == 0

    def test_breach_when_task_count_stagnates(self) -> None:
        breached, counter = self._simulate_no_progress(
            open_counts=[5, 5, 5],
            meaningful_flags=[False, False, False],
            max_no_progress=2,
        )
        assert breached
        assert counter == 2

    def test_meaningful_change_resets_counter(self) -> None:
        breached, counter = self._simulate_no_progress(
            open_counts=[5, 5, 5, 5],
            meaningful_flags=[False, True, False, False],
            max_no_progress=2,
        )
        # After iteration 1 (open=5, same): counter=1
        # After iteration 2 (open=5, meaningful): counter=0 (reset)
        # After iteration 3 (open=5, same): counter=1
        # After iteration 4 (open=5, same): counter=2 -> breach
        assert breached
        assert counter == 2

    def test_first_iteration_never_counts(self) -> None:
        """On first iteration last_open_task_count is -1, so the
        'open_count >= last_open_task_count' condition cannot trigger."""
        breached, counter = self._simulate_no_progress(
            open_counts=[10],
            meaningful_flags=[False],
            max_no_progress=1,
        )
        assert not breached
        assert counter == 0

    def test_increasing_counts_trigger_no_progress(self) -> None:
        """If open tasks increase, that still counts as no progress
        (open_count >= last_open_task_count)."""
        breached, counter = self._simulate_no_progress(
            open_counts=[3, 4, 5],
            meaningful_flags=[False, False, False],
            max_no_progress=2,
        )
        assert breached
        assert counter == 2


# ===================================================================
# 5. Update-docs-cycle escalation logic
# ===================================================================


class TestUpdateDocsCycleEscalation:
    """Verify the update_docs cycle counter logic from the
    extract_results -> update_docs transition in _run_once_standard.

    The logic is:
      update_docs_cycle_count = int(repeat_guard.get("update_docs_cycle_count", 0)) + 1
      if update_docs_cycle_count > int(guardrails.max_update_docs_cycles):
          -> escalate
    """

    @staticmethod
    def _simulate_update_docs_cycles(
        num_cycles: int,
        max_update_docs_cycles: int = 3,
    ) -> tuple[bool, int]:
        """Simulate N extract_results -> update_docs transitions and
        return (breached, final_count)."""
        update_docs_cycle_count = 0
        breached = False
        for _ in range(num_cycles):
            update_docs_cycle_count += 1
            if update_docs_cycle_count > max_update_docs_cycles:
                breached = True
                break
        return (breached, update_docs_cycle_count)

    def test_within_limit_no_breach(self) -> None:
        breached, count = self._simulate_update_docs_cycles(3, max_update_docs_cycles=3)
        assert not breached
        assert count == 3

    def test_exceeds_limit_triggers_breach(self) -> None:
        breached, count = self._simulate_update_docs_cycles(4, max_update_docs_cycles=3)
        assert breached
        assert count == 4

    def test_single_cycle_never_breaches(self) -> None:
        breached, count = self._simulate_update_docs_cycles(1, max_update_docs_cycles=1)
        assert not breached
        assert count == 1

    def test_limit_of_one_breaches_on_second(self) -> None:
        breached, count = self._simulate_update_docs_cycles(2, max_update_docs_cycles=1)
        assert breached
        assert count == 2

    def test_zero_cycles_never_breaches(self) -> None:
        breached, count = self._simulate_update_docs_cycles(0, max_update_docs_cycles=1)
        assert not breached
        assert count == 0


# ===================================================================
# 6. Integration: config + breach artifact round-trip
# ===================================================================


class TestGuardrailConfigBreachRoundTrip:
    """Verify that a guardrail configuration is correctly used when
    writing a breach artifact, and the artifact contains the config
    values."""

    def test_same_decision_streak_breach_records_config_values(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(
            tmp_path,
            guardrails={
                "max_same_decision_streak": 2,
                "on_breach": "stop",
            },
        )
        config = _load_guardrail_config(repo)
        assert config.max_same_decision_streak == 2
        assert config.on_breach == "stop"

        # Simulate breach: streak of 3 exceeds max_same of 2.
        breach_path = _write_guardrail_breach(
            repo,
            rule="same_decision_streak",
            counters={
                "same_decision_streak": 3,
                "max_same_decision_streak": config.max_same_decision_streak,
            },
            stage="decide_repeat",
            remediation=f"Escalated to '{config.on_breach}'.",
        )
        payload = json.loads(breach_path.read_text(encoding="utf-8"))
        assert payload["rule"] == "same_decision_streak"
        assert payload["counters"]["max_same_decision_streak"] == 2
        assert payload["remediation"] == "Escalated to 'stop'."

    def test_no_progress_breach_records_config_values(self, tmp_path: Path) -> None:
        repo = _make_repo(
            tmp_path,
            guardrails={
                "max_no_progress_decisions": 4,
                "on_breach": "human_review",
            },
        )
        config = _load_guardrail_config(repo)
        assert config.max_no_progress_decisions == 4

        breach_path = _write_guardrail_breach(
            repo,
            rule="no_progress",
            counters={
                "no_progress_decisions": 4,
                "max_no_progress_decisions": config.max_no_progress_decisions,
            },
            stage="decide_repeat",
            remediation=f"Escalated to '{config.on_breach}'.",
        )
        payload = json.loads(breach_path.read_text(encoding="utf-8"))
        assert payload["rule"] == "no_progress"
        assert payload["counters"]["max_no_progress_decisions"] == 4

    def test_update_docs_cycle_breach_records_config_values(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(
            tmp_path,
            guardrails={
                "max_update_docs_cycles": 2,
                "on_breach": "human_review",
            },
        )
        config = _load_guardrail_config(repo)
        assert config.max_update_docs_cycles == 2

        breach_path = _write_guardrail_breach(
            repo,
            rule="update_docs_cycle",
            counters={
                "update_docs_cycle_count": 3,
                "max_update_docs_cycles": config.max_update_docs_cycles,
            },
            stage="extract_results",
            remediation=f"Escalated to '{config.on_breach}'.",
        )
        payload = json.loads(breach_path.read_text(encoding="utf-8"))
        assert payload["rule"] == "update_docs_cycle"
        assert payload["counters"]["update_docs_cycle_count"] == 3
        assert payload["counters"]["max_update_docs_cycles"] == 2
