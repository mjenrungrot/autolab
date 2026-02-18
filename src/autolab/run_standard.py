from __future__ import annotations
from pathlib import Path
from typing import Any

from autolab.constants import TERMINAL_STAGES
from autolab.models import EvalResult, RunOutcome, StageCheckError
from autolab.config import (
    _load_guardrail_config,
    _load_meaningful_change_config,
    _resolve_run_agent_mode,
)
from autolab.evaluate import _evaluate_stage
from autolab.runners import _invoke_agent_runner
from autolab.state import (
    _append_state_history,
    _infer_unique_experiment_id_from_backlog,
    _is_active_experiment_completed,
    _load_state,
    _mark_backlog_experiment_completed,
    _normalize_state,
)
from autolab.utils import (
    _append_log,
    _append_todo_message,
    _collect_change_snapshot,
    _detect_priority_host_mode,
    _evaluate_meaningful_change,
    _has_open_stage_todo_task,
    _is_git_worktree,
    _meaningful_progress_detail,
    _outcome_payload,
    _persist_agent_result,
    _safe_todo_post_sync,
    _safe_todo_pre_sync,
    _todo_open_count,
    _write_json,
)
from autolab.models import StateError
from autolab.state import _resolve_repo_root
from autolab.todo_sync import select_decision_from_todo
from autolab.validators import _run_verification_step


def _handle_stage_failure(
    repo_root: Path,
    *,
    state_path: Path,
    state: dict[str, Any],
    stage_before: str,
    pre_sync_changed: list[Path],
    detail: str,
    verification: dict[str, Any] | None = None,
) -> RunOutcome:
    state["stage_attempt"] = int(state["stage_attempt"]) + 1
    exhausted = state["stage_attempt"] >= int(state["max_stage_attempts"])
    if exhausted:
        state["stage"] = "human_review"
        agent_status = "failed"
        message = f"{detail}; retry budget exhausted ({state['stage_attempt']}/{state['max_stage_attempts']}), escalating to human_review"
    else:
        agent_status = "needs_retry"
        message = (
            f"{detail}; retrying stage {stage_before} "
            f"({state['stage_attempt']}/{state['max_stage_attempts']})"
        )
    _append_state_history(
        state,
        stage_before=stage_before,
        stage_after=str(state.get("stage", stage_before)),
        status="failed",
        summary=message,
        verification=verification,
    )
    _write_json(state_path, state)
    changed = [state_path]
    outcome = RunOutcome(
        exit_code=1,
        transitioned=state["stage"] != stage_before,
        stage_before=stage_before,
        stage_after=state["stage"],
        message=message,
    )
    post_sync_changed, post_sync_message = _safe_todo_post_sync(
        repo_root,
        state,
        run_outcome=_outcome_payload(outcome),
    )
    summary_with_todo = _append_todo_message(message, post_sync_message)
    _persist_agent_result(
        repo_root,
        status=agent_status,
        summary=summary_with_todo,
        changed_files=[*changed, *pre_sync_changed, *post_sync_changed],
    )
    _append_log(repo_root, f"run failure at {stage_before}: {message}")
    return RunOutcome(
        exit_code=outcome.exit_code,
        transitioned=outcome.transitioned,
        stage_before=outcome.stage_before,
        stage_after=outcome.stage_after,
        message=summary_with_todo,
    )


def _run_once_standard(
    state_path: Path,
    decision: str | None,
    *,
    run_agent_mode: str = "policy",
    verify_before_evaluate: bool = False,
    auto_decision: bool = False,
    auto_mode: bool = False,
    commit_task_id: str = "",
    commit_cycle_stage: str = "",
    strict_implementation_progress: bool = True,
) -> RunOutcome:
    repo_root = _resolve_repo_root(state_path)
    pre_sync_changed: list[Path] = []
    state_bootstrap_changed: list[Path] = []
    detected_host_mode: str | None = None
    experiment_id_autofill_reason = ""
    try:
        raw_state = _load_state(state_path)
        state = _normalize_state(raw_state)
    except StateError as exc:
        message = f"invalid state: {exc}"
        pre_sync_changed, _ = _safe_todo_pre_sync(repo_root, None)
        post_sync_changed, post_sync_message = _safe_todo_post_sync(repo_root, None, run_outcome=None)
        summary = _append_todo_message(message, post_sync_message)
        _append_log(repo_root, f"run error: {message}")
        try:
            _persist_agent_result(
                repo_root,
                status="failed",
                summary=summary,
                changed_files=[*pre_sync_changed, *post_sync_changed],
            )
        except Exception:
            pass
        return RunOutcome(
            exit_code=1,
            transitioned=False,
            stage_before="<unknown>",
            stage_after="<unknown>",
            message=summary,
        )

    if not str(state.get("experiment_id", "")).strip():
        inferred_experiment_id, infer_reason = _infer_unique_experiment_id_from_backlog(
            repo_root,
            str(state.get("iteration_id", "")).strip(),
        )
        if inferred_experiment_id:
            state["experiment_id"] = inferred_experiment_id
            _write_json(state_path, state)
            state_bootstrap_changed.append(state_path)
            _append_log(repo_root, f"state.experiment_id auto-filled from backlog: {inferred_experiment_id}")
        else:
            experiment_id_autofill_reason = infer_reason

    detected_host_mode = _detect_priority_host_mode()
    active_completed, completion_summary = _is_active_experiment_completed(
        repo_root,
        state,
    )
    if active_completed and state["stage"] not in TERMINAL_STAGES:
        original_stage = state["stage"]
        state["stage"] = "stop"
        state["stage_attempt"] = 0
        state["current_task_id"] = ""
        state["task_cycle_stage"] = "done"
        state["task_change_baseline"] = {}
        _append_state_history(
            state,
            stage_before=original_stage,
            stage_after="stop",
            status="complete",
            summary=f"blocked completed experiment edits: {completion_summary}; re-open experiment in backlog to resume",
        )
        _write_json(state_path, state)
        state_bootstrap_changed.append(state_path)
        pre_sync_changed, _ = _safe_todo_pre_sync(repo_root, state, host_mode=detected_host_mode)
        if state_bootstrap_changed:
            pre_sync_changed = [*state_bootstrap_changed, *pre_sync_changed]
        message = f"blocked completed experiment edits: {completion_summary}; re-open experiment in backlog to resume"
        outcome = RunOutcome(
            exit_code=0,
            transitioned=True,
            stage_before=original_stage,
            stage_after="stop",
            message=message,
        )
        post_sync_changed, post_sync_message = _safe_todo_post_sync(
            repo_root,
            state,
            run_outcome=_outcome_payload(outcome),
        )
        summary = _append_todo_message(message, post_sync_message)
        _persist_agent_result(
            repo_root,
            status="complete",
            summary=summary,
            changed_files=[*pre_sync_changed, *post_sync_changed],
        )
        _append_log(repo_root, f"run blocked completed experiment at stage {original_stage}")
        return RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=summary,
        )

    pre_sync_changed, _ = _safe_todo_pre_sync(repo_root, state, host_mode=detected_host_mode)
    if state_bootstrap_changed:
        pre_sync_changed = [*state_bootstrap_changed, *pre_sync_changed]
    standard_baseline_snapshot = _collect_change_snapshot(repo_root)

    stage_before = state["stage"]
    verification_summary: dict[str, Any] | None = None
    if stage_before in TERMINAL_STAGES:
        message = f"stage '{stage_before}' is terminal; nothing to do"
        _append_state_history(
            state,
            stage_before=stage_before,
            stage_after=stage_before,
            status="noop",
            summary=message,
        )
        _write_json(state_path, state)
        outcome = RunOutcome(
            exit_code=0,
            transitioned=False,
            stage_before=stage_before,
            stage_after=stage_before,
            message=message,
        )
        post_sync_changed, post_sync_message = _safe_todo_post_sync(
            repo_root,
            state,
            run_outcome=_outcome_payload(outcome),
        )
        summary = _append_todo_message(message, post_sync_message)
        _persist_agent_result(
            repo_root,
            status="complete",
            summary=summary,
            changed_files=[state_path, *pre_sync_changed, *post_sync_changed],
        )
        _append_log(repo_root, f"run no-op at terminal stage {stage_before}")
        return RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=summary,
        )

    if stage_before == "decide_repeat":
        selected_decision = decision
        if selected_decision is None and auto_decision:
            selected_decision = select_decision_from_todo(
                repo_root,
                prioritize_implementation=(detected_host_mode == "local"),
            )
        if selected_decision is None and auto_decision and auto_mode:
            selected_decision = "stop"
        auto_selected = decision is None and selected_decision is not None

        if selected_decision is None:
            message = "stage 'decide_repeat' requires --decision (or --auto-decision) to transition. Rerun with --decision=<hypothesis|design|stop|human_review> or enable --auto-decision."
            _append_state_history(
                state,
                stage_before=stage_before,
                stage_after=stage_before,
                status="blocked",
                summary=message,
            )
            _write_json(state_path, state)
            outcome = RunOutcome(
                exit_code=0,
                transitioned=False,
                stage_before=stage_before,
                stage_after=stage_before,
                message=message,
            )
            post_sync_changed, post_sync_message = _safe_todo_post_sync(
                repo_root,
                state,
                run_outcome=_outcome_payload(outcome),
            )
            summary = _append_todo_message(message, post_sync_message)
            _persist_agent_result(
                repo_root,
                status="complete",
                summary=summary,
                changed_files=[state_path, *pre_sync_changed, *post_sync_changed],
            )
            _append_log(repo_root, "run paused at decide_repeat (no decision)")
            return RunOutcome(
                exit_code=outcome.exit_code,
                transitioned=outcome.transitioned,
                stage_before=outcome.stage_before,
                stage_after=outcome.stage_after,
                message=summary,
            )

        guardrails = _load_guardrail_config(repo_root)
        repeat_guard = state.get("repeat_guard", {})
        open_count = _todo_open_count(repo_root)
        last_decision = str(repeat_guard.get("last_decision", ""))
        same_decision_streak = int(repeat_guard.get("same_decision_streak", 0))
        no_progress_decisions = int(repeat_guard.get("no_progress_decisions", 0))
        last_open_task_count = int(repeat_guard.get("last_open_task_count", -1))

        if auto_mode:
            if selected_decision == last_decision:
                same_decision_streak += 1
            else:
                same_decision_streak = 1
            if last_open_task_count >= 0 and open_count >= last_open_task_count:
                no_progress_decisions += 1
            else:
                no_progress_decisions = 0
            if (
                same_decision_streak > guardrails.max_same_decision_streak
                or no_progress_decisions >= guardrails.max_no_progress_decisions
            ):
                selected_decision = guardrails.on_breach
                same_decision_streak = 0
                no_progress_decisions = 0

        repeat_guard["last_decision"] = selected_decision
        repeat_guard["same_decision_streak"] = same_decision_streak
        repeat_guard["last_open_task_count"] = open_count
        repeat_guard["no_progress_decisions"] = no_progress_decisions
        if selected_decision not in TERMINAL_STAGES:
            repeat_guard["update_docs_cycle_count"] = 0
        state["repeat_guard"] = repeat_guard
        state["stage"] = selected_decision
        state["stage_attempt"] = 0
        _append_state_history(
            state,
            stage_before=stage_before,
            stage_after=selected_decision,
            status="complete",
            summary=f"decision applied: decide_repeat -> {selected_decision}",
            decision=selected_decision,
        )
        _write_json(state_path, state)
        message = f"decision applied: decide_repeat -> {selected_decision}"
        if auto_selected:
            message = f"{message} (auto-selected from docs/todo.md)"
        if selected_decision == "hypothesis":
            message = f"{message} (note: reusing current iteration directory; prior hypothesis.md will be overwritten)"
        changed = [state_path]
        if selected_decision == "stop":
            completed, backlog_path, completion_summary = _mark_backlog_experiment_completed(
                repo_root,
                str(state.get("experiment_id", "")).strip(),
            )
            if completed and backlog_path is not None:
                changed.append(backlog_path)
                _append_log(repo_root, completion_summary)
            else:
                if (
                    not str(state.get("experiment_id", "")).strip()
                    and experiment_id_autofill_reason
                ):
                    completion_summary = (
                        f"state.experiment_id is unset ({experiment_id_autofill_reason})"
                    )
                completion_summary = f"backlog completion skipped: {completion_summary}"
                _append_log(repo_root, completion_summary)
            message = f"{message}; {completion_summary}"
        outcome = RunOutcome(
            exit_code=0,
            transitioned=True,
            stage_before=stage_before,
            stage_after=selected_decision,
            message=message,
        )
        post_sync_changed, post_sync_message = _safe_todo_post_sync(
            repo_root,
            state,
            run_outcome=_outcome_payload(outcome),
        )
        summary = _append_todo_message(message, post_sync_message)
        _persist_agent_result(
            repo_root,
            status="complete",
            summary=summary,
            changed_files=[*changed, *pre_sync_changed, *post_sync_changed],
        )
        _append_log(repo_root, f"run transition {stage_before} -> {selected_decision}")
        return RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=summary,
            commit_task_id=commit_task_id,
            commit_cycle_stage=commit_cycle_stage,
        )

    if _resolve_run_agent_mode(run_agent_mode) != "force_off":
        open_todo_count = _todo_open_count(repo_root)
        if open_todo_count > 0 and not _has_open_stage_todo_task(repo_root, stage_before):
            _append_log(repo_root, f"agent runner skipped stage={stage_before} (no stage-focused todo tasks)")
        else:
            try:
                _invoke_agent_runner(
                    repo_root,
                    state_path=state_path,
                    stage=stage_before,
                    iteration_id=str(state["iteration_id"]),
                    run_agent_mode=run_agent_mode,
                )
            except StageCheckError as exc:
                return _handle_stage_failure(
                    repo_root,
                    state_path=state_path,
                    state=state,
                    stage_before=stage_before,
                    pre_sync_changed=pre_sync_changed,
                    detail=f"agent runner error: {exc}",
                )

    if auto_mode or verify_before_evaluate:
        verified, verify_message = _run_verification_step(repo_root, state)
        verification_summary = {
            "passed": bool(verified),
            "message": verify_message,
            "mode": "auto" if auto_mode else "manual",
        }
        repeat_guard = state.get("repeat_guard", {})
        if not isinstance(repeat_guard, dict):
            repeat_guard = {}
        repeat_guard["last_verification_passed"] = bool(verified)
        state["repeat_guard"] = repeat_guard
        if not verified:
            return _handle_stage_failure(
                repo_root,
                state_path=state_path,
                state=state,
                stage_before=stage_before,
                pre_sync_changed=pre_sync_changed,
                detail=verify_message,
                verification=verification_summary,
            )
        if auto_mode:
            _append_log(repo_root, f"auto verification passed stage={stage_before}: {verify_message}")
        else:
            _append_log(repo_root, f"pre-evaluate verification passed stage={stage_before}: {verify_message}")

    try:
        eval_result = _evaluate_stage(repo_root, state)
        next_stage = eval_result.next_stage
        agent_status = eval_result.status
        summary = eval_result.summary
    except StageCheckError as exc:
        return _handle_stage_failure(
            repo_root,
            state_path=state_path,
            state=state,
            stage_before=stage_before,
            pre_sync_changed=pre_sync_changed,
            detail=str(exc),
        )

    if (
        strict_implementation_progress
        and stage_before == "implementation"
        and next_stage == "implementation_review"
    ):
        meaningful_config = _load_meaningful_change_config(repo_root)
        if not meaningful_config.require_implementation_progress:
            _append_log(
                repo_root,
                "implementation progress check skipped: require_implementation_progress=false",
            )
        else:
            non_git_required = bool(
                meaningful_config.require_git_for_progress and not _is_git_worktree(repo_root)
            )
            if non_git_required:
                if meaningful_config.on_non_git_behavior == "fail":
                    _append_log(
                        repo_root,
                        "implementation progress check failed: git worktree required but unavailable",
                    )
                    return _handle_stage_failure(
                        repo_root,
                        state_path=state_path,
                        state=state,
                        stage_before=stage_before,
                        pre_sync_changed=pre_sync_changed,
                        detail=(
                            "implementation progress check requires a git worktree; "
                            "set meaningful_change.require_git_for_progress=false to continue"
                        ),
                    )
                skip_message = (
                    "implementation progress check skipped: repository is not a git worktree; "
                    "continuing under policy"
                )
                _append_log(repo_root, skip_message)
                summary = f"{summary}; {skip_message}"
            else:
                implementation_progress, delta_paths, meaningful_paths, _current_snapshot = _evaluate_meaningful_change(
                    repo_root,
                    meaningful_config,
                    baseline_snapshot=standard_baseline_snapshot,
                )
                if not implementation_progress:
                    detail = (
                        "implementation produced no meaningful target changes beyond excluded paths "
                        f"({_meaningful_progress_detail(changed_paths=delta_paths, meaningful_paths=meaningful_paths)})"
                    )
                    _append_log(repo_root, f"implementation progress check failed: {detail}")
                    return _handle_stage_failure(
                        repo_root,
                        state_path=state_path,
                        state=state,
                        stage_before=stage_before,
                        pre_sync_changed=pre_sync_changed,
                        detail=detail,
                    )

    guardrail_stage_override = False
    if stage_before == "extract_results" and next_stage == "update_docs":
        guardrails = _load_guardrail_config(repo_root)
        repeat_guard = state.get("repeat_guard", {})
        if not isinstance(repeat_guard, dict):
            repeat_guard = {}
        update_docs_cycle_count = int(repeat_guard.get("update_docs_cycle_count", 0)) + 1
        repeat_guard["update_docs_cycle_count"] = update_docs_cycle_count
        state["repeat_guard"] = repeat_guard
        if update_docs_cycle_count > int(guardrails.max_update_docs_cycles):
            guardrail_stage_override = True
            state["stage"] = guardrails.on_breach
            state["stage_attempt"] = 0
            agent_status = "failed" if guardrails.on_breach == "human_review" else "complete"
            summary = (
                f"update_docs cycle limit exceeded ({update_docs_cycle_count}/{guardrails.max_update_docs_cycles}) "
                f"— escalating to '{guardrails.on_breach}'."
            )

    if not guardrail_stage_override:
        state["stage"] = next_stage
        prior_attempt = int(state["stage_attempt"])
        max_stage_attempts = int(state["max_stage_attempts"])
        retry_cycle_increment = (
            stage_before == "implementation_review"
            and next_stage == "implementation"
            and eval_result.needs_retry
        )
        retry_cycle_carry = (
            stage_before == "implementation"
            and next_stage == "implementation_review"
            and prior_attempt > 0
        )

        if retry_cycle_increment:
            state["stage_attempt"] = prior_attempt + 1
            if state["stage_attempt"] >= max_stage_attempts:
                state["stage"] = "human_review"
                agent_status = "failed"
                summary = (
                    f"implementation review retry budget exhausted "
                    f"({state['stage_attempt']}/{max_stage_attempts}) "
                    f"— handing off to human review"
                )
        elif retry_cycle_carry:
            state["stage_attempt"] = prior_attempt
        else:
            state["stage_attempt"] = 0

    _append_state_history(
        state,
        stage_before=stage_before,
        stage_after=str(state.get("stage", next_stage)),
        status=agent_status,
        summary=summary,
        verification=verification_summary,
    )
    _write_json(state_path, state)
    changed = [state_path]
    exit_code = 1 if agent_status == "failed" else 0
    stage_after = str(state["stage"])
    outcome = RunOutcome(
        exit_code=exit_code,
        transitioned=stage_after != stage_before,
        stage_before=stage_before,
        stage_after=stage_after,
        message=summary,
    )
    post_sync_changed, post_sync_message = _safe_todo_post_sync(
        repo_root,
        state,
        run_outcome=_outcome_payload(outcome),
    )
    summary_with_todo = _append_todo_message(summary, post_sync_message)
    _persist_agent_result(
        repo_root,
        status=agent_status,
        summary=summary_with_todo,
        changed_files=[*changed, *pre_sync_changed, *post_sync_changed],
    )
    _append_log(repo_root, f"run transition {stage_before} -> {stage_after} ({agent_status})")

    return RunOutcome(
        exit_code=outcome.exit_code,
        transitioned=outcome.transitioned,
        stage_before=outcome.stage_before,
        stage_after=outcome.stage_after,
        message=summary_with_todo,
        commit_task_id=commit_task_id,
        commit_cycle_stage=commit_cycle_stage,
    )
