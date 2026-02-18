## HARD GUARDRAILS (READ FIRST)
- Do not modify experiments already marked closed in `.autolab/backlog.yaml` (`done`, `completed`, `closed`, `resolved`) unless a human explicitly re-opens them.
- If the mapped experiment is closed, stop and request human intervention instead of editing artifacts.
- `runs//...` in policy/docs is a pattern key, not a real filesystem path.
- Never create literal placeholder paths (for example `experiments//design.yaml` or `runs//metrics.json`).
- Never leave unresolved template markers in required outputs (`{{...}}`, `<TODO>`, `TODO`, `TBD`, `FIXME`).
