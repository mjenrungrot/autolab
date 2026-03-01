"""Textual cockpit for the autolab CLI."""

from __future__ import annotations

__all__ = ("AutolabCockpitApp",)


def __getattr__(name: str):
    if name != "AutolabCockpitApp":
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from autolab.tui.app import AutolabCockpitApp

    return AutolabCockpitApp
