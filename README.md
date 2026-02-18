# Autolab shared package

This repository provides the shared `autolab` workflow package used across projects.

## Install

From this directory (`~/Workspaces/autolab`):

```bash
python -m pip install -e .
```

From a different location:

```bash
python -m pip install /Users/tjenrung/Workspaces/autolab
```

After install, invoke with:

```bash
autolab --help
python -m autolab --help
```

## Source layout

- `src/autolab/`: Python package modules (`__main__`, `todo_sync`, `slurm_job_list`)
- `dotautolab/.autolab/`: Shared default scaffold assets (prompts, schemas, verifier helpers, defaults)
