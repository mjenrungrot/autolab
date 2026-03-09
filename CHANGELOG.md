# Changelog

## [1.2.49] - 2026-03-09

### Summary

- Reworked `autolab oracle` into a neutral, self-contained expert-review handoff and updated the browser roundtrip prompt so external reviewers can reply in a less rigid format.
- Added hybrid Oracle reply ingestion so `autolab oracle apply` and browser roundtrips accept either structured `ReviewerVerdict:` replies or free-form expert feedback via a configured local ingestion LLM, while keeping advisory-only apply semantics.

<!-- autolab:range v1.2.48..v1.2.49 -->

## [1.2.48] - 2026-03-08

### Summary

- Added the new Oracle continuation-packet fields to the scaffold handoff schema so generated `handoff.json` artifacts validate cleanly during verify, golden-iteration, and brownfield canary flows.

<!-- autolab:range v1.2.47..v1.2.48 -->

## [1.2.47] - 2026-03-08

### Summary

- Added browser-only Oracle roundtrips with `autolab oracle roundtrip --auto`, persisted Oracle state/response artifacts, one-shot epoch limiting, advisory-only reply application, and Oracle status surfacing across progress, handoff, resume, docs views, and the TUI.
- Reworked `autolab oracle` into a deterministic continuation-packet export and hardened Oracle governance around browser CLI invocation, apply-policy allowlists, graceful failure handling, stable Oracle epochs, and campaign/loop reactions to Oracle advice.

<!-- autolab:range v1.2.46..v1.2.47 -->

## [1.2.46] - 2026-03-08

### Summary

- Added `autolab report --campaign` to generate a scope-root `morning_report.md` wake-up view with champion status, candidate totals, best recent delta, failure themes, and oracle/review follow-up guidance.
- Added bundled `experiment_search` and `integration_change` policy presets, active preset tracking in effective policy state, and a Home-screen TUI action for applying presets directly from the cockpit.

<!-- autolab:range v1.2.45..v1.2.46 -->

## [1.2.45] - 2026-03-08

### Summary

- Added campaign idea-journal novelty memory so unattended search records per-idea thesis, family, touched surfaces, retries, keep/discard/crash outcomes, and bounded family aggregates inside `.autolab/campaign.json`.
- Surfaced novelty-aware summaries through campaign status, handoff/continuation packets, generated results, and implementation/decide-repeat prompt context so campaigns can avoid repeating failed families and highlight near misses.

<!-- autolab:range v1.2.44..v1.2.45 -->

## [1.2.44] - 2026-03-08

### Summary

- Added campaign autonomy governance so unattended search now tracks active challengers, retries recoverable failures, restores the champion after crash-style outcomes, and escalates to `needs_rethink` with an oracle export on stagnation or crash-threshold breaches.
- Added champion-relative timeout cancellation plus governance-aware campaign status and handoff surfacing, including new policy thresholds, candidate/governance state, remote `scancel` support, and regression coverage for retry, stagnation, and timeout flows.

<!-- autolab:range v1.2.43..v1.2.44 -->

## [1.2.43] - 2026-03-08

### Summary

- Added `autolab oracle apply` so expert notes can be ingested from a file or stdin, classified through the local agent stack, and written back into discuss/research sidecars, TODOs, campaign feedback, and optional plan-approval notes.
- Added campaign oracle-feedback persistence plus campaign-status steering for `stop` and `rethink` signals, and refreshed handoff/continuation updates after oracle feedback is applied.

<!-- autolab:range v1.2.42..v1.2.43 -->

## [1.2.42] - 2026-03-08

### Summary

- Added campaign `--lock design|harness` mode with persisted lock contracts and drift detection so unattended search stops safely when hypothesis, design, parser, evaluator, or remote-profile contracts change.
- Added locked-campaign `implementation` decide-repeat support and lock-aware prompt, status, handoff, and runtime guidance so locked search stays in implementation while improving and escalates to redesign when it stalls.

<!-- autolab:range v1.2.41..v1.2.42 -->

## [1.2.41] - 2026-03-08

### Summary

- Added generated campaign `results.tsv` and `results.md` ledgers that replay baseline, kept, discarded, partial, and crashed runs from campaign artifacts.
- Surfaced campaign results paths and counts across campaign status, general status, handoff packets, and oracle exports for overnight experiment review.

<!-- autolab:range v1.2.40..v1.2.41 -->

## [1.2.40] - 2026-03-08

### Summary

- Added champion/challenger campaign arbitration that compares each completed challenger run against the active champion at `decide_repeat` and promotes or discards automatically.
- Added campaign-owned champion snapshots and strict discard restore so unattended experiment search can continue from the accepted champion state after non-winning runs.

<!-- autolab:range v1.2.39..v1.2.40 -->

## [1.2.39] - 2026-03-08

### Summary

- Added first-class unattended campaign mode with `autolab campaign start|status|stop|continue` and canonical `.autolab/campaign.json` state.
- Surfaced campaign context across `autolab status`, handoff/continuation packets, resume recommendations, and `autolab oracle` exports, including resumable `autolab campaign continue` guidance.

<!-- autolab:range v1.2.38..v1.2.39 -->

## [1.2.38] - 2026-03-08

### Summary

- Added a nested `continuation_packet` to handoff artifacts so `autolab progress`, `autolab handoff`, `autolab resume`, docs views, and the TUI can share one compact continuation source.
- Added `autolab oracle` to generate an on-demand scope-root `oracle.md` expert-review export with the continuation packet plus inlined artifact content.

<!-- autolab:range v1.2.37..v1.2.38 -->

## [1.2.37] - 2026-03-08

### Summary

- Promoted `checkpoint`, `remote`, and `hooks` into the top-level command map, help groups, and quickstart/README guidance so those surfaces are easier to discover.
- Added home-view TUI actions for manual checkpoint creation, remote profile diagnostics, and the Autolab post-commit hook installer, with matching action and help coverage.

<!-- autolab:range v1.2.36..v1.2.37 -->

## [1.2.36] - 2026-03-08

### Summary

- Added `autolab uat init --suggest` to scaffold Markdown UAT artifacts from touched project-wide surfaces, including bootstrap, docs, remote-profile, and evaluator defaults.
- Surfaced required UAT blockers more clearly across progress, handoff, prompts, and the TUI, with recommended `autolab uat init --suggest` follow-up guidance when UAT is still pending.

<!-- autolab:range v1.2.35..v1.2.36 -->

## [1.2.35] - 2026-03-07

### Summary

- Added `autolab gc` with preview-first retention controls for checkpoints, reset archives, execution artifacts, traceability outputs, and managed docs-view projections.
- Added checkpoint pin and unpin commands plus protection metadata so important recovery points can be retained or explicitly made prunable.

<!-- autolab:range v1.2.34..v1.2.35 -->

## [1.2.34] - 2026-03-06

### Summary

- Added a packaged brownfield workflow canary fixture and integration coverage so installs exercise a realistic repo-shaped Autolab workflow.
- Removed product-specific `tinydesk` naming from the bundled canary fixture and refreshed related verifier, parser, remote, and compatibility test coverage.

<!-- autolab:range v1.2.33..v1.2.34 -->

## [1.2.33] - 2026-03-06

### Summary

- Added iteration-scoped Markdown UAT artifacts with `autolab uat init` and plan-approval UAT overrides.
- Enforced required UAT pass status in implementation review, launch, handoff/progress reporting, prompts, and scaffold verifiers.

<!-- autolab:range v1.2.32..v1.2.33 -->

## [1.2.32] - 2026-03-05

### Summary

- Added first-class remote execution profiles with `autolab remote show`, `autolab remote doctor`, and `autolab remote smoke`.
- Added revision-label-based remote SLURM launch and allowlisted artifact pullback without local dataset sync.
- Hardened remote launch/sync handling, manifest provenance, and CLI profile compatibility checks.

<!-- autolab:range v1.2.31..v1.2.32 -->

## [1.2.31] - 2026-03-05

### Summary

- Added effective policy inheritance: runtime merge of scaffold, preset, host, scope, stage, risk, and repo-local policy layers with per-key provenance tracking.
- Added `autolab policy show --effective` to inspect the merged policy and `--json` to output the `effective_policy.json` artifact.
- Added `autolab policy doctor --explain` to display the full resolution chain and risk flag derivation.
- Added `policy_resolution`, `profile_mode`, `uat_surface_patterns`, and `policy_overlays` sections to scaffold `verifier_policy.yaml`.
- Added policy summary card to the TUI home view showing active preset, host/scope/profile, and risk flags.
- Added `effective_policy_summary` to checkpoint manifests for policy-aware recovery.

<!-- autolab:range v1.2.30..v1.2.31 -->

## [1.2.30] - 2026-03-05

### Summary

- Added formalized workflow checkpoints with `autolab checkpoint create` and `autolab checkpoint list` commands for saving and inspecting known-good continuation points.
- Added targeted reset via `autolab reset --to checkpoint:<id>` and `autolab reset --to stage:<name>` with automatic archiving of current artifacts before restoration.
- Added auto-checkpoint triggers on stage transitions, plan approval, handoff refresh, and decide-repeat decisions so recovery points are created without manual intervention.
- Added context-rot detection that compares current artifact fingerprints against checkpoint snapshots, surfaced in `autolab progress` and the TUI recovery card.
- Added `autolab hooks install` to set up a post-commit git hook for automatic version tagging and commit-triggered checkpoints.
- Added checkpoint integrity verifier running at implementation_review and decide_repeat stages.

<!-- autolab:range v1.2.29..v1.2.30 -->

## [1.2.29] - 2026-03-06

### Summary

- Updated `autolab research` so local agent surface detection no longer hard-fails when `claude`/`codex` are unavailable during test/mocked runs.
- Kept runtime enforcement unchanged for real agent execution so research still returns a clear error when no local LLM CLI is configured or discoverable.
- Synchronized release metadata for this patch, including the pinned `README.md` install tag.

<!-- autolab:range v1.2.28..v1.2.29 -->

## [1.2.28] - 2026-03-05

### Summary

- Restored TUI wave observability when `handoff.json` stage metadata lags behind `state.json`.
- Added a repo-local `AGENTS.md` rule forbidding `git commit --no-verify` except for explicit version-bump or release-only commits.
- Applied formatter-required cleanup in `tests/test_plan_approval_surfaces.py` so normal commit hooks pass.

<!-- autolab:range v1.2.27..v1.2.28 -->

## [1.2.27] - 2026-03-05

### Summary

- Added bundled Codex semantic-role skills (`researcher`, `planner`, `plan-checker`, `reviewer`) plus install/docs coverage for project-local skill copies.
- Added prompt and research-command agent-surface guidance so implementation, review, and research flows can advertise installed semantic roles without breaking fallback behavior.
- Tightened `run --execute-approved-plan` so it requires current planning artifacts plus a matching current approval artifact instead of silently regenerating plan state.

<!-- autolab:range v1.2.26..v1.2.27 -->

## [1.2.26] - 2026-03-05

### Summary

- Added promotion-safe implementation planning with required approval risk artifacts, plus `run --plan-only`, `approve-plan`, and `run --execute-approved-plan` for high-risk mixed-scope runs.
- Hardened handoff and verification behavior around plan approval, wave observability, and generated verifier outputs, including stable schema-error reporting and safe omission of absent approval artifacts.
- Updated golden fixtures, schemas, and regression coverage so the new implementation checkpoint flow validates cleanly across the full test suite.

<!-- autolab:range v1.2.25..v1.2.26 -->

## [1.2.25] - 2026-03-05

### Summary

- Added optional `autolab discuss` and `autolab research` commands that capture user intent and scoped evidence into canonical JSON+Markdown sidecars for project-wide and experiment work.
- Added compact discuss/research context consumption for `design` and `implementation`, plus advisory `design_context_quality.json` reporting so design quality improves when resolved discuss context is used but still works cleanly when sidecars are absent.
- Hardened sidecar lineage and runtime safety by enforcing resolver-backed context refs, blocking undeclared promoted-context injection into task execution, and stripping raw merged sidecar payloads from runner-facing stage context JSON.

<!-- autolab:range v1.2.24..v1.2.25 -->

## [1.2.24] - 2026-03-05

### Summary

- Added a scope-aware sidecar resolver and provenance engine that powers `autolab render --view context`, showing exactly which `project_map`, `context_delta`, and project-wide or experiment sidecars were loaded and why.
- Added optional discuss/research sidecar schemas plus verifier enforcement for dependency fingerprints, scope-root identity, and experiment identity on experiment-scoped sidecars.
- Hardened context resolution against wrong-experiment bundle pointers and invalid sidecar metadata, with diagnostics and regression coverage for stale or out-of-scope artifacts.

<!-- autolab:range v1.2.23..v1.2.24 -->

## [1.2.23] - 2026-03-05

### Summary

- Added end-to-end wave observability across `autolab progress`, generated docs views, handoff, and the TUI, including critical path, per-wave timings, retries, blocked/deferred/skipped tasks, file-conflict detail, and per-task evidence.
- Hardened observability correctness by filtering stale iteration-mismatched artifacts, modeling critical paths with wave barriers, and separating current retry state from historical retry history.
- Tightened observability contracts with richer execution artifacts plus stricter schema/verifier coverage for task ids, reason codes, timestamps, and review-stage plan graph/check outputs.

<!-- autolab:range v1.2.22..v1.2.23 -->

## [1.2.22] - 2026-03-05

### Summary

- Added generated `autolab docs generate` projection views (`project`, `roadmap`, `state`, `requirements`, `sidecar`) sourced from canonical state/backlog/handoff/traceability/context artifacts.
- Hardened docs-view loading and path handling with repo-contained pointer resolution, non-regular-file rejection, bounded artifact reads, safe numeric coercion, and stricter `--output-dir` containment/error handling.
- Restored compatibility-first docs behavior by defaulting `docs generate` to `--view registry`, while keeping generated projection views available via explicit `--view` selection and adding expanded regression coverage.

<!-- autolab:range v1.2.21..v1.2.22 -->

## [1.2.21] - 2026-03-05

### Summary

- Added first-class parser authoring commands: `autolab parser init` scaffolds parser modules plus capability manifests, and `autolab parser test` validates parser behavior in isolated or in-place modes.
- Added packaged golden parser fixture packs and parser capability schemas/index, with stricter design-time capability validation (parser kind + metric compatibility) wired into scaffold schema checks.
- Hardened scaffold sync and packaging contracts for parser assets by skipping Python cache artifacts (`__pycache__`, `*.pyc`) and expanding package-data coverage/tests for parser fixture repositories and expected outputs.

<!-- autolab:range v1.2.20..v1.2.21 -->

## [1.2.20] - 2026-03-05

### Summary

- Tightened project-wide expected-artifact verification so `scope_kind=project_wide` only accepts artifacts under configured `scope_roots.project_wide_root`.
- Added regression tests for project-wide out-of-scope artifact rejection, project-wide scope-violation enforcement in runner execution, and `{scope_root}` token substitution in runner commands.
- Added failure-path coverage for invalid `scope_roots.project_wide_root` in `autolab render` and `autolab docs generate`, and documented scope-root path constraints in README.

<!-- autolab:range v1.2.19..v1.2.20 -->

## [1.2.19] - 2026-03-05

### Summary

- Added configurable `scope_roots.project_wide_root` and routed project-wide scope resolution through it for runner task execution, handoff, prompt render context, and docs generation.
- Replaced runner edit-scope modes with scope-root-aware modes (`scope_root_plus_core`, `scope_root_only`) and added explicit scope-root runtime surfaces (`{scope_root}`, `AUTOLAB_SCOPE_ROOT`).
- Expanded regression coverage for scope-root policy validation, project-wide handoff/render behavior, runner project-wide scope enforcement, and scope-root-aware implementation execution helpers.

<!-- autolab:range v1.2.18..v1.2.19 -->

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
