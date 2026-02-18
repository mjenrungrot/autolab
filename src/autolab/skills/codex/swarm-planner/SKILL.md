---
name: swarm-planner
description: >
  [EXPLICIT INVOCATION ONLY] Create dependency-aware Autolab implementation plans optimized for
  parallel multi-agent execution.
metadata:
  invocation: explicit-only
---

# Swarm Planner

Use this skill when the user explicitly asks for `$swarm-planner` to create or revise an execution-ready implementation plan.

## Scope

- Produce planning artifacts only. Do not implement code changes.
- Generate plans with explicit dependencies for parallel execution.
- Align plan tasks to the active `.autolab` stage and iteration context.

## Mandatory context pass (read first)

1. Confirm repository root and `.autolab` availability.
2. Read:
   - `.autolab/state.json`
   - `.autolab/verifier_policy.yaml`
   - active-stage prompt in `.autolab/prompts/` when relevant
   - iteration artifacts under `experiments/<iteration_id>/` when available
3. Inspect code paths implicated by the user request before drafting tasks.

## Clarification policy

- Ask targeted clarifying questions whenever ambiguity changes scope, dependencies, validation, or rollout.
- If the user does not answer, proceed with explicit assumptions and label them in the plan.

## Plan output contract

Write the plan to:
- `experiments/<iteration_id>/implementation_plan.md` when `iteration_id` exists.
- Otherwise `experiments/plan/<topic>/implementation_plan.md`.

Every task must include:
- `id`
- `depends_on`
- `location`
- `description`
- `validation`
- `status` (default `Not Completed`)
- `log` (empty placeholder)
- `files edited/created` (empty placeholder)

Include:
- overview
- dependency graph
- parallel execution wave table
- testing strategy
- risks and mitigations
- assumptions/defaults

## Task design rules

1. All dependencies must be explicit (`depends_on: []` for roots).
2. Tasks must be atomic and independently executable by one agent.
3. Validation must be concrete (commands, checks, or artifact assertions).
4. File paths in `location` must be specific.
5. Sequence tasks to maximize safe parallelism.

## Subagent review pass (required)

After drafting the plan:
1. Run one review subagent focused on dependency gaps, ordering errors, missing edge cases, and validation holes.
2. Incorporate actionable feedback.
3. Finalize only after review issues are resolved or explicitly justified.

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

## Tasks

### T1: <name>
- **depends_on**: []
- **location**: <paths>
- **description**: <work>
- **validation**: <checks>
- **status**: Not Completed
- **log**:
- **files edited/created**:

### T2: <name>
- **depends_on**: [T1]
- **location**: <paths>
- **description**: <work>
- **validation**: <checks>
- **status**: Not Completed
- **log**:
- **files edited/created**:

## Parallel Execution Groups
| Wave | Tasks | Can Start When |
|------|-------|----------------|
| 1 | T1 | Immediately |
| 2 | T2 | T1 complete |

## Testing Strategy
<strategy>

## Risks and Mitigations
<risks>

## Assumptions and Defaults
<assumptions>
```

## Failure handling

- If plan file path cannot be resolved, state attempted paths and stop for user confirmation.
- If dependency graph is inconsistent, fix before yielding.
- If required context files are missing, continue with documented assumptions and note the gap.
