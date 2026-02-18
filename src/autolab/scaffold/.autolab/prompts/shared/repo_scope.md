## REPOSITORY PATH SCOPE
- Required stage artifacts may be under `experiments/{{iteration_id}}/...` and `.autolab/...` when specified.
- `src/` contains reusable implementation code; `scripts/` contains utilities; `experiments/{{iteration_id}}/implementation/` is for experiment-local artifacts.
- Use the runtime context `allowed_edit_dirs` list as authoritative scope. Avoid edits outside those paths.
- Treat all non-allowlisted directories as out-of-scope unless a human explicitly broadens scope.
