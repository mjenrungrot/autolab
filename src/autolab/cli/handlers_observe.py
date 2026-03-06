"""Observation/status-related CLI command handlers."""

from __future__ import annotations

from autolab.cli.support import *
from autolab.plan_approval import (
    approval_next_commands_for_mode,
    load_plan_approval,
    resolve_plan_approval_state,
)
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

    if str(state.get("stage", "")).strip() == "implementation":
        iteration_id = str(state.get("iteration_id", "")).strip()
        experiment_id = str(state.get("experiment_id", "")).strip()
        if iteration_id:
            iteration_dir, _ = _resolve_iteration_directory(
                repo_root,
                iteration_id=iteration_id,
                experiment_id=experiment_id,
                require_exists=False,
            )
            approval = load_plan_approval(iteration_dir)
            approval_action_mode = "none"
            approval_error = ""
            resolved_approval, approval_error, approval_action_mode = (
                resolve_plan_approval_state(
                    repo_root,
                    iteration_dir,
                )
            )
            if resolved_approval:
                approval = resolved_approval
            if approval:
                counts = approval.get("counts")
                if not isinstance(counts, dict):
                    counts = {}
                trigger_reasons = [
                    str(item).strip()
                    for item in approval.get("trigger_reasons", [])
                    if str(item).strip()
                ]
                print("plan_approval:")
                print(f"  status: {approval.get('status', '')}")
                print(
                    f"  requires_approval: {bool(approval.get('requires_approval', False))}"
                )
                print(f"  plan_hash: {approval.get('plan_hash', '')}")
                print(f"  risk_fingerprint: {approval.get('risk_fingerprint', '')}")
                print(
                    "  counts: "
                    f"tasks={int(counts.get('tasks_total', 0) or 0)} "
                    f"waves={int(counts.get('waves_total', 0) or 0)} "
                    f"project_wide_tasks={int(counts.get('project_wide_tasks', 0) or 0)} "
                    f"project_wide_paths={int(counts.get('project_wide_unique_paths', 0) or 0)} "
                    f"retries={int(counts.get('observed_retries', 0) or 0)}"
                )
                if trigger_reasons:
                    print("  trigger_reasons:")
                    for reason in trigger_reasons:
                        print(f"    - {reason}")
                if approval_error:
                    print(f"  diagnostic: {approval_error}")
                next_commands = approval_next_commands_for_mode(
                    approval,
                    action_mode=approval_action_mode,
                )
                if next_commands:
                    print("  next_commands:")
                    for command in next_commands:
                        print(f"    - {command}")

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


def _observe_non_empty_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            output.append(text)
    return output


def _observe_format_list(value: Any, *, blank: str = "-") -> str:
    items = _observe_non_empty_strings(value)
    return ", ".join(items) if items else blank


def _observe_format_seconds(value: Any, *, blank: str = "n/a") -> str:
    if value in ("", None):
        return blank
    try:
        numeric = float(value)
    except Exception:
        return blank
    if abs(numeric - round(numeric)) < 1e-9:
        return f"{numeric:.1f}s"
    return f"{numeric:.3f}".rstrip("0").rstrip(".") + "s"


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
    wave_observability = payload.get("wave_observability", {})
    if not isinstance(wave_observability, dict):
        wave_observability = {}
    critical_path = wave_observability.get("critical_path", {})
    if not isinstance(critical_path, dict):
        critical_path = {}
    observability_waves = wave_observability.get("waves", [])
    if not isinstance(observability_waves, list):
        observability_waves = []
    observability_tasks = wave_observability.get("tasks", [])
    if not isinstance(observability_tasks, list):
        observability_tasks = []
    file_conflicts = wave_observability.get("file_conflicts", [])
    if not isinstance(file_conflicts, list):
        file_conflicts = []
    observability_diagnostics = wave_observability.get("diagnostics", [])
    if not isinstance(observability_diagnostics, list):
        observability_diagnostics = []

    print("autolab progress")
    print(f"state_file: {state_path}")
    print(f"generated_at: {payload.get('generated_at', '')}")
    print(f"iteration_id: {payload.get('iteration_id', '')}")
    print(f"experiment_id: {payload.get('experiment_id', '')}")
    print(f"scope: {payload.get('current_scope', 'experiment')}")
    print(f"stage: {payload.get('current_stage', '')}")
    verifier_passed = verifier.get("passed")
    if isinstance(verifier_passed, bool):
        verifier_passed_text = "true" if verifier_passed else "false"
    else:
        verifier_passed_text = "unavailable"
    print(f"verifier_passed: {verifier_passed_text}")
    print(f"verifier_message: {verifier.get('message', '')}")
    print(f"blocking_failures: {len(blocking)}")
    print(f"pending_human_decisions: {len(pending)}")
    print(f"recommended_next_command: {recommended.get('command', '')}")
    print(f"safe_resume_status: {safe_resume.get('status', 'blocked')}")
    critical_wave_ids = _observe_non_empty_strings(critical_path.get("wave_ids", []))
    critical_task_ids = _observe_non_empty_strings(critical_path.get("task_ids", []))
    print("critical_path:")
    print(
        "  "
        + (
            f"status={critical_path.get('status', 'unavailable')} "
            f"mode={critical_path.get('mode', 'unavailable')} "
            f"duration={_observe_format_seconds(critical_path.get('duration_seconds', 0), blank='0.0s')} "
            f"weight={critical_path.get('weight', 0)} "
            f"waves={len(critical_wave_ids)} "
            f"tasks={len(critical_task_ids)}"
        )
    )
    print(f"  basis: {critical_path.get('basis_note', '') or 'n/a'}")
    print(f"  wave_ids: {_observe_format_list(critical_wave_ids)}")
    print(f"  task_ids: {_observe_format_list(critical_task_ids)}")
    if observability_waves:
        print("wave_details:")
        for entry in observability_waves:
            if not isinstance(entry, dict):
                continue
            print(
                "  "
                + (
                    f"wave={entry.get('wave', '?')} status={entry.get('status', 'unknown')} "
                    f"tasks={len(_observe_non_empty_strings(entry.get('tasks')))} "
                    f"attempts={entry.get('attempts', 0)} retries={entry.get('retries_used', 0)} "
                    f"retry_pending={'yes' if bool(entry.get('retry_pending')) else 'no'} "
                    f"critical_path={'yes' if bool(entry.get('critical_path')) else 'no'} "
                    f"timing={_observe_format_seconds(entry.get('duration_seconds', 0), blank='n/a')} "
                    f"last_attempt={_observe_format_seconds(entry.get('last_attempt_duration_seconds', 0), blank='n/a')}"
                )
            )
            print(
                "    window: "
                f"{entry.get('started_at', '') or '-'} -> {entry.get('completed_at', '') or '-'}"
            )
            print(f"    task_ids: {_observe_format_list(entry.get('tasks'))}")
            retry_reasons = entry.get("retry_reasons", [])
            if isinstance(retry_reasons, list) and retry_reasons:
                print(
                    "    retry_reasons: "
                    + ", ".join(
                        str(item).strip() for item in retry_reasons if str(item).strip()
                    )
                )
            failed_task_ids = entry.get("failed_task_ids", [])
            if isinstance(failed_task_ids, list) and failed_task_ids:
                print(
                    "    failed_tasks: "
                    + ", ".join(
                        str(item).strip()
                        for item in failed_task_ids
                        if str(item).strip()
                    )
                )
            blocked_task_ids = entry.get("blocked_task_ids", [])
            if isinstance(blocked_task_ids, list) and blocked_task_ids:
                print(
                    "    blocked_tasks: "
                    + ", ".join(
                        str(item).strip()
                        for item in blocked_task_ids
                        if str(item).strip()
                    )
                )
            deferred_task_ids = entry.get("deferred_task_ids", [])
            if isinstance(deferred_task_ids, list) and deferred_task_ids:
                print(
                    "    deferred_tasks: "
                    + ", ".join(
                        str(item).strip()
                        for item in deferred_task_ids
                        if str(item).strip()
                    )
                )
            skipped_task_ids = entry.get("skipped_task_ids", [])
            if isinstance(skipped_task_ids, list) and skipped_task_ids:
                print(
                    "    skipped_tasks: "
                    + ", ".join(
                        str(item).strip()
                        for item in skipped_task_ids
                        if str(item).strip()
                    )
                )
            pending_task_ids = entry.get("pending_task_ids", [])
            if isinstance(pending_task_ids, list) and pending_task_ids:
                print(
                    "    pending_tasks: "
                    + ", ".join(
                        str(item).strip()
                        for item in pending_task_ids
                        if str(item).strip()
                    )
                )
    if file_conflicts:
        print("file_conflicts:")
        for entry in file_conflicts:
            if not isinstance(entry, dict):
                continue
            print(
                "  "
                + (
                    f"wave={entry.get('wave', '?')} kind={entry.get('kind', 'conflict')} "
                    f"tasks={','.join(str(item) for item in entry.get('tasks', []) if str(item).strip()) or '-'} "
                    f"detail={entry.get('detail', '')}"
                )
            )
    if observability_tasks:
        print("task_evidence:")
        shown = 0
        for entry in observability_tasks:
            if not isinstance(entry, dict):
                continue
            evidence = entry.get("evidence_summary", {})
            if not isinstance(evidence, dict):
                evidence = {}
            print(
                "  "
                + (
                    f"task={entry.get('task_id', '')} wave={entry.get('wave', '?')} "
                    f"status={entry.get('status', 'unknown')} "
                    f"attempts={entry.get('attempts', 0)} retries={entry.get('retries_used', 0)} "
                    f"critical_path={'yes' if bool(entry.get('critical_path')) else 'no'} "
                    f"timing={_observe_format_seconds(entry.get('duration_seconds', 0), blank='n/a')} "
                    f"verify={entry.get('verification_status', 'not_run') or 'not_run'}"
                )
            )
            reason_code = str(entry.get("reason_code", "")).strip()
            reason_detail = str(entry.get("reason_detail", "")).strip()
            if reason_code or reason_detail:
                reason_text = reason_code or "unknown"
                if reason_detail:
                    reason_text = (
                        f"{reason_code} ({reason_detail})"
                        if reason_code
                        else reason_detail
                    )
                print(f"    reason: {reason_text}")
            blocked_by = _observe_non_empty_strings(entry.get("blocked_by"))
            if blocked_by:
                print(f"    blocked_by: {', '.join(blocked_by)}")
            print(f"    evidence: {evidence.get('text', '') or 'n/a'}")
            shown += 1
            if shown >= 12:
                remaining = len(observability_tasks) - shown
                if remaining > 0:
                    print(f"  ... and {remaining} more")
                break
    if observability_diagnostics:
        print("observability_diagnostics:")
        for entry in observability_diagnostics:
            text = str(entry).strip()
            if text:
                print(f"  - {text}")
    rot_flags = payload.get("context_rot_flags", [])
    if isinstance(rot_flags, list) and rot_flags:
        print("context_rot_flags:")
        for flag in rot_flags:
            print(f"  - {str(flag).strip()}")
    last_cps = payload.get("last_good_checkpoints", [])
    if isinstance(last_cps, list) and last_cps:
        print("last_good_checkpoints:")
        for cp in last_cps[:3]:
            if isinstance(cp, dict):
                print(
                    f"  - {cp.get('checkpoint_id', '')} "
                    f"stage={cp.get('stage', '')} "
                    f"at={cp.get('created_at', '')}"
                )
    rewind_targets = payload.get("recommended_rewind_targets", [])
    if isinstance(rewind_targets, list) and rewind_targets:
        print(
            f"recommended_rewind_targets: {', '.join(str(t) for t in rewind_targets)}"
        )
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

    try:
        from autolab.checkpoint import create_checkpoint

        state = _normalize_state(_load_state(state_path))
        create_checkpoint(
            _resolve_repo_root(state_path),
            state_path=state_path,
            stage=str(state.get("stage", "")).strip(),
            trigger="handoff",
        )
    except Exception:
        pass

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
