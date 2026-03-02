from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("textual")

from autolab.tui import app as app_module
from autolab.tui.app import AutolabCockpitApp
from autolab.tui.models import CommandIntent


def _write_state_file(repo_root: Path) -> Path:
    payload = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "design",
        "stage_attempt": 0,
        "last_run_id": "",
        "sync_status": "completed",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    state_path = repo_root / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    prompt_path = repo_root / ".autolab" / "prompts" / "stage_design.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("# Stage design\n", encoding="utf-8")

    return state_path


async def _click_when_visible(
    pilot,
    selector: str,
    *,
    attempts: int = 30,
) -> bool:
    for _ in range(attempts):
        await pilot.pause()
        try:
            pilot.app.screen.query_one(selector)
        except Exception:
            continue
        await pilot.click(selector)
        await pilot.pause()
        return True
    return False


def test_refresh_snapshot_failure_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    app = AutolabCockpitApp(state_path=tmp_path / "repo" / ".autolab" / "state.json")
    app._armed = True

    logs: list[str] = []
    notices: list[str] = []

    monkeypatch.setattr(app, "_append_console", lambda text: logs.append(text))
    monkeypatch.setattr(
        app, "notify", lambda message, *args, **kwargs: notices.append(str(message))
    )
    monkeypatch.setattr(app, "_clear_snapshot_views", lambda: None)
    monkeypatch.setattr(app, "_update_ui_chrome", lambda: None)
    monkeypatch.setattr(
        app_module,
        "load_cockpit_snapshot",
        lambda _state_path: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    success = app._refresh_snapshot()
    assert success is False
    assert app._snapshot is None
    assert app._armed is False
    assert any("snapshot refresh failed: boom" in line for line in logs)
    assert any("Snapshot refresh failed: boom" in line for line in notices)


def test_action_refresh_snapshot_logs_success_only_on_true_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    app = AutolabCockpitApp(state_path=tmp_path / "repo" / ".autolab" / "state.json")
    logs: list[str] = []
    monkeypatch.setattr(app, "_append_console", lambda text: logs.append(text))
    monkeypatch.setattr(app, "_refresh_snapshot", lambda: False)
    app.action_refresh_snapshot()
    assert "snapshot refreshed" not in logs

    monkeypatch.setattr(app, "_refresh_snapshot", lambda: True)
    app.action_refresh_snapshot()
    assert logs == ["snapshot refreshed"]


def test_action_activate_selection_falls_back_to_mode_default_focus(
    tmp_path: Path, monkeypatch
) -> None:
    app = AutolabCockpitApp(state_path=tmp_path / "repo" / ".autolab" / "state.json")
    focused: list[str] = []
    monkeypatch.setattr(type(app), "focused", property(lambda _self: None))
    monkeypatch.setattr(app, "_focus_mode_default", lambda: focused.append("default"))
    app.action_activate_selection()
    assert focused == ["default"]


def test_runner_done_auto_locks_after_mutating_command(
    tmp_path: Path, monkeypatch
) -> None:
    app = AutolabCockpitApp(state_path=tmp_path / "repo" / ".autolab" / "state.json")
    app._armed = True
    app._running_intent = CommandIntent(
        action_id="verify_current_stage",
        argv=("autolab", "verify"),
        cwd=tmp_path,
        expected_writes=(),
        mutating=True,
    )
    logs: list[str] = []
    refreshed: list[bool] = []
    monkeypatch.setattr(app, "_append_console", lambda text: logs.append(text))
    monkeypatch.setattr(app, "_update_ui_chrome", lambda: None)
    monkeypatch.setattr(app, "_refresh_snapshot", lambda: refreshed.append(True))
    monkeypatch.setattr(
        app,
        "call_from_thread",
        lambda callback, *args, **kwargs: callback(*args, **kwargs),
    )

    app._handle_runner_done(0, False)
    assert app._armed is False
    assert app._running_intent is None
    assert refreshed == [True]
    assert "process exit code: 0" in logs


def test_refresh_snapshot_repeated_keys_no_crash(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()

    asyncio.run(_run())


def test_home_enter_opens_viewer_modal_and_closes(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            assert await _click_when_visible(pilot, "#close") is True

    asyncio.run(_run())


def test_unlock_modal_opens_and_cancel_keeps_locked(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("u")
            assert await _click_when_visible(pilot, "#cancel") is True
            await pilot.pause()
            safety_status = app.query_one("#status-safety", app_module.Static)
            assert "Locked (read-only)." in str(safety_status.render())

    asyncio.run(_run())


def test_mode_shortcut_switches_to_runs(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            mode_status = app.query_one("#status-mode", app_module.Static)
            assert "Mode: runs" in str(mode_status.render())

    asyncio.run(_run())
