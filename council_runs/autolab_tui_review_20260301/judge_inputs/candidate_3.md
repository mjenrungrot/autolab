# Overview

## Change Summary

- Stabilize the Textual cockpit around clearer module boundaries (`cli` entrypoint, `app` shell, `action` dispatch, `snapshot` data loading, `runner` lifecycle) while preserving current CLI behavior and safety defaults.
- Reduce complexity in `AutolabCockpitApp` by extracting modal screens and presentation formatting, then replacing the action `if/elif` chain with a typed handler registry.
- Harden snapshot/runner behavior against malformed state and stop-race edge cases, and add targeted regression tests for locked product decisions.
- Update TUI docs to reflect the refactored architecture and extension workflow so future changes remain low-risk.

## Files Updated

- `/Users/tjenrung/Workspaces/autolab/src/autolab/commands.py:2382`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py:1`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py:1`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py:1`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py:1`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/models.py:1`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/cli.py:1` (new)
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/screens.py:1` (new)
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/presenters.py:1` (new)
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/action_handlers.py:1` (new)
- `/Users/tjenrung/Workspaces/autolab/tests/test_commands_smoke.py:1`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py:1`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py:1`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py:1` (new)
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_app_rendering.py:1` (new)
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_action_dispatch.py:1` (new)
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_cockpit_safety.py:1` (new)
- `/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md:1`
- `/Users/tjenrung/Workspaces/autolab/docs/quickstart.md:1`
- `/Users/tjenrung/Workspaces/autolab/README.md:1`

## Tasks

### Task 1

- id: TUI-01
- title: Isolate `autolab tui` CLI boundary
- depends_on: []
- location: `/Users/tjenrung/Workspaces/autolab/src/autolab/commands.py:2382`, `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/cli.py:1`
- description: Move `_cmd_tui` logic into a dedicated TUI CLI module (`tail-lines` validation, TTY checks, Textual import guard, app launch) and keep `commands.py` as a thin delegator to improve cohesion and reduce churn risk in the large command file.
- touches: \[`/Users/tjenrung/Workspaces/autolab/src/autolab/commands.py:2382`, `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/cli.py:1`, `/Users/tjenrung/Workspaces/autolab/tests/test_commands_smoke.py:1`\]
- conflict_group: cli-boundary
- scope_ok: true
- validation: `PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_commands_smoke.py -k "tui"`
- status: Not Completed

### Task 2

- id: TUI-02
- title: Harden snapshot parsing and artifact loading
- depends_on: []
- location: `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py:1`
- description: Add safe coercion utilities for state fields, avoid full snapshot failure on malformed values, preserve deterministic fallbacks, and simplify `load_artifact_text` to avoid duplicate reads while keeping truncation semantics stable.
- touches: \[`/Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py:1`, `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/models.py:1`, `/Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py:1`\]
- conflict_group: snapshot-data
- scope_ok: true
- validation: `PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py`
- status: Not Completed

### Task 3

- id: TUI-03
- title: Stabilize command runner lifecycle
- depends_on: []
- location: `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py:1`
- description: Make runner start/stop transitions explicit and idempotent, guarantee single completion callback, and add tests for interrupt/terminate/kill paths so loop-stop behavior is predictable under slow or non-cooperative subprocesses.
- touches: \[`/Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py:1`, `/Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py:1`\]
- conflict_group: runner-lifecycle
- scope_ok: true
- validation: `PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py`
- status: Not Completed

### Task 4

- id: TUI-04
- title: Decompose app UI shell from presentation code
- depends_on: [TUI-02]
- location: `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py:1`
- description: Extract modal screens and text/list formatting helpers into dedicated modules, then keep `AutolabCockpitApp` focused on orchestration; include a small selection-state fix so refresh does not silently override an intentional stage index `0` selection.
- touches: \[`/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py:1`, `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/screens.py:1`, `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/presenters.py:1`, `/Users/tjenrung/Workspaces/autolab/tests/test_tui_app_rendering.py:1`\]
- conflict_group: app-structure
- scope_ok: true
- validation: `PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_app_rendering.py`
- status: Not Completed

### Task 5

- id: TUI-05
- title: Replace action branching with typed handler registry
- depends_on: [TUI-04]
- location: `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py:1`, `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/action_handlers.py:1`
- description: Introduce a registry (`action_id -> handler`) with startup completeness checks against the action catalog, remove the large `if/elif` action switch, and centralize safety requirements so adding actions is a single-file, low-risk change.
- touches: \[`/Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py:1`, `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/action_handlers.py:1`, `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py:1`, `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/models.py:1`, `/Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py:1`, `/Users/tjenrung/Workspaces/autolab/tests/test_tui_action_dispatch.py:1`\]
- conflict_group: action-dispatch
- scope_ok: true
- validation: `PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_action_dispatch.py`
- status: Not Completed

### Task 6

- id: TUI-06
- title: Add cockpit safety-contract integration tests
- depends_on: [TUI-03, TUI-05]
- location: `/Users/tjenrung/Workspaces/autolab/tests/test_tui_cockpit_safety.py:1`
- description: Add regression tests for locked decisions: verify action is mutating and arm+confirm gated, default artifact open path is in-TUI viewer, stop button is loop-only, and mutating completion auto-disarms.
- touches: \[`/Users/tjenrung/Workspaces/autolab/tests/test_tui_cockpit_safety.py:1`, `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py:1`\]
- conflict_group: cockpit-contract-tests
- scope_ok: true
- validation: `PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_cockpit_safety.py`
- status: Not Completed

### Task 7

- id: TUI-07
- title: Refresh TUI documentation for new module boundaries
- depends_on: [TUI-01, TUI-05]
- location: `/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md:1`
- description: Document architecture responsibilities (`cli`, `app`, `snapshot`, `runner`, `screens`, `action_handlers`), extension checklist for adding actions, and explicit safety invariants without changing command surface.
- touches: \[`/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md:1`, `/Users/tjenrung/Workspaces/autolab/docs/quickstart.md:1`, `/Users/tjenrung/Workspaces/autolab/README.md:1`\]
- conflict_group: tui-docs
- scope_ok: true
- validation: `PYTHONPATH=src python -m autolab tui --help`
- status: Not Completed

## Parallel Execution Groups

- Wave 1: TUI-01, TUI-02, TUI-03
- Wave 2: TUI-04
- Wave 3: TUI-05
- Wave 4: TUI-06, TUI-07

## Risks and edge cases

- Textual pilot tests can be timing-sensitive; use deterministic stubs for snapshot/runner and avoid wall-clock sleeps.
- Runner stop semantics differ by subprocess behavior; verify interrupt, terminate, and kill branches independently.
- Snapshot hardening can mask real data issues; preserve warnings in logs/UI so corruption remains visible.
- Action registry migration can silently drop behavior if catalog-handler parity checks are missing.
- App decomposition can regress keybindings/selection sync unless rendering and selection tests are added together.

## Rollback or mitigation

- Keep a compatibility wrapper in `/Users/tjenrung/Workspaces/autolab/src/autolab/commands.py:2382` so `autolab tui` entrypoint remains unchanged during refactor.
- Land registry and app decomposition behind parity tests first; if regressions appear, revert only `action_handlers` wiring while keeping extracted screens/presenters.
- If runner lifecycle changes cause instability, fall back to current stop strategy and retain new tests to guide incremental fixes.
- If snapshot warning surfacing is noisy, gate warning display to console-only while preserving robust parsing.
- If docs drift during execution, treat `/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md:1` as source of truth and sync README/quickstart from it at the end.
