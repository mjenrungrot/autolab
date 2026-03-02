# Textual Inspector Cockpit (`autolab tui`)

`autolab tui` is an onboarding-first Textual cockpit for inspecting workflow state and executing common commands with strict safety guards.

## Launch

```bash
autolab tui --state-file .autolab/state.json
```

Optional:

```bash
autolab tui --tail-lines 2000
```

## Mental Model

The cockpit is mode-based, not multi-pane focus-based.

- **Home**: Stage summary, rendered prompt preview ("what will run now"), blockers, required artifacts, recommended actions.
- **Runs**: Run list plus quick open for manifest/metrics.
- **Files**: Stage/common files plus quick open for rendered prompt, render context, and prompt template.
- **Console**: Live output for the active command.
- **Help**: Keymap and safety model.

Only one primary workspace is shown at a time to reduce UI overload for first-time users.

## Keymap

- `1` Home
- `2` Runs
- `3` Files
- `4` Console
- `5` Help
- `?` Help
- `Enter` Activate selection or focused button
- `u` Unlock/lock mutating actions
- `x` Toggle advanced actions visibility
- `r` Refresh snapshot
- `s` Stop active loop
- `c` Clear console
- `q` Quit

## Safety Model (Strict)

- Cockpit starts **locked** (read-only default).
- Mutating actions require:
  1. explicit unlock (`u`), and
  1. per-action confirmation.
- After any mutating action completes, cockpit auto-locks again.
- If snapshot refresh fails, cockpit enters fail-closed mode:
  - clears snapshot-derived context,
  - auto-locks,
  - blocks mutating actions until refresh succeeds.

## Action UX

### Decision-first confirmations

Confirmation dialogs show:

- action, risk level, and purpose first,
- details (`command`, `cwd`, expected writes) behind **Show Details**.

### Preset-first run flows

`Run one transition` and `Start loop` use presets first, then optional advanced controls.

`Run one transition` presets:

- Quick safe run (recommended)
- Run with verify
- Advanced options

`Start loop` presets:

- Guided short loop (recommended)
- Unattended loop with verify
- Advanced options

## Advanced Actions

High-risk actions are intentionally de-emphasized and hidden by default.

- `Start loop (advanced)`
- `Break lock (advanced)`

Reveal them with `x` (Toggle Advanced).

## Notes

- External API remains unchanged: `autolab tui --state-file ... --tail-lines ...`.
- View actions never mutate repo-tracked workflow files.
- `Verify current stage` remains mutating because it updates verification artifacts/logs.
- Render preview uses the same internal prompt rendering path as `autolab render` with `write_outputs=False`.
- Semantic colors are intentionally restrained: success/info/warning/error cues improve scanability without changing behavior.
- If `stdin/stdout` are not interactive TTYs, `autolab tui` exits with an error.
