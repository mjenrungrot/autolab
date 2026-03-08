"""Compatibility shim that re-exports split CLI modules.

Historically, all CLI logic lived in this module. The implementation now lives in
``autolab.cli`` submodules to keep command, parser, and helper code maintainable.
"""

from __future__ import annotations

from functools import wraps

from autolab.cli import handlers_admin as _handlers_admin
from autolab.cli import handlers_backlog as _handlers_backlog
from autolab.cli import handlers_campaign as _handlers_campaign
from autolab.cli import handlers_observe as _handlers_observe
from autolab.cli import handlers_project as _handlers_project
from autolab.cli import handlers_run as _handlers_run
from autolab.cli import parser as _parser
from autolab.cli import handlers_parser as _handlers_parser
from autolab.cli import support as _support

_TARGET_MODULES = (
    _support,
    _handlers_observe,
    _handlers_backlog,
    _handlers_campaign,
    _handlers_project,
    _handlers_run,
    _handlers_parser,
    _handlers_admin,
    _parser,
)

for _module in _TARGET_MODULES:
    for _name in dir(_module):
        if _name.startswith("__"):
            continue
        globals()[_name] = getattr(_module, _name)


def _sync_runtime_overrides() -> None:
    for name, value in globals().items():
        if name.startswith("__"):
            continue
        if name in {"_TARGET_MODULES", "_sync_runtime_overrides", "_wrap_with_sync"}:
            continue
        for target in _TARGET_MODULES:
            if name == "main" and target is _parser:
                continue
            if hasattr(target, name):
                setattr(target, name, value)


def _wrap_with_sync(func):
    @wraps(func)
    def _wrapped(*args, **kwargs):
        _sync_runtime_overrides()
        return func(*args, **kwargs)

    return _wrapped


for _name, _value in list(globals().items()):
    if not callable(_value):
        continue
    if _name.startswith("_cmd_") or _name in {"_build_parser", "_run_once"}:
        globals()[_name] = _wrap_with_sync(_value)


def main(argv: list[str] | None = None) -> int:
    _sync_runtime_overrides()
    return int(_parser.main(argv))


__all__ = [name for name in globals() if not name.startswith("__")]
