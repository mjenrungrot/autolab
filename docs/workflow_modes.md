# Autolab Workflow Modes

This document clarifies who is responsible for artifact edits, verifier execution, and stage progression in each operating mode.

## Mode Summary

| Mode | Who edits artifacts | Who runs verifiers | Who advances stage |
| --- | --- | --- | --- |
| Manual (`autolab run`, no runner) | Human | Human via stage prompt and/or `autolab verify` | Autolab state machine |
| Agent runner (`agent_runner.enabled: true`) | Runner agent within allowed edit scope | Runner agent via stage prompt; Autolab can enforce via `autolab verify` / auto loop verification | Autolab state machine |
| Assistant (`--assistant`) | Assistant task cycle (`select -> implement -> verify -> review`) | Assistant verify phase + policy checks | Autolab assistant orchestration |

## Responsibility Contract

1. `.autolab/state.json` is orchestration-owned in all modes.
2. Workers (human or agent) produce stage artifacts; they do not manually set `state.stage`.
3. Policy (`.autolab/verifier_policy.yaml`) defines required checks by stage.
4. `autolab verify` is the canonical CLI for stage-relevant verification summaries.

## Recommended Usage

1. Manual development:
   - Run stage work.
   - Execute `autolab verify --stage <stage>`.
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
