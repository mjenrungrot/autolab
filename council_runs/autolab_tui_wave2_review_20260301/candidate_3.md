# Candidate 3

# Overview

Maintainability-first council plan: targeted, low-risk refactors and regression coverage focused on TUI/CLI clarity after Wave 1, with no architectural churn.

## Change Summary

- Tighten readability and defensive behavior in `autolab tui` preflight, snapshot parsing, action intent construction, and runner lifecycle without changing command surface.
- Make `AutolabCockpitApp` fail closed on snapshot refresh failure and remove ambiguous UI success messaging.
- Improve selection consistency (especially explicit first-item selection) through deterministic reconciliation helpers.
- Add focused tests where current coverage is thin (especially app-level behavior) and keep docs aligned with behavior contracts.

## Files Updated

- `/Users/tjenrung/Workspaces/autolab/src/autolab/commands.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py` (new)
- `/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md`
- `/Users/tjenrung/Workspaces/autolab/docs/quickstart.md`
- `/Users/tjenrung/Workspaces/autolab/README.md`

## Tasks

```yaml
id: MB-TUI-001
title: Clarify `_cmd_tui` preflight flow
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py::_cmd_tui
description: >
  Extract and linearize TUI preflight checks (tail-lines parsing, state-file readability, TTY guard)
  into small internal helpers to improve readability and branch auditability while preserving current
  exit-code behavior and `autolab tui` entrypoint semantics.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py
conflict_group: wave1_cli_preflight
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py"
  - "Add branch test: non-integer/invalid tail-lines coercion returns deterministic CLI error."
status: Not Completed
```

```yaml
id: MB-TUI-002
title: Consolidate action intent write-set constants
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_verify_intent
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_run_intent
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_loop_intent
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_todo_sync_intent
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_lock_break_intent
description: >
  Reduce duplication and drift by centralizing expected-write tuples and shared argv assembly helpers,
  keeping safety metadata unchanged (verify remains mutating + arm+confirm; editor remains confirm-only).
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py
conflict_group: wave1_actions_intents
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py"
  - "Add assertions for lock-break/editor intent invariants and run-agent mode fallback."
status: Not Completed
```

```yaml
id: MB-TUI-003
title: Harden snapshot state coercion paths
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::load_cockpit_snapshot
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::_build_stage_items
description: >
  Add narrow coercion helpers for numeric/state fields so malformed values degrade safely instead of
  raising, while preserving stage ordering, blocker merge semantics, and artifact-resolution behavior.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py
conflict_group: wave1_snapshot_coercion
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py"
  - "Add malformed-state regression case (invalid stage_attempt/max_stage_attempts) with safe fallback."
status: Not Completed
```

```yaml
id: MB-TUI-004
title: Refine runner stop flow readability
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py::CommandRunner.stop
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py::CommandRunner._finish
description: >
  Keep behavior unchanged but simplify stop escalation structure for maintainability, and add race-focused
  tests for natural completion and stop requests near process exit to confirm single done-callback delivery.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py
conflict_group: wave1_runner_lifecycle
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py"
  - "Add regression: natural completion reports stopped=False exactly once."
status: Not Completed
```

```yaml
id: MB-TUI-005
title: Fail closed on snapshot refresh errors
depends_on:
  - MB-TUI-003
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::_refresh_snapshot
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::_populate_stage_list
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::on_button_pressed
description: >
  Make refresh failure explicit and safe by clearing stale snapshot-dependent UI state, avoiding
  false “snapshot refreshed” console messages on failure, and reconciling stage selection by stable key
  so intentional first-item selection is preserved across successful refreshes.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py
conflict_group: wave2_app_refresh
scope_ok: true
validation:
  - "Manual/headless check: refresh failure disables unsafe execution path and shows failure notice."
  - "Regression check: explicit stage index 0 selection persists after refresh."
status: Not Completed
```

```yaml
id: MB-TUI-006
title: Add focused app behavior regressions
depends_on:
  - MB-TUI-005
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py
description: >
  Introduce deterministic app-level tests for refresh fail-closed state, refresh-button messaging,
  selection retention, and post-mutation auto-disarm behavior without broad UI snapshot testing.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py
conflict_group: wave3_app_tests
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"
status: Not Completed
```

```yaml
id: MB-TUI-007
title: Align TUI docs with clarified behavior
depends_on:
  - MB-TUI-001
  - MB-TUI-005
location:
  - /Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md
  - /Users/tjenrung/Workspaces/autolab/docs/quickstart.md
  - /Users/tjenrung/Workspaces/autolab/README.md
description: >
  Update operator-facing docs to reflect fail-closed refresh behavior, consistent selection semantics,
  and unchanged safety contracts (viewer default, verify as mutating with arm+confirm, unchanged command surface).
touches:
  - /Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md
  - /Users/tjenrung/Workspaces/autolab/docs/quickstart.md
  - /Users/tjenrung/Workspaces/autolab/README.md
conflict_group: wave3_docs_sync
scope_ok: true
validation:
  - "Run: PYTHONPATH=src python -m autolab tui --help"
  - "Doc checklist: safety model and default viewer path match implemented behavior."
status: Not Completed
```

## Parallel Execution Groups

- **Wave 1 (parallel):** `MB-TUI-001`, `MB-TUI-002`, `MB-TUI-003`, `MB-TUI-004`
- **Wave 2 (sequential):** `MB-TUI-005`
- **Wave 3 (parallel):** `MB-TUI-006`, `MB-TUI-007`

## Risks and edge cases

- App-level headless tests can be timing-sensitive; use deterministic stubs and avoid wall-clock waits.
- Fail-closed refresh behavior may surprise users who relied on stale data visibility after errors.
- Snapshot coercion may hide malformed state symptoms unless error notifications remain visible.
- Runner race tests can be flaky on slow CI hosts if sleep windows are too tight.

## Rollback or mitigation

- Keep each refactor isolated per file so reverts are surgical (`commands`, `actions`, `snapshot`, `runner`, `app` independently).
- If fail-closed refresh is too disruptive, temporarily retain stale snapshot display but keep explicit failure banner/logging.
- If new app-level tests are unstable, keep unit-level assertions and gate integration-style checks behind deterministic fixtures.
- If docs diverge during rollout, treat `/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md` as source of truth and sync other docs from it.
