# Autolab Workflow Modes

This document clarifies who is responsible for artifact edits, verifier execution, and stage progression in each operating mode.

Operator playbooks live in `docs/skills/autolab/SKILL.md`.

## Mode Summary

- `mode`: Manual (`autolab run`, no runner); `who_edits_artifacts`: Human; `who_runs_verifiers`: Human via stage prompt, `autolab verify`, and optionally `autolab run --verify`; `who_advances_stage`: Autolab state machine.
- `mode`: Agent runner (`agent_runner.enabled: true`); `who_edits_artifacts`: Runner agent within allowed edit scope; `who_runs_verifiers`: Runner agent via stage prompt; Autolab can enforce via `autolab verify` / auto loop verification; `who_advances_stage`: Autolab state machine.
- `mode`: Assistant (`--assistant`); `who_edits_artifacts`: Assistant task cycle (`select -> implement -> verify -> review`); `who_runs_verifiers`: Assistant verify phase + policy checks; `who_advances_stage`: Autolab assistant orchestration.

## Responsibility Contract

1. `.autolab/state.json` is orchestration-owned in all modes.
2. Workers (human or agent) produce stage artifacts; they do not manually set `state.stage`.
3. Policy (`.autolab/verifier_policy.yaml`) defines required checks by stage.
4. `autolab verify` is the canonical CLI for stage-relevant verification summaries.
5. Assistant-mode scaffold prompts can be customized under `.autolab/prompts/assistant_*.md` for `select`, `implement`, `verify`, and `review` phases.

## Recommended Usage

1. Manual development:
   - Run stage work.
   - Execute `autolab verify --stage <stage>` (or use `autolab run --verify`).
   - Run `autolab run` to apply transition logic.
2. Agent runner development:
   - Keep `agent_runner.edit_scope` strict.
   - Run `autolab loop --auto` when unattended execution is needed.
   - Let automatic verification gate progression.
3. Assistant mode:
   - Use for backlog/todo-driven delivery loops.
   - Treat verification failures as blocking until fixed.

## Failure Ownership

1. Verifier failure: stage retries until attempt budget is exhausted.
2. Attempt budget exhausted: escalates to `human_review`.
3. Guardrail breach (`max_same_decision_streak`, `max_no_progress_decisions`, etc.): escalates per policy (`human_review` by default).

## See Also

- `docs/skills/autolab/SKILL.md` for copy/paste operating commands by mode.
- `examples/golden_iteration/README.md` for canonical stage artifacts and expected layout.
