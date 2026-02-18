---
name: parallel-task
description: >
  [EXPLICIT INVOCATION ONLY] Execute dependency-aware implementation plans in parallel subagent
  waves with Autolab-aware logging.
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
   - `status`, `log`, `files edited/created`
4. Validate unique IDs and dependency references.
5. If `.autolab/state.json` exists, load iteration and stage context for prompts.

## Subset execution rules

- If task subset is requested, include all transitive dependencies.
- If a requested task ID is missing, stop and list available task IDs.

## Execution loop

1. Identify unblocked tasks:
   - task not completed
   - all `depends_on` tasks completed
2. Launch all unblocked tasks in parallel subagents.
3. Wait for completion, collect outputs, and validate results.
4. Mark task as complete only when plan updates are present:
   - `status: Completed`
   - non-empty `log`
   - non-empty `files edited/created`
5. If a task fails validation, retry once; otherwise report blocked status.
6. Repeat until no pending tasks remain.

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
