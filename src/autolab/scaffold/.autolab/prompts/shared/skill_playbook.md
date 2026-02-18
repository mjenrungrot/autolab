## SKILL PLAYBOOK (optional tool guidance)
- `complexity`: Simple; `indicator`: 1-3 files; `skill`: (none); `notes`: Write tasks inline.
- `complexity`: Medium; `indicator`: 4-10 files, clear deps; `skill`: `$swarm-planner`; `notes`: Dependency-aware task blocks.
- `complexity`: Complex; `indicator`: 10+ files, multiple approaches; `skill`: `$llm-council` -> `$swarm-planner`; `notes`: Council then swarm.
- `complexity`: Execution; `indicator`: Plan exists; `skill`: `$parallel-task`; `notes`: Run task waves.

All skills require explicit `$` invocation. Plans must conform to the implementation_plan.md unified format.

### Runtime Context Loading
When `.autolab/prompts/rendered/<stage>.context.json` exists, skills should load it to extract `runner_scope.allowed_edit_dirs`. Task `location` and `touches` fields must be constrained to paths within these allowed directories.
