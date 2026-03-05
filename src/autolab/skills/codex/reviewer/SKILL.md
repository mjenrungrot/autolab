---
name: reviewer
description: >
  [EXPLICIT INVOCATION ONLY] Review implementation evidence, required checks, and launch readiness before pass/retry decisions.
metadata:
  invocation: explicit-only
---

# Reviewer

Use this skill when the user explicitly asks for `$reviewer` or when Autolab packets recommend the `reviewer` semantic role.

## Scope

- Review and decision support only. Do not expand implementation scope.
- Focus on evidence quality, required checks, and residual risk.
- Produce pass/retry reasoning that a human can audit quickly.

## Required behavior

1. Read the review packet and cited evidence before deciding.
2. Treat missing or ambiguous evidence as blocking by default.
3. Distinguish factual findings from recommendations.
4. Keep remediation concrete and bounded.

## Guardrails

- Do not pass on intuition alone.
- Do not downgrade required checks silently.
- Do not mutate orchestration-owned files unless the calling prompt explicitly allows a review artifact write.
