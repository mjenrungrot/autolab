from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("textual")

from autolab.tui import app as app_module
from autolab.tui.app import AutolabCockpitApp
from autolab.tui.models import (
    ArtifactItem,
    CockpitSnapshot,
    CommandIntent,
    RunItem,
    StageItem,
)


class _FakeLabel:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeListItem:
    def __init__(self, *_children, id: str | None = None) -> None:
        self.id = id
        self.removed = False

    def remove(self) -> None:
        self.removed = True


class _FakeListView:
    def __init__(self) -> None:
        self.children: list[object] = []
        self.index = 0

    def append(self, item: object) -> None:
        self.children.append(item)

    def clear(self) -> None:
        self.children.clear()


class _FakeStatic:
    def __init__(self) -> None:
        self.value = ""

    def update(self, value: str) -> None:
        self.value = value


def _bind_query(app: AutolabCockpitApp, widgets: dict[str, object]) -> None:
    app.query_one = lambda selector, _widget_type=None: widgets[selector]  # type: ignore[method-assign]


def _make_snapshot(tmp_path: Path) -> CockpitSnapshot:
    repo_root = tmp_path / "repo"
    state_path = repo_root / ".autolab" / "state.json"
    autolab_dir = repo_root / ".autolab"
    iteration_dir = repo_root / "experiments" / "plan" / "iter1"
    stage_items = (
        StageItem(name="design", status="upcoming", attempts="-", is_current=False),
        StageItem(
            name="implementation", status="current", attempts="0/3", is_current=True
        ),
        StageItem(
            name="implementation_review",
            status="upcoming",
            attempts="-",
            is_current=False,
        ),
    )
    run = RunItem(
        run_id="run_001",
        status="running",
        started_at="2026-02-01T01:00:00Z",
        completed_at="",
        manifest_path=iteration_dir / "runs" / "run_001" / "run_manifest.json",
        metrics_path=iteration_dir / "runs" / "run_001" / "metrics.json",
    )
    stage_artifact = ArtifactItem(
        path=iteration_dir / "implementation_plan.md",
        exists=False,
        source="stage",
    )
    common_artifact = ArtifactItem(
        path=repo_root / ".autolab" / "state.json",
        exists=False,
        source="common",
    )
    return CockpitSnapshot(
        repo_root=repo_root,
        state_path=state_path,
        autolab_dir=autolab_dir,
        iteration_dir=iteration_dir,
        current_stage="implementation",
        stage_attempt=0,
        max_stage_attempts=3,
        last_run_id="run_001",
        stage_items=stage_items,
        runs=(run,),
        todos=(),
        verification=None,
        top_blockers=(),
        stage_summaries={"implementation": "summary"},
        artifacts_by_stage={"implementation": (stage_artifact,)},
        common_artifacts=(common_artifact,),
    )


def test_refresh_snapshot_failure_is_fail_closed(tmp_path: Path, monkeypatch) -> None:
    app = AutolabCockpitApp(state_path=tmp_path / "repo" / ".autolab" / "state.json")
    app._snapshot = _make_snapshot(tmp_path)
    app._current_artifacts = (
        ArtifactItem(path=tmp_path / "stale.md", exists=True, source="stage"),
    )
    app._armed = True

    logs: list[str] = []
    notices: list[str] = []

    monkeypatch.setattr(app, "_append_console", lambda text: logs.append(text))
    monkeypatch.setattr(
        app, "notify", lambda message, *args, **kwargs: notices.append(str(message))
    )
    monkeypatch.setattr(
        app,
        "_clear_snapshot_views",
        lambda: (
            setattr(app, "_selected_stage_index", 0),
            setattr(app, "_selected_stage_key", None),
            setattr(app, "_selected_run_index", 0),
            setattr(app, "_selected_todo_index", 0),
            setattr(app, "_selected_artifact_index", 0),
            setattr(app, "_current_artifacts", ()),
        ),
    )
    monkeypatch.setattr(app, "_update_safety_row", lambda: None)
    monkeypatch.setattr(app, "_update_action_button_state", lambda: None)
    monkeypatch.setattr(
        app_module,
        "load_cockpit_snapshot",
        lambda _state_path: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    success = app._refresh_snapshot()
    assert success is False
    assert app._snapshot is None
    assert app._current_artifacts == ()
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


def test_action_activate_selection_focuses_action_list_when_no_focus(
    tmp_path: Path, monkeypatch
) -> None:
    app = AutolabCockpitApp(state_path=tmp_path / "repo" / ".autolab" / "state.json")
    action_list = _FakeStatic()
    focused: list[str] = []
    monkeypatch.setattr(type(app), "focused", property(lambda _self: None))
    _bind_query(
        app,
        {
            "#action-list": action_list,
        },
    )
    action_list.focus = lambda: focused.append("action-list")  # type: ignore[attr-defined]
    app.action_activate_selection()
    assert focused == ["action-list"]


def test_populate_stage_list_keeps_explicit_first_selection(
    tmp_path: Path, monkeypatch
) -> None:
    app = AutolabCockpitApp(state_path=tmp_path / "repo" / ".autolab" / "state.json")
    app._snapshot = _make_snapshot(tmp_path)
    app._selected_stage_index = 0
    app._selected_stage_key = "design"

    stage_list = _FakeListView()
    _bind_query(app, {"#stage-list": stage_list})
    monkeypatch.setattr(app_module, "ListItem", _FakeListItem)
    monkeypatch.setattr(app_module, "Label", _FakeLabel)

    app._populate_stage_list()
    assert app._selected_stage_index == 0
    assert stage_list.index == 0


def test_runner_done_auto_disarms_after_mutating_command(
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
    monkeypatch.setattr(app, "_update_safety_row", lambda: None)
    monkeypatch.setattr(app, "_update_action_button_state", lambda: None)
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


def test_refresh_snapshot_repeated_clicks_no_crash(tmp_path: Path) -> None:
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


def test_run_action_viewer_modal_opens_and_closes(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            assert await _click_when_visible(pilot, "#close") is True

    asyncio.run(_run())


def test_arm_modal_opens_and_cancel_keeps_disarmed(tmp_path: Path) -> None:
    async def _run() -> None:
        repo_root = tmp_path / "repo"
        state_path = _write_state_file(repo_root)
        app = AutolabCockpitApp(state_path=state_path)
        async with app.run_test(size=(220, 70)) as pilot:
            await pilot.pause()
            await pilot.press("a")
            assert await _click_when_visible(pilot, "#cancel") is True
            await pilot.pause()
            safety_status = app.query_one("#safety-status", app_module.Static)
            assert "Disarmed (read-only mode)." in str(safety_status.render())

    asyncio.run(_run())
