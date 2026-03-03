from __future__ import annotations

import asyncio
import json
import time
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


def _write_state_file(repo_root: Path, *, stage: str = "design") -> Path:
    payload = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": stage,
        "stage_attempt": 0,
        "last_run_id": "",
        "sync_status": "completed",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    state_path = repo_root / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    prompt_path = repo_root / ".autolab" / "prompts" / f"stage_{stage}.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(f"# Stage {stage}\n", encoding="utf-8")

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


def _write_todo_state(repo_root: Path) -> None:
    todo_state_path = repo_root / ".autolab" / "todo_state.json"
    todo_state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 1,
        "next_order": 3,
        "tasks": {
            "t1": {
                "task_id": "t1",
                "status": "open",
                "priority": "critical",
                "source": "manual",
                "stage": "design",
                "task_class": "manual",
                "text": "Fix failing benchmark assertions",
                "first_seen_order": 1,
            },
            "t2": {
                "task_id": "t2",
                "status": "open",
                "priority": "medium",
                "source": "manual",
                "stage": "implementation",
                "task_class": "manual",
                "text": "Document rollout notes",
                "first_seen_order": 2,
            },
        },
    }
    todo_state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_run_manifest(
    repo_root: Path,
    run_id: str,
    started_at: str,
    *,
    host_mode: str = "local",
    job_id: str = "",
    sync_status: str = "",
) -> None:
    manifest_path = (
        repo_root
        / "experiments"
        / "plan"
        / "iter1"
        / "runs"
        / run_id
        / "run_manifest.json"
    )
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": run_id,
        "status": "running",
        "host_mode": host_mode,
        "timestamps": {"started_at": started_at},
    }
    if job_id:
        payload["job_id"] = job_id
        payload["slurm"] = {"job_id": job_id}
    if sync_status:
        payload["artifact_sync_to_local"] = {"status": sync_status}
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_design_prompt(repo_root: Path, *, lines: int = 40) -> None:
    prompt_path = repo_root / ".autolab" / "prompts" / "stage_design.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(
        "# Stage design\n\n" + "\n".join(f"line {index}" for index in range(1, lines)),
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


def _assert_artifact_viewer_edge_to_edge_layout(app: AutolabCockpitApp) -> None:
    dialog = app.screen.query_one("#artifact-dialog")
    title = app.screen.query_one("#artifact-path")
    scroll = app.screen.query_one("#artifact-scroll")
    buttons = app.screen.query_one("#artifact-buttons")

    # No dialog border/padding inset: content starts at dialog origin.
    assert title.region.x == dialog.region.x
    assert title.region.y == dialog.region.y

    # Button bar should stay compact rather than expanding as a flex row.
    assert buttons.region.height <= 3

    # Scroll region should consume the majority of the available vertical space.
    assert scroll.region.height > dialog.region.height // 2
    assert buttons.region.y >= scroll.region.y + scroll.region.height


def _list_item_label_texts(list_view: app_module.ListView) -> list[str]:
    labels: list[str] = []
    for item in list_view.children:
        label = item.query_one(app_module.Label)
        labels.append(str(label.render()))
    return labels


def _system_command_titles(app: AutolabCockpitApp) -> set[str]:
    return {command.title for command in app.get_system_commands(app.screen)}


def _write_run_files(repo_root: Path, *, run_id: str = "run-001") -> None:
    run_dir = repo_root / "experiments" / "plan" / "iter1" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_payload = {
        "run_id": run_id,
        "status": "completed",
        "timestamps": {
            "started_at": "2026-03-01T10:00:00Z",
            "completed_at": "2026-03-01T10:10:00Z",
        },
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_dir / "metrics.json").write_text('{"loss": 0.1}\n', encoding="utf-8")


def _materialize_all_current_stage_artifacts(state_path: Path) -> None:
    snapshot = load_cockpit_snapshot(state_path)
    stage_artifacts = snapshot.artifacts_by_stage.get(snapshot.current_stage, ())
    for artifact in [*stage_artifacts, *snapshot.common_artifacts]:
        if artifact.path.exists():
            continue
        artifact.path.parent.mkdir(parents=True, exist_ok=True)
        artifact.path.write_text("placeholder\n", encoding="utf-8")


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
            _assert_artifact_viewer_edge_to_edge_layout(app)
            assert await _click_when_visible(pilot, "#close") is True

    asyncio.run(_run())


def test_home_shows_render_preview_card(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        _write_design_prompt(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            title = app.query_one("#home-render-title", app_module.Static)
            assert "What Autolab Will Run Now" in str(title.render())
            render_markdown = app.query_one(
                "#home-render-markdown", app_module.Markdown
            )
            assert "**Stage:** `design`" in render_markdown._markdown
            assert "Excerpt shown." in render_markdown._markdown
            assert "line 39" not in render_markdown._markdown
            toggle_button = app.query_one("#home-render-toggle", app_module.Button)
            assert str(toggle_button.label) == "Show Full Prompt"

    asyncio.run(_run())


def test_home_prompt_toggle_switches_excerpt_and_full(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        _write_design_prompt(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            render_markdown = app.query_one(
                "#home-render-markdown", app_module.Markdown
            )
            assert "line 39" not in render_markdown._markdown

            await pilot.press("p")
            await pilot.pause()
            assert "line 39" in render_markdown._markdown
            toggle_button = app.query_one("#home-render-toggle", app_module.Button)
            assert str(toggle_button.label) == "Show Excerpt"

            await pilot.press("p")
            await pilot.pause()
            assert "line 39" not in render_markdown._markdown
            assert str(toggle_button.label) == "Show Full Prompt"

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
            filter_button = app.query_one(
                "#file-toggle-missing-filter", app_module.Button
            )
            assert "Filter: All" in str(filter_button.label)

            await pilot.click("#file-open-rendered")
            await pilot.pause()
            _assert_fullscreen_modal_dialog(app, "#artifact-dialog")
            _assert_artifact_viewer_edge_to_edge_layout(app)
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
            _assert_artifact_viewer_edge_to_edge_layout(app)
            title = app.screen.query_one("#artifact-path", app_module.Label)
            assert "Render Context (design)" in str(title.render())
            context_content = app.screen.query_one(
                "#artifact-content", app_module.Markdown
            )
            assert context_content._markdown.startswith("```json\n")
            assert await _click_when_visible(pilot, "#close") is True

    asyncio.run(_run())


def test_files_missing_filter_toggles_with_m_binding(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()

            context = app.query_one("#files-context", app_module.Static)
            assert "- Filter: all files" in str(context.render())

            await pilot.press("m")
            await pilot.pause()
            context = app.query_one("#files-context", app_module.Static)
            assert "- Filter: missing only" in str(context.render())
            filter_button = app.query_one(
                "#file-toggle-missing-filter", app_module.Button
            )
            assert "Filter: Missing Only" in str(filter_button.label)

            await pilot.press("m")
            await pilot.pause()
            context = app.query_one("#files-context", app_module.Static)
            assert "- Filter: all files" in str(context.render())

    asyncio.run(_run())


def test_files_name_filter_slash_focuses_input_and_clear_restores_list(
    tmp_path: Path,
) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            await pilot.press("/")
            await pilot.pause()

            focused = app.focused
            assert isinstance(focused, app_module.Input)
            assert focused.id == "artifact-filter-input"

            await pilot.press("d", "e", "s", "i", "g", "n")
            await pilot.pause()
            artifact_list = app.query_one("#artifact-list", app_module.ListView)
            filtered_entries = _list_item_label_texts(artifact_list)
            assert filtered_entries
            assert all("design" in entry.lower() for entry in filtered_entries)
            context = app.query_one("#files-context", app_module.Static)
            assert "- Name filter: design" in str(context.render())

            await pilot.click("#artifact-filter-clear")
            await pilot.pause()

            restored_entries = _list_item_label_texts(artifact_list)
            assert len(restored_entries) > len(filtered_entries)
            context = app.query_one("#files-context", app_module.Static)
            assert "- Name filter: none" in str(context.render())

    asyncio.run(_run())


def test_files_missing_filter_shows_only_missing_entries(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        snapshot = load_cockpit_snapshot(state_path)
        stage_artifacts = snapshot.artifacts_by_stage.get(snapshot.current_stage, ())
        assert stage_artifacts
        # Materialize exactly one expected artifact so both present and missing entries exist.
        known_existing = stage_artifacts[0].path
        known_existing.parent.mkdir(parents=True, exist_ok=True)
        known_existing.write_text("entrypoint: train.py\n", encoding="utf-8")
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()

            before_artifacts = tuple(app._current_artifacts)
            assert any(item.exists for item in before_artifacts)
            assert any(not item.exists for item in before_artifacts)

            await pilot.press("m")
            await pilot.pause()

            after_artifacts = tuple(app._current_artifacts)
            assert after_artifacts
            assert all(not item.exists for item in after_artifacts)

    asyncio.run(_run())


def test_files_missing_filter_empty_state_when_no_missing_files(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        _materialize_all_current_stage_artifacts(state_path)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()

            artifact_list = app.query_one("#artifact-list", app_module.ListView)
            entries = _list_item_label_texts(artifact_list)
            assert entries == ["(No missing files for this stage)"]
            context = app.query_one("#files-context", app_module.Static)
            assert "(No missing files for this stage)" in str(context.render())

    asyncio.run(_run())


def test_mode_quick_keys_dispatch_expected_actions(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        _write_run_files(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        dispatched: list[str] = []

        async def _fake_execute(action_id: str) -> None:
            dispatched.append(action_id)

        app._execute_action = _fake_execute  # type: ignore[method-assign]
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            expected_home_open = app._home_action_ids[app._home_action_index]

            await pilot.press("o")
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()

            await pilot.press("2")
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()

            await pilot.press("3")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()

        assert dispatched == [
            expected_home_open,
            "open_rendered_prompt",
            "open_selected_run_manifest",
            "open_selected_run_metrics",
            "open_selected_artifact_editor",
        ]

    asyncio.run(_run())


def test_runs_view_v_key_opens_selected_run_manifest(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        _write_run_files(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        dispatched: list[str] = []

        async def _fake_execute(action_id: str) -> None:
            dispatched.append(action_id)

        app._execute_action = _fake_execute  # type: ignore[method-assign]
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            await pilot.press("v")
            await pilot.pause()

        assert dispatched == ["open_selected_run_manifest"]

    asyncio.run(_run())


def test_files_view_n_key_jumps_to_next_missing_artifact(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        snapshot = load_cockpit_snapshot(state_path)
        stage_artifacts = snapshot.artifacts_by_stage.get(snapshot.current_stage, ())
        assert stage_artifacts
        stage_artifacts[0].path.parent.mkdir(parents=True, exist_ok=True)
        stage_artifacts[0].path.write_text("seed", encoding="utf-8")
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()

            app._selected_artifact_index = 0
            artifact_list = app.query_one("#artifact-list", app_module.ListView)
            artifact_list.index = 0
            app._update_files_context()
            app._update_ui_chrome()

            if all(item.exists for item in app._current_artifacts):
                # The fixture is expected to include missing artifacts.
                pytest.fail("Expected at least one missing artifact in files list.")
            await pilot.press("n")
            await pilot.pause()
            assert app._current_artifacts[app._selected_artifact_index].exists is False

    asyncio.run(_run())


def test_system_commands_are_contextual_for_files_filter(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            home_titles = _system_command_titles(app)
            assert "Go to Files view" in home_titles
            assert "Quick open selected item" in home_titles
            assert "Focus Files Name Filter" not in home_titles

            await pilot.press("3")
            await pilot.pause()
            files_titles = _system_command_titles(app)
            assert "Focus Files Name Filter" in files_titles
            assert "Toggle Files Missing-only Filter" in files_titles

            await pilot.press("/")
            await pilot.pause()
            await pilot.press("d", "e")
            await pilot.pause()
            filtered_titles = _system_command_titles(app)
            assert "Clear Files Name Filter" in filtered_titles

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


def test_escape_closes_unlock_modal(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()
            assert isinstance(app.screen, app_module.UnlockSafetyScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, app_module.UnlockSafetyScreen)
            safety_status = app.query_one("#status-safety", app_module.Static)
            assert "Locked: read-only." in str(safety_status.render())

    asyncio.run(_run())


def test_escape_closes_action_confirm_modal(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            app.push_screen(
                app_module.ActionConfirmScreen(
                    title="Confirm action",
                    summary="summary",
                    command="autolab verify",
                    cwd=repo_root,
                    expected_writes=(),
                )
            )
            await pilot.pause()
            assert isinstance(app.screen, app_module.ActionConfirmScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, app_module.ActionConfirmScreen)

    asyncio.run(_run())


def test_action_confirm_keyboard_shortcuts_toggle_details_and_confirm(
    tmp_path: Path,
) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        results: list[object] = []
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            app.push_screen(
                app_module.ActionConfirmScreen(
                    title="Confirm action",
                    summary="summary",
                    command="autolab verify",
                    cwd=repo_root,
                    expected_writes=("state.json",),
                ),
                callback=lambda result: results.append(result),
            )
            await pilot.pause()
            assert isinstance(app.screen, app_module.ActionConfirmScreen)

            details = app.screen.query_one("#action-confirm-details", app_module.Static)
            assert "Details hidden." in str(details.render())

            await pilot.press("d")
            await pilot.pause()
            details = app.screen.query_one("#action-confirm-details", app_module.Static)
            assert "Command:" in str(details.render())

            await pilot.press("enter")
            await pilot.pause()
            assert not isinstance(app.screen, app_module.ActionConfirmScreen)

        assert results == [True]

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


def test_view_cycle_shortcuts_switch_modes(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("]")
            await pilot.pause()
            mode_status = app.query_one("#status-mode", app_module.Static)
            assert "Mode: runs" in str(mode_status.render())

            await pilot.press("[")
            await pilot.pause()
            mode_status = app.query_one("#status-mode", app_module.Static)
            assert "Mode: home" in str(mode_status.render())

    asyncio.run(_run())


def test_key_hints_are_mode_aware_and_track_wrap_state(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            hints = app.query_one("#key-hints", app_module.Static)
            assert "Enter recommended action" in str(hints.render())
            assert "p prompt" in str(hints.render())

            await pilot.press("4")
            await pilot.pause()
            hints = app.query_one("#key-hints", app_module.Static)
            assert "w wrap(off)" in str(hints.render())

            await pilot.press("w")
            await pilot.pause()
            hints = app.query_one("#key-hints", app_module.Static)
            wrap_status = app.query_one("#status-console", app_module.Static)
            assert "w wrap(on)" in str(hints.render())
            assert "Console wrap: on" in str(wrap_status.render())

            await pilot.press("2")
            await pilot.pause()
            assert "m metrics" in str(hints.render())
            assert "v manifest" in str(hints.render())

            await pilot.press("3")
            await pilot.pause()
            assert "m missing-only(off)" in str(hints.render())
            assert "n next-missing" in str(hints.render())

    asyncio.run(_run())


def test_status_rail_shows_idle_counts(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            status_running = app.query_one("#status-running", app_module.Static)
            rendered = str(status_running.render())
            assert "Idle | runs:" in rendered
            assert "missing:" in rendered

    asyncio.run(_run())


def test_status_bar_tracks_running_duration_and_last_exit_summary(tmp_path: Path, monkeypatch) -> None:
    state_path = _write_state_file(tmp_path / "repo")
    app = AutolabCockpitApp(state_path=state_path)
    app._snapshot = load_cockpit_snapshot(state_path)
    running_intent = CommandIntent(
        action_id="verify_current_stage",
        argv=("autolab", "verify"),
        cwd=tmp_path / "repo",
        expected_writes=(),
        mutating=False,
    )

    monkeypatch.setattr(
        app,
        "call_from_thread",
        lambda callback, *args, **kwargs: callback(*args, **kwargs),
    )
    monkeypatch.setattr(app, "_refresh_snapshot", lambda: None)

    async def _run() -> None:
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            status_running = app.query_one("#status-running", app_module.Static)
            app._running_intent = running_intent
            app._command_started_at = time.perf_counter() - 1
            app._update_ui_chrome()
            running_rendered = str(status_running.render())
            assert "Running:" in running_rendered
            assert "[" in running_rendered

            app._handle_runner_done(2, False)
            await pilot.pause()
            idle_rendered = str(status_running.render())
            assert "last exit 2 in" in idle_rendered

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


def test_run_preset_advanced_row_auto_enables_checkbox(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        screen = app_module.RunPresetScreen()
        results: list[object] = []
        async with app.run_test(size=(220, 70)) as pilot:
            app.push_screen(screen, callback=lambda result: results.append(result))
            await pilot.pause()
            await pilot.press("down", "down")
            await pilot.pause()
            advanced = screen.query_one("#run-advanced", app_module.Checkbox)
            verify = screen.query_one("#run-verify", app_module.Checkbox)
            assert advanced.value is True
            assert verify.disabled is False
            await pilot.press("escape")
            await pilot.pause()
            assert results == [None]

    asyncio.run(_run())


def test_loop_preset_advanced_row_auto_enables_checkbox(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        screen = app_module.LoopPresetScreen()
        results: list[object] = []
        async with app.run_test(size=(220, 70)) as pilot:
            app.push_screen(screen, callback=lambda result: results.append(result))
            await pilot.pause()
            await pilot.press("down", "down")
            await pilot.pause()
            advanced = screen.query_one("#loop-advanced", app_module.Checkbox)
            max_iterations = screen.query_one("#loop-max-iterations", app_module.Input)
            assert advanced.value is True
            assert max_iterations.disabled is False
            await pilot.press("escape")
            await pilot.pause()
            assert results == [None]

    asyncio.run(_run())


def test_run_preset_enter_submits_selected_preset(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        screen = app_module.RunPresetScreen()
        results: list[object] = []
        async with app.run_test(size=(220, 70)) as pilot:
            app.push_screen(screen, callback=lambda result: results.append(result))
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert len(results) == 1
            result = results[0]
            assert result is not None
            assert result.verify is True
            assert result.auto_decision is False

    asyncio.run(_run())


def test_loop_preset_enter_submits_default_preset(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        screen = app_module.LoopPresetScreen()
        results: list[object] = []
        async with app.run_test(size=(220, 70)) as pilot:
            app.push_screen(screen, callback=lambda result: results.append(result))
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert len(results) == 1
            result = results[0]
            assert result is not None
            assert result.max_iterations == 2
            assert result.auto is False

    asyncio.run(_run())


def test_human_review_enter_submits_selected_decision(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        screen = app_module.HumanReviewDecisionScreen()
        results: list[object] = []
        async with app.run_test(size=(220, 70)) as pilot:
            app.push_screen(screen, callback=lambda result: results.append(result))
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert results == ["retry"]

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


def test_run_preset_agent_overrides_auto_resolve_conflict(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            app.push_screen(app_module.RunPresetScreen())
            await pilot.pause()
            await pilot.click("#run-advanced")
            await pilot.pause()
            await pilot.click("#run-agent-on")
            await pilot.pause()
            await pilot.click("#run-agent-off")
            await pilot.pause()

            agent_on = app.screen.query_one("#run-agent-on", app_module.Checkbox)
            agent_off = app.screen.query_one("#run-agent-off", app_module.Checkbox)
            assert agent_off.value is True
            assert agent_on.value is False

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


def test_loop_preset_agent_overrides_auto_resolve_conflict(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            app.push_screen(app_module.LoopPresetScreen())
            await pilot.pause()
            await pilot.click("#loop-advanced")
            await pilot.pause()
            await pilot.click("#loop-agent-on")
            await pilot.pause()
            await pilot.click("#loop-agent-off")
            await pilot.pause()

            agent_on = app.screen.query_one("#loop-agent-on", app_module.Checkbox)
            agent_off = app.screen.query_one("#loop-agent-off", app_module.Checkbox)
            assert agent_off.value is True
            assert agent_on.value is False

    asyncio.run(_run())


def test_human_review_modal_composes_without_mount_error(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            app.push_screen(app_module.HumanReviewDecisionScreen())
            await pilot.pause()

            assert isinstance(app.screen, app_module.HumanReviewDecisionScreen)
            _assert_fullscreen_modal_dialog(app, "#human-review-dialog")
            decision_list = app.screen.query_one(
                "#human-review-list", app_module.ListView
            )
            assert len(decision_list.children) == 3
            assert decision_list.index == 0

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


def test_run_details_include_artifact_presence_and_selection_counts(
    tmp_path: Path,
) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root, stage="extract_results")
        runs_root = repo_root / "experiments" / "plan" / "iter1" / "runs" / "run-1"
        runs_root.mkdir(parents=True, exist_ok=True)
        (runs_root / "run_manifest.json").write_text(
            json.dumps(
                {
                    "run_id": "run-1",
                    "status": "completed",
                    "timestamps": {"started_at": "2026-02-01T01:00:00Z"},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            details = app.query_one("#run-details", app_module.Static)
            rendered = str(details.render())
            assert "Selected: 1/1" in rendered
            assert "Manifest: OK" in rendered
            assert "Metrics: MISS" in rendered

    asyncio.run(_run())


def test_files_context_includes_selection_and_missing_counts(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("3")
            await pilot.pause()
            context = app.query_one("#files-context", app_module.Static)
            rendered = str(context.render())
            assert "Item: " in rendered
            assert "Missing files:" in rendered

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


def test_execute_action_resolve_human_review_starts_review_command(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    state_path = _write_state_file(repo_root, stage="human_review")
    app = AutolabCockpitApp(state_path=state_path)
    app._snapshot = load_cockpit_snapshot(state_path)
    started: list[CommandIntent] = []

    async def _unlock(_action) -> bool:
        return True

    async def _confirm(*, action, title, intent, confirm_label="Confirm") -> bool:
        return True

    async def _push_screen_wait(_screen):
        return "retry"

    monkeypatch.setattr(app, "_unlock_if_needed", _unlock)
    monkeypatch.setattr(app, "_confirm_action_intent", _confirm)
    monkeypatch.setattr(app, "push_screen_wait", _push_screen_wait)
    monkeypatch.setattr(app, "_start_command", lambda intent: started.append(intent))

    asyncio.run(app._execute_action("resolve_human_review"))

    assert len(started) == 1
    intent = started[0]
    assert intent.argv[:3] == ("autolab", "review", "--state-file")
    assert intent.argv[-2:] == ("--status", "retry")


def test_execute_action_resolve_human_review_guards_non_human_stage(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    state_path = _write_state_file(repo_root, stage="design")
    app = AutolabCockpitApp(state_path=state_path)
    app._snapshot = load_cockpit_snapshot(state_path)
    started: list[CommandIntent] = []
    notices: list[str] = []

    async def _unlock(_action) -> bool:
        return True

    async def _push_screen_wait(_screen):
        raise AssertionError("human review modal should not open outside human_review")

    monkeypatch.setattr(app, "_unlock_if_needed", _unlock)
    monkeypatch.setattr(app, "push_screen_wait", _push_screen_wait)
    monkeypatch.setattr(app, "_start_command", lambda intent: started.append(intent))
    monkeypatch.setattr(
        app, "notify", lambda message, *args, **kwargs: notices.append(str(message))
    )

    asyncio.run(app._execute_action("resolve_human_review"))

    assert started == []
    assert any("Human review can only be resolved" in item for item in notices)




def test_runs_view_shows_slurm_metadata(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        _write_run_manifest(
            repo_root,
            "run_slurm",
            "2026-02-01T02:00:00Z",
            host_mode="slurm",
            job_id="12345",
            sync_status="pending",
        )
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            run_details = app.query_one("#run-details", app_module.Static)
            rendered = str(run_details.render())
            assert "Host mode: slurm" in rendered
            assert "SLURM Job ID: 12345" in rendered
            assert "Artifact sync: pending" in rendered

    asyncio.run(_run())

def test_status_selection_updates_when_runs_selection_changes(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        _write_run_manifest(repo_root, "run_a", "2026-02-01T01:00:00Z")
        _write_run_manifest(repo_root, "run_b", "2026-02-01T02:00:00Z")
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("2")
            await pilot.pause()
            selection = app.query_one("#status-selection", app_module.Static)
            assert "Runs: 1/2" in str(selection.render())

            await pilot.press("down")
            await pilot.pause()
            selection = app.query_one("#status-selection", app_module.Static)
            assert "Runs: 2/2" in str(selection.render())

    asyncio.run(_run())


def test_home_todos_card_shows_open_tasks(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        _write_todo_state(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            todos = app.query_one("#home-todos-card", app_module.Static)
            rendered = str(todos.render())
            assert "Open Tasks" in rendered
            assert "[critical]" in rendered
            assert "Fix failing benchmark assertions" in rendered

    asyncio.run(_run())


def test_execute_action_focus_and_move_allow_manual_ids_without_backlog(
    tmp_path: Path, monkeypatch
) -> None:
    repo_root = tmp_path / "repo"
    state_path = _write_state_file(repo_root, stage="design")
    app = AutolabCockpitApp(state_path=state_path)
    app._snapshot = load_cockpit_snapshot(state_path)
    app._show_advanced = True
    started: list[CommandIntent] = []

    async def _unlock(_action) -> bool:
        return True

    async def _confirm(*, action, title, intent, confirm_label="Confirm") -> bool:
        return True

    selections = [("e10", "iter10"), ("e10", "iter10", "in_progress")]

    async def _push_screen_wait(_screen):
        return selections.pop(0)

    monkeypatch.setattr(app, "_unlock_if_needed", _unlock)
    monkeypatch.setattr(app, "_confirm_action_intent", _confirm)
    monkeypatch.setattr(app, "push_screen_wait", _push_screen_wait)
    monkeypatch.setattr(app, "_start_command", lambda intent: started.append(intent))

    asyncio.run(app._execute_action("focus_experiment"))
    asyncio.run(app._execute_action("experiment_move"))

    assert len(started) == 2
    assert started[0].argv[:2] == ("autolab", "focus")
    assert started[1].argv[:3] == ("autolab", "experiment", "move")


def test_start_command_preserves_console_history(tmp_path: Path, monkeypatch) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        started: list[CommandIntent] = []
        monkeypatch.setattr(app._runner, "start", lambda intent: started.append(intent))

        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            app._append_console("existing line")

            app._start_command(
                CommandIntent(
                    action_id="verify_current_stage",
                    argv=("autolab", "verify"),
                    cwd=repo_root,
                    expected_writes=(),
                    mutating=True,
                )
            )
            app._running_intent = None
            app._start_command(
                CommandIntent(
                    action_id="todo_sync",
                    argv=("autolab", "todo", "sync"),
                    cwd=repo_root,
                    expected_writes=(),
                    mutating=True,
                )
            )
            await pilot.pause()

            console_lines = list(app._console_tail)
            assert any("existing line" in line for line in console_lines)
            assert any("-" * 40 in line for line in console_lines)
            assert sum("starting:" in line for line in console_lines) == 2
            assert len(started) == 2

    asyncio.run(_run())


def test_start_command_preserves_prior_console_output(
    tmp_path: Path, monkeypatch
) -> None:
    app = AutolabCockpitApp(state_path=tmp_path / "repo" / ".autolab" / "state.json")
    app._console_tail.append("[12:00:00] previous line")
    lines: list[str] = []
    started: list[CommandIntent] = []
    intent = CommandIntent(
        action_id="verify_current_stage",
        argv=("autolab", "verify"),
        cwd=tmp_path,
        expected_writes=(),
        mutating=True,
    )

    class _FakeRunner:
        def start(self, run_intent: CommandIntent) -> None:
            started.append(run_intent)

    monkeypatch.setattr(app, "_append_console", lambda text: lines.append(text))
    monkeypatch.setattr(app, "_update_ui_chrome", lambda: None)
    app._runner = _FakeRunner()

    app._start_command(intent)

    assert app._console_tail
    assert "previous line" in app._console_tail[0]
    assert lines[0] == "-" * 40
    assert lines[1].startswith("starting: ")
    assert started == [intent]
