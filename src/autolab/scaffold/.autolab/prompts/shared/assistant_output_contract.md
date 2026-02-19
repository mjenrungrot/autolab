## ASSISTANT OUTPUT CONTRACT

You MUST use this reporting format for every assistant phase response:
- `files_changed`: list concrete repo-relative paths changed in this phase (or `[]`).
- `commands_run`: list concrete commands executed for validation/evidence (or `[]`).
- `evidence`: list concise pointers in the form `<path>: <what it proves>`.
- `residual_risks`: short list of remaining risks/blockers (or `none`).

For `.autolab/todo_focus.json`, follow `.autolab/schemas/todo_focus.schema.json` exactly:
- top-level required keys: `generated_at`, `stage`, `open_task_count`, `focus_tasks`
- `focus_tasks[]` required keys: `task_id`, `stage`, `source`, `text`

Do not invent fields outside the schema.
