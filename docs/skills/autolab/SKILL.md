---
name: autolab
description: Plan, run, and troubleshoot Autolab stage workflows with the right runtime mode, policy settings, and guardrails.
metadata:
  short-description: Autolab Workflow Operator
---

# /autolab - Autolab Workflow Operator

Use this skill when the user wants to operate or troubleshoot an Autolab workflow.

## Goal

Help the user execute Autolab safely and efficiently by:
- choosing the right runtime mode (`standard` vs `assistant`)
- selecting correct run cadence (`run` vs `loop --auto`)
- applying policy knobs in `.autolab/verifier_policy.yaml`
- diagnosing no-transition / retry / escalation outcomes

## Command resolution

Use this command order:
1. `autolab ...` if CLI is installed on `PATH`
2. `python -m autolab ...`
3. `PYTHONPATH=src python -m autolab ...` (source checkout fallback)

## Read-first context checklist

Before making recommendations or changes, inspect:
- `.autolab/state.json`
- `.autolab/backlog.yaml`
- `.autolab/verifier_policy.yaml`
- `docs/todo.md`
- latest relevant stage artifacts under `experiments/<iteration_id>/`

Prefer `autolab status` (or module equivalent) first when debugging.

## Mode selection

### Standard mode (default; no `--assistant`)

Use for:
- deterministic stage-machine control
- manual checkpoints
- verifier debugging and schema/prompt iteration
- explicit `decide_repeat` decisions

Behavior summary:
- runs one stage transition at a time
- at `decide_repeat`, typically requires `--decision` or `--auto-decision`

### Assistant mode (`--assistant`)

Use for:
- task-driven feature delivery from todo/backlog
- autonomous cycles in unattended loops
- workflows where completion should stop automatically when no tasks remain

Behavior summary:
- task cycle is `select -> implement -> verify -> review -> done`
- no actionable tasks lead to `stop`
- review gate expects meaningful changes and (by policy) verification

## Run cadence

- `autolab run`: one controlled transition
- `autolab loop --max-iterations N`: bounded multi-step sequence
- `autolab loop --auto --max-hours H`: unattended execution with lock and guardrails

Use `--auto-decision` when you want automatic stage choice at `decide_repeat`.

## Agent runner controls

Policy is in `.autolab/verifier_policy.yaml`:
- `agent_runner.enabled`: policy on/off
- `agent_runner.runner`: `codex`, `claude`, or `custom`
- `agent_runner.edit_scope.mode`: `iteration_plus_core` (default) or `iteration_only`

CLI overrides:
- `--run-agent`: force runner on for this command
- `--no-run-agent`: force runner off for this command

Use `iteration_only` when isolation is required; use `iteration_plus_core` for normal implementation work across code/docs/tests.

## Commit and quality-gate knobs

In `.autolab/verifier_policy.yaml`:
- `autorun.auto_commit.mode`: `meaningful_only` (default), `always`, `disabled`
- `autorun.meaningful_change.require_implementation_progress`: strict progress gate
- `autorun.meaningful_change.require_verification`: require verification before assistant task completion
- `autorun.meaningful_change.require_git_for_progress`: require git-based progress checks

CLI override:
- `--no-strict-implementation-progress`: temporarily relax strict implementation progress checks

## Guardrail tuning (for unattended loops)

Use:
- `autorun.guardrails.max_same_decision_streak`
- `autorun.guardrails.max_no_progress_decisions`
- `autorun.guardrails.max_update_docs_cycles`
- `autorun.guardrails.on_breach` (commonly `human_review`)

Tune conservatively; prefer explicit escalation over silent infinite loops.

## Troubleshooting playbook

1. Verify CLI availability and choose command resolution fallback.
2. Run status and inspect `stage`, `stage_attempt`, `assistant_mode`, `task_cycle_stage`.
3. If stuck at `decide_repeat`, use `--decision` or `--auto-decision`.
4. If assistant mode loops, check:
   - meaningful-change config
   - verification requirements
   - todo/backlog task quality
5. If escalation occurs (`human_review`), inspect:
   - `.autolab/agent_result.json`
   - stage verifier outputs/artifacts
   - guardrail counters in `repeat_guard`
6. Apply the smallest policy change needed, rerun, and re-check status.

## Response style for this skill

When answering users:
- lead with recommended mode + command
- include exact command(s) ready to run
- mention assumptions and policy knobs changed
- include a short validation step (`autolab status` / expected stage change)

## Safe defaults

- Do not manually edit `.autolab/state.json` just to force transitions.
- Prefer `--max-iterations` and `--max-hours` in automation.
- Keep guardrails enabled.
- Use `meaningful_only` commit mode unless the user explicitly wants otherwise.
