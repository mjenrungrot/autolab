"""Autolab constants — stage definitions, templates, patterns, and defaults.

Stage-level constants (ACTIVE_STAGES, STAGE_PROMPT_FILES, etc.) are the built-in
defaults.  At runtime, ``autolab.registry.load_registry()`` reads the canonical
``workflow.yaml`` and provides an overlay that takes precedence where available.
"""

from __future__ import annotations

import re
from pathlib import Path

ACTIVE_STAGES = (
    "hypothesis",
    "design",
    "implementation",
    "implementation_review",
    "launch",
    "extract_results",
    "update_docs",
)
TERMINAL_STAGES = ("human_review", "stop")
DECISION_STAGES = ("hypothesis", "design", "stop", "human_review")
RUNNER_ELIGIBLE_STAGES = ACTIVE_STAGES + ("decide_repeat",)
ALL_STAGES = set(ACTIVE_STAGES + ("decide_repeat",) + TERMINAL_STAGES)
PACKAGE_SCAFFOLD_DIR = Path(__file__).resolve().parent / "scaffold" / ".autolab"

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

DEFAULT_VERIFIER_POLICY = (PACKAGE_SCAFFOLD_DIR / "verifier_policy.yaml").read_text(encoding="utf-8")

LOCK_STALE_SECONDS = 30 * 60
DEFAULT_MAX_HOURS = 8.0
AGENT_RUNNER_PRESETS: dict[str, str] = {
    "codex": "codex exec -s workspace-write -a never -C {workspace_dir} {core_add_dirs} -",
    "claude": "env -u CLAUDECODE claude -p --output-format text --verbose -",
}
AGENT_RUNNER_CLAUDE_DANGEROUS_PRESET = (
    "env -u CLAUDECODE claude -p --dangerously-skip-permissions --output-format text --verbose -"
)
DEFAULT_AGENT_RUNNER_NAME = "codex"
DEFAULT_AGENT_RUNNER_COMMAND = (
    AGENT_RUNNER_PRESETS[DEFAULT_AGENT_RUNNER_NAME]
)
DEFAULT_AGENT_RUNNER_STAGES = tuple(ACTIVE_STAGES)
DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS = 3600.0
DEFAULT_AGENT_RUNNER_EDIT_SCOPE_MODE = "iteration_plus_core"
AGENT_RUNNER_EDIT_SCOPE_MODES = ("iteration_only", "iteration_plus_core")
DEFAULT_AGENT_RUNNER_CORE_DIRS = ("src", "scripts", ".autolab", "docs", "paper", "tests")
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
BACKLOG_COMPLETED_STATUSES = {"done", "completed", "closed", "resolved"}
EXPERIMENT_TYPES = ("plan", "in_progress", "done")
EXPERIMENT_LOCKED_TYPES = {"done"}
DEFAULT_EXPERIMENT_TYPE = "plan"
ITERATION_ID_SAFE_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
RUN_ID_TIMESTAMP_PATTERN = re.compile(r"(20\d{6}T\d{6}Z)")
STAGE_PROMPT_FILES = {
    "hypothesis": "stage_hypothesis.md",
    "design": "stage_design.md",
    "implementation": "stage_implementation.md",
    "implementation_review": "stage_implementation_review.md",
    "launch": "stage_launch.md",
    "extract_results": "stage_extract_results.md",
    "update_docs": "stage_update_docs.md",
    "decide_repeat": "stage_decide_repeat.md",
    "human_review": "stage_human_review.md",
    "stop": "stage_stop.md",
}
SLURM_JOB_LIST_PATH = Path("docs/slurm_job_list.md")
PROMPT_TOKEN_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
PROMPT_LITERAL_TOKENS = ("<ITERATION_ID>", "<ITERATION_PATH>", "<RUN_ID>")
PROMPT_SHARED_INCLUDE_PATTERN = re.compile(r"\{\{\s*shared:([A-Za-z0-9_.-]+)\s*\}\}")
PROMPT_REQUIRED_TOKENS_BY_STAGE = {
    "hypothesis": {"iteration_id", "iteration_path", "hypothesis_id"},
    "design": {"iteration_id", "iteration_path", "hypothesis_id"},
    "implementation": {"iteration_id", "iteration_path"},
    "implementation_review": {"iteration_id", "iteration_path"},
    "launch": {"iteration_id", "iteration_path"},
    "extract_results": {"iteration_id", "iteration_path", "run_id"},
    "update_docs": {"iteration_id", "iteration_path", "run_id"},
    "decide_repeat": {"iteration_id", "iteration_path"},
}
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
