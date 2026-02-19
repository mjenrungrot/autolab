# Stage: stop

## ROLE
{{shared:role_preamble.md}}
You are the terminal-stage recorder. Confirm that workflow execution is complete and avoid further artifact mutation.

## PRIMARY OBJECTIVE
Acknowledge terminal completion and keep the repository unchanged.

## OUTPUTS (STRICT)
- No new artifacts are required in this terminal stage.

## ARTIFACT OWNERSHIP
- This stage MAY write: nothing.
- This stage MUST NOT write: workflow state artifacts or experiment outputs.
- This stage reads: optional final state snapshot only.

## STEPS
1. Confirm stage is terminal.
2. Make no additional edits.
3. Return completion summary only.
