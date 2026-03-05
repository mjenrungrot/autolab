## HARD GUARDRAILS (READ FIRST)
- Do not modify experiments already marked closed in `.autolab/backlog.yaml` (`done`, `completed`, `closed`, `resolved`) unless a human explicitly re-opens them.
- If the mapped experiment is closed, stop and request human intervention instead of editing artifacts.
- `runs/<run-id>/...` in policy/docs/workflow.yaml is a **pattern path**, not a literal filesystem path. At runtime the angle-bracket token is replaced with the actual run ID (e.g. `runs/20260218T160045Z/metrics.json`). Similarly, iteration-id tokens resolve to the active iteration directory name.
- Never create literal placeholder paths (for example `experiments//design.yaml` or `runs//metrics.json`).
- **No unresolved placeholders**: Never leave template markers in required outputs. Forbidden markers include:
  - Double-brace tokens (mustache-style template markers like `{` `{token}` `}`)
  - `<PLACEHOLDER>`, `<VALUE>` --angle-bracket placeholders
  - `TODO`, `TODO:`, `TBD`, `FIXME` --deferred-work markers
  - Empty JSON string values (`""`) for required fields
  - Ellipsis stand-ins (`...`, `<...>`) used as content substitutes

{{shared:status_vocabulary.md}}
