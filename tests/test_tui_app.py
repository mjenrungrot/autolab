from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("textual")

from autolab.tui import app as app_module
from autolab.tui.app import AutolabCockpitApp
from autolab.tui.models import (
    BacklogExperimentItem,
    BacklogHypothesisItem,
    CommandIntent,
)
from autolab.tui.snapshot import load_cockpit_snapshot


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


def _write_backlog_file(repo_root: Path) -> None:
    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    backlog_path.parent.mkdir(parents=True, exist_ok=True)
    backlog_path.write_text(
        (
            "hypotheses:\n"
            "  - id: h1\n"
            "    status: open\n"
            "    title: Hypothesis one\n"
            "experiments:\n"
            "  - id: e1\n"
            "    hypothesis_id: h1\n"
            "    status: open\n"
            "    type: plan\n"
            "    iteration_id: iter1\n"
        ),
        encoding="utf-8",
    )


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


def _assert_fullscreen_modal_dialog(app: AutolabCockpitApp, selector: str) -> None:
    dialog = app.screen.query_one(selector)
    region = dialog.region
    screen_size = app.screen.size
    assert region.x == 0
    assert region.y == 0
    assert region.width == screen_size.width
    assert region.height == screen_size.height


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
            await pilot.pause()
            _assert_fullscreen_modal_dialog(app, "#artifact-dialog")
            assert await _click_when_visible(pilot, "#close") is True

    asyncio.run(_run())


def test_home_shows_render_preview_card(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        prompt_path = repo_root / ".autolab" / "prompts" / "stage_design.md"
        prompt_path.write_text(
            "# Stage design\n\n" + "\n".join(f"line {index}" for index in range(1, 26)),
            encoding="utf-8",
        )
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            title = app.query_one("#home-render-title", app_module.Static)
            assert "What Autolab Will Run Now" in str(title.render())
            render_markdown = app.query_one(
                "#home-render-markdown", app_module.Markdown
            )
            assert "**Stage:** `design`" in render_markdown._markdown
            assert "line 25" in render_markdown._markdown

    asyncio.run(_run())


def test_files_buttons_open_rendered_prompt_and_context(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()

            await pilot.click("#file-open-rendered")
            await pilot.pause()
            _assert_fullscreen_modal_dialog(app, "#artifact-dialog")
            title = app.screen.query_one("#artifact-path", app_module.Label)
            assert "Rendered Prompt (design)" in str(title.render())
            rendered_content = app.screen.query_one(
                "#artifact-content", app_module.Markdown
            )
            assert "# Stage design" in rendered_content._markdown
            assert await _click_when_visible(pilot, "#close") is True

            await pilot.click("#file-open-context")
            await pilot.pause()
            _assert_fullscreen_modal_dialog(app, "#artifact-dialog")
            title = app.screen.query_one("#artifact-path", app_module.Label)
            assert "Render Context (design)" in str(title.render())
            context_content = app.screen.query_one(
                "#artifact-content", app_module.Markdown
            )
            assert context_content._markdown.startswith("```json\n")
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
            await pilot.pause()
            _assert_fullscreen_modal_dialog(app, "#unlock-dialog")
            assert await _click_when_visible(pilot, "#cancel") is True
            await pilot.pause()
            safety_status = app.query_one("#status-safety", app_module.Static)
            assert "Locked: read-only." in str(safety_status.render())

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


def test_advanced_buttons_hidden_by_default_and_visible_after_toggle(
    tmp_path: Path,
) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            advanced_row = app.query_one(
                "#file-advanced-buttons", app_module.Horizontal
            )
            assert advanced_row.display is False
            await pilot.press("x")
            await pilot.pause()
            assert advanced_row.display is True
            app.query_one("#file-focus-experiment", app_module.Button)
            app.query_one("#file-experiment-create", app_module.Button)
            app.query_one("#file-experiment-move", app_module.Button)

    asyncio.run(_run())


def test_run_preset_screen_composes_without_mount_error(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            app.push_screen(app_module.RunPresetScreen())
            await pilot.pause()

            assert isinstance(app.screen, app_module.RunPresetScreen)
            _assert_fullscreen_modal_dialog(app, "#run-preset-dialog")
            preset_list = app.screen.query_one("#run-preset-list", app_module.ListView)
            assert len(preset_list.children) == 3
            assert preset_list.index == 0

    asyncio.run(_run())


def test_loop_preset_screen_composes_without_mount_error(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            app.push_screen(app_module.LoopPresetScreen())
            await pilot.pause()

            assert isinstance(app.screen, app_module.LoopPresetScreen)
            _assert_fullscreen_modal_dialog(app, "#loop-preset-dialog")
            preset_list = app.screen.query_one("#loop-preset-list", app_module.ListView)
            assert len(preset_list.children) == 3
            assert preset_list.index == 0

    asyncio.run(_run())


def test_action_confirm_modal_uses_fullscreen_geometry(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            app.push_screen(
                app_module.ActionConfirmScreen(
                    title="Confirm action",
                    summary="Run verification for the current stage.",
                    command="autolab verify",
                    cwd=repo_root,
                    expected_writes=(".autolab/logs/verify.log",),
                )
            )
            await pilot.pause()

            assert isinstance(app.screen, app_module.ActionConfirmScreen)
            _assert_fullscreen_modal_dialog(app, "#action-confirm-dialog")
            assert await _click_when_visible(pilot, "#cancel") is True

    asyncio.run(_run())


def test_focus_modal_uses_fullscreen_geometry(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        screen = app_module.FocusExperimentScreen(
            experiments=(
                BacklogExperimentItem(
                    experiment_id="e1",
                    iteration_id="iter1",
                    hypothesis_id="h1",
                    experiment_type="plan",
                    status="open",
                    is_current=True,
                ),
            ),
            backlog_error="",
        )
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            app.push_screen(screen)
            await pilot.pause()
            assert isinstance(app.screen, app_module.FocusExperimentScreen)
            _assert_fullscreen_modal_dialog(app, "#focus-dialog")
            assert await _click_when_visible(pilot, "#cancel") is True

    asyncio.run(_run())


def test_experiment_create_modal_prefills_suggested_ids(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        screen = app_module.ExperimentCreateScreen(
            experiments=(
                BacklogExperimentItem(
                    experiment_id="e1",
                    iteration_id="iter1",
                    hypothesis_id="h1",
                    experiment_type="plan",
                    status="open",
                    is_current=True,
                ),
            ),
            hypotheses=(
                BacklogHypothesisItem(
                    hypothesis_id="h1",
                    title="Hypothesis one",
                    status="open",
                    is_completed=False,
                ),
            ),
        )
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            app.push_screen(screen)
            await pilot.pause()
            assert isinstance(app.screen, app_module.ExperimentCreateScreen)
            _assert_fullscreen_modal_dialog(app, "#experiment-create-dialog")
            experiment_id = app.screen.query_one(
                "#experiment-create-experiment-id", app_module.Input
            )
            iteration_id = app.screen.query_one(
                "#experiment-create-iteration-id", app_module.Input
            )
            assert experiment_id.value == "e2"
            assert iteration_id.value == "iter2"
            assert await _click_when_visible(pilot, "#cancel") is True

    asyncio.run(_run())


def test_experiment_move_modal_blocks_noop_destination(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        notices: list[str] = []
        screen = app_module.ExperimentMoveScreen(
            experiments=(
                BacklogExperimentItem(
                    experiment_id="e1",
                    iteration_id="iter1",
                    hypothesis_id="h1",
                    experiment_type="plan",
                    status="open",
                    is_current=True,
                ),
            ),
            backlog_error="",
        )
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            app.push_screen(screen)
            await pilot.pause()
            assert isinstance(app.screen, app_module.ExperimentMoveScreen)
            _assert_fullscreen_modal_dialog(app, "#experiment-move-dialog")
            target_list = app.screen.query_one(
                "#experiment-move-target-list", app_module.ListView
            )
            target_list.index = 0
            app.screen.notify = lambda message, *args, **kwargs: notices.append(
                str(message)
            )
            await pilot.click("#continue")
            await pilot.pause()
            assert notices
            assert "Destination type matches source type." in notices[-1]
            assert await _click_when_visible(pilot, "#cancel") is True

    asyncio.run(_run())


def test_execute_action_focus_create_move_starts_expected_commands(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    state_path = _write_state_file(repo_root)
    _write_backlog_file(repo_root)
    app = AutolabCockpitApp(state_path=state_path)
    app._snapshot = load_cockpit_snapshot(state_path)
    app._show_advanced = True
    started: list[CommandIntent] = []

    async def _unlock(_action) -> bool:
        return True

    async def _confirm(*, action, title, intent, confirm_label="Confirm") -> bool:
        return True

    selections = [
        ("e1", "iter1"),
        ("e2", "iter2", "h1"),
        ("e1", "iter1", "in_progress"),
    ]

    async def _push_screen_wait(_screen):
        return selections.pop(0)

    monkeypatch.setattr(app, "_unlock_if_needed", _unlock)
    monkeypatch.setattr(app, "_confirm_action_intent", _confirm)
    monkeypatch.setattr(app, "push_screen_wait", _push_screen_wait)
    monkeypatch.setattr(app, "_start_command", lambda intent: started.append(intent))

    asyncio.run(app._execute_action("focus_experiment"))
    asyncio.run(app._execute_action("experiment_create"))
    asyncio.run(app._execute_action("experiment_move"))

    assert len(started) == 3
    assert started[0].argv[:2] == ("autolab", "focus")
    assert started[1].argv[:3] == ("autolab", "experiment", "create")
    assert started[2].argv[:3] == ("autolab", "experiment", "move")
