## REPOSITORY PATH SCOPE -- MUST-NOT-VIOLATE
- The runtime context `allowed_edit_dirs` list is the **authoritative edit boundary**. Any edit outside these directories is a **scope violation** and will be rejected by the runner.
- Required stage artifacts may be under `{{iteration_path}}/...` and `.autolab/...` when specified.
- `src/` contains reusable implementation code; `scripts/` contains utilities; `{{iteration_path}}/implementation/` is for experiment-local artifacts.
- Non-allowlisted directories are **out-of-scope**. Do not create, modify, or delete files there unless a human explicitly broadens scope.
- If you discover a necessary edit is out-of-scope, note it in your output as a follow-up action instead of making the edit.
