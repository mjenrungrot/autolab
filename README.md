# Autolab shared package

This repository provides the shared `autolab` workflow package used across projects.

## Install

```bash
# Editable (local development)
python -m pip install -e .

# From GitHub (shared usage)
python -m pip install git+https://github.com/mjenrungrot/autolab.git@main

# Pinned release (CI / stable)
python -m pip install git+https://github.com/mjenrungrot/autolab.git@v1.2.26
```

Upgrade to the latest stable GitHub tag in one step:

```bash
autolab update
```

`autolab update` compares your installed version with the latest `vX.Y.Z` release
tag, upgrades via pip when needed, and then runs `autolab sync-scaffold --force`
automatically when you are inside an Autolab repo. If run outside an Autolab repo,
it upgrades the package and skips scaffold sync with an explicit message.

Manual fallback (equivalent upgrade + scaffold refresh):

```bash
python -m pip install git+https://github.com/mjenrungrot/autolab.git@vX.Y.Z
autolab sync-scaffold --force
```

Enable commit hooks (staged-file formatting + repo style check + default-branch version bump):

```bash
./scripts/install-hooks.sh
```

Run formatter/style checks locally:

```bash
./scripts/check_style.sh
```

The pre-commit hook now runs `./scripts/check_style.sh` on every commit. It also checks that the pinned release tag in `README.md` matches the current
`pyproject.toml` version, then syncs it to the next patch release and can sync
the current `vX.Y.Z` tag to GitHub (`origin`) after each commit on the default
branch.
On default-branch commits, pre-commit now fails hard unless staged `CHANGELOG.md`
contains a valid section for exactly `v<previous>..v<current>` with a real
`### Summary` block.
By default, hooks only run on the default branch and release-tag pruning is disabled
unless `scripts/sync_release_tags.py --prune` is used explicitly.
These local hooks are optional; CI workflows under `.github/workflows/` are authoritative.

After install, invoke with `autolab --help` or `python -m autolab --help`.

## What it does

Autolab drives a repeatable experiment lifecycle through a nine-stage pipeline:

`hypothesis -> design -> implementation -> implementation_review -> launch -> slurm_monitor -> extract_results -> update_docs -> decide_repeat`

Two terminal stages (`human_review`, `stop`) handle escalation and completion.

### Local vs SLURM stage graph

- Local host mode:
  - `launch -> slurm_monitor (auto-skip/no-op) -> extract_results`
- SLURM host mode:
  - `launch`: submit job, write initial manifest, append/repair the SLURM ledger entry
  - `slurm_monitor`: poll scheduler, run optional sync hooks, update run-manifest status/sync fields and monitor logs
  - `extract_results`: consume local artifacts, emit `completed|partial|failed` metrics
- Why `slurm_monitor` exists:
  - It keeps async scheduler polling/sync responsibilities out of extraction logic while preserving a single canonical stage graph.
  - Ledger ownership stays in `launch`; `slurm_monitor` does not rewrite `docs/slurm_job_list.md` rows.

**Operating modes**

- **Manual** (`autolab run`) -- single stage transition, ideal for debugging verifiers and manual checkpoints.
- **Agent runner** (`autolab loop --auto`) -- bounded or unattended multi-step execution with guardrails and lock management.
- **Assistant** (`autolab run --assistant`) -- task-driven delivery from backlog (`select -> implement -> verify -> review`).

See `docs/workflow_modes.md` for detailed responsibility contracts per mode.

## Configuration

**Command categories (onboarding-first).**

- **Getting started**: `autolab init`, `autolab configure`, `autolab status`, `autolab progress`, `autolab docs generate`, `autolab explain stage`.
- **Run workflow**: `autolab run`, `autolab loop`, `autolab discuss`, `autolab research`, `autolab trace`, `autolab tui`, `autolab render`, `autolab verify`, `autolab verify-golden`, `autolab parser init|test`, `autolab lint`, `autolab approve-plan`, `autolab review`, `autolab skip`, `autolab handoff`, `autolab resume`.
- **Backlog steering**: `autolab focus`, `autolab todo sync|list|add|done|remove`, `autolab experiment create`, `autolab experiment move`.
- **Safety and policy**: `autolab policy list|show|doctor|apply preset`, `autolab guardrails`, `autolab lock status|break`, `autolab unlock`.
- **Maintenance**: `autolab sync-scaffold`, `autolab update`, `autolab install-skill`, `autolab slurm-job-list append|verify`, `autolab report`, `autolab reset`.

**Recommended first run sequence.** `autolab init` -> `autolab configure --check` -> `autolab status` -> `autolab run --verify`.

**Run mode.** `autolab run` executes a single transition; `autolab loop --max-iterations N` runs bounded multi-step; `autolab loop --auto --max-hours H` enables unattended operation. Add `--verify` to run policy-driven verification before evaluation. Use `--decision <stage>` to force a human choice at `decide_repeat`, or `--auto-decision` to let Autolab choose from the backlog. For high-risk implementation plans, use `autolab run --plan-only` to stop after planning, `autolab approve-plan --status approve|retry|stop` to record the checkpoint decision, and `autolab run --execute-approved-plan` to execute the approved plan without replanning. See `docs/workflow_modes.md`.

**Progress, handoff, and resume.** `autolab progress` refreshes and summarizes takeover state, including critical-path duration/basis, per-wave timing windows, retry reasons, blocked/deferred/skipped tasks, file conflicts, per-task evidence summaries, and pending implementation plan approvals. `autolab handoff` writes both handoff artifacts: machine JSON (`.autolab/handoff.json`) and human Markdown (`<scope-root>/handoff.md`). `autolab resume` previews the recommended next command and can execute it with `--apply` when the safe-resume point is ready. Handoff artifacts are auto-refreshed on verification updates, each run/loop iteration, and manual stage-steering exits (for example `review`, `approve-plan`, `skip`, `focus`, and `experiment move`).

**Traceability coverage.** `autolab trace` builds a per-iteration end-to-end coverage artifact (`traceability_coverage.json`) linking hypothesis claim, design requirements, plan tasks, verification evidence, metrics, and decision context. Use `--iteration-id <id>` to render a non-active iteration and `--json` for machine-readable command output. A convenience pointer (`.autolab/traceability_latest.json`) is also updated for quick inspection.

**Generated project views.** `autolab docs generate` defaults to the legacy registry view for compatibility (`--view registry`). Use `--view project|roadmap|state|requirements|sidecar|all` for projection views and `--iteration-id <id>` for iteration-scoped projections. The generated `project`, `state`, and `sidecar` views include wave observability projections from `plan_graph.json`, `plan_check_result.json`, `plan_execution_state.json`, `plan_execution_summary.json`, and `plan_approval.json`, including retry/block/deferred/skipped detail, critical-path timing notes, and pending approval triggers. Optional discuss/research context sidecars live at `.autolab/context/sidecars/project_wide/{discuss,research}.json` and `experiments/<type>/<iteration_id>/context/sidecars/{discuss,research}.json`; when present they are schema-validated, carry dependency fingerprints for staleness checks, and must identify the scope root they belong to. Project-wide sidecars omit experiment identity fields; experiment sidecars carry `iteration_id` and `experiment_id`. When those observability artifacts are stale or mismatched for the selected iteration, the views keep rendering and surface diagnostics instead of failing where possible. Use `--output-dir <path>` to write markdown view files instead of printing to stdout; the output path must stay within the repository.

**Discuss and research sidecars.** `autolab discuss` captures scope-specific intent before planning. Use `--scope project_wide|experiment`, `--answers-file <json>` for deterministic non-interactive runs, `--non-interactive` to materialize the current/default questionnaire without prompting, and `--write-question-pack <path>` to export the exact question pack used. `autolab research` is the optional evidence pass: it resolves the same sidecar lineage, answers unresolved discuss questions (or explicit `--question` prompts), and writes `research.json` / `research.md`. Override the local research CLI with `AUTOLAB_RESEARCH_AGENT_COMMAND`; if unset, Autolab falls back to `claude` or `codex` when available.

**Prompt render (no execution).** `autolab render` resolves the stage prompt pack without running transitions or verifiers, then prints one prompt-pack view to stdout. It defaults to `state.stage` and `--view runner`. Use `--stage <stage>` to override, `--view runner|audit|brief|human|context` to select output, and `--stats` for prompt-debugging diagnostics. `autolab render` is read-only and does not write `.autolab/prompts/rendered/*` artifacts.

`runner` is the primary execution payload that Autolab sends to runner stdin. `audit`, `brief`, `human`, and `context` are companion views for policy checks, retry/handoff context, and inspection tooling. The render `context` view always surfaces a `context_resolution` block, using optional discuss/research sidecars when present, so downstream tooling can see the exact component order, selected `project_map` / `context_delta` / sidecars, effective merged items, and dependency-based staleness diagnostics.

Runner packets are intentionally slim: mission, strict outputs, required inputs, stop conditions, and non-negotiables. Design and implementation stages receive compact discuss/research summaries plus promoted-constraint references in brief/runtime context, not raw sidecar JSON. Status vocabulary and other verification-policy payloads (file budgets, evidence schemas, raw verifier blobs) stay in companion views, not runner prompts.
Memory guidance is stage-opt-in via `shared/memory_brief.md`; orchestration handles todo/documentation reconciliation after opted-in stages.

```bash
autolab render
autolab render --stage implementation --view runner
autolab render --stage design --view context
autolab render --stage implementation --view audit
autolab render --stage implementation --view brief
autolab render --stage design --view runner --stats
```

**Interactive cockpit.** `autolab tui` launches a mode-based Textual inspector (`Home`, `Runs`, `Files`, `Console`, `Waves`, `Help`) with render-aware guidance:

- Home shows stage status, blockers, required artifacts, and a rendered prompt preview so users can see what Autolab will run.
- Home includes a dedicated "Handoff & Resume" card (scope, wave/task status, blockers/decisions, recommended next command, safe resume status).
- Waves shows the current wave graph, critical path, retry reasons, blocked/deferred/skipped tasks, file conflicts, and per-task evidence rows.
- Home can resolve `human_review` directly (`pass|retry|stop`) using the same unlock + confirm flow as other mutating actions.
- Files supports quick-open for rendered prompt, render context, rendered audit contract, rendered brief, rendered human packet, prompt template, state, and stage artifacts.
- Files advanced actions include backlog steering for `focus`, `experiment create`, and `experiment move` through picker modals.
- Semantic colors are used for status readability (success/info/warning/error) without changing workflow behavior.

Safety behavior is unchanged: starts locked (read-only), mutating actions require unlock + confirmation, mutating completion auto-locks, and snapshot refresh failures fail closed. Run/loop actions remain preset-first with optional advanced controls; high-risk and backlog-steering actions stay hidden until advanced mode is enabled. External artifact open defaults to `cursor` when `EDITOR` is unset. See `docs/tui_cockpit.md`.

**Agent runner.** Controlled via `agent_runner` in `.autolab/verifier_policy.yaml`. Runners: `codex` (sandboxed, default preset), `claude` (non-interactive `claude -p`), or `custom` (your own command template). Toggle per-run with `--run-agent` / `--no-run-agent`. Edit scope defaults to `scope_root_plus_core`; set `scope_root_only` for strict isolation. Project-wide tasks resolve through `scope_roots.project_wide_root` (must be repo-relative, non-empty, not `..`-escaping, and point to an existing directory). See `docs/runner_reference.md`.

Runner cutover: `launch`, `slurm_monitor`, and `extract_results` are deterministic runtime stages and are no longer runner-eligible. Keep `agent_runner.stages` limited to active runner stages (`hypothesis`, `design`, `implementation`, `implementation_review`, `update_docs`, `decide_repeat`).
Deterministic stage behavior: `run_agent_mode=policy|force_off` bypasses runner invocation, while `run_agent_mode=force_on` fails fast on `launch`, `slurm_monitor`, and `extract_results`.

**Commit and quality gates.** `auto_commit.mode` controls commit behavior (`meaningful_only` default, `always`, `disabled`). `meaningful_change` settings gate implementation progress, verification success, and git-based checks. Override with `--no-strict-implementation-progress` for experiments. See `docs/runner_reference.md`.

**Guardrails.** `autorun.guardrails` caps same-decision streaks, no-progress cycles, update-docs churn, and generated todo count. Breach action defaults to `human_review`. Fallback tasks are configurable per host mode (`local` / `slurm`). See `docs/workflow_modes.md`.

**Policy presets.** Apply bundled policy overlays with:
`autolab policy apply preset <local_dev|ci_strict|slurm>`.

## Source layout

- `src/autolab/` -- Python package modules (`__main__`, `todo_sync`, `slurm_job_list`)
- `src/autolab/scaffold/.autolab/` -- shared default scaffold assets (prompts, schemas, verifier helpers, defaults)
- `.autolab/` in user repos is materialized from scaffold via `autolab init` or `autolab sync-scaffold`

## Stage lifecycle and artifacts

Each stage produces specific artifacts and has defined exit behavior:

- **hypothesis** -- `hypothesis.md`; advances when metric/target/criteria fields are present.
- **design** -- `design.yaml`; advances when required keys are present (including `implementation_requirements` and `extract_parser`).
- **design context quality** -- `design_context_quality.json`; advisory verifier output scoring how much discuss/research context was available and explicitly referenced by `design.yaml`.
- **implementation** -- `implementation_plan.md` + code changes; wave execution also produces `plan_execution_state.json` and `plan_execution_summary.json` with timings, retries, critical-path, conflict, and evidence data. Advances to review (requires Dry Run section when `dry_run: true`). Prompt-pack views are resolved at runtime; inspect with `autolab render --stage implementation --view runner|audit|brief|human|context` (stdout only; no rendered file writes).
- **implementation_review** -- `implementation_review.md`, `review_result.json`; `pass` -> launch, `needs_retry` -> implementation, `failed` -> human_review.
- **launch** -- executes `launch/run_local.sh` (local) or submits `launch/run_slurm.sbatch` via `sbatch` (SLURM), writes `runs/<run_id>/run_manifest.json`, then advances to slurm_monitor.
- **slurm_monitor** -- updates `runs/<run_id>/run_manifest.json` and monitor logs (`runs/<run_id>/logs/slurm_monitor.*.log`) for SLURM progress; local runs auto-skip to extraction.
- **SLURM ledger ownership** -- `launch` appends idempotent `docs/slurm_job_list.md` entries; monitor/evaluate stages validate presence but do not rewrite prior rows.
- **extract_results** -- `runs/<run_id>/metrics.json`, `analysis/summary.md`; assumes local evidence or emits `partial|failed` with explicit missing-evidence accounting. Summary contract: parser hook writes `analysis/summary.md`, or `extract_results.summary.llm_command` must be configured when using `mode: llm_on_demand`.
- **parser capability artifacts** -- `experiments/<type>/<iteration_id>/parser_capabilities.json` plus `.autolab/parser_capabilities.json`; validate parser-kind + metric compatibility against `design.yaml`.
- **update_docs** -- `docs_update.md`; advances when run evidence references are present.
- **decide_repeat** -- `decision_result.json`; decides next iteration or terminal action. On decision application, Autolab also refreshes `traceability_coverage.json` plus `.autolab/traceability_latest.json` (advisory for stage transitions; non-blocking on generation failure).
- assistant audit trail: `.autolab/task_history.jsonl`

## Migration Notes

- Runner cutover: remove deterministic stages (`launch`, `slurm_monitor`, `extract_results`) from `agent_runner.stages`; keep only `hypothesis`, `design`, `implementation`, `implementation_review`, `update_docs`, `decide_repeat`. `run_agent_mode=force_on` is rejected on deterministic stages.
- Extract parser contract: `design.yaml` now requires `extract_parser` (schema-level).
- Parser SDK: bootstrap with `autolab parser init`; validate deterministically with `autolab parser test` (default isolated temp workspace).
- Parser fixtures: scaffold ships `.autolab/parser_fixtures/<pack>/...` for golden parser tests.
- Parser capability gate: when capability artifacts exist (or strict policy is enabled), `autolab verify` enforces parser-kind and primary-metric compatibility.
- Summary contract: if parser does not write `analysis/summary.md`, configure `.autolab/verifier_policy.yaml -> extract_results.summary.llm_command`.

**Failure and retry.** Verifier failure increments `state.stage_attempt` and marks `needs_retry` while below `max_stage_attempts`. When the budget is exhausted the workflow escalates to `human_review`. `implementation_review` can explicitly return `pass`, `needs_retry`, or `failed`.

**State ownership.** `.autolab/state.json` is orchestration-owned. Stage agents emit artifacts; Autolab applies transition, retry, and escalation logic. Agents should never manually advance stages by editing state.

## State and backlog contracts

### `.autolab/state.json`

Required fields: `iteration_id`, `stage`, `stage_attempt`, `max_stage_attempts`, `max_total_iterations`.
Optional: `last_run_id`, `sync_status`, `history` (recent transition records with verifier summary and timestamps).

```json
{
  "iteration_id": "e1",
  "stage": "implementation",
  "stage_attempt": 0,
  "max_stage_attempts": 3,
  "max_total_iterations": 20
}
```

### `.autolab/backlog.yaml`

Workflow bootstrap expects `hypotheses` and `experiments` lists with `id`, `status`, `title`/`hypothesis_id`, and `iteration_id` fields. Terminal statuses: `done`, `completed`, `closed`, `resolved`. See `docs/artifact_contracts.md` for the full schema and examples.

## Verifiers

- `template_fill.py` -- placeholder cleanup and artifact budget checks per stage.
- `prompt_lint.py` -- stage prompt structure and token contract enforcement.
- `schema_checks.py` -- JSON Schema validation for stage artifacts, `state.json`, and `backlog.yaml`.
- `registry_consistency.py` -- ensures policy requirements are supported by workflow registry capabilities.
- `consistency_checks.py` -- validates cross-artifact consistency (design/manifest/metrics/review).
- `run_health.py` / `result_sanity.py` -- env-smoke checks; `run_health.py` runs for env-smoke stages, while `result_sanity.py` is stage-gated to `extract_results`.
- Canonical command: `autolab verify --stage <stage>`.
- Latest result persisted to `.autolab/verification_result.json`.
- Timestamped verification summaries are written to `.autolab/logs/verification_*.json`.
- Verification summary retention is automatic: `autolab verify` keeps only the latest 200 summary files.
- Verifier commands are policy-driven; `python_bin` (default `python3`) controls interpreter portability.
- `dry_run_command` should be non-empty when any stage sets `dry_run: true` (scaffold provides a stub).

## Skill install

```bash
autolab install-skill codex
```

Installs to `<project-root>/.codex/skills/autolab/SKILL.md`.

Target a different project:

```bash
autolab install-skill codex --project-root /path/to/project
```

## Scaffold management

Preferred upgrade path:

```bash
autolab update
```

Manual scaffold sync into a repo:

```bash
autolab sync-scaffold --force
```

Bootstrap a new workspace and configure policy defaults interactively:

```bash
autolab init --interactive
```

Bootstrap an existing repository (brownfield) with inferred backlog/context defaults:

```bash
autolab init --from-existing --no-interactive
```

`--from-existing` scans the repo, infers likely experiment structure, seeds
`verifier_policy.yaml` bootstrap metadata, and writes scope-aware context maps:

- `.autolab/context/project_map.{json,md}`
- `experiments/<type>/<iteration_id>/context_delta.{json,md}`
- `.autolab/context/bundle.json`

Reset `.autolab/` to packaged defaults and clear workflow state:

```bash
autolab reset
```

Use `--state-file` to target a different state path if needed:

```bash
autolab reset --state-file .autolab/state.json
```

## Further reading

- `docs/workflow_modes.md` -- manual, agent-runner, and assistant mode contracts
- `docs/workflow_registry_policy.md` -- workflow capability vs policy requirement model
- `docs/runner_reference.md` -- agent runner YAML reference and runner presets
- `docs/artifact_contracts.md` -- full artifact schemas, backlog format, and state contract
- `docs/skills/README.md` -- skill source/distribution layout and redirect rationale
- `docs/prompt_authoring_guide.md` -- scaffold prompt conventions and stage-prompt wiring
- `docs/quickstart.md` -- getting started walkthrough
- `src/autolab/example_golden_iterations/` -- complete stage-by-stage artifact example
