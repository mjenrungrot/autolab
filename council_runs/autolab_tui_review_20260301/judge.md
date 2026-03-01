# Judge Report: autolab_tui_review_20260301

## Scoring table

| Candidate | coverage | feasibility | risk handling | test completeness | clarity/actionability | conciseness | parallelizability | conflict_risk | plan_quality_alignment | total (/90) |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| candidate_1 | 9 | 9 | 8 | 9 | 9 | 7 | 9 | 9 | 9 | 78 |
| candidate_2 | 8 | 8 | 8 | 8 | 8 | 8 | 8 | 7 | 8 | 71 |
| candidate_3 | 7 | 6 | 6 | 7 | 7 | 6 | 7 | 7 | 5 | 58 |

## Comparative analysis

- **candidate_1** is strongest overall: highest coverage of known caveats, explicit locked-decision preservation, concrete tests, and clean wave structure with low touch overlap.
- **candidate_2** is solid and concise, with good stop-safety and fail-closed emphasis, but mixes code+tests in the same tasks more often and introduces a behavior change (editor launch arming) that is not clearly required by locked decisions.
- **candidate_3** has useful maintainability ideas, but it is over-scoped for this run (new modules + architecture decomposition) and conflicts with the brief’s “focused hardening, not redesign” constraint.
- Winner basis: candidate_1 as backbone, augmented by candidate_2’s strongest elements (portable runner stop hardening, quickstart doc sync) while explicitly rejecting broad refactor scope from candidate_3.

## Missing steps and contradictions

### Missing steps across candidates

- Explicit `_cmd_tui` test coverage for Textual import/startup failure path and state-path readability preflight was not consistently specified as a dedicated test file.
- Cross-platform fallback expectations for runner escalation were noted as risks but not always encoded as concrete acceptance criteria.
- Validation matrix did not consistently include compile-time sanity (`compileall`) alongside targeted pytest and CLI help smoke.
- Scope guardrail (“no broad architecture decomposition in this pass”) was implicit, not explicit, in some candidates.

### Contradictions

- **Editor action safety**: candidate_2 proposes escalating external editor launch to arm+confirm; candidate_1 keeps it confirmation-gated view behavior. This is a user-facing behavior change and should be deferred unless explicitly requested.
- **Refactor breadth**: candidate_3 introduces multi-file architectural extraction (`cli.py`, `screens.py`, `presenters.py`, `action_handlers.py`), contradicting the stabilization-only scope.
- **Dependency sequencing style**: candidate_3’s deeper sequential chain reduces near-term delivery velocity compared with candidate_1’s wider safe parallel waves.

## Merged final plan

# Overview

Focused v0.1 hardening plan for the existing `autolab tui` Textual cockpit: improve safety/correctness under edge cases, keep locked product decisions unchanged, and add deterministic regression coverage without broad architectural redesign.

## Change Summary

- Harden command preflight, snapshot loading, runner stop semantics, and action intent construction.
- Stabilize app selection/focus behavior and fail-closed handling when refresh or artifacts fail.
- Preserve locked decisions: `autolab tui` entrypoint, Textual-based cockpit, verify as mutating (arm+confirm), and in-TUI viewer as default artifact open path.
- Add focused tests and docs updates for release readiness.

## Files Updated

- `/Users/tjenrung/Workspaces/autolab/src/autolab/commands.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py`
- `/Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py` (new)
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py`
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py` (new)
- `/Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py` (new)
- `/Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md`
- `/Users/tjenrung/Workspaces/autolab/docs/quickstart.md`
- `/Users/tjenrung/Workspaces/autolab/README.md`

## Tasks

```yaml
id: TUI-CLI-001
title: Harden `autolab tui` preflight and failure paths
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py::_cmd_tui
description: >
  Validate `--tail-lines`, enforce interactive TTY checks, fail early on unreadable/missing state paths,
  and keep deterministic non-zero exits for Textual import/runtime failures while preserving `autolab tui`
  as the entrypoint.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/commands.py
conflict_group: tui_cli_core
scope_ok: true
validation:
  - "Unit scenario: --tail-lines <= 0 returns exit code 2 with actionable error."
  - "Unit scenario: non-interactive stdin/stdout returns exit code 1."
  - "Unit scenario: missing/unreadable state path fails before app launch."
  - "Unit scenario: Textual import/app startup exceptions return exit code 1."
status: Not Completed
```

```yaml
id: TUI-SNAPSHOT-001
title: Harden snapshot and artifact loading determinism
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::_load_runs
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::load_artifact_text
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py::load_cockpit_snapshot
description: >
  Make run ordering stable across refreshes and ensure artifact reads handle missing, unreadable,
  malformed, or binary inputs without crashing. Preserve existing blocker merge semantics and stage
  artifact expectations.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/snapshot.py
conflict_group: tui_snapshot_core
scope_ok: true
validation:
  - "Unit scenario: missing/binary/unreadable artifacts return safe viewer text without raising."
  - "Unit scenario: malformed JSON artifact falls back safely and remains readable."
  - "Unit scenario: repeated snapshot loads produce deterministic run ordering."
  - "Unit scenario: verification+review blocker merge remains unchanged."
status: Not Completed
```

```yaml
id: TUI-RUNNER-001
title: Make runner stop lifecycle deterministic and idempotent
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py::CommandRunner
description: >
  Guarantee single completion callback delivery, maintain race-safe running state, and harden
  stop escalation (`SIGINT` -> terminate -> kill) with idempotent behavior under repeated stop requests.
  Use portable fallbacks where process-group controls are unavailable.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/runner.py
conflict_group: tui_runner_core
scope_ok: true
validation:
  - "Unit scenario: start() while active raises RuntimeError."
  - "Unit scenario: stop() when idle returns False."
  - "Unit scenario: repeated stop() during active command returns True and exits cleanly."
  - "Unit scenario: on_done callback fires exactly once per process."
status: Not Completed
```

```yaml
id: TUI-ACTIONS-001
title: Normalize intent construction and safety metadata
depends_on: []
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::ACTION_CATALOG
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::_resolve_run_agent_mode
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py::build_loop_intent
description: >
  Keep action metadata and intent assembly deterministic, including invalid run-agent mode fallback and
  expected-writes accuracy for loop options. Preserve locked safety behavior: verify remains mutating
  with arm+confirm; default artifact open remains in-TUI viewer; editor launch remains confirmation-gated.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/actions.py
conflict_group: tui_actions_core
scope_ok: true
validation:
  - "Unit scenario: invalid run_agent_mode normalizes to policy."
  - "Unit scenario: loop intent expected_writes remain accurate in auto/non-auto paths."
  - "Unit scenario: verify action metadata remains mutating + requires_arm + requires_confirmation."
status: Not Completed
```

```yaml
id: TUI-APP-001
title: Stabilize app selection state and fail-closed refresh behavior
depends_on:
  - TUI-SNAPSHOT-001
  - TUI-RUNNER-001
  - TUI-ACTIONS-001
location:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py::AutolabCockpitApp
description: >
  Replace index-only retention with stable-key reconciliation for stage/run/todo/artifact/action selections,
  preserve intentional index `0` selections across refreshes, set deterministic focus defaults, and clear or
  disable unsafe UI state when snapshot refresh fails. Keep mutating completion auto-disarm behavior.
touches:
  - /Users/tjenrung/Workspaces/autolab/src/autolab/tui/app.py
conflict_group: tui_app_core
scope_ok: true
validation:
  - "Pilot scenario: selecting first stage explicitly survives refresh and does not jump to current stage."
  - "Pilot scenario: refresh exception disables unsafe run path and surfaces failure clearly."
  - "Pilot scenario: stop-loop button only enables while run_loop is active."
  - "Pilot scenario: mutating action completion auto-disarms cockpit."
status: Not Completed
```

```yaml
id: TEST-TUI-CLI-001
title: Add `_cmd_tui` branch coverage tests
depends_on:
  - TUI-CLI-001
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py
description: >
  Add focused CLI tests for preflight and launch paths: tail-lines bounds, TTY requirement,
  state-path failures, Textual import/startup failure handling, and successful app wiring.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py
conflict_group: tests_tui_cli
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py"
status: Not Completed
```

```yaml
id: TEST-TUI-SNAPSHOT-001
title: Expand snapshot edge-case regression tests
depends_on:
  - TUI-SNAPSHOT-001
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py
description: >
  Extend snapshot tests to cover artifact read failures, malformed JSON, deterministic run ordering,
  and preservation of blocker-merging behavior.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py
conflict_group: tests_tui_snapshot
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py"
status: Not Completed
```

```yaml
id: TEST-TUI-RUNNER-001
title: Add deterministic runner lifecycle tests
depends_on:
  - TUI-RUNNER-001
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py
description: >
  Add unit tests for active-start rejection, idle stop behavior, repeated stop idempotency,
  escalation path handling, and single-callback completion guarantees.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py
conflict_group: tests_tui_runner
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py"
status: Not Completed
```

```yaml
id: TEST-TUI-ACTIONS-001
title: Extend action-intent safety tests
depends_on:
  - TUI-ACTIONS-001
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py
description: >
  Extend intent tests for run-agent mode normalization, loop expected-write consistency,
  and locked safety contract assertions for verify and viewer/editor defaults.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py
conflict_group: tests_tui_actions
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py"
status: Not Completed
```

```yaml
id: TEST-TUI-APP-001
title: Add app-level safety and selection integration tests
depends_on:
  - TUI-APP-001
location:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py
description: >
  Add deterministic Textual/headless tests for selection retention, refresh-failure fail-closed behavior,
  stop-loop enablement semantics, viewer-first artifact opening, and post-mutation auto-disarm.
touches:
  - /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py
conflict_group: tests_tui_app
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"
status: Not Completed
```

```yaml
id: DOC-TUI-001
title: Sync TUI documentation with stabilized behavior
depends_on:
  - TUI-CLI-001
  - TUI-ACTIONS-001
  - TUI-APP-001
location:
  - /Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md
  - /Users/tjenrung/Workspaces/autolab/docs/quickstart.md
  - /Users/tjenrung/Workspaces/autolab/README.md
description: >
  Update docs to match hardened preflight, refresh safety behavior, and locked action safety model
  without changing command surface.
touches:
  - /Users/tjenrung/Workspaces/autolab/docs/tui_cockpit.md
  - /Users/tjenrung/Workspaces/autolab/docs/quickstart.md
  - /Users/tjenrung/Workspaces/autolab/README.md
conflict_group: docs_tui
scope_ok: true
validation:
  - "Docs review: verify remains explicitly documented as mutating (arm + confirm)."
  - "Docs review: default artifact path remains in-TUI read-only viewer."
  - "Docs review: launch command remains `autolab tui`."
status: Not Completed
```

```yaml
id: VAL-TUI-001
title: Run targeted release-readiness validation sweep
depends_on:
  - TEST-TUI-CLI-001
  - TEST-TUI-SNAPSHOT-001
  - TEST-TUI-RUNNER-001
  - TEST-TUI-ACTIONS-001
  - TEST-TUI-APP-001
  - DOC-TUI-001
location:
  - /Users/tjenrung/Workspaces/autolab
description: >
  Execute focused TUI test matrix and command/help smoke checks to verify no regressions in locked
  product decisions or CLI behavior.
touches: []
conflict_group: validation_sweep
scope_ok: true
validation:
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_commands_tui.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_actions.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_snapshot.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_runner.py /Users/tjenrung/Workspaces/autolab/tests/test_tui_app.py"
  - "Run: PYTHONPATH=src pytest -q /Users/tjenrung/Workspaces/autolab/tests/test_commands_smoke.py::test_top_level_help_groups_commands_for_onboarding"
  - "Run: PYTHONPATH=src python -m autolab tui --help"
  - "Run: python -m compileall -q /Users/tjenrung/Workspaces/autolab/src/autolab/tui"
status: Not Completed
```

## Parallel Execution Groups

- **Wave 1:** `TUI-CLI-001`, `TUI-SNAPSHOT-001`, `TUI-RUNNER-001`, `TUI-ACTIONS-001`
- **Wave 2:** `TUI-APP-001`, `TEST-TUI-CLI-001`, `TEST-TUI-SNAPSHOT-001`, `TEST-TUI-RUNNER-001`, `TEST-TUI-ACTIONS-001`
- **Wave 3:** `TEST-TUI-APP-001`, `DOC-TUI-001`
- **Wave 4:** `VAL-TUI-001`

## Risks and edge cases

- Textual integration tests can be timing-sensitive in CI; deterministic fixtures/stubs are required.
- Process/signal behavior may differ across macOS/Linux; stop-path tests must cover fallback branches.
- Stricter CLI preflight may change expectations in partially initialized repos.
- Fail-closed refresh handling can hide stale details during transient filesystem errors; UX messaging must stay clear.
- Artifact IO edge cases (permission, encoding, huge files) must remain non-crashing while keeping useful diagnostics.

## Rollback or mitigation

- Land each Wave 1 hardening task independently to allow surgical reverts.
- If runner escalation is unstable on a platform, keep prior stop behavior behind portable fallback while retaining tests.
- If fail-closed refresh proves too aggressive, keep disarm+disable safeguards but show last-known snapshot as explicitly stale.
- If new app-level tests are flaky, quarantine only flaky integration cases while preserving deterministic unit coverage gates.
- Keep docs updates in the same release unit as behavior changes to avoid operator mismatch.
