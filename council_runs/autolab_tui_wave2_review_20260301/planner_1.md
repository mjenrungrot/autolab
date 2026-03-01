# Overview

This plan targets only post-Wave-1 gaps in `autolab tui` with safety/correctness-first, minimal-delta changes to TUI/CLI/docs/tests.

## Change Summary

- Close fail-open behavior on snapshot refresh errors in the TUI and enforce read-only fail-closed behavior until refresh recovers.
- Fix remaining selection/focus consistency issues (notably stage index `0` being overridden) without redesigning UI structure.
- Tighten action dispatch safety so confirmation/arm requirements are enforced consistently from action metadata.
- Fill targeted regression gaps in CLI/TUI snapshot/runner tests, then sync operator docs to the updated behavior.

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
- `/Users/tjenrung/Workspaces/autolab/README.md`
- `/Users/tjenrung/Workspaces/autolab/docs/quickstart.md`

## Tasks

### Task 1

- id: `TUI-W2-01`
- title: `Close remaining _cmd_tui preflight/runtime test gaps`
- depends_on: `[]`
- location: `/Users/tjenrung/Workspaces/autolab/src/autolab/commands.py` (`_cmd_tui`), `/Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py`
- description: Add regression coverage for untested safety branches (state path is directory, unreadable state file, KeyboardInterrupt path, tail-lines coercion/default edge), and only patch `_cmd_tui` if tests reveal mismatch.
- touches: `["/Users/tjenrung/Workspaces/autolab/src/autolab/commands.py", "/Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py"]`
- conflict_group: `cg_cmd_tui_preflight`
- scope_ok: `true`
- validation: `cd /Users/tjenrung/Workspaces/autolab && PYTHONPATH=src pytest -q tests/test_commands_tui.py`
- status: `Not Completed`

### Task 2

- id: `TUI-W2-02`
- title: `Harden runner completion semantics with edge-case tests`
- depends_on: `[]`
- location: `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py`, `/Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py`
- description: Add tests for natural process completion (`stopped=False`), stop-vs-exit race behavior, and single-callback guarantees; adjust lifecycle code only if a callback/state race is exposed.
- touches: `["/Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py", "/Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py"]`
- conflict_group: `cg_runner_lifecycle`
- scope_ok: `true`
- validation: `cd /Users/tjenrung/Workspaces/autolab && PYTHONPATH=src pytest -q tests/test_tui_runner.py`
- status: `Not Completed`

### Task 3

- id: `TUI-W2-03`
- title: `Expand snapshot safety regression coverage`
- depends_on: `[]`
- location: `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py`, `/Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py`
- description: Add tests for malformed/non-dict JSON payload handling, unknown-stage resilience, run/template artifact edge behavior, and blocker dedupe stability; patch helpers only where behavior is not fail-safe.
- touches: `["/Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py", "/Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py"]`
- conflict_group: `cg_snapshot_hardening`
- scope_ok: `true`
- validation: `cd /Users/tjenrung/Workspaces/autolab && PYTHONPATH=src pytest -q tests/test_tui_snapshot.py`
- status: `Not Completed`

### Task 4

- id: `TUI-W2-04`
- title: `Implement fail-closed snapshot refresh behavior in app`
- depends_on: `["TUI-W2-01", "TUI-W2-03"]`
- location: `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py` (`_refresh_snapshot`, UI refresh/update paths, refresh button handler), `/Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py`
- description: Make snapshot refresh transactional: on refresh failure, clear/invalid snapshot-derived state, keep mutating actions unusable until a successful refresh, and prevent false-success console messaging after failed refresh.
- touches: `["/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py", "/Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"]`
- conflict_group: `cg_app_refresh_fail_closed`
- scope_ok: `true`
- validation: `cd /Users/tjenrung/Workspaces/autolab && PYTHONPATH=src pytest -q tests/test_tui_app.py -k "refresh or fail_closed"`
- status: `Not Completed`

### Task 5

- id: `TUI-W2-05`
- title: `Enforce action confirmation/arm rules from metadata`
- depends_on: `["TUI-W2-04"]`
- location: `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py` (`_handle_action` dispatch path), `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py`, `/Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py`, `/Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py`
- description: Add a minimal dispatch/guard structure that enforces `requires_arm` and `requires_confirmation` consistently from `ActionSpec`, preventing silent policy drift while keeping existing action IDs and command surface unchanged.
- touches: `["/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py", "/Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py", "/Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py", "/Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"]`
- conflict_group: `cg_app_action_policy`
- scope_ok: `true`
- validation: `cd /Users/tjenrung/Workspaces/autolab && PYTHONPATH=src pytest -q tests/test_tui_actions.py tests/test_tui_app.py -k "confirm or arm or dispatch"`
- status: `Not Completed`

### Task 6

- id: `TUI-W2-06`
- title: `Stabilize selection/focus across snapshot refresh`
- depends_on: `["TUI-W2-04"]`
- location: `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py` (`_populate_stage_list`, selection helpers, refresh flow), `/Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py`
- description: Preserve explicit user selection by stable keys (stage/run/artifact) across refresh, remove index-0 forced-jump behavior, and keep deterministic clamping when selected items disappear.
- touches: `["/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py", "/Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"]`
- conflict_group: `cg_app_selection_consistency`
- scope_ok: `true`
- validation: `cd /Users/tjenrung/Workspaces/autolab && PYTHONPATH=src pytest -q tests/test_tui_app.py -k "selection or focus"`
- status: `Not Completed`

### Task 7

- id: `TUI-W2-07`
- title: `Align cockpit/operator docs with hardened behavior`
- depends_on: `["TUI-W2-04", "TUI-W2-05", "TUI-W2-06"]`
- location: `/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md`, `/Users/tjenrung/Workspaces/autolab/README.md`, `/Users/tjenrung/Workspaces/autolab/docs/quickstart.md`
- description: Document fail-closed refresh semantics, selection consistency guarantees, and confirmation/arm enforcement language so operator docs match runtime behavior.
- touches: `["/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md", "/Users/tjenrung/Workspaces/autolab/README.md", "/Users/tjenrung/Workspaces/autolab/docs/quickstart.md"]`
- conflict_group: `cg_tui_docs_alignment`
- scope_ok: `true`
- validation: `cd /Users/tjenrung/Workspaces/autolab && rg -n "tui|refresh|arm|confirm|disarm" docs/tui_cockpit.md README.md docs/quickstart.md`
- status: `Not Completed`

## Parallel Execution Groups

- **Wave 1 (parallel):** `TUI-W2-01`, `TUI-W2-02`, `TUI-W2-03` (no shared touches, unique conflict groups)
- **Wave 2:** `TUI-W2-04`
- **Wave 3:** `TUI-W2-05`
- **Wave 4:** `TUI-W2-06`
- **Wave 5:** `TUI-W2-07`

## Risks and edge cases

- TUI app tests can be timing-sensitive; assertions should avoid brittle timing assumptions and use deterministic state transitions.
- Permission-based CLI tests (`os.access`) can vary by environment; tests should avoid assumptions that require elevated privileges.
- Fail-closed refresh behavior may temporarily reduce usability during transient read errors; UX messaging must make recovery explicit.
- Dispatch hardening may accidentally miss an existing action ID; tests must assert full catalog coverage to prevent regressions.

## Rollback or mitigation

- Land changes in wave order so each wave can be reverted independently if instability appears.
- If fail-closed refresh proves too disruptive, keep disarmed mode plus explicit warning while retaining stale-data execution block.
- If dispatch refactor introduces regressions, revert to previous branch logic but keep the new catalog-coverage tests to guard rework.
- Keep docs updates in the final wave so behavioral rollback does not leave stale operator guidance.
