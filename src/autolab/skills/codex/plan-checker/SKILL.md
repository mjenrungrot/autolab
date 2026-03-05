---
name: plan-checker
description: >
  [EXPLICIT INVOCATION ONLY] Critique an implementation plan for dependency gaps, scope violations, missing verification, and promotion mistakes before execution.
metadata:
  invocation: explicit-only
---

# Plan Checker

Use this skill when the user explicitly asks for `$plan-checker` or when Autolab packets recommend the `plan_checker` semantic role.

## Scope

- Review a plan or plan packet only. Do not implement or rewrite unrelated code.
- Focus on execution safety and contract completeness.
- Return concrete findings and remediation, not broad commentary.

## Required behavior

1. Check dependency ordering and wave safety.
2. Check scope legality, especially mixed-scope and promoted-context usage.
3. Check verification coverage and expected artifact declarations.
4. Call out any ambiguity that would make execution unsafe.

## Guardrails

- Do not approve a plan on vague evidence.
- Do not replace canonical contract validation; treat this as advisory critique.
- Prefer precise findings tied to task ids, requirement ids, or artifact paths.
