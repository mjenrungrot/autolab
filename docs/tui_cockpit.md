# Textual Inspector Cockpit (`autolab tui`)

`autolab tui` provides a selection-first terminal cockpit for inspecting workflow state and running common commands with explicit safety checks.

## Launch

```bash
autolab tui --state-file .autolab/state.json
```

Optional:

```bash
autolab tui --tail-lines 2000
```

## Layout

- **Left (Navigator)**: stage list, run list, todo list.
- **Center (Details)**: stage summary, required artifacts checklist, last verification result, top blockers, relevant files.
- **Right (Actions)**: view + mutating action list, execute button, loop stop button.
- **Bottom (Console)**: timestamped output stream for the most recent command.

## Safety model

- Cockpit starts **disarmed** (read-only default).
- Mutating actions are disabled until you press **Arm actions** and confirm.
- If snapshot refresh fails, cockpit enters a fail-closed state: it clears snapshot-derived context, auto-disarms, and keeps action execution blocked until refresh succeeds.
- Every command execution is confirmation-gated and shows:
  - exact command
  - cwd
  - expected writes (best-effort)
- After any mutating action completes, cockpit auto-disarms.

## View actions

- Open selected artifact in read-only in-TUI viewer (default artifact behavior).
- Open selected artifact in `$EDITOR` (confirmation-gated).
- Open selected run manifest.
- Open selected run metrics.
- Open current stage prompt.
- Open state/history (`.autolab/state.json`).

## Mutating actions (arm + confirm required)

- Verify current stage.
- Run one transition (form-backed options).
- Start loop (form-backed options, with `Stop loop` button).
- Todo sync.
- Lock break.

## Run/Loop forms

`Run one transition` supports:

- `--verify`
- `--auto-decision`
- `--run-agent` / `--no-run-agent`

`Start loop` supports:

- `--max-iterations`
- `--max-hours`
- `--auto`
- `--verify`
- `--run-agent` / `--no-run-agent`

## Notes

- The cockpit never writes repo files from view-only actions.
- `Verify current stage` is treated as mutating because it updates verification artifacts/logs.
- Stage selection is persisted by stage name across refreshes, including explicit selection of the first row.
- If `stdin/stdout` are not interactive TTYs, `autolab tui` exits with an error.
