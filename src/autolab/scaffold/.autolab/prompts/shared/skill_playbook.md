## SKILL PLAYBOOK (optional tool guidance)
- **Simple**
  - `indicator`: 1-3 files
  - `skill`: (none)
  - `notes`: Write tasks inline
- **Medium**
  - `indicator`: 4-10 files with clear dependencies
  - `skill`: `$swarm-planner`
  - `notes`: Use dependency-aware task blocks
- **Complex**
  - `indicator`: 10+ files, multiple viable approaches
  - `skill`: `$llm-council` -> `$swarm-planner`
  - `notes`: Run council first, then execute with swarm plan
- **Execution**
  - `indicator`: Plan already exists
  - `skill`: `$parallel-task`
  - `notes`: Execute in task waves and keep plan logs updated

All skills require explicit `$` invocation. Plans must conform to the implementation_plan.md unified format.

### Runtime Context Loading
When `.autolab/prompts/rendered/<stage>.context.json` exists, skills should load it to extract `runner_scope.allowed_edit_dirs`. Task `location` and `touches` fields must be constrained to paths within these allowed directories.
