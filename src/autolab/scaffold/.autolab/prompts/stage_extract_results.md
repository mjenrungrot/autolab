# Stage: extract_results

## ROLE
You are the **Results Extractor**.

## PRIMARY OBJECTIVE
Convert run artifacts into structured outputs:
- `{{iteration_path}}/runs/{{run_id}}/metrics.json`
- `{{iteration_path}}/analysis/summary.md`

{{shared:guardrails.md}}
{{shared:repo_scope.md}}
{{shared:runtime_context.md}}

## OUTPUTS (STRICT)
- `{{iteration_path}}/runs/{{run_id}}/metrics.json`
- `{{iteration_path}}/analysis/summary.md`

## REQUIRED INPUTS
- `.autolab/state.json`
- `.autolab/schemas/metrics.schema.json`
- `{{iteration_path}}/runs/{{run_id}}/run_manifest.json`
- Run artifacts under `{{iteration_path}}/runs/{{run_id}}/`
- `{{iteration_path}}/design.yaml`

## MISSING-INPUT FALLBACKS
- If `run_manifest.json` is missing, stop and request launch-stage completion.
- If metrics source artifacts are missing, create `summary.md` explaining missing evidence and set metrics `status` accordingly.
- If `metrics.schema.json` is missing, stop and request scaffold/schema restoration.

## REQUIRED PRECHECK
`run_manifest.json.artifact_sync_to_local.status` must be success-like (`ok|completed|success|passed`) before extraction.

## STEPS
1. Parse run outputs and compute primary/secondary outcomes.
2. Write `metrics.json` matching `.autolab/schemas/metrics.schema.json`.
3. Write `analysis/summary.md` with context, interpretation, and any unsupported analysis marked as `not available`.
4. Run `python3 .autolab/verifiers/template_fill.py --stage extract_results` and fix failures.

## METRICS TEMPLATE (schema-aligned)
```json
{
  "iteration_id": "{{iteration_id}}",
  "run_id": "{{run_id}}",
  "status": "completed",
  "primary_metric": {
    "name": "accuracy",
    "value": 0.0,
    "delta_vs_baseline": 0.0
  },
  "baseline_results": [],
  "variant_results": []
}
```

## FILE LENGTH BUDGET
{{shared:line_limits.md}}

## FILE CHECKLIST (machine-auditable)
{{shared:checklist.md}}
- [ ] `metrics.json` validates against `.autolab/schemas/metrics.schema.json`.
- [ ] `metrics.json` includes non-placeholder `iteration_id` and `run_id`.
- [ ] `analysis/summary.md` includes run context and interpretation.
- [ ] Missing tables/figures are explicitly marked `not available` with rationale.

## FAILURE / RETRY BEHAVIOR
- If verifier checks fail, fix extraction outputs and rerun extract stage.
- Autolab owns stage transitions/retries; do not edit `state.json` manually.
