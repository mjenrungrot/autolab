"""Multi-device state machine workflow tests.

Documents gaps in cross-device synchronization when a single developer
works across a LOCAL laptop and a REMOTE SLURM HPC cluster.  Each test
exercises an identified gap and asserts what *currently* happens (even
if suboptimal), serving as documentation tests rather than fix-validation.

GAP-1: State desync on forgotten push/pull
GAP-2: Lock is local-filesystem-only
GAP-3: `autolab run` has zero concurrency protection (no lock)
GAP-4: SLURM pending sync exhausts retry budget
GAP-5: Divergent auto-commits cause state.json merge conflicts
GAP-6: Host mode silently changes behavior
GAP-7: No pre-run working tree check
GAP-8: Cross-device stage_attempt double-counting
"""

from __future__ import annotations

import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from autolab.__main__ import (
    LOCK_STALE_SECONDS,
    TERMINAL_STAGES,
    RunOutcome,
    _acquire_lock,
    _release_lock,
    _run_once,
    _utc_now,
)

# ---------------------------------------------------------------------------
# Helpers (reused from test_state_machine.py)
# ---------------------------------------------------------------------------

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore[assignment]


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
) -> dict[str, Any]:
    return {
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "stage": stage,
        "stage_attempt": stage_attempt,
        "last_run_id": "",
        "sync_status": "",
        "max_stage_attempts": max_stage_attempts,
        "max_total_iterations": 10,
        "assistant_mode": assistant_mode,
        "current_task_id": current_task_id,
        "task_cycle_stage": task_cycle_stage,
        "repeat_guard": repeat_guard or {
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
            {"id": "h1", "status": hypothesis_status, "title": "Test hypothesis", "success_metric": "metric", "target_delta": 0.0},
        ],
        "experiments": [
            {"id": experiment_id, "hypothesis_id": "h1", "status": status, "iteration_id": iteration_id},
        ],
    }
    path = repo / ".autolab" / "backlog.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(backlog, sort_keys=False), encoding="utf-8")


def _write_policy(repo: Path, *, guardrails: dict[str, Any] | None = None) -> None:
    policy = {
        "test_command": "true",
        "dry_run_command": "true",
        "require_tests": False,
        "require_dry_run": False,
        "require_env_smoke": False,
        "require_docs_target_update": False,
        "template_fill": {"enabled": False},
        "agent_runner": {"enabled": False, "stages": []},
        "autorun": {
            "guardrails": guardrails or {
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


def _seed_hypothesis(iteration_dir: Path) -> None:
    (iteration_dir / "hypothesis.md").write_text("# Hypothesis\nWe hypothesize X.", encoding="utf-8")


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
    (iteration_dir / "design.yaml").write_text(yaml.safe_dump(design, sort_keys=False), encoding="utf-8")


def _seed_implementation(iteration_dir: Path) -> None:
    (iteration_dir / "implementation_plan.md").write_text("# Implementation\nStep 1.", encoding="utf-8")


def _seed_review_pass(iteration_dir: Path) -> None:
    (iteration_dir / "implementation_review.md").write_text("# Review\nLGTM.", encoding="utf-8")
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
    (iteration_dir / "review_result.json").write_text(json.dumps(review, indent=2), encoding="utf-8")


def _seed_launch(iteration_dir: Path, run_id: str = "run_001") -> None:
    if not (iteration_dir / "design.yaml").exists():
        _seed_design(iteration_dir, iteration_id=iteration_dir.name)
    if not (iteration_dir / "review_result.json").exists():
        _seed_review_pass(iteration_dir)
    launch_dir = iteration_dir / "launch"
    launch_dir.mkdir(parents=True, exist_ok=True)
    (launch_dir / "run_local.sh").write_text("#!/bin/bash\necho run", encoding="utf-8")
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
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


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
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
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
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")


def _seed_update_docs(iteration_dir: Path) -> None:
    (iteration_dir / "docs_update.md").write_text("# Docs Update\nUpdated.", encoding="utf-8")
    analysis_dir = iteration_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "summary.md").write_text("# Summary\nResults.", encoding="utf-8")


def _seed_slurm_launch(
    iteration_dir: Path,
    run_id: str = "run_001",
    *,
    iteration_id: str = "iter_test_001",
    job_id: str = "12345",
    sync_status: str = "completed",
) -> None:
    if not (iteration_dir / "design.yaml").exists():
        _seed_design(iteration_dir, iteration_id=iteration_dir.name)
    design_path = iteration_dir / "design.yaml"
    design_payload = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    if isinstance(design_payload, dict):
        compute = design_payload.get("compute")
        if not isinstance(compute, dict):
            compute = {}
        compute["location"] = "slurm"
        design_payload["compute"] = compute
        design_path.write_text(yaml.safe_dump(design_payload, sort_keys=False), encoding="utf-8")
    if not (iteration_dir / "review_result.json").exists():
        _seed_review_pass(iteration_dir)
    launch_dir = iteration_dir / "launch"
    launch_dir.mkdir(parents=True, exist_ok=True)
    (launch_dir / "run_slurm.sbatch").write_text(
        "#!/bin/bash\n#SBATCH --job-name=test\necho run", encoding="utf-8",
    )
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "iteration_id": iteration_id,
        "launch_mode": "slurm",
        "host_mode": "slurm",
        "command": "sbatch launch/run_slurm.sbatch",
        "resource_request": {"partition": "debug"},
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "status": "completed",
        "slurm": {"job_id": job_id},
        "artifact_sync_to_local": {"status": sync_status},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:05:00Z",
        },
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _seed_slurm_extract(
    iteration_dir: Path,
    run_id: str = "run_001",
    *,
    iteration_id: str = "iter_test_001",
    job_id: str = "12345",
) -> None:
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "iteration_id": iteration_id,
        "launch_mode": "slurm",
        "host_mode": "slurm",
        "command": "sbatch launch/run_slurm.sbatch",
        "resource_request": {"partition": "debug"},
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "status": "completed",
        "slurm": {"job_id": job_id},
        "artifact_sync_to_local": {"status": "completed"},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:05:00Z",
        },
    }
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    metrics = {
        "iteration_id": iteration_id,
        "run_id": run_id,
        "status": "completed",
        "primary_metric": {
            "name": "loss",
            "value": 0.42,
            "delta_vs_baseline": 0.0,
        },
        "baseline_results": [],
        "variant_results": [],
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")


def _write_slurm_ledger(repo: Path, run_id: str, *, job_id: str = "12345", iteration_id: str = "iter_test_001") -> None:
    path = repo / "docs" / "slurm_job_list.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = f"- 2026-01-01 | job_id={job_id} | iteration_id={iteration_id} | run_id={run_id} | status=completed"
    path.write_text(f"# SLURM Job Ledger\n\n{entry}\n", encoding="utf-8")


def _setup_repo(
    tmp_path: Path,
    *,
    hypothesis_status: str = "open",
    backlog_experiment_status: str = "open",
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
    _write_policy(repo)
    _write_todo_state(repo)
    _write_todo_md(repo)
    iteration_dir = _seed_iteration(repo, state.get("iteration_id", "iter_test_001"))
    return repo, state_path, iteration_dir


def _run(state_path: Path, **kwargs: Any) -> RunOutcome:
    kwargs.setdefault("run_agent_mode", "force_off")
    kwargs.setdefault("strict_implementation_progress", False)
    return _run_once(state_path, kwargs.pop("decision", None), **kwargs)


# ---------------------------------------------------------------------------
# Test Class 1: TestStaleStateTransitions (GAP-1, GAP-5)
# ---------------------------------------------------------------------------

class TestStaleStateTransitions:
    """Simulates what happens when state.json on one device is behind the other."""

    def test_stale_state_causes_duplicate_transition(self, tmp_path: Path) -> None:
        """Set up stage=hypothesis with all artifacts through launch already
        seeded (as if REMOTE advanced).  Running _run_once transitions
        hypothesis->design even though the 'real' state should be at launch.
        Documents GAP-1: no mechanism to detect stale state.
        """
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="hypothesis")

        # Seed artifacts all the way through launch (as if REMOTE did the work)
        _seed_hypothesis(it_dir)
        _seed_design(it_dir)
        _seed_implementation(it_dir)
        _seed_review_pass(it_dir)
        _seed_launch(it_dir)

        # LOCAL still thinks we're at hypothesis -- duplicate transition succeeds
        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_before == "hypothesis"
        assert outcome.stage_after == "design"
        assert outcome.exit_code == 0
        # The system has no idea we should be at 'launch' -- gap documented
        persisted = _read_state(repo)
        assert persisted["stage"] == "design"

    def test_both_devices_at_same_stage_produce_independent_transitions(self, tmp_path: Path) -> None:
        """Run _run_once twice on the same stage, resetting state between runs
        to simulate two devices.  Both succeed independently with no conflict
        detection.  Documents GAP-1/GAP-5.
        """
        # "Device A" run
        dir_a = tmp_path / "device_a"
        dir_a.mkdir()
        repo_a, state_path_a, it_dir_a = _setup_repo(dir_a, stage="hypothesis")
        _seed_hypothesis(it_dir_a)
        outcome_a = _run(state_path_a)
        assert outcome_a.transitioned
        assert outcome_a.stage_after == "design"

        # "Device B" run -- independent repo, same starting stage
        dir_b = tmp_path / "device_b"
        dir_b.mkdir()
        repo_b, state_path_b, it_dir_b = _setup_repo(dir_b, stage="hypothesis")
        _seed_hypothesis(it_dir_b)
        outcome_b = _run(state_path_b)
        assert outcome_b.transitioned
        assert outcome_b.stage_after == "design"

        # Both transitioned independently with zero conflict detection
        assert _read_state(repo_a)["stage"] == "design"
        assert _read_state(repo_b)["stage"] == "design"

    def test_conflicting_decide_repeat_decisions(self, tmp_path: Path) -> None:
        """Run _run_once with decision='hypothesis', then reset state to
        decide_repeat and run with decision='stop'.  The second decision
        overwrites the first.  Documents GAP-5: last writer wins.
        """
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")

        # "Device A" decides hypothesis
        outcome_a = _run(state_path, decision="hypothesis")
        assert outcome_a.transitioned
        assert outcome_a.stage_after == "hypothesis"
        persisted_a = _read_state(repo)
        assert persisted_a["repeat_guard"]["last_decision"] == "hypothesis"

        # Simulate "Device B" overwriting with stale state at decide_repeat
        state_b = _make_state(stage="decide_repeat")
        _write_state(repo, state_b)

        outcome_b = _run(state_path, decision="stop")
        assert outcome_b.transitioned
        assert outcome_b.stage_after == "stop"

        # Last writer wins -- Device B's decision is the persisted one
        persisted_b = _read_state(repo)
        assert persisted_b["repeat_guard"]["last_decision"] == "stop"


# ---------------------------------------------------------------------------
# Test Class 2: TestLockLimitations (GAP-2, GAP-3)
# ---------------------------------------------------------------------------

class TestLockLimitations:
    """Documents that locks are local-only and `run` has no lock."""

    def test_run_command_creates_no_lock(self, tmp_path: Path) -> None:
        """Run _run_once and assert .autolab/lock does not exist afterward.
        Documents GAP-3: _cmd_run never calls _acquire_lock.
        """
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="hypothesis")
        _seed_hypothesis(it_dir)

        _run(state_path)

        lock_path = repo / ".autolab" / "lock"
        assert not lock_path.exists()

    def test_lock_contains_hostname(self, tmp_path: Path) -> None:
        """Call _acquire_lock directly, read the JSON payload, assert it
        contains the current socket.gethostname().  Documents GAP-2: the
        lock records host but only checks local filesystem.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        lock_path = repo / ".autolab" / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        state_path = repo / ".autolab" / "state.json"

        ok, msg = _acquire_lock(
            lock_path,
            state_file=state_path,
            command="test",
            stale_seconds=LOCK_STALE_SECONDS,
        )

        assert ok
        payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert payload["host"] == socket.gethostname()
        # Cleanup
        _release_lock(lock_path)

    def test_stale_lock_from_different_host_is_replaced(self, tmp_path: Path) -> None:
        """Write a lock payload with host='remote-cluster' and a heartbeat
        older than LOCK_STALE_SECONDS.  Call _acquire_lock and assert it
        succeeds with 'replaced stale lock'.  Documents GAP-2: stale locks
        from other hosts are replaced.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        lock_path = repo / ".autolab" / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        state_path = repo / ".autolab" / "state.json"

        # Write a stale lock from a different host
        stale_time = (
            datetime.now(timezone.utc) - timedelta(seconds=LOCK_STALE_SECONDS + 60)
        ).isoformat(timespec="seconds").replace("+00:00", "Z")
        stale_lock = {
            "pid": 99999,
            "host": "remote-cluster",
            "started_at": stale_time,
            "last_heartbeat_at": stale_time,
            "command": "autolab loop --auto",
            "state_file": str(state_path),
        }
        lock_path.write_text(json.dumps(stale_lock, indent=2), encoding="utf-8")

        ok, msg = _acquire_lock(
            lock_path,
            state_file=state_path,
            command="test",
            stale_seconds=LOCK_STALE_SECONDS,
        )

        assert ok
        assert "replaced stale lock" in msg
        # New lock has our host
        new_payload = json.loads(lock_path.read_text(encoding="utf-8"))
        assert new_payload["host"] == socket.gethostname()
        _release_lock(lock_path)

    def test_active_lock_from_different_host_blocks(self, tmp_path: Path) -> None:
        """Write a lock with host='remote-cluster' and a fresh heartbeat.
        Call _acquire_lock and assert it fails with 'active lock exists'
        mentioning 'remote-cluster'.  Documents GAP-2.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        lock_path = repo / ".autolab" / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        state_path = repo / ".autolab" / "state.json"

        # Write a fresh lock from a different host
        fresh_lock = {
            "pid": 99999,
            "host": "remote-cluster",
            "started_at": _utc_now(),
            "last_heartbeat_at": _utc_now(),
            "command": "autolab loop --auto",
            "state_file": str(state_path),
        }
        lock_path.write_text(json.dumps(fresh_lock, indent=2), encoding="utf-8")

        ok, msg = _acquire_lock(
            lock_path,
            state_file=state_path,
            command="test",
            stale_seconds=LOCK_STALE_SECONDS,
        )

        assert not ok
        assert "active lock exists" in msg
        assert "remote-cluster" in msg


# ---------------------------------------------------------------------------
# Test Class 3: TestSlurmSyncRetryBudgetExhaustion (GAP-4, GAP-8)
# ---------------------------------------------------------------------------

class TestSlurmSyncRetryBudgetExhaustion:
    """Documents that pending SLURM sync exhausts the retry budget and that
    cross-device checks double-count attempts.
    """

    def test_pending_sync_exhausts_budget(self, tmp_path: Path) -> None:
        """Set stage=launch, max_stage_attempts=3, seed SLURM launch with
        sync_status='pending'.  Run 3 times.  First 2 stay at launch with
        incrementing stage_attempt, third escalates to human_review.
        Documents GAP-4.
        """
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="launch", stage_attempt=0, max_stage_attempts=3,
        )
        _seed_slurm_launch(it_dir, sync_status="pending")
        _write_slurm_ledger(repo, "run_001")

        # Attempt 1
        outcome1 = _run(state_path)
        assert outcome1.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] == "launch"
        assert persisted["stage_attempt"] == 1

        # Attempt 2
        outcome2 = _run(state_path)
        assert outcome2.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] == "launch"
        assert persisted["stage_attempt"] == 2

        # Attempt 3 -> exhaustion -> human_review
        outcome3 = _run(state_path)
        assert outcome3.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"

    def test_cross_device_attempt_double_counting(self, tmp_path: Path) -> None:
        """Set stage=launch, stage_attempt=0.  Run once ('REMOTE' check,
        attempt->1).  The state is persisted.  Run again ('LOCAL' check,
        attempt->2).  Assert 2 attempts consumed for a single wait condition.
        Documents GAP-8.
        """
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="launch", stage_attempt=0, max_stage_attempts=5,
        )
        _seed_slurm_launch(it_dir, sync_status="pending")
        _write_slurm_ledger(repo, "run_001")

        # "REMOTE" checks -- sync is pending, attempt increments
        outcome_remote = _run(state_path)
        assert outcome_remote.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 1

        # "LOCAL" checks same state -- sync still pending, attempt increments again
        outcome_local = _run(state_path)
        assert outcome_local.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 2

        # Two attempts consumed for a single underlying wait condition

    def test_sync_transitions_from_pending_to_completed(self, tmp_path: Path) -> None:
        """Set stage=launch, stage_attempt=1.  First run with sync='running'
        (attempt->2).  Then update manifest to sync='completed' and run again.
        Assert transition to extract_results with stage_attempt reset to 0.
        Documents GAP-4 recovery path.
        """
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="launch", stage_attempt=1, max_stage_attempts=5,
        )
        _seed_slurm_launch(it_dir, sync_status="running")
        _write_slurm_ledger(repo, "run_001")

        # Run with sync still in-progress
        outcome1 = _run(state_path)
        assert outcome1.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] == "launch"
        assert persisted["stage_attempt"] == 2

        # Now sync completes -- update the manifest
        _seed_slurm_launch(it_dir, sync_status="completed")

        outcome2 = _run(state_path)
        assert outcome2.transitioned
        assert outcome2.stage_after == "extract_results"
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 0


# ---------------------------------------------------------------------------
# Test Class 4: TestRepeatGuardCrossDevice (GAP-8)
# ---------------------------------------------------------------------------

class TestRepeatGuardCrossDevice:
    """Documents cross-device repeat guard accumulation issues."""

    def test_stale_repeat_guard_accumulates_streaks(self, tmp_path: Path) -> None:
        """Set repeat_guard.same_decision_streak=2 and last_decision='design'.
        Run with decision='design'.  Assert streak becomes 3 (one step from
        breach at default max of 3).  Documents GAP-8: stale repeat_guard
        from another device accumulates without awareness.
        """
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "design",
                "same_decision_streak": 2,
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 0,
                "last_verification_passed": False,
            },
        )

        outcome = _run(
            state_path,
            decision="design",
            auto_mode=True,
            auto_decision=True,
        )

        assert outcome.stage_after == "design"
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["same_decision_streak"] == 3

    def test_no_progress_from_stale_state_triggers_breach(self, tmp_path: Path) -> None:
        """Set repeat_guard.no_progress_decisions=1 and last_open_task_count=3.
        Write 3 open tasks in docs/todo.md.  Run with decision='design'.
        Since open count (3) >= last_open_task_count (3), no_progress_decisions
        increments to 2 which hits max_no_progress_decisions=2.
        Assert escalation to human_review.  Documents GAP-8.
        """
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "hypothesis",
                "same_decision_streak": 0,
                "last_open_task_count": 3,
                "no_progress_decisions": 1,
                "update_docs_cycle_count": 0,
                "last_verification_passed": False,
            },
        )
        _write_todo_md(
            repo,
            "# Tasks\n- [ ] [hypothesis] task1\n- [ ] [design] task2\n- [ ] [implementation] task3\n",
        )
        _write_todo_state(repo, {})  # pre-sync will pick up bullets

        outcome = _run(
            state_path,
            decision="design",
            auto_mode=True,
            auto_decision=True,
        )

        assert outcome.stage_after == "human_review"
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"


# ---------------------------------------------------------------------------
# Test Class 5: TestMultiDeviceEndToEnd (Full scenario)
# ---------------------------------------------------------------------------

class TestMultiDeviceEndToEnd:
    """Full Dr. Maya multi-device scenario tests."""

    def test_local_to_slurm_handoff_happy_path(self, tmp_path: Path) -> None:
        """Walk through the full Dr. Maya scenario:
        hypothesis->design->impl->review->launch (LOCAL), then
        SLURM launch->extract->update_docs->decide_repeat (REMOTE), then
        decide_repeat->stop (LOCAL).
        Assert each transition and final state.
        """
        iteration_id = "iter_test_001"

        # --- LOCAL device: hypothesis -> launch ---
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="hypothesis")

        # hypothesis -> design
        _seed_hypothesis(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "design"

        # design -> implementation
        _seed_design(it_dir, iteration_id)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation"

        # implementation -> implementation_review
        _seed_implementation(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation_review"

        # implementation_review -> launch
        _seed_review_pass(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "launch"

        # --- Simulate "git push + pull" (same repo, just continue) ---

        # --- REMOTE device: SLURM launch -> decide_repeat ---

        # launch -> extract_results (SLURM with completed sync)
        _seed_slurm_launch(it_dir, sync_status="completed")
        _write_slurm_ledger(repo, "run_001")
        outcome = _run(state_path)
        assert outcome.stage_after == "extract_results"
        persisted = _read_state(repo)
        assert persisted["last_run_id"] == "run_001"
        assert persisted["sync_status"] == "completed"

        # extract_results -> update_docs
        _seed_slurm_extract(it_dir, "run_001")
        outcome = _run(state_path)
        assert outcome.stage_after == "update_docs"

        # update_docs -> decide_repeat
        _seed_update_docs(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "decide_repeat"

        # --- LOCAL device: decide_repeat -> stop ---
        outcome = _run(state_path, decision="stop")
        assert outcome.stage_after == "stop"

        # Final state verification
        persisted = _read_state(repo)
        assert persisted["stage"] == "stop"
        assert persisted["stage_attempt"] == 0

    def test_slurm_launch_pending_then_local_check_double_counts(self, tmp_path: Path) -> None:
        """Seed SLURM launch with pending sync.  Run once ('REMOTE' check,
        attempt=1).  Run again ('LOCAL' check, attempt=2).  Assert the single
        wait condition consumed 2 attempts.  Documents GAP-4 + GAP-8.
        """
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="launch", stage_attempt=0, max_stage_attempts=5,
        )
        _seed_slurm_launch(it_dir, sync_status="pending")
        _write_slurm_ledger(repo, "run_001")

        # "REMOTE" device checks
        outcome_remote = _run(state_path)
        assert outcome_remote.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] == "launch"
        assert persisted["stage_attempt"] == 1

        # "LOCAL" device checks the same (pushed/pulled) state
        outcome_local = _run(state_path)
        assert outcome_local.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] == "launch"
        assert persisted["stage_attempt"] == 2

        # Single wait condition (SLURM job still syncing) consumed 2 attempts
        # across two "devices"
