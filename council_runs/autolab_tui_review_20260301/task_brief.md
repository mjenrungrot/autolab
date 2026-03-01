# Task Brief: Autolab TUI Cockpit Review and Improvement Plan

## Intent

Review the recently implemented `autolab tui` Textual cockpit and produce a robust, decision-complete improvement plan that strengthens reliability, UX safety guarantees, test coverage, maintainability, and release readiness.

## Intake (answers optional, assumed for this run)

These answers were not explicitly provided by user and are assumed for this council run:

- Scope: keep architecture and command surface (`autolab tui`) as implemented; propose focused improvements rather than redesign.
- Backward compatibility: no breaking changes to existing CLI commands or workflows.
- Release target: near-term incremental hardening (v0.1 stabilization), not feature expansion beyond current scope.
- Risk tolerance: safety-first for mutating actions; avoid accidental execution.
- UI preference: selection-first, button/modal driven interactions remain primary.

## User-requested constraints

- Use llm-council style planning review.
- Focus on reviewing current implementation and checking for improvements.
- Verify currently locked product decisions remain respected:
  - Verify is mutating (arm + confirm)
  - Open artifact defaults to in-TUI read-only viewer
  - Entrypoint is `autolab tui`
  - Textual is a core dependency

## Current implementation context (from repo)

Implemented/modified files include:

- `src/autolab/commands.py` (new `_cmd_tui`, parser wiring, help grouping)
- `src/autolab/__main__.py` (exports `_cmd_tui`)
- `pyproject.toml` (adds `textual>=0.63.0`)
- New package: `src/autolab/tui/` with
  - `app.py`
  - `actions.py`
  - `snapshot.py`
  - `runner.py`
  - `models.py`
  - `__init__.py`
- Docs updates:
  - `README.md`
  - `docs/quickstart.md`
  - `docs/tui_cockpit.md`
- Tests:
  - `tests/test_commands_smoke.py`
  - `tests/test_tui_actions.py`
  - `tests/test_tui_snapshot.py`

## Validation status already observed

- `PYTHONPATH=src pytest -q tests/test_commands_smoke.py::test_top_level_help_groups_commands_for_onboarding tests/test_tui_actions.py tests/test_tui_snapshot.py` passes.
- `python -m compileall -q src/autolab/tui` passes.
- `PYTHONPATH=src python -m autolab tui --help` renders expected CLI help.

## Known caveats observed

- Full suite includes one unrelated pre-existing failure (`.DS_Store` fixture contract) in `test_packaged_golden_iteration_fixture_contract`.
- Runtime Textual smoke not executed in this environment due missing installed `textual` package in the active interpreter.

## Review goals

1. Identify correctness or safety gaps in command execution, arming/disarming, and stop behavior.
1. Identify UX edge cases in selection synchronization and detail panes.
1. Identify robustness issues in snapshot loading with missing/corrupt artifacts.
1. Identify test coverage gaps and propose concrete additions.
1. Suggest maintainability refactors that do not overcomplicate v0.1.
1. Produce a concrete implementation plan with file-level tasks and wave-safe parallelization metadata.

## Output format requirements for planners and judge

Use unified `implementation_plan.md` format with:

- Overview
- Change Summary
- Files Updated
- Tasks (must include: `depends_on`, `location`, `description`, `touches`, `conflict_group`, `scope_ok`, `validation`, `status`)
- Parallel Execution Groups
- Risks and edge cases
- Rollback or mitigation

## Non-goals

- Do not propose replacing Textual or changing product-level interaction model.
- Do not propose broad unrelated refactors outside TUI + parser/docs/tests surface.
- Do not execute any code changes in this council phase.
