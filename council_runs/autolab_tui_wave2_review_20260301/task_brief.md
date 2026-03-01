# Task Brief: Post-Wave-1 `autolab tui` Review and Improvement Plan

## Intent

Run a second-pass llm-council review after Wave 1 hardening changes to identify the highest-value remaining improvements for Wave 2+ without broad redesign.

## Intake questions (optional, but improve plan quality)

Answers were not provided in this turn; defaults below were assumed.

1. Should Wave 2 prioritize UI interaction safety/integration tests over additional behavior changes?
1. Should we keep external editor open as confirmation-only (no arm requirement), or tighten further?
1. Are we targeting immediate merge readiness (minimal deltas) or broader architectural cleanup?
1. Do we require CI-grade Textual app integration tests now, or staged introduction due potential flakiness?
1. Should docs updates be mandatory in same wave as behavioral changes?
1. Is backward compatibility with existing `autolab tui` workflows strictly required (assumed yes)?

## Assumed intake defaults used for this run

- Prioritize deterministic safety and correctness over feature expansion.
- Preserve locked decisions and current command surface.
- Avoid broad module decomposition/redesign in this pass.
- Keep editor action confirmation-gated (no new arm requirement unless strongly justified).
- Require targeted tests for any behavior hardening.
- Keep docs in sync with behavior changes.

## Current post-Wave-1 implementation context

Wave 1 changes now present in workspace:

- `_cmd_tui` preflight/runtime hardening in `src/autolab/commands.py`
- action intent normalization for loop expected writes in `src/autolab/tui/actions.py`
- snapshot/artifact loading hardening + deterministic run ordering in `src/autolab/tui/snapshot.py`
- runner lifecycle hardening and idempotent stop in `src/autolab/tui/runner.py`
- new/expanded tests:
  - `tests/test_commands_tui.py`
  - `tests/test_tui_runner.py`
  - `tests/test_tui_actions.py`
  - `tests/test_tui_snapshot.py`

Validation currently observed:

- `PYTHONPATH=src pytest -q tests/test_commands_tui.py tests/test_tui_actions.py tests/test_tui_snapshot.py tests/test_tui_runner.py tests/test_commands_smoke.py::test_top_level_help_groups_commands_for_onboarding` passes.
- `PYTHONPATH=src python -m autolab tui --help` passes.
- `python -m compileall -q src/autolab/tui` passes.

## Primary review target for this run

Find improvements still needed after Wave 1, especially around:

1. `AutolabCockpitApp` behavior consistency and selection/focus safety.
1. Fail-closed behavior on snapshot refresh failures.
1. Action dispatch maintainability and confirmation enforcement consistency.
1. Additional test gaps (app-level interaction tests, edge-case regressions).
1. Documentation alignment and operator clarity.

## Files to inspect

- `src/autolab/commands.py`
- `src/autolab/tui/app.py`
- `src/autolab/tui/actions.py`
- `src/autolab/tui/snapshot.py`
- `src/autolab/tui/runner.py`
- `tests/test_commands_tui.py`
- `tests/test_tui_actions.py`
- `tests/test_tui_snapshot.py`
- `tests/test_tui_runner.py`
- `docs/tui_cockpit.md`
- `README.md`
- `docs/quickstart.md`

## Constraints

- Planning only; do not implement.
- Keep scope to TUI + command wiring + docs/tests directly related to this feature.
- Respect locked product decisions:
  - Verify remains mutating and requires arm + confirm.
  - Artifact open defaults to in-TUI read-only viewer.
  - Entrypoint stays `autolab tui`.
  - Textual remains core dependency.

## Planner output format requirements

Use unified `implementation_plan.md` format with sections:

- Overview
- Change Summary
- Files Updated
- Tasks
- Parallel Execution Groups
- Risks and edge cases
- Rollback or mitigation

Each task block must include keys:

- `id`, `title`, `depends_on`, `location`, `description`, `touches`, `conflict_group`, `scope_ok`, `validation`, `status`

Status must be one of:

- `Not Completed`, `Completed`, `In Progress`, `Blocked`

Set `scope_ok: true` for each task.
