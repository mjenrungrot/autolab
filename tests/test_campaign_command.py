from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import autolab.commands as commands_module
import pytest
from autolab.campaign import (
    _campaign_apply_challenger_outcome,
    _campaign_has_champion_snapshot,
    _campaign_seed_champion_snapshot,
)
from autolab.checkpoint import list_checkpoints
from autolab.cli.handlers_campaign import (
    _cmd_campaign_continue,
    _cmd_campaign_start,
    _cmd_campaign_status,
    _cmd_campaign_stop,
)
from autolab.cli.parser import _build_parser
from autolab.handoff import refresh_handoff

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
) -> Path:
    state = _write_state(state_path, last_run_id=run_id)
    iteration_id = str(state["iteration_id"])
    run_dir = repo / "experiments" / "plan" / iteration_id / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": iteration_id,
                "run_id": run_id,
                "status": "completed",
                "primary_metric": {
                    "name": "primary_metric",
                    "value": metric_value,
                    "delta_vs_baseline": 0.0
                    if run_id == "run_baseline"
                    else metric_value - 1.0,
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
                "status": "completed",
                "host_mode": "local",
                "resource_request": {"memory": memory},
                "artifact_sync_to_local": {"status": "completed"},
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
) -> dict[str, object]:
    state = _load_state(state_path)
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
        "design_locked": False,
        "champion_run_id": str(state["last_run_id"]),
        "champion_revision_label": "unversioned-worktree",
        "no_improvement_streak": 0,
        "crash_streak": 0,
        "started_at": "2026-03-08T00:00:00Z",
        "last_oracle_at": "",
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
    policy_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


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
        ]
    )
    status_args = parser.parse_args(["campaign", "status"])
    stop_args = parser.parse_args(["campaign", "stop"])
    continue_args = parser.parse_args(["campaign", "continue"])

    assert start_args.state_file == "custom-state.json"
    assert start_args.label == "nightly"
    assert start_args.scope == "experiment"
    assert getattr(start_args.handler, "__name__", "") == _cmd_campaign_start.__name__
    assert getattr(status_args.handler, "__name__", "") == _cmd_campaign_status.__name__
    assert getattr(stop_args.handler, "__name__", "") == _cmd_campaign_stop.__name__
    assert (
        getattr(continue_args.handler, "__name__", "")
        == _cmd_campaign_continue.__name__
    )


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
    _write_campaign(repo, _campaign_payload(state_path, status="stopped"))

    exit_code = commands_module.main(
        ["campaign", "status", "--state-file", str(state_path)]
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "autolab campaign status" in output
    assert "campaign_id: campaign_test" in output
    assert "objective_metric: primary_metric" in output
    assert "status: stopped" in output
    assert "resumable: True" in output


def test_refresh_handoff_prefers_campaign_continue_for_resumable_campaign(
    tmp_path: Path,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _seed_baseline_run(repo, state_path)
    _write_campaign(repo, _campaign_payload(state_path, status="stopped"))

    artifacts = refresh_handoff(state_path)

    assert artifacts.payload["campaign"]["status"] == "stopped"
    assert (
        artifacts.payload["recommended_next_command"]["command"]
        == "autolab campaign continue"
    )
    assert (
        artifacts.payload["safe_resume_point"]["command"] == "autolab campaign continue"
    )
    assert artifacts.payload["continuation_packet"]["campaign"]["status"] == "stopped"
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
