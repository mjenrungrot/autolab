from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable

from autolab.tui.models import CommandIntent

LineCallback = Callable[[str], None]
DoneCallback = Callable[[int, bool], None]


class CommandRunner:
    def __init__(self, *, on_line: LineCallback, on_done: DoneCallback) -> None:
        self._on_line = on_line
        self._on_done = on_done
        self._process: subprocess.Popen[str] | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._stop_requested = False
        self._completion_reported = False
        self._intent: CommandIntent | None = None

    @property
    def is_running(self) -> bool:
        with self._lock:
            process = self._process
        return bool(process and process.poll() is None)

    @property
    def intent(self) -> CommandIntent | None:
        with self._lock:
            return self._intent

    def start(self, intent: CommandIntent) -> None:
        with self._lock:
            if self._process and self._process.poll() is None:
                raise RuntimeError("a command is already running")
            popen_kwargs: dict[str, object] = {}
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            process = subprocess.Popen(
                list(intent.argv),
                cwd=intent.cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                **popen_kwargs,
            )
            self._process = process
            self._stop_requested = False
            self._completion_reported = False
            self._intent = intent
            self._thread = threading.Thread(target=self._stream_output, daemon=True)
            self._thread.start()

    def _finish(self, return_code: int) -> None:
        with self._lock:
            if self._completion_reported:
                return
            self._completion_reported = True
            stopped = self._stop_requested
            self._process = None
            self._thread = None
            self._intent = None
        self._on_done(return_code, stopped)

    def _signal_interrupt(self, process: subprocess.Popen[str]) -> None:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGINT)
                return
            except Exception:
                pass
        process.send_signal(signal.SIGINT)

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                return
            except Exception:
                pass
        process.terminate()

    def _kill(self, process: subprocess.Popen[str]) -> None:
        if os.name == "posix":
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                return
            except Exception:
                pass
        process.kill()

    def _wait_for_exit(
        self, process: subprocess.Popen[str], *, grace_seconds: float
    ) -> bool:
        deadline = time.monotonic() + max(0.1, grace_seconds)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return True
            time.sleep(0.05)
        return process.poll() is not None

    def _stream_output(self) -> None:
        process: subprocess.Popen[str] | None
        with self._lock:
            process = self._process
        if process is None:
            return
        try:
            if process.stdout is not None:
                for raw_line in process.stdout:
                    try:
                        self._on_line(raw_line.rstrip("\n"))
                    except Exception:
                        pass
            return_code = process.wait()
        except Exception:
            return_code = process.poll()
            if return_code is None:
                return_code = 1
        self._finish(int(return_code))

    def stop(self, *, grace_seconds: float = 3.0) -> bool:
        with self._lock:
            if self._stop_requested:
                return True
            process = self._process
            if process is None or process.poll() is not None:
                return False
            self._stop_requested = True

        try:
            self._signal_interrupt(process)
        except Exception:
            try:
                self._terminate(process)
            except Exception:
                pass
        if self._wait_for_exit(process, grace_seconds=grace_seconds):
            return True
        try:
            self._terminate(process)
        except Exception:
            pass
        if self._wait_for_exit(process, grace_seconds=grace_seconds):
            return True
        try:
            self._kill(process)
        except Exception:
            pass
        return True
