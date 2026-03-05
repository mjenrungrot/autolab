"""Lightweight command registration models for CLI composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import argparse


CommandBuilder = Callable[[argparse._SubParsersAction[argparse.ArgumentParser]], None]


@dataclass(frozen=True)
class CommandRegistrar:
    """Registers a logical command group into the root subparser set."""

    group: str
    register: CommandBuilder
