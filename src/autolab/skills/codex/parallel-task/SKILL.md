---
name: parallel-task
description: >
  [EXPLICIT INVOCATION ONLY] Execute dependency-aware implementation plans in parallel subagent waves with Autolab-aware logging.
metadata:
  invocation: explicit-only
---

# Parallel Task

Use this skill when the user explicitly asks for `$parallel-task` to execute a plan file.

## Scope

- Execute an existing implementation plan; do not author a new plan.
- Run eligible tasks in dependency waves using parallel subagents.
- Keep task state, logs, and file lists updated in the plan file.

## Inputs

- `plan_file` (required)
- `task_ids` subset (optional)

If no `task_ids` are provided, execute the full plan.

## Preflight checks

1. Confirm `plan_file` exists and is readable.
2. Parse task blocks (for example `### T1: ...`).
3. For each task, extract:
   - ID and name
   - `depends_on` list
   - `location`, `description`, `validation`
   - `touches`, `scope_ok`
   - `status`, `log`, `files edited/created`
4. Validate unique IDs and dependency references.
5. If `.autolab/state.json` exists, load iteration and stage context for prompts.
6. Load `.autolab/prompts/rendered/<stage>.context.json` when present. Extract `allowed_edit_dirs` from `runner_scope` and verify each task's `touches` are within allowed scope before launching subagents.
7. Fail preflight if any task omits `touches` or `scope_ok`, or if `scope_ok` is not set to true.

## Subset execution rules

- If task subset is requested, include all transitive dependencies.
- If a requested task ID is missing, stop and list available task IDs.

## Execution loop

1. Identify unblocked tasks:
   - task not completed
   - all `depends_on` tasks completed
2. Extract `touches` and `conflict_group` from each unblocked task during preflight.
3. When building a wave, exclude tasks whose `touches` overlap or share a `conflict_group` with already-selected tasks in the wave.
4. Launch all wave tasks in parallel subagents.
5. Wait for completion, collect outputs, and validate results.
6. Mark task as complete only when plan updates are present:
   - `status: Completed`
   - non-empty `log`
   - non-empty `files edited/created`
7. If a task fails validation, retry once; otherwise report blocked status.
8. Repeat until no pending tasks remain.

## Subagent prompt contract

Each task prompt must include:

- plan file path
- iteration/stage context (if available)
- full task section
- acceptance/validation requirements

Instruction requirements for subagent:

1. Read affected files before editing.
2. Implement only the assigned task.
3. Run validation when feasible.
4. Update the task block in `plan_file` immediately after completion.
5. Return a concise summary of files changed, validation run, and residual risks.

## Guardrails

- Do not force per-task git commits.
- Do not stage unrelated files.
- Delegate commit workflow to `$commit` when requested.

## Failure handling

- Parse failure: show parse attempt and expected heading format.
- Dependency cycle: report cycle and stop.
- No unblocked tasks while pending tasks remain: report unresolved blockers.

## Execution summary

Return:

- tasks completed
- tasks failed/blocked with reasons
- plan file path
- validation coverage summary

Emit `plan_execution_summary.json` alongside the plan with fields: `schema_version` ("1.0"), `iteration_id`, `plan_file`, `tasks_total`, `tasks_completed`, `tasks_failed`, `tasks_blocked`. Optional: `waves_executed`, `task_details[]`.

Example `plan_execution_summary.json`:

```json
{
  "schema_version": "1.0",
  "iteration_id": "iter_001",
  "plan_file": "experiments/iter_001/implementation_plan.md",
  "tasks_total": 8,
  "tasks_completed": 7,
  "tasks_failed": 1,
  "tasks_blocked": 0,
  "waves_executed": 3,
  "task_details": [
    {"id": "T1", "status": "Completed", "wave": 1},
    {"id": "T2", "status": "Completed", "wave": 1},
    {"id": "T3", "status": "Failed", "wave": 2, "error": "validation failed"}
  ]
}
```
