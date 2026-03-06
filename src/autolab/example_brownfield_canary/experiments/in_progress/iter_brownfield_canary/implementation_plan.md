# Implementation Plan

## Scope

- Experiment: refresh iteration-local implementation notes.
- Project-wide: keep `docs/environment.md` and `scripts/bootstrap_venv.sh`
  aligned with the active iteration.

## Mixed-Scope Guardrail

The project-wide task consumes promoted context only through
`promoted:R_repo_bootstrap:pc_bootstrap`.

## Verification

- Review the implementation artifacts.
- Confirm UAT remains `pass` for the shared docs and bootstrap script surface.
