from __future__ import annotations

import shlex
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    Footer,
    Header,
    Input,
    Label,
    ListItem,
    ListView,
    RichLog,
    Static,
)

from autolab.tui.actions import (
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
    CockpitSnapshot,
    CommandIntent,
    LoopActionOptions,
    RunActionOptions,
)
from autolab.tui.runner import CommandRunner
from autolab.tui.snapshot import (
    load_artifact_text,
    load_cockpit_snapshot,
    resolve_stage_prompt_path,
)


class ConfirmationScreen(ModalScreen[bool]):
    CSS = """
    ConfirmationScreen {
      align: center middle;
    }

    #confirm-dialog {
      width: 100;
      max-width: 96%;
      height: auto;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }

    #confirm-title {
      text-style: bold;
      margin-bottom: 1;
    }

    #confirm-buttons {
      margin-top: 1;
      height: auto;
      align-horizontal: right;
    }
    """

    def __init__(
        self,
        *,
        title: str,
        command: str,
        cwd: Path,
        expected_writes: tuple[str, ...],
        confirm_label: str = "Confirm",
    ) -> None:
        super().__init__()
        self._title = title
        self._command = command
        self._cwd = cwd
        self._expected_writes = expected_writes
        self._confirm_label = confirm_label

    def compose(self) -> ComposeResult:
        writes = "\n".join(f"- {entry}" for entry in self._expected_writes)
        if not writes:
            writes = "- (none)"
        body = (
            f"Command:\n{self._command}\n\n"
            f"cwd:\n{self._cwd}\n\n"
            f"Expected writes (best-effort):\n{writes}"
        )
        with Vertical(id="confirm-dialog"):
            yield Label(self._title, id="confirm-title")
            with VerticalScroll():
                yield Static(body, markup=False)
            with Horizontal(id="confirm-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self._confirm_label, id="confirm", variant="error")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "confirm")


class RunOptionsScreen(ModalScreen[RunActionOptions | None]):
    CSS = """
    RunOptionsScreen {
      align: center middle;
    }

    #run-options-dialog {
      width: 72;
      max-width: 96%;
      height: auto;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="run-options-dialog"):
            yield Label("Run one transition options", id="run-options-title")
            yield Checkbox("Enable --verify", value=True, id="run-verify")
            yield Checkbox(
                "Enable --auto-decision", value=False, id="run-auto-decision"
            )
            yield Checkbox("Force --run-agent", value=False, id="run-agent-on")
            yield Checkbox("Force --no-run-agent", value=False, id="run-agent-off")
            with Horizontal(id="run-options-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="continue", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id != "continue":
            return
        force_on = self.query_one("#run-agent-on", Checkbox).value
        force_off = self.query_one("#run-agent-off", Checkbox).value
        if force_on and force_off:
            self.notify("Choose only one run-agent override.")
            return
        run_agent_mode = "policy"
        if force_on:
            run_agent_mode = "force_on"
        elif force_off:
            run_agent_mode = "force_off"
        self.dismiss(
            RunActionOptions(
                verify=self.query_one("#run-verify", Checkbox).value,
                run_agent_mode=run_agent_mode,
                auto_decision=self.query_one("#run-auto-decision", Checkbox).value,
            )
        )


class LoopOptionsScreen(ModalScreen[LoopActionOptions | None]):
    CSS = """
    LoopOptionsScreen {
      align: center middle;
    }

    #loop-options-dialog {
      width: 72;
      max-width: 96%;
      height: auto;
      border: round $accent;
      background: $panel;
      padding: 1 2;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="loop-options-dialog"):
            yield Label("Loop options", id="loop-options-title")
            yield Input(
                value="3", placeholder="max iterations", id="loop-max-iterations"
            )
            yield Input(value="2", placeholder="max hours", id="loop-max-hours")
            yield Checkbox("Enable --auto", value=True, id="loop-auto")
            yield Checkbox("Enable --verify", value=True, id="loop-verify")
            yield Checkbox("Force --run-agent", value=False, id="loop-agent-on")
            yield Checkbox("Force --no-run-agent", value=False, id="loop-agent-off")
            with Horizontal(id="loop-options-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Continue", id="continue", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#cancel", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id != "continue":
            return
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
        force_on = self.query_one("#loop-agent-on", Checkbox).value
        force_off = self.query_one("#loop-agent-off", Checkbox).value
        if force_on and force_off:
            self.notify("Choose only one run-agent override.")
            return
        run_agent_mode = "policy"
        if force_on:
            run_agent_mode = "force_on"
        elif force_off:
            run_agent_mode = "force_off"
        self.dismiss(
            LoopActionOptions(
                max_iterations=max_iterations,
                max_hours=max_hours,
                auto=self.query_one("#loop-auto", Checkbox).value,
                verify=self.query_one("#loop-verify", Checkbox).value,
                run_agent_mode=run_agent_mode,
            )
        )


class ArtifactViewerScreen(ModalScreen[str | None]):
    CSS = """
    ArtifactViewerScreen {
      align: center middle;
    }

    #artifact-dialog {
      width: 160;
      max-width: 98%;
      height: 90%;
      border: round $accent;
      background: $panel;
      padding: 1;
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
      margin-top: 1;
      align-horizontal: right;
    }
    """

    def __init__(self, *, artifact_path: Path, artifact_text: str) -> None:
        super().__init__()
        self._artifact_path = artifact_path
        self._artifact_text = artifact_text

    def compose(self) -> ComposeResult:
        with Vertical(id="artifact-dialog"):
            yield Label(str(self._artifact_path), id="artifact-path")
            with VerticalScroll(id="artifact-scroll"):
                yield Static(self._artifact_text, markup=False)
            with Horizontal(id="artifact-buttons"):
                yield Button("Close", id="close")
                yield Button("Open in $EDITOR", id="open-editor", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#close", Button).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open-editor":
            self.dismiss("open_editor")
            return
        self.dismiss(None)


class AutolabCockpitApp(App[None]):
    CSS = """
    Screen {
      layout: vertical;
    }

    #safety-row {
      height: auto;
      margin: 0 1;
      padding: 0 0 1 0;
    }

    #safety-status {
      width: 1fr;
      content-align: left middle;
      padding: 0 1;
    }

    #running-banner {
      width: 36;
      content-align: right middle;
    }

    #top-row {
      height: 1fr;
      margin: 0 1;
    }

    #nav-pane, #details-pane, #actions-pane {
      border: round $accent;
      padding: 0 1;
      height: 1fr;
    }

    #nav-pane {
      width: 33%;
    }

    #details-pane {
      width: 39%;
      margin: 0 1;
    }

    #actions-pane {
      width: 28%;
    }

    .pane-title {
      text-style: bold;
      margin-bottom: 1;
    }

    .section-title {
      text-style: bold;
      margin-top: 1;
      margin-bottom: 0;
    }

    #stage-list, #run-list, #todo-list, #artifact-list, #action-list {
      border: round $surface;
      height: 1fr;
    }

    #run-list, #todo-list {
      min-height: 7;
    }

    #action-list {
      min-height: 14;
      margin-bottom: 1;
    }

    #run-action {
      margin-bottom: 1;
    }

    #console-pane {
      height: 14;
      margin: 1;
      border: round $accent;
      padding: 0 1;
    }

    #console-header {
      height: auto;
      margin-top: 0;
    }

    #console-log {
      height: 1fr;
      border: round $surface;
    }
    """

    BINDINGS = [("q", "quit", "Quit")]

    def __init__(self, *, state_path: Path, tail_lines: int = 2000) -> None:
        super().__init__()
        self._state_path = state_path.expanduser().resolve()
        self._tail_lines = max(200, int(tail_lines))
        self._console_tail: deque[str] = deque(maxlen=self._tail_lines)
        self._armed = False
        self._snapshot: CockpitSnapshot | None = None
        self._actions: tuple[ActionSpec, ...] = list_actions()
        self._selected_stage_index = 0
        self._selected_stage_key: str | None = None
        self._selected_run_index = 0
        self._selected_todo_index = 0
        self._selected_artifact_index = 0
        self._selected_action_index = 0
        self._current_artifacts: tuple[ArtifactItem, ...] = ()
        self._runner = CommandRunner(
            on_line=self._handle_runner_line, on_done=self._handle_runner_done
        )
        self._running_intent: CommandIntent | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="safety-row"):
            yield Button("Arm actions: OFF", id="arm-toggle", variant="warning")
            yield Static("Disarmed (read-only mode).", id="safety-status")
            yield Static("", id="running-banner")
        with Horizontal(id="top-row"):
            with Vertical(id="nav-pane"):
                yield Static("Navigator", classes="pane-title")
                yield Static("Pipeline stages", classes="section-title")
                yield ListView(id="stage-list")
                yield Static("Runs", classes="section-title")
                yield ListView(id="run-list")
                yield Static("Todo", classes="section-title")
                yield ListView(id="todo-list")
            with Vertical(id="details-pane"):
                yield Static("Details", classes="pane-title")
                yield Static("", id="stage-summary", markup=False)
                yield Static("", id="required-artifacts", markup=False)
                yield Static("", id="verification-summary", markup=False)
                yield Static("", id="blockers-summary", markup=False)
                yield Static("Relevant files", classes="section-title")
                yield ListView(id="artifact-list")
            with Vertical(id="actions-pane"):
                yield Static("Actions", classes="pane-title")
                yield ListView(id="action-list")
                yield Button("Run selected action", id="run-action", variant="primary")
                yield Button(
                    "Stop loop", id="stop-loop", variant="error", disabled=True
                )
                yield Button("Refresh snapshot", id="refresh-snapshot")
        with Vertical(id="console-pane"):
            with Horizontal(id="console-header"):
                yield Static("Console", classes="pane-title")
                yield Button("Clear", id="clear-console")
            yield RichLog(id="console-log", markup=False, wrap=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_snapshot()
        self._populate_action_list()
        self._update_safety_row()
        self._update_action_button_state()

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

    def _clear_list_view(self, list_view: ListView) -> None:
        list_view.clear()

    def _display_path(self, path: Path) -> str:
        snapshot = self._snapshot
        if snapshot is None:
            return str(path)
        try:
            return str(path.relative_to(snapshot.repo_root))
        except ValueError:
            return str(path)

    def _selected_stage_name(self) -> str:
        snapshot = self._snapshot
        if snapshot is None or not snapshot.stage_items:
            return ""
        if self._selected_stage_key:
            for index, item in enumerate(snapshot.stage_items):
                if item.name != self._selected_stage_key:
                    continue
                self._selected_stage_index = index
                return item.name
        if self._selected_stage_index >= len(snapshot.stage_items):
            self._selected_stage_index = 0
        selected = snapshot.stage_items[self._selected_stage_index].name
        self._selected_stage_key = selected
        return selected

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

    def _selected_action(self) -> ActionSpec | None:
        if not self._actions:
            return None
        if self._selected_action_index >= len(self._actions):
            self._selected_action_index = 0
        return self._actions[self._selected_action_index]

    def _resolve_stage_selection_index(
        self, *, snapshot: CockpitSnapshot, current_index: int
    ) -> int:
        if not snapshot.stage_items:
            return 0
        if self._selected_stage_key:
            for index, stage_item in enumerate(snapshot.stage_items):
                if stage_item.name == self._selected_stage_key:
                    return index
        if 0 <= self._selected_stage_index < len(snapshot.stage_items):
            return self._selected_stage_index
        return current_index

    def _clear_snapshot_views(self) -> None:
        self._selected_stage_index = 0
        self._selected_stage_key = None
        self._selected_run_index = 0
        self._selected_todo_index = 0
        self._selected_artifact_index = 0
        self._current_artifacts = ()

        stage_list = self.query_one("#stage-list", ListView)
        run_list = self.query_one("#run-list", ListView)
        todo_list = self.query_one("#todo-list", ListView)
        artifact_list = self.query_one("#artifact-list", ListView)
        for widget in (stage_list, run_list, todo_list, artifact_list):
            self._clear_list_view(widget)
        stage_list.append(ListItem(Label("(snapshot unavailable)")))
        run_list.append(ListItem(Label("(snapshot unavailable)")))
        todo_list.append(ListItem(Label("(snapshot unavailable)")))
        artifact_list.append(ListItem(Label("(snapshot unavailable)")))

        self.query_one("#stage-summary", Static).update("Current stage: unavailable")
        self.query_one("#required-artifacts", Static).update(
            "Required artifacts:\n- unavailable (snapshot refresh failed)"
        )
        self.query_one("#verification-summary", Static).update(
            "Last verification: unavailable"
        )
        self.query_one("#blockers-summary", Static).update(
            "Top blockers:\n- snapshot refresh failed"
        )

    def _refresh_snapshot(self) -> bool:
        try:
            self._snapshot = load_cockpit_snapshot(self._state_path)
        except Exception as exc:
            self._snapshot = None
            self._armed = False
            self._clear_snapshot_views()
            self._update_safety_row()
            self._update_action_button_state()
            self._append_console(f"snapshot refresh failed: {exc}")
            self.notify(f"Snapshot refresh failed: {exc}")
            return False
        self._populate_stage_list()
        self._populate_run_list()
        self._populate_todo_list()
        self._update_details()
        self._update_action_button_state()
        return True

    def _populate_stage_list(self) -> None:
        snapshot = self._snapshot
        stage_list = self.query_one("#stage-list", ListView)
        self._clear_list_view(stage_list)
        if snapshot is None:
            return
        current_index = 0
        for index, item in enumerate(snapshot.stage_items):
            status_icon = {
                "complete": "✓",
                "current": "▶",
                "blocked": "⚠",
                "upcoming": "·",
            }.get(item.status, "·")
            label = (
                f"{status_icon} {item.name} [{item.status}] attempts {item.attempts}"
            )
            stage_list.append(ListItem(Label(label)))
            if item.is_current:
                current_index = index
        if snapshot.stage_items:
            self._selected_stage_index = self._resolve_stage_selection_index(
                snapshot=snapshot,
                current_index=current_index,
            )
            self._selected_stage_index = min(
                max(self._selected_stage_index, 0),
                len(snapshot.stage_items) - 1,
            )
            self._selected_stage_key = snapshot.stage_items[
                self._selected_stage_index
            ].name
            stage_list.index = self._selected_stage_index

    def _populate_run_list(self) -> None:
        snapshot = self._snapshot
        run_list = self.query_one("#run-list", ListView)
        self._clear_list_view(run_list)
        if snapshot is None:
            return
        if not snapshot.runs:
            run_list.append(ListItem(Label("(no runs found)")))
            self._selected_run_index = 0
            return
        for run in snapshot.runs:
            started = run.started_at or "-"
            label = f"{run.run_id} [{run.status}] start={started}"
            run_list.append(ListItem(Label(label)))
        self._selected_run_index = min(self._selected_run_index, len(snapshot.runs) - 1)
        run_list.index = self._selected_run_index

    def _populate_todo_list(self) -> None:
        snapshot = self._snapshot
        todo_list = self.query_one("#todo-list", ListView)
        self._clear_list_view(todo_list)
        if snapshot is None:
            return
        if not snapshot.todos:
            todo_list.append(ListItem(Label("(no open todo tasks)")))
            self._selected_todo_index = 0
            return
        for todo in snapshot.todos:
            task_text = todo.text.replace("\n", " ").strip()
            if len(task_text) > 56:
                task_text = f"{task_text[:56]}…"
            prefix = todo.priority.lower() if todo.priority else "normal"
            label = f"{todo.task_id} [{prefix}] {task_text}"
            todo_list.append(ListItem(Label(label)))
        self._selected_todo_index = min(
            self._selected_todo_index, len(snapshot.todos) - 1
        )
        todo_list.index = self._selected_todo_index

    def _populate_artifact_list(self) -> None:
        artifact_list = self.query_one("#artifact-list", ListView)
        self._clear_list_view(artifact_list)
        if not self._current_artifacts:
            artifact_list.append(ListItem(Label("(no relevant files)")))
            self._selected_artifact_index = 0
            return
        for artifact in self._current_artifacts:
            marker = "✓" if artifact.exists else "✗"
            label = f"{marker} {self._display_path(artifact.path)}"
            artifact_list.append(ListItem(Label(label)))
        self._selected_artifact_index = min(
            self._selected_artifact_index, len(self._current_artifacts) - 1
        )
        artifact_list.index = self._selected_artifact_index

    def _populate_action_list(self) -> None:
        action_list = self.query_one("#action-list", ListView)
        self._clear_list_view(action_list)
        for action in self._actions:
            prefix = "MUTATE" if action.kind == "mutating" else "VIEW"
            label = f"[{prefix}] {action.label}"
            action_list.append(ListItem(Label(label)))
        if self._actions:
            self._selected_action_index = min(
                self._selected_action_index, len(self._actions) - 1
            )
            action_list.index = self._selected_action_index

    def _update_details(self) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            return
        stage_name = self._selected_stage_name() or snapshot.current_stage
        stage_summary_widget = self.query_one("#stage-summary", Static)
        required_widget = self.query_one("#required-artifacts", Static)
        verification_widget = self.query_one("#verification-summary", Static)
        blockers_widget = self.query_one("#blockers-summary", Static)

        stage_summary = snapshot.stage_summaries.get(
            stage_name, "No summary available."
        )
        stage_summary_widget.update(
            (
                f"Current stage: {snapshot.current_stage} "
                f"(attempt {snapshot.stage_attempt}/{snapshot.max_stage_attempts})\n"
                f"Selected stage: {stage_name}\n{stage_summary}"
            )
        )

        stage_artifacts = list(snapshot.artifacts_by_stage.get(stage_name, ()))
        seen_paths: set[Path] = set()
        relevant: list[ArtifactItem] = []
        for artifact in [*stage_artifacts, *snapshot.common_artifacts]:
            if artifact.path in seen_paths:
                continue
            seen_paths.add(artifact.path)
            relevant.append(artifact)
        self._current_artifacts = tuple(relevant)
        self._populate_artifact_list()

        if stage_artifacts:
            required_lines = [
                f"[{'x' if item.exists else ' '}] {self._display_path(item.path)}"
                for item in stage_artifacts
            ]
            required_widget.update("Required artifacts:\n" + "\n".join(required_lines))
        else:
            required_widget.update("Required artifacts:\n- (none for this stage)")

        verification = snapshot.verification
        if verification is None:
            verification_widget.update("Last verification: unavailable")
        else:
            lines = [
                (
                    f"Last verification: {'PASS' if verification.passed else 'FAIL'} "
                    f"@ {verification.generated_at or '<unknown>'}"
                ),
                f"Stage: {verification.stage_effective or '<unknown>'}",
                f"Message: {verification.message or '<none>'}",
            ]
            if verification.failing_commands:
                lines.append("Failing commands:")
                lines.extend(
                    f"- {entry}" for entry in verification.failing_commands[:4]
                )
            verification_widget.update("\n".join(lines))

        if snapshot.top_blockers:
            blockers_text = "Top blockers:\n" + "\n".join(
                f"- {entry}" for entry in snapshot.top_blockers[:6]
            )
        else:
            blockers_text = "Top blockers:\n- none"
        blockers_widget.update(blockers_text)

    def _update_safety_row(self) -> None:
        arm_button = self.query_one("#arm-toggle", Button)
        status = self.query_one("#safety-status", Static)
        if self._armed:
            arm_button.label = "Arm actions: ON"
            arm_button.variant = "success"
            status.update("Armed: mutating commands enabled.")
        else:
            arm_button.label = "Arm actions: OFF"
            arm_button.variant = "warning"
            status.update("Disarmed (read-only mode).")

    def _update_running_banner(self) -> None:
        banner = self.query_one("#running-banner", Static)
        if self._running_intent is None:
            banner.update("")
            return
        command = shlex.join(self._running_intent.argv)
        banner.update(f"Running: {command[:72]}")

    def _update_action_button_state(self) -> None:
        run_button = self.query_one("#run-action", Button)
        stop_button = self.query_one("#stop-loop", Button)
        action = self._selected_action()
        running = self._running_intent is not None
        if running:
            run_button.disabled = True
        elif self._snapshot is None:
            run_button.disabled = True
        elif action is None:
            run_button.disabled = True
        elif action.requires_arm and not self._armed:
            run_button.disabled = True
        else:
            run_button.disabled = False
        if self._snapshot is None:
            run_button.label = "Run selected action (snapshot unavailable)"
        elif action and action.requires_arm and not self._armed:
            run_button.label = "Run selected action (disarmed)"
        else:
            run_button.label = "Run selected action"
        stop_button.disabled = not (
            self._running_intent is not None
            and self._running_intent.action_id == "run_loop"
        )
        self._update_running_banner()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        list_id = event.list_view.id or ""
        selected_index = event.list_view.index
        if selected_index is None or selected_index < 0:
            selected_index = 0
        if list_id == "stage-list":
            self._selected_stage_index = selected_index
            snapshot = self._snapshot
            if snapshot is not None and 0 <= self._selected_stage_index < len(
                snapshot.stage_items
            ):
                self._selected_stage_key = snapshot.stage_items[
                    self._selected_stage_index
                ].name
            self._update_details()
        elif list_id == "run-list":
            self._selected_run_index = selected_index
        elif list_id == "todo-list":
            self._selected_todo_index = selected_index
        elif list_id == "artifact-list":
            self._selected_artifact_index = selected_index
        elif list_id == "action-list":
            self._selected_action_index = selected_index
            self._update_action_button_state()

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

    async def _confirm_command(
        self, *, title: str, intent: CommandIntent, confirm_label: str = "Confirm"
    ) -> bool:
        command = shlex.join(intent.argv)
        confirmed = await self.push_screen_wait(
            ConfirmationScreen(
                title=title,
                command=command,
                cwd=intent.cwd,
                expected_writes=intent.expected_writes,
                confirm_label=confirm_label,
            )
        )
        return bool(confirmed)

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
        return await self._confirm_command(
            title=title,
            intent=intent,
            confirm_label=confirm_label,
        )

    async def _open_artifact_viewer(self, artifact_path: Path) -> None:
        text, truncated = load_artifact_text(artifact_path)
        if truncated:
            text = f"{text}\n\n[viewer note] content truncated for readability."
        result = await self.push_screen_wait(
            ArtifactViewerScreen(artifact_path=artifact_path, artifact_text=text)
        )
        if result != "open_editor":
            return
        snapshot = self._snapshot
        if snapshot is None:
            return
        intent = build_open_in_editor_intent(
            target_path=artifact_path, cwd=snapshot.repo_root
        )
        if await self._confirm_command(
            title="Open artifact in external editor?",
            intent=intent,
            confirm_label="Open",
        ):
            self._start_command(intent)

    def _start_command(self, intent: CommandIntent) -> None:
        if self._running_intent is not None:
            self.notify("A command is already running.")
            return
        self._console_tail.clear()
        self._render_console_tail()
        command = shlex.join(intent.argv)
        self._append_console(f"starting: {command}")
        self._running_intent = intent
        self._update_action_button_state()
        try:
            self._runner.start(intent)
        except Exception as exc:
            self._append_console(f"failed to start command: {exc}")
            self._running_intent = None
            self._update_action_button_state()

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
            self._update_safety_row()
            self._update_action_button_state()
            self._refresh_snapshot()

        try:
            self.call_from_thread(_finish)
        except Exception:
            pass

    async def _handle_action(self, action: ActionSpec) -> None:
        snapshot = self._snapshot
        if snapshot is None:
            self.notify("No snapshot loaded.")
            return
        if action.requires_arm and not self._armed:
            self.notify("Action is disarmed. Enable 'Arm actions' first.")
            return

        if action.action_id == "open_selected_artifact":
            artifact_path = self._selected_artifact_path()
            if artifact_path is None:
                self.notify("No artifact selected.")
                return
            await self._open_artifact_viewer(artifact_path)
            return

        if action.action_id == "open_selected_artifact_editor":
            artifact_path = self._selected_artifact_path()
            if artifact_path is None:
                self.notify("No artifact selected.")
                return
            intent = build_open_in_editor_intent(
                target_path=artifact_path,
                cwd=snapshot.repo_root,
            )
            if await self._confirm_action_intent(
                action=action,
                title="Open selected artifact in external editor?",
                intent=intent,
                confirm_label="Open",
            ):
                self._start_command(intent)
            return

        if action.action_id == "open_selected_run_manifest":
            run = self._selected_run()
            if run is None:
                self.notify("No run selected.")
                return
            await self._open_artifact_viewer(run.manifest_path)
            return

        if action.action_id == "open_selected_run_metrics":
            run = self._selected_run()
            if run is None:
                self.notify("No run selected.")
                return
            await self._open_artifact_viewer(run.metrics_path)
            return

        if action.action_id == "open_stage_prompt":
            stage_name = self._selected_stage_name() or snapshot.current_stage
            prompt_path = resolve_stage_prompt_path(snapshot, stage_name)
            if prompt_path is None:
                self.notify(f"No stage prompt found for '{stage_name}'.")
                return
            await self._open_artifact_viewer(prompt_path)
            return

        if action.action_id == "open_state_history":
            await self._open_artifact_viewer(snapshot.state_path)
            return

        intent: CommandIntent | None = None
        if action.action_id == "verify_current_stage":
            stage_name = self._selected_stage_name() or snapshot.current_stage
            intent = build_verify_intent(
                state_path=snapshot.state_path, stage=stage_name
            )
        elif action.action_id == "run_once":
            options = await self.push_screen_wait(RunOptionsScreen())
            if options is None:
                return
            intent = build_run_intent(state_path=snapshot.state_path, options=options)
        elif action.action_id == "run_loop":
            options = await self.push_screen_wait(LoopOptionsScreen())
            if options is None:
                return
            intent = build_loop_intent(state_path=snapshot.state_path, options=options)
        elif action.action_id == "todo_sync":
            intent = build_todo_sync_intent(state_path=snapshot.state_path)
        elif action.action_id == "lock_break":
            intent = build_lock_break_intent(
                state_path=snapshot.state_path, reason="tui manual break"
            )

        if intent is None:
            self.notify("Unsupported action.")
            return
        if await self._confirm_action_intent(
            action=action,
            title=f"Confirm: {action.label}",
            intent=intent,
        ):
            self._start_command(intent)

    async def _toggle_arm(self) -> None:
        if self._armed:
            self._armed = False
            self._update_safety_row()
            self._update_action_button_state()
            return
        pseudo_intent = CommandIntent(
            action_id="arm_actions",
            argv=("autolab", "tui", "--arm-actions"),
            cwd=self._state_path.parent.parent,
            expected_writes=(),
            mutating=False,
        )
        confirmed = await self._confirm_command(
            title="Enable mutating actions?",
            intent=pseudo_intent,
            confirm_label="Arm",
        )
        if not confirmed:
            return
        self._armed = True
        self._update_safety_row()
        self._update_action_button_state()

    async def _stop_loop(self) -> None:
        intent = self._running_intent
        if intent is None or intent.action_id != "run_loop":
            self.notify("No loop command is running.")
            return
        stop_intent = CommandIntent(
            action_id="stop_loop",
            argv=("autolab", "tui", "--stop-loop"),
            cwd=intent.cwd,
            expected_writes=(),
            mutating=False,
        )
        confirmed = await self._confirm_command(
            title="Stop active loop?",
            intent=stop_intent,
            confirm_label="Stop loop",
        )
        if not confirmed:
            return
        if self._runner.stop():
            self._append_console("stop requested for active loop process")
        else:
            self._append_console("no running loop process to stop")

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "arm-toggle":
            self._start_ui_flow(label="arm-toggle", flow_factory=self._toggle_arm)
            return
        if button_id == "run-action":
            action = self._selected_action()
            if action is None:
                self.notify("No action selected.")
                return
            self._start_ui_flow(
                label="run-action",
                flow_factory=lambda: self._handle_action(action),
            )
            return
        if button_id == "stop-loop":
            self._start_ui_flow(label="stop-loop", flow_factory=self._stop_loop)
            return
        if button_id == "refresh-snapshot":
            if self._refresh_snapshot():
                self._append_console("snapshot refreshed")
            return
        if button_id == "clear-console":
            self._console_tail.clear()
            self._render_console_tail()
