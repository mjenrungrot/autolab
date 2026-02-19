## HARD GUARDRAILS (READ FIRST)
- Do not modify experiments already marked closed in `.autolab/backlog.yaml` (`done`, `completed`, `closed`, `resolved`) unless a human explicitly re-opens them.
- If the mapped experiment is closed, stop and request human intervention instead of editing artifacts.
- `runs//...` in policy/docs is a pattern key, not a real filesystem path.
- Never create literal placeholder paths (for example `experiments//design.yaml` or `runs//metrics.json`).
- **No unresolved placeholders**: Never leave template markers in required outputs. Forbidden markers include:
  - Double-brace tokens (mustache-style template markers like `{` `{token}` `}`)
  - `<PLACEHOLDER>`, `<VALUE>` --angle-bracket placeholders
  - `TODO`, `TODO:`, `TBD`, `FIXME` --deferred-work markers
  - Empty JSON string values (`""`) for required fields
  - Ellipsis stand-ins (`...`, `<...>`) used as content substitutes

**Success-like statuses**: `ok`, `completed`, `success`, `passed`. Use this set when checking artifact sync, run completion, or verifier outcomes.

**Canonical run status values**: `pending`, `submitted`, `running`, `synced`, `completed`, `failed`, `partial`. These are the valid values for `run_manifest.json`'s `status` field (see `run_manifest.schema.json`).
