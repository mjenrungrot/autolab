______________________________________________________________________

## name: swarm-planner description: > [EXPLICIT INVOCATION ONLY] Create dependency-aware Autolab implementation plans optimized for parallel multi-agent execution. metadata: invocation: explicit-only

# Swarm Planner

Use this skill when the user explicitly asks for `$swarm-planner` to create or revise an execution-ready implementation plan.

## Scope

- Produce planning artifacts only. Do not implement code changes.
- Generate plans with explicit dependencies for parallel execution.
- Align plan tasks to the active `.autolab` stage and iteration context.

## Mandatory context pass (read first)

1. Confirm repository root and `.autolab` availability.
1. Read:
   - `.autolab/state.json`
   - `.autolab/verifier_policy.yaml`
   - active-stage prompt in `.autolab/prompts/` when relevant
   - iteration artifacts under `experiments/<iteration_id>/` when available
1. Inspect code paths implicated by the user request before drafting tasks.
1. If a council `final-plan.md` exists in the plan directory (e.g. from `$llm-council`), use it as starting input rather than generating from scratch.
1. Load `.autolab/prompts/rendered/<stage>.context.json` when present. Extract `allowed_edit_dirs` from `runner_scope` and constrain task `location` and `touches` to paths within allowed dirs.

## Clarification policy

- Ask targeted clarifying questions whenever ambiguity changes scope, dependencies, validation, or rollout.
- If the user does not answer, proceed with explicit assumptions and label them in the plan.

## Plan output contract

Write the plan to:

- `experiments/<iteration_id>/implementation_plan.md` when `iteration_id` exists.
- Otherwise `experiments/plan/<topic>/implementation_plan.md`.

Emit `plan_metadata.json` alongside the plan with fields: `schema_version` ("1.0"), `iteration_id`, `generated_at`, `skill_used` ("swarm-planner"), `task_count`. Optional: `wave_count`, `dependency_depth`, `conflict_groups`, `total_touches_count`.

Example `plan_metadata.json`:

```json
{
  "schema_version": "1.0",
  "iteration_id": "iter_001",
  "generated_at": "2026-01-15T10:30:00Z",
  "skill_used": "swarm-planner",
  "task_count": 8,
  "wave_count": 3,
  "dependency_depth": 2,
  "conflict_groups": ["db_schema", "config_files"],
  "total_touches_count": 14
}
```

Every task must include:

- `id`
- `depends_on`
- `location`
- `description`
- `touches` (list of file paths/globs the task edits) -- **required** for wave safety validation. Plans without `touches` produce weaker wave overlap detection.
- `scope_ok` (`true` after confirming task paths are inside `allowed_edit_dirs`)
- `validation`
- `status` (default `Not Completed`)
- `log` (empty placeholder)
- `files edited/created` (empty placeholder)

Optional per task:

- `conflict_group` -- tasks sharing a group must not run in the same wave

Include:

- overview
- dependency graph
- parallel execution wave table
- testing strategy
- risks and mitigations
- assumptions/defaults

## Task design rules

1. All dependencies must be explicit (`depends_on: []` for roots).
1. Tasks must be atomic and independently executable by one agent.
1. Validation must be concrete (commands, checks, or artifact assertions).
1. File paths in `location` must be specific.
1. Sequence tasks to maximize safe parallelism.
1. Each task must include `touches` (list of file paths/globs the task edits).
1. Each task must include `scope_ok: true` only after validating touches/location against `allowed_edit_dirs`.
1. Optional `conflict_group` field: tasks sharing a group must not be in the same wave.
1. Wave grouping must ensure no overlap in `touches` within a wave.

## Subagent review pass (required)

After drafting the plan:

1. Run one review subagent focused on dependency gaps, ordering errors, missing edge cases, and validation holes.
1. Incorporate actionable feedback.
1. Finalize only after review issues are resolved or explicitly justified.

## Plan template

```markdown
# Plan: <task title>

**Generated**: <timestamp>
**Iteration**: <iteration_id or none>
**Stage Context**: <active stage>

## Overview
<summary>

## Dependency Graph
<ascii graph>

## Verifier Outputs
- tests: pass|skip|fail
- dry_run: pass|skip|fail
- schema: pass|skip|fail

## Dry Run
- command:
- status:
- evidence:

## Evidence Paths
- `path/to/log_or_output`

## Change Summary
<concise summary of what this plan changes and why>

## Files Updated
<list of files to be created or modified>

## Tasks

### T1: <name>
- **depends_on**: []
- **location**: <paths>
- **description**: <work>
- **touches**: [<file paths/globs>]
- **scope_ok**: true
- **conflict_group**: <optional>
- **validation**: <checks>
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T2: <name>
- **depends_on**: [T1]
- **location**: <paths>
- **description**: <work>
- **touches**: [<file paths/globs>]
- **scope_ok**: true
- **conflict_group**: <optional>
- **validation**: <checks>
- **status**: Not Completed
- **log**:
- **files edited/created**:

## Parallel Execution Groups
- `wave`: 1; `tasks`: T1; `can_start_when`: Immediately
- `wave`: 2; `tasks`: T2; `can_start_when`: T1 complete

## Testing Strategy
<strategy>

## Risks and Mitigations
<risks>

## Assumptions and Defaults
<assumptions>
```

## Linter Constraints

- `status` must be one of: `Not Completed`, `Completed`, `In Progress`, `Blocked`
- `## Change Summary` section is required by `implementation_plan_lint.py`
- Tasks in the same wave must not have overlapping `touches` paths
- Tasks in the same wave must not share a `conflict_group`
- Circular dependencies will fail validation

## Failure handling

- If plan file path cannot be resolved, state attempted paths and stop for user confirmation.
- If dependency graph is inconsistent, fix before yielding.
- If required context files are missing, continue with documented assumptions and note the gap.
