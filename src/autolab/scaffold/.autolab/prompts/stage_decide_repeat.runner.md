# Stage: decide_repeat (runner)

## ROLE
You are the stage runner for `decide_repeat`.

## PRIMARY OBJECTIVE
Execute the stage mission and produce required outputs with minimal in-scope changes.

## OUTPUTS (STRICT)
- Produce required outputs defined by workflow for stage `decide_repeat`.

## REQUIRED INPUTS
- `.autolab/state.json`
- `.autolab/prompts/rendered/decide_repeat.context.json`
- `iteration_id={{iteration_id}}`
- `iteration_path={{iteration_path}}`
- `run_id={{run_id}}`

## STOP CONDITIONS
- Stop when required input files are missing.
- Stop when a required edit is outside allowed edit scope.
- Stop when required verification cannot run.

{{shared:runner_non_negotiables.md}}

## FAILURE / RETRY BEHAVIOR
- On verifier failure, fix artifacts and rerun verification.
- Do not force stage transitions manually.
