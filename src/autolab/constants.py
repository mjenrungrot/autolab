"""Autolab constants — stage definitions, templates, patterns, and defaults.

`workflow.yaml` is the canonical source of stage metadata.  These module-level
constants are derived from the bundled registry for compatibility with existing
imports and tests.
"""

from __future__ import annotations

import re
from pathlib import Path

from autolab.registry import (
    registry_brief_prompt_files,
    registry_human_prompt_files,
    load_registry,
    registry_active_stages,
    registry_all_stages,
    registry_decision_stages,
    registry_prompt_files,
    registry_runner_prompt_files,
    registry_required_tokens,
    registry_runner_eligible,
    registry_terminal_stages,
)

_FALLBACK_ACTIVE_STAGES = (
    "hypothesis",
    "design",
    "implementation",
    "implementation_review",
    "launch",
    "slurm_monitor",
    "extract_results",
    "update_docs",
    "decide_repeat",
)
PACKAGE_SCAFFOLD_DIR = Path(__file__).resolve().parent / "scaffold" / ".autolab"
_FALLBACK_TERMINAL_STAGES = ("human_review", "stop")
_FALLBACK_DECISION_STAGES = ("hypothesis", "design", "stop", "human_review")
_FALLBACK_RUNNER_ELIGIBLE_STAGES = (
    "hypothesis",
    "design",
    "implementation",
    "implementation_review",
    "update_docs",
    "decide_repeat",
)
_FALLBACK_ALL_STAGES = set(_FALLBACK_ACTIVE_STAGES + _FALLBACK_TERMINAL_STAGES)
_FALLBACK_STAGE_PROMPT_FILES = {
    "hypothesis": "stage_hypothesis.audit.md",
    "design": "stage_design.audit.md",
    "implementation": "stage_implementation.audit.md",
    "implementation_review": "stage_implementation_review.audit.md",
    "launch": "stage_launch.audit.md",
    "slurm_monitor": "stage_slurm_monitor.audit.md",
    "extract_results": "stage_extract_results.audit.md",
    "update_docs": "stage_update_docs.audit.md",
    "decide_repeat": "stage_decide_repeat.audit.md",
    "human_review": "stage_human_review.audit.md",
    "stop": "stage_stop.audit.md",
}
_FALLBACK_STAGE_RUNNER_PROMPT_FILES = {
    stage: filename.replace(".audit.md", ".runner.md")
    for stage, filename in _FALLBACK_STAGE_PROMPT_FILES.items()
}
_FALLBACK_STAGE_BRIEF_PROMPT_FILES = {
    stage: filename.replace(".audit.md", ".brief.md")
    for stage, filename in _FALLBACK_STAGE_PROMPT_FILES.items()
}
_FALLBACK_STAGE_HUMAN_PROMPT_FILES = {
    stage: filename.replace(".audit.md", ".human.md")
    for stage, filename in _FALLBACK_STAGE_PROMPT_FILES.items()
}
_FALLBACK_PROMPT_REQUIRED_TOKENS_BY_STAGE = {
    "hypothesis": {"iteration_id", "iteration_path", "hypothesis_id"},
    "design": {"iteration_id", "iteration_path", "hypothesis_id"},
    "implementation": {"iteration_id", "iteration_path"},
    "implementation_review": {"iteration_id", "iteration_path"},
    "launch": {"iteration_id", "iteration_path", "run_id"},
    "slurm_monitor": {"iteration_id", "iteration_path", "run_id"},
    "extract_results": {"iteration_id", "iteration_path", "run_id"},
    "update_docs": {"iteration_id", "iteration_path", "run_id"},
    "decide_repeat": {"iteration_id", "iteration_path", "run_id"},
}


def _load_bundled_registry() -> dict:
    try:
        # PACKAGE_SCAFFOLD_DIR.parent resolves to src/autolab/scaffold
        return load_registry(PACKAGE_SCAFFOLD_DIR.parent)
    except Exception:
        return {}


_BUNDLED_REGISTRY = _load_bundled_registry()

ACTIVE_STAGES = (
    registry_active_stages(_BUNDLED_REGISTRY)
    if _BUNDLED_REGISTRY
    else _FALLBACK_ACTIVE_STAGES
)
TERMINAL_STAGES = (
    registry_terminal_stages(_BUNDLED_REGISTRY)
    if _BUNDLED_REGISTRY
    else _FALLBACK_TERMINAL_STAGES
)
DECISION_STAGES = (
    registry_decision_stages(_BUNDLED_REGISTRY)
    if _BUNDLED_REGISTRY
    else _FALLBACK_DECISION_STAGES
)
RUNNER_ELIGIBLE_STAGES = (
    registry_runner_eligible(_BUNDLED_REGISTRY)
    if _BUNDLED_REGISTRY
    else _FALLBACK_RUNNER_ELIGIBLE_STAGES
)
ALL_STAGES = (
    registry_all_stages(_BUNDLED_REGISTRY)
    if _BUNDLED_REGISTRY
    else _FALLBACK_ALL_STAGES
)

DEFAULT_BACKLOG_TEMPLATE = """hypotheses:
  - id: h1
    status: open
    title: "Bootstrap hypothesis"
    success_metric: "primary_metric"
    target_delta: 0.0
experiments:
  - id: e1
    hypothesis_id: h1
    status: open
    type: plan
    iteration_id: "{iteration_id}"
"""

DEFAULT_VERIFIER_POLICY = (PACKAGE_SCAFFOLD_DIR / "verifier_policy.yaml").read_text(
    encoding="utf-8"
)

LOCK_STALE_SECONDS = 30 * 60
DEFAULT_MAX_HOURS = 8.0
AGENT_RUNNER_PRESETS: dict[str, str] = {
    "codex": "codex exec --full-auto -C {workspace_dir} {core_add_dirs} -",
    "claude": "env -u CLAUDECODE claude -p --output-format text --verbose -",
}
AGENT_RUNNER_CODEX_DANGEROUS_PRESET = "codex exec --dangerously-bypass-approvals-and-sandbox -C {workspace_dir} {core_add_dirs} -"
AGENT_RUNNER_CLAUDE_DANGEROUS_PRESET = "env -u CLAUDECODE claude -p --dangerously-skip-permissions --output-format text --verbose -"
DEFAULT_AGENT_RUNNER_NAME = "codex"
DEFAULT_AGENT_RUNNER_COMMAND = AGENT_RUNNER_PRESETS[DEFAULT_AGENT_RUNNER_NAME]
DEFAULT_AGENT_RUNNER_STAGES = tuple(RUNNER_ELIGIBLE_STAGES)
DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS = 3600.0
DEFAULT_AGENT_RUNNER_EDIT_SCOPE_MODE = "scope_root_plus_core"
AGENT_RUNNER_EDIT_SCOPE_MODES = ("scope_root_only", "scope_root_plus_core")
DEFAULT_AGENT_RUNNER_CORE_DIRS = (
    "src",
    "scripts",
    ".autolab",
    "docs",
    "paper",
    "tests",
)
DEFAULT_AGENT_RUNNER_ENSURE_ITERATION_DIR = True
DEFAULT_PROJECT_WIDE_ROOT = "."
DEFAULT_AUTO_COMMIT_MODE = "meaningful_only"
AUTO_COMMIT_MODES = ("meaningful_only", "always", "disabled")
DEFAULT_MEANINGFUL_EXCLUDE_PATHS = (
    ".autolab/**",
    "docs/todo.md",
    "docs/wiki/**",
    "experiments/*/*/docs_update.md",
)
DEFAULT_IMPLEMENTATION_CYCLE_EXCLUDE_PATHS = (
    ".autolab/**",
    "docs/todo.md",
    "**/implementation_review.md",
    "**/review_result.json",
)
DEFAULT_PLAN_EXECUTION_ENABLED = True
DEFAULT_PLAN_EXECUTION_RUN_UNIT = "wave"
DEFAULT_PLAN_EXECUTION_MAX_PARALLEL_TASKS = 4
DEFAULT_PLAN_EXECUTION_TASK_RETRY_MAX = 1
DEFAULT_PLAN_EXECUTION_WAVE_RETRY_MAX = 2
DEFAULT_PLAN_EXECUTION_FAILURE_MODE = "finish_wave_then_stop"
DEFAULT_PLAN_EXECUTION_ON_WAVE_RETRY_EXHAUSTED = "human_review"
DEFAULT_PLAN_EXECUTION_REQUIRE_VERIFICATION_COMMANDS = True
PLAN_EXECUTION_RUN_UNITS = ("wave",)
PLAN_EXECUTION_FAILURE_MODES = ("finish_wave_then_stop", "fail_fast")
ASSISTANT_CYCLE_STAGES = ("select", "implement", "verify", "review", "done")
ASSISTANT_CONTROL_COMMIT_PATHS = (
    ".autolab/agent_result.json",
    ".autolab/state.json",
    ".autolab/todo_state.json",
    "docs/todo.md",
)
TODO_DOC_SYNC_PRE_STAGES = ("decide_repeat", "human_review")
TODO_DOC_SYNC_POST_STAGES = ("implementation", "update_docs", "human_review")
# ---------------------------------------------------------------------------
# Canonical status vocabularies (mirrored from verifier_lib for package-side use)
# ---------------------------------------------------------------------------

SYNC_SUCCESS_STATUSES: frozenset[str] = frozenset(
    {"ok", "completed", "success", "passed"}
)
COMPLETION_LIKE_STATUSES: frozenset[str] = frozenset({"completed", "failed"})
IN_PROGRESS_STATUSES: frozenset[str] = frozenset(
    {"pending", "submitted", "running", "synced"}
)
RUN_MANIFEST_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "submitted",
        "running",
        "synced",
        "completed",
        "failed",
        "partial",
    }
)

BACKLOG_COMPLETED_STATUSES = {"done", "completed", "closed", "resolved"}
EXPERIMENT_TYPES = ("plan", "in_progress", "done")
EXPERIMENT_LOCKED_TYPES = {"done"}
DEFAULT_EXPERIMENT_TYPE = "plan"
ITERATION_ID_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
RUN_ID_TIMESTAMP_PATTERN = re.compile(r"(20\d{6}T\d{6}Z)")
STAGE_PROMPT_FILES = (
    registry_prompt_files(_BUNDLED_REGISTRY)
    if _BUNDLED_REGISTRY
    else _FALLBACK_STAGE_PROMPT_FILES
)
STAGE_RUNNER_PROMPT_FILES = (
    registry_runner_prompt_files(_BUNDLED_REGISTRY)
    if _BUNDLED_REGISTRY
    else _FALLBACK_STAGE_RUNNER_PROMPT_FILES
)
STAGE_BRIEF_PROMPT_FILES = (
    registry_brief_prompt_files(_BUNDLED_REGISTRY)
    if _BUNDLED_REGISTRY
    else _FALLBACK_STAGE_BRIEF_PROMPT_FILES
)
STAGE_HUMAN_PROMPT_FILES = (
    registry_human_prompt_files(_BUNDLED_REGISTRY)
    if _BUNDLED_REGISTRY
    else _FALLBACK_STAGE_HUMAN_PROMPT_FILES
)
SLURM_JOB_LIST_PATH = Path("docs/slurm_job_list.md")
PROMPT_TOKEN_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
PROMPT_LITERAL_TOKENS = ("<ITERATION_ID>", "<ITERATION_PATH>", "<RUN_ID>")
PROMPT_SHARED_INCLUDE_PATTERN = re.compile(r"\{\{\s*shared:([A-Za-z0-9_.-]+)\s*\}\}")
PROMPT_REQUIRED_TOKENS_BY_STAGE = (
    registry_required_tokens(_BUNDLED_REGISTRY)
    if _BUNDLED_REGISTRY
    else _FALLBACK_PROMPT_REQUIRED_TOKENS_BY_STAGE
)
REVIEW_RESULT_REQUIRED_CHECKS = (
    "tests",
    "dry_run",
    "schema",
    "env_smoke",
    "docs_target_update",
)
REVIEW_RESULT_CHECK_STATUSES = {"pass", "skip", "fail"}
HOST_MODE_COMMAND_TIMEOUT_SECONDS = 2
VERIFIER_COMMAND_TIMEOUT_SECONDS = 120
