from __future__ import annotations

import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = REPO_ROOT / "src"
if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))


_ORIGINAL_SUBPROCESS_RUN = subprocess.run
_ORIGINAL_SUBPROCESS_POPEN = subprocess.Popen
_ALLOW_REAL_ORACLE_ENV_VAR = "AUTOLAB_ALLOW_REAL_ORACLE_IN_TESTS"


def _real_oracle_allowed_in_tests() -> bool:
    value = str(os.environ.get(_ALLOW_REAL_ORACLE_ENV_VAR, "")).strip().lower()
    return value in {"1", "true", "yes", "on"}


def _oracle_command_token(command: Any, *, executable: Any = None) -> str:
    if executable not in (None, ""):
        return str(executable)
    if isinstance(command, (list, tuple)):
        if not command:
            return ""
        return str(command[0])
    if isinstance(command, str):
        stripped = command.strip()
        if not stripped:
            return ""
        try:
            parts = shlex.split(stripped)
        except ValueError:
            parts = stripped.split()
        return str(parts[0]) if parts else ""
    return ""


def _oracle_subprocess_requested(command: Any, *, executable: Any = None) -> bool:
    token = _oracle_command_token(command, executable=executable)
    return bool(token) and Path(token).name == "oracle"


def _raise_blocked_oracle_subprocess(command: Any) -> None:
    raise AssertionError(
        "pytest blocked an unmocked Oracle CLI execution. "
        "Mock _run_oracle_browser_cli/subprocess.run in the test, "
        f"or set {_ALLOW_REAL_ORACLE_ENV_VAR}=1 for intentional live Oracle debugging. "
        f"blocked command: {command!r}"
    )


def _guarded_subprocess_run(*popenargs: Any, **kwargs: Any):
    command = kwargs.get("args")
    if command is None and popenargs:
        command = popenargs[0]
    if not _real_oracle_allowed_in_tests() and _oracle_subprocess_requested(
        command, executable=kwargs.get("executable")
    ):
        _raise_blocked_oracle_subprocess(command)
    return _ORIGINAL_SUBPROCESS_RUN(*popenargs, **kwargs)


def _guarded_subprocess_popen(*popenargs: Any, **kwargs: Any):
    command = kwargs.get("args")
    if command is None and popenargs:
        command = popenargs[0]
    if not _real_oracle_allowed_in_tests() and _oracle_subprocess_requested(
        command, executable=kwargs.get("executable")
    ):
        _raise_blocked_oracle_subprocess(command)
    return _ORIGINAL_SUBPROCESS_POPEN(*popenargs, **kwargs)


def pytest_configure() -> None:
    subprocess.run = _guarded_subprocess_run
    subprocess.Popen = _guarded_subprocess_popen


def pytest_unconfigure() -> None:
    subprocess.run = _ORIGINAL_SUBPROCESS_RUN
    subprocess.Popen = _ORIGINAL_SUBPROCESS_POPEN
