## RESOLVED RUNTIME CONTEXT
{{stage_context}}

Autolab resolves stage placeholders before runner execution and writes:

- `.autolab/prompts/rendered/{{stage}}.runner.md`
- `.autolab/prompts/rendered/{{stage}}.audit.md`
- `.autolab/prompts/rendered/{{stage}}.brief.md`
- `.autolab/prompts/rendered/{{stage}}.human.md`
- `.autolab/prompts/rendered/{{stage}}.context.json`

`{{stage}}.runner.md` is the primary execution payload sent to the runner. Audit/brief/human/context packets are companion artifacts for policy, review, and handoff context.

Resolved placeholders must be concrete for required tokens and must match `.autolab/state.json`.
If any required token remains unresolved, this stage must fail before work starts.

`state.json` is owned by Autolab orchestration; do not edit it manually unless a human explicitly asks.
The runtime stage context block includes resolved edit scope allowlists (workspace + allowed dirs). Use those as the hard edit boundary.
If protected-file patterns are listed in runtime context/policy, treat them as hard denylist paths even when they appear under an allowed edit directory.
