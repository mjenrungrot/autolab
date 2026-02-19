# Agent Runner Reference

This document covers agent runner configuration, command substitution, environment variables, edit scope modes, and timeout settings.

## Runner Presets

Configure in `.autolab/verifier_policy.yaml` under `agent_runner`:

- `preset`: `codex`; `command`: `codex --approval-mode full-auto -q ...`; `notes`: OpenAI Codex CLI.
- `preset`: `claude`; `command`: `claude -p ... --allowedTools ...`; `notes`: Anthropic Claude Code.
- `preset`: `custom`; `command`: User-defined `command` field; `notes`: Any CLI tool.

Set `agent_runner.runner` to select a preset. Override with `agent_runner.command` for custom invocations.

### Claude runner options

- `claude_dangerously_skip_permissions: true` adds `--dangerously-skip-permissions` flag
- Only use in trusted, non-interactive CI environments
- Can also be enabled via `AUTOLAB_CLAUDE_ALLOW_DANGEROUS=true` environment variable

## Command Substitution Tokens

The runner command string supports these substitution tokens:

- `token`: `{stage}`; `replaced_with`: Current stage name; `example`: `implementation`.
- `token`: `{workspace_dir}`; `replaced_with`: Absolute path to repo root; `example`: `/home/user/myproject`.
- `token`: `{prompt_file}`; `replaced_with`: Path to rendered prompt; `example`: `.autolab/prompts/rendered/implementation.md`.
- `token`: `{state_file}`; `replaced_with`: Path to state file; `example`: `.autolab/state.json`.
- `token`: `{iteration_id}`; `replaced_with`: Current iteration ID; `example`: `iter_001`.
- `token`: `{allowed_dirs}`; `replaced_with`: Comma-separated allowed edit dirs; `example`: `experiments/plan/iter_001,src,scripts`.

## Environment Variables

Set by Autolab before runner invocation:

- `variable`: `AUTOLAB_STAGE`; `value`: Current stage name; `purpose`: Runner can condition on stage.
- `variable`: `AUTOLAB_ITERATION_ID`; `value`: Current iteration ID; `purpose`: Scoping.
- `variable`: `AUTOLAB_STATE_FILE`; `value`: Absolute path to state.json; `purpose`: State access.
- `variable`: `AUTOLAB_WORKSPACE_DIR`; `value`: Absolute path to repo root; `purpose`: Working directory.
- `variable`: `AUTOLAB_PROMPT_FILE`; `value`: Path to rendered prompt; `purpose`: Prompt input.
- `variable`: `AUTOLAB_ALLOWED_EDIT_DIRS`; `value`: Colon-separated allowed dirs; `purpose`: Scope enforcement.

## Edit Scope Modes

Configured via `agent_runner.edit_scope.mode`:

### `iteration_plus_core` (default)

Allows edits to:

- The current iteration directory: `experiments/<type>/<iteration_id>/`
- Core directories listed in `edit_scope.core_dirs` (default: `src`, `scripts`, `.autolab`, `docs`, `paper`, `tests`)

Best for: normal implementation work that spans experiment artifacts and shared code.

### `iteration_only`

Allows edits only to:

- The current iteration directory: `experiments/<type>/<iteration_id>/`
- `core_dirs` is ignored

Best for: strict isolation when you want to prevent any shared-code changes.

### Configuration example

```yaml
agent_runner:
  edit_scope:
    mode: "iteration_plus_core"
    core_dirs:
      - "src"
      - "scripts"
      - ".autolab"
      - "docs"
    ensure_iteration_dir: true
```

When `ensure_iteration_dir: true`, the iteration directory is created before runner invocation if it does not exist.

## Scope Violation Detection

Autolab detects out-of-scope edits after runner execution:

1. **Git-based** (default): Compares git diff before/after runner execution
1. **Filesystem snapshot** (fallback): When not a git worktree, uses `os.walk()` to detect file changes by mtime/size

Policy for non-git repos (`meaningful_change.on_non_git_behavior`):

- `warn_and_continue` (default): Log warning, allow the run to proceed
- `fail`: Fail the run immediately if git is unavailable

## Timeout Configuration

Set `agent_runner.timeout_seconds` (default: 3600):

```yaml
agent_runner:
  timeout_seconds: 7200  # 2 hours
```

- A value of 0 uses the default (3600 seconds)
- Runner process is terminated if it exceeds the timeout
- Timeout applies per-stage, not per-loop

## Runner Stage Selection

Not all stages support runner invocation. Eligible stages:

- `hypothesis`, `design`, `implementation`, `implementation_review`
- `launch`, `slurm_monitor`, `extract_results`, `update_docs`, `decide_repeat`

Terminal stages (`human_review`, `stop`) are not runner-eligible.

Configure which stages the runner executes via `agent_runner.stages`:

```yaml
agent_runner:
  stages:
    - implementation
    - implementation_review
    - extract_results
```

## CLI Overrides

- `--run-agent`: Force runner on for this command (ignores `enabled` policy)
- `--no-run-agent`: Force runner off (skips runner even if enabled)

These apply to both `autolab run` and `autolab loop`.
