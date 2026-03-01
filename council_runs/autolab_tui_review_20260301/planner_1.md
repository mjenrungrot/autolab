# Overview

## Change Summary

- Stabilize `autolab tui` for v0.1 with safety-first hardening: stricter preflight/error handling, safer stop semantics, fail-closed UI behavior, and stronger accidental-mutation guards.
- Preserve locked product decisions: `autolab tui` entrypoint stays, Textual remains core, verify remains mutating (arm + confirm), and artifact open default remains in-TUI read-only viewer.
- Expand focused regression coverage for runner lifecycle, action safety metadata, snapshot robustness, and app-level safety flows.

## Files Updated

- `/Users/tjenrung/Workspaces/autolab/src/autolab/commands.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_commands_smoke.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py` (new)
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py` (new)
- `/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md`
- `/Users/tjenrung/Workspaces/autolab/README.md`
- `/Users/tjenrung/Workspaces/autolab/docs/quickstart.md`

## Tasks

```yaml
id: TUI-SAFE-01
title: Harden `autolab tui` CLI preflight and failure exits
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_commands_smoke.py
description: >
  Strengthen `_cmd_tui` argument/runtime guards by validating tail-lines and state-file usability before launch, preserving TTY gating, and wrapping app startup/run errors with stable non-zero exits and operator-friendly diagnostics.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_commands_smoke.py
conflict_group: cli-preflight
scope_ok: true
validation:
  - PYTHONPATH=src pytest -q tests/test_commands_smoke.py -k "tui or help_groups"
  - PYTHONPATH=src python -m autolab tui --help
status: Not Completed
```

```yaml
id: TUI-SAFE-02
title: Make command stop semantics process-group safe
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py
description: >
  Update `CommandRunner` to launch subprocesses in an isolated process group/session and apply interrupt->terminate->kill escalation to the whole group with bounded waits; keep stop idempotent and ensure stopped-state reporting is race-safe.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py
conflict_group: runner-stop
scope_ok: true
validation:
  - PYTHONPATH=src pytest -q tests/test_tui_runner.py
status: Not Completed
```

```yaml
id: TUI-SAFE-03
title: Tighten accidental-mutation boundaries for editor launch
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py
description: >
  Reclassify external editor launch as an explicitly risky action (arm + confirm required), keep in-TUI viewer as the default artifact open path, and retain verify as mutating; align action metadata tests with the hardened safety contract.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py
conflict_group: action-safety
scope_ok: true
validation:
  - PYTHONPATH=src pytest -q tests/test_tui_actions.py
status: Not Completed
```

```yaml
id: TUI-SAFE-04
title: Harden snapshot/artifact loading for corrupt and large inputs
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py
description: >
  Make artifact reads bounded and resilient (large files, unreadable files, malformed JSON, missing optional files) so viewer/snapshot paths fail safely without crashing or excessive memory use.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py
conflict_group: snapshot-robustness
scope_ok: true
validation:
  - PYTHONPATH=src pytest -q tests/test_tui_snapshot.py
status: Not Completed
```

```yaml
id: TUI-SAFE-05
title: Enforce app-level safety state machine and target stability
depends_on:
  - TUI-SAFE-02
  - TUI-SAFE-03
  - TUI-SAFE-04
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py
description: >
  Add guarded quit behavior while commands are active, fail-closed UI behavior on snapshot refresh failure (clear stale state + disarm), stable selection mapping across refresh to prevent wrong-target actions, and centralized confirmation enforcement from action metadata.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py
conflict_group: app-safety
scope_ok: true
validation:
  - PYTHONPATH=src pytest -q tests/test_tui_app.py
  - PYTHONPATH=src pytest -q tests/test_tui_actions.py tests/test_tui_snapshot.py tests/test_tui_runner.py
status: Not Completed
```

```yaml
id: TUI-SAFE-06
title: Sync cockpit docs with stabilized safety behavior
depends_on:
  - TUI-SAFE-01
  - TUI-SAFE-03
  - TUI-SAFE-05
location:
  - /Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md
  - /Users/tjenrung/Workspaces/autolab/README.md
  - /Users/tjenrung/Workspaces/autolab/docs/quickstart.md
description: >
  Update operator docs to reflect arm/confirm behavior changes, stop/quit lifecycle semantics, and unchanged product invariants so onboarding and runtime expectations match v0.1 stabilization behavior.
touches:
  - /Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md
  - /Users/tjenrung/Workspaces/autolab/README.md
  - /Users/tjenrung/Workspaces/autolab/docs/quickstart.md
conflict_group: docs-sync
scope_ok: true
validation:
  - rg -n "autolab tui|Arm actions|Open selected artifact|Stop loop|read-only" docs/tui_cockpit.md README.md docs/quickstart.md
status: Not Completed
```

## Parallel Execution Groups

- **Wave 1:** `TUI-SAFE-01`, `TUI-SAFE-02`, `TUI-SAFE-03`, `TUI-SAFE-04`
- **Wave 2:** `TUI-SAFE-05`
- **Wave 3:** `TUI-SAFE-06`

## Risks and edge cases

- Process-group signal behavior can vary by platform; stop semantics need portable fallback paths.
- Requiring arm for editor launch is a behavior change that may surprise users accustomed to quick-open.
- Fail-closed snapshot handling may temporarily hide stale-but-readable data during transient filesystem issues.
- Textual app interaction tests can be sensitive in headless CI environments.

## Rollback or mitigation

- Land each task as an isolated patch set so safety regressions can be reverted per task without full rollback.
- Keep runner escalation fallback to current single-process termination if process-group operations are unsupported.
- If editor-gating friction is too high, retain explicit mutation warning + confirm while deferring arm requirement.
- If app-level test flakiness appears, keep deterministic unit coverage (actions/snapshot/runner) as release gate and quarantine flaky app tests until stabilized.
