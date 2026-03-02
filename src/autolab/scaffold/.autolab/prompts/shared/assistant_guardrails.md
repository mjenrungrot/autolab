## ASSISTANT-SPECIFIC GUARDRAILS
- Output must be a single strict JSON object matching `assistant_output_contract.md`.
- Do not edit `.autolab/state.json` or other orchestration-owned control files to force transitions.
- Dual-memory contract:
  - `docs/todo.md` is required actionable checklist memory.
  - `{{iteration_path}}/documentation.md` is required narrative experiment memory.
  - Keep both as Markdown; do not migrate memory tracking to YAML/JSON formats.
  - If intent conflicts, prioritize `documentation.md`, then reconcile actionable items into `docs/todo.md` before finalizing output.
- Keep all edits inside `allowed_edit_dirs` from runtime context.
- If required evidence is unavailable, report that explicitly in JSON fields instead of inventing results.
