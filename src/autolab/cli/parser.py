"""CLI parser construction and command dispatch entrypoint."""

from __future__ import annotations

from autolab.cli.support import *
from autolab.cli.handlers_observe import *
from autolab.cli.handlers_backlog import *
from autolab.cli.handlers_campaign import *
from autolab.cli.handlers_project import *
from autolab.cli.handlers_run import *
from autolab.cli.handlers_admin import *
from autolab.cli.handlers_parser import *
from autolab.cli.handlers_checkpoint import *
from autolab.cli.handlers_gc import *

# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


def _top_level_help_epilog() -> str:
    lines = [
        "Recommended onboarding flow:",
        "  autolab init -> autolab configure --check -> autolab status -> autolab run --verify",
        "",
        "Use 'autolab COMMAND --help' for detailed command options.",
    ]
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="autolab command line interface",
        epilog=_top_level_help_epilog(),
        formatter_class=_AutolabHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    init = subparsers.add_parser(
        "init", help="Initialize autolab scaffold and state files"
    )
    init.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    init.add_argument(
        "--from-existing",
        action="store_true",
        help="Bootstrap context/backlog/policy from an existing repository layout.",
    )
    init_interactive = init.add_mutually_exclusive_group()
    init_interactive.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for policy bootstrap values during init.",
    )
    init_interactive.add_argument(
        "--no-interactive",
        action="store_true",
        help="Disable interactive prompts during init.",
    )
    init.set_defaults(handler=_cmd_init)

    configure_parser = subparsers.add_parser(
        "configure", help="Validate and configure autolab settings"
    )
    configure_parser.add_argument(
        "--check", action="store_true", help="Check configuration without modifying"
    )
    configure_parser.add_argument(
        "--state-file", default=".autolab/state.json", help="Path to state file"
    )
    configure_parser.set_defaults(handler=_cmd_configure)

    reset = subparsers.add_parser(
        "reset", help="Reset autolab scaffold and state to defaults"
    )
    reset.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    reset.add_argument(
        "--to",
        default="",
        help="Targeted reset: 'checkpoint:<id>' or 'stage:<stage>'",
    )
    reset.add_argument(
        "--archive-only",
        action="store_true",
        default=False,
        help="Preview what would be archived/restored without performing the reset",
    )
    reset.set_defaults(handler=_cmd_reset)

    checkpoint = subparsers.add_parser("checkpoint", help="Manage workflow checkpoints")
    checkpoint_sub = checkpoint.add_subparsers(dest="checkpoint_command")

    cp_create = checkpoint_sub.add_parser("create", help="Create a manual checkpoint")
    cp_create.add_argument("--state-file", default=".autolab/state.json")
    cp_create.add_argument("--label", default="")
    cp_create.add_argument(
        "--pin",
        action="store_true",
        default=False,
        help="Protect the new checkpoint from autolab gc pruning",
    )
    cp_create.add_argument(
        "--scope",
        choices=("project_wide", "experiment"),
        default="",
    )
    cp_create.add_argument("--iteration-id", dest="iteration_id", default="")
    cp_create.set_defaults(handler=_cmd_checkpoint_create)

    cp_list = checkpoint_sub.add_parser("list", help="List available checkpoints")
    cp_list.add_argument("--state-file", default=".autolab/state.json")
    cp_list.add_argument("--iteration-id", dest="iteration_id", default="")
    cp_list.add_argument(
        "--trigger",
        choices=("auto", "manual", "handoff", "commit", ""),
        default="",
        help="Filter checkpoints by trigger type",
    )
    cp_list.add_argument("--json", action="store_true", default=False)
    cp_list.set_defaults(handler=_cmd_checkpoint_list)

    cp_pin = checkpoint_sub.add_parser("pin", help="Protect a checkpoint from pruning")
    cp_pin.add_argument("checkpoint_id", help="Checkpoint id to pin")
    cp_pin.add_argument("--state-file", default=".autolab/state.json")
    cp_pin.set_defaults(handler=_cmd_checkpoint_pin)

    cp_unpin = checkpoint_sub.add_parser(
        "unpin", help="Allow a checkpoint to be pruned again"
    )
    cp_unpin.add_argument("checkpoint_id", help="Checkpoint id to unpin")
    cp_unpin.add_argument("--state-file", default=".autolab/state.json")
    cp_unpin.set_defaults(handler=_cmd_checkpoint_unpin)

    gc = subparsers.add_parser(
        "gc",
        help="Preview or prune recoverable autolab artifacts",
    )
    gc.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    gc.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Delete the reported artifacts instead of previewing them",
    )
    gc.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit machine-readable output",
    )
    gc.add_argument(
        "--only",
        action="append",
        choices=GC_ONLY_CHOICES,
        default=[],
        help="Limit GC to a specific artifact class; repeat to include multiple classes",
    )
    gc.add_argument(
        "--checkpoint-keep-latest",
        type=int,
        default=DEFAULT_CHECKPOINT_KEEP_LATEST,
        help="Keep this many unprotected checkpoints per iteration/stage",
    )
    gc.add_argument(
        "--execution-keep-latest",
        type=int,
        default=DEFAULT_EXECUTION_KEEP_LATEST,
        help="Keep this many non-active execution bundles",
    )
    gc.add_argument(
        "--traceability-keep-latest",
        type=int,
        default=DEFAULT_TRACEABILITY_KEEP_LATEST,
        help="Keep this many non-active traceability outputs",
    )
    gc.add_argument(
        "--reset-archive-max-age-days",
        type=int,
        default=DEFAULT_RESET_ARCHIVE_MAX_AGE_DAYS,
        help="Expire reset archives older than this many days",
    )
    gc.add_argument(
        "--views-keep-latest",
        type=int,
        default=DEFAULT_DOCS_VIEWS_KEEP_LATEST,
        help="Keep this many managed docs-view output directories",
    )
    gc.set_defaults(handler=_cmd_gc)

    hooks = subparsers.add_parser(
        "hooks", help="Manage the Autolab post-commit hook helper"
    )
    hooks_sub = hooks.add_subparsers(dest="hooks_command")
    hooks_install = hooks_sub.add_parser(
        "install",
        help="Install the Autolab post-commit hook helper in the current repo",
    )
    hooks_install.add_argument("--state-file", default=".autolab/state.json")
    hooks_install.add_argument("--force", action="store_true", default=False)
    hooks_install.set_defaults(handler=_cmd_hooks_install)

    verify = subparsers.add_parser(
        "verify", help="Run verification checks for a stage and write a summary report"
    )
    verify.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    verify.add_argument(
        "--stage",
        default=None,
        help="Verify a specific stage instead of state.stage.",
    )
    verify.set_defaults(handler=_cmd_verify)

    verify_golden = subparsers.add_parser(
        "verify-golden",
        help="Run verifiers against bundled golden iteration fixtures",
    )
    verify_golden.set_defaults(handler=_cmd_verify_golden)

    render = subparsers.add_parser(
        "render",
        help="Print the resolved stage prompt without executing workflow transitions",
    )
    render.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    render.add_argument(
        "--stage",
        default=None,
        help="Render a specific stage instead of state.stage.",
    )
    render.add_argument(
        "--view",
        choices=("runner", "audit", "brief", "human", "context"),
        default=None,
        help="Select which rendered packet to print (default: runner; with --stats defaults to all views).",
    )
    render.add_argument(
        "--stats",
        action="store_true",
        default=False,
        help="Print prompt-debugging stats instead of rendered packet text.",
    )
    render.set_defaults(handler=_cmd_render)

    discuss = subparsers.add_parser(
        "discuss",
        help="Capture scope-specific decisions, constraints, and unresolved questions",
    )
    discuss.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    discuss.add_argument(
        "--scope",
        required=True,
        choices=("project_wide", "experiment"),
        help="Which scope to capture decisions for.",
    )
    discuss.add_argument(
        "--iteration-id",
        "--iteration",
        dest="iteration_id",
        default="",
        help="Override experiment iteration_id (default: state.iteration_id).",
    )
    discuss.add_argument(
        "--answers-file",
        default="",
        help="Optional JSON answers file for non-interactive discuss execution.",
    )
    discuss.add_argument(
        "--non-interactive",
        action="store_true",
        default=False,
        help="Do not prompt; use --answers-file or current sidecar contents/defaults.",
    )
    discuss.add_argument(
        "--write-question-pack",
        default="",
        help="Optional path to export the discuss question pack as JSON.",
    )
    discuss.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable result metadata.",
    )
    discuss.set_defaults(handler=_cmd_discuss)

    research = subparsers.add_parser(
        "research",
        help="Synthesize repo-local evidence into research findings and recommendations",
    )
    research.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    research.add_argument(
        "--scope",
        required=True,
        choices=("project_wide", "experiment"),
        help="Which scope to research for.",
    )
    research.add_argument(
        "--iteration-id",
        "--iteration",
        dest="iteration_id",
        default="",
        help="Override experiment iteration_id (default: state.iteration_id).",
    )
    research.add_argument(
        "--question",
        action="append",
        default=[],
        help="Optional extra research question (repeatable).",
    )
    research.add_argument(
        "--timeout-seconds",
        type=float,
        default=240.0,
        help="LLM command timeout in seconds (default: 240).",
    )
    research.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable result metadata.",
    )
    research.set_defaults(handler=_cmd_research)

    run = subparsers.add_parser("run", help="Run one workflow stage transition")
    run.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    run.add_argument(
        "--decision",
        choices=DECISION_STAGES,
        default=None,
        help="Decision target to use when current stage is decide_repeat.",
    )
    run.add_argument(
        "--assistant",
        action="store_true",
        help="Use engineer-assistant task cycle mode for this run.",
    )
    run.add_argument(
        "--auto-decision",
        action="store_true",
        help="Let decide_repeat auto-select from todo/backlog when --decision is omitted.",
    )
    run.add_argument(
        "--verify",
        action="store_true",
        help="Run policy-driven verification before stage evaluation.",
    )
    run.add_argument(
        "--strict-implementation-progress",
        dest="strict_implementation_progress",
        action="store_true",
        help="Require meaningful implementation progress checks (default).",
    )
    run.add_argument(
        "--no-strict-implementation-progress",
        dest="strict_implementation_progress",
        action="store_false",
        help="Disable meaningful implementation progress checks.",
    )
    run_runner_group = run.add_mutually_exclusive_group()
    run_runner_group.add_argument(
        "--run-agent",
        dest="run_agent_mode",
        action="store_const",
        const="force_on",
        help="Force agent_runner for eligible stages.",
    )
    run_runner_group.add_argument(
        "--no-run-agent",
        dest="run_agent_mode",
        action="store_const",
        const="force_off",
        help="Disable agent_runner even when policy enables it.",
    )
    run_checkpoint_group = run.add_mutually_exclusive_group()
    run_checkpoint_group.add_argument(
        "--plan-only",
        action="store_true",
        help="Generate and validate the implementation plan, then stop before any wave executes.",
    )
    run_checkpoint_group.add_argument(
        "--execute-approved-plan",
        action="store_true",
        help="Execute the current approved implementation plan without replanning.",
    )
    run.set_defaults(run_agent_mode="policy")
    run.set_defaults(strict_implementation_progress=True)
    run.set_defaults(plan_only=False)
    run.set_defaults(execute_approved_plan=False)
    run.set_defaults(handler=_cmd_run)

    loop = subparsers.add_parser(
        "loop", help="Run bounded workflow transitions in sequence"
    )
    loop.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    loop.add_argument(
        "--max-iterations",
        type=int,
        default=1,
        help="Maximum transitions to execute (must be > 0).",
    )
    loop.add_argument(
        "--auto",
        action="store_true",
        help="Enable unattended mode with automatic decisions and lock enforcement.",
    )
    loop.add_argument(
        "--assistant",
        action="store_true",
        help="Use engineer-assistant task cycle mode for unattended delivery.",
    )
    loop.add_argument(
        "--verify",
        action="store_true",
        help="Run policy-driven verification before each stage evaluation.",
    )
    loop.add_argument(
        "--strict-implementation-progress",
        dest="strict_implementation_progress",
        action="store_true",
        help="Require meaningful implementation progress checks (default).",
    )
    loop.add_argument(
        "--no-strict-implementation-progress",
        dest="strict_implementation_progress",
        action="store_false",
        help="Disable meaningful implementation progress checks.",
    )
    loop.add_argument(
        "--max-hours",
        type=float,
        default=DEFAULT_MAX_HOURS,
        help="Maximum runtime in hours for --auto mode (must be > 0).",
    )
    loop_runner_group = loop.add_mutually_exclusive_group()
    loop_runner_group.add_argument(
        "--run-agent",
        dest="run_agent_mode",
        action="store_const",
        const="force_on",
        help="Force agent_runner for eligible stages.",
    )
    loop_runner_group.add_argument(
        "--no-run-agent",
        dest="run_agent_mode",
        action="store_const",
        const="force_off",
        help="Disable agent_runner even when policy enables it.",
    )
    loop.set_defaults(run_agent_mode="policy")
    loop.set_defaults(strict_implementation_progress=True)
    loop.set_defaults(handler=_cmd_loop)

    campaign = subparsers.add_parser(
        "campaign",
        help="Manage first-class unattended research campaigns",
    )
    campaign_sub = campaign.add_subparsers(dest="campaign_command")

    campaign_start = campaign_sub.add_parser(
        "start", help="Start a dedicated unattended campaign and enter campaign mode"
    )
    campaign_start.add_argument("--state-file", default=".autolab/state.json")
    campaign_start.add_argument("--label", required=True)
    campaign_start.add_argument(
        "--scope",
        choices=("experiment", "project_wide"),
        required=True,
    )
    campaign_start.add_argument(
        "--lock",
        action="append",
        choices=("design", "harness"),
        default=[],
        help="Lock campaign search to the current design or full harness contract.",
    )
    campaign_start.set_defaults(handler=_cmd_campaign_start)

    campaign_status = campaign_sub.add_parser(
        "status", help="Show campaign state and resumability"
    )
    campaign_status.add_argument("--state-file", default=".autolab/state.json")
    campaign_status.set_defaults(handler=_cmd_campaign_status)

    campaign_stop = campaign_sub.add_parser(
        "stop", help="Gracefully request campaign shutdown"
    )
    campaign_stop.add_argument("--state-file", default=".autolab/state.json")
    campaign_stop.set_defaults(handler=_cmd_campaign_stop)

    campaign_continue = campaign_sub.add_parser(
        "continue", help="Resume a stopped or errored campaign"
    )
    campaign_continue.add_argument("--state-file", default=".autolab/state.json")
    campaign_continue.set_defaults(handler=_cmd_campaign_continue)

    tui = subparsers.add_parser(
        "tui", help="Launch the interactive Textual workflow cockpit"
    )
    tui.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    tui.add_argument(
        "--tail-lines",
        type=int,
        default=2000,
        help="Maximum console lines kept in memory (default: 2000).",
    )
    tui.set_defaults(handler=_cmd_tui)

    status = subparsers.add_parser("status", help="Show current .autolab state")
    status.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    status.set_defaults(handler=_cmd_status)

    trace = subparsers.add_parser(
        "trace",
        help="Build traceability coverage artifact for the active iteration",
    )
    trace.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    trace.add_argument(
        "--iteration-id",
        default="",
        help="Optional iteration_id override (default: state.iteration_id).",
    )
    trace.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON summary.",
    )
    trace.set_defaults(handler=_cmd_trace)

    progress = subparsers.add_parser(
        "progress",
        help="Refresh and summarize handoff/progress state",
    )
    progress.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    progress.set_defaults(handler=_cmd_progress)

    focus = subparsers.add_parser(
        "focus",
        help="Manually retarget workflow focus to a backlog experiment/iteration",
    )
    focus.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    focus.add_argument(
        "--iteration-id",
        default="",
        help="Target iteration_id to focus (optional when --experiment-id is provided).",
    )
    focus.add_argument(
        "--experiment-id",
        default="",
        help="Target experiment_id to focus (optional when --iteration-id is provided).",
    )
    focus.set_defaults(handler=_cmd_focus)

    todo = subparsers.add_parser(
        "todo",
        help="Manage docs/todo.md and .autolab/todo_state.json for engineer steering",
    )
    todo_subparsers = todo.add_subparsers(dest="todo_command")

    todo_sync = todo_subparsers.add_parser(
        "sync",
        help="Reconcile docs/todo.md with generated and manual tasks",
    )
    todo_sync.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    todo_sync.set_defaults(handler=_cmd_todo, todo_action="sync")

    todo_list = todo_subparsers.add_parser("list", help="List open todo tasks")
    todo_list.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    todo_list.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON",
    )
    todo_list.set_defaults(handler=_cmd_todo, todo_action="list")

    todo_add = todo_subparsers.add_parser("add", help="Add a manual todo task")
    todo_add.add_argument("text", help="Task text")
    todo_add.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    todo_add.add_argument(
        "--stage",
        default="",
        help="Optional stage tag for the task (defaults to state.stage).",
    )
    todo_add.add_argument(
        "--priority",
        choices=("critical", "high", "medium", "low"),
        default="",
        help="Optional priority tag.",
    )
    todo_add.add_argument("--owner", default="", help="Optional owner tag.")
    todo_add.add_argument(
        "--label",
        action="append",
        default=[],
        help="Optional label tag (repeatable).",
    )
    todo_add.set_defaults(handler=_cmd_todo, todo_action="add")

    todo_done = todo_subparsers.add_parser(
        "done", help="Mark an open todo task as completed"
    )
    todo_done.add_argument("selector", help="Task selector (task_id or 1-based index).")
    todo_done.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    todo_done.set_defaults(handler=_cmd_todo, todo_action="done")

    todo_remove = todo_subparsers.add_parser(
        "remove", help="Remove an open todo task without marking completion"
    )
    todo_remove.add_argument(
        "selector",
        help="Task selector (task_id or 1-based index).",
    )
    todo_remove.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    todo_remove.set_defaults(handler=_cmd_todo, todo_action="remove")

    guardrails_parser = subparsers.add_parser(
        "guardrails", help="Show guardrail counters and thresholds"
    )
    guardrails_parser.add_argument(
        "--state-file", default=".autolab/state.json", help="Path to state file"
    )
    guardrails_parser.set_defaults(handler=_cmd_guardrails)

    sync_scaffold = subparsers.add_parser(
        "sync-scaffold",
        help="Sync bundled autolab scaffold files into the repository",
    )
    sync_scaffold.add_argument(
        "--dest",
        default=".autolab",
        help="Target directory for scaffold files (default: .autolab)",
    )
    sync_scaffold.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing scaffold files.",
    )
    sync_scaffold.set_defaults(handler=_cmd_sync_scaffold)

    update = subparsers.add_parser(
        "update",
        help="Upgrade autolab to the latest stable release",
    )
    update.set_defaults(handler=_cmd_update)

    install_skill = subparsers.add_parser(
        "install-skill",
        help="Install bundled skill templates into provider-specific project skill directories.",
    )
    install_skill.add_argument(
        "provider",
        choices=SUPPORTED_SKILL_PROVIDERS,
        help="Skill provider to install (supported: codex, claude).",
    )
    install_skill.add_argument(
        "--skill",
        default=None,
        help="Install only this skill (default: all bundled skills).",
    )
    install_skill.add_argument(
        "--project-root",
        default=".",
        help="Project root where provider skill directories will be created (default: current directory).",
    )
    install_skill.set_defaults(handler=_cmd_install_skill)

    slurm_job_list = subparsers.add_parser(
        "slurm-job-list",
        help="Maintain or verify docs/slurm_job_list.md ledger entries for run manifests.",
    )
    slurm_job_list.add_argument(
        "action",
        choices=("append", "verify"),
        help="Action to perform against a run manifest.",
    )
    slurm_job_list.add_argument(
        "--manifest",
        required=True,
        help="Path to experiments/<type>/<iteration_id>/runs/<run_id>/run_manifest.json",
    )
    slurm_job_list.add_argument(
        "--doc",
        required=True,
        help="Path to docs/slurm_job_list.md.",
    )
    slurm_job_list.set_defaults(handler=_cmd_slurm_job_list)

    remote = subparsers.add_parser(
        "remote",
        help="Inspect and validate remote execution profiles.",
    )
    remote_subparsers = remote.add_subparsers(dest="remote_command")

    remote_show = remote_subparsers.add_parser(
        "show",
        help="Show the resolved remote execution profile.",
    )
    remote_show.add_argument(
        "--profile",
        default="",
        help="Profile name to inspect (default: use default_profile).",
    )
    remote_show.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json).",
    )
    remote_show.set_defaults(handler=_cmd_remote_show)

    remote_doctor = remote_subparsers.add_parser(
        "doctor",
        help="Diagnose remote profile and revision readiness.",
    )
    remote_doctor.add_argument(
        "--profile",
        default="",
        help="Profile name to inspect (default: use default_profile).",
    )
    remote_doctor.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json).",
    )
    remote_doctor.set_defaults(handler=_cmd_remote_doctor)

    remote_smoke = remote_subparsers.add_parser(
        "smoke",
        help="Verify remote reachability, repo, Python, and optional smoke command.",
    )
    remote_smoke.add_argument(
        "--profile",
        default="",
        help="Profile name to inspect (default: use default_profile).",
    )
    remote_smoke.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json).",
    )
    remote_smoke.set_defaults(handler=_cmd_remote_smoke)

    report = subparsers.add_parser(
        "report",
        help="Generate a developer issue report or campaign wake-up report",
    )
    report.add_argument(
        "--campaign",
        action="store_true",
        default=False,
        help="Generate a campaign wake-up report for the active campaign instead of an issue report.",
    )
    report.add_argument(
        "--comment",
        "-m",
        default="",
        help="Optional user comment describing the issue or improvement idea.",
    )
    report.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json).",
    )
    report.add_argument(
        "--log-tail",
        type=int,
        default=500,
        help="Number of trailing orchestrator.log lines to analyze (default: 500).",
    )
    report.add_argument(
        "--timeout-seconds",
        type=float,
        default=240.0,
        help="LLM command timeout in seconds (default: 240).",
    )
    report.add_argument(
        "--output",
        default="",
        help="Optional output path for the issue document (default: .autolab/logs/issue_report_<timestamp>.md).",
    )
    report.set_defaults(handler=_cmd_report)

    oracle = subparsers.add_parser(
        "oracle",
        help="Generate or apply oracle expert-review feedback",
    )
    oracle.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json).",
    )
    oracle.add_argument(
        "--timeout-seconds",
        type=float,
        default=240.0,
        help="LLM command timeout in seconds (default: 240).",
    )
    oracle.add_argument(
        "--output",
        default="",
        help="Optional output path for the oracle document (default: <scope-root>/oracle.md).",
    )
    oracle.set_defaults(handler=_cmd_oracle)
    oracle_subparsers = oracle.add_subparsers(dest="oracle_command")

    oracle_export = oracle_subparsers.add_parser(
        "export",
        help="Generate an expert-review oracle document from the continuation packet",
    )
    oracle_export.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json).",
    )
    oracle_export.add_argument(
        "--timeout-seconds",
        type=float,
        default=240.0,
        help="LLM command timeout in seconds (default: 240).",
    )
    oracle_export.add_argument(
        "--output",
        default="",
        help="Optional output path for the oracle document (default: <scope-root>/oracle.md).",
    )
    oracle_export.set_defaults(handler=_cmd_oracle)

    oracle_apply = oracle_subparsers.add_parser(
        "apply",
        help="Apply expert notes into sidecars, todos, and campaign steering state",
    )
    oracle_apply.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json).",
    )
    oracle_apply_input = oracle_apply.add_mutually_exclusive_group(required=False)
    oracle_apply.add_argument(
        "reply_path",
        nargs="?",
        default="",
        help="Path to an Oracle reply markdown file to parse and apply.",
    )
    oracle_apply_input.add_argument(
        "--notes",
        default="",
        help="Path to a notes file or oracle export to ingest (legacy alias).",
    )
    oracle_apply_input.add_argument(
        "--stdin",
        action="store_true",
        help="Read notes to ingest from stdin.",
    )
    oracle_apply.set_defaults(handler=_cmd_oracle_apply)

    oracle_roundtrip = oracle_subparsers.add_parser(
        "roundtrip",
        help="Run a browser-only Oracle roundtrip and apply advisory feedback",
    )
    oracle_roundtrip.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json).",
    )
    oracle_roundtrip.add_argument(
        "--output",
        default="",
        help="Optional output path for the oracle document (default: <scope-root>/oracle.md).",
    )
    oracle_roundtrip.add_argument(
        "--auto",
        action="store_true",
        default=False,
        help="Run the unattended browser-only Oracle automation path.",
    )
    oracle_roundtrip.set_defaults(handler=_cmd_oracle_roundtrip)

    handoff = subparsers.add_parser(
        "handoff",
        help="Write machine/human handoff artifacts for takeover",
    )
    handoff.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json).",
    )
    handoff.set_defaults(handler=_cmd_handoff)

    resume = subparsers.add_parser(
        "resume",
        help="Preview or apply the recommended safe resume command",
    )
    resume.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json).",
    )
    resume.add_argument(
        "--apply",
        action="store_true",
        help="Execute the recommended command when safe to resume.",
    )
    resume.set_defaults(handler=_cmd_resume)

    # Phase 6b: review subcommand
    review = subparsers.add_parser("review", help="Record a human review decision")
    review.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    review.add_argument(
        "--status",
        required=True,
        choices=("pass", "retry", "stop"),
        help="Human review decision: pass (continue to launch), retry (back to implementation), stop (end experiment)",
    )
    review.set_defaults(handler=_cmd_review)

    approve_plan = subparsers.add_parser(
        "approve-plan",
        help="Record an approval decision for the current implementation plan checkpoint",
    )
    approve_plan.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    approve_plan.add_argument(
        "--status",
        required=True,
        choices=("approve", "retry", "stop"),
        help="Approval decision: approve (allow execution), retry (force replanning), stop (end experiment)",
    )
    approve_plan.add_argument(
        "--notes",
        default="",
        help="Optional review notes to persist alongside the approval decision.",
    )
    approve_plan.add_argument(
        "--require-uat",
        action="store_true",
        help="Mark UAT as required via plan approval for this iteration.",
    )
    approve_plan.set_defaults(handler=_cmd_approve_plan)

    uat = subparsers.add_parser("uat", help="UAT artifact helpers")
    uat_subparsers = uat.add_subparsers(dest="uat_command")

    uat_init = uat_subparsers.add_parser(
        "init",
        help="Create experiments/<iteration_id>/uat.md when UAT is required or requested",
    )
    uat_init.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    uat_init.add_argument(
        "--suggest",
        action="store_true",
        help="Infer suggested UAT checks from touched project-wide surfaces when scaffolding a new artifact.",
    )
    uat_init.set_defaults(handler=_cmd_uat_init)

    experiment = subparsers.add_parser(
        "experiment", help="Experiment lifecycle management commands"
    )
    experiment_subparsers = experiment.add_subparsers(dest="experiment_command")

    experiment_create = experiment_subparsers.add_parser(
        "create",
        help="Create a new plan experiment and iteration skeleton",
    )
    experiment_create.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    experiment_create.add_argument(
        "--experiment-id",
        required=True,
        help="New experiment_id for backlog and state steering",
    )
    experiment_create.add_argument(
        "--iteration-id",
        required=True,
        help="New iteration_id for experiments/plan/<iteration_id>",
    )
    experiment_create.add_argument(
        "--hypothesis-id",
        default="",
        help="Optional backlog hypothesis_id (defaults to first non-completed hypothesis).",
    )
    experiment_create.set_defaults(handler=_cmd_experiment_create)

    experiment_move = experiment_subparsers.add_parser(
        "move",
        help="Move an experiment between plan/in_progress/done lifecycle types",
    )
    experiment_move.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    experiment_move.add_argument(
        "--iteration-id",
        default="",
        help="Target iteration_id (optional when --experiment-id is provided).",
    )
    experiment_move.add_argument(
        "--experiment-id",
        default="",
        help="Target experiment_id (optional when --iteration-id is provided).",
    )
    experiment_move.add_argument(
        "--to",
        required=True,
        help="Target lifecycle type: planned|plan|in_progress|done",
    )
    experiment_move.set_defaults(handler=_cmd_experiment_move)

    # Lock management
    lock = subparsers.add_parser("lock", help="Inspect or break the autolab run lock")
    lock.add_argument(
        "action",
        choices=("status", "break"),
        help="Action: status (show lock info) or break (force remove lock)",
    )
    lock.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    lock.add_argument(
        "--reason",
        default="manual break",
        help="Reason for breaking the lock (used in audit log)",
    )
    lock.set_defaults(handler=_cmd_lock)

    # Unlock alias (delegates to lock break)
    unlock = subparsers.add_parser(
        "unlock", help="Force-break the autolab run lock (alias for 'lock break')"
    )
    unlock.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    unlock.add_argument(
        "--reason",
        default="manual break",
        help="Reason for breaking the lock (used in audit log)",
    )
    unlock.set_defaults(handler=_cmd_lock, action="break")

    # Skip stage
    skip = subparsers.add_parser(
        "skip", help="Skip the current stage forward with audit trail"
    )
    skip.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    skip.add_argument(
        "--stage",
        required=True,
        help="Target stage to skip to (must be a forward stage in the pipeline)",
    )
    skip.add_argument(
        "--reason",
        required=True,
        help="Reason for skipping (recorded in state history)",
    )
    skip.set_defaults(handler=_cmd_skip)

    # Lint (user-friendly verify alias)
    lint = subparsers.add_parser(
        "lint", help="Run stage verifiers with user-friendly output"
    )
    lint.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    lint.add_argument(
        "--stage",
        default=None,
        help="Override stage for linting (default: state.stage)",
    )
    lint.set_defaults(handler=_cmd_lint)

    # Explain stage
    explain = subparsers.add_parser(
        "explain", help="Show effective configuration for a stage"
    )
    explain_subparsers = explain.add_subparsers(dest="explain_command")
    explain_stage = explain_subparsers.add_parser(
        "stage", help="Show effective stage config"
    )
    explain_stage.add_argument("stage", help="Stage name to explain")
    explain_stage.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    explain_stage.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON",
    )
    explain_stage.set_defaults(handler=_cmd_explain)

    # Policy management
    policy = subparsers.add_parser(
        "policy",
        help="Manage verifier policy profiles",
    )
    policy_subparsers = policy.add_subparsers(dest="policy_command")

    policy_list = policy_subparsers.add_parser(
        "list", help="List available policy presets"
    )
    policy_list.set_defaults(handler=_cmd_policy_list)

    policy_show = policy_subparsers.add_parser(
        "show", help="Show contents of a policy preset or effective merged policy"
    )
    policy_show.add_argument(
        "preset", nargs="?", default=None, help="Preset name to show"
    )
    policy_show.add_argument(
        "--effective",
        action="store_true",
        default=False,
        help="Show the computed effective policy (merged from all layers)",
    )
    policy_show.add_argument(
        "--stage", default="", help="Stage context for effective policy"
    )
    policy_show.add_argument(
        "--scope", default="", help="Scope context (experiment|project_wide)"
    )
    policy_show.add_argument("--host", default="", help="Host mode (local|slurm)")
    policy_show.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON artifact",
    )
    policy_show.set_defaults(handler=_cmd_policy_show)

    policy_doctor = policy_subparsers.add_parser(
        "doctor", help="Diagnose common policy misconfigurations"
    )
    policy_doctor.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    policy_doctor.add_argument(
        "--explain",
        action="store_true",
        default=False,
        help="Show effective policy resolution chain and risk flag derivation",
    )
    policy_doctor.set_defaults(handler=_cmd_policy_doctor)

    policy_apply = policy_subparsers.add_parser("apply", help="Apply policy changes")
    policy_apply_subparsers = policy_apply.add_subparsers(dest="policy_apply_command")
    policy_preset = policy_apply_subparsers.add_parser(
        "preset",
        help="Apply a bundled policy preset",
    )
    policy_preset.add_argument(
        "preset",
        choices=POLICY_PRESET_NAMES,
        help="Preset name to apply.",
    )
    policy_preset.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    policy_preset.set_defaults(handler=_cmd_policy_apply_preset)

    # Parser SDK
    parser_command = subparsers.add_parser(
        "parser",
        help="Parser authoring and validation SDK commands",
    )
    parser_subparsers = parser_command.add_subparsers(dest="parser_command")

    parser_init = parser_subparsers.add_parser(
        "init",
        help="Initialize parser module, capability manifest, and design extract_parser hook",
    )
    parser_init.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    parser_init.add_argument(
        "--iteration-id",
        default="",
        help="Override target iteration_id (default: state.iteration_id).",
    )
    parser_init.add_argument(
        "--module",
        default="",
        help="Optional parser module stem under parsers/ (default: <iteration_id>_extract_parser).",
    )
    parser_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite generated parser module when it already exists.",
    )
    parser_init.set_defaults(handler=_cmd_parser_init)

    parser_test = parser_subparsers.add_parser(
        "test",
        help="Run deterministic parser extraction tests with capability validation",
    )
    parser_test.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    parser_test.add_argument(
        "--iteration-id",
        default="",
        help="Override target iteration_id (default: state.iteration_id).",
    )
    parser_test.add_argument(
        "--run-id",
        default="",
        help="Override run_id used for parser execution (default: state.last_run_id or parser_test_run).",
    )
    parser_test.add_argument(
        "--fixture-pack",
        default="",
        help="Run against scaffold fixture pack name under .autolab/parser_fixtures/.",
    )
    parser_test.add_argument(
        "--in-place",
        action="store_true",
        help="Execute against the repository directly (default: isolated temp workspace).",
    )
    parser_test.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable parser test results.",
    )
    parser_test.set_defaults(handler=_cmd_parser_test)

    # Docs generation
    docs = subparsers.add_parser("docs", help="Generate documentation from registry")
    docs_subparsers = docs.add_subparsers(dest="docs_command")
    docs_generate = docs_subparsers.add_parser(
        "generate",
        help=(
            "Generate canonical-artifact projection views "
            "(project, roadmap, state, requirements, sidecar)"
        ),
    )
    docs_generate.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    docs_generate.add_argument(
        "--view",
        choices=(
            "project",
            "roadmap",
            "state",
            "requirements",
            "sidecar",
            "all",
            "registry",
        ),
        default="registry",
        help=(
            "Select generated view to render "
            "(default: registry; use all for canonical generated docs projection views)."
        ),
    )
    docs_generate.add_argument(
        "--iteration-id",
        default="",
        help="Override target iteration_id for iteration-scoped views (default: state.iteration_id).",
    )
    docs_generate.add_argument(
        "--output-dir",
        default="",
        help="Optional output directory to write rendered view markdown files.",
    )
    docs_generate.set_defaults(handler=_cmd_docs_generate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))


__all__ = [name for name in globals() if not name.startswith("__")]
