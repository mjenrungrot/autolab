## RESOLVED RUNTIME CONTEXT
Autolab resolves stage placeholders before runner execution and writes:

- `.autolab/prompts/rendered/<stage>.md`
- `.autolab/prompts/rendered/<stage>.context.json`

Resolved placeholders must be concrete for required tokens and must match `.autolab/state.json`.
If any required token remains unresolved, this stage must fail before work starts.

