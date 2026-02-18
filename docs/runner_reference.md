# Agent Runner Reference

This document covers agent runner configuration, command substitution, environment variables, edit scope modes, and timeout settings.

## Runner Presets

Configure in `.autolab/verifier_policy.yaml` under `agent_runner`:

| Preset | Command | Notes |
|--------|---------|-------|
| `codex` | `codex --approval-mode full-auto -q ...` | OpenAI Codex CLI |
| `claude` | `claude -p ... --allowedTools ...` | Anthropic Claude Code |
| `custom` | User-defined `command` field | Any CLI tool |

Set `agent_runner.runner` to select a preset. Override with `agent_runner.command` for custom invocations.

### Claude runner options

- `claude_dangerously_skip_permissions: true` adds `--dangerously-skip-permissions` flag
- Only use in trusted, non-interactive CI environments
- Can also be enabled via `AUTOLAB_CLAUDE_ALLOW_DANGEROUS=true` environment variable

## Command Substitution Tokens

The runner command string supports these substitution tokens:

| Token | Replaced With | Example |
|-------|---------------|---------|
| `{stage}` | Current stage name | `implementation` |
| `{workspace_dir}` | Absolute path to repo root | `/home/user/myproject` |
| `{prompt_file}` | Path to rendered prompt | `.autolab/prompts/rendered/implementation.md` |
| `{state_file}` | Path to state file | `.autolab/state.json` |
| `{iteration_id}` | Current iteration ID | `iter_001` |
| `{allowed_dirs}` | Comma-separated allowed edit dirs | `experiments/plan/iter_001,src,scripts` |

## Environment Variables

Set by Autolab before runner invocation:

| Variable | Value | Purpose |
|----------|-------|---------|
| `AUTOLAB_STAGE` | Current stage name | Runner can condition on stage |
| `AUTOLAB_ITERATION_ID` | Current iteration ID | Scoping |
| `AUTOLAB_STATE_FILE` | Absolute path to state.json | State access |
| `AUTOLAB_WORKSPACE_DIR` | Absolute path to repo root | Working directory |
| `AUTOLAB_PROMPT_FILE` | Path to rendered prompt | Prompt input |
| `AUTOLAB_ALLOWED_EDIT_DIRS` | Colon-separated allowed dirs | Scope enforcement |

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
2. **Filesystem snapshot** (fallback): When not a git worktree, uses `os.walk()` to detect file changes by mtime/size

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
- `launch`, `extract_results`, `update_docs`, `decide_repeat`

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
