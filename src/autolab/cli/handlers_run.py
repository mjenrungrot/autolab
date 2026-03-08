"""Execution-loop and runtime CLI handlers."""

from __future__ import annotations

from autolab.cli.support import *
from autolab.cli.handlers_observe import _safe_refresh_handoff
from autolab.config import _load_agent_runner_config
from autolab.plan_approval import (
    load_plan_approval,
    record_manual_uat_request,
    record_plan_approval_decision,
)
from autolab.render_debug import ALL_RENDER_VIEWS, build_render_stats_report
from autolab.scope import _resolve_project_wide_root, _resolve_scope_context
from autolab.uat import (
    render_uat_template,
    resolve_uat_requirement,
    resolve_uat_template_context,
)


def main(argv: list[str] | None = None) -> int:
    """Late-bind to autolab.commands.main to preserve monkeypatch compatibility."""
    from autolab.commands import main as commands_main

    return int(commands_main(argv))


def _cmd_verify(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    raw_stage = getattr(args, "stage", None)
    stage_override = str(raw_stage).strip() if raw_stage is not None else None
    if not stage_override:
        stage_override = None

    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        print(f"autolab verify: ERROR {exc}", file=sys.stderr)
        return 1

    passed, message, details = _run_verification_step_detailed(
        repo_root,
        state,
        stage_override=stage_override,
    )
    canonical_result_path = repo_root / ".autolab" / "verification_result.json"
    effective_stage = (
        str(details.get("stage", "")).strip()
        or str(state.get("stage", "")).strip()
        or "unknown"
    )
    safe_stage = (
        "".join(
            ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in effective_stage
        )
        or "unknown"
    )
    timestamp = _utc_now().replace("-", "").replace(":", "").replace(".", "")
    summary_path = (
        repo_root / ".autolab" / "logs" / f"verification_{timestamp}_{safe_stage}.json"
    )
    summary_payload = {
        "generated_at": _utc_now(),
        "state_file": str(state_path),
        "stage_requested": stage_override or str(state.get("stage", "")).strip(),
        "stage_effective": effective_stage,
        "passed": bool(passed),
        "message": message,
        "details": details,
    }
    _write_json(summary_path, summary_payload)
    retained_before, retained_deleted, retained_after = (
        _prune_verification_summary_logs(
            repo_root,
            keep_latest=VERIFICATION_SUMMARY_RETENTION_LIMIT,
        )
    )
    _append_log(
        repo_root,
        (
            f"verify stage={effective_stage} passed={passed} "
            f"summary={summary_path} message={message}"
        ),
    )
    _append_log(
        repo_root,
        (
            "verify log-retention "
            f"keep_latest={VERIFICATION_SUMMARY_RETENTION_LIMIT} "
            f"before={retained_before} deleted={retained_deleted} after={retained_after}"
        ),
    )

    print("autolab verify")
    print(f"state_file: {state_path}")
    print(f"stage: {effective_stage}")
    print(f"passed: {passed}")
    print(f"message: {message}")
    print(f"result: {canonical_result_path}")
    print(f"summary: {summary_path}")
    _handoff_payload, _handoff_error = _safe_refresh_handoff(state_path)
    if _handoff_payload is None:
        print(
            f"autolab verify: WARN failed to refresh handoff snapshot: {_handoff_error}",
            file=sys.stderr,
        )
    if not passed:
        print(f"autolab verify: ERROR {message}", file=sys.stderr)
        return 1
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    raw_stage = getattr(args, "stage", None)
    stage_override = str(raw_stage).strip() if raw_stage is not None else None
    if not stage_override:
        stage_override = None

    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        print(f"autolab render: ERROR {exc}", file=sys.stderr)
        return 1

    stage = stage_override or str(state.get("stage", "")).strip()
    if not stage:
        print("autolab render: ERROR unable to determine stage", file=sys.stderr)
        return 1

    state_for_render = dict(state)
    state_for_render["stage"] = stage

    try:
        scope_kind, scope_root, _iteration_dir = _resolve_scope_context(
            repo_root,
            iteration_id=str(state.get("iteration_id", "")).strip(),
            experiment_id=str(state.get("experiment_id", "")).strip(),
        )
        project_wide_root = _resolve_project_wide_root(repo_root)
        runner_config = _load_agent_runner_config(repo_root)
        allowed_edit_dirs = (
            list(runner_config.edit_scope.core_dirs)
            if runner_config.edit_scope.mode == "scope_root_plus_core"
            else []
        )
        runner_scope = {
            "mode": runner_config.edit_scope.mode,
            "scope_kind": scope_kind,
            "scope_root": str(scope_root),
            "project_wide_root": str(project_wide_root),
            "workspace_dir": str(scope_root),
            "allowed_edit_dirs": allowed_edit_dirs,
        }
        template_path = _resolve_stage_prompt_path(
            repo_root, stage, prompt_role="runner"
        )
        bundle = _render_stage_prompt(
            repo_root,
            stage=stage,
            state=state_for_render,
            template_path=template_path,
            runner_scope=runner_scope,
            write_outputs=False,
        )
    except StageCheckError as exc:
        print(f"autolab render: ERROR {exc}", file=sys.stderr)
        return 1

    view = str(getattr(args, "view", "") or "").strip().lower()
    if view and view not in ALL_RENDER_VIEWS:
        print(f"autolab render: ERROR unsupported view '{view}'", file=sys.stderr)
        return 1

    stats_enabled = bool(getattr(args, "stats", False))
    if stats_enabled:
        stats_views = [view] if view else list(ALL_RENDER_VIEWS)
        rendered_text = build_render_stats_report(
            stage=stage,
            bundle=bundle,
            views=stats_views,
        )
    else:
        selected_view = view or "runner"
        if selected_view == "runner":
            rendered_text = bundle.prompt_text
        elif selected_view == "audit":
            rendered_text = bundle.audit_text
        elif selected_view == "brief":
            rendered_text = bundle.brief_text
        elif selected_view == "human":
            rendered_text = bundle.human_text
        elif selected_view == "context":
            rendered_text = json.dumps(bundle.context_payload, indent=2)
        else:
            print(
                f"autolab render: ERROR unsupported view '{selected_view}'",
                file=sys.stderr,
            )
            return 1

    if rendered_text:
        sys.stdout.write(rendered_text)
        if not rendered_text.endswith("\n"):
            sys.stdout.write("\n")

    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    lock_path = autolab_dir / "lock"
    run_agent_mode = _resolve_run_agent_mode(getattr(args, "run_agent_mode", "policy"))
    assistant_mode = bool(getattr(args, "assistant", False))
    lock_ok, lock_msg = _acquire_lock(
        lock_path,
        state_file=state_path,
        command=" ".join(sys.argv),
        stale_seconds=LOCK_STALE_SECONDS,
    )
    if not lock_ok:
        print(f"autolab run: ERROR {lock_msg}", file=sys.stderr)
        return 1
    lock_acquired = True

    try:
        with _periodic_run_lock_heartbeat(lock_path):
            _append_log(repo_root, f"run lock acquired: {lock_msg}")
            baseline_snapshot = _collect_change_snapshot(repo_root)
            run_once_kwargs = {
                "run_agent_mode": run_agent_mode,
                "verify_before_evaluate": bool(getattr(args, "verify", False)),
                "assistant": assistant_mode,
                "auto_mode": False,
                "auto_decision": bool(getattr(args, "auto_decision", False)),
                "strict_implementation_progress": bool(
                    getattr(args, "strict_implementation_progress", True)
                ),
                "plan_only": bool(getattr(args, "plan_only", False)),
                "execute_approved_plan": bool(
                    getattr(args, "execute_approved_plan", False)
                ),
            }
            try:
                outcome = _run_once(
                    state_path,
                    args.decision,
                    **run_once_kwargs,
                )
            except TypeError as exc:
                if (
                    not run_once_kwargs["plan_only"]
                    and not run_once_kwargs["execute_approved_plan"]
                    and ("plan_only" in str(exc) or "execute_approved_plan" in str(exc))
                ):
                    run_once_kwargs.pop("plan_only", None)
                    run_once_kwargs.pop("execute_approved_plan", None)
                    outcome = _run_once(
                        state_path,
                        args.decision,
                        **run_once_kwargs,
                    )
                else:
                    raise
            commit_outcome = _prepare_standard_commit_outcome(
                repo_root,
                outcome,
                baseline_snapshot,
                assistant=assistant_mode,
                strict_implementation_progress=bool(
                    getattr(args, "strict_implementation_progress", True)
                ),
            )
            commit_summary = _try_auto_commit(repo_root, outcome=commit_outcome)
            print("autolab run")
            print(f"state_file: {state_path}")
            print(f"run_agent_mode: {run_agent_mode}")
            print(f"assistant: {bool(getattr(args, 'assistant', False))}")
            print(f"verify_before_evaluate: {bool(getattr(args, 'verify', False))}")
            print(f"auto_decision: {bool(getattr(args, 'auto_decision', False))}")
            print(f"plan_only: {bool(getattr(args, 'plan_only', False))}")
            print(
                "execute_approved_plan: "
                f"{bool(getattr(args, 'execute_approved_plan', False))}"
            )
            print(f"stage_before: {outcome.stage_before}")
            print(f"stage_after: {outcome.stage_after}")
            print(f"transitioned: {outcome.transitioned}")
            if outcome.pause_reason:
                print(f"pause_reason: {outcome.pause_reason}")
            print(f"message: {outcome.message}")
            print(commit_summary)
            _handoff_payload, _handoff_error = _safe_refresh_handoff(state_path)
            if _handoff_payload is None:
                print(
                    f"autolab run: WARN failed to refresh handoff snapshot: {_handoff_error}",
                    file=sys.stderr,
                )
            if outcome.exit_code != 0:
                print(f"autolab run: ERROR {outcome.message}", file=sys.stderr)

                # Phase 7a: manual mode hint
                stage = outcome.stage_before
                prompt_file = STAGE_PROMPT_FILES.get(stage)
                if prompt_file:
                    print(
                        f"\nHint: Follow instructions in .autolab/prompts/{prompt_file} to complete the '{stage}' stage."
                    )

            return outcome.exit_code
    finally:
        if lock_acquired:
            _release_lock(lock_path)


def _cmd_loop(args: argparse.Namespace) -> int:
    if args.max_iterations <= 0:
        print("autolab loop: ERROR --max-iterations must be > 0", file=sys.stderr)
        return 2
    if args.auto and args.max_hours <= 0:
        print(
            "autolab loop: ERROR --max-hours must be > 0 when --auto is enabled",
            file=sys.stderr,
        )
        return 2

    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    effective_max_iterations = int(args.max_iterations)
    try:
        state_for_limit = _normalize_state(_load_state(state_path))
        state_limit = int(
            state_for_limit.get("max_total_iterations", effective_max_iterations)
        )
        if state_limit > 0:
            effective_max_iterations = min(effective_max_iterations, state_limit)
    except StateError:
        state_for_limit = None
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    lock_path = autolab_dir / "lock"
    max_hours = float(args.max_hours)
    started_at = _utc_now()
    started_monotonic = time.monotonic()
    todo_open_before = _todo_open_count(repo_root)
    terminal_reason = "iteration_budget_reached"
    loop_rows: list[dict[str, Any]] = []
    auto_decision_count = 0
    retry_escalation_count = 0
    consecutive_errors = 0
    recoverable_error_count = 0
    overall_exit_code = 0
    lock_acquired = False

    print("autolab loop")
    print(f"state_file: {state_path}")
    print(f"max_iterations: {effective_max_iterations}")
    if effective_max_iterations != int(args.max_iterations):
        print(
            f"max_iterations_clamped_by_state: {state_for_limit['max_total_iterations']}"
        )
    run_agent_mode = _resolve_run_agent_mode(getattr(args, "run_agent_mode", "policy"))
    auto_decision_enabled = bool(args.auto or run_agent_mode == "force_on")
    assistant_mode = bool(getattr(args, "assistant", False))
    print(f"run_agent_mode: {run_agent_mode}")
    print(f"assistant: {assistant_mode}")
    print(f"verify_before_evaluate: {bool(getattr(args, 'verify', False))}")
    print(f"plan_only: {bool(getattr(args, 'plan_only', False))}")
    print(
        f"execute_approved_plan: {bool(getattr(args, 'execute_approved_plan', False))}"
    )
    if args.auto:
        print("auto: true")
        print(f"max_hours: {max_hours}")
    lock_ok, lock_msg = _acquire_lock(
        lock_path,
        state_file=state_path,
        command=" ".join(sys.argv),
        stale_seconds=LOCK_STALE_SECONDS,
    )
    if not lock_ok:
        print(f"autolab loop: ERROR {lock_msg}", file=sys.stderr)
        return 1
    lock_acquired = True
    _append_log(repo_root, f"loop lock acquired: {lock_msg}")
    if args.auto:
        _guardrail_cfg = _load_guardrail_config(repo_root)
        _max_consecutive_errors = _guardrail_cfg.max_consecutive_errors
        _error_backoff_base = _guardrail_cfg.error_backoff_base_seconds

    try:
        for index in range(1, effective_max_iterations + 1):
            if args.auto and (time.monotonic() - started_monotonic) >= max_hours * 3600:
                terminal_reason = "time_budget_reached"
                print("autolab loop: stop (time budget reached)")
                break

            decision: str | None = None
            current_stage = ""
            if args.auto:
                try:
                    current_state = _normalize_state(_load_state(state_path))
                except StateError:
                    current_state = None
                if current_state is not None:
                    current_stage = str(current_state.get("stage", ""))
                if current_stage == "decide_repeat":
                    auto_decision_count += 1
                _heartbeat_lock(lock_path)

            baseline_snapshot = _collect_change_snapshot(repo_root)
            outcome = _run_once(
                state_path,
                decision if args.auto else None,
                run_agent_mode=run_agent_mode,
                verify_before_evaluate=bool(getattr(args, "verify", False)),
                assistant=assistant_mode,
                auto_mode=bool(args.auto),
                auto_decision=auto_decision_enabled,
                strict_implementation_progress=bool(
                    getattr(args, "strict_implementation_progress", True)
                ),
                plan_only=bool(getattr(args, "plan_only", False)),
                execute_approved_plan=bool(
                    getattr(args, "execute_approved_plan", False)
                ),
            )
            commit_outcome = _prepare_standard_commit_outcome(
                repo_root,
                outcome,
                baseline_snapshot,
                assistant=assistant_mode,
                strict_implementation_progress=bool(
                    getattr(args, "strict_implementation_progress", True)
                ),
            )
            commit_summary = _try_auto_commit(repo_root, outcome=commit_outcome)
            if "escalating to human_review" in outcome.message:
                retry_escalation_count += 1
            _is_recoverable = (
                outcome.exit_code != 0
                and args.auto
                and outcome.stage_after not in TERMINAL_STAGES
            )
            loop_rows.append(
                {
                    "index": index,
                    "stage_before": outcome.stage_before,
                    "stage_after": outcome.stage_after,
                    "transitioned": outcome.transitioned,
                    "exit_code": outcome.exit_code,
                    "decision": "auto"
                    if args.auto and current_stage == "decide_repeat"
                    else "-",
                    "message": outcome.message,
                    "recoverable": _is_recoverable,
                }
            )
            print(
                f"iteration {index}: {outcome.stage_before} -> {outcome.stage_after} "
                f"(transitioned={outcome.transitioned}, exit={outcome.exit_code})"
            )
            if outcome.pause_reason:
                print(f"iteration {index}: pause_reason={outcome.pause_reason}")
            print(f"iteration {index}: {commit_summary}")
            _handoff_payload, _handoff_error = _safe_refresh_handoff(state_path)
            if _handoff_payload is None:
                print(
                    f"autolab loop: WARN failed to refresh handoff snapshot: {_handoff_error}",
                    file=sys.stderr,
                )
            if outcome.exit_code == 0:
                consecutive_errors = 0

            if outcome.exit_code != 0:
                print(f"autolab loop: ERROR {outcome.message}", file=sys.stderr)
                overall_exit_code = outcome.exit_code
                consecutive_errors += 1

                # Auto mode: recoverable errors continue the loop
                if args.auto and outcome.stage_after not in TERMINAL_STAGES:
                    recoverable_error_count += 1
                    if consecutive_errors >= _max_consecutive_errors:
                        terminal_reason = "consecutive_error_limit"
                        print(
                            f"autolab loop: stop (consecutive error limit "
                            f"{consecutive_errors}/{_max_consecutive_errors})",
                            file=sys.stderr,
                        )
                        break
                    backoff = min(
                        _error_backoff_base * (2 ** (consecutive_errors - 1)), 300.0
                    )
                    if backoff > 0:
                        print(
                            f"autolab loop: recoverable error, backoff {backoff:.0f}s "
                            f"before retry ({consecutive_errors}/{_max_consecutive_errors})"
                        )
                        time.sleep(backoff)
                    continue

                # Fatal or interactive: break as before
                terminal_reason = "error"
                if outcome.stage_after == "human_review":
                    terminal_reason = "human_review"
                break
            if outcome.stage_after in TERMINAL_STAGES:
                terminal_reason = outcome.stage_after
                print(f"autolab loop: stop (terminal stage): {outcome.stage_after}")
                if args.auto and outcome.stage_after == "human_review":
                    overall_exit_code = 1
                break
            if outcome.pause_reason == "plan_approval_required":
                terminal_reason = "plan_approval_required"
                print("autolab loop: stop (plan approval required)")
                break
            if outcome.pause_reason == "plan_only":
                terminal_reason = "plan_only"
                print("autolab loop: stop (plan-only requested)")
                break
            if not outcome.transitioned:
                continue_auto_after_implementation_wave = bool(
                    args.auto
                    and outcome.exit_code == 0
                    and outcome.stage_before == "implementation"
                    and outcome.stage_after == "implementation"
                )
                if assistant_mode and outcome.exit_code == 0:
                    continue
                if continue_auto_after_implementation_wave:
                    print(
                        "autolab loop: continue (implementation wave completed without stage transition)"
                    )
                    continue
                terminal_reason = "no_transition"
                print(f"autolab loop: stop (no transition): {outcome.message}")
                break
        else:
            terminal_reason = "iteration_budget_reached"

        final_stage = "<unknown>"
        try:
            final_state = _normalize_state(_load_state(state_path))
            final_stage = str(final_state["stage"])
        except StateError:
            pass

        if args.auto and final_stage == "human_review" and overall_exit_code == 0:
            overall_exit_code = 1
            terminal_reason = "human_review"

        print("autolab loop: complete")
        return overall_exit_code
    finally:
        ended_at = _utc_now()
        elapsed_seconds = time.monotonic() - started_monotonic
        if args.auto:
            final_stage = "<unknown>"
            try:
                final_state = _normalize_state(_load_state(state_path))
                final_stage = str(final_state["stage"])
            except StateError:
                pass
            todo_open_after = _todo_open_count(repo_root)
            try:
                _write_overnight_summary(
                    repo_root,
                    state_path=state_path,
                    started_at=started_at,
                    ended_at=ended_at,
                    elapsed_seconds=elapsed_seconds,
                    max_iterations=int(args.max_iterations),
                    max_hours=max_hours,
                    auto_decision_count=auto_decision_count,
                    retry_escalation_count=retry_escalation_count,
                    recoverable_error_count=recoverable_error_count,
                    consecutive_errors_at_exit=consecutive_errors,
                    todo_open_before=todo_open_before,
                    todo_open_after=todo_open_after,
                    terminal_reason=terminal_reason,
                    final_stage=final_stage,
                    exit_code=overall_exit_code,
                    rows=loop_rows,
                )
            except Exception as exc:
                print(
                    f"autolab loop: WARN failed to write overnight summary: {exc}",
                    file=sys.stderr,
                )
        if lock_acquired:
            _release_lock(lock_path)


# ---------------------------------------------------------------------------
# Interactive TUI cockpit
# ---------------------------------------------------------------------------


def _cmd_tui(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    raw_tail_lines = getattr(args, "tail_lines", 2000)
    try:
        tail_lines = 2000 if raw_tail_lines is None else int(raw_tail_lines)
    except Exception:
        print("autolab tui: ERROR --tail-lines must be an integer > 0", file=sys.stderr)
        return 2
    if tail_lines <= 0:
        print("autolab tui: ERROR --tail-lines must be > 0", file=sys.stderr)
        return 2
    if not state_path.exists():
        print(
            f"autolab tui: ERROR state file does not exist: {state_path}",
            file=sys.stderr,
        )
        return 1
    if not state_path.is_file():
        print(
            f"autolab tui: ERROR state path is not a file: {state_path}",
            file=sys.stderr,
        )
        return 1
    if not os.access(state_path, os.R_OK):
        print(
            f"autolab tui: ERROR state file is not readable: {state_path}",
            file=sys.stderr,
        )
        return 1
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "autolab tui: ERROR requires an interactive TTY (stdin/stdout).",
            file=sys.stderr,
        )
        return 1

    try:
        from autolab.tui.app import AutolabCockpitApp
    except Exception as exc:
        print(
            f"autolab tui: ERROR failed to load Textual cockpit: {exc}", file=sys.stderr
        )
        return 1

    app = AutolabCockpitApp(state_path=state_path, tail_lines=tail_lines)
    try:
        app.run()
    except KeyboardInterrupt:
        print("autolab tui: interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"autolab tui: ERROR app runtime failed: {exc}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Phase 6b: review command
# ---------------------------------------------------------------------------


def _cmd_review(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        print(f"autolab review: ERROR {exc}", file=sys.stderr)
        return 1
    if state["stage"] != "human_review":
        print(
            f"autolab review: ERROR current stage is '{state['stage']}', not 'human_review'",
            file=sys.stderr,
        )
        return 1
    status = args.status
    if status == "pass":
        state["stage"] = "launch"
        state["stage_attempt"] = 0
        message = "human review decision: pass — advancing to launch"
    elif status == "retry":
        state["stage"] = "implementation"
        state["stage_attempt"] = 0
        message = "human review decision: retry — returning to implementation"
    elif status == "stop":
        state["stage"] = "stop"
        state["stage_attempt"] = 0
        state["current_task_id"] = ""
        state["task_cycle_stage"] = "done"
        state["task_change_baseline"] = {}
        completed, backlog_path, completion_summary = (
            _mark_backlog_experiment_completed(
                repo_root,
                str(state.get("experiment_id", "")).strip(),
            )
        )
        message = "human review decision: stop — experiment ended"
        if completed:
            message = f"{message}; {completion_summary}"
    else:
        print(f"autolab review: ERROR invalid status '{status}'", file=sys.stderr)
        return 1
    _write_json(state_path, state)
    _persist_agent_result(
        repo_root, status="complete", summary=message, changed_files=[state_path]
    )
    _handoff_payload, _handoff_error = _safe_refresh_handoff(state_path)
    if _handoff_payload is None:
        print(
            f"autolab review: WARN failed to refresh handoff snapshot: {_handoff_error}",
            file=sys.stderr,
        )
    _append_log(repo_root, f"review command: {message}")
    print(f"autolab review: {message}")
    return 0


def _cmd_approve_plan(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        print(f"autolab approve-plan: ERROR {exc}", file=sys.stderr)
        return 1
    if str(state.get("stage", "")).strip() != "implementation":
        print(
            "autolab approve-plan: ERROR current stage must be 'implementation'",
            file=sys.stderr,
        )
        return 1

    iteration_id = str(state.get("iteration_id", "")).strip()
    experiment_id = str(state.get("experiment_id", "")).strip()
    iteration_dir, _ = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        require_exists=False,
    )

    decision = str(args.status).strip().lower()
    normalized_status = "approved" if decision == "approve" else decision
    try:
        approval_payload = record_plan_approval_decision(
            iteration_dir,
            status=normalized_status,
            notes=str(getattr(args, "notes", "") or "").strip(),
            require_uat=bool(getattr(args, "require_uat", False)),
        )
    except RuntimeError as exc:
        print(f"autolab approve-plan: ERROR {exc}", file=sys.stderr)
        return 1

    changed_files: list[Path] = [
        iteration_dir / "plan_approval.json",
        iteration_dir / "plan_approval.md",
    ]
    if normalized_status == "approved":
        message = (
            "implementation plan approval recorded: approved — "
            "run `autolab run --execute-approved-plan` or `autolab run` to continue"
        )
        if bool(getattr(args, "require_uat", False)):
            message = f"{message}; UAT required"
    elif normalized_status == "retry":
        message = (
            "implementation plan approval recorded: retry — "
            "rerun `autolab run` to regenerate the plan"
        )
    else:
        state["stage"] = "stop"
        state["stage_attempt"] = 0
        state["current_task_id"] = ""
        state["task_cycle_stage"] = "done"
        state["task_change_baseline"] = {}
        completed, backlog_path, completion_summary = (
            _mark_backlog_experiment_completed(
                repo_root,
                str(state.get("experiment_id", "")).strip(),
            )
        )
        _write_json(state_path, state)
        changed_files.append(state_path)
        if completed:
            changed_files.append(backlog_path)
        message = "implementation plan approval recorded: stop — experiment ended"
        if completed:
            message = f"{message}; {completion_summary}"

    if normalized_status == "approved":
        try:
            from autolab.checkpoint import create_checkpoint

            create_checkpoint(
                repo_root,
                state_path=state_path,
                stage="implementation",
                trigger="auto",
                label="plan_approved",
            )
        except Exception:
            pass

    if normalized_status != "stop":
        _write_json(state_path, state)
        changed_files.append(state_path)

    _persist_agent_result(
        repo_root,
        status="complete",
        summary=message,
        changed_files=changed_files,
    )
    _handoff_payload, _handoff_error = _safe_refresh_handoff(state_path)
    if _handoff_payload is None:
        print(
            f"autolab approve-plan: WARN failed to refresh handoff snapshot: {_handoff_error}",
            file=sys.stderr,
        )
    approval_status = str(approval_payload.get("status", "")).strip()
    _append_log(repo_root, f"approve-plan command: {approval_status} {message}")
    print(f"autolab approve-plan: {message}")
    return 0


def _cmd_uat_init(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        print(f"autolab uat init: ERROR {exc}", file=sys.stderr)
        return 1

    iteration_id = str(state.get("iteration_id", "")).strip()
    experiment_id = str(state.get("experiment_id", "")).strip()
    if not iteration_id:
        print(
            "autolab uat init: ERROR current state is missing iteration_id",
            file=sys.stderr,
        )
        return 1

    iteration_dir, _ = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        require_exists=False,
    )
    iteration_dir.mkdir(parents=True, exist_ok=True)

    changed_files: list[Path] = []
    plan_approval_payload = load_plan_approval(iteration_dir)
    summary = resolve_uat_requirement(
        repo_root,
        iteration_dir,
        plan_approval_payload=plan_approval_payload if plan_approval_payload else None,
    )

    requested_manually = False
    if not bool(summary.get("effective_required", False)):
        before = load_plan_approval(iteration_dir)
        updated_payload = record_manual_uat_request(iteration_dir)
        requested_manually = True
        if updated_payload:
            plan_approval_payload = updated_payload
            changed_files.extend(
                [
                    iteration_dir / "plan_approval.json",
                    iteration_dir / "plan_approval.md",
                ]
            )
        elif before:
            plan_approval_payload = before
        summary = resolve_uat_requirement(
            repo_root,
            iteration_dir,
            plan_approval_payload=plan_approval_payload
            if plan_approval_payload
            else None,
        )

    artifact_path = Path(str(summary.get("artifact_path", iteration_dir / "uat.md")))
    if artifact_path.exists():
        if changed_files:
            message = f"preserved existing UAT artifact at {artifact_path}"
            _persist_agent_result(
                repo_root,
                status="complete",
                summary=message,
                changed_files=changed_files,
            )
            _handoff_payload, _handoff_error = _safe_refresh_handoff(state_path)
            if _handoff_payload is None:
                print(
                    f"autolab uat init: WARN failed to refresh handoff snapshot: {_handoff_error}",
                    file=sys.stderr,
                )
            _append_log(repo_root, f"uat init command: {message}")
        print(f"autolab uat init: existing artifact preserved at {artifact_path}")
        return 0

    required_by = str(summary.get("required_by", "")).strip() or "manual"
    if requested_manually and required_by == "none":
        required_by = "manual"
    context = resolve_uat_template_context(repo_root)
    suggested_checks = None
    if bool(getattr(args, "suggest", False)):
        raw_suggestions = summary.get("suggested_checks")
        if isinstance(raw_suggestions, list):
            suggested_checks = [
                suggestion
                for suggestion in raw_suggestions
                if isinstance(suggestion, dict)
            ]
    artifact_path.write_text(
        render_uat_template(
            iteration_id=iteration_id,
            scope_kind=str(summary.get("scope_kind", "experiment")).strip()
            or "experiment",
            required_by=required_by,
            revision_label=str(
                context.get("revision_label", "unversioned-worktree")
            ).strip()
            or "unversioned-worktree",
            host_mode=str(context.get("host_mode", "local")).strip() or "local",
            remote_profile=str(context.get("remote_profile", "none")).strip() or "none",
            suggested_checks=suggested_checks,
        ),
        encoding="utf-8",
    )
    changed_files.append(artifact_path)

    reason = str(summary.get("required_by", "")).strip() or required_by
    if requested_manually and reason == "none":
        reason = "manual"
    message = f"initialized UAT template at {artifact_path} (required_by={reason})"
    _persist_agent_result(
        repo_root,
        status="complete",
        summary=message,
        changed_files=changed_files,
    )
    _handoff_payload, _handoff_error = _safe_refresh_handoff(state_path)
    if _handoff_payload is None:
        print(
            f"autolab uat init: WARN failed to refresh handoff snapshot: {_handoff_error}",
            file=sys.stderr,
        )
    _append_log(repo_root, f"uat init command: {message}")
    print(f"autolab uat init: {message}")
    return 0


# ---------------------------------------------------------------------------
# Lock management
# ---------------------------------------------------------------------------


def _cmd_lock(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    lock_path = autolab_dir / "lock"
    action = args.action

    if action == "status":
        info = _inspect_lock(lock_path)
        if info is None:
            print("autolab lock: no active lock")
            return 0
        print("autolab lock: active")
        for key in (
            "pid",
            "host",
            "owner_uuid",
            "started_at",
            "last_heartbeat_at",
            "command",
            "state_file",
        ):
            print(f"  {key}: {info.get(key, '<unknown>')}")
        age = info.get("age_seconds")
        if age is not None:
            print(f"  age: {age:.0f}s")
        return 0

    if action == "break":
        reason = getattr(args, "reason", "") or "manual break"
        message = _force_break_lock(lock_path, reason=reason)
        _append_log(repo_root, f"lock break: {message}")
        print(f"autolab lock: {message}")
        return 0

    print(f"autolab lock: unknown action '{action}'", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Skip stage
# ---------------------------------------------------------------------------


def _cmd_skip(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    lock_path = autolab_dir / "lock"
    lock_ok, lock_msg = _acquire_lock(
        lock_path,
        state_file=state_path,
        command=" ".join(sys.argv),
        stale_seconds=LOCK_STALE_SECONDS,
    )
    if not lock_ok:
        print(f"autolab skip: ERROR {lock_msg}", file=sys.stderr)
        return 1

    try:
        try:
            state = _load_state(state_path)
        except RuntimeError as exc:
            print(f"autolab skip: ERROR {exc}", file=sys.stderr)
            return 1

        current_stage = str(state.get("stage", "")).strip()
        target_stage = args.stage
        reason = args.reason

        if current_stage in TERMINAL_STAGES:
            print(
                f"autolab skip: ERROR current stage '{current_stage}' is terminal; cannot skip",
                file=sys.stderr,
            )
            return 1

        if target_stage in TERMINAL_STAGES:
            print(
                f"autolab skip: ERROR cannot skip to terminal stage '{target_stage}'",
                file=sys.stderr,
            )
            return 1

        # Validate forward-only skip within ACTIVE_STAGES (includes decide_repeat)
        ordered_stages = list(ACTIVE_STAGES)
        if "decide_repeat" not in ordered_stages:
            ordered_stages.append("decide_repeat")
        if current_stage not in ordered_stages:
            print(
                f"autolab skip: ERROR current stage '{current_stage}' is not skippable",
                file=sys.stderr,
            )
            return 1
        if target_stage not in ordered_stages:
            print(
                f"autolab skip: ERROR target stage '{target_stage}' is not a valid skip target",
                file=sys.stderr,
            )
            return 1
        current_idx = ordered_stages.index(current_stage)
        target_idx = ordered_stages.index(target_stage)
        if target_idx <= current_idx:
            print(
                f"autolab skip: ERROR can only skip forward (current={current_stage}, target={target_stage})",
                file=sys.stderr,
            )
            return 1

        state["stage"] = target_stage
        state["stage_attempt"] = 0
        _append_state_history(
            state,
            stage_before=current_stage,
            stage_after=target_stage,
            status="manual_skip",
            summary=f"manual skip from {current_stage} to {target_stage}: {reason}",
        )
        _write_json(state_path, state)
        _handoff_payload, _handoff_error = _safe_refresh_handoff(state_path)
        if _handoff_payload is None:
            print(
                f"autolab skip: WARN failed to refresh handoff snapshot: {_handoff_error}",
                file=sys.stderr,
            )
        _append_log(
            repo_root, f"skip: {current_stage} -> {target_stage} reason={reason}"
        )
        print(f"autolab skip: {current_stage} -> {target_stage}")
        return 0
    finally:
        _release_lock(lock_path)


# ---------------------------------------------------------------------------
# Lint (alias for verify)
# ---------------------------------------------------------------------------


def _cmd_lint(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    try:
        state = _load_state(state_path)
    except RuntimeError as exc:
        print(f"autolab lint: ERROR {exc}", file=sys.stderr)
        return 1

    stage_override = getattr(args, "stage", None)
    stage = stage_override or str(state.get("stage", "")).strip()
    if not stage:
        print("autolab lint: ERROR unable to determine stage", file=sys.stderr)
        return 1

    passed, detail_message, _details = _run_verification_step_detailed(
        repo_root, state, stage_override=stage_override
    )
    status = "PASS" if passed else "FAIL"
    print(f"autolab lint: {status} stage={stage}")
    if detail_message:
        print(detail_message)
    return 0 if passed else 1


# ---------------------------------------------------------------------------
# Verify golden iteration (self-test against bundled fixtures)
# ---------------------------------------------------------------------------


def _cmd_verify_golden(args: argparse.Namespace) -> int:
    """Run verifiers against bundled golden iteration fixtures.

    Creates a temporary directory, copies the scaffold and golden iteration
    fixtures into it, then runs ``autolab verify --stage <stage>`` for every
    active stage plus ``decide_repeat``.  Reports pass/fail for each stage
    and returns 0 if all pass, 1 if any fail.
    """
    stages = list(ACTIVE_STAGES)
    if "decide_repeat" not in stages:
        stages.append("decide_repeat")
    results: list[tuple[str, bool]] = []
    with ExitStack() as resource_stack:
        golden_resource = importlib_resources.files("autolab").joinpath(
            "example_golden_iterations"
        )
        scaffold_resource = importlib_resources.files("autolab").joinpath(
            "scaffold", ".autolab"
        )
        if not golden_resource.is_dir():
            print(
                "autolab verify-golden: ERROR packaged golden iteration fixtures are unavailable "
                "(expected package://autolab/example_golden_iterations)",
                file=sys.stderr,
            )
            return 1
        if not scaffold_resource.is_dir():
            print(
                "autolab verify-golden: ERROR packaged scaffold is unavailable "
                "(expected package://autolab/scaffold/.autolab)",
                file=sys.stderr,
            )
            return 1

        golden_root = resource_stack.enter_context(
            importlib_resources.as_file(golden_resource)
        )
        scaffold_source = resource_stack.enter_context(
            importlib_resources.as_file(scaffold_resource)
        )

        with tempfile.TemporaryDirectory(prefix="autolab_verify_golden_") as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()

            # 1. Copy scaffold to .autolab/
            target_autolab = repo / ".autolab"
            shutil.copytree(scaffold_source, target_autolab, dirs_exist_ok=True)

            # 2. Patch verifier_policy.yaml: replace python_bin with actual binary
            #    and replace the dry-run stub so it passes.
            policy_path = target_autolab / "verifier_policy.yaml"
            policy_text = policy_path.read_text(encoding="utf-8")
            had_trailing_newline = policy_text.endswith("\n")
            policy_lines = policy_text.splitlines()
            for idx, line in enumerate(policy_lines):
                if line.strip().startswith("python_bin:"):
                    policy_lines[idx] = f'python_bin: "{sys.executable}"'
                    break
            for idx, line in enumerate(policy_lines):
                if line.strip().startswith("dry_run_command:"):
                    policy_lines[idx] = (
                        'dry_run_command: "{{python_bin}} -c \\"print(\'golden iteration dry-run: OK\')\\""'
                    )
                    break
            updated_policy_text = "\n".join(policy_lines)
            if had_trailing_newline:
                updated_policy_text += "\n"
            policy_path.write_text(updated_policy_text, encoding="utf-8")

            # 3. Copy golden iteration experiments/ and paper/
            shutil.copytree(
                golden_root / "experiments", repo / "experiments", dirs_exist_ok=True
            )
            shutil.copytree(golden_root / "paper", repo / "paper", dirs_exist_ok=True)

            # 4. Overlay packaged golden .autolab fixtures (state/backlog plus
            # supporting contract artifacts such as plan_contract.json).
            golden_autolab_root = golden_root / ".autolab"
            if not golden_autolab_root.is_dir():
                print(
                    "autolab verify-golden: ERROR packaged golden .autolab fixtures are unavailable "
                    "(expected package://autolab/example_golden_iterations/.autolab)",
                    file=sys.stderr,
                )
                return 1
            for source_path in sorted(golden_autolab_root.rglob("*")):
                if not source_path.is_file():
                    continue
                relative_path = source_path.relative_to(golden_autolab_root)
                destination_path = target_autolab / relative_path
                destination_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination_path)

            # 5. Write minimal agent_result.json
            agent_result = {
                "status": "complete",
                "summary": "golden fixture",
                "changed_files": [],
                "completion_token_seen": True,
            }
            (target_autolab / "agent_result.json").write_text(
                json.dumps(agent_result, indent=2), encoding="utf-8"
            )

            state_path = target_autolab / "state.json"

            # 6. Run verify for each stage
            print("autolab verify-golden")
            for stage in stages:
                exit_code = main(
                    ["verify", "--state-file", str(state_path), "--stage", stage]
                )
                passed = exit_code == 0
                results.append((stage, passed))
                status_label = "PASS" if passed else "FAIL"
                print(f"  {stage}: {status_label}")

    # 7. Print summary
    total = len(results)
    passed_count = sum(1 for _, ok in results if ok)
    failed_count = total - passed_count
    print("")
    print(f"stages_total: {total}")
    print(f"stages_passed: {passed_count}")
    print(f"stages_failed: {failed_count}")
    if failed_count > 0:
        failed_stages = [name for name, ok in results if not ok]
        print(f"failed: {', '.join(failed_stages)}")
        print("autolab verify-golden: FAIL", file=sys.stderr)
        return 1
    print("autolab verify-golden: ALL PASSED")
    return 0


# ---------------------------------------------------------------------------
# Explain stage command
# ---------------------------------------------------------------------------


__all__ = [name for name in globals() if not name.startswith("__")]
