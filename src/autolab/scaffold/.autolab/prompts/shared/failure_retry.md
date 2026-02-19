## FAILURE / RETRY BEHAVIOR
- If any verification step fails, fix artifacts and rerun from the verification ritual.
- Do not force stage advancement in state; the orchestrator manages retry counters and escalation.
- If a required input is missing or unresolvable, stop and report the blocker rather than producing partial output.
- Retry/escalation thresholds are configured in `.autolab/verifier_policy.yaml` under `retry_policy_by_stage` and `autorun.guardrails`.
