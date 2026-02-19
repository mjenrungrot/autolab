# Workflow Registry vs Policy

Autolab resolves verifier behavior from two sources:

- `.autolab/workflow.yaml` `verifier_categories`: stage capabilities (what CAN run).
- `.autolab/verifier_policy.yaml` `requirements_by_stage`: stage requirements (what MUST run).

Rule:

- Requirements must be a subset of capabilities.
- `registry_consistency.py` enforces this.

## Worked Example (launch stage)

Registry capability (`workflow.yaml`):

- `stages.launch.verifier_categories.env_smoke: true`
- `stages.launch.verifier_categories.schema: true`
- `stages.launch.verifier_categories.prompt_lint: true`

Policy requirement (`verifier_policy.yaml`):

- `requirements_by_stage.launch.env_smoke: true` (valid)
- `requirements_by_stage.launch.schema: true` (valid)
- `requirements_by_stage.launch.tests: true` (invalid if capability is false)

Result:

- First two requirements are accepted.
- The third causes `registry_consistency` failure because policy requests a category not supported by registry capability.

## Common Misconfigurations

- Missing stage in policy:
  - Symptom: unexpected fallback behavior from registry defaults.
  - Fix: add explicit stage entry in `requirements_by_stage` and `retry_policy_by_stage`.
- Requirement not in registry capabilities:
  - Symptom: `registry_consistency` reports `...=true is not supported by workflow.yaml verifier_categories`.
  - Fix: either disable that requirement in policy or enable capability in `workflow.yaml`.
- Stage omitted from policy maps used by helper verifiers:
  - Symptom: silent drift in `template_fill.stages` or `prompt_lint.enabled_by_stage`.
  - Fix: add explicit per-stage toggle, even when false.

## Recommended Validation Loop

1. `autolab explain stage <stage>`
2. `autolab verify --stage <stage>`
3. `python3 .autolab/verifiers/registry_consistency.py --json`
