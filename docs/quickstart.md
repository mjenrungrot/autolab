# Quickstart: Configure Your Repo in 10 Minutes

This guide walks you through setting up Autolab for a new or existing research repository.

## Prerequisites

- Python 3.10+
- `pip install autolab` (or `pip install -e .` from source)
- PyYAML and jsonschema: `pip install pyyaml jsonschema`

## Upgrading

```bash
autolab update
```

`autolab update` upgrades to the latest stable GitHub release tag (`vX.Y.Z`) and
automatically runs `autolab sync-scaffold --force` when executed inside an Autolab
repository. If run outside a repo, it upgrades and skips scaffold sync.

## Command map (grouped)

- **Getting started**: `autolab init`, `autolab configure`, `autolab status`, `autolab progress`, `autolab docs generate`, `autolab explain stage`.
- **Run workflow**: `autolab run`, `autolab loop`, `autolab checkpoint create|list|pin|unpin`, `autolab tui`, `autolab render`, `autolab verify`, `autolab verify-golden`, `autolab lint`, `autolab approve-plan`, `autolab uat init`, `autolab review`, `autolab skip`, `autolab handoff`, `autolab resume`.
- **Backlog steering**: `autolab focus`, `autolab todo sync|list|add|done|remove`, `autolab experiment create`, `autolab experiment move`.
- **Safety and policy**: `autolab policy list|show|doctor|apply preset`, `autolab remote show|doctor|smoke`, `autolab guardrails`, `autolab lock status|break`, `autolab unlock`.
- **Maintenance**: `autolab hooks install`, `autolab sync-scaffold`, `autolab update`, `autolab install-skill`, `autolab slurm-job-list append|verify`, `autolab report`, `autolab reset`.

`autolab hooks install` installs the Autolab `post-commit` helper only; any
repo-managed `pre-commit` or `core.hooksPath` setup remains separate.

Recommended onboarding flow:

```bash
autolab init
autolab configure --check
autolab status
autolab run --verify
```

Generate readable projections directly from canonical runtime artifacts:

```bash
autolab docs generate
autolab docs generate --view roadmap
autolab docs generate --view requirements --iteration-id <iteration_id>
autolab docs generate --view all --output-dir docs/autolab_views
autolab docs generate --view registry
```

`autolab docs generate` defaults to legacy compatibility output (`--view registry`).
Use `--view project|roadmap|state|requirements|sidecar|all` for projection views,
and keep `--view registry` when you need the compatibility mode. `--output-dir <path>` writes markdown view files to disk instead of stdout, and the path must
resolve within the repository.

## Step 1: Initialize the scaffold

```bash
autolab init --state-file .autolab/state.json
```

For an existing repository, use the brownfield bootstrap path:

```bash
autolab init --from-existing --state-file .autolab/state.json --no-interactive
```

`--from-existing` keeps scaffold/state initialization, then adds a repo scan that
infers likely experiment context, updates placeholder backlog entries when safe,
and writes context inheritance artifacts (`project_map`, `context_delta`, bundle).

This creates:

- `.autolab/state.json` -- workflow state
- `.autolab/backlog.yaml` -- experiment backlog
- `.autolab/verifier_policy.yaml` -- verification policy
- `.autolab/context/project_map.json` -- project-wide brownfield map (when `--from-existing`)
- `.autolab/context/bundle.json` -- starter context bundle pointer set (when `--from-existing`)
- `.autolab/prompts/` -- stage prompt templates
- `.autolab/schemas/` -- JSON schemas for artifacts
- `.autolab/verifiers/` -- verification scripts
- `experiments/plan/<iteration_id>/` -- iteration directory skeleton

## Step 2: Configure the verifier policy

Start by checking that your generated config is valid:

```bash
autolab configure --check --state-file .autolab/state.json
```

Edit `.autolab/verifier_policy.yaml`:

1. **Set the dry-run command** (required -- the default stub fails on purpose):

   ```yaml
   dry_run_command: "python3 -m myproject.dry_run --config path/to/config.yaml"
   ```

1. **Review stage requirements** under `requirements_by_stage`:

   - Enable `tests: true` for stages where you want test runs
   - Enable `dry_run: true` for stages that need smoke tests
   - `schema: true` is recommended for all stages

1. **Optional: Enable the agent runner** for automated stage execution:

   ```yaml
   agent_runner:
     enabled: true
     runner: claude  # or: codex, custom
     stages:
       - hypothesis
       - design
       - implementation
       - implementation_review
       - launch
       - slurm_monitor
       - extract_results
       - update_docs
   ```

## Step 3: Customize prompts (optional)

Stage prompts live in `.autolab/prompts/stage_*.runner.md`, `.audit.md`,
`.brief.md`, and `.human.md`. Each prompt:

- Defines the agent's role and objectives for that stage
- Uses `{{token}}` placeholders resolved at runtime

Edit prompts to match your project's domain (e.g., specific metric names, compute constraints, code structure).

Shared prompt fragments in `.autolab/prompts/shared/` are included via `{{shared:filename.md}}`.

Runner views stay thin (mission, strict outputs, required inputs, stop conditions, non-negotiables). Verification-policy payloads stay in companion `audit`/`brief`/`human`/`context` views, not `runner`.

Preview the resolved prompt for the current state or a specific stage without running transitions. `autolab render` is read-only and does not write `.autolab/prompts/rendered/*` artifacts:

```bash
autolab render
autolab render --stage implementation --view runner
autolab render --stage design --view context
autolab render --stage design --view runner --stats
```

## Step 4: Run your first stage

```bash
# Check current state
autolab status

# Run a single stage transition
autolab run

# Or run with verification
autolab run --verify
```

## Step 5: Verify outputs

```bash
# Run stage verification
autolab verify --stage hypothesis

# Or use the lint alias for friendlier output
autolab lint --stage hypothesis
```

## Step 6: Capture handoff and safe resume context

```bash
# Refresh and print concise takeover state
autolab progress

# Emit machine + human handoff artifacts
autolab handoff

# Preview safe resume command (or execute with --apply)
autolab resume
```

## What's next

- **Multi-step execution**: `autolab loop --max-iterations 5`
- **Unattended mode**: `autolab loop --auto --max-hours 2 --max-iterations 10`
- **Interactive inspector**: `autolab tui` (mode-based UI with Home/Runs/Files/Console/Help; Home includes a dedicated Handoff & Resume card; advanced actions include focus/create/move experiment steering; `human_review` can be resolved in Home with `pass|retry|stop`; starts locked; mutating actions require unlock + confirm; refresh failures fail closed until next successful refresh)
- **Assistant mode**: `autolab loop --auto --assistant --max-hours 2`
- **Manual decisions**: `autolab run --decision=design`
- **Human review decision**: `autolab review --status=pass|retry|stop`
- **Takeover artifacts**: `autolab progress`, `autolab handoff`, `autolab resume --apply`
- **Retarget state focus**: `autolab focus --experiment-id e1`
- **Steer backlog tasks**: `autolab todo list`, `autolab todo add "Implement feature X" --stage implementation`
- **Create a new experiment**: `autolab experiment create --experiment-id e2 --iteration-id iter2`
- **Move lifecycle type**: `autolab experiment move --experiment-id e1 --to in_progress`

## File structure reference

```
.autolab/
  state.json              # Workflow state (stage, iteration, attempts)
  backlog.yaml            # Experiment/hypothesis backlog
  verifier_policy.yaml    # Verification and runner policy
  agent_result.json       # Last agent execution result
  handoff.json            # Machine handoff + safe resume snapshot
  todo_state.json         # Task tracking state
  context/
    project_map.json      # Project-wide brownfield map (from --from-existing)
    project_map.md        # Human-readable project map summary
    bundle.json           # Pointer bundle to project map + experiment delta
  prompts/
    stage_*.md            # Per-stage prompt templates
    shared/*.md           # Shared prompt fragments
    rendered/             # Reserved path; autolab render does not write artifacts here
  schemas/                # JSON schemas for artifacts
  verifiers/              # Verification scripts
experiments/
  plan/<iteration_id>/    # Iteration artifacts
    handoff.md            # Human-readable handoff snapshot (scope-root)
    hypothesis.md
    design.yaml
    implementation_plan.md
    implementation_review.md
    review_result.json
    context_delta.json    # Experiment-specific context delta (from --from-existing)
    context_delta.md      # Human-readable delta summary
    launch/
    runs/<run_id>/
    analysis/
docs/
  todo.md                 # Task list for assistant mode
```
