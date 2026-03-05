## NON-NEGOTIABLES
- Use `.autolab/prompts/rendered/{{stage}}.context.json` as the only runtime context source.
- Keep edits inside `runner_scope.allowed_edit_dirs` from context JSON.
- Produce only required stage outputs and concrete evidence.
- Stop when required inputs are missing or unverifiable.
