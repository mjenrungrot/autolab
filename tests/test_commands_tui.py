from __future__ import annotations

import argparse
import json
import sys
import types
from pathlib import Path

import autolab.commands as commands_module


class _FakeTTY:
    def __init__(self, *, interactive: bool) -> None:
        self._interactive = interactive

    def isatty(self) -> bool:
        return self._interactive


def _write_state(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _args(state_path: Path, *, tail_lines: int = 2000) -> argparse.Namespace:
    return argparse.Namespace(state_file=str(state_path), tail_lines=tail_lines)


def _set_tty(monkeypatch, *, interactive: bool) -> None:
    monkeypatch.setattr(commands_module.sys, "stdin", _FakeTTY(interactive=interactive))
    monkeypatch.setattr(
        commands_module.sys, "stdout", _FakeTTY(interactive=interactive)
    )


def test_cmd_tui_rejects_non_positive_tail_lines(tmp_path: Path) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    _write_state(state_path)
    exit_code = commands_module._cmd_tui(_args(state_path, tail_lines=0))
    assert exit_code == 2


def test_cmd_tui_rejects_non_integer_tail_lines(tmp_path: Path) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    _write_state(state_path)
    args = argparse.Namespace(state_file=str(state_path), tail_lines="abc")
    exit_code = commands_module._cmd_tui(args)
    assert exit_code == 2


def test_cmd_tui_requires_existing_state_file(tmp_path: Path) -> None:
    missing_path = tmp_path / ".autolab" / "missing.json"
    exit_code = commands_module._cmd_tui(_args(missing_path))
    assert exit_code == 1


def test_cmd_tui_rejects_state_path_directory(tmp_path: Path) -> None:
    state_dir = tmp_path / ".autolab" / "state.json"
    state_dir.mkdir(parents=True, exist_ok=True)
    exit_code = commands_module._cmd_tui(_args(state_dir))
    assert exit_code == 1


def test_cmd_tui_rejects_unreadable_state_file(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    _write_state(state_path)
    monkeypatch.setattr(commands_module.os, "access", lambda _path, _mode: False)
    exit_code = commands_module._cmd_tui(_args(state_path))
    assert exit_code == 1


def test_cmd_tui_rejects_non_interactive_tty(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    _write_state(state_path)
    _set_tty(monkeypatch, interactive=False)
    exit_code = commands_module._cmd_tui(_args(state_path))
    assert exit_code == 1


def test_cmd_tui_handles_import_failure(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    _write_state(state_path)
    _set_tty(monkeypatch, interactive=True)
    fake_module = types.ModuleType("autolab.tui.app")
    monkeypatch.setitem(sys.modules, "autolab.tui.app", fake_module)
    exit_code = commands_module._cmd_tui(_args(state_path))
    assert exit_code == 1


def test_cmd_tui_handles_runtime_failure(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    _write_state(state_path)
    _set_tty(monkeypatch, interactive=True)

    class _FakeApp:
        def __init__(self, *, state_path: Path, tail_lines: int) -> None:
            self.state_path = state_path
            self.tail_lines = tail_lines

        def run(self) -> None:
            raise RuntimeError("boom")

    fake_module = types.ModuleType("autolab.tui.app")
    fake_module.AutolabCockpitApp = _FakeApp
    monkeypatch.setitem(sys.modules, "autolab.tui.app", fake_module)
    exit_code = commands_module._cmd_tui(_args(state_path, tail_lines=123))
    assert exit_code == 1


def test_cmd_tui_handles_keyboard_interrupt(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    _write_state(state_path)
    _set_tty(monkeypatch, interactive=True)

    class _FakeApp:
        def __init__(self, *, state_path: Path, tail_lines: int) -> None:
            self.state_path = state_path
            self.tail_lines = tail_lines

        def run(self) -> None:
            raise KeyboardInterrupt

    fake_module = types.ModuleType("autolab.tui.app")
    fake_module.AutolabCockpitApp = _FakeApp
    monkeypatch.setitem(sys.modules, "autolab.tui.app", fake_module)
    exit_code = commands_module._cmd_tui(_args(state_path, tail_lines=123))
    assert exit_code == 130


def test_cmd_tui_success_path(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    _write_state(state_path)
    _set_tty(monkeypatch, interactive=True)
    captured: dict[str, object] = {}

    class _FakeApp:
        def __init__(self, *, state_path: Path, tail_lines: int) -> None:
            captured["state_path"] = state_path
            captured["tail_lines"] = tail_lines

        def run(self) -> None:
            captured["ran"] = True

    fake_module = types.ModuleType("autolab.tui.app")
    fake_module.AutolabCockpitApp = _FakeApp
    monkeypatch.setitem(sys.modules, "autolab.tui.app", fake_module)
    exit_code = commands_module._cmd_tui(_args(state_path, tail_lines=3000))
    assert exit_code == 0
    assert captured["ran"] is True
    assert captured["state_path"] == state_path.resolve()
    assert captured["tail_lines"] == 3000
