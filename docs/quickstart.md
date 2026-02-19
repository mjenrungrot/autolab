# Quickstart: Configure Your Repo in 10 Minutes

This guide walks you through setting up Autolab for a new or existing research repository.

## Prerequisites

- Python 3.10+
- `pip install autolab` (or `pip install -e .` from source)
- PyYAML and jsonschema: `pip install pyyaml jsonschema`

## Step 1: Initialize the scaffold

```bash
autolab init --state-file .autolab/state.json
```

This creates:

- `.autolab/state.json` -- workflow state
- `.autolab/backlog.yaml` -- experiment backlog
- `.autolab/verifier_policy.yaml` -- verification policy
- `.autolab/prompts/` -- stage prompt templates
- `.autolab/schemas/` -- JSON schemas for artifacts
- `.autolab/verifiers/` -- verification scripts
- `experiments/plan/<iteration_id>/` -- iteration directory skeleton

## Step 2: Configure the verifier policy

Edit `.autolab/verifier_policy.yaml`:

1. **Set the dry-run command** (required -- the default stub fails on purpose):

   ```yaml
   dry_run_command: "python3 -m myproject.dry_run --config path/to/config.yaml"
   ```

2. **Review stage requirements** under `requirements_by_stage`:

   - Enable `tests: true` for stages where you want test runs
   - Enable `dry_run: true` for stages that need smoke tests
   - `schema: true` is recommended for all stages

3. **Optional: Enable the agent runner** for automated stage execution:

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

Stage prompts live in `.autolab/prompts/stage_*.md`. Each prompt:

- Defines the agent's role and objectives for that stage
- Includes shared guardrails, scope rules, and verification rituals
- Uses `{{token}}` placeholders resolved at runtime

Edit prompts to match your project's domain (e.g., specific metric names, compute constraints, code structure).

Shared prompt fragments in `.autolab/prompts/shared/` are included via `{{shared:filename.md}}`.

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

## What's next

- **Multi-step execution**: `autolab loop --max-iterations 5`
- **Unattended mode**: `autolab loop --auto --max-hours 2 --max-iterations 10`
- **Assistant mode**: `autolab loop --auto --assistant --max-hours 2`
- **Manual decisions**: `autolab run --decision=design`
- **Human review**: `autolab review --status=pass`
- **Retarget state focus**: `autolab focus --experiment-id e1`
- **Steer backlog tasks**: `autolab todo list`, `autolab todo add "Implement feature X" --stage implementation`
- **Move lifecycle type**: `autolab experiment move --experiment-id e1 --to in_progress`

## File structure reference

```
.autolab/
  state.json              # Workflow state (stage, iteration, attempts)
  backlog.yaml            # Experiment/hypothesis backlog
  verifier_policy.yaml    # Verification and runner policy
  agent_result.json       # Last agent execution result
  todo_state.json         # Task tracking state
  prompts/
    stage_*.md            # Per-stage prompt templates
    shared/*.md           # Shared prompt fragments
    rendered/             # Rendered prompts (generated at runtime)
  schemas/                # JSON schemas for artifacts
  verifiers/              # Verification scripts
experiments/
  plan/<iteration_id>/    # Iteration artifacts
    hypothesis.md
    design.yaml
    implementation_plan.md
    implementation_review.md
    review_result.json
    launch/
    runs/<run_id>/
    analysis/
docs/
  todo.md                 # Task list for assistant mode
```
