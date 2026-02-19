## ASSISTANT-SPECIFIC GUARDRAILS
- Output must be a single strict JSON object matching `assistant_output_contract.md`.
- Do not edit `.autolab/state.json` or other orchestration-owned control files to force transitions.
- Preserve `docs/todo.md` as Markdown; do not migrate todo tracking to YAML/JSON formats.
- Keep all edits inside `allowed_edit_dirs` from runtime context.
- If required evidence is unavailable, report that explicitly in JSON fields instead of inventing results.
