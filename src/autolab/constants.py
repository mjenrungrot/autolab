"""Autolab constants — stage definitions, templates, patterns, and defaults.

`workflow.yaml` is the canonical source of stage metadata.  These module-level
constants are derived from the bundled registry for compatibility with existing
imports and tests.
"""

from __future__ import annotations

import re
from pathlib import Path

from autolab.registry import (
    load_registry,
    registry_active_stages,
    registry_all_stages,
    registry_decision_stages,
    registry_prompt_files,
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
_FALLBACK_RUNNER_ELIGIBLE_STAGES = _FALLBACK_ACTIVE_STAGES
_FALLBACK_ALL_STAGES = set(_FALLBACK_ACTIVE_STAGES + _FALLBACK_TERMINAL_STAGES)
_FALLBACK_STAGE_PROMPT_FILES = {
    "hypothesis": "stage_hypothesis.md",
    "design": "stage_design.md",
    "implementation": "stage_implementation.md",
    "implementation_review": "stage_implementation_review.md",
    "launch": "stage_launch.md",
    "slurm_monitor": "stage_slurm_monitor.md",
    "extract_results": "stage_extract_results.md",
    "update_docs": "stage_update_docs.md",
    "decide_repeat": "stage_decide_repeat.md",
    "human_review": "stage_human_review.md",
    "stop": "stage_stop.md",
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
DEFAULT_AGENT_RUNNER_STAGES = tuple(ACTIVE_STAGES)
DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS = 3600.0
DEFAULT_AGENT_RUNNER_EDIT_SCOPE_MODE = "iteration_plus_core"
AGENT_RUNNER_EDIT_SCOPE_MODES = ("iteration_only", "iteration_plus_core")
DEFAULT_AGENT_RUNNER_CORE_DIRS = (
    "src",
    "scripts",
    ".autolab",
    "docs",
    "paper",
    "tests",
)
DEFAULT_AGENT_RUNNER_ENSURE_ITERATION_DIR = True
DEFAULT_AUTO_COMMIT_MODE = "meaningful_only"
AUTO_COMMIT_MODES = ("meaningful_only", "always", "disabled")
DEFAULT_MEANINGFUL_EXCLUDE_PATHS = (
    ".autolab/**",
    "docs/todo.md",
    "docs/wiki/**",
    "experiments/*/*/docs_update.md",
)
ASSISTANT_CYCLE_STAGES = ("select", "implement", "verify", "review", "done")
ASSISTANT_CONTROL_COMMIT_PATHS = (
    ".autolab/agent_result.json",
    ".autolab/state.json",
    ".autolab/todo_state.json",
    "docs/todo.md",
)
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
