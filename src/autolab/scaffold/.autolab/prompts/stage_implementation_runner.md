# Deprecated Template: stage_implementation_runner.md

This file is retained only for scaffold backward compatibility.

Autolab does not resolve this legacy path for runner execution when workflow
registry mappings are present. The active implementation prompt pack is:

- `stage_implementation.runner.md` (runner payload)
- `stage_implementation.audit.md` (audit contract)
- `stage_implementation.brief.md` (retry/handoff brief)
- `stage_implementation.human.md` (human-facing packet)

If custom tooling still references `stage_implementation_runner.md`, migrate to
`stage_implementation.runner.md`.
