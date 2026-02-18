from __future__ import annotations
import json
import subprocess
from pathlib import Path
from typing import Any

from autolab.constants import ACTIVE_STAGES, TERMINAL_STAGES
from autolab.models import RunOutcome, StateError
from autolab.config import (
    _load_assistant_auto_complete_policy,
    _load_guardrail_config,
    _load_meaningful_change_config,
)
from autolab.run_standard import _run_once_standard
from autolab.state import (
    _append_state_history,
    _is_active_experiment_completed,
    _load_state,
    _mark_backlog_experiment_completed,
    _normalize_state,
    _resolve_repo_root,
)
from autolab.utils import (
    _append_log,
    _append_todo_message,
    _assistant_commit_paths,
    _collect_change_snapshot,
    _detect_priority_host_mode,
    _evaluate_meaningful_change,
    _outcome_payload,
    _persist_agent_result,
    _safe_todo_post_sync,
    _safe_todo_pre_sync,
    _utc_now,
    _write_block_reason,
    _write_guardrail_breach,
    _write_json,
)
from autolab.validators import _run_verification_step
from autolab.todo_sync import mark_task_completed, select_open_task


def _append_task_ledger(
    repo_root: Path,
    *,
    event: str,
    task_id: str,
    stage_before: str,
    stage_after: str,
    transitioned: bool,
    status: str,
    exit_code: int,
    message: str,
    verification_passed: bool | None = None,
    verification_message: str = "",
    commit_allowed: bool | None = None,
    commit_reason: str = "",
    changed_files_summary: list[str] | None = None,
    meaningful_files_summary: list[str] | None = None,
) -> None:
    entry: dict[str, Any] = {
        "timestamp": _utc_now(),
        "event": str(event).strip(),
        "task_id": str(task_id).strip(),
        "stage_before": str(stage_before).strip(),
        "stage_after": str(stage_after).strip(),
        "transitioned": bool(transitioned),
        "status": str(status).strip(),
        "exit_code": int(exit_code),
        "message": str(message).strip(),
    }
    if verification_passed is not None:
        entry["verification"] = {
            "passed": bool(verification_passed),
            "message": str(verification_message).strip(),
        }
    if commit_allowed is not None:
        entry["commit_decision"] = {
            "allowed": bool(commit_allowed),
            "reason": str(commit_reason).strip(),
        }
    if changed_files_summary is not None:
        entry["changed_files"] = changed_files_summary
    if meaningful_files_summary is not None:
        entry["meaningful_files"] = meaningful_files_summary

    ledger_path = repo_root / ".autolab" / "task_history.jsonl"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _assistant_target_stage(task: dict[str, Any]) -> str:
    stage = str(task.get("stage", "")).strip()
    task_class = str(task.get("task_class", "unknown")).strip().lower()
    if stage in ACTIVE_STAGES:
        return stage
    if task_class == "docs":
        return "update_docs"
    if task_class == "experiment":
        return "design"
    return "implementation"


def _run_once_assistant(
    state_path: Path,
    *,
    run_agent_mode: str = "policy",
    auto_mode: bool = False,
) -> RunOutcome:
    repo_root = _resolve_repo_root(state_path)
    pre_sync_changed: list[Path] = []
    detected_host_mode = _detect_priority_host_mode()
    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        return _run_once_standard(
            state_path,
            None,
            run_agent_mode=run_agent_mode,
            auto_decision=False,
            auto_mode=auto_mode,
            strict_implementation_progress=False,
        )

    state["assistant_mode"] = "on"
    current_stage = str(state.get("stage", ""))
    completed_experiment, completion_summary = _is_active_experiment_completed(
        repo_root,
        state,
    )
    if completed_experiment and current_stage not in TERMINAL_STAGES:
        state["stage"] = "stop"
        state["current_task_id"] = ""
        state["task_cycle_stage"] = "done"
        state["task_change_baseline"] = {}
        state["stage_attempt"] = 0
        _write_json(state_path, state)
        pre_sync_changed, _ = _safe_todo_pre_sync(repo_root, state, host_mode=detected_host_mode)
        _write_block_reason(
            repo_root,
            reason=completion_summary,
            stage_at_block=current_stage,
            action_required="re-open experiment in backlog to resume",
        )
        message = f"blocked completed experiment edits: {completion_summary}; re-open experiment in backlog to resume"
        outcome = RunOutcome(
            exit_code=0,
            transitioned=True,
            stage_before=current_stage,
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
            changed_files=[state_path, *pre_sync_changed, *post_sync_changed],
        )
        try:
            _append_task_ledger(
                repo_root,
                event="blocked_completed",
                task_id="",
                stage_before=current_stage,
                stage_after="stop",
                transitioned=True,
                status="complete",
                exit_code=0,
                message=summary,
                commit_allowed=False,
                commit_reason="completed backlog experiment",
            )
        except Exception as exc:
            _append_log(repo_root, f"assistant task ledger write failed: {exc}")
        _append_log(repo_root, f"assistant blocked completed experiment from stage {current_stage}")
        return RunOutcome(
            exit_code=0,
            transitioned=True,
            stage_before=current_stage,
            stage_after="stop",
            message=summary,
            commit_task_id="",
            commit_cycle_stage="done",
        )

    pre_sync_changed, _ = _safe_todo_pre_sync(repo_root, state, host_mode=detected_host_mode)
    stage_before = str(state.get("stage", ""))
    current_task_id = str(state.get("current_task_id", ""))
    cycle_stage = str(state.get("task_cycle_stage", "select"))
    force_task_selection = stage_before == "human_review"
    baseline_snapshot_raw = state.get("task_change_baseline", {})
    baseline_snapshot = baseline_snapshot_raw if isinstance(baseline_snapshot_raw, dict) else {}

    def _persist_simple(
        *,
        status: str,
        message: str,
        changed_files: list[Path],
        transitioned: bool,
        stage_after: str,
        exit_code: int = 0,
        commit_allowed: bool = False,
        commit_cycle_stage: str = "",
        commit_paths: tuple[str, ...] = (),
        verification_passed: bool | None = None,
        verification_message: str = "",
        commit_reason: str = "",
        ledger_event: str = "",
        changed_files_summary: list[str] | None = None,
        meaningful_files_summary: list[str] | None = None,
    ) -> RunOutcome:
        _append_state_history(
            state,
            stage_before=stage_before,
            stage_after=stage_after,
            status=status,
            summary=message,
        )
        _write_json(state_path, state)
        post_sync_changed, post_sync_message = _safe_todo_post_sync(
            repo_root,
            state,
            run_outcome={
                "exit_code": exit_code,
                "transitioned": transitioned,
                "stage_before": stage_before,
                "stage_after": stage_after,
            },
        )
        summary = _append_todo_message(message, post_sync_message)
        _persist_agent_result(
            repo_root,
            status=status,
            summary=summary,
            changed_files=[state_path, *changed_files, *pre_sync_changed, *post_sync_changed],
        )
        try:
            _append_task_ledger(
                repo_root,
                event=ledger_event or commit_cycle_stage or cycle_stage or "assistant",
                task_id=current_task_id,
                stage_before=stage_before,
                stage_after=stage_after,
                transitioned=transitioned,
                status=status,
                exit_code=exit_code,
                message=summary,
                verification_passed=verification_passed,
                verification_message=verification_message,
                commit_allowed=commit_allowed,
                commit_reason=commit_reason,
                changed_files_summary=changed_files_summary,
                meaningful_files_summary=meaningful_files_summary,
            )
        except Exception as exc:
            _append_log(repo_root, f"assistant task ledger write failed: {exc}")
        return RunOutcome(
            exit_code=exit_code,
            transitioned=transitioned,
            stage_before=stage_before,
            stage_after=stage_after,
            message=summary,
            commit_allowed=commit_allowed,
            commit_task_id=current_task_id,
            commit_cycle_stage=commit_cycle_stage,
            commit_paths=commit_paths,
        )

    if force_task_selection or cycle_stage in {"select", "done"} or not current_task_id:
        task = select_open_task(
            repo_root,
            prioritize_implementation=(detected_host_mode == "local"),
        )
        if task is None:
            auto_complete = _load_assistant_auto_complete_policy(repo_root)
            if not auto_complete:
                state["current_task_id"] = ""
                state["task_cycle_stage"] = "done"
                state["task_change_baseline"] = {}
                state["stage"] = "human_review"
                state["stage_attempt"] = 0
                _write_json(state_path, state)
                return _persist_simple(
                    status="complete",
                    message="assistant cycle: no actionable tasks remain; escalating to human_review (auto_complete_backlog=false)",
                    changed_files=[state_path],
                    transitioned=stage_before != "human_review",
                    stage_after="human_review",
                    commit_allowed=False,
                    commit_cycle_stage="done",
                    commit_reason="auto_complete_backlog policy is false",
                    ledger_event="done",
                )
            state["current_task_id"] = ""
            state["task_cycle_stage"] = "done"
            state["task_change_baseline"] = {}
            state["stage"] = "stop"
            state["stage_attempt"] = 0
            _write_json(state_path, state)
            changed: list[Path] = [state_path]
            completion_msg = ""
            completed, backlog_path, completion_summary = _mark_backlog_experiment_completed(
                repo_root,
                str(state.get("experiment_id", "")).strip(),
            )
            if completed and backlog_path is not None:
                changed.append(backlog_path)
                completion_msg = f"; {completion_summary}"
            _append_log(repo_root, completion_summary)
            return _persist_simple(
                status="complete",
                message=f"assistant cycle complete: no actionable tasks remain{completion_msg}",
                changed_files=changed,
                transitioned=stage_before != "stop",
                stage_after="stop",
                commit_allowed=False,
                commit_cycle_stage="done",
                commit_reason="no actionable tasks remained",
                ledger_event="done",
            )

        current_task_id = str(task.get("task_id", "")).strip()
        state["current_task_id"] = current_task_id
        state["task_cycle_stage"] = "implement"
        state["task_change_baseline"] = _collect_change_snapshot(repo_root)
        target_stage = _assistant_target_stage(task)
        state["stage"] = target_stage
        state["stage_attempt"] = 0
        _write_json(state_path, state)
        return _persist_simple(
            status="complete",
            message=f"assistant selected task {current_task_id} ({task.get('task_class', 'unknown')}) -> {target_stage}",
            changed_files=[state_path],
            transitioned=target_stage != stage_before,
            stage_after=target_stage,
            commit_allowed=False,
            commit_cycle_stage="select",
            commit_reason="task selected for implementation cycle",
            ledger_event="select",
        )

    if cycle_stage == "verify":
        verified, verify_message = _run_verification_step(repo_root, state, auto_mode=auto_mode)
        repeat_guard = dict(state.get("repeat_guard", {}))
        repeat_guard["last_verification_passed"] = verified
        state["repeat_guard"] = repeat_guard
        state["task_cycle_stage"] = "review" if verified else "implement"
        _write_json(state_path, state)
        return _persist_simple(
            status="complete" if verified else "needs_retry",
            message=f"assistant verification: {verify_message}",
            changed_files=[state_path],
            transitioned=False,
            stage_after=str(state.get("stage", stage_before)),
            exit_code=0,
            commit_allowed=False,
            commit_cycle_stage="verify",
            verification_passed=verified,
            verification_message=verify_message,
            commit_reason="verification gate",
            ledger_event="verify",
        )

    if cycle_stage == "review":
        meaningful_config = _load_meaningful_change_config(repo_root)
        meaningful, changed_paths, meaningful_paths, _current_snapshot = _evaluate_meaningful_change(
            repo_root,
            meaningful_config,
            baseline_snapshot=baseline_snapshot,
        )
        repeat_guard = dict(state.get("repeat_guard", {}))
        verification_passed = bool(repeat_guard.get("last_verification_passed", False))
        passes_gate = meaningful and (not meaningful_config.require_verification or verification_passed)

        if passes_gate:
            mark_task_completed(repo_root, current_task_id)
            # Persist task completion evidence
            try:
                completions_dir = repo_root / ".autolab" / "task_completions"
                completions_dir.mkdir(parents=True, exist_ok=True)
                commit_hash = ""
                try:
                    result = subprocess.run(
                        ["git", "rev-parse", "HEAD"],
                        cwd=repo_root, capture_output=True, text=True, check=False, timeout=5,
                    )
                    if result.returncode == 0:
                        commit_hash = result.stdout.strip()
                except Exception:
                    pass
                evidence = {
                    "task_id": current_task_id,
                    "completed_at": _utc_now(),
                    "verification_passed": verification_passed,
                    "verification_message": "review gate considered last verification state",
                    "changed_files": sorted(changed_paths) if changed_paths else [],
                    "meaningful_files": sorted(meaningful_paths) if meaningful_paths else [],
                    "commit_hash": commit_hash,
                }
                evidence_path = completions_dir / f"{current_task_id}.json"
                evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
            except Exception:
                pass
            state["current_task_id"] = ""
            state["task_cycle_stage"] = "done"
            state["task_change_baseline"] = {}
            repeat_guard["no_progress_decisions"] = 0
            state["repeat_guard"] = repeat_guard
            scoped_commit_paths = _assistant_commit_paths(changed_paths, meaningful_paths)
            _write_json(state_path, state)
            return _persist_simple(
                status="complete",
                message=(
                    "assistant review passed meaningful-change gate: "
                    f"{len(meaningful_paths)} meaningful file(s) changed"
                ),
                changed_files=[state_path],
                transitioned=False,
                stage_after=str(state.get("stage", stage_before)),
                commit_allowed=True,
                commit_cycle_stage="review",
                commit_paths=scoped_commit_paths,
                verification_passed=verification_passed,
                verification_message="review gate considered last verification state",
                commit_reason="meaningful-change gate passed",
                ledger_event="review",
                changed_files_summary=sorted(changed_paths) if changed_paths else [],
                meaningful_files_summary=sorted(meaningful_paths) if meaningful_paths else [],
            )

        repeat_guard["no_progress_decisions"] = int(repeat_guard.get("no_progress_decisions", 0)) + 1
        state["repeat_guard"] = repeat_guard
        guardrails = _load_guardrail_config(repo_root)
        if auto_mode and int(repeat_guard["no_progress_decisions"]) >= int(guardrails.max_no_progress_decisions):
            state["task_cycle_stage"] = "done"
            state["current_task_id"] = ""
            state["task_change_baseline"] = {}
            state["stage"] = guardrails.on_breach
            _write_json(state_path, state)
            _write_guardrail_breach(
                repo_root,
                rule="no_progress",
                counters={
                    "no_progress_decisions": int(repeat_guard["no_progress_decisions"]),
                    "max_no_progress_decisions": int(guardrails.max_no_progress_decisions),
                },
                stage=stage_before,
                remediation=f"Escalated to '{guardrails.on_breach}'. Assistant review found no meaningful changes after multiple attempts.",
            )
            return _persist_simple(
                status="failed",
                message="assistant review guardrail breach: escalating to human_review",
                changed_files=[state_path],
                transitioned=stage_before != guardrails.on_breach,
                stage_after=guardrails.on_breach,
                exit_code=1,
                commit_allowed=False,
                commit_cycle_stage="review",
                verification_passed=verification_passed,
                verification_message="review gate",
                commit_reason="guardrail breach",
                ledger_event="review",
            )

        state["task_cycle_stage"] = "implement"
        _write_json(state_path, state)
        missing_verification = meaningful_config.require_verification and not verification_passed
        details: list[str] = []
        if not meaningful:
            changed_summary = ", ".join(sorted(changed_paths)[:5]) if changed_paths else "none"
            meaningful_summary = ", ".join(sorted(meaningful_paths)[:5]) if meaningful_paths else "none"
            details.append(
                f"no meaningful code/config/docs targets changed "
                f"(changed_paths=[{changed_summary}], meaningful_paths=[{meaningful_summary}])"
            )
        if missing_verification:
            details.append("verification not passed")
        if not details:
            details.append("gate did not pass")
        return _persist_simple(
            status="needs_retry",
            message=f"assistant review blocked: {', '.join(details)}",
            changed_files=[state_path],
            transitioned=False,
            stage_after=str(state.get("stage", stage_before)),
            commit_allowed=False,
            commit_cycle_stage="review",
            verification_passed=verification_passed,
            verification_message="review gate",
            commit_reason=", ".join(details),
            ledger_event="review",
        )

    outcome = _run_once_standard(
        state_path,
        None,
        run_agent_mode=run_agent_mode,
        auto_decision=False,
        auto_mode=auto_mode,
        commit_task_id=current_task_id,
        commit_cycle_stage="implement",
        strict_implementation_progress=False,
    )

    try:
        refreshed = _normalize_state(_load_state(state_path))
    except StateError:
        refreshed = None
    if refreshed is not None and outcome.exit_code == 0 and refreshed.get("stage") not in TERMINAL_STAGES:
        refreshed["assistant_mode"] = "on"
        refreshed["task_cycle_stage"] = "verify"
        _write_json(state_path, refreshed)
        outcome = RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=f"{outcome.message}; assistant cycle -> verify",
            commit_allowed=False,
            commit_task_id=current_task_id,
            commit_cycle_stage="implement",
        )
    elif refreshed is not None:
        refreshed["assistant_mode"] = "on"
        if refreshed.get("stage") in TERMINAL_STAGES:
            refreshed["current_task_id"] = ""
            refreshed["task_cycle_stage"] = "done"
            refreshed["task_change_baseline"] = {}
        _write_json(state_path, refreshed)
        outcome = RunOutcome(
            exit_code=outcome.exit_code,
            transitioned=outcome.transitioned,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            message=outcome.message,
            commit_allowed=False,
            commit_task_id=current_task_id,
            commit_cycle_stage="implement",
        )
    try:
        _append_task_ledger(
            repo_root,
            event="implement",
            task_id=current_task_id,
            stage_before=outcome.stage_before,
            stage_after=outcome.stage_after,
            transitioned=outcome.transitioned,
            status="complete" if outcome.exit_code == 0 else "failed",
            exit_code=outcome.exit_code,
            message=outcome.message,
            commit_allowed=outcome.commit_allowed,
            commit_reason="implementation cycle execution",
        )
    except Exception as exc:
        _append_log(repo_root, f"assistant task ledger write failed: {exc}")
    return outcome
