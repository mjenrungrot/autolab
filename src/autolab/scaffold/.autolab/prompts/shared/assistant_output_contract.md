## ASSISTANT OUTPUT CONTRACT

You MUST return **only** one strict JSON object for every assistant phase response.
Do not return Markdown headings, prose wrappers, or fenced code blocks.

Required JSON shape:

```json
{
  "files_changed": ["path/one", "path/two"],
  "commands_run": ["pytest -q tests/test_example.py"],
  "evidence": ["path/to/file: what it proves"],
  "residual_risks": ["remaining blocker or risk"]
}
```

Contract rules:
- `files_changed`: array of repo-relative path strings (use `[]` when no edits were made).
- `commands_run`: array of command strings that were actually executed (use `[]` when none).
- `evidence`: array of concise evidence strings in the form `<path>: <what it proves>` (use `[]` when none).
- `residual_risks`: array of short risk/blocker strings (use `[]` when none).
- Do not add extra top-level keys.

For `.autolab/todo_focus.json`, follow `.autolab/schemas/todo_focus.schema.json` exactly:
- top-level required keys: `generated_at`, `stage`, `open_task_count`, `focus_tasks`
- `focus_tasks[]` required keys: `task_id`, `stage`, `source`, `text`

Do not invent fields outside the schema.
