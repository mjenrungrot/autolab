## REPOSITORY PATH SCOPE
- Required stage artifacts may be under `experiments/{{iteration_id}}/...` and `.autolab/...` when specified.
- `src/` contains core implementation reusable across experiments.
- `scripts/` is for utility and CLI support code.
- `autolab/` is valid for orchestration/policy changes.
- Avoid changing unrelated files outside task scope.
