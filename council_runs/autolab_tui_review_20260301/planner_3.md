# Overview

Stabilization plan for the `autolab tui` cockpit with test-and-UX-first hardening, focused on selection safety, modal safety defaults, stop behavior, snapshot resilience, and deterministic validation while preserving locked product decisions.

## Change Summary

- Fix selection synchronization and focus behavior so refreshes do not silently retarget user intent.
- Preserve safety guarantees (`verify` stays mutating with arm+confirm; artifact opening defaults to in-TUI read-only viewer).
- Harden snapshot/file loading and runner stop lifecycle against edge cases.
- Improve command-intent clarity and CLI preflight behavior for predictable failures.
- Add deterministic, scenario-driven tests and update cockpit docs to match behavior.

## Files Updated

- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/commands.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py` (new)
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py` (new)
- `/Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py` (new)
- `/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md`
- `/Users/tjenrung/Workspaces/autolab/README.md`

## Tasks

```yaml
id: TUI-APP-001
title: Stabilize selection state, focus defaults, and stale-snapshot safety
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::AutolabCockpitApp
description: >
  Replace index-only selection persistence with stable-key reconciliation (stage name, run_id, todo task_id, artifact path, action_id), remove first-item reset behavior, set deterministic initial focus, and hard-disable unsafe action execution when snapshot refresh fails. Keep mutating flows arm+confirm and auto-disarm after mutating completion.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py
conflict_group: tui_app_core
scope_ok: true
validation:
  - "Pilot scenario: choose first stage when current stage is different, refresh snapshot, selection remains on chosen first stage."
  - "Pilot scenario: force snapshot loader exception, details panes move to unavailable state and Run button is disabled."
  - "Pilot scenario: mutating action completes -> arm state resets OFF and mutating action remains blocked until re-armed."
  - "Pilot scenario: open selected artifact still launches in-TUI viewer by default."
status: Not Completed
```

```yaml
id: TUI-SNAPSHOT-001
title: Harden artifact loading and snapshot determinism
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::load_artifact_text
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::_load_runs
description: >
  Ensure artifact reads never crash the UI on permission/encoding/malformed-content errors, provide readable fallback messages, and make run ordering deterministic across refreshes. Preserve blocker merging and stage artifact semantics.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py
conflict_group: tui_snapshot_core
scope_ok: true
validation:
  - "Unit scenario: missing, binary, unreadable, and malformed JSON artifacts return safe text payloads without raising."
  - "Unit scenario: mixed run manifests produce stable sorted ordering on repeated loads."
  - "Unit scenario: blocker merge behavior remains unchanged for verification + review blockers."
status: Not Completed
```

```yaml
id: TUI-RUNNER-001
title: Make stop lifecycle deterministic and idempotent
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py::CommandRunner
description: >
  Tighten start/stop concurrency behavior, guarantee single completion callback delivery, and keep SIGINT->terminate->kill fallback deterministic under repeated stop requests.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py
conflict_group: tui_runner_core
scope_ok: true
validation:
  - "Unit scenario: starting while already running raises RuntimeError."
  - "Unit scenario: stop() when idle returns False; stop() during active run returns True and exits."
  - "Unit scenario: completion callback is emitted exactly once per process."
status: Not Completed
```

```yaml
id: TUI-ACTIONS-001
title: Tighten intent construction and expected-write accuracy
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_loop_intent
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::_resolve_run_agent_mode
description: >
  Normalize intent construction for deterministic previews and adjust expected-write reporting so loop expectations match selected options (especially auto vs non-auto). Preserve verify as mutating and confirmation-gated.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py
conflict_group: tui_actions_core
scope_ok: true
validation:
  - "Unit scenario: invalid run_agent_mode normalizes to policy."
  - "Unit scenario: loop intent expected_writes reflect auto-mode correctly."
  - "Unit scenario: verify intent remains mutating and includes selected stage arg when provided."
status: Not Completed
```

```yaml
id: CLI-TUI-001
title: Add CLI preflight checks and deterministic exit paths
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py::_cmd_tui
description: >
  Add explicit preflight validation for state-file readability and tail-lines bounds, keep interactive TTY requirement, and ensure predictable non-zero exits with actionable errors while preserving `autolab tui` as the entrypoint.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py
conflict_group: cli_tui_entrypoint
scope_ok: true
validation:
  - "Unit scenario: --tail-lines <= 0 returns exit code 2 with clear error."
  - "Unit scenario: non-interactive stdin/stdout returns exit code 1."
  - "Unit scenario: missing/unreadable state file returns exit code 1 before app launch."
  - "Unit scenario: success path instantiates app with resolved state path and configured tail-lines."
status: Not Completed
```

```yaml
id: TEST-TUI-APP-001
title: Add deterministic app UX behavior tests
depends_on:
  - TUI-APP-001
  - TUI-SNAPSHOT-001
  - TUI-RUNNER-001
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py
description: >
  Add headless/pilot tests that assert selection retention, focus safety defaults, disarm behavior, stop-loop button enablement, refresh-failure safety, and viewer-first artifact opening flow.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py
conflict_group: tests_tui_app
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"
  - "Acceptance: no flaky timing assertions; rely on deterministic injected fixtures/stubs."
status: Not Completed
```

```yaml
id: TEST-TUI-SNAPSHOT-001
title: Expand snapshot edge-case coverage
depends_on:
  - TUI-SNAPSHOT-001
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py
description: >
  Extend snapshot tests to cover artifact read failures, malformed payloads, and deterministic run ordering while retaining existing blocker-merge assertions.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py
conflict_group: tests_tui_snapshot
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py"
  - "Acceptance: all new edge-case scenarios pass without changing existing passing assertions."
status: Not Completed
```

```yaml
id: TEST-TUI-CORE-001
title: Expand action, runner, and CLI tui tests
depends_on:
  - TUI-RUNNER-001
  - TUI-ACTIONS-001
  - CLI-TUI-001
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py
description: >
  Extend action intent tests, add runner lifecycle tests, and add `_cmd_tui` branch coverage tests for preflight and launch paths.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py
conflict_group: tests_tui_core
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py"
  - "Acceptance: verify action remains mutating+arm+confirm in catalog and CLI entrypoint remains `autolab tui`."
status: Not Completed
```

```yaml
id: DOC-TUI-001
title: Update cockpit docs to match hardened behavior
depends_on:
  - TUI-APP-001
  - CLI-TUI-001
location:
  - /Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md
  - /Users/tjenrung/Workspaces/autolab/README.md
description: >
  Document refined selection/focus behavior, refresh-failure safety handling, CLI preflight expectations, and preserved product decisions (viewer default, verify mutating, `autolab tui` entrypoint).
touches:
  - /Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md
  - /Users/tjenrung/Workspaces/autolab/README.md
conflict_group: docs_tui
scope_ok: true
validation:
  - "Docs review: safety model section explicitly states verify is mutating (arm + confirm)."
  - "Docs review: artifact open default remains in-TUI read-only viewer."
  - "Docs review: launch command remains `autolab tui` with current preflight expectations."
status: Not Completed
```

```yaml
id: VAL-TUI-001
title: Run final targeted acceptance sweep
depends_on:
  - TEST-TUI-APP-001
  - TEST-TUI-SNAPSHOT-001
  - TEST-TUI-CORE-001
  - DOC-TUI-001
location:
  - /Users/tjenrung/Workspaces/autolab
description: >
  Execute targeted test and help-smoke suite for TUI stabilization and confirm no regressions to locked product decisions or command surface.
touches: []
conflict_group: validation_sweep
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py"
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_commands_smoke.py::test_top_level_help_groups_commands_for_onboarding"
  - "Run: PYTHONPATH=src python -m autolab tui --help"
status: Not Completed
```

## Parallel Execution Groups

- **Wave 1:** `TUI-APP-001`, `TUI-SNAPSHOT-001`, `TUI-RUNNER-001`, `TUI-ACTIONS-001`, `CLI-TUI-001`
- **Wave 2:** `TEST-TUI-APP-001`, `TEST-TUI-SNAPSHOT-001`, `TEST-TUI-CORE-001`, `DOC-TUI-001`
- **Wave 3:** `VAL-TUI-001`

## Risks and edge cases

- Textual pilot tests can become flaky if timing-based waits are used instead of deterministic fixture injection.
- Stricter `_cmd_tui` preflight (state-file checks) may alter current operator expectations in partially initialized repos.
- Signal handling differences can affect stop-path behavior across macOS/Linux CI runners.
- Permission-error artifact fixtures may be platform-sensitive; fallback test paths should include simulated IO exceptions.

## Rollback or mitigation

- Land Wave 1 changes in isolated commits per file so individual regressions can be reverted surgically.
- If CLI preflight causes unexpected disruption, retain clear error messaging and temporarily gate strict checks behind a conservative fallback branch.
- If runner stop changes introduce instability, roll back `runner.py` to prior behavior while keeping new tests marked and triaged.
- Keep docs synchronized with shipped behavior in the same release to avoid operator mismatch.
