# Agent Runner Reference

This document covers agent runner configuration, command substitution, environment variables, edit scope modes, and timeout settings.

Deterministic-stage note: `launch`, `slurm_monitor`, and `extract_results` are
orchestrator-backed runtime stages. Runner invocation is bypassed for these stages
unless `run_agent_mode=force_on`, which is rejected as a stage failure.

## Runner Presets

Configure in `.autolab/verifier_policy.yaml` under `agent_runner`:

- `preset`: `codex`; `command`: `codex exec --full-auto -C {workspace_dir} {core_add_dirs} -`; `notes`: OpenAI Codex CLI.
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
- `token`: `{workspace_dir}`; `replaced_with`: Absolute path to runner workspace; `example`: `/home/user/myproject/experiments/plan/iter1`.
- `token`: `{scope_root}`; `replaced_with`: Absolute path to effective edit scope root; `example`: `/home/user/myproject/src`.
- `token`: `{prompt_runner_path}`; `replaced_with`: Path to rendered runner prompt; `example`: `.autolab/prompts/rendered/implementation.runner.md`.
- `token`: `{prompt_path}`; `replaced_with`: Deprecated alias for `{prompt_runner_path}`; `example`: `.autolab/prompts/rendered/implementation.runner.md`.
- `token`: `{prompt_template_path}`; `replaced_with`: Path to source prompt template; `example`: `.autolab/prompts/stage_implementation.runner.md`.
- `token`: `{prompt_context_path}`; `replaced_with`: Path to rendered prompt context JSON; `example`: `.autolab/prompts/rendered/implementation.context.json`.
- `token`: `{prompt_audit_path}`; `replaced_with`: Path to rendered audit contract; `example`: `.autolab/prompts/rendered/implementation.audit.md`.
- `token`: `{prompt_brief_path}`; `replaced_with`: Path to rendered brief packet; `example`: `.autolab/prompts/rendered/implementation.brief.md`.
- `token`: `{prompt_retry_brief_path}`; `replaced_with`: Deprecated alias for `{prompt_brief_path}`; `example`: `.autolab/prompts/rendered/implementation.brief.md`.
- `token`: `{prompt_human_path}`; `replaced_with`: Path to rendered human packet; `example`: `.autolab/prompts/rendered/implementation.human.md`.
- `token`: `{iteration_id}`; `replaced_with`: Current iteration ID; `example`: `iter_001`.
- `token`: `{core_add_dirs}`; `replaced_with`: Runner-specific `--add-dir` flag fragment; `example`: `--add-dir /repo/src --add-dir /repo/tests`.

## Environment Variables

Set by Autolab before runner invocation:

- `variable`: `AUTOLAB_STAGE`; `value`: Current stage name; `purpose`: Runner can condition on stage.
- `variable`: `AUTOLAB_ITERATION_ID`; `value`: Current iteration ID; `purpose`: Scoping.
- `variable`: `AUTOLAB_STATE_FILE`; `value`: Absolute path to state.json; `purpose`: State access.
- `variable`: `AUTOLAB_WORKSPACE_DIR`; `value`: Absolute path to runner workspace; `purpose`: Working directory.
- `variable`: `AUTOLAB_SCOPE_ROOT`; `value`: Absolute path to effective scope root; `purpose`: Scope-aware execution context.
- `variable`: `AUTOLAB_PROMPT_RUNNER_PATH`; `value`: Path to rendered runner prompt; `purpose`: Prompt input.
- `variable`: `AUTOLAB_PROMPT_PATH`; `value`: Deprecated alias for `AUTOLAB_PROMPT_RUNNER_PATH`; `purpose`: Backward compatibility.
- `variable`: `AUTOLAB_PROMPT_TEMPLATE_PATH`; `value`: Path to source prompt template; `purpose`: Debug/template traceability.
- `variable`: `AUTOLAB_PROMPT_CONTEXT_PATH`; `value`: Path to rendered context JSON; `purpose`: Scope + stage metadata input.
- `variable`: `AUTOLAB_PROMPT_AUDIT_PATH`; `value`: Path to rendered audit contract; `purpose`: Human/audit policy access.
- `variable`: `AUTOLAB_PROMPT_BRIEF_PATH`; `value`: Path to rendered brief packet; `purpose`: Compact retry/review/handoff context.
- `variable`: `AUTOLAB_PROMPT_RETRY_BRIEF_PATH`; `value`: Deprecated alias for `AUTOLAB_PROMPT_BRIEF_PATH`; `purpose`: Backward compatibility.
- `variable`: `AUTOLAB_PROMPT_HUMAN_PATH`; `value`: Path to rendered human packet; `purpose`: Human review packet access.
- `variable`: `AUTOLAB_CORE_ADD_DIRS`; `value`: Comma-separated core add-dir absolute paths; `purpose`: Scope diagnostics.

## Memory Brief Ownership

- Memory sync guidance is stage-opted, not universal: only templates that include
  `{{shared:memory_brief.md}}` carry todo/documentation reminders.
- Orchestration owns todo/documentation reconciliation after those opted-in stages.
- Runner prompts should keep memory edits concise and task-scoped.

## Edit Scope Modes

Configured via `agent_runner.edit_scope.mode`:

Scope root resolution:

- `scope_kind=experiment` => active iteration directory
- `scope_kind=project_wide` => `scope_roots.project_wide_root` (default `.`)

### `scope_root_plus_core` (default)

Allows edits to:

- The effective scope root (iteration directory or configured project-wide root)
- Core directories listed in `edit_scope.core_dirs` (default: `src`, `scripts`, `.autolab`, `docs`, `paper`, `tests`)

Best for: normal work that may span the active scope root and shared code.

### `scope_root_only`

Allows edits only to:

- The effective scope root
- `core_dirs` is ignored

Best for: strict isolation when you want edits confined to the resolved scope root.

### Configuration example

```yaml
scope_roots:
  project_wide_root: "."

agent_runner:
  edit_scope:
    mode: "scope_root_plus_core"
    core_dirs:
      - "src"
      - "scripts"
      - ".autolab"
      - "docs"
    ensure_iteration_dir: true
```

When `ensure_iteration_dir: true`, the iteration directory is created before runner invocation for experiment-scoped runs if it does not exist.

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
- `update_docs`, `decide_repeat`

Deterministic runtime cutover: `launch`, `slurm_monitor`, and `extract_results` are
orchestrator-owned and not runner-eligible.

Run-mode behavior for deterministic stages:

- `run_agent_mode=policy`: runner is bypassed; deterministic runtime executes.
- `run_agent_mode=force_off`: runner is bypassed; deterministic runtime executes.
- `run_agent_mode=force_on`: rejected with
  `run_agent_mode=force_on is incompatible with deterministic stage '<stage>'`.

Terminal stages (`human_review`, `stop`) are not runner-eligible.

Configure which stages the runner executes via `agent_runner.stages`:

```yaml
agent_runner:
  stages:
    - implementation
    - implementation_review
    - update_docs
```

Migration note: legacy `agent_runner.stages` values that include deterministic
stages should be removed during scaffold/policy upgrades.

## CLI Overrides

- `--run-agent`: Force runner on for this command (ignores `enabled` policy)
- `--no-run-agent`: Force runner off (skips runner even if enabled)

These apply to both `autolab run` and `autolab loop`. For deterministic stages,
`--run-agent` (`force_on`) fails fast instead of forcing runner execution.
