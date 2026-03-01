from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

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


class _FakeButton:
    def __init__(self) -> None:
        self.disabled = False
        self.label = ""
        self.variant = ""


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
    monkeypatch.setattr(app, "_clear_snapshot_views", lambda: None)
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


def test_refresh_button_logs_success_only_when_refresh_succeeds(
    tmp_path: Path, monkeypatch
) -> None:
    app = AutolabCockpitApp(state_path=tmp_path / "repo" / ".autolab" / "state.json")
    logs: list[str] = []
    monkeypatch.setattr(app, "_append_console", lambda text: logs.append(text))
    monkeypatch.setattr(app, "_refresh_snapshot", lambda: False)
    asyncio.run(
        app.on_button_pressed(
            SimpleNamespace(button=SimpleNamespace(id="refresh-snapshot"))
        )
    )
    assert "snapshot refreshed" not in logs

    monkeypatch.setattr(app, "_refresh_snapshot", lambda: True)
    asyncio.run(
        app.on_button_pressed(
            SimpleNamespace(button=SimpleNamespace(id="refresh-snapshot"))
        )
    )
    assert logs == ["snapshot refreshed"]


def test_action_button_disabled_without_snapshot(tmp_path: Path) -> None:
    app = AutolabCockpitApp(state_path=tmp_path / "repo" / ".autolab" / "state.json")
    run_button = _FakeButton()
    stop_button = _FakeButton()
    banner = _FakeStatic()
    _bind_query(
        app,
        {
            "#run-action": run_button,
            "#stop-loop": stop_button,
            "#running-banner": banner,
        },
    )
    app._snapshot = None
    app._armed = True
    app._update_action_button_state()
    assert run_button.disabled is True
    assert stop_button.disabled is True
    assert banner.value == ""


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
