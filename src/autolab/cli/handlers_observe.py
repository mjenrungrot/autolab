"""Observation/status-related CLI command handlers."""

from __future__ import annotations

from autolab.cli.support import *
from autolab.traceability import build_traceability_coverage


def main(argv: list[str] | None = None) -> int:
    """Late-bind to autolab.commands.main to preserve monkeypatch compatibility."""
    from autolab.commands import main as commands_main

    return int(commands_main(argv))


def _cmd_status(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    try:
        state = _load_state(state_path)
    except RuntimeError as exc:
        print(f"autolab status: ERROR {exc}", file=sys.stderr)
        return 1

    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)

    print("autolab status")
    print(f"state_file: {state_path}")
    for key in (
        "iteration_id",
        "experiment_id",
        "stage",
        "last_run_id",
        "sync_status",
        "assistant_mode",
        "current_task_id",
        "task_cycle_stage",
        "repeat_guard",
        "task_change_baseline",
        "max_stage_attempts",
        "max_total_iterations",
    ):
        value = state.get(key, "<missing>")
        print(f"{key}: {value}")
    attempt = state.get("stage_attempt", 0)
    max_attempts = state.get("max_stage_attempts", 5)
    print(f"stage_attempt: {attempt}/{max_attempts}")

    # Phase 7b: human_review banner
    if state.get("stage") == "human_review":
        print("\n*** HUMAN REVIEW REQUIRED ***")
        print(
            "Run `autolab review --status=pass|retry|stop` to record the human review decision."
        )

    # --- Lock status ---
    lock_path = autolab_dir / "lock"
    if lock_path.exists():
        lock_payload = _read_lock_payload(lock_path)
        if lock_payload:
            lock_pid = lock_payload.get("pid", "<unknown>")
            lock_started = lock_payload.get("started_at", "<unknown>")
            from datetime import datetime, timedelta, timezone

            heartbeat_raw = lock_payload.get("last_heartbeat_at", "")
            from autolab.utils import _parse_utc

            heartbeat_dt = _parse_utc(str(heartbeat_raw))
            now = datetime.now(timezone.utc)
            if (
                heartbeat_dt is not None
                and (now - heartbeat_dt).total_seconds() > LOCK_STALE_SECONDS
            ):
                print("lock: stale")
            else:
                print(f"lock: held by PID {lock_pid} since {lock_started}")
        else:
            print("lock: stale")
    else:
        print("lock: free")

    # --- Last verification result ---
    verification_result_path = autolab_dir / "verification_result.json"
    vr_payload = _load_json_if_exists(verification_result_path)
    if isinstance(vr_payload, dict):
        vr_passed = "passed" if vr_payload.get("passed") else "failed"
        vr_generated_at = vr_payload.get("generated_at", "<unknown>")
        print(f"last_verification: {vr_passed} at {vr_generated_at}")

    # --- Last 3 history entries ---
    history = state.get("history")
    if isinstance(history, list) and history:
        print("recent_history:")
        for entry in history[-3:]:
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp_utc", "")
            sb = entry.get("stage_before", "")
            sa = entry.get("stage_after", "")
            status = entry.get("status", "")
            summary = entry.get("summary", "")
            # One-liner summary
            print(f"  {ts} {sb}->{sa} [{status}] {summary}")

    # --- Open todo count ---
    todo_state_path = autolab_dir / "todo_state.json"
    if todo_state_path.exists():
        open_count = _todo_open_count(repo_root)
        print(f"open_tasks: {open_count}")

    # --- Experiment completion ---
    experiment_id = str(state.get("experiment_id", "")).strip()
    iteration_id = str(state.get("iteration_id", "")).strip()
    backlog_path = autolab_dir / "backlog.yaml"
    backlog_payload, _backlog_err = _load_backlog_yaml(backlog_path)
    if backlog_payload is not None:
        entry, _entry_err = _find_backlog_experiment_entry(
            backlog_payload,
            experiment_id=experiment_id,
            iteration_id=iteration_id,
        )
        if entry is not None:
            exp_status = str(entry.get("status", "<unknown>")).strip()
            print(f"experiment_status: {exp_status}")

    # --- Guardrail counters ---
    repeat_guard = state.get("repeat_guard")
    if isinstance(repeat_guard, dict):
        try:
            guardrail_cfg = _load_guardrail_config(repo_root)
            max_streak = int(guardrail_cfg.max_same_decision_streak)
            max_no_prog = int(guardrail_cfg.max_no_progress_decisions)
            max_docs = int(guardrail_cfg.max_update_docs_cycles)
            on_breach = str(guardrail_cfg.on_breach)
        except Exception:
            max_streak = 3
            max_no_prog = 2
            max_docs = 3
            on_breach = "human_review"

        streak = repeat_guard.get("same_decision_streak", 0)
        no_prog = repeat_guard.get("no_progress_decisions", 0)
        docs_cyc = repeat_guard.get("update_docs_cycles", 0)
        print("guardrails:")
        print(f"  same_decision_streak: {streak}/{max_streak} (breach -> {on_breach})")
        print(
            f"  no_progress_decisions: {no_prog}/{max_no_prog} (breach -> {on_breach})"
        )
        print(f"  update_docs_cycles: {docs_cyc}/{max_docs} (breach -> {on_breach})")

    return 0


def _cmd_trace(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    try:
        state = _normalize_state(_load_state(state_path))
    except RuntimeError as exc:
        print(f"autolab trace: ERROR {exc}", file=sys.stderr)
        return 1

    iteration_override = str(getattr(args, "iteration_id", "") or "").strip()
    if iteration_override:
        state = dict(state)
        state["iteration_id"] = iteration_override

    try:
        result = build_traceability_coverage(
            repo_root,
            state,
            write_outputs=True,
        )
    except Exception as exc:
        print(f"autolab trace: ERROR {exc}", file=sys.stderr)
        return 1

    coverage_payload = result.coverage_payload
    summary = coverage_payload.get("summary", {})
    if bool(getattr(args, "json", False)):
        try:
            coverage_path_value = result.coverage_path.relative_to(repo_root).as_posix()
        except ValueError:
            coverage_path_value = str(result.coverage_path)
        try:
            latest_path_value = result.latest_path.relative_to(repo_root).as_posix()
        except ValueError:
            latest_path_value = str(result.latest_path)
        output = {
            "status": "ok",
            "iteration_id": str(coverage_payload.get("iteration_id", "")).strip(),
            "experiment_id": str(coverage_payload.get("experiment_id", "")).strip(),
            "coverage_path": coverage_path_value,
            "latest_path": latest_path_value,
            "summary": summary if isinstance(summary, dict) else {},
        }
        print(json.dumps(output, indent=2))
        return 0

    rows_total = int(summary.get("rows_total", 0) or 0)
    rows_covered = int(summary.get("rows_covered", 0) or 0)
    rows_untested = int(summary.get("rows_untested", 0) or 0)
    rows_failed = int(summary.get("rows_failed", 0) or 0)
    requirements_total = int(summary.get("requirements_total", 0) or 0)
    requirements_covered = int(summary.get("requirements_covered", 0) or 0)
    requirements_untested = int(summary.get("requirements_untested", 0) or 0)
    requirements_failed = int(summary.get("requirements_failed", 0) or 0)

    print("autolab trace")
    print(f"state_file: {state_path}")
    print(f"iteration_id: {coverage_payload.get('iteration_id', '')}")
    print(f"experiment_id: {coverage_payload.get('experiment_id', '')}")
    print(f"coverage_artifact: {result.coverage_path}")
    print(f"latest_pointer: {result.latest_path}")
    print(
        "rows: "
        f"total={rows_total}, covered={rows_covered}, "
        f"untested={rows_untested}, failed={rows_failed}"
    )
    print(
        "requirements: "
        f"total={requirements_total}, covered={requirements_covered}, "
        f"untested={requirements_untested}, failed={requirements_failed}"
    )
    return 0


def _safe_refresh_handoff(state_path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        artifacts = refresh_handoff(state_path)
    except Exception as exc:
        return (None, str(exc))
    return (artifacts.payload, "")


def _cmd_progress(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    payload, error = _safe_refresh_handoff(state_path)
    if payload is None:
        print(
            f"autolab progress: ERROR failed to refresh handoff: {error}",
            file=sys.stderr,
        )
        return 1

    verifier = payload.get("latest_verifier_summary", {})
    if not isinstance(verifier, dict):
        verifier = {}
    recommended = payload.get("recommended_next_command", {})
    if not isinstance(recommended, dict):
        recommended = {}
    safe_resume = payload.get("safe_resume_point", {})
    if not isinstance(safe_resume, dict):
        safe_resume = {}
    blocking = payload.get("blocking_failures", [])
    if not isinstance(blocking, list):
        blocking = []
    pending = payload.get("pending_human_decisions", [])
    if not isinstance(pending, list):
        pending = []

    print("autolab progress")
    print(f"state_file: {state_path}")
    print(f"generated_at: {payload.get('generated_at', '')}")
    print(f"iteration_id: {payload.get('iteration_id', '')}")
    print(f"experiment_id: {payload.get('experiment_id', '')}")
    print(f"scope: {payload.get('current_scope', 'experiment')}")
    print(f"stage: {payload.get('current_stage', '')}")
    print(f"verifier_passed: {bool(verifier.get('passed', False))}")
    print(f"verifier_message: {verifier.get('message', '')}")
    print(f"blocking_failures: {len(blocking)}")
    print(f"pending_human_decisions: {len(pending)}")
    print(f"recommended_next_command: {recommended.get('command', '')}")
    print(f"safe_resume_status: {safe_resume.get('status', 'blocked')}")
    return 0


def _cmd_handoff(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    payload, error = _safe_refresh_handoff(state_path)
    if payload is None:
        print(
            f"autolab handoff: ERROR failed to refresh handoff: {error}",
            file=sys.stderr,
        )
        return 1
    handoff_json_path = Path(
        str(payload.get("handoff_json_path", ".autolab/handoff.json"))
    )
    handoff_md_path = Path(str(payload.get("handoff_markdown_path", "handoff.md")))
    recommended = payload.get("recommended_next_command", {})
    if not isinstance(recommended, dict):
        recommended = {}

    print("autolab handoff")
    print(f"state_file: {state_path}")
    print(f"handoff_json: {handoff_json_path}")
    print(f"handoff_md: {handoff_md_path}")
    print(f"recommended_next_command: {recommended.get('command', '')}")
    return 0


def _cmd_resume(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    payload, error = _safe_refresh_handoff(state_path)
    if payload is None:
        print(
            f"autolab resume: ERROR failed to refresh handoff: {error}", file=sys.stderr
        )
        return 1

    recommended = payload.get("recommended_next_command", {})
    if not isinstance(recommended, dict):
        recommended = {}
    safe_resume = payload.get("safe_resume_point", {})
    if not isinstance(safe_resume, dict):
        safe_resume = {}
    command_text = str(recommended.get("command", "")).strip()
    executable = bool(recommended.get("executable", False))
    reason = str(recommended.get("reason", "")).strip()
    status = str(safe_resume.get("status", "blocked")).strip() or "blocked"
    preconditions = safe_resume.get("preconditions", [])
    if not isinstance(preconditions, list):
        preconditions = []

    print("autolab resume")
    print(f"state_file: {state_path}")
    print(f"safe_resume_status: {status}")
    print(f"recommended_command: {command_text}")
    print(f"reason: {reason}")
    if preconditions:
        print("preconditions:")
        for precondition in preconditions:
            text = str(precondition).strip()
            if text:
                print(f"- {text}")

    if not bool(getattr(args, "apply", False)):
        print("mode: preview (use --apply to execute when safe)")
        return 0

    if status != "ready" or not executable:
        print(
            "autolab resume: ERROR safe resume point is blocked; resolve preconditions first",
            file=sys.stderr,
        )
        return 1
    if not command_text:
        print(
            "autolab resume: ERROR no recommended command is available", file=sys.stderr
        )
        return 1

    try:
        command_tokens = shlex.split(command_text)
    except ValueError as exc:
        print(f"autolab resume: ERROR invalid command: {exc}", file=sys.stderr)
        return 1
    if not command_tokens:
        print("autolab resume: ERROR recommended command is empty", file=sys.stderr)
        return 1
    if command_tokens[0] != "autolab":
        print(
            "autolab resume: ERROR only autolab commands are executable via --apply",
            file=sys.stderr,
        )
        return 1

    resume_argv = command_tokens[1:]
    if not resume_argv:
        print(
            "autolab resume: ERROR missing subcommand in recommended command",
            file=sys.stderr,
        )
        return 1
    stateful_subcommands = {
        "run",
        "loop",
        "trace",
        "render",
        "verify",
        "status",
        "focus",
        "todo",
        "guardrails",
        "review",
        "skip",
        "lint",
        "lock",
        "unlock",
        "report",
        "progress",
        "handoff",
        "resume",
    }
    if resume_argv[0] in stateful_subcommands and "--state-file" not in resume_argv:
        resume_argv.extend(["--state-file", str(state_path)])

    print(f"apply: executing {' '.join(shlex.quote(token) for token in resume_argv)}")
    return main(resume_argv)


__all__ = [name for name in globals() if not name.startswith("__")]
