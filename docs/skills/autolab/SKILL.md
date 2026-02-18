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

See also: `docs/workflow_modes.md` for mode responsibility boundaries.

## Quickstart

Autolab is an ML experiment workflow engine that orchestrates iterative
research cycles -- from hypothesis through results extraction -- with
built-in verification and guardrails at every stage.

Key commands:

- `autolab run` -- run the next stage in the workflow
- `autolab status` -- check current state, guardrails, and diagnostics
- `autolab verify --stage <stage>` -- validate stage outputs against policy

Stage flow at a glance:

    hypothesis -> design -> implementation -> implementation_review ->
    launch -> extract_results -> update_docs -> decide_repeat

## Quick recipes

1. Standard mode:
   - `autolab status`
   - `autolab verify --stage <stage>`
   - `autolab run`
2. Agent-runner unattended mode:
   - `autolab loop --auto --max-hours <H> --max-iterations <N>`
3. Assistant unattended mode:
   - `autolab loop --auto --assistant --max-hours <H> --max-iterations <N>`

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
- `autolab run --verify`: run policy verification before stage evaluation
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
- `autorun.guardrails.max_generated_todo_tasks`
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
- Keep `docs/todo.md` as Markdown; do not migrate todo tracking to YAML/JSON formats.
- Mention this explicitly in recommendations/reviews to avoid repeated review flags.
- Prefer `--max-iterations` and `--max-hours` in automation.
- Keep guardrails enabled.
- Use `meaningful_only` commit mode unless the user explicitly wants otherwise.

## Implementation Review Check Contract

- `review_result.json.required_checks` is a fixed 5-key map: `tests`, `dry_run`, `schema`, `env_smoke`, `docs_target_update`.
- Do not add extra keys (for example `prompt_lint` or `consistency`) to `required_checks`.
- Treat `.autolab/verification_result.json` as the canonical evidence for auto-enforced verifier categories.

## Common Failures

- `symptom`: `stage_attempt` keeps incrementing, never advances; `cause`: Verification fails repeatedly; `fix`: Check verifier output: `autolab verify --stage <stage>`. Fix artifacts, not state.
- `symptom`: `decide_repeat` blocks with "requires --decision"; `cause`: No decision source configured; `fix`: Add `--auto-decision` or provide `decision_result.json` or pass `--decision=<target>`.
- `symptom`: `human_review` reached unexpectedly; `cause`: Guardrail threshold breached; `fix`: Inspect `repeat_guard` in state. Tune guardrails in policy or fix root cause.
- `symptom`: Agent runner produces no changes; `cause`: Runner not enabled or stage not in runner stages list; `fix`: Set `agent_runner.enabled: true` and check `agent_runner.stages` in policy.
- `symptom`: "prompt template has unsupported token(s)"; `cause`: Prompt uses a token not in the context payload; `fix`: Check `.autolab/prompts/rendered/<stage>.context.json` for available tokens.
- `symptom`: Schema validation fails on design.yaml; `cause`: Missing required field or wrong type; `fix`: Check `design.schema.json` -- common issues: missing `schema_version: "1.0"`, wrong `compute.location` enum, empty `baselines`.
- `symptom`: Implementation progress check fails; `cause`: No meaningful file changes detected; `fix`: Ensure implementation edits are outside excluded paths (`.autolab/**`, `docs/todo.md`).
- `symptom`: Lock prevents run; `cause`: Stale lock from crashed process; `fix`: Run `autolab lock status` then `autolab lock break --reason "stale"`.
- `symptom`: Assistant mode stops immediately; `cause`: No open tasks in todo; `fix`: Add tasks to `docs/todo.md` or check backlog for open experiments.

## Runner Execution Artifacts

Files written during agent runner execution (under `.autolab/`):

- `file`: `agent_result.json`; `purpose`: Runner completion status, summary, changed files; `written_by`: Every run.
- `file`: `run.lock`; `purpose`: Prevents concurrent execution; `written_by`: `autolab loop --auto`.
- `file`: `block_reason.json`; `purpose`: Why the experiment was blocked; `written_by`: Completed-experiment guard.
- `file`: `state.json`; `purpose`: Stage, attempt, history; `written_by`: Every transition.
- `file`: `todo_state.json`; `purpose`: Task tracking state; `written_by`: Todo sync.
- `file`: `todo_focus.json`; `purpose`: Current task focus for agent; `written_by`: Assistant mode.
- `file`: `prompts/rendered/<stage>.md`; `purpose`: Rendered prompt for runner; `written_by`: Prompt render.
- `file`: `prompts/rendered/<stage>.context.json`; `purpose`: Resolved context payload; `written_by`: Prompt render.
- `file`: `verification_result.json`; `purpose`: Last verification outcome; `written_by`: `autolab verify`.

## Policy Setup Snippets

### Claude runner with iteration+core scope
```yaml
agent_runner:
  enabled: true
  runner: claude
  claude_dangerously_skip_permissions: false
  stages:
    - hypothesis
    - design
    - implementation
    - implementation_review
    - launch
    - extract_results
    - update_docs
  edit_scope:
    mode: "iteration_plus_core"
    core_dirs: ["src", "scripts", ".autolab", "docs", "paper", "tests"]
    ensure_iteration_dir: true
  timeout_seconds: 3600
```

### Strict verification for all stages
```yaml
requirements_by_stage:
  implementation:
    tests: true
    dry_run: true
    schema: true
  implementation_review:
    tests: true
    dry_run: true
    schema: true
    env_smoke: true
    docs_target_update: true
```

### SLURM dry-run command
```yaml
dry_run_command: "{{python_bin}} -m myproject.dry_run --config experiments/plan/iter1/design.yaml"
```

### Meaningful-change config (strict)
```yaml
autorun:
  meaningful_change:
    require_implementation_progress: true
    require_git_for_progress: true
    on_non_git_behavior: "fail"
    require_verification: true
    exclude_paths:
      - ".autolab/**"
      - "docs/todo.md"
```

## Assistant Mode Completion Semantics

In assistant mode (`--assistant`), the task cycle follows:

1. **Task selection**: Picks the highest-priority open task from `docs/todo.md` (manual tasks first, then generated)
2. **Implementation**: Runs the agent for the task's stage
3. **Verification**: If `require_verification: true`, runs verifiers before marking complete
4. **Review gate**: Checks for meaningful changes; if none detected, task stays open
5. **Completion**: Marks task done, syncs todo state, optionally auto-commits

Auto-stop behavior:
- When no open tasks remain and no generated fallback tasks apply, assistant mode selects `stop`
- The `strict_mode.forbid_auto_stop` policy can override this to `human_review`
- Guardrail escalation applies: repeated same-decision or no-progress streaks trigger `on_breach`

Blocked-experiment behavior:
- If the active experiment is marked completed in `backlog.yaml`, the run writes `block_reason.json` and transitions to `stop`
- Re-open the experiment in backlog to resume work
