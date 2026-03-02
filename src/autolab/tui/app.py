from __future__ import annotations

import json
import shlex
from collections import deque
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "unlock")


class ActionConfirmScreen(ModalScreen[bool]):
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
            yield Static("Details hidden.", id="action-confirm-details", markup=False)
            with Horizontal(id="action-confirm-buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Show Details", id="toggle-details")
                yield Button(self._confirm_label, id="confirm", variant="error")

    def on_mount(self) -> None:
        self._render_details()
        self.query_one("#cancel", Button).focus()

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
            self._show_details = not self._show_details
            self._render_details()
            return
        self.dismiss(event.button.id == "confirm")


class RunPresetScreen(ModalScreen[RunActionOptions | None]):
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id != "continue":
            return

        preset_index = self.query_one("#run-preset-list", ListView).index or 0
        use_advanced = self.query_one("#run-advanced", Checkbox).value

        if preset_index == 0:
            base = RunActionOptions(
                verify=False, auto_decision=False, run_agent_mode="policy"
            )
        elif preset_index == 1:
            base = RunActionOptions(
                verify=True, auto_decision=False, run_agent_mode="policy"
            )
        else:
            if not use_advanced:
                self.notify("Enable 'Use advanced options' for this preset.")
                return
            base = RunActionOptions(
                verify=True, auto_decision=False, run_agent_mode="policy"
            )

        if not use_advanced:
            self.dismiss(base)
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
                auto_decision=self.query_one("#run-auto", Checkbox).value,
                run_agent_mode=run_agent_mode,
            )
        )


class LoopPresetScreen(ModalScreen[LoopActionOptions | None]):
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        if event.button.id != "continue":
            return

        preset_index = self.query_one("#loop-preset-list", ListView).index or 0
        use_advanced = self.query_one("#loop-advanced", Checkbox).value

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
            if not use_advanced:
                self.notify("Enable 'Use advanced options' for this preset.")
                return
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
            self.notify("Choose only one run-agent override.")
            return
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


class ArtifactViewerScreen(ModalScreen[str | None]):
    CSS = """
    ArtifactViewerScreen {
      align: center middle;
    }

    #artifact-dialog {
      width: 100%;
      height: 100%;
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

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "open-editor":
            self.dismiss("open_editor")
            return
        self.dismiss(None)


class AutolabCockpitApp(App[None]):
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
      width: 18;
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
        ("question_mark", "show_help", "Help"),
        ("tab", "focus_next", "Next"),
        ("shift+tab", "focus_previous", "Prev"),
        ("enter", "activate_selection", "Activate"),
        ("u", "toggle_safety_lock", "Unlock/Lock"),
        ("r", "refresh_snapshot", "Refresh"),
        ("x", "toggle_advanced", "Advanced"),
        ("s", "stop_loop", "Stop Loop"),
        ("c", "clear_console", "Clear Console"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self, *, state_path: Path, tail_lines: int = 2000) -> None:
        super().__init__()
        self._state_path = state_path.expanduser().resolve()
        self._tail_lines = max(200, int(tail_lines))
        self._console_tail: deque[str] = deque(maxlen=self._tail_lines)
        self._armed = False
        self._show_advanced = False
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
            yield Static("", id="status-running")
        yield Static(
            "Keys: 1-5 view | Enter run action | u lock | x advanced | r refresh | s stop loop | c clear | ? help",
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
                with Vertical(id="home-render-card"):
                    yield Static("What Autolab Will Run Now", id="home-render-title")
                    with VerticalScroll(id="home-render-scroll"):
                        yield Markdown("", id="home-render-markdown", open_links=False)
                yield Static("", id="home-blocker-card", markup=False)
                yield Static("", id="home-artifacts-card", markup=False)
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
                yield ListView(id="artifact-list")
                with Horizontal(id="file-buttons"):
                    yield Button("Open Viewer", id="file-open-viewer")
                    yield Button("Open Editor", id="file-open-editor")
                    yield Button("Open Rendered", id="file-open-rendered")
                    yield Button("Open Context", id="file-open-context")
                    yield Button("Open Template", id="file-open-prompt")
                    yield Button("Open State", id="file-open-state")
                with Horizontal(id="file-advanced-buttons"):
                    yield Button(
                        "Start Loop (Advanced)", id="file-run-loop", variant="warning"
                    )
                    yield Button(
                        "Break Lock (Advanced)", id="file-lock-break", variant="error"
                    )
            with Vertical(id="console-view", classes="view-panel"):
                yield Static("Console", classes="view-title")
                yield RichLog(
                    id="console-log", markup=False, wrap=False, highlight=False
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

    def _update_ui_chrome(self) -> None:
        safety = self.query_one("#status-safety", Static)
        mode = self.query_one("#status-mode", Static)
        advanced = self.query_one("#status-advanced", Static)
        running = self.query_one("#status-running", Static)

        safety.update(
            "Unlocked: mutating enabled." if self._armed else "Locked: read-only."
        )
        self._set_tone(safety, "tone-warning" if self._armed else "tone-success")
        mode.update(f"Mode: {self._mode}")
        advanced.update(
            "Advanced: visible" if self._show_advanced else "Advanced: hidden"
        )
        self._set_tone(advanced, "tone-info" if self._show_advanced else "tone-muted")

        if self._running_intent is None:
            running.update("Idle")
            self._set_tone(running, "tone-muted")
        else:
            command = shlex.join(self._running_intent.argv)
            running.update(f"Running: {command[:72]}")
            self._set_tone(running, "tone-info")

        self.query_one(
            "#file-advanced-buttons", Horizontal
        ).display = self._show_advanced

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

        artifacts_widget = self.query_one("#home-artifacts-card", Static)
        artifacts_widget.update("Artifacts\nUnavailable.")
        self._set_tone(artifacts_widget, "tone-warning")

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
            self._armed = False
            self._clear_snapshot_views()
            self._update_ui_chrome()
            self._append_console(f"snapshot refresh failed: {exc}")
            self.notify(f"Snapshot refresh failed: {exc}")
            return False

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
        render_preview = snapshot.render_preview
        if render_preview.status == "ok":
            prompt_markdown = build_preview_markdown(
                render_preview.prompt_text,
                source_path=render_preview.template_path,
                hint="markdown",
            )
            render_markdown.update(
                f"**Stage:** `{render_preview.stage}`\n\n{prompt_markdown}"
            )
            self._set_tone(render_card, "tone-info")
        elif render_preview.status == "unavailable":
            render_markdown.update(
                build_preview_markdown(
                    "Render preview unavailable for this stage.",
                    hint="text",
                )
            )
            self._set_tone(render_card, "tone-warning")
        else:
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
            run_label = Label(f"{run.run_id} [{run.status}] start={started}")
            run_label.add_class(self._tone_for_run_status(run.status))
            run_list.append(ListItem(run_label))
        self._selected_run_index = min(self._selected_run_index, len(snapshot.runs) - 1)
        run_list.index = self._selected_run_index
        self._update_run_details()

    def _update_run_details(self) -> None:
        run = self._selected_run()
        if run is None:
            details_widget = self.query_one("#run-details", Static)
            details_widget.update("Run Details\nNo run selected.")
            self._set_tone(details_widget, "tone-muted")
            return
        details_widget = self.query_one("#run-details", Static)
        details_widget.update(
            "Run Details\n"
            f"- Run ID: {run.run_id}\n"
            f"- Status: {run.status}\n"
            f"- Started: {run.started_at or '-'}\n"
            f"- Completed: {run.completed_at or '-'}"
        )
        self._set_tone(details_widget, self._tone_for_run_status(run.status))

    def _populate_artifact_list(self) -> None:
        snapshot = self._snapshot
        artifact_list = self.query_one("#artifact-list", ListView)
        artifact_list.clear()
        self._current_artifacts = ()

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
        self._current_artifacts = tuple(merged)

        if not self._current_artifacts:
            empty_label = Label("(No relevant files)")
            empty_label.add_class("tone-muted")
            artifact_list.append(ListItem(empty_label))
            self._selected_artifact_index = 0
            context_widget = self.query_one("#files-context", Static)
            context_widget.update(
                f"Files\n- Stage: {stage}\n- No files detected for this stage."
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
        context_widget = self.query_one("#files-context", Static)
        context_widget.update(
            "Files\n"
            f"- Stage: {snapshot.current_stage}\n"
            f"- Selected: {selected_text}\n"
            "- View-only: Viewer, Editor, Rendered, Context, Template, State\n"
            "- Mutating: Loop and Lock Break (unlock + confirm required)"
        )
        self._set_tone(context_widget, "tone-info")

    def _update_help_text(self) -> None:
        help_widget = self.query_one("#help-text", Static)
        help_widget.update(
            "Autolab TUI\n"
            "\n"
            "Views\n"
            "- Home: stage status, rendered prompt preview, recommended actions.\n"
            "- Runs: run manifest and metrics overview.\n"
            "- Files: artifacts plus rendered prompt/context/template quick-open.\n"
            "- Console: live command output.\n"
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

    def action_refresh_snapshot(self) -> None:
        if self._refresh_snapshot():
            self._append_console("snapshot refreshed")

    def action_clear_console(self) -> None:
        self._console_tail.clear()
        self._render_console_tail()

    def action_stop_loop(self) -> None:
        self._start_ui_flow(label="stop-loop", flow_factory=self._stop_loop)

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
        self._console_tail.clear()
        self._render_console_tail()
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
        confirmed = await self.push_screen_wait(
            ActionConfirmScreen(
                title="Stop active loop?",
                summary="This requests a graceful interrupt for the active loop process.",
                command=shlex.join(stop_intent.argv),
                cwd=stop_intent.cwd,
                expected_writes=stop_intent.expected_writes,
                confirm_label="Stop loop",
            )
        )
        if not confirmed:
            return

        if self._runner.stop():
            self._append_console("stop requested for active loop process")
        else:
            self._append_console("no running loop process to stop")
