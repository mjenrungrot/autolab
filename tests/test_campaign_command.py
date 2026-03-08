from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import autolab.commands as commands_module
import pytest
from autolab.campaign import (
    _campaign_backfill_lock_contract,
    _campaign_apply_challenger_outcome,
    _campaign_build_morning_report_payload,
    _campaign_has_champion_snapshot,
    _campaign_render_morning_report,
    _campaign_results_markdown_path,
    _campaign_results_tsv_path,
    _campaign_seed_champion_snapshot,
    _refresh_campaign_results,
)
from autolab.checkpoint import list_checkpoints
from autolab.cli.handlers_campaign import (
    _cmd_campaign_continue,
    _cmd_campaign_start,
    _cmd_campaign_status,
    _cmd_campaign_stop,
    _run_campaign_session,
)
from autolab.cli.parser import _build_parser
from autolab.handoff import refresh_handoff
from autolab.utils import _append_log

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


def _init_repo_state(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    _init_git_repo(repo)
    return (repo, state_path)


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_AUTHOR_NAME": "Autolab Test",
            "GIT_AUTHOR_EMAIL": "autolab-test@example.invalid",
            "GIT_COMMITTER_NAME": "Autolab Test",
            "GIT_COMMITTER_EMAIL": "autolab-test@example.invalid",
        }
    )
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )


def _assert_git_ok(result: subprocess.CompletedProcess[str], *, label: str) -> None:
    assert result.returncode == 0, (
        f"{label} failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def _init_git_repo(repo: Path) -> None:
    _assert_git_ok(_run_git(repo, "init"), label="git init")
    _assert_git_ok(_run_git(repo, "add", "-A"), label="git add")
    _assert_git_ok(
        _run_git(repo, "commit", "--allow-empty", "-m", "seed repo"),
        label="git commit",
    )


def _load_state(state_path: Path) -> dict[str, object]:
    return json.loads(state_path.read_text(encoding="utf-8"))


def _write_state(state_path: Path, **updates: object) -> dict[str, object]:
    state = _load_state(state_path)
    state.update(updates)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def _seed_baseline_run(
    repo: Path,
    state_path: Path,
    *,
    run_id: str = "run_baseline",
    metric_value: float = 1.0,
    memory: str = "8GB",
    manifest_status: str = "completed",
    metrics_status: str = "completed",
    sync_status: str = "completed",
    started_at: str | None = None,
    completed_at: str | None = None,
    host_mode: str = "local",
    job_id: str = "",
) -> Path:
    state = _write_state(state_path, last_run_id=run_id)
    iteration_id = str(state["iteration_id"])
    if started_at is None:
        started_at = (
            "2026-03-07T23:50:00Z"
            if run_id == "run_baseline"
            else "2026-03-08T00:10:00Z"
        )
    if completed_at is None and manifest_status in {
        "completed",
        "failed",
        "partial",
        "synced",
    }:
        completed_at = (
            "2026-03-07T23:55:00Z"
            if run_id == "run_baseline"
            else "2026-03-08T00:15:00Z"
        )
    run_dir = repo / "experiments" / "plan" / iteration_id / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": iteration_id,
                "run_id": run_id,
                "status": metrics_status,
                "primary_metric": {
                    "name": "primary_metric",
                    "value": metric_value if metrics_status == "completed" else None,
                    "delta_vs_baseline": 0.0
                    if run_id == "run_baseline"
                    else (
                        metric_value - 1.0 if metrics_status == "completed" else None
                    ),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "run_id": run_id,
                "iteration_id": iteration_id,
                "status": manifest_status,
                "host_mode": host_mode,
                **({"job_id": job_id} if job_id else {}),
                "resource_request": {"memory": memory},
                "artifact_sync_to_local": {"status": sync_status},
                "timestamps": {
                    "started_at": started_at,
                    **({"completed_at": completed_at} if completed_at else {}),
                },
                "started_at": started_at,
                **({"completed_at": completed_at} if completed_at else {}),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def _write_campaign(repo: Path, payload: dict[str, object]) -> Path:
    path = repo / ".autolab" / "campaign.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _load_campaign_file(repo: Path) -> dict[str, object]:
    return json.loads((repo / ".autolab" / "campaign.json").read_text(encoding="utf-8"))


def _write_lock(
    repo: Path, *, state_path: Path, command: str = "autolab campaign continue"
) -> Path:
    now = commands_module._utc_now()
    lock_path = repo / ".autolab" / "lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "pid": 43210,
                "host": "test-host",
                "owner_uuid": "campaign-owner",
                "started_at": now,
                "last_heartbeat_at": now,
                "command": command,
                "state_file": str(state_path),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return lock_path


def _campaign_payload(
    state_path: Path,
    *,
    status: str = "stopped",
    scope_kind: str = "experiment",
    lock_mode: str = "none",
    lock_contract: dict[str, object] | None = None,
    no_improvement_streak: int = 0,
    crash_streak: int = 0,
    active_candidate: dict[str, object] | None = None,
    last_governance_event: dict[str, object] | None = None,
    idea_journal: dict[str, object] | None = None,
) -> dict[str, object]:
    state = _load_state(state_path)
    normalized_lock_mode = str(lock_mode).strip().lower() or "none"
    return {
        "campaign_id": "campaign_test",
        "label": "nightly-search",
        "scope_kind": scope_kind,
        "iteration_id": str(state["iteration_id"])
        if scope_kind == "experiment"
        else "",
        "objective_metric": "primary_metric",
        "objective_mode": "maximize",
        "status": status,
        "design_locked": normalized_lock_mode in {"design", "harness"},
        "harness_locked": normalized_lock_mode == "harness",
        "lock_contract": dict(lock_contract or {}),
        "champion_run_id": str(state["last_run_id"]),
        "champion_revision_label": "unversioned-worktree",
        "no_improvement_streak": no_improvement_streak,
        "crash_streak": crash_streak,
        "started_at": "2026-03-08T00:00:00Z",
        "last_oracle_at": "",
        "oracle_feedback": [],
        "active_candidate": dict(
            active_candidate
            or {
                "decision": "",
                "started_at": "",
                "run_id": "",
                "fix_attempts": 0,
                "timeout_reference_seconds": 0.0,
                "journal_entry_id": "",
                "family_hint": "",
                "thesis_hint": "",
            }
        ),
        "last_governance_event": dict(
            last_governance_event
            or {
                "at": "",
                "category": "",
                "run_id": "",
                "reason": "",
            }
        ),
        "idea_journal": dict(
            idea_journal
            or {
                "active_entry_id": "",
                "next_entry_seq": 1,
                "retained_entry_limit": 100,
                "entries": [],
                "family_stats": {},
            }
        ),
    }


def _set_decide_repeat_state(state_path: Path, *, run_id: str) -> dict[str, object]:
    return _write_state(state_path, stage="decide_repeat", last_run_id=run_id)


def _write_source_file(repo: Path, rel_path: str, content: str) -> Path:
    path = repo / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _write_plan_approval(
    repo: Path,
    state_path: Path,
    *,
    requires_approval: bool,
    tasks_total: int = 0,
    waves_total: int = 0,
) -> Path:
    state = _load_state(state_path)
    iteration_dir = repo / "experiments" / "plan" / str(state["iteration_id"])
    path = iteration_dir / "plan_approval.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-08T00:00:00Z",
                "iteration_id": str(state["iteration_id"]),
                "status": "pending" if requires_approval else "not_required",
                "requires_approval": requires_approval,
                "plan_hash": "plan-hash",
                "risk_fingerprint": "risk-fingerprint",
                "trigger_reasons": ["project_wide_tasks_present"]
                if requires_approval
                else [],
                "counts": {
                    "tasks_total": tasks_total,
                    "waves_total": waves_total,
                    "project_wide_tasks": 1 if requires_approval else 0,
                    "project_wide_unique_paths": 1 if requires_approval else 0,
                    "observed_retries": 0,
                    "stage_attempt": 0,
                },
                "source_paths": [],
                "reviewed_by": "",
                "reviewed_at": "",
                "notes": "",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _update_campaign_policy(
    repo: Path,
    *,
    complexity_proxy: str | None = None,
    change_size_metric: str | None = None,
    project_wide_root: str | None = None,
    max_fix_attempts_per_idea: int | None = None,
    max_timeout_factor: float | None = None,
    max_no_improvement_streak: int | None = None,
    max_crash_streak_before_rethink: int | None = None,
) -> None:
    if yaml is None:  # pragma: no cover
        raise AssertionError("PyYAML is required for campaign policy tests")
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    payload = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    autorun = payload.setdefault("autorun", {})
    assert isinstance(autorun, dict)
    campaign = autorun.setdefault("campaign", {})
    assert isinstance(campaign, dict)
    if complexity_proxy is not None:
        campaign["complexity_proxy"] = complexity_proxy
    if change_size_metric is not None:
        campaign["change_size_metric"] = change_size_metric
    if max_fix_attempts_per_idea is not None:
        campaign["max_fix_attempts_per_idea"] = max_fix_attempts_per_idea
    if max_timeout_factor is not None:
        campaign["max_timeout_factor"] = max_timeout_factor
    if max_no_improvement_streak is not None:
        campaign["max_no_improvement_streak"] = max_no_improvement_streak
    if max_crash_streak_before_rethink is not None:
        campaign["max_crash_streak_before_rethink"] = max_crash_streak_before_rethink
    if project_wide_root is not None:
        scope_roots = payload.setdefault("scope_roots", {})
        assert isinstance(scope_roots, dict)
        scope_roots["project_wide_root"] = project_wide_root
    policy_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _read_results_tsv(path: Path) -> list[dict[str, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split("\t")
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        values = line.split("\t")
        rows.append(dict(zip(header, values[: len(header)], strict=False)))
    return rows


def _campaign_checkpoint_entries(
    repo: Path,
    campaign: dict[str, object],
) -> list[dict[str, object]]:
    label = f"campaign_champion_{campaign['campaign_id']}"
    return [
        entry
        for entry in list_checkpoints(repo, iteration_id=str(campaign["iteration_id"]))
        if entry.get("label") == label
    ]


def _checkpoint_by_id(
    checkpoints: list[dict[str, object]],
    checkpoint_id: str,
) -> dict[str, object]:
    for entry in checkpoints:
        if entry.get("checkpoint_id") == checkpoint_id:
            return entry
    raise AssertionError(f"checkpoint {checkpoint_id} not found")


def test_campaign_parser_accepts_subcommands() -> None:
    parser = _build_parser()

    start_args = parser.parse_args(
        [
            "campaign",
            "start",
            "--state-file",
            "custom-state.json",
            "--label",
            "nightly",
            "--scope",
            "experiment",
            "--lock",
            "design",
            "--lock",
            "harness",
        ]
    )
    status_args = parser.parse_args(["campaign", "status"])
    stop_args = parser.parse_args(["campaign", "stop"])
    continue_args = parser.parse_args(["campaign", "continue"])
    run_args = parser.parse_args(["run", "--decision", "implementation"])

    assert start_args.state_file == "custom-state.json"
    assert start_args.label == "nightly"
    assert start_args.scope == "experiment"
    assert start_args.lock == ["design", "harness"]
    assert getattr(start_args.handler, "__name__", "") == _cmd_campaign_start.__name__
    assert getattr(status_args.handler, "__name__", "") == _cmd_campaign_status.__name__
    assert getattr(stop_args.handler, "__name__", "") == _cmd_campaign_stop.__name__
    assert (
        getattr(continue_args.handler, "__name__", "")
        == _cmd_campaign_continue.__name__
    )
    assert run_args.decision == "implementation"


def test_campaign_start_requires_completed_baseline_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _repo, state_path = _init_repo_state(tmp_path)
    _write_state(state_path, stage="decide_repeat")
    _ = capsys.readouterr()

    exit_code = commands_module.main(
        [
            "campaign",
            "start",
            "--state-file",
            str(state_path),
            "--label",
            "nightly",
            "--scope",
            "experiment",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "requires an accepted baseline run" in captured.err


def test_campaign_start_writes_campaign_file_and_enters_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)
    _write_state(state_path, stage="decide_repeat")
    _write_source_file(repo, "src/champion.py", "VALUE = 'baseline'\n")

    seen: dict[str, object] = {}

    def _run_campaign_session_stub(resolved_state_path: Path) -> int:
        seen["state_path"] = resolved_state_path
        return 0

    monkeypatch.setattr(
        "autolab.cli.handlers_campaign._run_campaign_session",
        _run_campaign_session_stub,
    )
    monkeypatch.setattr(
        commands_module, "_run_campaign_session", _run_campaign_session_stub
    )

    exit_code = commands_module.main(
        [
            "campaign",
            "start",
            "--state-file",
            str(state_path),
            "--label",
            "nightly",
            "--scope",
            "experiment",
        ]
    )
    output = capsys.readouterr().out
    campaign_payload = json.loads(
        (repo / ".autolab" / "campaign.json").read_text(encoding="utf-8")
    )

    assert exit_code == 0
    assert seen["state_path"] == state_path.resolve()
    assert "autolab campaign start" in output
    assert campaign_payload["label"] == "nightly"
    assert campaign_payload["scope_kind"] == "experiment"
    assert campaign_payload["objective_metric"] == "primary_metric"
    assert campaign_payload["objective_mode"] == "maximize"
    assert campaign_payload["status"] == "running"
    assert campaign_payload["champion_run_id"] == "run_baseline"
    assert _campaign_has_champion_snapshot(repo, campaign_payload)
    results_tsv = (
        repo
        / "experiments"
        / "plan"
        / str(campaign_payload["iteration_id"])
        / "results.tsv"
    )
    results_md = (
        repo
        / "experiments"
        / "plan"
        / str(campaign_payload["iteration_id"])
        / "results.md"
    )
    assert results_tsv.exists()
    assert results_md.exists()


def test_campaign_start_supports_harness_lock_and_captures_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)
    _write_state(state_path, stage="decide_repeat")

    monkeypatch.setattr(
        "autolab.cli.handlers_campaign._run_campaign_session",
        lambda _state_path: 0,
    )
    monkeypatch.setattr(commands_module, "_run_campaign_session", lambda _state_path: 0)

    exit_code = commands_module.main(
        [
            "campaign",
            "start",
            "--state-file",
            str(state_path),
            "--label",
            "nightly",
            "--scope",
            "experiment",
            "--lock",
            "harness",
            "--lock",
            "design",
        ]
    )
    output = capsys.readouterr().out
    campaign_payload = _load_campaign_file(repo)
    lock_contract = campaign_payload["lock_contract"]

    assert exit_code == 0
    assert "lock_mode: harness" in output
    assert campaign_payload["design_locked"] is True
    assert campaign_payload["harness_locked"] is True
    assert str(lock_contract["captured_at"]).strip()
    assert str(lock_contract["hypothesis_fingerprint"]).strip()
    assert str(lock_contract["design_fingerprint"]).strip()
    assert str(lock_contract["extract_parser_fingerprint"]).strip()
    assert str(lock_contract["evaluator_fingerprint"]).strip()
    assert lock_contract["remote_profile_name"] == "none"
    assert lock_contract["remote_profile_mode"] == "none"


def test_campaign_start_requires_decide_repeat_stage(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)

    exit_code = commands_module.main(
        [
            "campaign",
            "start",
            "--state-file",
            str(state_path),
            "--label",
            "nightly",
            "--scope",
            "experiment",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "must be 'decide_repeat'" in captured.err


def test_campaign_stop_sets_stop_requested_when_lock_is_active(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)
    _write_campaign(repo, _campaign_payload(state_path, status="running"))
    _write_lock(repo, state_path=state_path)

    exit_code = commands_module.main(
        ["campaign", "stop", "--state-file", str(state_path)]
    )
    output = capsys.readouterr().out
    campaign_payload = json.loads(
        (repo / ".autolab" / "campaign.json").read_text(encoding="utf-8")
    )

    assert exit_code == 0
    assert "status: stop_requested" in output
    assert campaign_payload["status"] == "stop_requested"


def test_campaign_status_prints_campaign_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)
    _write_campaign(
        repo,
        _campaign_payload(
            state_path,
            status="stopped",
            active_candidate={
                "decision": "implementation",
                "started_at": "2026-03-08T00:10:00Z",
                "run_id": "run_trial",
                "fix_attempts": 1,
                "timeout_reference_seconds": 300.0,
            },
            last_governance_event={
                "at": "2026-03-08T00:11:00Z",
                "category": "retry_candidate",
                "run_id": "run_trial",
                "reason": "recoverable challenger failure; retrying same idea (1/2)",
            },
            idea_journal={
                "active_entry_id": "idea_0002",
                "next_entry_seq": 3,
                "retained_entry_limit": 100,
                "entries": [
                    {
                        "entry_id": "idea_0001",
                        "decision": "implementation",
                        "started_at": "2026-03-08T00:00:00Z",
                        "updated_at": "2026-03-08T00:05:00Z",
                        "completed_at": "2026-03-08T00:05:00Z",
                        "status": "discard",
                        "attempt_count": 1,
                        "run_ids": ["run_prev"],
                        "thesis": "implementation search touching src/baseline.py",
                        "thesis_source": "heuristic",
                        "family_key": "implementation:prev",
                        "family_label": "src/baseline.py",
                        "family_source": "heuristic",
                        "touched_surfaces": ["src/baseline.py"],
                        "family_surfaces": ["src/baseline.py"],
                        "near_miss": False,
                        "outcome_reason": "primary metric did not improve",
                        "champion_before_run_id": "run_baseline",
                        "champion_after_run_id": "run_baseline",
                    },
                    {
                        "entry_id": "idea_0002",
                        "decision": "implementation",
                        "started_at": "2026-03-08T00:10:00Z",
                        "updated_at": "2026-03-08T00:11:00Z",
                        "completed_at": "",
                        "status": "active",
                        "attempt_count": 2,
                        "run_ids": ["run_trial"],
                        "thesis": "implementation search touching src/model.py",
                        "thesis_source": "heuristic",
                        "family_key": "implementation:model",
                        "family_label": "src/model.py",
                        "family_source": "heuristic",
                        "touched_surfaces": ["src/model.py"],
                        "family_surfaces": ["src/model.py"],
                        "near_miss": False,
                        "outcome_reason": "",
                        "champion_before_run_id": "run_baseline",
                        "champion_after_run_id": "",
                    },
                ],
                "family_stats": {
                    "implementation:prev": {
                        "family_label": "src/baseline.py",
                        "first_seen_at": "2026-03-08T00:00:00Z",
                        "last_seen_at": "2026-03-08T00:05:00Z",
                        "counts": {"keep": 0, "discard": 1, "crash": 0},
                        "near_miss_count": 0,
                        "last_outcome": "discard",
                        "last_thesis": "implementation search touching src/baseline.py",
                        "sample_surfaces": ["src/baseline.py"],
                    }
                },
            },
        ),
    )

    exit_code = commands_module.main(
        ["campaign", "status", "--state-file", str(state_path)]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "autolab campaign status" in output
    assert "campaign_id: campaign_test" in output
    assert "objective_metric: primary_metric" in output
    assert "status: stopped" in output
    assert "max_fix_attempts_per_idea: 2" in output
    assert "max_no_improvement_streak: 3" in output
    assert "active_candidate_run_id: run_trial" in output
    assert "last_governance_event_category: retry_candidate" in output
    assert "idea_journal_entry_count: 2" in output
    assert "idea_journal_active_family: src/model.py" in output
    assert "idea_journal_same_family_streak: 1" in output
    assert "resumable: True" in output
    assert "results_tsv:" in output
    assert "results_md:" in output


def test_refresh_handoff_prefers_campaign_continue_for_resumable_campaign(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path)
    _write_campaign(repo, _campaign_payload(state_path, status="stopped"))

    artifacts = refresh_handoff(state_path)

    assert artifacts.payload["campaign"]["status"] == "stopped"
    assert "idea_journal_entry_count" in artifacts.payload["campaign"]
    assert (
        artifacts.payload["recommended_next_command"]["command"]
        == "autolab campaign continue"
    )
    assert (
        artifacts.payload["safe_resume_point"]["command"] == "autolab campaign continue"
    )
    assert artifacts.payload["continuation_packet"]["campaign"]["status"] == "stopped"
    assert (
        "idea_journal_entry_count"
        in artifacts.payload["continuation_packet"]["campaign"]
    )
    assert any(
        entry.get("role") == "campaign"
        and entry.get("path") == ".autolab/campaign.json"
        for entry in artifacts.payload["continuation_packet"]["artifact_pointers"]
    )


def test_status_command_surfaces_campaign_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)
    _write_campaign(repo, _campaign_payload(state_path, status="error"))

    exit_code = commands_module.main(["status", "--state-file", str(state_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "campaign:" in output
    assert "status: error" in output
    assert "champion_run_id: run_baseline" in output
    assert "idea_journal_entry_count: 0" in output
    assert "max_crash_streak_before_rethink: 2" in output


def test_campaign_status_and_status_command_surface_lock_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)
    _write_state(state_path, stage="decide_repeat")
    monkeypatch.setattr(
        "autolab.cli.handlers_campaign._run_campaign_session",
        lambda _state_path: 0,
    )
    monkeypatch.setattr(commands_module, "_run_campaign_session", lambda _state_path: 0)
    assert (
        commands_module.main(
            [
                "campaign",
                "start",
                "--state-file",
                str(state_path),
                "--label",
                "nightly",
                "--scope",
                "experiment",
                "--lock",
                "design",
            ]
        )
        == 0
    )
    campaign_payload = _load_campaign_file(repo)
    campaign_payload["status"] = "stopped"
    _write_campaign(repo, campaign_payload)
    _ = capsys.readouterr()

    assert (
        commands_module.main(["campaign", "status", "--state-file", str(state_path)])
        == 0
    )
    campaign_status_output = capsys.readouterr().out
    assert commands_module.main(["status", "--state-file", str(state_path)]) == 0
    status_output = capsys.readouterr().out

    assert "lock_mode: design" in campaign_status_output
    assert "design_locked: True" in campaign_status_output
    assert "harness_locked: False" in campaign_status_output
    assert "lock_ok: True" in campaign_status_output
    assert "lock_drift: none" in campaign_status_output
    assert "lock_mode: design" in status_output
    assert "design_locked: True" in status_output
    assert "harness_locked: False" in status_output
    assert "lock_ok: True" in status_output
    assert "lock_drift: none" in status_output


def test_refresh_campaign_results_renders_keep_discard_crash_and_partial_rows(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path, metric_value=1.0, memory="12GB")
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_source_file(repo, "src/model.py", "VALUE = 'baseline'\n")
    campaign = _campaign_payload(state_path)
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    _write_source_file(repo, "src/model.py", "VALUE = 'keep'\n")
    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_keep",
        metric_value=2.0,
        memory="12GB",
        started_at="2026-03-08T00:10:00Z",
        completed_at="2026-03-08T00:15:00Z",
    )
    state = _set_decide_repeat_state(state_path, run_id="run_keep")
    keep_result = _campaign_apply_challenger_outcome(repo, state_path, state, campaign)
    updated_campaign = _load_campaign_file(repo)
    _append_log(
        repo,
        f"campaign promote: champion=run_baseline challenger=run_keep; {keep_result['summary']}",
    )

    _write_source_file(repo, "src/model.py", "VALUE = 'discard'\n")
    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_discard",
        metric_value=0.5,
        memory="12GB",
        started_at="2026-03-08T00:20:00Z",
        completed_at="2026-03-08T00:25:00Z",
    )
    state = _set_decide_repeat_state(state_path, run_id="run_discard")
    discard_result = _campaign_apply_challenger_outcome(
        repo, state_path, state, updated_campaign
    )
    updated_campaign = _load_campaign_file(repo)
    _append_log(
        repo,
        f"campaign discard: champion=run_keep challenger=run_discard; {discard_result['summary']}",
    )

    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_partial",
        metric_value=1.0,
        metrics_status="partial",
        manifest_status="partial",
        sync_status="failed",
        started_at="2026-03-08T00:30:00Z",
        completed_at="2026-03-08T00:35:00Z",
    )
    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_crash",
        metric_value=1.0,
        metrics_status="failed",
        manifest_status="failed",
        sync_status="failed",
        started_at="2026-03-08T00:40:00Z",
        completed_at="2026-03-08T00:45:00Z",
    )

    result = _refresh_campaign_results(repo, updated_campaign)
    rows = _read_results_tsv(Path(result["results_tsv_path"]))
    rows_by_run = {row["run_id"]: row for row in rows}

    assert [row["run_id"] for row in rows] == [
        "run_baseline",
        "run_keep",
        "run_discard",
        "run_partial",
        "run_crash",
    ]
    assert rows_by_run["run_baseline"]["status"] == "keep"
    assert rows_by_run["run_keep"]["status"] == "keep"
    assert rows_by_run["run_discard"]["status"] == "discard"
    assert rows_by_run["run_partial"]["status"] == "partial"
    assert rows_by_run["run_crash"]["status"] == "crash"
    results_md = Path(result["results_md_path"]).read_text(encoding="utf-8")
    assert "- keep: `2`" in results_md
    assert "- discard: `1`" in results_md
    assert "- partial: `1`" in results_md
    assert "- crash: `1`" in results_md
    assert "## Idea Journal" in results_md


def test_refresh_campaign_results_use_project_wide_scope_root(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    (repo / "src").mkdir(parents=True, exist_ok=True)
    _update_campaign_policy(repo, project_wide_root="src")
    _seed_baseline_run(repo, state_path, metric_value=1.0)
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    campaign = _campaign_payload(state_path, scope_kind="project_wide")
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    result = _refresh_campaign_results(repo, campaign)

    assert Path(result["results_tsv_path"]) == _campaign_results_tsv_path(
        repo, campaign
    )


def test_campaign_morning_report_payload_excludes_baseline_from_candidate_totals(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path, run_id="run_baseline", metric_value=1.0)
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    campaign = _campaign_payload(state_path, status="running")
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_keep",
        metric_value=1.3,
        started_at="2026-03-08T01:00:00Z",
        completed_at="2026-03-08T01:05:00Z",
    )
    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_discard",
        metric_value=0.8,
        started_at="2026-03-08T02:00:00Z",
        completed_at="2026-03-08T02:05:00Z",
    )
    log_path = repo / ".autolab" / "logs" / "orchestrator.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                "2026-03-08T01:06:00Z campaign promote: champion=run_baseline challenger=run_keep; improved metric",
                "2026-03-08T02:06:00Z campaign discard: champion=run_keep challenger=run_discard; regression",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    results_payload = _refresh_campaign_results(repo, campaign)
    report_payload = _campaign_build_morning_report_payload(
        repo,
        campaign,
        results_payload=results_payload,
        handoff_payload={
            "continuation_packet": {
                "next_action": {
                    "recommended_command": "autolab campaign continue",
                    "reason": "campaign is resumable",
                    "safe_status": "ready",
                }
            },
            "safe_resume_point": {
                "command": "autolab campaign continue",
                "status": "ready",
                "preconditions": [],
            },
        },
    )

    assert report_payload["candidate_total"] == 2
    counts = report_payload["candidate_counts"]
    assert isinstance(counts, dict)
    assert counts["keep"] == 1
    assert counts["discard"] == 1
    assert report_payload["best_delta"] == pytest.approx(0.3)
    assert report_payload["recommended_command"] == "autolab campaign continue"
    report_text = _campaign_render_morning_report(repo, campaign, report_payload)
    assert "- total_candidates: `2`" in report_text
    assert "best_primary_delta: `+0.3 via run_keep (keep)`" in report_text


def test_campaign_morning_report_payload_prefers_oracle_when_rethink_is_pending(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path, run_id="run_baseline", metric_value=1.0)
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    campaign = _campaign_payload(
        state_path,
        status="needs_rethink",
        last_governance_event={
            "at": "2026-03-08T03:00:00Z",
            "category": "stagnation_rethink",
            "run_id": "run_baseline",
            "reason": "campaign stagnated",
        },
    )
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    results_payload = _refresh_campaign_results(repo, campaign)
    report_payload = _campaign_build_morning_report_payload(
        repo,
        campaign,
        results_payload=results_payload,
        handoff_payload={
            "continuation_packet": {
                "next_action": {
                    "recommended_command": "autolab campaign continue",
                    "reason": "campaign is resumable",
                    "safe_status": "ready",
                }
            },
            "safe_resume_point": {
                "command": "autolab campaign continue",
                "status": "ready",
                "preconditions": [],
            },
        },
    )

    assert report_payload["oracle_required"] is True
    assert report_payload["recommended_command"] == "autolab oracle"


def test_refresh_handoff_includes_campaign_results_artifacts(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path)
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    campaign = _campaign_payload(state_path, status="stopped")
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)
    _refresh_campaign_results(repo, campaign)

    artifacts = refresh_handoff(state_path)
    pointers = artifacts.payload["continuation_packet"]["artifact_pointers"]

    assert any(
        entry.get("role") == "campaign_results_markdown"
        and entry.get("path") == "experiments/plan/bootstrap_iteration/results.md"
        and entry.get("inline_in_oracle") is True
        for entry in pointers
    )
    assert any(
        entry.get("role") == "campaign_results_tsv"
        and entry.get("path") == "experiments/plan/bootstrap_iteration/results.tsv"
        and entry.get("inline_in_oracle") is False
        for entry in pointers
    )


def test_campaign_seed_champion_snapshot_pins_latest_and_unpins_previous(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path)
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_source_file(repo, "src/model.py", "VALUE = 'baseline'\n")
    campaign = _campaign_payload(state_path)

    first_id = _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_source_file(repo, "src/model.py", "VALUE = 'updated'\n")
    second_id = _campaign_seed_champion_snapshot(repo, state_path, campaign)

    checkpoints = _campaign_checkpoint_entries(repo, campaign)
    first_entry = _checkpoint_by_id(checkpoints, first_id)
    second_entry = _checkpoint_by_id(checkpoints, second_id)

    assert first_id != second_id
    assert len(checkpoints) == 2
    assert second_entry["pinned"] is True
    assert not first_entry.get("pinned", False)


def test_campaign_apply_challenger_promotes_better_metric_and_rotates_snapshot(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path, metric_value=1.0, memory="12GB")
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_source_file(repo, "src/model.py", "VALUE = 'baseline'\n")
    campaign = _campaign_payload(state_path)
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    _write_source_file(repo, "src/model.py", "VALUE = 'challenger'\n")
    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_challenger",
        metric_value=2.0,
        memory="12GB",
    )
    state = _set_decide_repeat_state(state_path, run_id="run_challenger")

    result = _campaign_apply_challenger_outcome(repo, state_path, state, campaign)
    updated_campaign = _load_campaign_file(repo)
    checkpoints = _campaign_checkpoint_entries(repo, updated_campaign)
    pinned_entries = [entry for entry in checkpoints if entry.get("pinned", False)]

    assert result["action"] == "promote"
    assert updated_campaign["champion_run_id"] == "run_challenger"
    assert updated_campaign["no_improvement_streak"] == 0
    assert len(checkpoints) == 2
    assert len(pinned_entries) == 1
    assert "primary metric improved" in str(result["summary"])


def test_campaign_apply_challenger_records_idea_journal_for_promotion(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path, metric_value=1.0, memory="12GB")
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_source_file(repo, "src/model.py", "VALUE = 'baseline'\n")
    campaign = _campaign_payload(state_path)
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    _write_source_file(repo, "src/model.py", "VALUE = 'challenger'\n")
    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_challenger",
        metric_value=2.0,
        memory="12GB",
    )
    state = _set_decide_repeat_state(state_path, run_id="run_challenger")

    _campaign_apply_challenger_outcome(repo, state_path, state, campaign)
    updated_campaign = _load_campaign_file(repo)
    idea_journal = updated_campaign["idea_journal"]
    entries = idea_journal["entries"]

    assert idea_journal["active_entry_id"] == ""
    assert len(entries) == 1
    entry = entries[0]
    assert entry["status"] == "keep"
    assert entry["run_ids"] == ["run_challenger"]
    assert entry["family_label"] == "src/model.py"
    assert entry["champion_before_run_id"] == "run_baseline"
    assert entry["champion_after_run_id"] == "run_challenger"
    assert idea_journal["family_stats"][entry["family_key"]]["counts"]["keep"] == 1


def test_campaign_apply_challenger_discards_and_restores_tracked_and_untracked_files(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _write_source_file(repo, "src/model.py", "VALUE = 'baseline'\n")
    _assert_git_ok(_run_git(repo, "add", "src/model.py"), label="git add model.py")
    _assert_git_ok(
        _run_git(repo, "commit", "-m", "track baseline model"),
        label="git commit model.py",
    )
    _seed_baseline_run(repo, state_path, metric_value=1.0)
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_source_file(repo, "notes/champion.txt", "champion notes\n")
    campaign = _campaign_payload(state_path)
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    _write_source_file(repo, "src/model.py", "VALUE = 'challenger'\n")
    _write_source_file(repo, "notes/champion.txt", "challenger notes\n")
    _write_source_file(repo, "tmp/challenger_only.txt", "delete me\n")
    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_challenger",
        metric_value=0.5,
        memory="8GB",
    )
    state = _set_decide_repeat_state(state_path, run_id="run_challenger")

    result = _campaign_apply_challenger_outcome(repo, state_path, state, campaign)
    restored_state = _load_state(state_path)
    updated_campaign = _load_campaign_file(repo)

    assert result["action"] == "discard"
    assert updated_campaign["champion_run_id"] == "run_baseline"
    assert updated_campaign["no_improvement_streak"] == 1
    assert restored_state["last_run_id"] == "run_baseline"
    assert (repo / "src/model.py").read_text(encoding="utf-8") == "VALUE = 'baseline'\n"
    assert (repo / "notes/champion.txt").read_text(
        encoding="utf-8"
    ) == "champion notes\n"
    assert not (repo / "tmp/challenger_only.txt").exists()


def test_campaign_apply_challenger_discards_null_metric_without_counting_crash(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path, metric_value=1.0)
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    campaign = _campaign_payload(state_path)
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    run_dir = _seed_baseline_run(
        repo,
        state_path,
        run_id="run_challenger",
        metric_value=1.0,
    )
    metrics_path = run_dir / "metrics.json"
    metrics_payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics_payload["primary_metric"]["value"] = None
    metrics_path.write_text(
        json.dumps(metrics_payload, indent=2) + "\n", encoding="utf-8"
    )
    state = _set_decide_repeat_state(state_path, run_id="run_challenger")

    result = _campaign_apply_challenger_outcome(repo, state_path, state, campaign)
    updated_campaign = _load_campaign_file(repo)

    assert result["action"] == "discard"
    assert updated_campaign["no_improvement_streak"] == 1
    assert updated_campaign["crash_streak"] == 0


def test_campaign_apply_challenger_uses_memory_tie_break(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path, metric_value=1.0, memory="12GB")
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    campaign = _campaign_payload(state_path)
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_challenger",
        metric_value=1.0,
        memory="8GB",
    )
    state = _set_decide_repeat_state(state_path, run_id="run_challenger")

    result = _campaign_apply_challenger_outcome(repo, state_path, state, campaign)
    updated_campaign = _load_campaign_file(repo)

    assert result["action"] == "promote"
    assert updated_campaign["champion_run_id"] == "run_challenger"
    assert "memory tie-break" in str(result["summary"])


def test_campaign_apply_challenger_marks_near_miss_when_losing_tie_break(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path, metric_value=1.0, memory="8GB")
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_source_file(repo, "src/model.py", "VALUE = 'baseline'\n")
    campaign = _campaign_payload(state_path)
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    _write_source_file(repo, "src/model.py", "VALUE = 'challenger'\n")
    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_challenger",
        metric_value=1.0,
        memory="12GB",
    )
    state = _set_decide_repeat_state(state_path, run_id="run_challenger")

    result = _campaign_apply_challenger_outcome(repo, state_path, state, campaign)
    updated_campaign = _load_campaign_file(repo)
    idea_journal = updated_campaign["idea_journal"]
    entry = idea_journal["entries"][0]

    assert result["action"] == "discard"
    assert entry["status"] == "discard"
    assert entry["near_miss"] is True
    assert idea_journal["family_stats"][entry["family_key"]]["near_miss_count"] == 1


def test_campaign_apply_challenger_uses_complexity_tie_break(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path, metric_value=1.0, memory="8GB")
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_source_file(repo, "src/family_a.py", "A = 1\n")
    _write_source_file(repo, "src/family_b.py", "B = 1\n")
    campaign = _campaign_payload(state_path)
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    (repo / "src/family_b.py").unlink()
    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_challenger",
        metric_value=1.0,
        memory="8GB",
    )
    state = _set_decide_repeat_state(state_path, run_id="run_challenger")

    result = _campaign_apply_challenger_outcome(repo, state_path, state, campaign)
    updated_campaign = _load_campaign_file(repo)

    assert result["action"] == "promote"
    assert updated_campaign["champion_run_id"] == "run_challenger"
    assert "complexity tie-break" in str(result["summary"])


def test_campaign_apply_challenger_can_disable_complexity_tie_break(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _update_campaign_policy(repo, complexity_proxy="none")
    _seed_baseline_run(repo, state_path, metric_value=1.0, memory="8GB")
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_source_file(repo, "src/family_a.py", "A = 1\n")
    _write_source_file(repo, "src/family_b.py", "B = 1\n")
    campaign = _campaign_payload(state_path)
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    (repo / "src/family_b.py").unlink()
    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_challenger",
        metric_value=1.0,
        memory="8GB",
    )
    state = _set_decide_repeat_state(state_path, run_id="run_challenger")

    result = _campaign_apply_challenger_outcome(repo, state_path, state, campaign)
    updated_campaign = _load_campaign_file(repo)

    assert result["action"] == "discard"
    assert updated_campaign["champion_run_id"] == "run_baseline"
    assert "keeping existing champion" in str(result["summary"])


def test_campaign_apply_challenger_uses_policy_risk_tie_break(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _update_campaign_policy(repo, complexity_proxy="none")
    _seed_baseline_run(repo, state_path, metric_value=1.0, memory="8GB")
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_plan_approval(repo, state_path, requires_approval=False)
    campaign = _campaign_payload(state_path)
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)

    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_challenger",
        metric_value=1.0,
        memory="8GB",
    )
    _write_plan_approval(
        repo,
        state_path,
        requires_approval=True,
        tasks_total=4,
        waves_total=2,
    )
    state = _set_decide_repeat_state(state_path, run_id="run_challenger")

    result = _campaign_apply_challenger_outcome(repo, state_path, state, campaign)
    updated_campaign = _load_campaign_file(repo)

    assert result["action"] == "discard"
    assert updated_campaign["champion_run_id"] == "run_baseline"
    assert "policy-risk tie-break" in str(result["summary"])


def test_campaign_continue_backfills_legacy_snapshot_from_decide_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    campaign = _campaign_payload(state_path, status="stopped")
    _write_campaign(repo, campaign)

    seen: dict[str, object] = {}

    def _run_campaign_session_stub(resolved_state_path: Path) -> int:
        seen["state_path"] = resolved_state_path
        return 0

    monkeypatch.setattr(
        "autolab.cli.handlers_campaign._run_campaign_session",
        _run_campaign_session_stub,
    )
    monkeypatch.setattr(
        commands_module, "_run_campaign_session", _run_campaign_session_stub
    )

    exit_code = commands_module.main(
        ["campaign", "continue", "--state-file", str(state_path)]
    )

    assert exit_code == 0
    assert seen["state_path"] == state_path.resolve()
    assert _campaign_has_champion_snapshot(repo, campaign)


def test_campaign_continue_backfills_locked_legacy_contract_from_decide_repeat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    campaign = _campaign_payload(
        state_path,
        status="stopped",
        lock_mode="design",
        lock_contract={},
    )
    _write_campaign(repo, campaign)

    seen: dict[str, object] = {}

    def _run_campaign_session_stub(resolved_state_path: Path) -> int:
        seen["state_path"] = resolved_state_path
        return 0

    monkeypatch.setattr(
        "autolab.cli.handlers_campaign._run_campaign_session",
        _run_campaign_session_stub,
    )
    monkeypatch.setattr(
        commands_module, "_run_campaign_session", _run_campaign_session_stub
    )

    exit_code = commands_module.main(
        ["campaign", "continue", "--state-file", str(state_path)]
    )
    updated_campaign = _load_campaign_file(repo)

    assert exit_code == 0
    assert seen["state_path"] == state_path.resolve()
    assert _campaign_has_champion_snapshot(repo, updated_campaign)
    assert str(updated_campaign["lock_contract"]["captured_at"]).strip()
    assert str(updated_campaign["lock_contract"]["design_fingerprint"]).strip()


def test_campaign_continue_marks_locked_legacy_campaign_needs_rethink_outside_decide_repeat(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)
    _write_state(state_path, stage="implementation")
    campaign = _campaign_payload(
        state_path,
        status="stopped",
        lock_mode="design",
        lock_contract={},
    )
    _write_campaign(repo, campaign)

    exit_code = commands_module.main(
        ["campaign", "continue", "--state-file", str(state_path)]
    )
    captured = capsys.readouterr()
    updated_campaign = _load_campaign_file(repo)

    assert exit_code == 1
    assert "missing its lock contract" in captured.err
    assert updated_campaign["status"] == "needs_rethink"


def test_campaign_continue_marks_legacy_campaign_needs_rethink_outside_decide_repeat(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)
    _write_state(state_path, stage="implementation")
    campaign = _campaign_payload(state_path, status="stopped")
    _write_campaign(repo, campaign)

    exit_code = commands_module.main(
        ["campaign", "continue", "--state-file", str(state_path)]
    )
    captured = capsys.readouterr()
    updated_campaign = _load_campaign_file(repo)

    assert exit_code == 1
    assert "missing its champion snapshot" in captured.err
    assert updated_campaign["status"] == "needs_rethink"


def test_campaign_continue_stops_on_design_lock_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)
    _write_state(state_path, stage="decide_repeat")
    monkeypatch.setattr(
        "autolab.cli.handlers_campaign._run_campaign_session",
        lambda _state_path: 0,
    )
    monkeypatch.setattr(commands_module, "_run_campaign_session", lambda _state_path: 0)
    assert (
        commands_module.main(
            [
                "campaign",
                "start",
                "--state-file",
                str(state_path),
                "--label",
                "nightly",
                "--scope",
                "experiment",
                "--lock",
                "design",
            ]
        )
        == 0
    )
    campaign_payload = _load_campaign_file(repo)
    campaign_payload["status"] = "stopped"
    _write_campaign(repo, campaign_payload)

    hypothesis_path = (
        repo
        / "experiments"
        / "plan"
        / str(_load_state(state_path)["iteration_id"])
        / "hypothesis.md"
    )
    hypothesis_path.write_text(
        hypothesis_path.read_text(encoding="utf-8") + "\nDrift.\n",
        encoding="utf-8",
    )

    exit_code = commands_module.main(
        ["campaign", "continue", "--state-file", str(state_path)]
    )
    captured = capsys.readouterr()
    updated_campaign = _load_campaign_file(repo)

    assert exit_code == 1
    assert "hypothesis.md changed" in captured.err
    assert updated_campaign["status"] == "needs_rethink"


def test_campaign_continue_stops_on_harness_lock_evaluator_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _seed_baseline_run(repo, state_path)
    _write_state(state_path, stage="decide_repeat")
    monkeypatch.setattr(
        "autolab.cli.handlers_campaign._run_campaign_session",
        lambda _state_path: 0,
    )
    monkeypatch.setattr(commands_module, "_run_campaign_session", lambda _state_path: 0)
    assert (
        commands_module.main(
            [
                "campaign",
                "start",
                "--state-file",
                str(state_path),
                "--label",
                "nightly",
                "--scope",
                "experiment",
                "--lock",
                "harness",
            ]
        )
        == 0
    )
    campaign_payload = _load_campaign_file(repo)
    campaign_payload["status"] = "stopped"
    _write_campaign(repo, campaign_payload)

    workflow_path = repo / ".autolab" / "workflow.yaml"
    workflow_path.write_text(
        workflow_path.read_text(encoding="utf-8") + "\n# evaluator drift\n",
        encoding="utf-8",
    )

    exit_code = commands_module.main(
        ["campaign", "continue", "--state-file", str(state_path)]
    )
    captured = capsys.readouterr()
    updated_campaign = _load_campaign_file(repo)

    assert exit_code == 1
    assert "evaluator contract changed" in captured.err
    assert updated_campaign["status"] == "needs_rethink"


def test_locked_campaign_auto_decision_stays_on_implementation(
    tmp_path: Path,
) -> None:
    from autolab.run_standard import _run_once_standard

    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path)
    state = _set_decide_repeat_state(state_path, run_id="run_baseline")
    implementation_campaign = _campaign_backfill_lock_contract(
        repo,
        state,
        _campaign_payload(
            state_path,
            status="running",
            lock_mode="design",
        ),
    )
    _write_campaign(repo, implementation_campaign)

    implementation_outcome = _run_once_standard(
        state_path,
        decision=None,
        auto_decision=True,
        auto_mode=True,
        run_agent_mode="force_off",
    )
    auto_decision_payload = json.loads(
        (repo / ".autolab" / "auto_decision.json").read_text(encoding="utf-8")
    )

    assert implementation_outcome.exit_code == 0
    assert implementation_outcome.stage_after == "implementation"
    assert auto_decision_payload["outputs"]["selected_decision"] == "implementation"

    _set_decide_repeat_state(state_path, run_id="run_baseline")
    (
        repo / "experiments" / "plan" / "bootstrap_iteration" / "decision_result.json"
    ).unlink(missing_ok=True)
    retry_campaign = _campaign_backfill_lock_contract(
        repo,
        _load_state(state_path),
        _campaign_payload(
            state_path,
            status="running",
            lock_mode="design",
        )
        | {"no_improvement_streak": 1},
    )
    _write_campaign(repo, retry_campaign)
    _write_state(
        state_path,
        repeat_guard={
            "last_decision": "implementation",
            "same_decision_streak": 99,
            "last_open_task_count": 999,
            "no_progress_decisions": 99,
        },
    )

    retry_outcome = _run_once_standard(
        state_path,
        decision=None,
        auto_decision=True,
        auto_mode=True,
        run_agent_mode="force_off",
    )
    auto_decision_payload = json.loads(
        (repo / ".autolab" / "auto_decision.json").read_text(encoding="utf-8")
    )

    assert retry_outcome.exit_code == 0
    assert retry_outcome.stage_after == "implementation"
    assert auto_decision_payload["outputs"]["selected_decision"] == "implementation"


def test_campaign_session_retries_candidate_before_discard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path)
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_source_file(repo, "src/model.py", "VALUE = 'baseline'\n")
    campaign = _campaign_payload(
        state_path,
        status="running",
    )
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_state(state_path, stage="implementation", last_run_id="run_baseline")
    campaign["active_candidate"] = {
        "decision": "implementation",
        "started_at": "2026-03-08T00:10:00Z",
        "run_id": "",
        "fix_attempts": 0,
        "timeout_reference_seconds": 300.0,
    }
    _write_campaign(repo, campaign)

    monkeypatch.setattr(
        "autolab.cli.handlers_campaign._safe_refresh_handoff",
        lambda _state_path: ({}, ""),
    )

    calls = {"count": 0}

    def _cmd_loop_stub(_args) -> int:
        calls["count"] += 1
        if calls["count"] == 1:
            return 9
        payload = _load_campaign_file(repo)
        assert payload["active_candidate"]["fix_attempts"] == 1
        payload["status"] = "stop_requested"
        _write_campaign(repo, payload)
        return 0

    monkeypatch.setattr("autolab.cli.handlers_run._cmd_loop", _cmd_loop_stub)

    exit_code = _run_campaign_session(state_path)
    updated_campaign = _load_campaign_file(repo)

    assert exit_code == 0
    assert calls["count"] == 2
    assert updated_campaign["status"] == "stopped"
    assert updated_campaign["active_candidate"]["fix_attempts"] == 1
    assert updated_campaign["last_governance_event"]["category"] == "retry_candidate"
    assert updated_campaign["idea_journal"]["active_entry_id"]
    assert len(updated_campaign["idea_journal"]["entries"]) == 1
    entry = updated_campaign["idea_journal"]["entries"][0]
    assert entry["status"] == "active"
    assert entry["attempt_count"] == 2
    assert entry["family_label"] == "src/model.py"


def test_campaign_session_exhausted_candidate_triggers_oracle_rethink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _update_campaign_policy(
        repo,
        max_fix_attempts_per_idea=0,
        max_crash_streak_before_rethink=1,
    )
    _seed_baseline_run(repo, state_path)
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_source_file(repo, "src/model.py", "VALUE = 'baseline'\n")
    campaign = _campaign_payload(
        state_path,
        status="running",
        active_candidate={
            "decision": "implementation",
            "started_at": "2026-03-08T00:10:00Z",
            "run_id": "",
            "fix_attempts": 0,
            "timeout_reference_seconds": 300.0,
        },
    )
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_state(state_path, stage="implementation", last_run_id="run_baseline")
    _write_campaign(repo, campaign)

    monkeypatch.setattr(
        "autolab.cli.handlers_campaign._safe_refresh_handoff",
        lambda _state_path: ({}, ""),
    )

    def _export_oracle_document(*, repo_root: Path, **_kwargs):
        payload = _load_campaign_file(repo_root)
        payload["last_oracle_at"] = "2026-03-08T00:30:00Z"
        _write_campaign(repo_root, payload)
        output_path = repo_root / "oracle.md"
        output_path.write_text("# Oracle\n", encoding="utf-8")
        return (output_path, 0, "oracle test")

    monkeypatch.setattr(
        "autolab.cli.handlers_admin._export_oracle_document",
        _export_oracle_document,
    )
    monkeypatch.setattr("autolab.cli.handlers_run._cmd_loop", lambda _args: 9)

    exit_code = _run_campaign_session(state_path)
    updated_campaign = _load_campaign_file(repo)
    restored_state = _load_state(state_path)

    assert exit_code == 1
    assert updated_campaign["status"] == "needs_rethink"
    assert updated_campaign["no_improvement_streak"] == 1
    assert updated_campaign["crash_streak"] == 1
    assert updated_campaign["last_oracle_at"] == "2026-03-08T00:30:00Z"
    assert updated_campaign["last_governance_event"]["category"] == "crash_rethink"
    assert restored_state["stage"] == "decide_repeat"
    assert restored_state["last_run_id"] == "run_baseline"


def test_campaign_session_stagnation_exports_oracle_after_metric_discard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _update_campaign_policy(repo, max_no_improvement_streak=1)
    _seed_baseline_run(repo, state_path, metric_value=1.0)
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_source_file(repo, "src/model.py", "VALUE = 'baseline'\n")
    campaign = _campaign_payload(
        state_path,
        status="running",
        lock_mode="design",
    )
    campaign = _campaign_backfill_lock_contract(repo, _load_state(state_path), campaign)
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)
    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_challenger",
        metric_value=0.5,
    )
    _set_decide_repeat_state(state_path, run_id="run_challenger")

    monkeypatch.setattr(
        "autolab.cli.handlers_campaign._safe_refresh_handoff",
        lambda _state_path: ({}, ""),
    )

    def _export_oracle_document(*, repo_root: Path, **_kwargs):
        payload = _load_campaign_file(repo_root)
        payload["last_oracle_at"] = "2026-03-08T00:40:00Z"
        _write_campaign(repo_root, payload)
        output_path = repo_root / "oracle.md"
        output_path.write_text("# Oracle\n", encoding="utf-8")
        return (output_path, 0, "oracle test")

    monkeypatch.setattr(
        "autolab.cli.handlers_admin._export_oracle_document",
        _export_oracle_document,
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_run._cmd_loop",
        lambda _args: pytest.fail("campaign compare should not advance to a new loop"),
    )

    exit_code = _run_campaign_session(state_path)
    updated_campaign = _load_campaign_file(repo)
    restored_state = _load_state(state_path)

    assert exit_code == 1
    assert updated_campaign["status"] == "needs_rethink"
    assert updated_campaign["no_improvement_streak"] == 1
    assert updated_campaign["crash_streak"] == 0
    assert updated_campaign["last_governance_event"]["category"] == "stagnation_rethink"
    assert restored_state["stage"] == "decide_repeat"
    assert restored_state["last_run_id"] == "run_baseline"


def test_campaign_session_timeout_discards_slurm_candidate_and_exports_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _update_campaign_policy(
        repo,
        max_timeout_factor=2.0,
        max_crash_streak_before_rethink=1,
    )
    _seed_baseline_run(
        repo,
        state_path,
        started_at="2026-03-08T00:00:00Z",
        completed_at="2026-03-08T00:05:00Z",
    )
    _set_decide_repeat_state(state_path, run_id="run_baseline")
    _write_source_file(repo, "src/model.py", "VALUE = 'baseline'\n")
    campaign = _campaign_payload(
        state_path,
        status="running",
        active_candidate={
            "decision": "implementation",
            "started_at": "2026-03-08T00:10:00Z",
            "run_id": "run_timeout",
            "fix_attempts": 0,
            "timeout_reference_seconds": 300.0,
        },
    )
    _campaign_seed_champion_snapshot(repo, state_path, campaign)
    _write_campaign(repo, campaign)
    _seed_baseline_run(
        repo,
        state_path,
        run_id="run_timeout",
        metric_value=1.0,
        manifest_status="running",
        metrics_status="running",
        sync_status="pending",
        host_mode="slurm",
        job_id="12345",
        started_at="2026-03-07T00:00:00Z",
        completed_at=None,
    )
    _write_state(
        state_path,
        stage="slurm_monitor",
        last_run_id="run_baseline",
        pending_run_id="run_timeout",
    )

    monkeypatch.setattr(
        "autolab.cli.handlers_campaign._safe_refresh_handoff",
        lambda _state_path: ({}, ""),
    )
    monkeypatch.setattr(
        "autolab.campaign.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="",
        ),
    )

    def _export_oracle_document(*, repo_root: Path, **_kwargs):
        payload = _load_campaign_file(repo_root)
        payload["last_oracle_at"] = "2026-03-08T01:00:00Z"
        _write_campaign(repo_root, payload)
        output_path = repo_root / "oracle.md"
        output_path.write_text("# Oracle\n", encoding="utf-8")
        return (output_path, 0, "oracle test")

    monkeypatch.setattr(
        "autolab.cli.handlers_admin._export_oracle_document",
        _export_oracle_document,
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_run._cmd_loop",
        lambda _args: pytest.fail(
            "timed-out challengers should be handled before loop"
        ),
    )

    exit_code = _run_campaign_session(state_path)
    updated_campaign = _load_campaign_file(repo)
    restored_state = _load_state(state_path)
    timeout_manifest = json.loads(
        (
            repo
            / "experiments"
            / "plan"
            / str(restored_state["iteration_id"])
            / "runs"
            / "run_timeout"
            / "run_manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert exit_code == 1
    assert updated_campaign["status"] == "needs_rethink"
    assert updated_campaign["crash_streak"] == 1
    assert updated_campaign["last_governance_event"]["category"] == "crash_rethink"
    assert timeout_manifest["status"] == "failed"
    assert timeout_manifest["campaign_timeout"]["cancel_command"] == "scancel 12345"
    assert restored_state["stage"] == "decide_repeat"
    assert restored_state["last_run_id"] == "run_baseline"
