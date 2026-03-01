# Overview

This merged Wave-2 plan keeps `autolab tui` product decisions locked while applying minimal-delta hardening after Wave 1: fail-closed refresh behavior, deterministic selection persistence, consistent action safety enforcement, deterministic snapshot/runner handling, and focused test/doc updates.

## Change Summary

- Add deterministic regression tests first for CLI, actions, snapshot, runner, and app-level safety flows.
- Apply only targeted source changes needed to satisfy those regressions and preserve existing command surface.
- Enforce refresh failure fail-closed behavior in the TUI app (disarm, clear unsafe state, block mutating actions until recovery).
- Keep safety contracts unchanged: verify remains mutating + arm+confirm; external editor remains confirmation-gated; default artifact open remains in-TUI viewer; no new hotkey requirement.
- Finish with focused docs alignment and a deterministic validation gate.

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
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py` (new or expanded)
- `/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md`
- `/Users/tjenrung/Workspaces/autolab/docs/quickstart.md`
- `/Users/tjenrung/Workspaces/autolab/README.md`

## Tasks

```yaml
id: W2-TST-CLI-001
title: Add deterministic `_cmd_tui` branch regressions
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py::_cmd_tui
description: >
  Add focused tests for remaining CLI safety branches: state path exists but is not a file,
  unreadable state file handling, KeyboardInterrupt exit-code behavior (130), and deterministic
  tail-lines coercion/default handling without altering `autolab tui` command surface.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py
conflict_group: cg_w2_tests_cli
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py"
status: Not Completed
```

```yaml
id: W2-TST-ACTIONS-001
title: Add action safety metadata invariants
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py
description: >
  Add deterministic tests for ActionSpec safety contracts and intent normalization boundaries:
  verify remains mutating with arm+confirm, external editor remains confirmation-gated without arm,
  and run-agent mode normalization/fallback stays deterministic.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py
conflict_group: cg_w2_tests_actions
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py"
status: Not Completed
```

```yaml
id: W2-TST-SNAPSHOT-001
title: Add snapshot/artifact deterministic edge tests
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py
description: >
  Add focused regression tests for malformed/non-dict snapshot payloads, placeholder and prompt-path
  resolution behavior, binary/read-error artifact loading, deterministic run ordering, and blocker dedupe stability.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py
conflict_group: cg_w2_tests_snapshot
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py"
status: Not Completed
```

```yaml
id: W2-TST-RUNNER-001
title: Add runner lifecycle race and completion tests
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py::CommandRunner
description: >
  Add deterministic tests for natural completion (`stopped=False`), stop-vs-exit race handling,
  output line forwarding order, and exactly-once done-callback guarantees.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py
conflict_group: cg_w2_tests_runner
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py"
status: Not Completed
```

```yaml
id: W2-TST-APP-001
title: Add app-level fail-closed and selection safety tests
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::AutolabCockpitApp
description: >
  Add deterministic headless app tests for transactional refresh fail-closed behavior (clear unsafe
  snapshot state, force disarm, disable mutating paths), no false success messaging on failed refresh,
  explicit selection persistence including index 0 via stable keys, confirmation+arm gating, and post-mutation auto-disarm.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py
conflict_group: cg_w2_tests_app
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"
status: Not Completed
```

```yaml
id: W2-SRC-CLI-001
title: Align `_cmd_tui` behavior to deterministic regressions
depends_on:
  - W2-TST-CLI-001
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py::_cmd_tui
description: >
  Apply minimal CLI preflight/runtime fixes required by tests so error branches are deterministic,
  user-facing, and fail-safe without adding new flags or changing `autolab tui` entrypoint behavior.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py
conflict_group: cg_w2_src_cli
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py"
status: Not Completed
```

```yaml
id: W2-SRC-ACTIONS-001
title: Harden action catalog safety contract consistency
depends_on:
  - W2-TST-ACTIONS-001
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::ACTION_CATALOG
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_verify_intent
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_open_in_editor_intent
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_run_intent
description: >
  Apply only intent/catalog updates needed to keep safety metadata and argv construction deterministic:
  verify remains mutating with arm+confirm, editor open remains confirmation-only, and no new hotkey behavior is introduced.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py
conflict_group: cg_w2_src_actions
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py"
status: Not Completed
```

```yaml
id: W2-SRC-SNAPSHOT-001
title: Preserve deterministic snapshot and artifact semantics
depends_on:
  - W2-TST-SNAPSHOT-001
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::load_cockpit_snapshot
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::load_artifact_text
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::resolve_stage_prompt_path
description: >
  Make targeted fixes uncovered by tests so malformed data degrades safely, artifact read failures are fail-safe,
  and ordering/dedupe behavior remains deterministic without redesign.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py
conflict_group: cg_w2_src_snapshot
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py"
status: Not Completed
```

```yaml
id: W2-SRC-RUNNER-001
title: Finalize deterministic runner completion semantics
depends_on:
  - W2-TST-RUNNER-001
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py::CommandRunner
description: >
  Apply minimal lifecycle changes required by tests so stop/complete transitions remain idempotent,
  `stopped` reporting is correct on natural completion, and done-callback emission is exactly once.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py
conflict_group: cg_w2_src_runner
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py"
status: Not Completed
```

```yaml
id: W2-SRC-APP-001
title: Enforce transactional fail-closed app refresh and dispatch safety
depends_on:
  - W2-TST-APP-001
  - W2-SRC-ACTIONS-001
  - W2-SRC-SNAPSHOT-001
  - W2-SRC-RUNNER-001
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::_refresh_snapshot
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::_populate_stage_list
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::_update_action_button_state
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::_handle_action
description: >
  Implement transactional refresh behavior: on refresh failure, clear stale snapshot-dependent state,
  force disarm, and keep mutating actions blocked until successful refresh; preserve explicit stage selection
  by stable keys (including index 0), remove false-success messaging, and enforce metadata-driven arm/confirm dispatch.
  Keep default in-TUI viewer behavior and external editor confirmation-gate unchanged.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py
conflict_group: cg_w2_src_app
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py -k 'confirm or arm'"
status: Not Completed
```

```yaml
id: W2-DOCS-001
title: Sync operator docs with Wave-2 safety behavior
depends_on:
  - W2-SRC-CLI-001
  - W2-SRC-ACTIONS-001
  - W2-SRC-SNAPSHOT-001
  - W2-SRC-RUNNER-001
  - W2-SRC-APP-001
location:
  - /Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md
  - /Users/tjenrung/Workspaces/autolab/docs/quickstart.md
  - /Users/tjenrung/Workspaces/autolab/README.md
description: >
  Update docs to match runtime behavior: fail-closed refresh expectations, selection persistence semantics,
  unchanged `autolab tui` entrypoint, verify mutating arm+confirm flow, default in-TUI viewer behavior, and external editor confirmation gate.
touches:
  - /Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md
  - /Users/tjenrung/Workspaces/autolab/docs/quickstart.md
  - /Users/tjenrung/Workspaces/autolab/README.md
conflict_group: cg_w2_docs
scope_ok: true
validation:
  - "Docs review: safety model and action behavior match implemented app/actions semantics."
status: Not Completed
```

```yaml
id: W2-VAL-001
title: Run focused Wave-2 deterministic validation gate
depends_on:
  - W2-TST-CLI-001
  - W2-TST-ACTIONS-001
  - W2-TST-SNAPSHOT-001
  - W2-TST-RUNNER-001
  - W2-TST-APP-001
  - W2-SRC-CLI-001
  - W2-SRC-ACTIONS-001
  - W2-SRC-SNAPSHOT-001
  - W2-SRC-RUNNER-001
  - W2-SRC-APP-001
  - W2-DOCS-001
location:
  - /Users/tjenrung/Workspaces/autolab
description: >
  Execute a deterministic, feature-scoped validation matrix before merge to confirm Wave-2 hardening
  without broad regression blast radius.
touches: []
conflict_group: cg_w2_validation
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"
  - "Run: PYTHONPATH=src python -m autolab tui --help"
  - "Run: python -m compileall -q /Users/tjenrung/Workspaces/autolab/src/autolab/tui"
status: Not Completed
```

## Parallel Execution Groups

- **Wave 1 (parallel, tests-first):** `W2-TST-CLI-001`, `W2-TST-ACTIONS-001`, `W2-TST-SNAPSHOT-001`, `W2-TST-RUNNER-001`, `W2-TST-APP-001`
- **Wave 2 (parallel, source modules):** `W2-SRC-CLI-001`, `W2-SRC-ACTIONS-001`, `W2-SRC-SNAPSHOT-001`, `W2-SRC-RUNNER-001`
- **Wave 3 (sequential app integration):** `W2-SRC-APP-001`
- **Wave 4 (docs):** `W2-DOCS-001`
- **Wave 5 (gate):** `W2-VAL-001`

## Risks and edge cases

- Headless Textual app tests can become timing-sensitive; use deterministic fixtures and avoid wall-clock sleeps.
- Fail-closed refresh may temporarily block actions during transient read failures; retry guidance and messaging must stay explicit.
- Selection reconciliation must preserve intentional first-item choice (index `0`) while handling deleted/missing items safely.
- Runner stop/exit races can vary by platform timing; assertions should target guarantees (single callback, correct stopped flag) rather than exact timing.
- Snapshot coercion fixes must not hide serious data issues; user-visible failure context should remain available.

## Rollback or mitigation

- Keep commits/task batches wave-scoped so rollback can be done per module (`commands`, `actions`, `snapshot`, `runner`, `app`, `docs`).
- If fail-closed refresh is too disruptive, retain disarm + mutating-action disablement while improving recovery messaging before relaxing anything else.
- If app-level tests are flaky in CI, keep deterministic unit assertions as required gates and quarantine only unstable integration-style checks.
- If docs drift from behavior during rollback, treat `/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md` as source of truth and resync `README.md`/`docs/quickstart.md`.
