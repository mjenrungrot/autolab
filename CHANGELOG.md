# Changelog

## [1.2.11] - 2026-03-04

### Summary

- Fixed the cockpit TUI header so narrow terminals keep a compact single-line status rail instead of expanding into a tall blank region.
- Added regression coverage to lock status-rail/workspace placement in narrow viewport sizes.

<!-- autolab:range v1.2.10..v1.2.11 -->

## [1.2.10] - 2026-03-04

### Summary

- Fixed `autolab verify-golden` CLI wiring so internal verifier invocations resolve through the canonical `autolab.commands.main` entrypoint.
- Updated golden verification setup to copy the full packaged `.autolab` fixture set, including `plan_contract.json`, to prevent implementation-stage verifier failures.

<!-- autolab:range v1.2.9..v1.2.10 -->

## [1.2.9] - 2026-03-04

### Summary

- Refactored the CLI monolith into a split `autolab.cli` package with dedicated handler/parser modules while preserving command behavior.
- Introduced a typed orchestration layer (`RunRequest`, `LoopRequest`, `OrchestrationEngine`, and mode adapters) and moved standard/assistant runtimes under `autolab.orchestration`.
- Converted legacy entry modules (`commands.py`, `run_standard.py`, `run_assistant.py`) into compatibility shims to retain existing imports and test monkeypatch hooks.

<!-- autolab:range v1.2.8..v1.2.9 -->

## [1.2.8] - 2026-03-04

### Summary

- Added a true brownfield bootstrap path via `autolab init --from-existing` that scans existing repositories and seeds Autolab context.
- Introduced scope-aware context inheritance artifacts (`project_map`, `context_delta`, and `bundle`) and surfaced them in runtime stage context for prompt consumers.
- Extended schema/verifier coverage plus docs/tests for brownfield context artifacts and `init` onboarding behavior.

<!-- autolab:range v1.2.7..v1.2.8 -->

## [1.2.7] - 2026-03-04

### Summary

- Added first-class `autolab progress`, `autolab handoff`, and `autolab resume` commands for takeover and safe resume workflows.
- Introduced machine and human handoff artifacts (`.autolab/handoff.json`, `<scope-root>/handoff.md`) with automatic refresh across verify/run/loop and manual stage-steering exits.
- Extended TUI Home with a dedicated handoff/resume panel, added handoff schema validation, and updated docs/tests for end-to-end coverage.

<!-- autolab:range v1.2.6..v1.2.7 -->

## [1.2.6] - 2026-03-04

### Summary

- Added a checked implementation plan contract loop with machine-readable DAG artifacts and execution gating before implementation runs.
- Introduced new plan-contract/check-result/graph schemas and an `implementation_plan_contract` verifier integrated into stage verification and explain flows.
- Updated design/workflow/prompt scaffolding plus golden fixtures and tests to require `implementation_requirements` and contract outputs end to end.

<!-- autolab:range v1.2.5..v1.2.6 -->

## [1.2.5] - 2026-03-04

### Summary

- Split implementation-stage prompting into a prompt-pack with dedicated runner, context, audit, and retry-brief artifacts.
- Updated `autolab render`, runner integrations, and TUI views to expose runner/audit/retry prompt surfaces explicitly.
- Added schema/lint/readiness and regression-test coverage for implementation runner-prompt routing and fail-fast scaffold sync remediation.

<!-- autolab:range v1.2.4..v1.2.5 -->

## [1.2.4] - 2026-03-04

### Summary

- Made GitHub Actions CI fail when pytest fails by removing non-blocking test execution.
- Fixed TUI command palette deduplication to work with current Textual `SystemCommand` fields.
- Stabilized TUI key-hints rendering and aligned regression tests with current status/output formatting.

<!-- autolab:range v1.2.3..v1.2.4 -->

## [1.2.3] - 2026-03-04

### Summary

- Added the `textual` dependency and updated resolved package pins in `uv.lock`.

<!-- autolab:range v1.2.2..v1.2.3 -->

## [1.2.2] - 2026-03-03

### Summary

- Enforced strict pre-commit changelog validation for the exact release hop `v<previous>..v<current>`.
- Added changelog tooling to scaffold sections, validate release ranges, and render release-note bodies.
- Wired release CI to validate `CHANGELOG.md` and publish notes from the version-scoped changelog section.

<!-- autolab:range v1.2.1..v1.2.2 -->

## [1.2.0] - 2026-03-03

### Summary

- Added an onboarding-focused TUI cockpit flow with rendered prompt preview and guided actions.
- Expanded stage and verifier documentation to clarify workflow ownership, artifacts, and policy behavior.
- Hardened release automation by keeping hook-based version/tag sync behavior aligned with CI workflow checks.

<!-- autolab:range v1.1.70..v1.2.0 -->
