"""CLI parser construction and command dispatch entrypoint."""

from __future__ import annotations

from autolab.cli.support import *
from autolab.cli.handlers_observe import *
from autolab.cli.handlers_backlog import *
from autolab.cli.handlers_project import *
from autolab.cli.handlers_run import *
from autolab.cli.handlers_admin import *
from autolab.cli.handlers_parser import *

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
    reset.set_defaults(handler=_cmd_reset)

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
    run.set_defaults(run_agent_mode="policy")
    run.set_defaults(strict_implementation_progress=True)
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

    report = subparsers.add_parser(
        "report",
        help="Generate a developer issue report from local autolab logs",
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
        "show", help="Show contents of a policy preset"
    )
    policy_show.add_argument("preset", help="Preset name to show")
    policy_show.set_defaults(handler=_cmd_policy_show)

    policy_doctor = policy_subparsers.add_parser(
        "doctor", help="Diagnose common policy misconfigurations"
    )
    policy_doctor.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
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
        "generate", help="Generate stage flow, artifact map, and token reference"
    )
    docs_generate.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
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
