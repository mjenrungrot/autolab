# Codex Skill Index

## Skill Catalog

- `autolab`: Operate and troubleshoot Autolab workflow execution modes and policy guardrails.
- `llm-council`: Generate multiple candidate implementation plans and synthesize a judged final plan.
- `swarm-planner`: Produce dependency-aware implementation plans for parallel execution.
- `parallel-task`: Execute implementation plans in dependency waves with structured run summaries.

## Invocation Rules

- Skills are explicit-invocation tools; call them using `$skill-name` in prompts.
- Use `$llm-council` when planning ambiguity is high and multiple strategies should be compared.
- Use `$swarm-planner` when one robust execution plan is needed.
- Use `$parallel-task` only after a valid implementation plan already exists.

## Recommended Chaining

1. `$llm-council` -> produce `final-plan.md` from multiple planner candidates.
1. `$swarm-planner` -> normalize/validate the final plan into `implementation_plan.md`.
1. `$parallel-task` -> execute tasks by dependency wave and emit `plan_execution_summary.json`.

## Stage Mapping

- `implementation` stage: primary consumer of `$swarm-planner` and `$parallel-task`.
- Complex `implementation` stage planning: `$llm-council` before `$swarm-planner`.
- Workflow operations and recovery: `$autolab`.
