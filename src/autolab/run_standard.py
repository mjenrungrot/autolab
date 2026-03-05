"""Compatibility shim for standard orchestration runtime.

The implementation now lives under ``autolab.orchestration.standard_runtime``.
"""

from __future__ import annotations

from functools import wraps

from autolab.orchestration import standard_runtime as _runtime

_ENTRYPOINTS = {
    "_handle_stage_failure",
    "_prepare_launch_run_context",
    "_run_once_standard",
}


def _sync_runtime_overrides() -> None:
    for name, value in globals().items():
        if name.startswith("__"):
            continue
        if name in {
            "_ENTRYPOINTS",
            "_runtime",
            "_sync_runtime_overrides",
            "_wrap_entrypoint",
        }:
            continue
        if name in _ENTRYPOINTS:
            continue
        if hasattr(_runtime, name):
            setattr(_runtime, name, value)


def _wrap_entrypoint(name: str):
    runtime_func = getattr(_runtime, name)

    @wraps(runtime_func)
    def _wrapped(*args, **kwargs):
        _sync_runtime_overrides()
        return getattr(_runtime, name)(*args, **kwargs)

    return _wrapped


for _name in dir(_runtime):
    if _name.startswith("__"):
        continue
    _value = getattr(_runtime, _name)
    if callable(_value) and _name in _ENTRYPOINTS:
        globals()[_name] = _wrap_entrypoint(_name)
    else:
        globals()[_name] = _value

__all__ = [name for name in globals() if not name.startswith("__")]
