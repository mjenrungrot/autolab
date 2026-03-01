from __future__ import annotations

import sys
import time
from pathlib import Path
from threading import Event

import pytest

from autolab.tui.models import CommandIntent
from autolab.tui.runner import CommandRunner


def _sleep_intent(tmp_path: Path, seconds: float = 30.0) -> CommandIntent:
    argv = (
        sys.executable,
        "-c",
        (
            "import sys,time;"
            "print('runner-start');"
            "sys.stdout.flush();"
            f"time.sleep({seconds})"
        ),
    )
    return CommandIntent(
        action_id="run_loop",
        argv=argv,
        cwd=tmp_path,
        expected_writes=(),
        mutating=True,
    )


def _quick_intent(tmp_path: Path) -> CommandIntent:
    argv = (
        sys.executable,
        "-c",
        ("import sys;print('line-1');print('line-2');sys.stdout.flush()"),
    )
    return CommandIntent(
        action_id="verify_current_stage",
        argv=argv,
        cwd=tmp_path,
        expected_writes=(),
        mutating=True,
    )


def test_runner_rejects_second_start_while_running(tmp_path: Path) -> None:
    done_event = Event()
    done_calls: list[tuple[int, bool]] = []
    runner = CommandRunner(
        on_line=lambda _line: None,
        on_done=lambda rc, stopped: (
            done_calls.append((rc, stopped)),
            done_event.set(),
        ),
    )
    runner.start(_sleep_intent(tmp_path))
    with pytest.raises(RuntimeError):
        runner.start(_sleep_intent(tmp_path))
    assert runner.stop() is True
    assert done_event.wait(timeout=10.0) is True
    assert len(done_calls) == 1


def test_runner_stop_returns_false_when_idle() -> None:
    runner = CommandRunner(
        on_line=lambda _line: None, on_done=lambda _rc, _stopped: None
    )
    assert runner.stop() is False


def test_runner_stop_is_idempotent_and_callback_once(tmp_path: Path) -> None:
    done_event = Event()
    done_calls: list[tuple[int, bool]] = []
    runner = CommandRunner(
        on_line=lambda _line: None,
        on_done=lambda rc, stopped: (
            done_calls.append((rc, stopped)),
            done_event.set(),
        ),
    )
    runner.start(_sleep_intent(tmp_path))
    time.sleep(0.1)
    assert runner.stop(grace_seconds=0.2) is True
    assert runner.stop(grace_seconds=0.2) is True
    assert done_event.wait(timeout=10.0) is True
    assert len(done_calls) == 1
    assert done_calls[0][1] is True


def test_runner_natural_completion_reports_not_stopped_once(tmp_path: Path) -> None:
    done_event = Event()
    done_calls: list[tuple[int, bool]] = []
    lines: list[str] = []
    runner = CommandRunner(
        on_line=lambda line: lines.append(line),
        on_done=lambda rc, stopped: (
            done_calls.append((rc, stopped)),
            done_event.set(),
        ),
    )

    runner.start(_quick_intent(tmp_path))

    assert done_event.wait(timeout=10.0) is True
    assert done_calls == [(0, False)]
    assert lines == ["line-1", "line-2"]
    assert runner.is_running is False
    assert runner.intent is None


def test_runner_stop_near_completion_emits_done_once(tmp_path: Path) -> None:
    done_event = Event()
    done_calls: list[tuple[int, bool]] = []
    runner = CommandRunner(
        on_line=lambda _line: None,
        on_done=lambda rc, stopped: (
            done_calls.append((rc, stopped)),
            done_event.set(),
        ),
    )
    runner.start(_sleep_intent(tmp_path, seconds=0.2))
    time.sleep(0.15)
    runner.stop(grace_seconds=0.2)
    assert done_event.wait(timeout=10.0) is True
    assert len(done_calls) == 1
