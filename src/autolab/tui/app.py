from __future__ import annotations

import json
import re
import shlex
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from textual import events
from textual.app import App, ComposeResult, SystemCommand
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    Markdown,
    RichLog,
    Static,
)

from autolab.constants import BACKLOG_COMPLETED_STATUSES, EXPERIMENT_TYPES
from autolab.tui.actions import (
    build_experiment_create_intent,
    build_experiment_move_intent,
    build_focus_intent,
    build_human_review_intent,
    build_lock_break_intent,
    build_loop_intent,
    build_open_in_editor_intent,
    build_run_intent,
    build_todo_sync_intent,
    build_verify_intent,
    list_actions,
)
from autolab.tui.models import (
    ActionSpec,
    ArtifactItem,
    BacklogExperimentItem,
    BacklogHypothesisItem,
    CockpitSnapshot,
    CommandHistoryItem,
    CommandIntent,
    RunItem,
    LoopActionOptions,
    RunItem,
    RunActionOptions,
    ViewMode,
)
from autolab.tui.preview_render import PreviewRenderHint, build_preview_markdown
from autolab.tui.runner import CommandRunner
from autolab.tui.snapshot import (
    load_artifact_text,
    load_cockpit_snapshot,
    resolve_stage_prompt_path,
)


@dataclass
class CommandHistoryItem:
    intent: CommandIntent
    command: str
    started_at: float
    started_at_text: str
    finished_at: float | None = None
    exit_code: int | None = None
    stopped: bool = False


_COMMAND_HISTORY_MAX = 25
_AUTO_REFRESH_INTERVAL_SECONDS = 5.0
_ARTIFACT_PREVIEW_MAX_CHARS = 12_000


def _query_matches(raw_text: str, query: str) -> bool:
    haystack = str(raw_text).strip().lower()
    if not haystack:
        haystack = ""
    normalized_query = str(query).strip().lower()
    if not normalized_query:
        return True
    return all(term in haystack for term in normalized_query.split())


class UnlockSafetyScreen(ModalScreen[bool]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    UnlockSafetyScreen {
      align: center middle;
    }

    #unlock-dialog {
      width: 100%;
      height: 100%;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #unlock-title {
      text-style: bold;
      margin-bottom: 1;
    }

    #unlock-buttons {
      margin-top: 1;
      align-horizontal: right;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="unlock-dialog"):
            yield Label("Unlock mutating actions?", id="unlock-title")
            yield Static(
                "Mutating actions can change workflow state and files.\n"
                "Each mutating command still requires confirmation.",
                markup=False,
            )
            with Horizontal(id="unlock-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Unlock", id="unlock", variant="warning")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "unlock")


class CommandHistoryScreen(ModalScreen[CommandHistoryItem | None]):
    BINDINGS = [
        ("escape", "cancel", "Close"),
        ("enter", "replay", "Replay"),
        ("r", "replay", "Replay"),
    ]

    CSS = """
    CommandHistoryScreen {
      align: center middle;
    }

    #command-history-dialog {
      width: 100%;
      height: 100%;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #command-history-title {
      text-style: bold;
      margin-bottom: 1;
    }

    #command-history-list {
      border: round $surface;
      height: 1fr;
      min-height: 8;
    }

    #command-history-filter-row {
      height: auto;
      margin-bottom: 1;
    }

    #command-history-filter {
      width: 1fr;
    }

    #command-history-detail {
      margin-top: 1;
      color: $text-muted;
    }

    #command-history-buttons {
      margin-top: 1;
      align-horizontal: right;
    }
    """

    def __init__(self, *, history: tuple[CommandHistoryItem, ...]) -> None:
        super().__init__()
        self._history = history
        self._filtered_history = history
        self._last_query = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="command-history-dialog"):
            yield Label("Command History", id="command-history-title")
            with Horizontal(id="command-history-filter-row"):
                yield Input(
                    value="",
                    placeholder="Filter by command or label...",
                    id="command-history-filter",
                )
                yield Button("Clear", id="command-history-filter-clear")
            yield ListView(id="command-history-list")
            yield Static("", id="command-history-detail", markup=False)
            with Horizontal(id="command-history-buttons"):
                yield Button("Close", id="command-history-close")
                yield Button(
                    "Replay selected", id="command-history-replay", variant="warning"
                )

    def on_mount(self) -> None:
        if not self._history:
            self.dismiss(None)
            return
        list_view = self.query_one("#command-history-list", ListView)
        list_view.clear()
        self._refresh_history_matches("")
        list_view.index = 0
        list_view.focus()
        self._update_detail()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_replay(self) -> None:
        self._replay_selected()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "command-history-close":
            self.dismiss(None)
            return
        if event.button.id == "command-history-replay":
            self._replay_selected()
            return
        if event.button.id == "command-history-filter-clear":
            self.query_one("#command-history-filter", Input).value = ""
            self._refresh_history_matches("")
            return

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id != "command-history-list":
            return
        self._update_detail()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "command-history-list":
            return
        self._replay_selected()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "command-history-filter":
            return
        self._refresh_history_matches(event.value)

    def _refresh_history_matches(self, query: str) -> None:
        normalized_query = str(query).strip().lower()
        if self._last_query == normalized_query:
            return
        self._last_query = normalized_query
        if normalized_query:
            self._filtered_history = tuple(
                item
                for item in self._history
                if _query_matches(
                    f"{item.command} {item.intent.action_id} {item.command}",
                    normalized_query,
                )
            )
        else:
            self._filtered_history = self._history

        list_view = self.query_one("#command-history-list", ListView)
        list_view.clear()
        if not self._filtered_history:
            list_view.append(
                ListItem(
                    Label(f"(No command history matches {query!r})", classes="tone-muted")
                )
            )
            self._update_detail()
            return
        for item in self._filtered_history:
            list_view.append(ListItem(Label(self._format_history_line(item))))
        list_view.index = 0
        self._update_detail()

    def _replay_selected(self) -> None:
        selected = self._selected_item()
        if selected is None:
            return
        self.dismiss(selected)

    @staticmethod
    def _format_status(item: CommandHistoryItem) -> str:
        if item.exit_code is None and item.finished_at is None:
            return "running"
        if item.exit_code is None:
            return "pending"
        if item.exit_code == 0:
            return "ok"
        if item.stopped:
            return "stopped"
        return "fail"

    @staticmethod
    def _format_duration(item: CommandHistoryItem) -> str:
        if item.finished_at is None:
            return "-"
        return f"{item.finished_at - item.started_at:.1f}s"

    def _format_history_line(self, item: CommandHistoryItem) -> str:
        status = self._format_status(item)
        duration = self._format_duration(item)
        return f"[{item.started_at_text}] {status:7} {duration:>7} | {item.command}"

    def _selected_item(self) -> CommandHistoryItem | None:
        list_view = self.query_one("#command-history-list", ListView)
        if not self._filtered_history:
            return None
        index = list_view.index
        if index is None or index < 0 or index >= len(self._filtered_history):
            return None
        return self._filtered_history[index]

    def _update_detail(self) -> None:
        item = self._selected_item()
        detail = self.query_one("#command-history-detail", Static)
        if item is None:
            detail.update("")
            return
        detail.update(
            f"{item.command}\n"
            f"Started: {item.started_at_text}\n"
            f"Exit: {item.exit_code if item.exit_code is not None else '-'}\n"
            f"Stopped: {'yes' if item.stopped else 'no'}"
        )


class ActionConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("enter", "confirm", "Confirm"),
        ("d", "toggle_details", "Toggle Details"),
    ]

    CSS = """
    ActionConfirmScreen {
      align: center middle;
    }

    #action-confirm-dialog {
      width: 100%;
      height: 100%;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #action-confirm-title {
      text-style: bold;
      margin-bottom: 1;
    }

    #action-confirm-summary {
      margin-bottom: 1;
    }

    #action-confirm-hint {
      margin-bottom: 1;
      color: $text-muted;
    }

    #action-confirm-details {
      height: auto;
      border: round $surface;
      padding: 0 1;
    }

    #action-confirm-buttons {
      margin-top: 1;
      align-horizontal: right;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        summary: str,
        command: str,
        cwd: Path,
        expected_writes: tuple[str, ...],
        confirm_label: str = "Confirm",
    ) -> None:
        super().__init__()
        self._title = title
        self._summary = summary
        self._command = command
        self._cwd = cwd
        self._expected_writes = expected_writes
        self._confirm_label = confirm_label
        self._show_details = False

    def compose(self) -> ComposeResult:
        with Vertical(id="action-confirm-dialog"):
            yield Label(self._title, id="action-confirm-title")
            yield Static(self._summary, id="action-confirm-summary", markup=False)
            yield Static(
                "Keys: Enter confirm | d toggle details | Esc cancel",
                id="action-confirm-hint",
                markup=False,
            )
            yield Static("Details hidden.", id="action-confirm-details", markup=False)
            with Horizontal(id="action-confirm-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Show Details", id="toggle-details")
                yield Button(self._confirm_label, id="confirm", variant="error")

    def on_mount(self) -> None:
        self._render_details()
        self.query_one("#cancel", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_toggle_details(self) -> None:
        self._show_details = not self._show_details
        self._render_details()

    def _render_details(self) -> None:
        details = self.query_one("#action-confirm-details", Static)
        toggle = self.query_one("#toggle-details", Button)
        if not self._show_details:
            details.update("Details hidden.")
            toggle.label = "Show Details"
            return
        writes = "\n".join(f"- {entry}" for entry in self._expected_writes)
        if not writes:
            writes = "- (none)"
        details.update(
            "Command:\n"
            f"{self._command}\n\n"
            "cwd:\n"
            f"{self._cwd}\n\n"
            "Expected writes (best-effort):\n"
            f"{writes}"
        )
        toggle.label = "Hide Details"

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "toggle-details":
            self.action_toggle_details()
            return
        if event.button.id == "confirm":
            self.action_confirm()
            return
        self.dismiss(False)

    def on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.stop()
            self.action_confirm()
            return
        if event.key == "d":
            event.stop()
            self.action_toggle_details()


class RunPresetScreen(ModalScreen[RunActionOptions | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    RunPresetScreen {
      align: center middle;
    }

    #run-preset-dialog {
      width: 100%;
      height: 100%;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #run-preset-list {
      height: 7;
      border: round $surface;
      margin-bottom: 1;
    }

    #run-preset-advanced {
      border: round $surface;
      padding: 0 1;
      margin-top: 1;
    }

    #run-preset-buttons {
      margin-top: 1;
      align-horizontal: right;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="run-preset-dialog"):
            yield Label("Run one transition", id="run-preset-title")
            yield Static(
                "Start here: choose a preset first. Advanced flags are optional.",
                markup=False,
            )
            yield ListView(
                ListItem(Label("Quick safe run (recommended)")),
                ListItem(Label("Run with verify")),
                ListItem(Label("Advanced options")),
                id="run-preset-list",
            )
            yield Checkbox("Use advanced options", value=False, id="run-advanced")
            with Vertical(id="run-preset-advanced"):
                yield Checkbox("Enable verification", value=True, id="run-verify")
                yield Checkbox("Enable auto decision", value=False, id="run-auto")
                yield Checkbox("Force --run-agent", value=False, id="run-agent-on")
                yield Checkbox("Force --no-run-agent", value=False, id="run-agent-off")
            with Horizontal(id="run-preset-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="continue", variant="primary")

    def on_mount(self) -> None:
        preset_list = self.query_one("#run-preset-list", ListView)
        preset_list.index = 0
        preset_list.focus()
        self._update_advanced_enabled(False)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _selected_preset_index(self) -> int:
        return self.query_one("#run-preset-list", ListView).index or 0

    def _sync_advanced_from_preset(self) -> None:
        if self._selected_preset_index() != 2:
            return
        advanced = self.query_one("#run-advanced", Checkbox)
        if not advanced.value:
            advanced.value = True
        self._update_advanced_enabled(True)

    def _update_advanced_enabled(self, enabled: bool) -> None:
        for widget_id in (
            "#run-verify",
            "#run-auto",
            "#run-agent-on",
            "#run-agent-off",
        ):
            self.query_one(widget_id, Checkbox).disabled = not enabled

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "run-advanced":
            self._update_advanced_enabled(event.checkbox.value)
            return
        if event.checkbox.id == "run-agent-on" and event.checkbox.value:
            self.query_one("#run-agent-off", Checkbox).value = False
            return
        if event.checkbox.id == "run-agent-off" and event.checkbox.value:
            self.query_one("#run-agent-on", Checkbox).value = False

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "run-preset-list":
            self._sync_advanced_from_preset()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "run-preset-list":
            return
        self._sync_advanced_from_preset()
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
            return
        if event.button.id != "continue":
            return
        self._submit()

    def _submit(self) -> None:
        preset_index = self._selected_preset_index()
        use_advanced = self.query_one("#run-advanced", Checkbox).value
        if preset_index == 2 and not use_advanced:
            self.query_one("#run-advanced", Checkbox).value = True
            self._update_advanced_enabled(True)
            use_advanced = True

        if preset_index == 0:
            base = RunActionOptions(
                verify=False, auto_decision=False, run_agent_mode="policy"
            )
        elif preset_index == 1:
            base = RunActionOptions(
                verify=True, auto_decision=False, run_agent_mode="policy"
            )
        else:
            base = RunActionOptions(
                verify=True, auto_decision=False, run_agent_mode="policy"
            )

        if not use_advanced:
            self.dismiss(base)
            return

        force_on = self.query_one("#run-agent-on", Checkbox).value
        force_off = self.query_one("#run-agent-off", Checkbox).value
        if force_on and force_off:
            force_off = False
        run_agent_mode = "policy"
        if force_on:
            run_agent_mode = "force_on"
        elif force_off:
            run_agent_mode = "force_off"

        self.dismiss(
            RunActionOptions(
                verify=self.query_one("#run-verify", Checkbox).value,
                auto_decision=self.query_one("#run-auto", Checkbox).value,
                run_agent_mode=run_agent_mode,
            )
        )


class LoopPresetScreen(ModalScreen[LoopActionOptions | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    LoopPresetScreen {
      align: center middle;
    }

    #loop-preset-dialog {
      width: 100%;
      height: 100%;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #loop-preset-list {
      height: 7;
      border: round $surface;
      margin-bottom: 1;
    }

    #loop-preset-advanced {
      border: round $surface;
      padding: 0 1;
      margin-top: 1;
    }

    #loop-preset-buttons {
      margin-top: 1;
      align-horizontal: right;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="loop-preset-dialog"):
            yield Label("Start loop", id="loop-preset-title")
            yield Static(
                "Start here: pick a loop style. Advanced options are optional.",
                markup=False,
            )
            yield ListView(
                ListItem(Label("Guided short loop (recommended)")),
                ListItem(Label("Unattended loop with verify")),
                ListItem(Label("Advanced options")),
                id="loop-preset-list",
            )
            yield Checkbox("Use advanced options", value=False, id="loop-advanced")
            with Vertical(id="loop-preset-advanced"):
                yield Input(
                    value="3", placeholder="max iterations", id="loop-max-iterations"
                )
                yield Input(value="2", placeholder="max hours", id="loop-max-hours")
                yield Checkbox("Enable --auto", value=True, id="loop-auto")
                yield Checkbox("Enable --verify", value=True, id="loop-verify")
                yield Checkbox("Force --run-agent", value=False, id="loop-agent-on")
                yield Checkbox("Force --no-run-agent", value=False, id="loop-agent-off")
            with Horizontal(id="loop-preset-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="continue", variant="primary")

    def on_mount(self) -> None:
        preset_list = self.query_one("#loop-preset-list", ListView)
        preset_list.index = 0
        preset_list.focus()
        self._update_advanced_enabled(False)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _selected_preset_index(self) -> int:
        return self.query_one("#loop-preset-list", ListView).index or 0

    def _sync_advanced_from_preset(self) -> None:
        if self._selected_preset_index() != 2:
            return
        advanced = self.query_one("#loop-advanced", Checkbox)
        if not advanced.value:
            advanced.value = True
        self._update_advanced_enabled(True)

    def _update_advanced_enabled(self, enabled: bool) -> None:
        for selector, widget_type in (
            ("#loop-max-iterations", Input),
            ("#loop-max-hours", Input),
            ("#loop-auto", Checkbox),
            ("#loop-verify", Checkbox),
            ("#loop-agent-on", Checkbox),
            ("#loop-agent-off", Checkbox),
        ):
            self.query_one(selector, widget_type).disabled = not enabled

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id == "loop-advanced":
            self._update_advanced_enabled(event.checkbox.value)
            return
        if event.checkbox.id == "loop-agent-on" and event.checkbox.value:
            self.query_one("#loop-agent-off", Checkbox).value = False
            return
        if event.checkbox.id == "loop-agent-off" and event.checkbox.value:
            self.query_one("#loop-agent-on", Checkbox).value = False

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "loop-preset-list":
            self._sync_advanced_from_preset()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id != "loop-preset-list":
            return
        self._sync_advanced_from_preset()
        self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
            return
        if event.button.id != "continue":
            return
        self._submit()

    def _submit(self) -> None:
        preset_index = self._selected_preset_index()
        use_advanced = self.query_one("#loop-advanced", Checkbox).value
        if preset_index == 2 and not use_advanced:
            self.query_one("#loop-advanced", Checkbox).value = True
            self._update_advanced_enabled(True)
            use_advanced = True

        if preset_index == 0:
            base = LoopActionOptions(
                max_iterations=2,
                max_hours=2.0,
                auto=False,
                verify=True,
                run_agent_mode="policy",
            )
        elif preset_index == 1:
            base = LoopActionOptions(
                max_iterations=3,
                max_hours=2.0,
                auto=True,
                verify=True,
                run_agent_mode="policy",
            )
        else:
            base = LoopActionOptions(
                max_iterations=3,
                max_hours=2.0,
                auto=True,
                verify=True,
                run_agent_mode="policy",
            )

        if not use_advanced:
            self.dismiss(base)
            return

        force_on = self.query_one("#loop-agent-on", Checkbox).value
        force_off = self.query_one("#loop-agent-off", Checkbox).value
        if force_on and force_off:
            force_off = False
        run_agent_mode = "policy"
        if force_on:
            run_agent_mode = "force_on"
        elif force_off:
            run_agent_mode = "force_off"

        try:
            max_iterations = int(self.query_one("#loop-max-iterations", Input).value)
            max_hours = float(self.query_one("#loop-max-hours", Input).value)
        except ValueError:
            self.notify("max iterations and max hours must be numeric.")
            return
        if max_iterations <= 0:
            self.notify("max iterations must be > 0.")
            return
        if max_hours <= 0:
            self.notify("max hours must be > 0.")
            return

        self.dismiss(
            LoopActionOptions(
                max_iterations=max_iterations,
                max_hours=max_hours,
                auto=self.query_one("#loop-auto", Checkbox).value,
                verify=self.query_one("#loop-verify", Checkbox).value,
                run_agent_mode=run_agent_mode,
            )
        )


class HumanReviewDecisionScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    HumanReviewDecisionScreen {
      align: center middle;
    }

    #human-review-dialog {
      width: 100%;
      height: 100%;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #human-review-list {
      height: 1fr;
      border: round $surface;
      margin-bottom: 1;
    }

    #human-review-buttons {
      margin-top: 1;
      align-horizontal: right;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self._statuses: tuple[str, ...] = ("pass", "retry", "stop")

    def compose(self) -> ComposeResult:
        with Vertical(id="human-review-dialog"):
            yield Label("Resolve Human Review", id="human-review-title")
            yield Static(
                "Choose a human review decision to apply to workflow state.",
                markup=False,
            )
            yield ListView(
                ListItem(Label("pass - advance to launch")),
                ListItem(Label("retry - return to implementation")),
                ListItem(Label("stop - end experiment")),
                id="human-review-list",
            )
            with Horizontal(id="human-review-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="continue", variant="primary")

    def on_mount(self) -> None:
        decision_list = self.query_one("#human-review-list", ListView)
        decision_list.index = 0
        decision_list.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "human-review-list":
            self._submit()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
            return
        if event.button.id != "continue":
            return
        self._submit()

    def _submit(self) -> None:
        decision_list = self.query_one("#human-review-list", ListView)
        selected_index = decision_list.index
        if selected_index is None or not (0 <= selected_index < len(self._statuses)):
            self.notify("Select a decision.")
            return
        self.dismiss(self._statuses[selected_index])


class RunJumpScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    RunJumpScreen {
      align: center middle;
    }

    #run-jump-dialog {
      width: 100%;
      height: 100%;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #run-jump-title {
      text-style: bold;
      margin-bottom: 1;
    }

    #run-jump-list {
      border: round $surface;
      height: 1fr;
      margin-bottom: 1;
    }

    #run-jump-buttons {
      align-horizontal: right;
    }
    """

    def __init__(self, *, runs: tuple[RunItem, ...]) -> None:
        super().__init__()
        self._all_runs = tuple(runs)
        self._filtered_runs: tuple[RunItem, ...] = ()

    def compose(self) -> ComposeResult:
        with Vertical(id="run-jump-dialog"):
            yield Label("Jump to run", id="run-jump-title")
            yield Static(
                "Type to find runs by id/status/host/job id, then press Enter.",
                id="run-jump-hint",
                markup=False,
            )
            yield Input(value="", placeholder="Find run...", id="run-jump-query")
            yield ListView(id="run-jump-list")
            with Horizontal(id="run-jump-buttons"):
                yield Button("Cancel", id="run-jump-cancel")
                yield Button("Continue", id="run-jump-continue", variant="primary")

    def on_mount(self) -> None:
        query_input = self.query_one("#run-jump-query", Input)
        query_input.focus()
        self._refresh_run_matches("")

    def _refresh_run_matches(self, query: str) -> None:
        query_lower = query.strip().lower()
        if query_lower:
            matches = [
                run
                for run in self._all_runs
                if _query_matches(
                    f"{run.run_id} {run.status} {run.host_mode} {run.job_id}",
                    query_lower,
                )
            ]
        else:
            matches = list(self._all_runs)
        self._filtered_runs = tuple(matches)
        run_list = self.query_one("#run-jump-list", ListView)
        run_list.clear()
        if not matches:
            empty_text = (
                "(No runs match this query)"
                if self._all_runs
                else "(No runs available)"
            )
            run_list.append(ListItem(Label(empty_text)))
            return
        for run in self._filtered_runs:
            run_list.append(
                ListItem(
                    Label(
                        f"{run.run_id} [{run.status}] ({run.host_mode})"
                        f" start={run.started_at or '-'}"
                    )
                )
            )
        run_list.index = 0

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "run-jump-query":
            return
        self._refresh_run_matches(event.value)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "run-jump-list":
            self._submit_selection()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "run-jump-cancel":
            self.action_cancel()
            return
        if event.button.id != "run-jump-continue":
            return
        self._submit_selection()

    def _submit_selection(self) -> None:
        if not self._filtered_runs:
            return
        run_list = self.query_one("#run-jump-list", ListView)
        selected_index = run_list.index or 0
        if selected_index is None or selected_index < 0:
            return
        if selected_index >= len(self._filtered_runs):
            selected_index = 0
        self.dismiss(self._filtered_runs[selected_index].run_id)


class ArtifactJumpScreen(ModalScreen[Path | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    ArtifactJumpScreen {
      align: center middle;
    }

    #artifact-jump-dialog {
      width: 100%;
      height: 100%;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #artifact-jump-title {
      text-style: bold;
      margin-bottom: 1;
    }

    #artifact-jump-list {
      border: round $surface;
      height: 1fr;
      margin-bottom: 1;
    }

    #artifact-jump-buttons {
      align-horizontal: right;
    }
    """

    def __init__(
        self,
        *,
        artifact_paths: tuple[tuple[Path, str], ...],
    ) -> None:
        super().__init__()
        self._artifact_paths = tuple(artifact_paths)
        self._filtered_artifacts: tuple[tuple[Path, str], ...] = ()

    def compose(self) -> ComposeResult:
        with Vertical(id="artifact-jump-dialog"):
            yield Label("Jump to file", id="artifact-jump-title")
            yield Static(
                "Type to find files by path, then press Enter.",
                id="artifact-jump-hint",
                markup=False,
            )
            yield Input(value="", placeholder="Find file...", id="artifact-jump-query")
            yield ListView(id="artifact-jump-list")
            with Horizontal(id="artifact-jump-buttons"):
                yield Button("Cancel", id="artifact-jump-cancel")
                yield Button("Continue", id="artifact-jump-continue", variant="primary")

    def on_mount(self) -> None:
        query_input = self.query_one("#artifact-jump-query", Input)
        query_input.focus()
        self._refresh_artifact_matches("")

    def _refresh_artifact_matches(self, query: str) -> None:
        query_lower = query.strip().lower()
        if query_lower:
            matches = [
                (path, label)
                for path, label in self._artifact_paths
                if _query_matches(f"{path} {label}", query_lower)
            ]
        else:
            matches = list(self._artifact_paths)
        self._filtered_artifacts = tuple(matches)
        artifact_list = self.query_one("#artifact-jump-list", ListView)
        artifact_list.clear()
        if not matches:
            empty_text = (
                "(No files match this query)"
                if self._artifact_paths
                else "(No files available)"
            )
            artifact_list.append(ListItem(Label(empty_text)))
            return
        for path, label in self._filtered_artifacts:
            artifact_list.append(ListItem(Label(f"{label} [{path.name}]")))
        artifact_list.index = 0

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "artifact-jump-query":
            return
        self._refresh_artifact_matches(event.value)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if event.list_view.id == "artifact-jump-list":
            self._submit_selection()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "artifact-jump-cancel":
            self.action_cancel()
            return
        if event.button.id != "artifact-jump-continue":
            return
        self._submit_selection()

    def _submit_selection(self) -> None:
        if not self._filtered_artifacts:
            return
        artifact_list = self.query_one("#artifact-jump-list", ListView)
        selected_index = artifact_list.index or 0
        if selected_index is None or selected_index < 0:
            return
        if selected_index >= len(self._filtered_artifacts):
            selected_index = 0
        path, _label = self._filtered_artifacts[selected_index]
        self.dismiss(path)


class SelectionInspectorScreen(ModalScreen[None]):
    BINDINGS = [("escape", "cancel", "Close")]

    CSS = """
    SelectionInspectorScreen {
      align: center middle;
    }

    #selection-inspector-dialog {
      width: 100%;
      height: 100%;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #selection-inspector-title {
      text-style: bold;
      margin-bottom: 1;
    }

    #selection-inspector-content {
      border: round $surface;
      height: 1fr;
      margin-bottom: 1;
      padding: 1 1;
    }

    #selection-inspector-buttons {
      align-horizontal: right;
    }
    """

    def __init__(self, *, title: str, lines: tuple[str, ...]) -> None:
        super().__init__()
        self._title = title
        self._lines = lines

    def compose(self) -> ComposeResult:
        with Vertical(id="selection-inspector-dialog"):
            yield Label(self._title, id="selection-inspector-title")
            yield Static("\n".join(self._lines), id="selection-inspector-content")
            with Horizontal(id="selection-inspector-buttons"):
                yield Button("Close", id="selection-inspector-close")

    def on_mount(self) -> None:
        self.query_one("#selection-inspector-close", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "selection-inspector-close":
            self.dismiss(None)


class ArtifactViewerScreen(ModalScreen[str | None]):
    BINDINGS = [("escape", "cancel", "Close")]

    CSS = """
    ArtifactViewerScreen {
      align: center middle;
    }

    #artifact-dialog {
      width: 100%;
      height: 100%;
      background: $panel;
    }

    #artifact-path {
      text-style: bold;
      margin-bottom: 1;
    }

    #artifact-scroll {
      height: 1fr;
      border: round $surface;
      padding: 0 1;
    }

    #artifact-buttons {
      height: auto;
      margin-top: 1;
      align-horizontal: right;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        artifact_markdown: str,
        editor_path: Path | None = None,
    ) -> None:
        super().__init__()
        self._title = title
        self._artifact_markdown = artifact_markdown
        self._editor_path = editor_path

    def compose(self) -> ComposeResult:
        with Vertical(id="artifact-dialog"):
            yield Label(self._title, id="artifact-path", markup=False)
            with VerticalScroll(id="artifact-scroll"):
                yield Markdown(
                    self._artifact_markdown,
                    id="artifact-content",
                    open_links=False,
                )
            with Horizontal(id="artifact-buttons"):
                yield Button("Close", id="close")
                if self._editor_path is not None:
                    yield Button("Open in $EDITOR", id="open-editor", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#close", Button).focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open-editor":
            self.dismiss("open_editor")
            return
        self.action_cancel()


def _is_completed_backlog_status(status: str) -> bool:
    return str(status).strip().lower() in BACKLOG_COMPLETED_STATUSES


def _default_experiment_index(
    experiments: tuple[BacklogExperimentItem, ...],
) -> int:
    if not experiments:
        return 0
    for index, item in enumerate(experiments):
        if item.is_current:
            return index
    for index, item in enumerate(experiments):
        if not _is_completed_backlog_status(item.status):
            return index
    return 0


def _next_suggested_ids(
    experiments: tuple[BacklogExperimentItem, ...],
) -> tuple[str, str]:
    max_suffix = 0
    used_experiment_ids: set[str] = set()
    used_iteration_ids: set[str] = set()
    for item in experiments:
        experiment_id = str(item.experiment_id).strip()
        iteration_id = str(item.iteration_id).strip()
        if experiment_id:
            used_experiment_ids.add(experiment_id)
        if iteration_id:
            used_iteration_ids.add(iteration_id)

        experiment_match = re.fullmatch(r"e(\d+)", experiment_id)
        if experiment_match:
            max_suffix = max(max_suffix, int(experiment_match.group(1)))
        iteration_match = re.fullmatch(r"iter(\d+)", iteration_id)
        if iteration_match:
            max_suffix = max(max_suffix, int(iteration_match.group(1)))

    candidate = max_suffix + 1
    while True:
        experiment_id = f"e{candidate}"
        iteration_id = f"iter{candidate}"
        if (
            experiment_id not in used_experiment_ids
            and iteration_id not in used_iteration_ids
        ):
            return (experiment_id, iteration_id)
        candidate += 1


def _default_move_target(source_type: str) -> str:
    normalized = str(source_type).strip().lower()
    if normalized == "plan":
        return "in_progress"
    if normalized == "in_progress":
        return "done"
    return "plan"


class FocusExperimentScreen(ModalScreen[tuple[str, str] | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    FocusExperimentScreen {
      align: center middle;
    }

    #focus-dialog {
      width: 100%;
      height: 100%;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #focus-experiment-list {
      height: 1fr;
      border: round $surface;
      margin-bottom: 1;
    }

    #focus-inputs {
      border: round $surface;
      padding: 0 1;
      margin-bottom: 1;
    }

    #focus-buttons {
      margin-top: 1;
      align-horizontal: right;
    }
    """

    def __init__(
        self,
        *,
        experiments: tuple[BacklogExperimentItem, ...],
        backlog_error: str,
    ) -> None:
        super().__init__()
        self._experiments = experiments
        self._backlog_error = str(backlog_error).strip()

    def compose(self) -> ComposeResult:
        with Vertical(id="focus-dialog"):
            yield Label("Focus Experiment", id="focus-title")
            yield Static(
                (
                    "Choose a backlog experiment and confirm the target identifiers. "
                    "If backlog data is missing, enter IDs manually below."
                ),
                markup=False,
            )
            if self._backlog_error:
                yield Static(
                    f"Backlog warning: {self._backlog_error}",
                    id="focus-backlog-warning",
                    markup=False,
                )
            experiment_items: list[ListItem] = []
            if self._experiments:
                for item in self._experiments:
                    marker = "*" if item.is_current else " "
                    experiment_items.append(
                        ListItem(
                            Label(
                                (
                                    f"[{marker}] {item.experiment_id} "
                                    f"(iter={item.iteration_id}, "
                                    f"type={item.experiment_type or '-'}, "
                                    f"status={item.status or '-'})"
                                )
                            )
                        )
                    )
            else:
                experiment_items.append(
                    ListItem(Label("(No backlog experiments found)"))
                )
            yield ListView(*experiment_items, id="focus-experiment-list")
            with Vertical(id="focus-inputs"):
                yield Label("Experiment ID")
                yield Input(value="", id="focus-experiment-id")
                yield Label("Iteration ID")
                yield Input(value="", id="focus-iteration-id")
            with Horizontal(id="focus-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="continue", variant="primary")

    def on_mount(self) -> None:
        experiment_list = self.query_one("#focus-experiment-list", ListView)
        if self._experiments:
            experiment_list.index = _default_experiment_index(self._experiments)
            self._sync_inputs_from_selection()
        experiment_list.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _sync_inputs_from_selection(self) -> None:
        if not self._experiments:
            return
        experiment_list = self.query_one("#focus-experiment-list", ListView)
        index = experiment_list.index
        if index is None or index < 0 or index >= len(self._experiments):
            return
        selected = self._experiments[index]
        self.query_one("#focus-experiment-id", Input).value = selected.experiment_id
        self.query_one("#focus-iteration-id", Input).value = selected.iteration_id

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "focus-experiment-list":
            self._sync_inputs_from_selection()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
            return
        if event.button.id != "continue":
            return
        experiment_id = self.query_one("#focus-experiment-id", Input).value.strip()
        iteration_id = self.query_one("#focus-iteration-id", Input).value.strip()
        if not experiment_id and not iteration_id:
            self.notify("Set experiment_id and/or iteration_id.")
            return
        self.dismiss((experiment_id, iteration_id))


class ExperimentCreateScreen(ModalScreen[tuple[str, str, str] | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    ExperimentCreateScreen {
      align: center middle;
    }

    #experiment-create-dialog {
      width: 100%;
      height: 100%;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #experiment-create-hypothesis-list {
      height: 1fr;
      border: round $surface;
      margin-bottom: 1;
    }

    #experiment-create-inputs {
      border: round $surface;
      padding: 0 1;
      margin-bottom: 1;
    }

    #experiment-create-buttons {
      margin-top: 1;
      align-horizontal: right;
    }
    """

    def __init__(
        self,
        *,
        experiments: tuple[BacklogExperimentItem, ...],
        hypotheses: tuple[BacklogHypothesisItem, ...],
    ) -> None:
        super().__init__()
        self._experiments = experiments
        self._open_hypotheses = tuple(
            item for item in hypotheses if item.hypothesis_id and not item.is_completed
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="experiment-create-dialog"):
            yield Label("Create Experiment", id="experiment-create-title")
            yield Static(
                (
                    "Pick an open hypothesis and confirm IDs. "
                    "Suggested IDs are editable before submit."
                ),
                markup=False,
            )
            hypothesis_items: list[ListItem] = []
            if self._open_hypotheses:
                for item in self._open_hypotheses:
                    hypothesis_items.append(
                        ListItem(
                            Label(
                                (
                                    f"{item.hypothesis_id} "
                                    f"(status={item.status or '-'}) "
                                    f"{item.title or ''}".strip()
                                )
                            )
                        )
                    )
            else:
                hypothesis_items.append(ListItem(Label("(No open hypotheses found)")))
            yield ListView(*hypothesis_items, id="experiment-create-hypothesis-list")
            with Vertical(id="experiment-create-inputs"):
                yield Label("Experiment ID")
                yield Input(value="", id="experiment-create-experiment-id")
                yield Label("Iteration ID")
                yield Input(value="", id="experiment-create-iteration-id")
            with Horizontal(id="experiment-create-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="continue", variant="primary")

    def on_mount(self) -> None:
        suggestion_experiment_id, suggestion_iteration_id = _next_suggested_ids(
            self._experiments
        )
        self.query_one(
            "#experiment-create-experiment-id", Input
        ).value = suggestion_experiment_id
        self.query_one(
            "#experiment-create-iteration-id", Input
        ).value = suggestion_iteration_id
        hypothesis_list = self.query_one("#experiment-create-hypothesis-list", ListView)
        hypothesis_list.index = 0
        hypothesis_list.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
            return
        if event.button.id != "continue":
            return
        experiment_id = self.query_one(
            "#experiment-create-experiment-id", Input
        ).value.strip()
        iteration_id = self.query_one(
            "#experiment-create-iteration-id", Input
        ).value.strip()
        if not experiment_id:
            self.notify("experiment_id is required.")
            return
        if not iteration_id:
            self.notify("iteration_id is required.")
            return

        hypothesis_id = ""
        if self._open_hypotheses:
            hypothesis_list = self.query_one(
                "#experiment-create-hypothesis-list", ListView
            )
            selected_index = hypothesis_list.index
            if selected_index is not None and 0 <= selected_index < len(
                self._open_hypotheses
            ):
                hypothesis_id = self._open_hypotheses[selected_index].hypothesis_id
        self.dismiss((experiment_id, iteration_id, hypothesis_id))


class ExperimentMoveScreen(ModalScreen[tuple[str, str, str] | None]):
    BINDINGS = [("escape", "cancel", "Cancel")]

    CSS = """
    ExperimentMoveScreen {
      align: center middle;
    }

    #experiment-move-dialog {
      width: 100%;
      height: 100%;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #experiment-move-experiment-list,
    #experiment-move-target-list {
      height: 1fr;
      border: round $surface;
      margin-bottom: 1;
    }

    #experiment-move-inputs {
      border: round $surface;
      padding: 0 1;
      margin-bottom: 1;
    }

    #experiment-move-buttons {
      margin-top: 1;
      align-horizontal: right;
    }
    """

    def __init__(
        self,
        *,
        experiments: tuple[BacklogExperimentItem, ...],
        backlog_error: str,
    ) -> None:
        super().__init__()
        self._experiments = experiments
        self._backlog_error = str(backlog_error).strip()
        self._targets: tuple[str, ...] = tuple(EXPERIMENT_TYPES)

    def compose(self) -> ComposeResult:
        with Vertical(id="experiment-move-dialog"):
            yield Label("Move Experiment", id="experiment-move-title")
            yield Static(
                (
                    "Choose an experiment and destination lifecycle type. "
                    "If backlog data is missing, enter IDs manually below."
                ),
                markup=False,
            )
            if self._backlog_error:
                yield Static(
                    f"Backlog warning: {self._backlog_error}",
                    id="experiment-move-backlog-warning",
                    markup=False,
                )
            experiment_items: list[ListItem] = []
            if self._experiments:
                for item in self._experiments:
                    marker = "*" if item.is_current else " "
                    experiment_items.append(
                        ListItem(
                            Label(
                                (
                                    f"[{marker}] {item.experiment_id} "
                                    f"(iter={item.iteration_id}, "
                                    f"type={item.experiment_type or '-'}, "
                                    f"status={item.status or '-'})"
                                )
                            )
                        )
                    )
            else:
                experiment_items.append(
                    ListItem(Label("(No backlog experiments found)"))
                )
            yield ListView(*experiment_items, id="experiment-move-experiment-list")

            with Vertical(id="experiment-move-inputs"):
                yield Label("Experiment ID")
                yield Input(value="", id="experiment-move-experiment-id")
                yield Label("Iteration ID")
                yield Input(value="", id="experiment-move-iteration-id")

            yield Label("Destination Type")
            target_items = [ListItem(Label(target)) for target in self._targets]
            yield ListView(*target_items, id="experiment-move-target-list")

            with Horizontal(id="experiment-move-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="continue", variant="primary")

    def on_mount(self) -> None:
        experiment_list = self.query_one("#experiment-move-experiment-list", ListView)
        if self._experiments:
            experiment_list.index = _default_experiment_index(self._experiments)
            self._sync_inputs_and_target()
        else:
            self.query_one("#experiment-move-target-list", ListView).index = 0
        experiment_list.focus()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _selected_experiment(self) -> BacklogExperimentItem | None:
        if not self._experiments:
            return None
        experiment_list = self.query_one("#experiment-move-experiment-list", ListView)
        index = experiment_list.index
        if index is None or index < 0 or index >= len(self._experiments):
            return None
        return self._experiments[index]

    def _set_target(self, value: str) -> None:
        target_list = self.query_one("#experiment-move-target-list", ListView)
        normalized = str(value).strip().lower()
        try:
            target_index = self._targets.index(normalized)
        except ValueError:
            target_index = 0
        target_list.index = target_index

    def _sync_inputs_and_target(self) -> None:
        selected = self._selected_experiment()
        if selected is None:
            return
        self.query_one(
            "#experiment-move-experiment-id", Input
        ).value = selected.experiment_id
        self.query_one(
            "#experiment-move-iteration-id", Input
        ).value = selected.iteration_id
        self._set_target(_default_move_target(selected.experiment_type))

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if event.list_view.id == "experiment-move-experiment-list":
            self._sync_inputs_and_target()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.action_cancel()
            return
        if event.button.id != "continue":
            return
        experiment_id = self.query_one("#experiment-move-experiment-id", Input).value
        experiment_id = experiment_id.strip()
        iteration_id = self.query_one("#experiment-move-iteration-id", Input).value
        iteration_id = iteration_id.strip()
        if not experiment_id and not iteration_id:
            self.notify("Set experiment_id and/or iteration_id.")
            return
        target_list = self.query_one("#experiment-move-target-list", ListView)
        target_index = target_list.index
        if target_index is None or not (0 <= target_index < len(self._targets)):
            self.notify("Select a destination type.")
            return
        target_type = self._targets[target_index]
        matching = [
            item
            for item in self._experiments
            if item.experiment_id == experiment_id and item.iteration_id == iteration_id
        ]
        if (
            len(matching) == 1
            and matching[0].experiment_type.strip().lower() == target_type
        ):
            self.notify("Destination type matches source type.")
            return
        self.dismiss((experiment_id, iteration_id, target_type))


class AutolabCockpitApp(App[None]):
    _MODE_ORDER: tuple[ViewMode, ...] = ("home", "runs", "files", "console", "help")
    _FILE_SOURCE_SCOPES: tuple[str, ...] = ("all", "stage", "common")
    _RUN_STATUS_FILTER_OPTIONS: tuple[str, ...] = (
        "all",
        "running",
        "completed",
        "failed",
    )
    _RUN_SORT_MODES: tuple[Literal["newest", "oldest", "status"], ...] = (
        "newest",
        "oldest",
        "status",
    )
    _RUN_SORT_LABELS = {
        "newest": "Newest",
        "oldest": "Oldest",
        "status": "Status",
    }
    _RUN_SORT_STATUS_ORDER = {
        "running": 0,
        "queued": 0,
        "pending": 0,
        "in_progress": 0,
        "submitted": 0,
        "partial": 1,
        "warning": 1,
        "needs_attention": 1,
        "pass": 2,
        "success": 2,
        "done": 2,
        "completed": 2,
        "synced": 2,
        "failed": 3,
        "fail": 3,
        "timeout": 3,
        "error": 3,
        "stopped": 3,
    }
    _AUTO_REFRESH_INTERVAL_SECONDS = 5.0
    _TONE_CLASSES = (
        "tone-success",
        "tone-info",
        "tone-warning",
        "tone-danger",
        "tone-muted",
    )
    _ARTIFACT_PREVIEW_MAX_CHARS = 12_000
    _CONSOLE_ERROR_KEYWORDS = (
        "error",
        "failed",
        "failure",
        "traceback",
        "exception",
        "fatal",
    )
    _RUN_PROBLEM_STATUSES = (
        "failed",
        "fail",
        "error",
        "timeout",
        "timed_out",
        "stopped",
    )

    CSS = """
    Screen {
      layout: vertical;
    }

    .tone-success {
      color: $success;
    }

    .tone-info {
      color: $accent;
    }

    .tone-warning {
      color: $warning;
    }

    .tone-danger {
      color: $error;
    }

    .tone-muted {
      color: $text-muted;
    }

    #status-rail {
      height: auto;
      margin: 0 1;
      padding: 0 1;
      border: round $accent;
    }

    #status-safety {
      width: 30;
    }

    #status-mode {
      width: 16;
    }

    #status-autorefresh {
      width: 17;
    }

    #status-advanced {
      width: 17;
    }

    #status-autorefresh {
      width: 18;
      content-align: right middle;
    }

    #status-selection {
      width: 24;
    }

    #status-updated {
      width: 16;
    }

    #status-snapshot {
      width: 20;
    }

    #status-console {
      width: 28;
    }

    #status-command {
      width: 42;
      content-align: right middle;
    }

    #status-running {
      width: 1fr;
      content-align: right middle;
    }

    #runs-filter-row {
      height: auto;
      margin-bottom: 1;
      align-vertical: middle;
    }

    #run-filter-label,
    #run-filter-status,
    #run-filter-input,
    #run-filter-clear,
    #run-sort-order {
      margin-right: 1;
    }

    #key-hints {
      height: auto;
      margin: 0 1 1 1;
      padding: 0 1;
      color: $text-muted;
    }

    #nav-row {
      height: auto;
      margin: 0 1;
      padding: 0 1;
      border: round $surface;
    }

    #workspace {
      height: 1fr;
      margin: 1;
    }

    .view-panel {
      border: round $accent;
      padding: 0 1;
      height: 1fr;
      display: none;
    }

    .view-title {
      text-style: bold;
      margin-bottom: 1;
    }

    .section-title {
      text-style: bold;
      margin-top: 1;
      margin-bottom: 0;
    }

    #home-action-list, #run-list, #artifact-list {
      border: round $surface;
      height: 1fr;
      min-height: 8;
    }

    #home-action-filter-row {
      height: auto;
      margin-bottom: 1;
      align-vertical: middle;
    }

    #home-action-filter-label {
      width: 16;
      color: $text-muted;
    }

    #home-action-filter-input {
      width: 1fr;
    }

    #run-details,
    #run-filter-row,
    #files-context,
    #home-stage-card,
    #home-stage-list,
    #home-blocker-card,
    #home-artifacts-card,
    #home-verification-card,
    #home-todos-card,
    #help-text {
      border: round $surface;
      padding: 0 1;
      height: auto;
      margin-bottom: 1;
    }

    #home-render-card {
      border: round $surface;
      padding: 0 1;
      height: 18;
      min-height: 12;
      margin-bottom: 1;
    }

    #home-render-title {
      text-style: bold;
      margin-bottom: 1;
    }

    #home-render-scroll {
      height: 1fr;
    }

    #home-render-buttons {
      height: auto;
      align-horizontal: right;
      margin-top: 1;
    }

    #artifact-content {
      width: 1fr;
    }

    #run-buttons,
    #file-buttons,
    #file-advanced-buttons {
      height: auto;
      align-horizontal: left;
      margin-top: 1;
    }

    #run-filter-row {
      height: auto;
      margin-top: 1;
      align-vertical: middle;
    }

    #run-filter-label {
      width: 18;
      color: $text-muted;
    }

    #run-filter-input {
      width: 16;
    }

    #files-filter-row {
      height: auto;
      margin-bottom: 1;
      align-vertical: middle;
    }

    #artifact-filter-label {
      width: 12;
      color: $text-muted;
    }

    #artifact-filter-input,
    #run-filter-input {
      width: 1fr;
    }

    #file-source-filter {
      display: none;
    }

    #run-filter-row {
      height: auto;
      margin-bottom: 1;
      align-vertical: middle;
    }

    #run-filter-label {
      width: 12;
      color: $text-muted;
    }

    #run-filter-input {
      width: 1fr;
    }

    #console-log {
      height: 1fr;
      border: round $surface;
    }

    #console-filter-row {
      height: auto;
      margin-bottom: 1;
      align-vertical: middle;
    }

    #console-filter-input {
      width: 1fr;
    }

    #console-follow {
      width: 14;
    }
    """

    BINDINGS = [
        ("1", "show_home", "Home"),
        ("2", "show_runs", "Runs"),
        ("3", "show_files", "Files"),
        ("4", "show_console", "Console"),
        ("5", "show_help", "Help"),
        ("h", "show_command_history", "History"),
        ("left_square_bracket", "show_previous_view", "Prev View"),
        ("right_square_bracket", "show_next_view", "Next View"),
        ("question_mark", "show_help", "Help"),
        ("ctrl+k", "command_palette", "Commands"),
        ("tab", "focus_next", "Next"),
        ("shift+tab", "focus_previous", "Prev"),
        ("enter", "activate_selection", "Activate"),
        ("slash", "focus_files_filter", "Filter Files"),
        ("f", "focus_mode_filter", "Filter"),
        ("v", "mode_v_shortcut", "Mode V"),
        ("home", "list_first", "List Start"),
        ("end", "list_last", "List End"),
        ("o", "quick_open", "Open"),
        ("t", "toggle_run_sort", "Sort Runs"),
        ("y", "toggle_runs_sort", "Sort Runs"),
        ("m", "quick_secondary", "Mode Quick"),
        ("j", "jump_to_item", "Jump"),
        ("e", "open_selected_in_editor", "Open Editor"),
        ("n", "next_missing_artifact", "Next Missing"),
        ("u", "toggle_safety_lock", "Unlock/Lock"),
        ("a", "toggle_auto_refresh", "Auto-refresh"),
        ("r", "refresh_snapshot", "Refresh"),
        ("R", "rerun_last_command", "Rerun Last"),
        ("p", "toggle_prompt_view", "Prompt View"),
        ("x", "toggle_advanced", "Advanced"),
        ("s", "stop_loop", "Stop Loop"),
        ("k", "stop_running_command", "Stop Command"),
        ("c", "clear_console", "Clear Console"),
        ("shift+e", "toggle_console_error_filter", "Errors-only"),
        ("w", "toggle_console_wrap", "Wrap Console"),
        ("ctrl+p", "toggle_console_follow", "Follow Console"),
        ("i", "inspect_selection", "Inspect"),
        ("b", "jump_to_problem_run", "Next Problem Run"),
        ("shift+b", "jump_to_previous_problem_run", "Prev Problem Run"),
        ("q", "quit", "Quit"),
        ("shift+c", "clear_command_history", "Clear command history"),
    ]

    def __init__(self, *, state_path: Path, tail_lines: int = 2000) -> None:
        super().__init__()
        self._state_path = state_path.expanduser().resolve()
        self._tail_lines = max(200, int(tail_lines))
        self._console_tail: deque[str] = deque(maxlen=self._tail_lines)
        self._command_history: deque[CommandHistoryItem] = deque(maxlen=20)
        self._console_wrap = False
        self._console_show_errors_only = False
        self._console_follow = True
        self._last_snapshot_refreshed_at: float | None = None
        self._armed = False
        self._run_status_filter = ""
        self._show_advanced = False
        self._show_full_prompt = False
        self._auto_refresh_enabled = False
        self._last_snapshot_refresh_at: float | None = None
        self._run_sort_mode: Literal["newest", "oldest", "status"] = "newest"
        self._mode: ViewMode = "home"
        self._snapshot_refreshed_at: float | None = None
        self._snapshot: CockpitSnapshot | None = None
        self._actions: tuple[ActionSpec, ...] = list_actions()
        self._actions_by_id: dict[str, ActionSpec] = {
            action.action_id: action for action in self._actions
        }

        self._home_action_ids: tuple[str, ...] = ()
        self._home_action_index = 0
        self._home_action_filter_query = ""
        self._run_status_filter: str = ""
        self._visible_runs: tuple[RunItem, ...] = ()
        self._selected_run_index = 0
        self._visible_runs: tuple[RunItem, ...] = ()
        self._selected_artifact_index = 0
        self._visible_runs: tuple[RunItem, ...] = ()
        self._current_artifacts: tuple[ArtifactItem, ...] = ()
        self._all_artifacts: tuple[ArtifactItem, ...] = ()
        self._visible_runs: tuple[RunItem, ...] = ()
        self._missing_artifacts_count = 0
        self._run_sort_newest_first = True
        self._file_source_filter = "all"
        self._files_missing_only = False
        self._files_source_filter = "all"
        self._artifact_filter_query = ""
        self._last_command_label: str | None = None
        self._last_command_exit_code: int | None = None
        self._last_command_return_code: int | None = None
        self._last_command_duration: float | None = None
        self._running_command_started_at: float | None = None
        self._last_command_intent: CommandIntent | None = None
        self._artifact_preview_max_chars: int = _ARTIFACT_PREVIEW_MAX_CHARS
        self._command_history: deque[CommandHistoryItem] = deque(
            maxlen=_COMMAND_HISTORY_MAX
        )
        self._last_snapshot_error: str | None = None
        self._active_history_item: CommandHistoryItem | None = None
        self._auto_refresh_enabled = False
        self._console_filter_query = ""

        self._runner = CommandRunner(
            on_line=self._handle_runner_line, on_done=self._handle_runner_done
        )
        self._running_intent: CommandIntent | None = None
        self._running_started_at: float | None = None
        self._running_command_label: str = ""
        self._last_command_exit_code: int | None = None
        self._last_command_elapsed: str = ""
        self._last_command_label = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="status-rail"):
            yield Static("Locked: read-only.", id="status-safety")
            yield Static("Mode: home", id="status-mode")
            yield Static("Advanced: hidden", id="status-advanced")
            yield Static("Auto-refresh: off", id="status-autorefresh")
            yield Static("Selection: -", id="status-selection")
            yield Static("Updated: n/a", id="status-updated")
            yield Static("Snapshot: n/a", id="status-snapshot")
            yield Static("Console wrap: off", id="status-console")
            yield Static("Command: n/a", id="status-command")
            yield Static("", id="status-running")
        yield Static(
            "Keys: 1-5 view | [/] cycle views | Home/End list | Enter activate | o open | m mode quick | ? help",
            id="key-hints",
            classes="tone-muted",
        )
        with Horizontal(id="nav-row"):
            yield Button("1 Home", id="nav-home", variant="primary")
            yield Button("2 Runs", id="nav-runs")
            yield Button("3 Files", id="nav-files")
            yield Button("4 Console", id="nav-console")
            yield Button("5 Help", id="nav-help")
            yield Button("Toggle Advanced", id="toggle-advanced")
        with Vertical(id="workspace"):
            with Vertical(id="home-view", classes="view-panel"):
                yield Static("Home", classes="view-title")
                yield Static("", id="home-stage-card", markup=False)
                yield Static("", id="home-stage-list", markup=False)
                yield Static("", id="home-stage-timeline", markup=False)
                with Vertical(id="home-render-card"):
                    yield Static("What Autolab Will Run Now", id="home-render-title")
                    with VerticalScroll(id="home-render-scroll"):
                        yield Markdown("", id="home-render-markdown", open_links=False)
                    with Horizontal(id="home-render-buttons"):
                        yield Button(
                            "Show Full Prompt",
                            id="home-render-toggle",
                            variant="default",
                        )
                yield Static("", id="home-blocker-card", markup=False)
                yield Static("", id="home-verification-card", markup=False)
                yield Static("", id="home-artifacts-card", markup=False)
                yield Static("", id="home-todos-card", markup=False)
                yield Static("Recommended Actions", classes="section-title")
                with Horizontal(id="home-action-filter-row"):
                    yield Static(
                        "Filter",
                        id="home-action-filter-label",
                        markup=False,
                    )
                    yield Input(
                        value=self._home_action_filter_query,
                        placeholder="Filter recommended actions by text...",
                        id="home-action-filter-input",
                    )
                    yield Button("Clear", id="home-action-filter-clear")
                yield ListView(id="home-action-list")
            with Vertical(id="runs-view", classes="view-panel"):
                yield Static("Runs", classes="view-title")
                with Horizontal(id="run-filter-row"):
                    yield Static("Filter", id="run-filter-label", markup=False)
                    yield Input(
                        value=self._run_status_filter,
                        placeholder="Filter runs by status/id/stage...",
                        id="run-filter-input",
                    )
                    yield Button("Clear", id="run-filter-clear")
                    yield Button("Status: all", id="run-filter-status")
                yield ListView(id="run-list")
                yield Static("", id="run-details", markup=False)
                with Horizontal(id="run-details-buttons"):
                    yield Button("Open Manifest", id="run-open-manifest")
                    yield Button("Open Metrics", id="run-open-metrics")
                    yield Button("Sort: Newest", id="run-sort-order")
            with Vertical(id="files-view", classes="view-panel"):
                yield Static("Files", classes="view-title")
                yield Static("", id="files-context", markup=False)
                with Horizontal(id="files-filter-row"):
                    yield Static(
                        "Name Filter", id="artifact-filter-label", markup=False
                    )
                    yield Input(
                        value=self._artifact_filter_query,
                        placeholder="Type to filter files by path...",
                        id="artifact-filter-input",
                    )
                    yield Button("Clear", id="artifact-filter-clear")
                    yield Button("Source: All", id="file-source-filter")
                    yield Button("Source: All", id="file-cycle-source-scope")
                yield ListView(id="artifact-list")
                with Horizontal(id="file-buttons"):
                    yield Button("Open Viewer", id="file-open-viewer")
                    yield Button("Open Editor", id="file-open-editor")
                    yield Button("Open Rendered", id="file-open-rendered")
                    yield Button("Open Context", id="file-open-context")
                    yield Button("Open Template", id="file-open-prompt")
                    yield Button("Open State", id="file-open-state")
                    yield Button("Filter: All", id="file-toggle-missing-filter")
                with Horizontal(id="file-advanced-buttons"):
                    yield Button(
                        "Start Loop (Advanced)", id="file-run-loop", variant="warning"
                    )
                    yield Button(
                        "Break Lock (Advanced)", id="file-lock-break", variant="error"
                    )
                    yield Button(
                        "Focus (Advanced)",
                        id="file-focus-experiment",
                        variant="default",
                    )
                    yield Button(
                        "Experiment Create (Advanced)",
                        id="file-experiment-create",
                        variant="default",
                    )
                    yield Button(
                        "Experiment Move (Advanced)",
                        id="file-experiment-move",
                        variant="default",
                    )
            with Vertical(id="console-view", classes="view-panel"):
                yield Static("Console", classes="view-title")
                with Horizontal(id="console-filter-row"):
                    yield Input(
                        value=self._console_filter_query,
                        placeholder="Filter console output... (empty = no filter)",
                        id="console-filter-input",
                    )
                    yield Button("Clear", id="console-filter-clear")
                    yield Button("Follow", id="console-follow", variant="warning")
                yield RichLog(
                    id="console-log",
                    markup=False,
                    wrap=self._console_wrap,
                    highlight=False,
                )
            with Vertical(id="help-view", classes="view-panel"):
                yield Static("Help", classes="view-title")
                yield Static("", id="help-text", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_snapshot_impl()
        self.set_interval(
            self._AUTO_REFRESH_INTERVAL_SECONDS,
            self._auto_refresh_tick,
        )
        self._update_help_text()
        self._update_ui_chrome()
        self._switch_mode("home")

    def on_key(self, event: events.Key) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if isinstance(self.focused, Input):
            if event.key == "escape" and self._clear_focused_filter():
                event.prevent_default()
            return
        mode_by_key = {
            "1": "home",
            "2": "runs",
            "3": "files",
            "4": "console",
            "5": "help",
        }
        next_mode = mode_by_key.get(event.key)
        if next_mode is None:
            return
        self._switch_mode(next_mode)
        event.stop()

    def _timestamp(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _shorten_command(self, command: str, *, max_len: int) -> str:
        if len(command) <= max_len:
            return command
        if max_len <= 4:
            return command[:max_len]
        return f"{command[: max_len - 3]}..."

    def _running_elapsed_label(self) -> str:
        if self._running_started_at is None:
            return "00:00"
        elapsed_seconds = max(0, int(time.perf_counter() - self._running_started_at))
        minutes, seconds = divmod(elapsed_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes:02d}:{seconds:02d}"

    def _auto_refresh_state_label(self) -> str:
        return "on" if self._auto_refresh_enabled else "off"

    def _last_command_summary(self) -> str:
        if self._last_command_exit_code is None:
            return ""
        return (
            f"last: {self._last_command_label} | exit={self._last_command_exit_code}"
            f" | {self._last_command_elapsed}"
        )

    def _append_console(self, text: str) -> None:
        line = f"[{self._timestamp()}] {text}"
        self._console_tail.append(line)
        if not self._passes_console_filters(line) or not self._console_follow:
            return
        log = self.query_one("#console-log", RichLog)
        log.write(line)

    def _passes_console_filters(self, line: str) -> bool:
        if self._console_show_errors_only and not self._is_console_error_line(line):
            return False
        query = self._console_filter_query.strip().lower()
        if not query:
            return True
        return query in str(line).lower()

    def _render_console_tail(self) -> None:
        log = self.query_one("#console-log", RichLog)
        log.clear()
        lines = self._console_tail
        lines = tuple(line for line in lines if self._passes_console_filters(line))
        for line in lines:
            log.write(line)

    def _clear_focused_filter(self) -> bool:
        focused = self.focused
        if not isinstance(focused, Input):
            return False

        if focused.id == "run-filter-input":
            if not self._run_status_filter:
                self._focus_mode_default()
                return True
            self.action_clear_runs_filter()
            self._focus_mode_default()
            return True

        if focused.id == "artifact-filter-input":
            if not self._artifact_filter_query:
                self._focus_mode_default()
                return True
            self.action_clear_files_filter()
            self._focus_mode_default()
            return True

        if focused.id == "console-filter-input":
            if not self._console_filter_query:
                self._focus_mode_default()
                return True
            self.action_clear_console_filter()
            self._focus_mode_default()
            return True

        if focused.id == "home-action-filter-input":
            if not self._home_action_filter_query:
                self._focus_mode_default()
                return True
            self._clear_home_action_filter()
            self._focus_mode_default()
            return True

        return False

    def _is_console_error_line(self, line: str) -> bool:
        normalized = str(line).lower()
        return any(
            re.search(rf"\b{re.escape(keyword)}\b", normalized)
            for keyword in self._CONSOLE_ERROR_KEYWORDS
        )

    def _display_path(self, path: Path) -> str:
        snapshot = self._snapshot
        if snapshot is None:
            return str(path)
        try:
            return str(path.relative_to(snapshot.repo_root))
        except ValueError:
            return str(path)

    def _set_tone(self, widget: Widget, tone_class: str) -> None:
        for class_name in self._TONE_CLASSES:
            widget.remove_class(class_name)
        if tone_class:
            widget.add_class(tone_class)

    def _tone_for_run_status(self, status: str) -> str:
        normalized = str(status).strip().lower()
        if normalized in {"completed", "synced", "pass", "success", "done"}:
            return "tone-success"
        if normalized in {"running", "queued", "pending", "submitted", "in_progress"}:
            return "tone-info"
        if normalized in {"partial", "warning", "needs_attention"}:
            return "tone-warning"
        if normalized in {"failed", "fail", "error", "timeout", "stopped"}:
            return "tone-danger"
        return "tone-muted"

    def _run_status_icon(self, status: str) -> str:
        normalized = str(status).strip().lower()
        if normalized in {"completed", "synced", "pass", "success", "done"}:
            return "[OK]"
        if normalized in {"running", "queued", "pending", "submitted", "in_progress"}:
            return "[RUN]"
        if normalized in {"partial", "warning", "needs_attention"}:
            return "[WARN]"
        if normalized in {"failed", "fail", "error", "timeout", "stopped"}:
            return "[ERR]"
        return "[INFO]"

    def _stage_timeline_icon(self, status: str) -> str:
        normalized = str(status).strip().lower()
        if normalized == "complete":
            return "[ok]"
        if normalized == "current":
            return "[->]"
        if normalized == "blocked":
            return "[!]"
        if normalized == "upcoming":
            return "[  ]"
        return "[??]"

    def _run_sort_label(self) -> str:
        return self._RUN_SORT_LABELS.get(self._run_sort_mode, "Newest")

    def _run_sort_button_label(self) -> str:
        return f"Sort: {self._run_sort_label()}"

    def _key_hints_text(self) -> str:
        auto_refresh_state = "on" if self._auto_refresh_enabled else "off"
        wrap_state = "on" if self._console_wrap else "off"
        follow_state = "on" if self._console_follow else "off"
        run_filter_state = "on" if self._run_status_filter else "off"
        console_filter_state = "on" if self._console_filter_query else "off"
        error_filter_state = "on" if self._console_show_errors_only else "off"
        parts = [
            "1-5 view",
            "[ / ] cycle",
            "Enter activate",
            "Home/End list",
            "h history",
            "o open",
            f"a auto-refresh({auto_refresh_state})",
            "u lock",
            "x advanced",
            "p prompt",
            "ctrl+k commands",
            "f filter",
            "r refresh",
            "R rerun",
            "? help",
            "q quit",
        ]
        if self._command_history:
            parts.append("C clear history")
        if self._mode == "console":
            parts.append(f"w wrap({wrap_state})")
            parts.append(f"ctrl+p follow({follow_state})")
            parts.append("c clear")
            parts.append(f"shift+e errors-only({error_filter_state})")
            parts.append(f"f filter({console_filter_state})")
        elif self._mode == "runs":
            parts.append("v manifest")
            parts.append("m metrics")
            parts.append("b next-problem")
            parts.append("B prev-problem")
            parts.append(f"f filter({run_filter_state})")
            parts.append(f"y sort({self._run_sort_mode})")
            parts.append("i inspect")
        elif self._mode == "files":
            parts.append("Enter viewer")
            parts.append("e editor")
            parts.append("n next-missing")
            filter_state = "on" if self._files_missing_only else "off"
            parts.append(f"m missing-only({filter_state})")
            name_filter_state = "on" if self._artifact_filter_query else "off"
            parts.append(f"/ name-filter({name_filter_state})")
            parts.append(
                f"f source({self._files_source_filter_label(self._file_source_filter)})"
            )
            parts.append("i inspect")
        elif self._mode == "home":
            parts.append("Enter recommended action")
            parts.append("m rendered prompt")
            parts.append("R rerun last command")
            parts.append("i inspect")
        elif self._mode == "console":
            parts.append("i inspect")
        if (
            self._running_intent is not None
            and self._running_intent.action_id == "run_loop"
        ):
            parts.append("s stop loop")
        return "Keys: " + " | ".join(parts)

    def _files_source_filter_label(self, value: str) -> str:
        return str(value).strip().lower() or "all"

    def _file_source_filter_display_label(self, value: str) -> str:
        normalized = self._files_source_filter_label(value)
        if normalized == "stage":
            return "stage files"
        if normalized == "common":
            return "common files"
        return "all files"

    def _set_files_source_filter(self, value: str) -> None:
        normalized = self._files_source_filter_label(value)
        if normalized not in self._FILE_SOURCE_SCOPES:
            normalized = "all"
        self._files_source_filter = normalized
        self._file_source_filter = normalized

    def _next_files_source_filter(self) -> str:
        try:
            index = self._FILE_SOURCE_SCOPES.index(
                self._files_source_filter_label(self._file_source_filter)
            )
        except ValueError:
            index = 0
        return self._FILE_SOURCE_SCOPES[(index + 1) % len(self._FILE_SOURCE_SCOPES)]

    def _files_scope_artifacts(self) -> tuple[ArtifactItem, ...]:
        snapshot = self._snapshot
        if snapshot is None:
            return ()
        stage = snapshot.current_stage
        stage_artifacts = snapshot.artifacts_by_stage.get(stage, ())
        source_scope = self._files_source_filter_label(self._file_source_filter)
        if source_scope == "stage":
            return stage_artifacts
        if source_scope == "common":
            return snapshot.common_artifacts
        merged: list[ArtifactItem] = []
        seen: set[Path] = set()
        for artifact in [*stage_artifacts, *snapshot.common_artifacts]:
            if artifact.path in seen:
                continue
            seen.add(artifact.path)
            merged.append(artifact)
        return tuple(merged)

    def _selection_status_label(self) -> str:
        if self._mode == "home":
            total = len(self._home_action_ids)
            index = self._home_action_index
            prefix = "Actions"
        elif self._mode == "runs":
            total = len(self._visible_runs)
            index = self._selected_run_index
            prefix = "Runs"
        elif self._mode == "files":
            total = len(self._current_artifacts)
            index = self._selected_artifact_index
            prefix = "Files"
        else:
            return "Selection: n/a"

        if total <= 0:
            return f"{prefix}: 0/0"
        position = min(max(index, 0), total - 1) + 1
        return f"{prefix}: {position}/{total}"

    def _action_label(self, action_id: str) -> str:
        action = self._actions_by_id.get(action_id)
        if action is not None and action.user_label:
            return action.user_label
        label = action_id.replace("_", " ").replace("-", " ").strip()
        return label or "command"

    def _update_ui_chrome(self) -> None:
        safety = self.query_one("#status-safety", Static)
        mode = self.query_one("#status-mode", Static)
        advanced = self.query_one("#status-advanced", Static)
        auto_refresh = self.query_one("#status-autorefresh", Static)
        selection = self.query_one("#status-selection", Static)
        updated = self.query_one("#status-updated", Static)
        console = self.query_one("#status-console", Static)
        command_status = self.query_one("#status-command", Static)
        running = self.query_one("#status-running", Static)
        snapshot_status = self.query_one("#status-snapshot", Static)
        key_hints = self.query_one("#key-hints", Static)

        safety.update(
            "Unlocked: mutating enabled." if self._armed else "Locked: read-only."
        )
        self._set_tone(safety, "tone-warning" if self._armed else "tone-success")
        mode.update(f"Mode: {self._mode}")
        auto_refresh = self.query_one("#status-autorefresh", Static)
        auto_refresh.update(f"Auto-refresh: {self._auto_refresh_state_label()}")
        self._set_tone(
            auto_refresh,
            "tone-info" if self._auto_refresh_enabled else "tone-muted",
        )
        advanced.update(
            "Advanced: visible" if self._show_advanced else "Advanced: hidden"
        )
        self._set_tone(advanced, "tone-info" if self._show_advanced else "tone-muted")
        auto_refresh.update(
            f"Auto-refresh: {'on' if self._auto_refresh_enabled else 'off'}"
        )
        self._set_tone(
            auto_refresh, "tone-info" if self._auto_refresh_enabled else "tone-muted"
        )
        selection_label = self._selection_status_label()
        selection.update(selection_label)
        self._set_tone(
            selection,
            "tone-info"
            if "0/0" not in selection_label and "n/a" not in selection_label
            else "tone-muted",
        )
        console.update(
            f"Console: wrap {'on' if self._console_wrap else 'off'} "
            f"| follow {'on' if self._console_follow else 'off'}"
        )
        self._set_tone(console, "tone-info" if self._console_follow else "tone-warning")
        console_follow_button = self.query_one("#console-follow", Button)
        console_follow_button.label = "Following" if self._console_follow else "Paused"
        console_follow_button.variant = (
            "primary" if self._console_follow else "default"
        )
        run_sort_button = self.query_one("#run-sort-order", Button)
        run_sort_button.label = self._run_sort_button_label()
        key_hints.update(self._key_hints_text())
        if self._running_intent is not None:
            elapsed: str = ""
            if self._running_command_started_at is not None:
                elapsed = (
                    f" ({time.monotonic() - self._running_command_started_at:.1f}s)"
                )
            command_status.update(
                f"Command: {self._action_label(self._running_intent.action_id)}{elapsed}"
            )
            self._set_tone(command_status, "tone-info")
        elif self._last_command_label is None:
            command_status.update("Command: n/a")
            self._set_tone(command_status, "tone-muted")
        else:
            last_exit = (
                f" exit:{self._last_command_exit_code}"
                if self._last_command_exit_code is not None
                else ""
            )
            duration = (
                f"{self._last_command_duration:.1f}s"
                if self._last_command_duration is not None
                else "n/a"
            )
            command_status.update(
                f"Last: {self._last_command_label} |{last_exit} | {duration}"
            )
            last_tone = (
                "tone-success" if self._last_command_exit_code == 0 else "tone-danger"
            )
            self._set_tone(command_status, last_tone)

        if self._snapshot is None or self._last_snapshot_refreshed_at is None:
            if self._last_snapshot_error:
                snapshot_status.update(f"Snapshot: error ({self._last_snapshot_error})")
                self._set_tone(snapshot_status, "tone-danger")
            else:
                snapshot_status.update("Snapshot: n/a")
                self._set_tone(snapshot_status, "tone-warning")
        else:
            age_seconds = time.monotonic() - self._last_snapshot_refreshed_at
            age_seconds = max(age_seconds, 0.0)
            snapshot_status.update(f"Snapshot: {age_seconds:.1f}s ago")
            if age_seconds < 10:
                tone = "tone-success"
            elif age_seconds < 60:
                tone = "tone-warning"
            else:
                tone = "tone-danger"
            self._set_tone(snapshot_status, tone)

        if self._running_intent is None:
            snapshot = self._snapshot
            last_summary = self._last_command_summary()
            if snapshot is None:
                running.update("Idle")
                self._set_tone(running, "tone-muted")
                if last_summary:
                    running.update(f"Idle | {last_summary}")
            else:
                stage_artifacts = snapshot.artifacts_by_stage.get(
                    snapshot.current_stage, ()
                )
                refresh_label = (
                    " | auto-refresh:on" if self._auto_refresh_enabled else ""
                )
                missing_required = sum(1 for item in stage_artifacts if not item.exists)
                blocker_count = (
                    0
                    if snapshot.primary_blocker == "none"
                    else len(snapshot.top_blockers)
                )
                status = (
                    "Idle | "
                    f"runs:{len(snapshot.runs)} "
                    f"blockers:{blocker_count} "
                    f"todos:{len(snapshot.todos)} "
                    f"missing:{missing_required}"
                    f"{refresh_label}"
                )
                if last_summary:
                    status = f"{status} | {last_summary}"
                running.update(status)
                if blocker_count or missing_required:
                    self._set_tone(running, "tone-warning")
                elif (
                    self._last_command_return_code is not None
                    and self._last_command_return_code != 0
                ):
                    self._set_tone(running, "tone-danger")
                else:
                    self._set_tone(running, "tone-success")
        else:
            command = self._running_command_label or shlex.join(
                self._running_intent.argv
            )
            running.update(
                "Running "
                f"({self._running_elapsed_label()}) | "
                f"{self._shorten_command(command, max_len=72)}"
            )
            self._set_tone(running, "tone-info")

        self.query_one(
            "#file-advanced-buttons", Horizontal
        ).display = self._show_advanced
        run_filter_clear = self.query_one("#run-filter-clear", Button)
        run_filter_clear.disabled = not bool(self._run_status_filter)
        run_filter_clear.variant = "primary" if self._run_status_filter else "default"
        console_filter_clear = self.query_one("#console-filter-clear", Button)
        console_filter_clear.disabled = not bool(self._console_filter_query)
        console_filter_clear.variant = "primary" if self._console_filter_query else "default"
        run_status_button = self.query_one("#run-filter-status", Button)
        run_status_value = self._run_status_filter.strip().lower()
        run_status_button.label = (
            f"Status: {run_status_value}"
            if run_status_value in self._RUN_STATUS_FILTER_OPTIONS
            and run_status_value != "all"
            else "Status: all"
        )
        filter_button = self.query_one("#file-toggle-missing-filter", Button)
        filter_button.label = (
            "Filter: Missing Only" if self._files_missing_only else "Filter: All"
        )
        filter_button.variant = "primary" if self._files_missing_only else "default"
        source_button = self.query_one("#file-cycle-source-scope", Button)
        source_scope = self._files_source_filter_label(self._file_source_filter)
        source_button.label = (
            "Source: All"
            if source_scope == "all"
            else f"Source: {source_scope.title()}"
        )
        source_button.variant = "warning" if source_scope != "all" else "default"
        source_alias_button = self.query_one("#file-source-filter", Button)
        source_alias_button.label = str(source_button.label)
        source_alias_button.variant = source_button.variant
        clear_button = self.query_one("#artifact-filter-clear", Button)
        clear_button.disabled = not bool(self._artifact_filter_query)
        run_clear_button = self.query_one("#run-filter-clear", Button)
        run_clear_button.disabled = not bool(self._run_status_filter)

    def _run_auto_refresh(self) -> None:
        if not self._auto_refresh_enabled:
            return
        if self._running_intent is not None:
            return
        if isinstance(self.screen, ModalScreen):
            return
        self._refresh_snapshot()

    def _cycle_mode(self, direction: int) -> None:
        if isinstance(self.screen, ModalScreen) or isinstance(self.focused, Input):
            return
        try:
            current_index = self._MODE_ORDER.index(self._mode)
        except ValueError:
            current_index = 0
        next_index = (current_index + direction) % len(self._MODE_ORDER)
        self._switch_mode(self._MODE_ORDER[next_index])

    def _switch_mode(self, mode: ViewMode) -> None:
        self._mode = mode
        panel_by_mode = {
            "home": "#home-view",
            "runs": "#runs-view",
            "files": "#files-view",
            "console": "#console-view",
            "help": "#help-view",
        }
        for key, selector in panel_by_mode.items():
            self.query_one(selector, Vertical).display = key == mode

        button_by_mode = {
            "home": "#nav-home",
            "runs": "#nav-runs",
            "files": "#nav-files",
            "console": "#nav-console",
            "help": "#nav-help",
        }
        for key, selector in button_by_mode.items():
            button = self.query_one(selector, Button)
            button.variant = "primary" if key == mode else "default"

        self._update_ui_chrome()
        self._focus_mode_default()

    def _focus_mode_default(self) -> None:
        if self._mode == "home":
            self.query_one("#home-action-list", ListView).focus()
        elif self._mode == "runs":
            self.query_one("#run-list", ListView).focus()
        elif self._mode == "files":
            self.query_one("#artifact-list", ListView).focus()
        elif self._mode == "console":
            self.query_one("#console-log", RichLog).focus()
        else:
            self.query_one("#help-text", Static).focus()

    def _selected_run(self):
        if not self._visible_runs:
            return None
        if self._selected_run_index >= len(self._visible_runs):
            self._selected_run_index = 0
        return self._visible_runs[self._selected_run_index]

    def _selected_artifact_path(self) -> Path | None:
        if not self._current_artifacts:
            return None
        if self._selected_artifact_index >= len(self._current_artifacts):
            self._selected_artifact_index = 0
        return self._current_artifacts[self._selected_artifact_index].path

    def _clear_snapshot_views(self) -> None:
        self._home_action_ids = ()
        self._home_action_index = 0
        self._selected_run_index = 0
        self._visible_runs = ()
        self._selected_artifact_index = 0
        self._visible_runs = ()
        self._current_artifacts = ()
        self._all_artifacts = ()
        self._visible_runs = ()
        self._missing_artifacts_count = 0

        home_actions = self.query_one("#home-action-list", ListView)
        home_actions.clear()
        home_actions.append(ListItem(Label("(snapshot unavailable)")))

        run_list = self.query_one("#run-list", ListView)
        run_list.clear()
        run_list.append(ListItem(Label("(snapshot unavailable)")))

        artifact_list = self.query_one("#artifact-list", ListView)
        artifact_list.clear()
        artifact_list.append(ListItem(Label("(snapshot unavailable)")))

        stage_widget = self.query_one("#home-stage-card", Static)
        stage_widget.update("Stage\nUnavailable.")
        self._set_tone(stage_widget, "tone-muted")

        stage_list_widget = self.query_one("#home-stage-list", Static)
        stage_list_widget.update("Stage timeline\nUnavailable.")
        self._set_tone(stage_list_widget, "tone-muted")

        render_card = self.query_one("#home-render-card", Vertical)
        render_markdown = self.query_one("#home-render-markdown", Markdown)
        render_markdown.update(
            build_preview_markdown(
                "Render preview unavailable because snapshot refresh failed.",
                hint="text",
            )
        )
        self._set_tone(render_card, "tone-danger")

        blocker_widget = self.query_one("#home-blocker-card", Static)
        blocker_widget.update("Blockers\nSnapshot refresh failed.")
        self._set_tone(blocker_widget, "tone-danger")

        verification_widget = self.query_one("#home-verification-card", Static)
        verification_widget.update("Verification\nUnavailable.")
        self._set_tone(verification_widget, "tone-muted")

        artifacts_widget = self.query_one("#home-artifacts-card", Static)
        artifacts_widget.update("Artifacts\nUnavailable.")
        self._set_tone(artifacts_widget, "tone-warning")

        todos_widget = self.query_one("#home-todos-card", Static)
        todos_widget.update("Open Tasks\nUnavailable.")
        self._set_tone(todos_widget, "tone-muted")

        run_widget = self.query_one("#run-details", Static)
        run_widget.update("Run Details\nUnavailable.")
        self._set_tone(run_widget, "tone-muted")

        files_widget = self.query_one("#files-context", Static)
        files_widget.update("Files\nUnavailable.")
        self._set_tone(files_widget, "tone-muted")

    def _refresh_snapshot_impl(self, *, from_auto: bool = False) -> bool:
        try:
            self._snapshot = load_cockpit_snapshot(self._state_path)
        except Exception as exc:
            self._snapshot = None
            self._snapshot_refreshed_at = None
            self._last_snapshot_error = str(exc).strip() or "refresh failed"
            self._armed = False
            self._last_snapshot_refresh_at = None
            self._clear_snapshot_views()
            self._update_ui_chrome()
            self._append_console(f"snapshot refresh failed: {exc}")
            if not from_auto:
                self.notify(f"Snapshot refresh failed: {exc}")
            return False
        self._last_snapshot_error = None
        self._last_snapshot_refreshed_at = time.monotonic()

        self._populate_home_view()
        self._populate_run_list()
        self._populate_artifact_list()
        self._last_snapshot_refresh_at = time.time()
        self._update_ui_chrome()
        return True

    def _refresh_snapshot(self, *, from_auto: bool = False) -> bool:
        return self._refresh_snapshot_impl(from_auto=from_auto)

    def _populate_home_view(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return

        stage = snapshot.current_stage
        stage_widget = self.query_one("#home-stage-card", Static)
        stage_widget.update(
            "Stage\n"
            f"- Current: {stage}\n"
            f"- Attempt: {snapshot.stage_attempt}/{snapshot.max_stage_attempts}\n"
            f"- Summary: {snapshot.stage_summaries.get(stage, 'No summary available.')}"
        )
        self._set_tone(stage_widget, "tone-info")

        stage_list_widget = self.query_one("#home-stage-list", Static)
        stage_lines = ["Stage Timeline"]
        status_markers = {
            "complete": "[done]",
            "current": "[next]",
            "blocked": "[wait]",
            "upcoming": "[todo]",
        }
        for item in snapshot.stage_items:
            stage_name = str(item.name).strip()
            attempts = f" ({item.attempts})" if item.attempts else ""
            marker = status_markers.get(item.status, "[     ]")
            stage_lines.append(f"{marker} {stage_name}{attempts} [{item.status}]")
        stage_list_widget.update("\n".join(stage_lines))
        self._set_tone(stage_list_widget, "tone-info")

        workflow_widget = self.query_one("#home-stage-timeline", Static)
        workflow_lines = ["Workflow Timeline"]
        for item in snapshot.stage_items:
            stage_name = str(item.name).strip()
            attempts = f" ({item.attempts})" if item.attempts else ""
            workflow_lines.append(
                f"{self._stage_timeline_icon(item.status)} > {stage_name}{attempts}"
            )
        workflow_widget.update("\n".join(workflow_lines))
        self._set_tone(workflow_widget, "tone-info")

        render_card = self.query_one("#home-render-card", Vertical)
        render_markdown = self.query_one("#home-render-markdown", Markdown)
        render_toggle = self.query_one("#home-render-toggle", Button)
        render_preview = snapshot.render_preview
        if render_preview.status == "ok":
            render_toggle.disabled = False
            render_toggle.label = (
                "Show Excerpt" if self._show_full_prompt else "Show Full Prompt"
            )
            prompt_text = (
                render_preview.prompt_text
                if self._show_full_prompt
                else (render_preview.prompt_excerpt or render_preview.prompt_text)
            )
            prompt_markdown = build_preview_markdown(
                prompt_text,
                source_path=render_preview.template_path,
                hint="markdown",
            )
            if (
                not self._show_full_prompt
                and render_preview.prompt_excerpt
                and render_preview.prompt_excerpt != render_preview.prompt_text
            ):
                prompt_markdown = (
                    f"{prompt_markdown}\n\n"
                    "_Excerpt shown. Toggle to full prompt for complete content._"
                )
            render_markdown.update(
                f"**Stage:** `{render_preview.stage}`\n\n{prompt_markdown}"
            )
            self._set_tone(render_card, "tone-info")
        elif render_preview.status == "unavailable":
            render_toggle.disabled = True
            render_toggle.label = "Show Full Prompt"
            render_markdown.update(
                build_preview_markdown(
                    "Render preview unavailable for this stage.",
                    hint="text",
                )
            )
            self._set_tone(render_card, "tone-warning")
        else:
            render_toggle.disabled = True
            render_toggle.label = "Show Full Prompt"
            template_hint = (
                self._display_path(render_preview.template_path)
                if render_preview.template_path is not None
                else "unknown template path"
            )
            render_markdown.update(
                build_preview_markdown(
                    "Render preview failed.\n"
                    f"Error: {render_preview.error_message or 'unknown render error'}\n"
                    f"Template: {template_hint}",
                    hint="text",
                )
            )
            self._set_tone(render_card, "tone-danger")

        blocker_lines = [f"- Primary: {snapshot.primary_blocker}"]
        if snapshot.secondary_blockers:
            blocker_lines.append("- Additional blockers:")
            blocker_lines.extend(f"- {entry}" for entry in snapshot.secondary_blockers)
        blocker_widget = self.query_one("#home-blocker-card", Static)
        blocker_widget.update("Blockers\n" + "\n".join(blocker_lines))
        self._set_tone(
            blocker_widget,
            "tone-success" if snapshot.primary_blocker == "none" else "tone-danger",
        )

        verification = snapshot.verification
        verification_widget = self.query_one("#home-verification-card", Static)
        if verification is None:
            verification_widget.update(
                "Verification\n- Result: not available.\n- Run autolab verify to capture results."
            )
            self._set_tone(verification_widget, "tone-muted")
        else:
            verification_lines = [
                f"- Result: {'pass' if verification.passed else 'fail'}",
                f"- Stage: {verification.stage_effective or snapshot.current_stage or '-'}",
                f"- Message: {verification.message or 'no message'}",
            ]
            if verification.generated_at:
                verification_lines.insert(1, f"- Updated: {verification.generated_at}")
            if verification.failing_commands:
                verification_lines.append("- Failing command(s):")
                verification_lines.extend(
                    f"  - {entry}" for entry in verification.failing_commands[:2]
                )
            verification_widget.update("Verification\n" + "\n".join(verification_lines))
            self._set_tone(
                verification_widget,
                "tone-success" if verification.passed else "tone-danger",
            )

        stage_artifacts = snapshot.artifacts_by_stage.get(stage, ())
        if stage_artifacts:
            lines = [
                f"- {'OK' if item.exists else 'MISS'} {self._display_path(item.path)}"
                for item in stage_artifacts
            ]
            artifact_text = "Required Artifacts\n" + "\n".join(lines)
        else:
            artifact_text = "Required Artifacts\n- None for this stage."
        artifacts_widget = self.query_one("#home-artifacts-card", Static)
        artifacts_widget.update(artifact_text)
        has_missing = any(not item.exists for item in stage_artifacts)
        self._set_tone(
            artifacts_widget, "tone-warning" if has_missing else "tone-success"
        )

        todos_widget = self.query_one("#home-todos-card", Static)
        if snapshot.todos:
            todo_lines: list[str] = []
            for item in snapshot.todos[:5]:
                priority = str(item.priority).strip().lower() or "unspecified"
                stage_hint = str(item.stage).strip().lower() or "unknown"
                summary = (
                    str(item.text).strip() or str(item.task_id).strip() or "(empty)"
                )
                todo_lines.append(f"- [{priority}] ({stage_hint}) {summary}")
            extra = len(snapshot.todos) - len(todo_lines)
            if extra > 0:
                todo_lines.append(f"- ... and {extra} more")
            todos_widget.update("Open Tasks\n" + "\n".join(todo_lines))
            urgent = any(
                str(item.priority).strip().lower() in {"critical", "high"}
                for item in snapshot.todos
            )
            self._set_tone(todos_widget, "tone-warning" if urgent else "tone-info")
        else:
            todos_widget.update("Open Tasks\n- None detected.")
            self._set_tone(todos_widget, "tone-success")

        action_list = self.query_one("#home-action-list", ListView)
        action_list.clear()
        action_ids: list[str] = []
        query = self._home_action_filter_query.strip().lower()
        for recommended in snapshot.recommended_actions:
            action = self._actions_by_id.get(recommended.action_id)
            if action is None:
                continue
            if action.advanced and not self._show_advanced:
                continue
            action_label = action.user_label or action.label
            action_help = action.help_text or action.description
            if (
                query
                and query not in action_label.lower()
                and query not in action_help.lower()
                and query not in recommended.reason.lower()
            ):
                continue
            label = action.user_label or action.label
            tags = []
            if action.requires_arm:
                tags.append("mutating")
            tags.append(f"risk:{action.risk_level}")
            tag_text = f" [{' / '.join(tags)}]"
            item_label = Label(f"{label}: {recommended.reason}{tag_text}")
            item_label.add_class("tone-warning" if action.requires_arm else "tone-info")
            action_list.append(ListItem(item_label))
            action_ids.append(action.action_id)

        if not action_ids:
            if query:
                item_label = Label(
                    f"No matching recommended actions for '{self._home_action_filter_query}'."
                )
                item_label.add_class("tone-warning")
                action_list.append(ListItem(item_label))
                action_ids = ()
            else:
                fallback_action_id = (
                    "open_rendered_prompt"
                    if snapshot.render_preview.status == "ok"
                    else "open_stage_prompt"
                )
                fallback_text = (
                    "Open rendered prompt: preview what will run next."
                    if fallback_action_id == "open_rendered_prompt"
                    else "Open stage prompt template."
                )
                item_label = Label(fallback_text)
                item_label.add_class("tone-info")
                action_list.append(ListItem(item_label))
                action_ids.append(fallback_action_id)

        self._home_action_ids = tuple(action_ids)
        if self._home_action_ids:
            self._home_action_index = min(
                self._home_action_index, len(self._home_action_ids) - 1
            )
            action_list.index = self._home_action_index
        else:
            self._home_action_index = 0

    def _populate_run_list(
        self, *, preserve_selected_run_id: str | None = None
    ) -> None:
        snapshot = self._snapshot
        run_list = self.query_one("#run-list", ListView)
        run_list.clear()
        self._visible_runs = ()
        if snapshot is None:
            self._visible_runs = ()
            return
        runs = list(snapshot.runs)

        def _run_timestamp(run: RunItem) -> float:
            parsed = self._parse_run_timestamp(run.started_at or "")
            return parsed.timestamp() if parsed is not None else float("-inf")

        if self._run_sort_mode == "newest":
            runs.sort(key=lambda run: (_run_timestamp(run), run.run_id), reverse=True)
        elif self._run_sort_mode == "oldest":
            runs.sort(key=lambda run: (_run_timestamp(run), run.run_id))
        else:
            runs.sort(
                key=lambda run: (
                    self._RUN_SORT_STATUS_ORDER.get(
                        str(run.status).strip().lower(), 99
                    ),
                    -_run_timestamp(run),
                    run.run_id,
                )
            )

        filter_query = self._run_status_filter.strip().lower()
        if filter_query:
            filtered_runs: list[RunItem] = []
            for run in runs:
                haystack = " ".join(
                    [
                        str(run.run_id),
                        str(run.status),
                        str(run.host_mode),
                        str(run.job_id),
                        str(run.sync_status),
                        str(run.started_at),
                        str(run.completed_at),
                    ]
                )
                if _query_matches(haystack, filter_query):
                    filtered_runs.append(run)
            self._visible_runs = tuple(filtered_runs)
        else:
            self._visible_runs = tuple(runs)

        if not self._visible_runs:
            empty_label = Label("(No runs found yet)")
            if filter_query:
                empty_label.update(
                    f"(No runs match current filter query={self._run_status_filter!r})"
                )
            empty_label.add_class("tone-muted")
            run_list.append(ListItem(empty_label))
            self._selected_run_index = 0
            details_widget = self.query_one("#run-details", Static)
            details_widget.update(
                "Run Details\nNo runs match the active filter.\n"
                "Use / and clear to show all runs."
            )
            self._set_tone(details_widget, "tone-muted")
            return

        if preserve_selected_run_id is not None:
            for index, run in enumerate(self._visible_runs):
                if run.run_id == preserve_selected_run_id:
                    self._selected_run_index = index
                    break

        run_total = len(self._visible_runs)
        for position, run in enumerate(self._visible_runs, start=1):
            started = run.started_at or "-"
            slurm_suffix = ""
            if run.host_mode == "slurm":
                slurm_suffix = f" job={run.job_id or '-'}"
            run_label = Label(
                f"{self._run_status_icon(run.status)} {run.run_id} [{run.status}] "
                f"({run.host_mode}{slurm_suffix}) start={started}"
            )
            run_label.add_class(self._tone_for_run_status(run.status))
            run_list.append(ListItem(run_label))
        self._selected_run_index = min(
            self._selected_run_index, len(self._visible_runs) - 1
        )
        run_list.index = self._selected_run_index
        self._update_run_details()

    def _parse_run_timestamp(self, raw_value: str) -> datetime | None:
        raw = str(raw_value).strip()
        if not raw:
            return None
        normalized = raw.replace(" ", "T")
        normalized = normalized.replace("Z", "+00:00")
        for fmt in (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S%z",
            "%Y-%m-%dT%H:%M:%S.%f%z",
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M:%S.%f",
        ):
            try:
                return datetime.strptime(normalized, fmt)
            except ValueError:
                continue
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None

    def _run_duration_text(self, run: RunItem) -> str:
        if not run.started_at:
            return "-"
        started = self._parse_run_timestamp(run.started_at)
        if started is None:
            return "-"

        ended = self._parse_run_timestamp(run.completed_at or "")
        if ended is None and run.completed_at:
            return "-"
        if ended is None:
            now = (
                datetime.now(started.tzinfo)
                if started.tzinfo is not None
                else datetime.now()
            )
            ended = now
        elapsed = (ended - started).total_seconds()
        return f"{elapsed:.1f}s"

    def _update_run_details(self) -> None:
        run = self._selected_run()
        snapshot = self._snapshot
        if run is None:
            details_widget = self.query_one("#run-details", Static)
            details_widget.update("Run Details\nNo run selected.")
            self._set_tone(details_widget, "tone-muted")
            return
        completion = (
            "finished" if _is_completed_backlog_status(run.status) else "active"
        )
        run_duration = self._run_duration_text(run)
        selected_index = self._selected_run_index + 1
        run_count = len(self._visible_runs)
        total_count = len(snapshot.runs) if snapshot is not None else run_count
        details_widget = self.query_one("#run-details", Static)
        query_text = (
            f"- query={self._run_status_filter!r}\n" if self._run_status_filter else ""
        )
        details_widget.update(
            "Run Details\n"
            f"- Selected: {selected_index}/{run_count}\n"
            f"- Total runs: {total_count}\n"
            f"- Filter: {self._run_status_filter or 'none'}\n"
            f"{query_text}"
            f"- Completion: {completion}\n"
            f"- Duration: {run_duration}\n"
            f"- Run ID: {run.run_id}\n"
            f"- Status: {run.status}\n"
            f"- Host mode: {run.host_mode}\n"
            f"- SLURM Job ID: {run.job_id or '-'}\n"
            f"- Artifact sync: {run.sync_status or '-'}\n"
            f"- Started: {run.started_at or '-'}\n"
            f"- Completed: {run.completed_at or '-'}\n"
            f"- Manifest: {'OK' if run.manifest_path.exists() else 'MISS'}\n"
            f"- Metrics: {'OK' if run.metrics_path.exists() else 'MISS'}\n"
            f"- Sort: {self._run_sort_mode}\n"
            "- Keys: Enter open manifest | Open Metrics button for metrics"
        )
        self._set_tone(details_widget, self._tone_for_run_status(run.status))

    def _populate_artifact_list(
        self, *, preserve_artifact_path: Path | None = None
    ) -> None:
        snapshot = self._snapshot
        artifact_list = self.query_one("#artifact-list", ListView)
        artifact_list.clear()
        self._current_artifacts = ()
        self._all_artifacts = ()
        self._missing_artifacts_count = 0

        if snapshot is None:
            return

        scope_artifacts = self._files_scope_artifacts()
        self._all_artifacts = scope_artifacts
        self._missing_artifacts_count = sum(
            1 for artifact in self._all_artifacts if not artifact.exists
        )
        if preserve_artifact_path is not None:
            for candidate_index, artifact in enumerate(self._all_artifacts):
                if artifact.path == preserve_artifact_path:
                    self._selected_artifact_index = candidate_index
                    break
        visible_artifacts: tuple[ArtifactItem, ...]
        if self._files_missing_only:
            visible_artifacts = tuple(
                artifact for artifact in self._all_artifacts if not artifact.exists
            )
        else:
            visible_artifacts = self._all_artifacts
        query = self._artifact_filter_query.strip().lower()
        if query:
            self._current_artifacts = tuple(
                artifact
                for artifact in visible_artifacts
                if _query_matches(self._display_path(artifact.path), query)
            )
        else:
            self._current_artifacts = visible_artifacts

        if preserve_artifact_path is not None:
            visible_match = next(
                (
                    idx
                    for idx, artifact in enumerate(self._current_artifacts)
                    if artifact.path == preserve_artifact_path
                ),
                None,
            )
            if visible_match is not None:
                self._selected_artifact_index = visible_match

        if not self._current_artifacts:
            if query:
                empty_text = (
                    f"(No files match name filter: {self._artifact_filter_query})"
                )
            else:
                if self._file_source_filter == "stage":
                    empty_text = "(No stage files)"
                elif self._file_source_filter == "common":
                    empty_text = "(No common files)"
                elif self._files_missing_only:
                    empty_text = "(No missing files for this stage)"
                else:
                    empty_text = "(No relevant files)"
            empty_label = Label(empty_text)
            empty_label.add_class("tone-muted")
            artifact_list.append(ListItem(empty_label))
            self._selected_artifact_index = 0
            context_widget = self.query_one("#files-context", Static)
            context_widget.update(
                "Files\n"
                f"- Stage: {snapshot.current_stage}\n"
                f"- Source: {self._file_source_filter_display_label(self._file_source_filter)}\n"
                f"- Filter: {'missing only' if self._files_missing_only else 'all files'}\n"
                f"- Name filter: {self._artifact_filter_query or 'none'}\n"
                f"- {empty_text}\n"
                "- Tip: use / to focus filter, Esc to continue, and Clear button to reset."
            )
            self._set_tone(context_widget, "tone-muted")
            return

        artifact_total = len(self._current_artifacts)
        for position, artifact in enumerate(self._current_artifacts, start=1):
            marker = "OK" if artifact.exists else "MISS"
            entry = Label(
                f"{position:>2}/{artifact_total} [{marker}] {self._display_path(artifact.path)}"
            )
            entry.add_class("tone-success" if artifact.exists else "tone-warning")
            artifact_list.append(ListItem(entry))
        self._selected_artifact_index = min(
            self._selected_artifact_index,
            len(self._current_artifacts) - 1,
        )
        artifact_list.index = self._selected_artifact_index
        self._update_files_context()

    def _update_files_context(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            context_widget = self.query_one("#files-context", Static)
            context_widget.update("Files\nUnavailable.")
            self._set_tone(context_widget, "tone-muted")
            return
        selected = self._selected_artifact_path()
        selected_text = self._display_path(selected) if selected else "none"
        visible_count = len(self._current_artifacts)
        selected_index = min(self._selected_artifact_index + 1, visible_count)
        total_count = len(self._all_artifacts)
        missing_count = self._missing_artifacts_count
        context_widget = self.query_one("#files-context", Static)
        context_widget.update(
            "Files\n"
            f"- Stage: {snapshot.current_stage}\n"
            f"- Source: {self._file_source_filter_display_label(self._file_source_filter)}\n"
            f"- Item: {selected_index}/{visible_count}\n"
            f"- Selected: {selected_text}\n"
            f"- Filter: {'missing only' if self._files_missing_only else 'all files'}\n"
            f"- Name filter: {self._artifact_filter_query or 'none'}\n"
            f"- Missing files: {missing_count}\n"
            f"- Showing: {visible_count}/{total_count} (missing: {missing_count})\n"
            "- View-only: Viewer, Editor, Rendered, Context, Template, State\n"
            "- Mutating: Loop, Lock Break, Focus, Experiment Create/Move\n"
            "  (unlock + confirm required)\n"
            "- Keys: Enter open viewer | n next missing | / focus name filter"
        )
        self._set_tone(context_widget, "tone-info")

    def _command_history_lines(self) -> str:
        if not self._command_history:
            return "No commands executed yet."

        lines: list[str] = []
        for item in list(self._command_history)[:5]:
            status = "OK" if item.exit_code == 0 else f"ERR({item.exit_code})"
            duration = (
                f"{item.duration_seconds:.1f}s"
                if item.duration_seconds is not None
                else "n/a"
            )
            command = item.command
            if len(command) > 110:
                command = command[:107] + "..."
            lines.append(f"- {item.label}: {status} ({duration})\n  {command}")
        return "\n".join(lines)

    def _update_help_text(self) -> None:
        help_widget = self.query_one("#help-text", Static)
        help_widget.update(
            "Autolab TUI\n"
            "\n"
            "Keyboard\n"
            "- Global: 1-5 switch views, Tab/Shift+Tab move focus, Enter activate.\n"
            "- Lists: Home/End jump to first or last list item.\n"
            "- Safety: u unlock/lock, x toggle advanced, q quit.\n"
            "- Utilities: r refresh, a auto-refresh on/off, s stop active loop,\n"
            "  c clear console, Shift+C clear command history.\n"
            "- Console: w toggle wrap, ctrl+p follow, shift+e errors-only toggle.\n"
            "- Filters: f (or / in files/runs) focuses active filter input.\n"
            "  Console input includes a text search filter and error-only toggle.\n"
            "- History: R reruns the last command after confirmation.\n"
            "- History: h opens command history for quick replay.\n"
            "- Home: p toggle prompt excerpt/full.\n"
            "- Runs: t/y toggles newest/oldest sort, b next problem run, Shift+B previous problem run.\n"
            "- Files: v cycles all/stage/common source.\n"
            "- Modals: Esc closes or cancels.\n"
            "\n"
            "Views\n"
            "- Home: stage status, stage timeline, rendered prompt preview, recommended actions.\n"
            "- Home: open tasks card highlights active todo priorities.\n"
            "- Runs: run manifest and metrics overview with optional run filtering.\n"
            "- Files: artifacts plus rendered prompt/context/template quick-open.\n"
            "- Files advanced: focus experiment, create experiment, move experiment.\n"
            "- Console: live command output with optional error-only filtering.\n"
            "\n"
            "Keys\n"
            "- 1-5: jump directly to Home/Runs/Files/Console/Help.\n"
            "- [ and ]: cycle views.\n"
            "- Enter: activate selected list item.\n"
            "- v/t/y: cycle run sorting order (recent, oldest, status).\n"
            "- w: toggle console line wrapping.\n"
            "- shift+e: toggle console error-only filter (Console view).\n"
            "- ctrl+p: toggle console follow mode (Console view).\n"
            "- f: focus and filter in active mode (Runs/Files/Console/Home views).\n"
            "Quick Actions\n"
            "- o: Open selected item in current view (action/manifest/viewer).\n"
            "- v: Open selected run manifest from Runs view.\n"
            "- m: Mode quick action (home rendered prompt, runs metrics, files filter).\n"
            "- t: Toggle runs sort order (newest, oldest, status).\n"
            "- b: Jump to next problematic run when available (Runs view).\n"
            "- Shift+B: Jump to previous problematic run.\n"
            "- e: Open selected file in editor (Files view).\n"
            "- /: Focus active filter input (Runs or Files view).\n"
            "- Ctrl+k: Open command palette.\n"
            "- s: Stop active command (if running).\n"
            "\n"
            "Safety\n"
            "- Starts locked (read-only).\n"
            "- Unlock before mutating commands.\n"
            "- Every mutating command requires confirmation.\n"
            "- Cockpit auto-locks after mutating command completion.\n"
            "- Snapshot refresh failures fail closed and lock actions.\n"
            "\n"
            "Color Cues\n"
            "- Green: ready/success\n"
            "- Blue: active/info\n"
            "- Yellow: warning/attention\n"
            "- Red: blocking/error\n"
            "- Command: last action result is shown in status rail."
        )
        self._set_tone(help_widget, "tone-muted")

    def _start_ui_flow(
        self,
        *,
        label: str,
        flow_factory: Callable[[], Awaitable[None]],
    ) -> None:
        async def _run_flow_guarded() -> None:
            try:
                await flow_factory()
            except Exception as exc:
                self._append_console(f"ui action failed ({label}): {exc}")
                self.notify(f"UI action failed ({label}): {exc}")

        self.run_worker(
            _run_flow_guarded(),
            name=f"ui-flow:{label}",
            group="tui-ui-flows",
            exit_on_error=False,
        )

    def _auto_refresh_tick(self) -> None:
        if not self._auto_refresh_enabled or self._running_intent is not None:
            return
        if isinstance(self.screen, ModalScreen):
            return
        self._refresh_snapshot(from_auto=True)

    def get_system_commands(self, screen) -> list[SystemCommand]:
        commands = list(super().get_system_commands(screen))
        commands.extend(
            [
                SystemCommand(
                    "Go to Home view",
                    "Switch to Home stage overview.",
                    self.action_show_home,
                ),
                SystemCommand(
                    "Go to Runs view",
                    "Switch to Runs list and details.",
                    self.action_show_runs,
                ),
                SystemCommand(
                    "Go to Files view",
                    "Switch to Files list and artifact tools.",
                    self.action_show_files,
                ),
                SystemCommand(
                    "Go to Console view",
                    "Switch to live command output.",
                    self.action_show_console,
                ),
                SystemCommand(
                    "Go to Help view",
                    "Open keymap and safety guidance.",
                    self.action_show_help,
                ),
                SystemCommand(
                    "Open command history",
                    "Review completed command results and status.",
                    self.action_show_command_history,
                ),
                SystemCommand(
                    "Show command history",
                    "Review completed command results and status.",
                    self.action_show_command_history,
                ),
                SystemCommand(
                    "Refresh snapshot",
                    "Reload state, runs, and artifact status.",
                    self.action_refresh_snapshot,
                ),
                SystemCommand(
                    "Toggle advanced actions",
                    "Show or hide advanced actions.",
                    self.action_toggle_advanced,
                ),
                SystemCommand(
                    "Toggle auto-refresh",
                    "Enable or disable automatic periodic snapshot refresh.",
                    self.action_toggle_auto_refresh,
                ),
                SystemCommand(
                    "Toggle safety lock",
                    "Lock or unlock mutating actions.",
                    self.action_toggle_safety_lock,
                ),
                SystemCommand(
                    "Toggle auto-refresh",
                    "Enable or disable periodic snapshot refresh.",
                    self.action_toggle_auto_refresh,
                ),
            ]
        )
        if self._mode in {"home", "runs", "files"}:
            commands.append(
                SystemCommand(
                    "Quick open selected item",
                    "Run the primary open action for this view.",
                    self.action_quick_open,
                )
            )
            commands.append(
                SystemCommand(
                    "Run mode quick action",
                    "Run the secondary quick action for this view.",
                    self.action_quick_secondary,
                )
            )
        commands.append(
            SystemCommand(
                "Inspect selected item",
                "Open a read-only inspector for the active selection.",
                self.action_inspect_selection,
            )
        )
        if self._command_history:
            commands.append(
                SystemCommand(
                    "Clear command history",
                    "Remove all command history entries from this TUI session.",
                    self.action_clear_command_history,
                )
            )
        if self._last_command_intent is not None:
            commands.append(
                SystemCommand(
                    "Rerun last command",
                    "Re-run the most recently started command.",
                    self.action_rerun_last_command,
                )
            )
        if self._command_history:
            commands.append(
                SystemCommand(
                    "Show command history",
                    "Review recent commands and replay one.",
                    self.action_show_command_history,
                )
            )
        if self._mode == "home" and self._home_action_ids:
            index = min(self._home_action_index, len(self._home_action_ids) - 1)
            action_id = self._home_action_ids[index]
            action = self._actions_by_id.get(action_id)
            if action is not None:
                label = action.user_label or action.label
                commands.append(
                    SystemCommand(
                        f"Run selected recommended action: {label}",
                        "Execute the selected action from Home recommendations.",
                        self.action_activate_selection,
                    )
                )
        if self._mode == "runs":
            commands.append(
                SystemCommand(
                    "Focus Runs Filter",
                    "Focus the runs filter input.",
                    self.action_focus_mode_filter,
                )
            )
            commands.append(
                SystemCommand(
                    "Focus Runs Status Filter",
                    "Focus the runs filter input.",
                    self.action_focus_mode_filter,
                )
            )
            commands.append(
                SystemCommand(
                    "Cycle Runs Status Filter",
                    "Cycle status filter between all/running/completed/failed.",
                    self.action_cycle_runs_status_filter,
                )
            )
            if self._run_status_filter:
                commands.append(
                    SystemCommand(
                        "Clear Runs Filter",
                        "Reset the runs filter query.",
                        self.action_clear_runs_filter,
                    )
                )
                commands.append(
                    SystemCommand(
                        "Clear Runs Status Filter",
                        "Reset the runs filter query.",
                        self.action_clear_runs_filter,
                    )
                )
        if self._mode == "runs":
            commands.append(
                SystemCommand(
                    "Cycle Runs sort order",
                    "Switch runs between newest-first and oldest-first order.",
                    self.action_toggle_runs_sort,
                )
            )
            commands.append(
                SystemCommand(
                    "Cycle run sort order",
                    "Switch runs between newest-first and oldest-first order.",
                    self.action_toggle_runs_sort,
                )
            )
            commands.append(
                SystemCommand(
                    "Toggle Runs Sort Order",
                    "Switch runs between newest-first and oldest-first order.",
                    self.action_toggle_runs_sort,
                )
            )
            commands.append(
                SystemCommand(
                    "Jump to run",
                    "Open a searchable list of runs and jump to a row.",
                    self.action_jump_to_item,
                )
            )
            commands.append(
                SystemCommand(
                    "Jump to next problem run",
                    "Jump to the next failed, error, timeout, or stopped run.",
                    self.action_jump_to_problem_run,
                )
            )
            commands.append(
                SystemCommand(
                    "Jump to previous problem run",
                    "Jump to the previous failed, error, timeout, or stopped run.",
                    self.action_jump_to_previous_problem_run,
                )
            )
        if self._mode == "files":
            commands.append(
                SystemCommand(
                    "Jump to file",
                    "Open a searchable list of visible files and jump to a row.",
                    self.action_jump_to_item,
                )
            )
            commands.append(
                SystemCommand(
                    "Focus Files Name Filter",
                    "Focus the files name filter input.",
                    self.action_focus_mode_filter,
                )
            )
            commands.append(
                SystemCommand(
                    "Cycle Files Source Filter",
                    "Switch between all, stage-only, and common-only file views.",
                    self.action_cycle_file_source_filter,
                )
            )
            commands.append(
                SystemCommand(
                    "Toggle Files Missing-only Filter",
                    "Switch between all files and only missing files.",
                    self.action_toggle_missing_only_filter,
                )
            )
            commands.append(
                SystemCommand(
                    "Cycle Files Source Scope",
                    "Switch between all, stage, and common artifacts.",
                    self.action_cycle_file_source_scope,
                )
            )
            commands.append(
                SystemCommand(
                    "Open selected file in editor",
                    "Open the selected artifact in your external editor.",
                    self.action_open_selected_in_editor,
                )
            )
            commands.append(
                SystemCommand(
                    "Focus next missing file",
                    "Jump to the next missing artifact in the file list.",
                    self.action_next_missing_artifact,
                )
            )
            if self._artifact_filter_query:
                commands.append(
                    SystemCommand(
                        "Clear Files Name Filter",
                        "Reset the file name filter query.",
                        self.action_clear_files_filter,
                    )
                )
        if self._mode == "console":
            commands.append(
                SystemCommand(
                    "Toggle Console Error-only Filter",
                    "Show only console entries with error-like markers.",
                    self.action_toggle_console_error_filter,
                )
            )
            commands.append(
                SystemCommand(
                    "Toggle Console Follow",
                    "Pause or resume auto-following incoming console output.",
                    self.action_toggle_console_follow,
                )
            )
            commands.append(
                SystemCommand(
                    "Focus Console Filter",
                    "Focus the console output text filter.",
                    self.action_focus_mode_filter,
                )
            )
            if self._console_filter_query:
                commands.append(
                    SystemCommand(
                        "Clear Console Filter",
                        "Clear the console output text filter.",
                        self.action_clear_console_filter,
                    )
                )
        if (
            self._running_intent is not None
            and self._running_intent.action_id == "run_loop"
        ):
            commands.append(
                SystemCommand(
                    "Stop active command",
                    "Request a graceful stop for the active command.",
                    self.action_stop_loop,
                )
            )
        if self._mode == "home":
            commands.append(
                SystemCommand(
                    "Focus Home Action Filter",
                    "Filter recommended actions by text.",
                    self.action_focus_home_action_filter,
                )
            )
            if self._home_action_filter_query:
                commands.append(
                    SystemCommand(
                        "Clear Home Action Filter",
                        "Reset the home action filter query.",
                        self._clear_home_action_filter,
                    )
                )
        return self._deduplicate_system_commands(commands)

    @staticmethod
    def _deduplicate_system_commands(
        commands: list[SystemCommand],
    ) -> list[SystemCommand]:
        deduped: list[SystemCommand] = []
        seen: set[tuple[str, str]] = set()
        for command in commands:
            key = (command.title.casefold(), command.description.casefold())
            if key in seen:
                continue
            deduped.append(command)
            seen.add(key)
        return deduped

    def action_show_home(self) -> None:
        self._switch_mode("home")

    def action_show_runs(self) -> None:
        self._switch_mode("runs")

    def action_show_files(self) -> None:
        self._switch_mode("files")

    def action_show_console(self) -> None:
        self._switch_mode("console")

    def action_show_help(self) -> None:
        self._switch_mode("help")

    def action_show_previous_view(self) -> None:
        self._cycle_mode(-1)

    def action_show_next_view(self) -> None:
        self._cycle_mode(1)

    def action_inspect_selection(self) -> None:
        self._start_ui_flow(label="inspect", flow_factory=self._open_selection_inspector)

    def action_toggle_safety_lock(self) -> None:
        self._start_ui_flow(
            label="toggle-safety", flow_factory=self._toggle_safety_lock
        )

    async def _toggle_safety_lock(self) -> None:
        if self._armed:
            self._armed = False
            self._update_ui_chrome()
            return
        unlocked = await self.push_screen_wait(UnlockSafetyScreen())
        if unlocked:
            self._armed = True
            self._update_ui_chrome()

    def action_toggle_advanced(self) -> None:
        self._show_advanced = not self._show_advanced
        if self._snapshot is not None:
            self._populate_home_view()
            self._populate_artifact_list()
        self._update_ui_chrome()

    def _selected_artifact(self) -> ArtifactItem | None:
        selected_path = self._selected_artifact_path()
        if selected_path is None:
            return None
        for item in self._current_artifacts:
            if item.path == selected_path:
                return item
        return None

    def _build_console_inspector_lines(self) -> tuple[str, ...]:
        if self._running_intent is not None:
            running_label = self._action_label(self._running_intent.action_id)
            start = self._timestamp()
            return (
                f"Running command: {running_label}",
                f"Started: {start}",
                f"Command: {shlex.join(self._running_intent.argv)}",
                f"Working directory: {self._running_intent.cwd}",
            )

        if self._active_history_item is not None:
            item = self._active_history_item
        elif self._command_history:
            item = self._command_history[0]
        else:
            return ("No command history is available in this session.",)

        status = "finished"
        if item.exit_code is None and item.finished_at is None:
            status = "running"
        elif item.exit_code is None:
            status = "pending"
        elif item.stopped:
            status = "stopped"
        elif item.exit_code == 0:
            status = "ok"
        else:
            status = "failed"
        duration = (
            f"{(item.finished_at - item.started_at):.1f}s"
            if item.finished_at is not None
            else "n/a"
        )
        label = self._action_label(item.intent.action_id)
        return (
            f"Last command: {label}",
            f"Status: {status}",
            f"Duration: {duration}",
            f"Exit code: {item.exit_code!s}",
            f"Command: {item.command}",
            f"Working directory: {item.intent.cwd}",
        )

    def _build_run_inspector_lines(self) -> tuple[str, ...]:
        run = self._selected_run()
        if run is None:
            return ("No run selected in this view.",)
        snapshot = self._snapshot
        summary = ("",)
        run_summary = [
            f"Run details for {run.run_id}",
            f"- Status: {run.status}",
            f"- Host mode: {run.host_mode}",
            f"- Sync status: {run.sync_status or '-'}",
            f"- Job id: {run.job_id or '-'}",
            f"- Started: {run.started_at or '-'}",
            f"- Completed: {run.completed_at or '-'}",
            f"- Manifest: {self._display_path(run.manifest_path)}",
            f"- Metrics: {self._display_path(run.metrics_path)}",
            f"- Manifest exists: {'yes' if run.manifest_path.exists() else 'no'}",
            f"- Metrics exists: {'yes' if run.metrics_path.exists() else 'no'}",
        ]
        if snapshot is not None:
            run_summary.append(f"- Total runs: {len(snapshot.runs)}")
            run_summary.append(f"- Visible runs: {len(self._visible_runs)}")
        return tuple(run_summary)

    def _build_file_inspector_lines(self) -> tuple[str, ...]:
        item = self._selected_artifact()
        if item is None:
            return ("No file selected in this view.",)
        return (
            f"File details for {self._display_path(item.path)}",
            f"- Source: {item.source}",
            f"- Exists: {'yes' if item.exists else 'no'}",
            f"- Relative path: {self._display_path(item.path)}",
            f"- Absolute path: {item.path}",
        )

    def _build_home_inspector_lines(self) -> tuple[str, ...]:
        if not self._home_action_ids:
            return ("No home actions are currently available.",)
        snapshot = self._snapshot
        action_id = self._home_action_ids[self._home_action_index]
        action = self._actions_by_id.get(action_id)
        if action is None:
            return (f"Action id: {action_id}",)
        reason = ""
        if snapshot is not None:
            for recommended in snapshot.recommended_actions:
                if recommended.action_id == action_id:
                    reason = recommended.reason
                    break
        return (
            f"Action: {action.user_label or action.label}",
            f"- Id: {action.action_id}",
            f"- Type: {action.kind}",
            f"- Risk: {action.risk_level}",
            f"- Requires arm: {'yes' if action.requires_arm else 'no'}",
            f"- Requires confirm: {'yes' if action.requires_confirmation else 'no'}",
            f"- Advanced: {'yes' if action.advanced else 'no'}",
            f"- Reason: {reason or 'custom action'}",
            f"- Help: {action.help_text or action.description}",
        )

    async def _open_selection_inspector(self) -> None:
        if self._mode == "home":
            lines = self._build_home_inspector_lines()
            title = "Home selection inspector"
        elif self._mode == "runs":
            lines = self._build_run_inspector_lines()
            title = "Run selection inspector"
        elif self._mode == "files":
            lines = self._build_file_inspector_lines()
            title = "File selection inspector"
        elif self._mode == "console":
            lines = self._build_console_inspector_lines()
            title = "Console selection inspector"
        else:
            self.notify("Selection inspector is unavailable in this view.")
            return

        await self.push_screen_wait(SelectionInspectorScreen(title=title, lines=lines))

    def action_toggle_prompt_view(self) -> None:
        snapshot = self._snapshot
        if snapshot is None or snapshot.render_preview.status != "ok":
            self.notify("Rendered prompt preview is not available.")
            return
        self._show_full_prompt = not self._show_full_prompt
        self._populate_home_view()
        self._update_ui_chrome()

    def action_toggle_auto_refresh(self) -> None:
        if self._running_intent is not None:
            self.notify("Auto-refresh cannot be changed while a command is running.")
            return
        self._auto_refresh_enabled = not self._auto_refresh_enabled
        if self._auto_refresh_enabled:
            self._append_console("auto-refresh enabled")
        else:
            self._append_console("auto-refresh disabled")
        self._update_ui_chrome()

    def action_toggle_run_sort(self) -> None:
        if self._snapshot is None:
            self.notify("Run sort is not available until snapshot loads.")
            return
        current_index = self._RUN_SORT_MODES.index(self._run_sort_mode)
        self._run_sort_mode = self._RUN_SORT_MODES[
            (current_index + 1) % len(self._RUN_SORT_MODES)
        ]
        selected = self._selected_run()
        selected_id = selected.run_id if selected is not None else None
        self._populate_run_list(preserve_selected_run_id=selected_id)
        self._append_console(f"run sort mode: {self._run_sort_label().lower()}")
        self._update_ui_chrome()

    def action_toggle_runs_sort(self) -> None:
        self.action_toggle_run_sort()

    def action_refresh_snapshot(self) -> None:
        if self._refresh_snapshot():
            self._append_console("snapshot refreshed")

    def action_clear_console(self) -> None:
        self._console_tail.clear()
        self._render_console_tail()

    def action_toggle_console_wrap(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self._console_wrap = not self._console_wrap
        self.query_one("#console-log", RichLog).wrap = self._console_wrap
        self._update_ui_chrome()

    def action_toggle_console_follow(self) -> None:
        self._console_follow = not self._console_follow
        if self._console_follow:
            self._append_console("console follow enabled")
            self._render_console_tail()
        else:
            self._console_tail.append(f"[{self._timestamp()}] console follow disabled")
        self._update_ui_chrome()

    def action_stop_loop(self) -> None:
        self._start_ui_flow(
            label="stop-command", flow_factory=self._stop_running_command
        )

    def action_cycle_file_source_scope(self) -> None:
        self.action_cycle_file_source_filter()

    def action_quick_open(self) -> None:
        if self._mode == "home":
            self._activate_list_selection("home-action-list")
            return
        if self._mode == "runs":
            self._start_ui_flow(
                label="quick-open:run-manifest",
                flow_factory=lambda: self._execute_action("open_selected_run_manifest"),
            )
            return
        if self._mode == "files":
            self._start_ui_flow(
                label="quick-open:file-viewer",
                flow_factory=lambda: self._execute_action("open_selected_artifact"),
            )
            return
        self.notify("Quick open is available in Home, Runs, and Files views.")

    def action_mode_v_shortcut(self) -> None:
        if self._mode == "runs":
            self.action_open_selected_run_manifest()
            return
        if self._mode == "files":
            self.action_cycle_file_source_filter()
            return
        self.action_quick_open()

    def action_open_selected_run_manifest(self) -> None:
        if self._mode != "runs":
            self.notify("Open run manifest is available in Runs view (2).")
            return
        self._start_ui_flow(
            label="runs-open-manifest",
            flow_factory=lambda: self._execute_action("open_selected_run_manifest"),
        )

    def action_next_missing_artifact(self) -> None:
        if self._mode != "files":
            self.notify("Next missing artifact is available in Files view (3).")
            return
        if not self._current_artifacts:
            self.notify("No files are currently listed.")
            return
        current_index = self._selected_artifact_index
        next_missing_index: int | None = None
        for offset in range(1, len(self._current_artifacts) + 1):
            candidate = (current_index + offset) % len(self._current_artifacts)
            if not self._current_artifacts[candidate].exists:
                next_missing_index = candidate
                break
        if next_missing_index is None:
            self.notify("No missing artifacts in this view.")
            return
        self._selected_artifact_index = next_missing_index
        artifact_list = self.query_one("#artifact-list", ListView)
        artifact_list.index = next_missing_index
        self._update_files_context()
        self._update_ui_chrome()
        artifact_list.focus()

    def action_quick_secondary(self) -> None:
        if self._mode == "home":
            self._start_ui_flow(
                label="quick-secondary:rendered-prompt",
                flow_factory=lambda: self._execute_action("open_rendered_prompt"),
            )
            return
        if self._mode == "runs":
            self._start_ui_flow(
                label="quick-secondary:run-metrics",
                flow_factory=lambda: self._execute_action("open_selected_run_metrics"),
            )
            return
        if self._mode == "files":
            self.action_toggle_missing_only_filter()
            return
        self.notify("Mode quick action is available in Home, Runs, and Files views.")

    def action_jump_to_item(self) -> None:
        if self._mode not in {"runs", "files"}:
            self.notify("Jump is available in Runs and Files views.")
            return
        self._start_ui_flow(
            label=f"jump:{self._mode}",
            flow_factory=self._jump_to_item,
        )

    async def _jump_to_item(self) -> None:
        if self._mode == "runs":
            if not self._visible_runs:
                self.notify("No runs available for jump.")
                return
            selected_run_id = await self.push_screen_wait(
                RunJumpScreen(runs=self._visible_runs)
            )
            if selected_run_id is None:
                return
            for index, run in enumerate(self._visible_runs):
                if run.run_id == selected_run_id:
                    self._selected_run_index = index
                    self._update_run_details()
                    self._update_ui_chrome()
                    self.query_one("#run-list", ListView).index = index
                    break
            else:
                self.notify("Selected run no longer available.")
            return

        if not self._current_artifacts:
            self.notify("No files available for jump.")
            return
        artifact_options = tuple(
            (artifact.path, self._display_path(artifact.path))
            for artifact in self._current_artifacts
        )
        selected_artifact = await self.push_screen_wait(
            ArtifactJumpScreen(artifact_paths=artifact_options)
        )
        if selected_artifact is None:
            return
        for index, artifact in enumerate(self._current_artifacts):
            if artifact.path == selected_artifact:
                self._selected_artifact_index = index
                self._update_files_context()
                self._update_ui_chrome()
                self.query_one("#artifact-list", ListView).index = index
                break

    def _next_problem_run_index(
        self,
        *,
        reverse: bool = False,
    ) -> int | None:
        if self._mode != "runs" or not self._visible_runs:
            return None
        run_count = len(self._visible_runs)
        step = -1 if reverse else 1
        for offset in range(1, run_count + 1):
            candidate = (self._selected_run_index + (step * offset)) % run_count
            if (
                str(self._visible_runs[candidate].status).strip().lower()
                in self._RUN_PROBLEM_STATUSES
            ):
                return candidate
        return None

    def action_jump_to_problem_run(self) -> None:
        if self._mode != "runs":
            self.notify("Next problem run is available in Runs view (2).")
            return
        if not self._visible_runs:
            self.notify("No runs available for jump.")
            return
        next_problem_index = self._next_problem_run_index(reverse=False)
        if next_problem_index is None:
            self.notify("No failing/problematic runs found.")
            return
        self._selected_run_index = next_problem_index
        self._update_run_details()
        run_list = self.query_one("#run-list", ListView)
        run_list.index = next_problem_index
        self._focus_mode_default()
        self._append_console(
            f"jumped to problem run: {self._visible_runs[next_problem_index].run_id}"
        )
        self._update_ui_chrome()

    def action_jump_to_previous_problem_run(self) -> None:
        if self._mode != "runs":
            self.notify("Previous problem run is available in Runs view (2).")
            return
        if not self._visible_runs:
            self.notify("No runs available for jump.")
            return
        previous_problem_index = self._next_problem_run_index(reverse=True)
        if previous_problem_index is None:
            self.notify("No failing/problematic runs found.")
            return
        self._selected_run_index = previous_problem_index
        self._update_run_details()
        run_list = self.query_one("#run-list", ListView)
        run_list.index = previous_problem_index
        self._focus_mode_default()
        self._append_console(
            "jumped to previous problem run: "
            f"{self._visible_runs[previous_problem_index].run_id}"
        )
        self._update_ui_chrome()

    def action_open_selected_in_editor(self) -> None:
        if self._mode != "files":
            self.notify("Open in editor is available in Files view (3).")
            return
        self._start_ui_flow(
            label="quick-open:file-editor",
            flow_factory=lambda: self._execute_action("open_selected_artifact_editor"),
        )

    def action_toggle_missing_only_filter(self) -> None:
        if self._mode != "files":
            self.notify("Missing-only filter is available in Files view (3).")
            return
        self._files_missing_only = not self._files_missing_only
        if self._snapshot is not None:
            self._populate_artifact_list()
        self._update_ui_chrome()

    def action_cycle_file_source_filter(self) -> None:
        if self._mode != "files":
            self.notify("File source filter is available in Files view (3).")
            return
        self._set_files_source_filter(self._next_files_source_filter())
        if self._snapshot is not None:
            self._populate_artifact_list()
        self._update_ui_chrome()

    def action_focus_files_filter(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if self._mode == "files":
            filter_input = self.query_one("#artifact-filter-input", Input)
        elif self._mode == "runs":
            filter_input = self.query_one("#run-filter-input", Input)
        else:
            self.notify("Filter input is available in Runs and Files views.")
            return
        filter_input.focus()
        filter_input.cursor_position = len(filter_input.value)

    def action_clear_runs_filter(self) -> None:
        if self._mode != "runs":
            self.notify("Run status filter is available in Runs view (2).")
            return
        if not self._run_status_filter:
            return
        self._run_status_filter = ""
        filter_input = self.query_one("#run-filter-input", Input)
        filter_input.value = ""
        if self._snapshot is not None:
            self._populate_run_list()
        self._update_ui_chrome()

    def action_cycle_runs_status_filter(self) -> None:
        if self._mode != "runs":
            self.notify("Run status filter is available in Runs view (2).")
            return
        current = self._run_status_filter.strip().lower() or "all"
        try:
            index = self._RUN_STATUS_FILTER_OPTIONS.index(current)
        except ValueError:
            index = 0
        next_value = self._RUN_STATUS_FILTER_OPTIONS[
            (index + 1) % len(self._RUN_STATUS_FILTER_OPTIONS)
        ]
        self._run_status_filter = "" if next_value == "all" else next_value
        filter_input = self.query_one("#run-filter-input", Input)
        filter_input.value = self._run_status_filter
        if self._snapshot is not None:
            self._populate_run_list()
        self._update_ui_chrome()

    def action_clear_files_filter(self) -> None:
        if not self._artifact_filter_query:
            return
        self._artifact_filter_query = ""
        filter_input = self.query_one("#artifact-filter-input", Input)
        filter_input.value = ""
        if self._snapshot is not None:
            self._populate_artifact_list()
        self._update_ui_chrome()

    def action_focus_mode_filter(self) -> None:
        if self._mode == "home":
            self.action_focus_home_action_filter()
            return
        if self._mode == "files":
            self.action_cycle_file_source_filter()
            self.action_focus_files_filter()
            return
        if self._mode == "runs":
            self.action_focus_files_filter()
            return
        if self._mode == "console":
            filter_input = self.query_one("#console-filter-input", Input)
            filter_input.focus()
            filter_input.cursor_position = len(filter_input.value)
            return
        self.action_focus_files_filter()

    def action_clear_console_filter(self) -> None:
        if not self._console_filter_query:
            return
        self._console_filter_query = ""
        filter_input = self.query_one("#console-filter-input", Input)
        filter_input.value = ""
        self._render_console_tail()
        self._update_ui_chrome()

    def _clear_home_action_filter(self) -> None:
        if not self._home_action_filter_query:
            return
        self._home_action_filter_query = ""
        if self._mode == "home":
            filter_input = self.query_one("#home-action-filter-input", Input)
            filter_input.value = ""
            if self._snapshot is not None:
                self._populate_home_view()
        self._update_ui_chrome()

    def action_toggle_console_error_filter(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self._console_show_errors_only = not self._console_show_errors_only
        state = "on" if self._console_show_errors_only else "off"
        self._append_console(f"console errors-only {state}")
        self._render_console_tail()
        self._update_ui_chrome()

    def action_focus_home_action_filter(self) -> None:
        if isinstance(self.screen, ModalScreen):
            return
        if self._mode != "home":
            self.notify("Home action filter is available in Home view (1).")
            return
        filter_input = self.query_one("#home-action-filter-input", Input)
        filter_input.focus()
        filter_input.cursor_position = len(filter_input.value)

    def action_list_first(self) -> None:
        if self._mode == "home":
            if not self._home_action_ids:
                return
            self._home_action_index = 0
            list_view = self.query_one("#home-action-list", ListView)
            self._apply_list_selection(list_id="home-action-list", selected_index=0)
            list_view.index = 0
            list_view.focus()
            return
        if self._mode == "runs":
            if not self._visible_runs:
                return
            self._selected_run_index = 0
            list_view = self.query_one("#run-list", ListView)
            self._apply_list_selection(list_id="run-list", selected_index=0)
            list_view.index = 0
            list_view.focus()
            return
        if self._mode == "files":
            if not self._current_artifacts:
                return
            self._selected_artifact_index = 0
            list_view = self.query_one("#artifact-list", ListView)
            self._apply_list_selection(list_id="artifact-list", selected_index=0)
            list_view.index = 0
            list_view.focus()

    def action_list_last(self) -> None:
        if self._mode == "home":
            if not self._home_action_ids:
                return
            index = len(self._home_action_ids) - 1
            list_view = self.query_one("#home-action-list", ListView)
            self._home_action_index = index
            self._apply_list_selection(list_id="home-action-list", selected_index=index)
            list_view.index = index
            list_view.focus()
            return
        if self._mode == "runs":
            if not self._visible_runs:
                return
            index = len(self._visible_runs) - 1
            list_view = self.query_one("#run-list", ListView)
            self._selected_run_index = index
            self._apply_list_selection(list_id="run-list", selected_index=index)
            list_view.index = index
            list_view.focus()
            return
        if self._mode == "files":
            if not self._current_artifacts:
                return
            index = len(self._current_artifacts) - 1
            list_view = self.query_one("#artifact-list", ListView)
            self._selected_artifact_index = index
            self._apply_list_selection(list_id="artifact-list", selected_index=index)
            list_view.index = index
            list_view.focus()

    def action_show_command_history(self) -> None:
        self._start_ui_flow(
            label="command-history",
            flow_factory=self._open_command_history,
        )

    def action_clear_command_history(self) -> None:
        self._start_ui_flow(
            label="clear-command-history",
            flow_factory=self._clear_command_history,
        )

    async def _clear_command_history(self) -> None:
        if not self._command_history:
            self.notify("Command history is already empty.")
            return
        if self._snapshot is not None:
            cwd = self._snapshot.repo_root
        else:
            cwd = self._state_path.parent
        confirmed = await self.push_screen_wait(
            ActionConfirmScreen(
                title="Clear command history?",
                summary=(
                    "This clears only the in-memory command history for this TUI session."
                ),
                command="command history clear",
                cwd=cwd,
                expected_writes=(),
                confirm_label="Clear",
            )
        )
        if not confirmed:
            return
        self._command_history.clear()
        self._append_console("command history cleared")
        self._update_ui_chrome()

    async def _open_command_history(self) -> None:
        if not self._command_history:
            self.notify("No command history available yet.")
            return
        selected = await self.push_screen_wait(
            CommandHistoryScreen(history=tuple(self._command_history))
        )
        if selected is None:
            return
        await self._replay_command_intent(
            intent=selected.intent,
            title=f"Re-run: {selected.intent.action_id}",
            confirm_label="Re-run",
        )

    async def _replay_command_intent(
        self,
        *,
        intent: CommandIntent,
        title: str,
        confirm_label: str,
    ) -> None:
        action = self._actions_by_id.get(intent.action_id)
        if action is None:
            self._start_command(intent)
            return
        if not await self._unlock_if_needed(action):
            return
        if await self._confirm_action_intent(
            action=action,
            title=title,
            intent=intent,
            confirm_label=confirm_label,
        ):
            self._start_command(intent)

    def action_rerun_last_command(self) -> None:
        self._start_ui_flow(label="rerun-last", flow_factory=self._rerun_last_command)

    async def _rerun_last_command(self) -> None:
        if self._last_command_intent is None:
            self.notify("No previous command to rerun.")
            return
        last_intent = self._last_command_intent
        action = self._actions_by_id.get(last_intent.action_id)
        action_name = (
            (action.user_label or action.label)
            if action is not None
            else last_intent.action_id
        )
        await self._replay_command_intent(
            intent=last_intent,
            title=f"Re-run: {action_name}",
            confirm_label="Re-run",
        )

    def action_activate_selection(self) -> None:
        focused = self.focused
        if isinstance(focused, ListView):
            list_id = focused.id or ""
            selected_index = focused.index
            if selected_index is not None and selected_index >= 0:
                self._apply_list_selection(
                    list_id=list_id, selected_index=selected_index
                )
            self._activate_list_selection(list_id)
            return
        if isinstance(focused, Button):
            self._handle_button_action(focused.id or "")
            return
        self._focus_mode_default()

    def _apply_list_selection(self, *, list_id: str, selected_index: int) -> None:
        if list_id == "home-action-list":
            self._home_action_index = selected_index
        elif list_id == "run-list":
            self._selected_run_index = selected_index
            self._update_run_details()
        elif list_id == "artifact-list":
            self._selected_artifact_index = selected_index
            self._update_files_context()
        self._update_ui_chrome()

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        index = event.list_view.index
        if index is None or index < 0:
            return
        self._apply_list_selection(
            list_id=event.list_view.id or "", selected_index=index
        )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is None or index < 0:
            index = 0
        list_id = event.list_view.id or ""
        self._apply_list_selection(list_id=list_id, selected_index=index)
        self._activate_list_selection(list_id)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self._handle_button_action(event.button.id or "")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "artifact-filter-input":
            self._artifact_filter_query = event.value.strip()
            if self._snapshot is not None:
                self._populate_artifact_list()
        elif event.input.id == "run-filter-input":
            self._run_status_filter = event.value.strip()
            if self._snapshot is not None:
                self._populate_run_list()
        elif event.input.id == "console-filter-input":
            self._console_filter_query = event.value.strip()
            self._render_console_tail()
        elif event.input.id == "home-action-filter-input":
            self._home_action_filter_query = event.value.strip()
            if self._snapshot is not None:
                self._populate_home_view()
        else:
            return
        self._update_ui_chrome()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "artifact-filter-input":
            self.query_one("#artifact-list", ListView).focus()
        elif event.input.id == "run-filter-input":
            self.query_one("#run-list", ListView).focus()
        elif event.input.id == "console-filter-input":
            self.query_one("#console-log", RichLog).focus()
        elif event.input.id == "home-action-filter-input":
            self.query_one("#home-action-list", ListView).focus()
        else:
            return

    def _handle_button_action(self, button_id: str) -> None:
        if button_id == "nav-home":
            self._switch_mode("home")
            return
        if button_id == "nav-runs":
            self._switch_mode("runs")
            return
        if button_id == "nav-files":
            self._switch_mode("files")
            return
        if button_id == "nav-console":
            self._switch_mode("console")
            return
        if button_id == "nav-help":
            self._switch_mode("help")
            return
        if button_id == "toggle-advanced":
            self.action_toggle_advanced()
            return
        if button_id == "artifact-filter-clear":
            self.action_clear_files_filter()
            self.action_focus_files_filter()
            return
        if button_id == "run-filter-clear":
            self.action_clear_runs_filter()
            return
        if button_id == "home-action-filter-clear":
            self._clear_home_action_filter()
            return
        if button_id == "run-filter-status":
            self.action_cycle_runs_status_filter()
            return
        if button_id == "run-sort-order":
            self.action_toggle_run_sort()
            return
        if button_id == "console-follow":
            self.action_toggle_console_follow()
            return
        if button_id == "console-filter-clear":
            self.action_clear_console_filter()
            return
        if button_id in {"file-cycle-source-scope", "file-source-filter"}:
            self.action_cycle_file_source_filter()
            return
        if button_id == "file-toggle-missing-filter":
            self.action_toggle_missing_only_filter()
            return
        if button_id == "home-render-toggle":
            self.action_toggle_prompt_view()
            return

        action_by_button = {
            "run-open-manifest": "open_selected_run_manifest",
            "run-open-metrics": "open_selected_run_metrics",
            "file-open-viewer": "open_selected_artifact",
            "file-open-editor": "open_selected_artifact_editor",
            "file-open-rendered": "open_rendered_prompt",
            "file-open-context": "open_render_context",
            "file-open-prompt": "open_stage_prompt",
            "file-open-state": "open_state_history",
            "file-run-loop": "run_loop",
            "file-lock-break": "lock_break",
            "file-focus-experiment": "focus_experiment",
            "file-experiment-create": "experiment_create",
            "file-experiment-move": "experiment_move",
        }
        action_id = action_by_button.get(button_id)
        if action_id:
            self._start_ui_flow(
                label=f"action:{action_id}",
                flow_factory=lambda: self._execute_action(action_id),
            )

    def _activate_list_selection(self, list_id: str) -> None:
        if list_id == "home-action-list":
            if not self._home_action_ids:
                return
            index = min(self._home_action_index, len(self._home_action_ids) - 1)
            action_id = self._home_action_ids[index]
            self._start_ui_flow(
                label=f"home-action:{action_id}",
                flow_factory=lambda: self._execute_action(action_id),
            )
            return

        if list_id == "run-list":
            self._start_ui_flow(
                label="runs-open-manifest",
                flow_factory=lambda: self._execute_action("open_selected_run_manifest"),
            )
            return

        if list_id == "artifact-list":
            self._start_ui_flow(
                label="files-open-viewer",
                flow_factory=lambda: self._execute_action("open_selected_artifact"),
            )

    async def _confirm_action_intent(
        self,
        *,
        action: ActionSpec,
        title: str,
        intent: CommandIntent,
        confirm_label: str = "Confirm",
    ) -> bool:
        if not action.requires_confirmation:
            return True
        command = shlex.join(intent.argv)
        summary = (
            f"Action: {action.user_label or action.label}\n"
            f"Risk: {action.risk_level}\n"
            f"Purpose: {action.help_text or action.description}"
        )
        confirmed = await self.push_screen_wait(
            ActionConfirmScreen(
                title=title,
                summary=summary,
                command=command,
                cwd=intent.cwd,
                expected_writes=intent.expected_writes,
                confirm_label=confirm_label,
            )
        )
        return bool(confirmed)

    async def _unlock_if_needed(self, action: ActionSpec) -> bool:
        if not action.requires_arm:
            return True
        if self._armed:
            return True
        unlocked = await self.push_screen_wait(UnlockSafetyScreen())
        if not unlocked:
            return False
        self._armed = True
        self._update_ui_chrome()
        return True

    async def _open_text_viewer(
        self,
        *,
        title: str,
        text: str,
        editor_path: Path | None = None,
        source_path: Path | None = None,
        render_hint: PreviewRenderHint = "auto",
    ) -> None:
        artifact_markdown = build_preview_markdown(
            text,
            source_path=source_path,
            hint=render_hint,
        )
        result = await self.push_screen_wait(
            ArtifactViewerScreen(
                title=title,
                artifact_markdown=artifact_markdown,
                editor_path=editor_path,
            )
        )
        if result != "open_editor" or editor_path is None:
            return
        snapshot = self._snapshot
        if snapshot is None:
            return
        action = self._actions_by_id["open_selected_artifact_editor"]
        intent = build_open_in_editor_intent(
            target_path=editor_path,
            cwd=snapshot.repo_root,
        )
        if await self._confirm_action_intent(
            action=action,
            title="Open file in external editor?",
            intent=intent,
            confirm_label="Open",
        ):
            self._start_command(intent)

    async def _open_artifact_viewer(self, artifact_path: Path) -> None:
        text, truncated = load_artifact_text(
            artifact_path, max_chars=self._artifact_preview_max_chars
        )
        if truncated:
            title = f"{self._display_path(artifact_path)} [truncated]"
        else:
            title = self._display_path(artifact_path)
        await self._open_text_viewer(
            title=title,
            text=text,
            editor_path=artifact_path,
            source_path=artifact_path,
            render_hint="auto",
        )

    async def _execute_action(self, action_id: str) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return

        action = self._actions_by_id.get(action_id)
        if action is None:
            self.notify(f"Unsupported action: {action_id}")
            return
        if action.advanced and not self._show_advanced:
            self.notify("Enable advanced actions first (x).")
            return
        if not await self._unlock_if_needed(action):
            return

        if action_id == "open_selected_artifact":
            artifact_path = self._selected_artifact_path()
            if artifact_path is None:
                self.notify("No file is available in this view yet.")
                return
            await self._open_artifact_viewer(artifact_path)
            return

        if action_id == "open_selected_artifact_editor":
            artifact_path = self._selected_artifact_path()
            if artifact_path is None:
                self.notify("No file is available in this view yet.")
                return
            intent = build_open_in_editor_intent(
                target_path=artifact_path,
                cwd=snapshot.repo_root,
            )
            if await self._confirm_action_intent(
                action=action,
                title="Open selected file in external editor?",
                intent=intent,
                confirm_label="Open",
            ):
                self._start_command(intent)
            return

        if action_id == "open_selected_run_manifest":
            run = self._selected_run()
            if run is None:
                self.notify("No run is available yet.")
                return
            await self._open_artifact_viewer(run.manifest_path)
            return

        if action_id == "open_selected_run_metrics":
            run = self._selected_run()
            if run is None:
                self.notify("No run is available yet.")
                return
            await self._open_artifact_viewer(run.metrics_path)
            return

        if action_id == "open_rendered_prompt":
            preview = snapshot.render_preview
            if preview.status != "ok" or not preview.prompt_text.strip():
                self.notify("Rendered prompt preview is not available.")
                return
            await self._open_text_viewer(
                title=f"Rendered Prompt ({preview.stage})",
                text=preview.prompt_text,
                editor_path=preview.template_path,
                source_path=preview.template_path,
                render_hint="markdown",
            )
            return

        if action_id == "open_render_context":
            preview = snapshot.render_preview
            if preview.status != "ok" or not preview.context_payload:
                self.notify("Render context is not available.")
                return
            context_text = json.dumps(preview.context_payload, indent=2, sort_keys=True)
            await self._open_text_viewer(
                title=f"Render Context ({preview.stage})",
                text=context_text,
                editor_path=None,
                source_path=None,
                render_hint="json",
            )
            return

        if action_id == "open_stage_prompt":
            prompt_path = resolve_stage_prompt_path(snapshot, snapshot.current_stage)
            if prompt_path is None:
                self.notify(f"No stage prompt found for '{snapshot.current_stage}'.")
                return
            await self._open_artifact_viewer(prompt_path)
            return

        if action_id == "open_state_history":
            await self._open_artifact_viewer(snapshot.state_path)
            return

        if action_id == "open_verification_result":
            verification_path = snapshot.autolab_dir / "verification_result.json"
            if not verification_path.exists():
                self.notify("Verification result file is not available yet.")
                return
            await self._open_artifact_viewer(verification_path)
            return

        intent: CommandIntent | None = None
        if action_id == "verify_current_stage":
            intent = build_verify_intent(
                state_path=snapshot.state_path,
                stage=snapshot.current_stage,
            )
        elif action_id == "run_once":
            options = await self.push_screen_wait(RunPresetScreen())
            if options is None:
                return
            intent = build_run_intent(state_path=snapshot.state_path, options=options)
        elif action_id == "resolve_human_review":
            if snapshot.current_stage != "human_review":
                self.notify(
                    "Human review can only be resolved in 'human_review' stage."
                )
                return
            selected_status = await self.push_screen_wait(HumanReviewDecisionScreen())
            if selected_status is None:
                return
            intent = build_human_review_intent(
                state_path=snapshot.state_path,
                status=selected_status,
            )
        elif action_id == "run_loop":
            options = await self.push_screen_wait(LoopPresetScreen())
            if options is None:
                return
            intent = build_loop_intent(state_path=snapshot.state_path, options=options)
        elif action_id == "todo_sync":
            intent = build_todo_sync_intent(state_path=snapshot.state_path)
        elif action_id == "lock_break":
            intent = build_lock_break_intent(
                state_path=snapshot.state_path,
                reason="tui manual break",
            )
        elif action_id == "focus_experiment":
            if not snapshot.backlog_experiments and snapshot.backlog_error:
                self.notify(f"{snapshot.backlog_error} Enter IDs manually to continue.")
            selected = await self.push_screen_wait(
                FocusExperimentScreen(
                    experiments=snapshot.backlog_experiments,
                    backlog_error=snapshot.backlog_error,
                )
            )
            if selected is None:
                return
            experiment_id, iteration_id = selected
            intent = build_focus_intent(
                state_path=snapshot.state_path,
                experiment_id=experiment_id,
                iteration_id=iteration_id,
            )
        elif action_id == "experiment_create":
            if not snapshot.backlog_hypotheses and snapshot.backlog_error:
                self.notify(snapshot.backlog_error)
                return
            selected = await self.push_screen_wait(
                ExperimentCreateScreen(
                    experiments=snapshot.backlog_experiments,
                    hypotheses=snapshot.backlog_hypotheses,
                )
            )
            if selected is None:
                return
            experiment_id, iteration_id, hypothesis_id = selected
            intent = build_experiment_create_intent(
                state_path=snapshot.state_path,
                experiment_id=experiment_id,
                iteration_id=iteration_id,
                hypothesis_id=hypothesis_id,
            )
        elif action_id == "experiment_move":
            if not snapshot.backlog_experiments and snapshot.backlog_error:
                self.notify(f"{snapshot.backlog_error} Enter IDs manually to continue.")
            selected = await self.push_screen_wait(
                ExperimentMoveScreen(
                    experiments=snapshot.backlog_experiments,
                    backlog_error=snapshot.backlog_error,
                )
            )
            if selected is None:
                return
            experiment_id, iteration_id, target_type = selected
            intent = build_experiment_move_intent(
                state_path=snapshot.state_path,
                to_type=target_type,
                experiment_id=experiment_id,
                iteration_id=iteration_id,
            )

        if intent is None:
            self.notify(f"Unsupported action: {action_id}")
            return

        if await self._confirm_action_intent(
            action=action,
            title=f"Confirm: {action.user_label or action.label}",
            intent=intent,
        ):
            self._start_command(intent)

    async def _show_command_history(self) -> None:
        should_clear = await self.push_screen_wait(
            CommandHistoryScreen(entries=tuple(self._command_history))
        )
        if should_clear:
            self._command_history.clear()
            self._append_console("command history cleared")

    def _start_command(self, intent: CommandIntent) -> None:
        if self._running_intent is not None:
            self.notify("A command is already running.")
            return
        if self._console_tail:
            self._append_console("-" * 40)
        command = shlex.join(intent.argv)
        started_at = time.monotonic()
        history_item = CommandHistoryItem(
            intent=intent,
            command=command,
            started_at=started_at,
            started_at_text=self._timestamp(),
        )
        self._command_history.appendleft(history_item)
        self._append_console(f"starting: {command}")
        self._last_command_label = self._action_label(intent.action_id)
        self._last_command_exit_code = None
        self._last_command_return_code = None
        self._last_command_duration = None
        self._running_intent = intent
        self._running_command_started_at = started_at
        self._active_history_item = history_item
        self._last_command_intent = intent
        self._update_ui_chrome()
        try:
            self._runner.start(intent)
        except Exception as exc:
            history_item.exit_code = 1
            history_item.finished_at = time.monotonic()
            self._append_console(f"failed to start command: {exc}")
            self._running_intent = None
            self._running_command_started_at = None
            self._active_history_item = None
            self._update_ui_chrome()
            return

    def _handle_runner_line(self, line: str) -> None:
        try:
            self.call_from_thread(self._append_console, line)
            self.call_from_thread(self._update_ui_chrome)
        except Exception:
            pass

    def _action_label(self, action_id: str) -> str:
        """Return a user-facing label for a command action id."""

        action = self._actions_by_id.get(action_id)
        if action is None:
            return action_id.replace("_", " ").title()
        return action.user_label or action.label

    def _handle_runner_done(self, return_code: int, stopped: bool) -> None:
        def _finish() -> None:
            intent = self._running_intent
            if self._active_history_item is None:
                history_item = None
                for item in self._command_history:
                    if (
                        item.intent == intent
                        and item.finished_at is None
                        and item.exit_code is None
                    ):
                        history_item = item
                        break
                self._active_history_item = history_item
            command = shlex.join(intent.argv) if intent is not None else "command"
            if stopped:
                self._append_console("process interrupted")
            self._append_console(f"command finished: {command} | exit={return_code}")
            if self._active_history_item is not None and intent is not None:
                self._active_history_item.finished_at = time.monotonic()
                self._active_history_item.exit_code = return_code
                self._active_history_item.stopped = stopped
            self._running_intent = None
            if self._running_command_started_at is not None:
                end_time = time.monotonic()
                self._last_command_duration = (
                    end_time - self._running_command_started_at
                )
            else:
                self._last_command_duration = None
            self._running_command_started_at = None
            self._last_command_exit_code = return_code
            self._last_command_return_code = return_code
            if self._last_command_label is None and intent is not None:
                self._last_command_label = self._action_label(intent.action_id)
            if intent is not None and intent.mutating:
                self._armed = False
            self._active_history_item = None
            self._update_ui_chrome()
            self._refresh_snapshot()
            self._running_command_started_at = None

        try:
            self.call_from_thread(_finish)
        except Exception:
            pass

    def _running_command_title(self) -> str:
        if self._running_intent is None:
            return "Running command"
        action = self._actions_by_id.get(self._running_intent.action_id)
        if action is not None:
            return action.user_label or action.label
        return self._running_intent.action_id or "running command"

    async def _stop_running_command(self) -> None:
        intent = self._running_intent
        if intent is None:
            self.notify("No command is running.")
            return

        command = shlex.join(intent.argv)
        label = self._running_command_title()
        confirmed = await self.push_screen_wait(
            ActionConfirmScreen(
                title=f"Stop active command: {label}?",
                summary="This requests a graceful interrupt for the active command.",
                command=command,
                cwd=intent.cwd,
                expected_writes=(),
                confirm_label="Stop command",
            )
        )
        if not confirmed:
            return

        if self._runner.stop():
            self._append_console(f"stop requested for active command: {command}")
        else:
            self._append_console("no running command process to stop")
