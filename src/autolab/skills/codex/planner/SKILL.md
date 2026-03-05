---
name: planner
description: >
  [EXPLICIT INVOCATION ONLY] Turn approved scope into dependency-safe, verifiable implementation tasks aligned with Autolab contracts.
metadata:
  invocation: explicit-only
---

# Planner

Use this skill when the user explicitly asks for `$planner` or when Autolab packets recommend the `planner` semantic role.

## Scope

- Planning only. Do not implement code.
- Produce tasks that match Autolab scope, verification, and wave-safety rules.
- Keep plans compatible with the current implementation contract and task-packet model.

## Required behavior

1. Read the resolved context packet and active design inputs first.
2. Produce atomic tasks with explicit dependencies and verification intent.
3. Respect scope boundaries and current `allowed_edit_dirs`.
4. Prefer compact plans that maximize safe parallelism.

## Guardrails

- Do not invent requirements that are not grounded in design or promoted constraints.
- Do not route project-wide work through experiment-local sidecars.
- Treat task `touches`, conflict risk, and verification coverage as first-class planning constraints.
