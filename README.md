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

After install, invoke with:

```bash
autolab --help
python -m autolab --help
```

## Source layout

- `src/autolab/`: Python package modules (`__main__`, `todo_sync`, `slurm_job_list`)
- `dotautolab/.autolab/`: Shared default scaffold assets (prompts, schemas, verifier helpers, defaults)
