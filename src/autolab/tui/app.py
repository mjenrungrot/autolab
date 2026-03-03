from __future__ import annotations

import json
import time
import re
import shlex
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

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
    CommandIntent,
    LoopActionOptions,
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
            yield Label(self._title, id="artifact-path")
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
    _TONE_CLASSES = (
        "tone-success",
        "tone-info",
        "tone-warning",
        "tone-danger",
        "tone-muted",
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

    #status-advanced {
      width: 17;
    }

    #status-selection {
      width: 24;
    }

    #status-console {
      width: 16;
    }

    #status-snapshot {
      width: 18;
      content-align: right middle;
    }

    #status-running {
      width: 1fr;
      content-align: right middle;
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

    #run-details,
    #files-context,
    #home-stage-card,
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

    #files-filter-row {
      height: auto;
      margin-bottom: 1;
      align-vertical: middle;
    }

    #artifact-filter-label {
      width: 12;
      color: $text-muted;
    }

    #artifact-filter-input {
      width: 1fr;
    }

    #console-log {
      height: 1fr;
      border: round $surface;
    }
    """

    BINDINGS = [
        ("1", "show_home", "Home"),
        ("2", "show_runs", "Runs"),
        ("3", "show_files", "Files"),
        ("4", "show_console", "Console"),
        ("5", "show_help", "Help"),
        ("left_square_bracket", "show_previous_view", "Prev View"),
        ("right_square_bracket", "show_next_view", "Next View"),
        ("question_mark", "show_help", "Help"),
        ("ctrl+k", "command_palette", "Commands"),
        ("tab", "focus_next", "Next"),
        ("shift+tab", "focus_previous", "Prev"),
        ("enter", "activate_selection", "Activate"),
        ("slash", "focus_files_filter", "Filter Files"),
        ("o", "quick_open", "Open"),
        ("m", "quick_secondary", "Mode Quick"),
        ("e", "open_selected_in_editor", "Open Editor"),
        ("u", "toggle_safety_lock", "Unlock/Lock"),
        ("r", "refresh_snapshot", "Refresh"),
        ("p", "toggle_prompt_view", "Prompt View"),
        ("n", "next_missing_artifact", "Next missing file"),
        ("x", "toggle_advanced", "Advanced"),
        ("s", "stop_loop", "Stop Loop"),
        ("k", "stop_running_command", "Stop Command"),
        ("c", "clear_console", "Clear Console"),
        ("w", "toggle_console_wrap", "Wrap Console"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, *, state_path: Path, tail_lines: int = 2000) -> None:
        super().__init__()
        self._state_path = state_path.expanduser().resolve()
        self._tail_lines = max(200, int(tail_lines))
        self._console_tail: deque[str] = deque(maxlen=self._tail_lines)
        self._console_wrap = False
        self._armed = False
        self._last_snapshot_refreshed_at: float | None = None
        self._show_advanced = False
        self._show_full_prompt = False
        self._mode: ViewMode = "home"
        self._snapshot: CockpitSnapshot | None = None
        self._actions: tuple[ActionSpec, ...] = list_actions()
        self._actions_by_id: dict[str, ActionSpec] = {
            action.action_id: action for action in self._actions
        }

        self._home_action_ids: tuple[str, ...] = ()
        self._home_action_index = 0
        self._selected_run_index = 0
        self._selected_artifact_index = 0
        self._current_artifacts: tuple[ArtifactItem, ...] = ()
        self._all_artifacts: tuple[ArtifactItem, ...] = ()
        self._missing_artifacts_count = 0
        self._files_missing_only = False
        self._artifact_filter_query = ""

        self._runner = CommandRunner(
            on_line=self._handle_runner_line, on_done=self._handle_runner_done
        )
        self._running_intent: CommandIntent | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="status-rail"):
            yield Static("Locked: read-only.", id="status-safety")
            yield Static("Mode: home", id="status-mode")
            yield Static("Advanced: hidden", id="status-advanced")
            yield Static("Selection: -", id="status-selection")
            yield Static("Console wrap: off", id="status-console")
            yield Static("Snapshot: n/a", id="status-snapshot")
            yield Static("", id="status-running")
        yield Static(
            "Keys: 1-5 view | [/] cycle views | Enter activate | o open | m mode quick | ? help",
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
                yield Static("", id="home-verification-card", markup=False)
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
                yield Static("", id="home-artifacts-card", markup=False)
                yield Static("", id="home-todos-card", markup=False)
                yield Static("Recommended Actions", classes="section-title")
                yield ListView(id="home-action-list")
            with Vertical(id="runs-view", classes="view-panel"):
                yield Static("Runs", classes="view-title")
                yield ListView(id="run-list")
                yield Static("", id="run-details", markup=False)
                with Horizontal(id="run-buttons"):
                    yield Button("Open Manifest", id="run-open-manifest")
                    yield Button("Open Metrics", id="run-open-metrics")
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
        self._refresh_snapshot()
        self._update_help_text()
        self._update_ui_chrome()
        self._switch_mode("home")

    def _timestamp(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _append_console(self, text: str) -> None:
        line = f"[{self._timestamp()}] {text}"
        self._console_tail.append(line)
        log = self.query_one("#console-log", RichLog)
        log.write(line)

    def _render_console_tail(self) -> None:
        log = self.query_one("#console-log", RichLog)
        log.clear()
        for line in self._console_tail:
            log.write(line)

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

    def _key_hints_text(self) -> str:
        wrap_state = "on" if self._console_wrap else "off"
        parts = [
            "1-5 view",
            "[ / ] cycle",
            "Enter activate",
            "o open",
            "u lock",
            "x advanced",
            "p prompt",
            "ctrl+k commands",
            "r refresh",
            "? help",
            "q quit",
        ]
        if self._mode == "console":
            parts.append(f"w wrap({wrap_state})")
            parts.append("c clear")
        elif self._mode == "runs":
            parts.append("Enter manifest")
            parts.append("m metrics")
        elif self._mode == "files":
            parts.append("Enter viewer")
            parts.append("e editor")
            filter_state = "on" if self._files_missing_only else "off"
            parts.append(f"m missing-only({filter_state})")
            name_filter_state = "on" if self._artifact_filter_query else "off"
            parts.append(f"/ name-filter({name_filter_state})")
            parts.append("n next-missing")
        elif self._mode == "home":
            parts.append("Enter recommended action")
            parts.append("m rendered prompt")
        if self._running_intent is not None:
            parts.append("k stop")
            if self._running_intent.action_id == "run_loop":
                parts.append("s stop loop")
        return "Keys: " + " | ".join(parts)

    def _selection_status_label(self) -> str:
        if self._mode == "home":
            total = len(self._home_action_ids)
            index = self._home_action_index
            prefix = "Actions"
        elif self._mode == "runs":
            total = len(self._snapshot.runs) if self._snapshot is not None else 0
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

    def _update_ui_chrome(self) -> None:
        safety = self.query_one("#status-safety", Static)
        mode = self.query_one("#status-mode", Static)
        advanced = self.query_one("#status-advanced", Static)
        selection = self.query_one("#status-selection", Static)
        console = self.query_one("#status-console", Static)
        running = self.query_one("#status-running", Static)
        snapshot_status = self.query_one("#status-snapshot", Static)
        key_hints = self.query_one("#key-hints", Static)

        safety.update(
            "Unlocked: mutating enabled." if self._armed else "Locked: read-only."
        )
        self._set_tone(safety, "tone-warning" if self._armed else "tone-success")
        mode.update(f"Mode: {self._mode}")
        advanced.update(
            "Advanced: visible" if self._show_advanced else "Advanced: hidden"
        )
        self._set_tone(advanced, "tone-info" if self._show_advanced else "tone-muted")
        selection_label = self._selection_status_label()
        selection.update(selection_label)
        self._set_tone(
            selection,
            "tone-info"
            if "0/0" not in selection_label and "n/a" not in selection_label
            else "tone-muted",
        )
        console.update(f"Console wrap: {'on' if self._console_wrap else 'off'}")
        self._set_tone(console, "tone-info" if self._console_wrap else "tone-muted")
        key_hints.update(self._key_hints_text())

        if self._snapshot is None or self._last_snapshot_refreshed_at is None:
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
            if snapshot is None:
                running.update("Idle")
                self._set_tone(running, "tone-muted")
            else:
                stage_artifacts = snapshot.artifacts_by_stage.get(
                    snapshot.current_stage, ()
                )
                missing_required = sum(1 for item in stage_artifacts if not item.exists)
                blocker_count = (
                    0
                    if snapshot.primary_blocker == "none"
                    else len(snapshot.top_blockers)
                )
                running.update(
                    "Idle | "
                    f"runs:{len(snapshot.runs)} "
                    f"blockers:{blocker_count} "
                    f"todos:{len(snapshot.todos)} "
                    f"missing:{missing_required}"
                )
                if blocker_count or missing_required:
                    self._set_tone(running, "tone-warning")
                else:
                    self._set_tone(running, "tone-success")
        else:
            command = shlex.join(self._running_intent.argv)
            running.update(f"Running: {command[:72]}")
            self._set_tone(running, "tone-info")

        self.query_one(
            "#file-advanced-buttons", Horizontal
        ).display = self._show_advanced
        filter_button = self.query_one("#file-toggle-missing-filter", Button)
        filter_button.label = (
            "Filter: Missing Only" if self._files_missing_only else "Filter: All"
        )
        filter_button.variant = "primary" if self._files_missing_only else "default"
        clear_button = self.query_one("#artifact-filter-clear", Button)
        clear_button.disabled = not bool(self._artifact_filter_query)

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
        snapshot = self._snapshot
        if snapshot is None or not snapshot.runs:
            return None
        if self._selected_run_index >= len(snapshot.runs):
            self._selected_run_index = 0
        return snapshot.runs[self._selected_run_index]

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
        self._selected_artifact_index = 0
        self._current_artifacts = ()
        self._all_artifacts = ()
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

    def _refresh_snapshot(self) -> bool:
        try:
            self._snapshot = load_cockpit_snapshot(self._state_path)
        except Exception as exc:
            self._snapshot = None
            self._last_snapshot_refreshed_at = None
            self._armed = False
            self._clear_snapshot_views()
            self._update_ui_chrome()
            self._append_console(f"snapshot refresh failed: {exc}")
            self.notify(f"Snapshot refresh failed: {exc}")
            return False
        self._last_snapshot_refreshed_at = time.monotonic()

        self._populate_home_view()
        self._populate_run_list()
        self._populate_artifact_list()
        self._update_ui_chrome()
        return True

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
        for recommended in snapshot.recommended_actions:
            action = self._actions_by_id.get(recommended.action_id)
            if action is None:
                continue
            if action.advanced and not self._show_advanced:
                continue
            label = action.user_label or action.label
            item_label = Label(f"{label}: {recommended.reason}")
            item_label.add_class("tone-info")
            action_list.append(ListItem(item_label))
            action_ids.append(action.action_id)

        if not action_ids:
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
        self._home_action_index = min(
            self._home_action_index, len(self._home_action_ids) - 1
        )
        action_list.index = self._home_action_index

    def _populate_run_list(self) -> None:
        snapshot = self._snapshot
        run_list = self.query_one("#run-list", ListView)
        run_list.clear()
        if snapshot is None:
            return

        if not snapshot.runs:
            empty_label = Label("(No runs found yet)")
            empty_label.add_class("tone-muted")
            run_list.append(ListItem(empty_label))
            self._selected_run_index = 0
            details_widget = self.query_one("#run-details", Static)
            details_widget.update(
                "Run Details\nNo runs available yet.\nRun one transition to create run artifacts."
            )
            self._set_tone(details_widget, "tone-muted")
            return

        for run in snapshot.runs:
            started = run.started_at or "-"
            slurm_suffix = ""
            if run.host_mode == "slurm":
                slurm_suffix = f" job={run.job_id or '-'}"
            run_label = Label(
                f"{run.run_id} [{run.status}] ({run.host_mode}{slurm_suffix}) start={started}"
            )
            run_label.add_class(self._tone_for_run_status(run.status))
            run_list.append(ListItem(run_label))
        self._selected_run_index = min(self._selected_run_index, len(snapshot.runs) - 1)
        run_list.index = self._selected_run_index
        self._update_run_details()

    def _update_run_details(self) -> None:
        run = self._selected_run()
        snapshot = self._snapshot
        if run is None:
            details_widget = self.query_one("#run-details", Static)
            details_widget.update("Run Details\nNo run selected.")
            self._set_tone(details_widget, "tone-muted")
            return
        selected_index = self._selected_run_index + 1
        run_count = len(snapshot.runs) if snapshot is not None else selected_index
        details_widget = self.query_one("#run-details", Static)
        details_widget.update(
            "Run Details\n"
            f"- Selected: {selected_index}/{run_count}\n"
            f"- Run ID: {run.run_id}\n"
            f"- Status: {run.status}\n"
            f"- Host mode: {run.host_mode}\n"
            f"- SLURM Job ID: {run.job_id or '-'}\n"
            f"- Artifact sync: {run.sync_status or '-'}\n"
            f"- Started: {run.started_at or '-'}\n"
            f"- Completed: {run.completed_at or '-'}\n"
            f"- Manifest: {'OK' if run.manifest_path.exists() else 'MISS'}\n"
            f"- Metrics: {'OK' if run.metrics_path.exists() else 'MISS'}\n"
            "- Keys: Enter open manifest | Open Metrics button for metrics"
        )
        self._set_tone(details_widget, self._tone_for_run_status(run.status))

    def _populate_artifact_list(self) -> None:
        snapshot = self._snapshot
        artifact_list = self.query_one("#artifact-list", ListView)
        artifact_list.clear()
        self._current_artifacts = ()
        self._all_artifacts = ()
        self._missing_artifacts_count = 0

        if snapshot is None:
            return

        stage = snapshot.current_stage
        stage_artifacts = list(snapshot.artifacts_by_stage.get(stage, ()))
        seen: set[Path] = set()
        merged: list[ArtifactItem] = []
        for artifact in [*stage_artifacts, *snapshot.common_artifacts]:
            if artifact.path in seen:
                continue
            seen.add(artifact.path)
            merged.append(artifact)
        self._all_artifacts = tuple(merged)
        self._missing_artifacts_count = sum(
            1 for artifact in self._all_artifacts if not artifact.exists
        )
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
                if query in self._display_path(artifact.path).lower()
            )
        else:
            self._current_artifacts = visible_artifacts

        if not self._current_artifacts:
            if query:
                empty_text = (
                    f"(No files match name filter: {self._artifact_filter_query})"
                )
            else:
                empty_text = (
                    "(No missing files for this stage)"
                    if self._files_missing_only
                    else "(No relevant files)"
                )
            empty_label = Label(empty_text)
            empty_label.add_class("tone-muted")
            artifact_list.append(ListItem(empty_label))
            self._selected_artifact_index = 0
            context_widget = self.query_one("#files-context", Static)
            context_widget.update(
                "Files\n"
                f"- Stage: {stage}\n"
                f"- Filter: {'missing only' if self._files_missing_only else 'all files'}\n"
                f"- Name filter: {self._artifact_filter_query or 'none'}\n"
                f"- {empty_text}"
            )
            self._set_tone(context_widget, "tone-muted")
            return

        for artifact in self._current_artifacts:
            marker = "OK" if artifact.exists else "MISS"
            entry = Label(f"[{marker}] {self._display_path(artifact.path)}")
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
            f"- Item: {selected_index}/{visible_count}\n"
            f"- Selected: {selected_text}\n"
            f"- Filter: {'missing only' if self._files_missing_only else 'all files'}\n"
            f"- Name filter: {self._artifact_filter_query or 'none'}\n"
            f"- Missing files: {missing_count}\n"
            f"- Showing: {visible_count}/{total_count} (missing: {missing_count})\n"
            "- View-only: Viewer, Editor, Rendered, Context, Template, State\n"
            "- Mutating: Loop, Lock Break, Focus, Experiment Create/Move\n"
            "  (unlock + confirm required)\n"
            "- Keys: Enter open viewer | / focus name filter"
        )
        self._set_tone(context_widget, "tone-info")

    def _update_help_text(self) -> None:
        help_widget = self.query_one("#help-text", Static)
        help_widget.update(
            "Autolab TUI\n"
            "\n"
            "Keyboard\n"
            "- Global: 1-5 switch views, Tab/Shift+Tab move focus, Enter activate.\n"
            "- Safety: u unlock/lock, x toggle advanced, q quit.\n"
            "- Utilities: r refresh, k stop active command, s stop active loop, c clear console.\n"
            "- Home: p toggle prompt excerpt/full.\n"
            "- Modals: Esc closes or cancels.\n"
            "\n"
            "Views\n"
            "- Home: stage status, rendered prompt preview, recommended actions.\n"
            "- Home: open tasks card highlights active todo priorities.\n"
            "- Runs: run manifest and metrics overview.\n"
            "- Files: artifacts plus rendered prompt/context/template quick-open.\n"
            "- Files advanced: focus experiment, create experiment, move experiment.\n"
            "- Console: live command output.\n"
            "\n"
            "Keys\n"
            "- 1-5: jump directly to Home/Runs/Files/Console/Help.\n"
            "- [ and ]: cycle views.\n"
            "- Enter: activate selected list item.\n"
            "- w: toggle console line wrapping.\n"
            "Quick Actions\n"
            "- o: Open selected item in current view (action/manifest/viewer).\n"
            "- m: Mode quick action (home rendered prompt, runs metrics, files filter).\n"
            "- e: Open selected file in editor (Files view).\n"
            "- n: Jump to next missing artifact (Files view).\n"
            "- /: Focus files name filter (Files view).\n"
            "- Ctrl+k: Open command palette.\n"
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
            "- Red: blocking/error"
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
                    "Toggle safety lock",
                    "Lock or unlock mutating actions.",
                    self.action_toggle_safety_lock,
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
        if self._mode == "files":
            commands.append(
                SystemCommand(
                    "Focus Files Name Filter",
                    "Focus the files name filter input.",
                    self.action_focus_files_filter,
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
                    "Open selected file in editor",
                    "Open the selected artifact in your external editor.",
                    self.action_open_selected_in_editor,
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
        if self._running_intent is not None:
            commands.append(
                SystemCommand(
                    "Stop active command",
                    f"Stop the active command: {self._running_intent.action_id}",
                    self.action_stop_running_command,
                )
            )
            if self._running_intent.action_id == "run_loop":
                commands.append(
                    SystemCommand(
                        "Stop active loop",
                        "Request a graceful stop for the active loop command.",
                        self.action_stop_loop,
                    )
                )
        return commands

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

    def action_toggle_prompt_view(self) -> None:
        snapshot = self._snapshot
        if snapshot is None or snapshot.render_preview.status != "ok":
            self.notify("Rendered prompt preview is not available.")
            return
        self._show_full_prompt = not self._show_full_prompt
        self._populate_home_view()
        self._update_ui_chrome()

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

    def action_stop_loop(self) -> None:
        self._start_ui_flow(
            label="stop-loop",
            flow_factory=lambda: self._stop_running_command(for_loop=True),
        )

    def action_stop_running_command(self) -> None:
        self._start_ui_flow(
            label="stop-command", flow_factory=lambda: self._stop_running_command()
        )

    def action_next_missing_artifact(self) -> None:
        if self._mode != "files":
            self.notify("Next missing artifact is available in Files view (3).")
            return
        if not self._current_artifacts:
            self.notify("No artifacts are currently visible.")
            return

        if self._running_intent is not None:
            self.notify("Navigation is unavailable while a command is running.")
            return

        missing_indices: list[int] = [
            index
            for index, artifact in enumerate(self._current_artifacts)
            if not artifact.exists
        ]
        if not missing_indices:
            self.notify("No missing artifacts in current file list.")
            return

        start = self._selected_artifact_index + 1
        next_missing = next(
            (index for index in missing_indices if index >= start),
            missing_indices[0],
        )
        self._selected_artifact_index = next_missing
        artifact_list = self.query_one("#artifact-list", ListView)
        artifact_list.index = next_missing
        self._update_files_context()
        self._update_ui_chrome()

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

    def action_focus_files_filter(self) -> None:
        if self._mode != "files":
            self.notify("Files name filter is available in Files view (3).")
            return
        if isinstance(self.screen, ModalScreen):
            return
        filter_input = self.query_one("#artifact-filter-input", Input)
        filter_input.focus()
        filter_input.cursor_position = len(filter_input.value)

    def action_clear_files_filter(self) -> None:
        if not self._artifact_filter_query:
            return
        self._artifact_filter_query = ""
        filter_input = self.query_one("#artifact-filter-input", Input)
        filter_input.value = ""
        if self._snapshot is not None:
            self._populate_artifact_list()
        self._update_ui_chrome()

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
        if event.input.id != "artifact-filter-input":
            return
        self._artifact_filter_query = event.value.strip()
        if self._snapshot is not None:
            self._populate_artifact_list()
        self._update_ui_chrome()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "artifact-filter-input":
            return
        self.query_one("#artifact-list", ListView).focus()

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
        if button_id == "file-toggle-missing-filter":
            self.action_toggle_missing_only_filter()
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
        text, _truncated = load_artifact_text(artifact_path, max_chars=None)
        await self._open_text_viewer(
            title=self._display_path(artifact_path),
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

    def _start_command(self, intent: CommandIntent) -> None:
        if self._running_intent is not None:
            self.notify("A command is already running.")
            return
        if self._console_tail:
            self._append_console("-" * 40)
        command = shlex.join(intent.argv)
        self._append_console(f"starting: {command}")
        self._running_intent = intent
        self._update_ui_chrome()
        try:
            self._runner.start(intent)
        except Exception as exc:
            self._append_console(f"failed to start command: {exc}")
            self._running_intent = None
            self._update_ui_chrome()

    def _handle_runner_line(self, line: str) -> None:
        try:
            self.call_from_thread(self._append_console, line)
        except Exception:
            pass

    def _handle_runner_done(self, return_code: int, stopped: bool) -> None:
        def _finish() -> None:
            intent = self._running_intent
            if stopped:
                self._append_console("process interrupted")
            self._append_console(f"process exit code: {return_code}")
            self._running_intent = None
            if intent is not None and intent.mutating:
                self._armed = False
            self._update_ui_chrome()
            self._refresh_snapshot()

        try:
            self.call_from_thread(_finish)
        except Exception:
            pass

    async def _stop_running_command(self, *, for_loop: bool = False) -> None:
        intent = self._running_intent
        if intent is None:
            self.notify("No command is running.")
            return
        if for_loop and intent.action_id != "run_loop":
            self.notify("No loop command is running.")
            return

        is_loop = intent.action_id == "run_loop"
        target_command = shlex.join(intent.argv)
        stop_intent = CommandIntent(
            action_id="stop_loop",
            argv=("autolab", "tui", "--stop-loop"),
            cwd=intent.cwd,
            expected_writes=(),
            mutating=False,
        )
        stop_command = target_command if target_command else shlex.join(stop_intent.argv)
        confirmed = await self.push_screen_wait(
            ActionConfirmScreen(
                title="Stop active loop?" if is_loop else "Stop active command?",
                summary=(
                    "This requests a graceful stop for the active process."
                    if is_loop
                    else f"Stop command: {target_command}"
                ),
                command=stop_command,
                cwd=stop_intent.cwd,
                expected_writes=stop_intent.expected_writes,
                confirm_label="Stop loop" if is_loop else "Stop",
            )
        )
        if not confirmed:
            return

        if self._runner.stop():
            if is_loop:
                self._append_console("stop requested for active loop process")
            else:
                self._append_console(
                    f"stop requested for active command: {target_command}"
                )
        else:
            self._append_console("no running process to stop")
