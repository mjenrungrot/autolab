# Overview

This wave prioritizes deterministic, user-safety-first behavior in `autolab tui`: write app-level tests first, then apply minimal source changes to enforce fail-closed UI behavior, consistent confirmation/arming interactions, and branch-complete regressions across CLI, actions, snapshot loading, and runner lifecycle.

## Change Summary

- Add deterministic headless app interaction tests for safety-critical UX flows.
- Close remaining branch gaps in CLI/action/snapshot/runner tests after Wave 1.
- Harden app refresh/selection/dispatch behavior to fail closed when state cannot be trusted.
- Keep locked decisions unchanged (`autolab tui`, Textual UI, verify as mutating arm+confirm, viewer-first artifact opens).
- Sync docs to match post-hardening behavior and run a focused TUI validation matrix.

## Files Updated

- `/Users/tjenrung/Workspaces/autolab/src/autolab/commands.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py`
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
id: TEST-TUI-APP-001
title: Add deterministic app-level safety interaction tests
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::AutolabCockpitApp
description: >
  Create headless deterministic app tests for safety-critical UX flows: refresh failure fail-closed behavior,
  run-action disablement when snapshot is unavailable, explicit stage selection persistence (including index 0),
  confirmation-gated arm/stop interactions, and auto-disarm after mutating command completion.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py
conflict_group: tests_app_behavior
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"
status: Not Completed
```

```yaml
id: TEST-TUI-CMD-001
title: Expand CLI preflight and exit-code branch tests
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py::_cmd_tui
description: >
  Add deterministic tests for remaining `_cmd_tui` branches: state path exists but is not a file,
  unreadable state path, and KeyboardInterrupt runtime handling (exit 130), while preserving existing
  interactive TTY and import/runtime failure expectations.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py
conflict_group: tests_cli_tui
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py"
status: Not Completed
```

```yaml
id: TEST-TUI-ACTIONS-001
title: Complete action catalog and intent safety coverage
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py
description: >
  Add tests for lock-break and editor intents, run-agent mode normalization boundaries, verify-stage
  argument behavior, and safety metadata invariants so confirmation/arm rules cannot drift from catalog intent.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py
conflict_group: tests_actions_tui
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py"
status: Not Completed
```

```yaml
id: TEST-TUI-SNAPSHOT-001
title: Add deterministic snapshot and artifact edge-case tests
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py
description: >
  Extend snapshot tests for run-id placeholder omission, prompt path resolution, binary/stat-failure artifact
  loading paths, non-positive max_chars handling, and blocker dedupe/cap stability.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py
conflict_group: tests_snapshot_tui
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py"
status: Not Completed
```

```yaml
id: TEST-TUI-RUNNER-001
title: Add deterministic runner completion and streaming tests
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py
description: >
  Add coverage for normal completion (`stopped=False`), output line forwarding order, intent/is_running lifecycle
  before and after exit, and callback exactly-once guarantees across natural and interrupted termination paths.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py
conflict_group: tests_runner_tui
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py"
status: Not Completed
```

```yaml
id: TUI-APP-UX-001
title: Enforce fail-closed refresh and consistent in-app safety UX
depends_on:
  - TEST-TUI-APP-001
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::_refresh_snapshot
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::_update_action_button_state
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::_populate_stage_list
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::_handle_action
description: >
  Apply minimal UX hardening to make snapshot refresh failures fail closed: clear unsafe snapshot state,
  force disarm, and keep run action disabled until a valid snapshot is loaded; preserve explicit user stage
  selection deterministically across refresh; and keep confirmation behavior consistent with action safety metadata.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py
conflict_group: src_app_ux
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py"
status: Not Completed
```

```yaml
id: TUI-CLI-HARDEN-001
title: Align `_cmd_tui` handling with expanded deterministic branches
depends_on:
  - TEST-TUI-CMD-001
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py::_cmd_tui
description: >
  Make any minimal CLI preflight/runtime adjustments required by new tests so all documented error branches
  return deterministic, user-facing exit codes/messages without changing command surface.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py
conflict_group: src_cli_tui
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py"
status: Not Completed
```

```yaml
id: TUI-ACTIONS-HARDEN-001
title: Tighten action intent normalization and catalog safety contract
depends_on:
  - TEST-TUI-ACTIONS-001
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::ACTION_CATALOG
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_run_intent
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_loop_intent
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_open_in_editor_intent
description: >
  Implement only the intent/catalog adjustments needed to satisfy new safety tests, preserving locked behavior:
  verify remains mutating with arm+confirm, editor open remains confirmation-only, and run-agent mode handling
  remains deterministic.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py
conflict_group: src_actions_tui
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py"
status: Not Completed
```

```yaml
id: TUI-SNAPSHOT-HARDEN-001
title: Preserve deterministic snapshot/artifact behavior under edge cases
depends_on:
  - TEST-TUI-SNAPSHOT-001
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::load_cockpit_snapshot
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::load_artifact_text
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::resolve_stage_prompt_path
description: >
  Apply targeted snapshot/artifact fixes uncovered by new tests to keep ordering deterministic and artifact
  loading resilient for binary, malformed, missing, and read-error cases without broad redesign.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py
conflict_group: src_snapshot_tui
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py"
status: Not Completed
```

```yaml
id: TUI-RUNNER-HARDEN-001
title: Finalize deterministic runner lifecycle semantics
depends_on:
  - TEST-TUI-RUNNER-001
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py::CommandRunner
description: >
  Make minimal lifecycle hardening changes required by tests so start/stop/complete behavior remains idempotent,
  line streaming is deterministic, and completion callbacks are emitted once per command.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py
conflict_group: src_runner_tui
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py"
status: Not Completed
```

```yaml
id: DOC-TUI-UX-001
title: Update operator docs for post-hardening safety UX
depends_on:
  - TUI-APP-UX-001
  - TUI-CLI-HARDEN-001
  - TUI-ACTIONS-HARDEN-001
  - TUI-SNAPSHOT-HARDEN-001
  - TUI-RUNNER-HARDEN-001
location:
  - /Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md
  - /Users/tjenrung/Workspaces/autolab/docs/quickstart.md
  - /Users/tjenrung/Workspaces/autolab/README.md
description: >
  Sync user-facing docs with final behavior: fail-closed snapshot refresh messaging, arm/confirm safety flow,
  deterministic command handling, and unchanged entrypoint/viewer defaults.
touches:
  - /Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md
  - /Users/tjenrung/Workspaces/autolab/docs/quickstart.md
  - /Users/tjenrung/Workspaces/autolab/README.md
conflict_group: docs_tui_ux
scope_ok: true
validation:
  - "Docs review: safety model and action list match actual app behavior."
status: Not Completed
```

```yaml
id: VAL-TUI-W2-001
title: Run focused TUI regression validation sweep
depends_on:
  - DOC-TUI-UX-001
  - TUI-APP-UX-001
  - TUI-CLI-HARDEN-001
  - TUI-ACTIONS-HARDEN-001
  - TUI-SNAPSHOT-HARDEN-001
  - TUI-RUNNER-HARDEN-001
location:
  - /Users/tjenrung/Workspaces/autolab
description: >
  Execute a deterministic, feature-scoped validation matrix for TUI/CLI behavior and help output before merge.
touches: []
conflict_group: validation_tui_wave2
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"
  - "Run: PYTHONPATH=src python -m autolab tui --help"
  - "Run: python -m compileall -q /Users/tjenrung/Workspaces/autolab/src/autolab/tui"
status: Not Completed
```

## Parallel Execution Groups

- **Wave 1 (test-first, parallel):** `TEST-TUI-APP-001`, `TEST-TUI-CMD-001`, `TEST-TUI-ACTIONS-001`, `TEST-TUI-SNAPSHOT-001`, `TEST-TUI-RUNNER-001`
- **Wave 2 (source hardening, parallel):** `TUI-APP-UX-001`, `TUI-CLI-HARDEN-001`, `TUI-ACTIONS-HARDEN-001`, `TUI-SNAPSHOT-HARDEN-001`, `TUI-RUNNER-HARDEN-001`
- **Wave 3 (docs):** `DOC-TUI-UX-001`
- **Wave 4 (gate):** `VAL-TUI-W2-001`

## Risks and edge cases

- Headless app tests can become timing-sensitive if they rely on uncontrolled async waits.
- Fail-closed refresh behavior can feel abrupt during transient file-system errors unless messaging is explicit.
- Selection persistence must distinguish intentional index `0` choice from default startup selection.
- Cross-platform process handling can diverge for signal escalation; runner assertions must remain platform-tolerant.
- External editor actions stay confirmation-only; tests must ensure this remains non-armed by design.

## Rollback or mitigation

- Land Wave 2 source tasks independently so any regression can be reverted per module.
- Keep all new behavior behind existing UI affordances (no new command flags or entrypoints).
- If app integration tests show flakiness, retain deterministic unit tests and quarantine only unstable interaction cases.
- If fail-closed refresh is too strict in practice, keep disarm + disable safeguards and add clearer retry guidance in UI/docs.
- Treat docs updates as part of the same merge unit to prevent operator mismatch.
