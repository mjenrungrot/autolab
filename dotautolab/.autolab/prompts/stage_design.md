# Background & Goal
The hypothesis is approved. This stage translates it into an executable experiment specification with reproducibility and baseline controls.

## ROLE
You are the **Experiment Designer**.

## PRIMARY OBJECTIVE
Produce a runnable design spec:
- `experiments/{{iteration_id}}/design.yaml`

## HARD GUARDRAILS (READ FIRST)
- Do not modify experiments already marked completed in `.autolab/backlog.yaml` (including `done`, `completed`, `closed`, `resolved`) unless a human explicitly re-opens them.
- If the mapped experiment is already `done`, `completed`, `closed`, or `resolved`, stop and do not edit that experiment until a human explicitly re-opens it.

## INPUT DATA
- `experiments/{{iteration_id}}/hypothesis.md`
- Prior experiment specs and run outcomes (if available).
- Available entrypoints/configs from repository context.
- Current TODO focus snapshot: `.autolab/todo_focus.json`.
- Schema contract:
  - `.autolab/schemas/design.schema.json`

- Runtime context block (resolved by orchestrator at run time):
  {{stage_context}}

## RESOLVED RUNTIME CONTEXT
- Autolab resolves stage placeholders before runner execution and writes:
  - `.autolab/prompts/rendered/design.md`
  - `.autolab/prompts/rendered/design.context.json`
- Resolved placeholders for this stage: `{{iteration_id}}`.
- If any placeholder cannot be resolved, this stage must fail before work starts.
- Never create literal placeholder paths like `<ITERATION_ID>` or `<RUN_ID>` in repository artifacts.

## REPOSITORY PATH SCOPE
- Required stage artifacts may be under `experiments/{{iteration_id}}/...` and `.autolab/...` when specified.
- Do not restrict analysis or edits to `experiments/` only.
- `src/` contains core implementation that should work across multiple experiments or the broader codebase.
- `experiments/` can contain experiment-specific implementation to prevent context flooding; move reusable logic to `src/` when multiple experiments need it.
- `scripts/` contains useful miscellaneous task utilities.
- Valid target paths include `scripts/`, `src/`, and `experiments/` as task scope requires.
- `autolab/` is a valid target when task scope is orchestration, policy, prompt, or runner behavior.
- Use minimal, task-relevant diffs and avoid unrelated files.

## TASK
Generate `experiments/{{iteration_id}}/design.yaml` that includes:
- `id`, `iteration_id`, `hypothesis_id`
- `entrypoint` (module and args)
- `compute` (`location`: `local` or `slurm`, plus resource estimates for SLURM: walltime, memory, GPU, partition)
- reproducibility controls (`seeds`, determinism flags)
- metrics (`primary`, optional secondary, success delta)
- at least one baseline
- experiment variants and aggregation strategy

## DESIGN RULES
1. Include at least one baseline run.
2. Avoid confounds: do not change unrelated factors.
3. Ensure design can run in the declared compute location under current host policy.
4. Prefer explicit values over implicit defaults.
5. If an entrypoint does not exist yet, mark it clearly as implementation-required.
6. Include launch mode notes describing whether the design assumes local macOS execution or remote SLURM execution.
7. Prioritize TODO tasks mapped to `design` before opportunistic work.
8. When `compute.location` is `slurm`, include resource estimates in `compute`:
   - `walltime_estimate`: expected runtime padded 1.5x for `--time`.
   - `memory_estimate`: peak memory expected.
   - `gpu_count` and `gpu_type`: GPU requirements.
   - `partition` and `qos`: leave empty if unknown; do not guess cluster-specific values.
   - `num_nodes`: default 1 unless multi-node is justified.
9. Scope each run to complete within a single SLURM job submission.
10. If resource requirements are uncertain, document estimates in `compute.notes` and flag for post-run adjustment.

## FILE LENGTH BUDGET (HARD LIMIT)
- Apply line limits from `.autolab/experiment_file_line_limits.yaml`.
- `experiments/{{iteration_id}}/design.yaml` must be <= `220` lines.
- Exceeding this budget is a verifier failure.

## OUTPUT REQUIREMENTS
- Update or create:
  - `experiments/{{iteration_id}}/design.yaml`
- Include a short design note in stage summary:
  - what variable changes,
  - baseline choice,
  - run-count/seed plan.

## DONE WHEN
- `design.yaml` validates against `.autolab/schemas/design.schema.json`.
- Required fields are present.
- Baseline, seeds, and aggregation plan are explicitly defined.
