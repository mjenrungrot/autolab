## SKILL PLAYBOOK (optional tool guidance)
| Complexity | Indicator | Skill | Notes |
|------------|-----------|-------|-------|
| Simple | 1-3 files | (none) | Write tasks inline |
| Medium | 4-10 files, clear deps | `$swarm-planner` | Dependency-aware task blocks |
| Complex | 10+ files, multiple approaches | `$llm-council` → `$swarm-planner` | Council then swarm |
| Execution | Plan exists | `$parallel-task` | Run task waves |

All skills require explicit `$` invocation. Plans must conform to the implementation_plan.md unified format.

### Runtime Context Loading
When `.autolab/prompts/rendered/<stage>.context.json` exists, skills should load it to extract `runner_scope.allowed_edit_dirs`. Task `location` and `touches` fields must be constrained to paths within these allowed directories.
