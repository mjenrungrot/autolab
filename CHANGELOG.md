# Changelog

## [1.2.18] - 2026-03-05

### Summary

- Added end-to-end traceability coverage artifacts linking requirement -> task -> verifier -> metrics/result -> decision, with `autolab trace` CLI generation.
- `decide_repeat` now refreshes traceability artifacts non-blockingly and consistently rewrites iteration `decision_result.json` for manual and auto decisions.
- Added traceability schemas/verifier checks (`traceability_coverage`, `traceability_latest`), advisory-vs-strict policy handling, and expanded traceability test coverage.

<!-- autolab:range v1.2.17..v1.2.18 -->

## [1.2.17] - 2026-03-05

### Summary

- Hardened prompt extraction rendering to strip sentinel placeholders, normalize booleans/integers, and avoid leaking unknown/unavailable markers into runner-facing prompts.
- Expanded prompt lint enforcement for runner packs, including stricter checks for duplicated guidance, audit leakage, and stage-irrelevant policy content.
- Added milestone verification coverage to lock deterministic launch/slurm-monitor execution paths and prompt-pack DoD guarantees.
- Updated docs to reflect compact review briefs, stricter prompt hygiene, and runtime contract behavior for this milestone.

<!-- autolab:range v1.2.16..v1.2.17 -->

## [1.2.16] - 2026-03-05

### Summary

- De-LLM'd operational execution paths by adding deterministic runtimes for `launch`, `slurm_monitor`, and `extract_results`, including multi-run monitor aggregation and task-level runtime evidence handling.
- Hardened parser and command execution surfaces with stricter policy controls, safer command templating, run/path containment checks, and timeout-aware hook execution.
- Tightened prompt/runtime contracts: `design.yaml` now requires `extract_parser`, deterministic stages reject `run_agent_mode=force_on`, and memory guidance moved to stage-opted compact memory briefs with orchestration-scoped todo/doc reconciliation.
- Aligned runtime docs/contracts with implementation behavior: `launch` owns SLURM ledger append semantics, while `slurm_monitor` owns run-manifest progress updates and monitor logs.

<!-- autolab:range v1.2.15..v1.2.16 -->

## [1.2.15] - 2026-03-04

### Summary

- Hardened default-branch pre-commit versioning by requiring `README.md`'s pinned install tag to match the current `pyproject.toml` version before bumping.
- Added regression coverage for stale README-tag detection and updated hook docs to describe the new version-freshness check.

<!-- autolab:range v1.2.14..v1.2.15 -->

## [1.2.14] - 2026-03-04

### Summary

- Added implementation plan execution as the runtime plan of record with wave scheduling, per-task runner prompts/context, task-level verification, and execution state/summary artifacts.
- Hardened implementation execution controls and policy handling (`failure_mode`, `failure_policy`, retry semantics, `--auto` continuation, and explicit `force_off` incompatibility when execution summary gating is required).
- Expanded schema/prompt/test coverage for plan execution (new execution-state schema, tightened contract requirements including `objective`, runner prompt isolation, and end-to-end regressions).

<!-- autolab:range v1.2.13..v1.2.14 -->

## [1.2.13] - 2026-03-04

### Summary

- Reworked `autolab render` to use explicit view selection (`--view runner|audit|brief|human|context`) and added `--stats` prompt-debug reports.
- Added prompt-debug diagnostics for line counts, token estimates, largest sections, dropped sections, and warning classes (duplicate headers, sentinel leaks, raw blob injection, stage-irrelevant includes).
- Updated render/docs/test contracts for read-only behavior, legacy `--audience` hard-fail semantics, and expanded render/debug regression coverage.

<!-- autolab:range v1.2.12..v1.2.13 -->

## [1.2.12] - 2026-03-04

### Summary

- Refactored stage prompting into strict audience-scoped packets (`runner`, `audit`, `brief`, `human`, `context`) and removed legacy single-file fallback behavior.
- Hardened runner prompt policy/lint contracts (non-negotiables, required token presence, status-vocabulary scoping, banned audit payload leakage, transitive include checks).
- Expanded prompt/render/TUI/CLI regression coverage and aligned docs/scaffold guidance with the strict prompt-pack model.

<!-- autolab:range v1.2.11..v1.2.12 -->

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
