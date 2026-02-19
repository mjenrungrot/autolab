from __future__ import annotations
import json
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from autolab.constants import DECISION_STAGES, TERMINAL_STAGES
from autolab.models import EvalResult, RunOutcome, StageCheckError
from autolab.config import (
    _load_guardrail_config,
    _load_meaningful_change_config,
    _load_strict_mode_config,
    _load_verifier_policy,
    _resolve_run_agent_mode,
    _resolve_stage_max_retries,
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
    _resolve_iteration_directory,
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
    _utc_now,
    _generate_run_id,
    _write_block_reason,
    _write_guardrail_breach,
    _write_json,
)
from autolab.models import StateError
from autolab.state import _resolve_repo_root
from autolab.prompts import _suggest_decision_from_metrics
from autolab.todo_sync import select_decision_from_todo
from autolab.validators import _run_verification_step, _validate_stage_readiness


def _decision_from_artifact(
    repo_root: Path,
    state: dict[str, Any],
) -> tuple[str | None, str]:
    iteration_dir, _iteration_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=str(state.get("iteration_id", "")).strip(),
        experiment_id=str(state.get("experiment_id", "")).strip(),
        require_exists=False,
    )
    decision_path = iteration_dir / "decision_result.json"
    if not decision_path.exists():
        return (None, "")

    try:
        payload = json.loads(decision_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return (None, f"{decision_path} is not valid JSON: {exc}")
    if not isinstance(payload, dict):
        return (None, f"{decision_path} must contain a JSON object")

    decision = str(payload.get("decision", "")).strip()
    if decision not in DECISION_STAGES:
        return (
            None,
            (
                f"{decision_path} decision must be one of {list(DECISION_STAGES)}, "
                f"got '{decision or '<missing>'}'"
            ),
        )

    rationale = str(payload.get("rationale", "")).strip()
    if not rationale:
        return (None, f"{decision_path} must include a non-empty rationale")

    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or len(evidence) == 0:
        return (None, f"{decision_path} must include a non-empty 'evidence' list")
    for idx, item in enumerate(evidence):
        if not isinstance(item, dict):
            return (None, f"{decision_path} evidence[{idx}] must be a dict")
        for field in ("source", "pointer", "summary"):
            val = item.get(field)
            if not isinstance(val, str) or not val.strip():
                return (
                    None,
                    f"{decision_path} evidence[{idx}] must have a non-empty string '{field}'",
                )
    return (decision, "")


def _compute_next_stage_attempt(
    *,
    stage_before: str,
    next_stage: str,
    prior_attempt: int,
    max_stage_attempts: int,
    needs_retry: bool,
    stage_max_retries: int | None = None,
) -> tuple[int, str | None, str | None]:
    """Compute the stage_attempt value after a stage transition.

    Returns (new_attempt, override_stage, override_summary).
    override_stage/override_summary are set only when retry budget is exhausted.

    When *stage_max_retries* is provided it takes precedence over
    *max_stage_attempts* for the exhaustion check, enabling per-stage retry
    budgets while keeping backward compatibility with the global fallback.
    """
    effective_max = (
        stage_max_retries if stage_max_retries is not None else max_stage_attempts
    )

    retry_cycle_increment = (
        stage_before == "implementation_review"
        and next_stage == "implementation"
        and needs_retry
    )
    retry_cycle_carry = (
        stage_before == "implementation"
        and next_stage == "implementation_review"
        and prior_attempt > 0
    )

    if retry_cycle_increment:
        new_attempt = prior_attempt + 1
        if new_attempt >= effective_max:
            return (
                new_attempt,
                "human_review",
                f"implementation review retry budget exhausted ({new_attempt}/{effective_max})"
                " -- handing off to human review",
            )
        return (new_attempt, None, None)
    if retry_cycle_carry:
        return (prior_attempt, None, None)
    return (0, None, None)


def _augment_agent_runner_failure_detail(detail: str) -> str:
    normalized = str(detail).strip()
    if "modified protected file(s)" not in normalized:
        return normalized
    if "Remediation:" in normalized:
        return normalized
    return (
        f"{normalized}. Remediation: restore protected files and rerun with --no-run-agent "
        "or narrow agent_runner.stages/edit_scope for this stage."
    )


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
    # Resolve per-stage retry budget from policy, falling back to the global max.
    policy = _load_verifier_policy(repo_root)
    global_max = int(state["max_stage_attempts"])
    effective_max = _resolve_stage_max_retries(
        policy, stage_before, fallback=global_max
    )

    state["stage_attempt"] = int(state["stage_attempt"]) + 1
    exhausted = state["stage_attempt"] >= effective_max
    if exhausted:
        state["stage"] = "human_review"
        agent_status = "failed"
        message = f"{detail}; retry budget exhausted ({state['stage_attempt']}/{effective_max}), escalating to human_review"
        # Write escalation packet for diagnostics / human review.
        history = state.get("history", [])
        recent_history: list[str] = []
        if isinstance(history, list):
            for entry in history[-3:]:
                if isinstance(entry, dict):
                    recent_history.append(
                        f"{entry.get('stage_before', '?')} -> {entry.get('stage_after', '?')}: "
                        f"{entry.get('summary', '')}"
                    )
                else:
                    recent_history.append(str(entry))
        escalation_packet: dict[str, Any] = {
            "escalated_at": _utc_now(),
            "stage": stage_before,
            "stage_attempt": int(state["stage_attempt"]),
            "max_retries": effective_max,
            "last_failures": [detail],
            "history": recent_history,
        }
        try:
            _write_json(
                repo_root / ".autolab" / "escalation_packet.json", escalation_packet
            )
        except Exception:
            pass
    else:
        agent_status = "needs_retry"
        message = (
            f"{detail}; retrying stage {stage_before} "
            f"({state['stage_attempt']}/{effective_max})"
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


def _write_auto_decision_artifact(
    repo_root: Path,
    *,
    state: dict[str, Any],
    selected_decision: str,
    decision_source: str,
    auto_selected: bool,
    requested_decision: str | None,
    artifact_error: str,
    repeat_guard: dict[str, Any],
    metrics_evidence: dict[str, Any] | None = None,
) -> None:
    payload = {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "iteration_id": str(state.get("iteration_id", "")).strip(),
        "experiment_id": str(state.get("experiment_id", "")).strip(),
        "stage": "decide_repeat",
        "inputs": {
            "requested_decision": requested_decision,
            "artifact_error": artifact_error,
            "metrics_evidence": metrics_evidence or {},
        },
        "outputs": {
            "selected_decision": selected_decision,
            "decision_source": decision_source,
            "auto_selected": auto_selected,
            "guardrails": repeat_guard,
        },
    }
    _write_json(repo_root / ".autolab" / "auto_decision.json", payload)


def _read_design_replicate_count(repo_root: Path, state: dict[str, Any]) -> int:
    """Read replicates.count from design.yaml, returning 1 if absent."""
    if yaml is None:
        return 1
    iteration_id = str(state.get("iteration_id", "")).strip()
    experiment_id = str(state.get("experiment_id", "")).strip()
    if not iteration_id:
        return 1
    iteration_dir, _type = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        require_exists=False,
    )
    design_path = iteration_dir / "design.yaml"
    if not design_path.exists():
        return 1
    try:
        loaded = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    except Exception:
        return 1
    if not isinstance(loaded, dict):
        return 1
    replicates = loaded.get("replicates")
    if not isinstance(replicates, dict):
        return 1
    count = replicates.get("count")
    if not isinstance(count, int) or count < 1:
        return 1
    return count


def _prepare_launch_run_context(
    repo_root: Path,
    *,
    state: dict[str, Any],
    state_path: Path,
) -> Path:
    run_id = _generate_run_id()
    replicate_count = _read_design_replicate_count(repo_root, state)

    if replicate_count > 1:
        run_ids = [f"{run_id}_r{i}" for i in range(1, replicate_count + 1)]
        state["run_group"] = run_ids
        state["pending_run_id"] = run_id
        _write_json(state_path, state)

        context_path = repo_root / ".autolab" / "run_context.json"
        _write_json(
            context_path,
            {
                "schema_version": "1.0",
                "generated_at": _utc_now(),
                "iteration_id": str(state.get("iteration_id", "")).strip(),
                "experiment_id": str(state.get("experiment_id", "")).strip(),
                "stage": "launch",
                "run_id": run_id,
                "run_ids": run_ids,
                "replicate_count": replicate_count,
            },
        )
        _append_log(
            repo_root,
            f"launch multi-run prepared by orchestrator: {run_id} ({replicate_count} replicates)",
        )
        return context_path

    state["pending_run_id"] = run_id
    _write_json(state_path, state)

    context_path = repo_root / ".autolab" / "run_context.json"
    _write_json(
        context_path,
        {
            "schema_version": "1.0",
            "generated_at": _utc_now(),
            "iteration_id": str(state.get("iteration_id", "")).strip(),
            "experiment_id": str(state.get("experiment_id", "")).strip(),
            "stage": "launch",
            "run_id": run_id,
        },
    )
    _append_log(repo_root, f"launch run_id prepared by orchestrator: {run_id}")
    return context_path


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
        post_sync_changed, post_sync_message = _safe_todo_post_sync(
            repo_root, None, run_outcome=None
        )
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
            _append_log(
                repo_root,
                f"state.experiment_id auto-filled from backlog: {inferred_experiment_id}",
            )
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
        pre_sync_changed, _ = _safe_todo_pre_sync(
            repo_root, state, host_mode=detected_host_mode
        )
        if state_bootstrap_changed:
            pre_sync_changed = [*state_bootstrap_changed, *pre_sync_changed]
        message = f"blocked completed experiment edits: {completion_summary}; re-open experiment in backlog to resume"
        _write_block_reason(
            repo_root,
            reason=completion_summary,
            stage_at_block=original_stage,
            action_required="re-open experiment in backlog to resume",
        )
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
        _append_log(
            repo_root, f"run blocked completed experiment at stage {original_stage}"
        )
        return RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=summary,
        )

    pre_sync_changed, _ = _safe_todo_pre_sync(
        repo_root, state, host_mode=detected_host_mode
    )
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
        decision_source = "cli"
        artifact_decision_error = ""
        metrics_evidence: dict[str, Any] = {}
        if selected_decision is None:
            artifact_decision, artifact_decision_error = _decision_from_artifact(
                repo_root, state
            )
            if artifact_decision is not None:
                selected_decision = artifact_decision
                decision_source = "artifact"
        if selected_decision is None and auto_decision:
            selected_decision = select_decision_from_todo(
                repo_root,
                prioritize_implementation=(detected_host_mode == "local"),
            )
            if selected_decision is not None:
                decision_source = "auto_todo"
        if selected_decision is None and auto_decision:
            metrics_suggestion, _metrics_evidence = _suggest_decision_from_metrics(
                repo_root, state
            )
            if isinstance(_metrics_evidence, dict):
                metrics_evidence = _metrics_evidence
            if metrics_suggestion is not None:
                selected_decision = metrics_suggestion
                decision_source = "auto_metrics"
                _append_log(
                    repo_root,
                    f"decide_repeat auto_metrics suggestion: {metrics_suggestion}",
                )
        if selected_decision is None and auto_decision and auto_mode:
            selected_decision = "stop"
            decision_source = "auto_default"
        auto_selected = decision is None and decision_source in {
            "auto_todo",
            "auto_metrics",
            "auto_default",
        }

        # Item 6: strict mode overrides for unattended loops
        if auto_mode and selected_decision is not None:
            strict_config = _load_strict_mode_config(repo_root, auto_mode=auto_mode)
            if selected_decision == "stop" and strict_config.forbid_auto_stop:
                selected_decision = "human_review"
                decision_source = "strict_override"
                _append_log(
                    repo_root,
                    "strict_mode.forbid_auto_stop overrode 'stop' to 'human_review'",
                )
            elif (
                selected_decision == "stop"
                and strict_config.require_human_review_for_stop
            ):
                selected_decision = "human_review"
                decision_source = "strict_override"
                _append_log(
                    repo_root,
                    "strict_mode.require_human_review_for_stop overrode 'stop' to 'human_review'",
                )

        if selected_decision is None:
            message = (
                "stage 'decide_repeat' requires --decision "
                "(or decision_result.json or --auto-decision) to transition. "
                "Rerun with --decision=<hypothesis|design|stop|human_review> or enable --auto-decision."
            )
            if artifact_decision_error:
                message = (
                    f"{message} Invalid decision artifact: {artifact_decision_error}"
                )
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

        if selected_decision not in DECISION_STAGES:
            return _handle_stage_failure(
                repo_root,
                state_path=state_path,
                state=state,
                stage_before=stage_before,
                pre_sync_changed=pre_sync_changed,
                detail=(
                    f"decide_repeat decision '{selected_decision}' is invalid "
                    f"(expected one of {list(DECISION_STAGES)})"
                ),
                verification=verification_summary,
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

            last_change_baseline = repeat_guard.get("last_change_baseline")
            if not isinstance(last_change_baseline, dict):
                last_change_baseline = standard_baseline_snapshot
            meaningful_config = _load_meaningful_change_config(repo_root)
            meaningful_changed, _delta, _meaningful, current_snapshot = (
                _evaluate_meaningful_change(
                    repo_root,
                    meaningful_config,
                    baseline_snapshot=last_change_baseline,
                )
            )
            if meaningful_changed:
                no_progress_decisions = 0
            repeat_guard["last_change_baseline"] = current_snapshot

            if (
                same_decision_streak > guardrails.max_same_decision_streak
                or no_progress_decisions >= guardrails.max_no_progress_decisions
            ):
                selected_decision = guardrails.on_breach
                same_decision_streak = 0
                no_progress_decisions = 0
                _write_guardrail_breach(
                    repo_root,
                    rule="same_decision_streak"
                    if same_decision_streak > guardrails.max_same_decision_streak
                    else "no_progress",
                    counters={
                        "same_decision_streak": same_decision_streak,
                        "max_same_decision_streak": guardrails.max_same_decision_streak,
                        "no_progress_decisions": no_progress_decisions,
                        "max_no_progress_decisions": guardrails.max_no_progress_decisions,
                    },
                    stage="decide_repeat",
                    remediation=f"Escalated to '{guardrails.on_breach}'. Review experiment progress and consider manual intervention.",
                )

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
        try:
            _write_json(
                repo_root / ".autolab" / "decision_trace.json",
                {
                    "timestamp": _utc_now(),
                    "decision": selected_decision,
                    "decision_source": decision_source,
                    "auto_selected": auto_selected,
                    "iteration_id": str(state.get("iteration_id", "")).strip(),
                    "experiment_id": str(state.get("experiment_id", "")).strip(),
                    "repeat_guard": repeat_guard,
                },
            )
        except Exception:
            pass
        try:
            _write_auto_decision_artifact(
                repo_root,
                state=state,
                selected_decision=selected_decision,
                decision_source=decision_source,
                auto_selected=auto_selected,
                requested_decision=decision,
                artifact_error=artifact_decision_error,
                repeat_guard=repeat_guard,
                metrics_evidence=metrics_evidence,
            )
        except Exception:
            pass
        if auto_selected:
            try:
                _iter_dir, _iter_type = _resolve_iteration_directory(
                    repo_root,
                    iteration_id=str(state.get("iteration_id", "")).strip(),
                    experiment_id=str(state.get("experiment_id", "")).strip(),
                    require_exists=False,
                )
                _write_json(
                    _iter_dir / "decision_result.json",
                    {
                        "schema_version": "1.0",
                        "decision": selected_decision,
                        "rationale": f"Auto-selected via {decision_source}",
                        "evidence": [
                            {
                                "source": decision_source,
                                "pointer": str(
                                    repo_root / ".autolab" / "decision_trace.json"
                                ),
                                "summary": f"Decision '{selected_decision}' auto-selected by {decision_source} policy",
                            }
                        ],
                        "risks": [],
                    },
                )
            except Exception:
                pass
        message = f"decision applied: decide_repeat -> {selected_decision}"
        if auto_selected:
            _source_labels = {
                "auto_todo": "(auto-selected from docs/todo.md)",
                "auto_metrics": "(auto-selected from metrics comparison)",
                "auto_default": "(auto-selected: default stop)",
            }
            message = (
                f"{message} {_source_labels.get(decision_source, '(auto-selected)')}"
            )
        elif decision_source == "strict_override":
            message = f"{message} (overridden by strict_mode policy)"
        elif decision_source == "artifact":
            message = f"{message} (from decision_result.json)"
        if selected_decision == "hypothesis":
            message = f"{message} (note: reusing current iteration directory; prior hypothesis.md will be overwritten)"
        changed = [state_path]
        if selected_decision == "stop":
            completed, backlog_path, completion_summary = (
                _mark_backlog_experiment_completed(
                    repo_root,
                    str(state.get("experiment_id", "")).strip(),
                )
            )
            if completed and backlog_path is not None:
                changed.append(backlog_path)
                _append_log(repo_root, completion_summary)
            else:
                if (
                    not str(state.get("experiment_id", "")).strip()
                    and experiment_id_autofill_reason
                ):
                    completion_summary = f"state.experiment_id is unset ({experiment_id_autofill_reason})"
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

    if stage_before == "launch":
        try:
            run_context_path = _prepare_launch_run_context(
                repo_root,
                state=state,
                state_path=state_path,
            )
            pre_sync_changed.append(run_context_path)
            pre_sync_changed.append(state_path)
        except StageCheckError as exc:
            return _handle_stage_failure(
                repo_root,
                state_path=state_path,
                state=state,
                stage_before=stage_before,
                pre_sync_changed=pre_sync_changed,
                detail=f"launch run_id preparation failed: {exc}",
                verification=verification_summary,
            )

    try:
        ready, readiness_message, readiness_details = _validate_stage_readiness(
            repo_root, state
        )
    except StageCheckError as exc:
        return _handle_stage_failure(
            repo_root,
            state_path=state_path,
            state=state,
            stage_before=stage_before,
            pre_sync_changed=pre_sync_changed,
            detail=f"stage readiness failed: {exc}",
            verification=verification_summary,
        )

    if not ready:
        details_json = json.dumps(readiness_details, sort_keys=True)
        return _handle_stage_failure(
            repo_root,
            state_path=state_path,
            state=state,
            stage_before=stage_before,
            pre_sync_changed=pre_sync_changed,
            detail=f"{readiness_message}; details={details_json}",
            verification=verification_summary,
        )
    _append_log(repo_root, f"stage readiness passed stage={stage_before}")

    if _resolve_run_agent_mode(run_agent_mode) != "force_off":
        open_todo_count = _todo_open_count(repo_root)
        if open_todo_count > 0 and not _has_open_stage_todo_task(
            repo_root, stage_before
        ):
            _append_log(
                repo_root,
                f"agent runner skipped stage={stage_before} (no stage-focused todo tasks)",
            )
        else:
            try:
                _invoke_agent_runner(
                    repo_root,
                    state_path=state_path,
                    stage=stage_before,
                    iteration_id=str(state["iteration_id"]),
                    run_agent_mode=run_agent_mode,
                    auto_mode=auto_mode,
                )
            except StageCheckError as exc:
                detail = _augment_agent_runner_failure_detail(str(exc))
                return _handle_stage_failure(
                    repo_root,
                    state_path=state_path,
                    state=state,
                    stage_before=stage_before,
                    pre_sync_changed=pre_sync_changed,
                    detail=f"agent runner error: {detail}",
                )

    if auto_mode or verify_before_evaluate:
        verified, verify_message = _run_verification_step(
            repo_root, state, auto_mode=auto_mode
        )
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
            _append_log(
                repo_root,
                f"auto verification passed stage={stage_before}: {verify_message}",
            )
        else:
            _append_log(
                repo_root,
                f"pre-evaluate verification passed stage={stage_before}: {verify_message}",
            )

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
                meaningful_config.require_git_for_progress
                and not _is_git_worktree(repo_root)
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
                (
                    implementation_progress,
                    delta_paths,
                    meaningful_paths,
                    _current_snapshot,
                ) = _evaluate_meaningful_change(
                    repo_root,
                    meaningful_config,
                    baseline_snapshot=standard_baseline_snapshot,
                )
                if not implementation_progress:
                    detail = (
                        "implementation produced no meaningful target changes beyond excluded paths "
                        f"({_meaningful_progress_detail(changed_paths=delta_paths, meaningful_paths=meaningful_paths)})"
                    )
                    _append_log(
                        repo_root, f"implementation progress check failed: {detail}"
                    )
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
        update_docs_cycle_count = (
            int(repeat_guard.get("update_docs_cycle_count", 0)) + 1
        )
        repeat_guard["update_docs_cycle_count"] = update_docs_cycle_count
        state["repeat_guard"] = repeat_guard
        if update_docs_cycle_count > int(guardrails.max_update_docs_cycles):
            guardrail_stage_override = True
            state["stage"] = guardrails.on_breach
            state["stage_attempt"] = 0
            agent_status = (
                "failed" if guardrails.on_breach == "human_review" else "complete"
            )
            summary = (
                f"update_docs cycle limit exceeded ({update_docs_cycle_count}/{guardrails.max_update_docs_cycles}) "
                f"— escalating to '{guardrails.on_breach}'."
            )
            _write_guardrail_breach(
                repo_root,
                rule="update_docs_cycle",
                counters={
                    "update_docs_cycle_count": update_docs_cycle_count,
                    "max_update_docs_cycles": int(guardrails.max_update_docs_cycles),
                },
                stage="extract_results",
                remediation=f"Escalated to '{guardrails.on_breach}'. The extract_results -> update_docs cycle has repeated too many times.",
            )

    if not guardrail_stage_override:
        state["stage"] = next_stage
        # Resolve per-stage retry budget for the implementation review cycle.
        _transition_policy = _load_verifier_policy(repo_root)
        _transition_stage_max = _resolve_stage_max_retries(
            _transition_policy,
            next_stage,
            fallback=int(state["max_stage_attempts"]),
        )
        new_attempt, override_stage, override_summary = _compute_next_stage_attempt(
            stage_before=stage_before,
            next_stage=next_stage,
            prior_attempt=int(state["stage_attempt"]),
            max_stage_attempts=int(state["max_stage_attempts"]),
            needs_retry=eval_result.needs_retry,
            stage_max_retries=_transition_stage_max,
        )
        state["stage_attempt"] = new_attempt
        if override_stage is not None:
            state["stage"] = override_stage
            agent_status = "failed"
            summary = override_summary or summary

    if stage_before == "launch" and str(state.get("stage", "")) != "launch":
        state["pending_run_id"] = ""

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
    _append_log(
        repo_root, f"run transition {stage_before} -> {stage_after} ({agent_status})"
    )

    return RunOutcome(
        exit_code=outcome.exit_code,
        transitioned=outcome.transitioned,
        stage_before=outcome.stage_before,
        stage_after=outcome.stage_after,
        message=summary_with_todo,
        commit_task_id=commit_task_id,
        commit_cycle_stage=commit_cycle_stage,
    )
