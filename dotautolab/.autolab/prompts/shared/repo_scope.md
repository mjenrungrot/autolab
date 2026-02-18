## REPOSITORY PATH SCOPE
- Required stage artifacts may be under `{{iteration_path}}/...` and `.autolab/...` when specified.
- `src/` contains reusable implementation code; `scripts/` contains utilities; `{{iteration_path}}/implementation/` is for experiment-local artifacts.
- Use the runtime context `allowed_edit_dirs` list as authoritative scope. Avoid edits outside those paths.
- Treat all non-allowlisted directories as out-of-scope unless a human explicitly broadens scope.
