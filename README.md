# Autolab shared package

This repository provides the shared `autolab` workflow package used across projects.

## Install

From this directory (`~/Workspaces/autolab`), editable install:

```bash
python -m pip install -e .
```

From a different location / shared usage:

```bash
python -m pip install git+https://github.com/mjenrungrot/autolab.git@main
```

For stable CI or release installs, pin to a tag:

```bash
python -m pip install git+https://github.com/mjenrungrot/autolab.git@v1.0.0
```

After upgrading the package from GitHub, refresh local workflow defaults:

```bash
autolab sync-scaffold --force
```

After install, invoke with:

```bash
autolab --help
python -m autolab --help
```

## Agent runner

Autolab supports multiple agent runners via the `runner` field in `.autolab/verifier_policy.yaml`:

```yaml
agent_runner:
  enabled: true
  runner: claude  # Options: codex, claude, custom
```

- **codex** (default): Uses `codex exec` with sandboxed `--add-dir` flags.
- **claude**: Uses Claude Code in non-interactive mode (`claude -p`). Operates from the repo root.
- **custom**: Set `runner: custom` and provide your own `command:` template.

When `runner` is set, the `command` field is auto-populated from the preset. You can still override `command` explicitly for any runner.

## Source layout

- `src/autolab/`: Python package modules (`__main__`, `todo_sync`, `slurm_job_list`)
- `src/autolab/scaffold/.autolab/`: Shared default scaffold assets (prompts, schemas, verifier helpers, defaults)
