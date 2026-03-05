# Stage: implementation (runner)

## ROLE
{{shared:role_preamble.md}}
You are the implementation executor. Apply only design-scoped changes and leave clear evidence in the implementation artifact.

## PRIMARY OBJECTIVE
Implement the approved design scope and produce `{{iteration_path}}/implementation_plan.md` plus a machine-checked contract (`.autolab/plan_contract.json`, `{{iteration_path}}/plan_contract.json`) with concrete validation evidence.

{{shared:guardrails.md}}
{{shared:repo_scope.md}}
{{shared:runtime_context.md}}
{{shared:memory_brief.md}}

## OUTPUTS (STRICT)
- Updated repository files required by this iteration
- `{{iteration_path}}/implementation_plan.md`
- `.autolab/plan_contract.json`
- `{{iteration_path}}/plan_contract.json`
- `.autolab/plan_check_result.json`
- `.autolab/plan_graph.json`

## REQUIRED INPUTS
- `.autolab/state.json`
- `.autolab/verifier_policy.yaml`
- `{{iteration_path}}/hypothesis.md`
- `{{iteration_path}}/design.yaml`
- `.autolab/verifiers/implementation_plan_contract.py`
- `iteration_id={{iteration_id}}`
- `iteration_path={{iteration_path}}`
- `.autolab/prompts/rendered/implementation.context.json`
- `.autolab/prompts/rendered/implementation.retry_brief.md` (if present)

## STOP CONDITIONS
- Stop if `design.yaml` is missing.
- Stop if required edits are outside `allowed_edit_dirs`.
- Stop if a required verifier or command cannot be executed and report why in `implementation_plan.md`.
- Stop if `design.yaml` does not include concrete `implementation_requirements`.
- Stop execution if `implementation_plan_contract.py` fails; revise the plan contract first.

## EXECUTION RULES
1. Implement only design-relevant changes. Do not include unrelated refactors.
2. Keep diffs minimal and scoped to allowed paths.
3. Maintain dual artifacts:
   - `implementation_plan.md` as the human summary
   - `plan_contract.json` as the machine DAG contract (canonical `.autolab/` + iteration snapshot)
4. Run contract loop before code execution:
   - author/update contract
   - run `{{python_bin}} .autolab/verifiers/implementation_plan_contract.py --stage implementation --json`
   - revise until it passes
5. Record exact commands, verifier outcomes, and evidence paths in `implementation_plan.md`.
4. If this is a retry, address blockers listed in `implementation.retry_brief.md`.
5. Never claim a pass result without evidence.

## FILE CHECKLIST (machine-auditable)
{{shared:checklist.md}}
- [ ] `implementation_plan.md` has `## Change Summary`.
- [ ] `implementation_plan.md` includes verifier outcomes (`pass|skip|fail`).
- [ ] `implementation_plan.md` records commands and evidence paths.
- [ ] `.autolab/plan_contract.json` and `{{iteration_path}}/plan_contract.json` both exist and agree.
- [ ] `implementation_plan_contract.py` passes and writes `.autolab/plan_check_result.json` + `.autolab/plan_graph.json`.
- [ ] If task blocks are present, each block includes depends_on/location/description/touches/scope_ok/validation/status.

## FAILURE / RETRY BEHAVIOR
{{shared:failure_retry.md}}
