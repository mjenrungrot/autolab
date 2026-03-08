from __future__ import annotations

import json
from pathlib import Path

import autolab.commands as commands_module
import pytest
from autolab.cli.handlers_campaign import (
    _cmd_campaign_continue,
    _cmd_campaign_start,
    _cmd_campaign_status,
    _cmd_campaign_stop,
)
from autolab.cli.parser import _build_parser
from autolab.handoff import refresh_handoff


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
    return (repo, state_path)


def _load_state(state_path: Path) -> dict[str, object]:
    return json.loads(state_path.read_text(encoding="utf-8"))


def _seed_baseline_run(
    repo: Path, state_path: Path, *, run_id: str = "run_baseline"
) -> Path:
    state = _load_state(state_path)
    state["last_run_id"] = run_id
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
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
                    "value": 1.0,
                    "delta_vs_baseline": 0.0,
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
