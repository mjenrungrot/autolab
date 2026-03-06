from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autolab.constants import ACTIVE_STAGES, DECISION_STAGES
from autolab.plan_approval import (
    approval_next_commands_for_mode,
    approval_requires_action,
    load_plan_approval,
    resolve_plan_approval_state,
)
from autolab.scope import _detect_scope_kind_from_plan_contract, _resolve_scope_context
from autolab.state import (
    _normalize_state,
    _resolve_repo_root,
)
from autolab.uat import resolve_uat_requirement
from autolab.utils import (
    _collect_change_snapshot,
    _compact_json,
    _load_json_if_exists,
    _normalize_space,
    _utc_now,
    _write_json,
)
from autolab.wave_observability import build_wave_observability


_HANDOFF_FILENAME = "handoff.json"
_HANDOFF_MD_FILENAME = "handoff.md"


@dataclass(frozen=True)
class HandoffArtifacts:
    payload: dict[str, Any]
    handoff_json_path: Path
    handoff_md_path: Path


def _safe_dict(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        return payload
    return {}


def _safe_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    return []


def _sort_by_generated_at_desc(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def _key(entry: dict[str, Any]) -> tuple[str, str]:
        generated = str(entry.get("generated_at", "")).strip()
        stage = str(entry.get("stage_effective", "")).strip()
        return (generated, stage)

    return sorted(entries, key=_key, reverse=True)


def _latest_verification_summary(repo_root: Path) -> tuple[dict[str, Any], str]:
    logs_dir = repo_root / ".autolab" / "logs"
    candidates: list[dict[str, Any]] = []
    if logs_dir.exists():
        for path in sorted(logs_dir.glob("verification_*.json")):
            payload = _load_json_if_exists(path)
            if not isinstance(payload, dict):
                continue
            generated_at = str(payload.get("generated_at", "")).strip()
            if not generated_at:
                continue
            stage_effective = str(payload.get("stage_effective", "")).strip()
            passed = bool(payload.get("passed", False))
            message = str(payload.get("message", "")).strip()
            candidates.append(
                {
                    "generated_at": generated_at,
                    "stage_effective": stage_effective,
                    "passed": passed,
                    "message": message,
                }
            )

    current_result = _load_json_if_exists(
        repo_root / ".autolab" / "verification_result.json"
    )
    if isinstance(current_result, dict):
        generated_at = str(current_result.get("generated_at", "")).strip()
        if generated_at:
            candidates.append(
                {
                    "generated_at": generated_at,
                    "stage_effective": str(
                        current_result.get("stage_effective", "")
                    ).strip(),
                    "passed": bool(current_result.get("passed", False)),
                    "message": str(current_result.get("message", "")).strip(),
                }
            )

    if not candidates:
        return ({}, "")

    ordered = _sort_by_generated_at_desc(candidates)
    latest = ordered[0]
    latest_passed_at = ""
    for entry in ordered:
        if bool(entry.get("passed", False)):
            latest_passed_at = str(entry.get("generated_at", "")).strip()
            break
    return (latest, latest_passed_at)


def _verification_blockers(repo_root: Path) -> list[str]:
    payload = _load_json_if_exists(repo_root / ".autolab" / "verification_result.json")
    if not isinstance(payload, dict):
        return []
    if bool(payload.get("passed", False)):
        return []
    blockers: list[str] = []
    message = str(payload.get("message", "")).strip()
    if message:
        blockers.append(message)
    details = payload.get("details")
    if isinstance(details, dict):
        commands = details.get("commands")
        if isinstance(commands, list):
            for command in commands:
                if not isinstance(command, dict):
                    continue
                status = str(command.get("status", "")).strip().lower()
                if status not in {"fail", "error", "timeout"}:
                    continue
                name = str(command.get("name", "")).strip() or "unknown"
                detail = (
                    str(command.get("detail", "")).strip()
                    or str(command.get("stderr", "")).strip()
                    or str(command.get("stdout", "")).strip()
                )
                if detail:
                    blockers.append(f"{name}: {detail}")
                else:
                    blockers.append(f"{name}: verification failed")
    return blockers


def _review_blockers(iteration_dir: Path | None) -> list[str]:
    if iteration_dir is None:
        return []
    payload = _load_json_if_exists(iteration_dir / "review_result.json")
    if not isinstance(payload, dict):
        return []
    findings = payload.get("blocking_findings")
    if not isinstance(findings, list):
        return []
    output: list[str] = []
    for finding in findings:
        text = _normalize_space(finding)
        if text:
            output.append(text)
    return output


def _decision_result_is_valid(iteration_dir: Path | None) -> bool:
    if iteration_dir is None:
        return False
    payload = _load_json_if_exists(iteration_dir / "decision_result.json")
    if not isinstance(payload, dict):
        return False
    decision = str(payload.get("decision", "")).strip()
    rationale = str(payload.get("rationale", "")).strip()
    evidence = payload.get("evidence")
    if decision not in DECISION_STAGES:
        return False
    if not rationale:
        return False
    if not isinstance(evidence, list) or not evidence:
        return False
    return True


def _unique_list(items: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for item in items:
        normalized = _normalize_space(item)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return deduped


def _load_existing_handoff(autolab_dir: Path) -> dict[str, Any]:
    payload = _load_json_if_exists(autolab_dir / _HANDOFF_FILENAME)
    if not isinstance(payload, dict):
        return {}
    return payload


def _compute_changed_since_green(
    *,
    repo_root: Path,
    existing_handoff: dict[str, Any],
    latest_passed_at: str,
) -> tuple[list[str], dict[str, str], str]:
    current_snapshot = _collect_change_snapshot(repo_root)
    stored_green_at = str(existing_handoff.get("last_green_at", "")).strip()
    baseline_snapshot = existing_handoff.get("baseline_snapshot")
    baseline = baseline_snapshot if isinstance(baseline_snapshot, dict) else {}
    if latest_passed_at:
        if latest_passed_at != stored_green_at or not baseline:
            baseline = dict(current_snapshot)
            stored_green_at = latest_passed_at
    delta: list[str] = []
    for path, signature in current_snapshot.items():
        if str(baseline.get(path, "")) != str(signature):
            delta.append(path)
    for path in baseline:
        if path not in current_snapshot:
            delta.append(path)
    return (sorted(set(delta)), current_snapshot, stored_green_at)


def _detect_scope(
    *,
    repo_root: Path,
    iteration_dir: Path | None,
) -> str:
    return _detect_scope_kind_from_plan_contract(
        repo_root=repo_root,
        iteration_dir=iteration_dir,
    )


def _compute_wave_observability(
    *,
    repo_root: Path,
    iteration_dir: Path | None,
) -> dict[str, Any]:
    return build_wave_observability(
        repo_root,
        iteration_dir=iteration_dir,
    )


def _pending_human_decisions(
    *,
    state: dict[str, Any],
    iteration_dir: Path | None,
    block_reason_payload: dict[str, Any],
    plan_approval: dict[str, Any],
    plan_approval_action_mode: str,
    uat: dict[str, Any],
) -> list[str]:
    stage = str(state.get("stage", "")).strip()
    decisions: list[str] = []
    if stage == "human_review":
        decisions.append(
            "Resolve human review via `autolab review --status=pass|retry|stop`."
        )
    if stage == "decide_repeat" and not _decision_result_is_valid(iteration_dir):
        decisions.append(
            "Provide decide_repeat decision via `autolab run --decision=<hypothesis|design|stop|human_review>`."
        )
    action_required = str(block_reason_payload.get("action_required", "")).strip()
    if action_required:
        decisions.append(action_required)
    if stage == "implementation" and approval_requires_action(plan_approval):
        if plan_approval_action_mode == "refresh":
            decisions.append(
                "Regenerate the implementation plan checkpoint before wave execution."
            )
        else:
            decisions.append(
                "Approve, retry, or stop the implementation plan before wave execution."
            )
        decisions.extend(
            approval_next_commands_for_mode(
                plan_approval,
                action_mode=plan_approval_action_mode,
            )
        )
    if stage in {"implementation_review", "launch"} and bool(
        uat.get("required", False)
    ):
        uat_status = str(uat.get("status", "")).strip().lower()
        artifact_path = str(uat.get("artifact_path", "")).strip()
        if uat_status != "pass":
            if uat_status == "missing":
                decisions.append(
                    f"Create the required UAT artifact via `autolab uat init` and complete {artifact_path}."
                )
            else:
                decisions.append(
                    f"Update {artifact_path} and set `UATStatus: pass` before continuing."
                )
    return _unique_list(decisions)


def _recommended_command(
    *,
    state: dict[str, Any],
    blockers: list[str],
    pending_decisions: list[str],
    latest_verification: dict[str, Any],
    plan_approval: dict[str, Any],
    plan_approval_action_mode: str,
    uat: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    stage = str(state.get("stage", "")).strip()
    if stage == "human_review":
        command = "autolab review --status=<pass|retry|stop>"
        reason = "Current stage is human_review and requires an explicit decision."
        executable = False
    elif (
        stage in {"implementation_review", "launch"}
        and bool(uat.get("required", False))
        and str(uat.get("status", "")).strip().lower() == "missing"
    ):
        command = "autolab uat init"
        reason = "Required UAT artifact is missing."
        executable = True
    elif (
        stage in {"implementation_review", "launch"}
        and bool(uat.get("required", False))
        and str(uat.get("status", "")).strip().lower() != "pass"
    ):
        command = f"autolab verify --stage {stage}"
        reason = "Required UAT is incomplete or not yet marked pass."
        executable = True
    elif stage == "implementation" and approval_requires_action(plan_approval):
        if plan_approval_action_mode == "refresh":
            command = "autolab run --plan-only"
            reason = (
                "Current implementation plan approval artifact is missing or stale; "
                "refresh planning artifacts before approval."
            )
            executable = True
        else:
            command = "autolab approve-plan --status approve"
            reason = "Implementation plan approval is required before wave execution can continue."
            executable = False
    elif stage == "decide_repeat" and pending_decisions:
        command = "autolab run --decision=<hypothesis|design|stop|human_review>"
        reason = "decide_repeat requires a decision before stage transition."
        executable = False
    elif blockers:
        command = (
            f"autolab verify --stage {stage}"
            if stage in ACTIVE_STAGES
            else "autolab verify"
        )
        reason = "Blocking failures detected; rerun verification and fix blockers before resume."
        executable = True
    else:
        command = "autolab run"
        reason = (
            "Workflow has no unresolved blockers and can continue from current stage."
        )
        executable = True

    safe_status = "ready" if executable and not pending_decisions else "blocked"
    preconditions = list(pending_decisions)
    if bool(latest_verification) and not bool(latest_verification.get("passed", False)):
        preconditions.append("Resolve failing verification commands.")

    recommended = {
        "command": command,
        "reason": reason,
        "executable": executable,
    }
    safe_resume = {
        "command": command,
        "status": safe_status,
        "preconditions": _unique_list(preconditions),
    }
    return (recommended, safe_resume)


def _render_handoff_markdown(payload: dict[str, Any]) -> str:
    current_scope = str(payload.get("current_scope", "experiment")).strip()
    current_stage = str(payload.get("current_stage", "")).strip()
    wave = _safe_dict(payload.get("wave"))
    tasks = _safe_dict(payload.get("task_status"))
    verifier = _safe_dict(payload.get("latest_verifier_summary"))
    blockers = [
        str(item).strip()
        for item in _safe_list(payload.get("blocking_failures"))
        if str(item).strip()
    ]
    decisions = [
        str(item).strip()
        for item in _safe_list(payload.get("pending_human_decisions"))
        if str(item).strip()
    ]
    changed_files = [
        str(item).strip()
        for item in _safe_list(payload.get("files_changed_since_last_green_point"))
        if str(item).strip()
    ]
    recommended = _safe_dict(payload.get("recommended_next_command"))
    safe_resume = _safe_dict(payload.get("safe_resume_point"))
    plan_approval = _safe_dict(payload.get("plan_approval"))
    wave_observability = _safe_dict(payload.get("wave_observability"))
    uat = _safe_dict(payload.get("uat"))
    critical_path = _safe_dict(wave_observability.get("critical_path"))
    observability_waves = _safe_list(wave_observability.get("waves"))
    observability_conflicts = _safe_list(wave_observability.get("file_conflicts"))
    observability_tasks = _safe_list(wave_observability.get("tasks"))
    observability_diagnostics = [
        str(item).strip()
        for item in _safe_list(wave_observability.get("diagnostics"))
        if str(item).strip()
    ]

    lines = [
        "# Autolab Handoff",
        "",
        f"- generated_at: `{payload.get('generated_at', '')}`",
        f"- iteration_id: `{payload.get('iteration_id', '')}`",
        f"- experiment_id: `{payload.get('experiment_id', '')}`",
        f"- scope: `{current_scope}`",
        f"- scope_root: `{payload.get('scope_root', '')}`",
        f"- stage: `{current_stage}`",
        "",
        "## Wave and Task Status",
        "",
        f"- wave.status: `{wave.get('status', 'unavailable')}`",
        f"- wave.current: `{wave.get('current', '-')}`",
        f"- wave.executed: `{wave.get('executed', 0)}`",
        f"- wave.total: `{wave.get('total', 0)}`",
        f"- tasks.total: `{tasks.get('total', 0)}`",
        f"- tasks.completed: `{tasks.get('completed', 0)}`",
        f"- tasks.failed: `{tasks.get('failed', 0)}`",
        f"- tasks.blocked: `{tasks.get('blocked', 0)}`",
        f"- tasks.pending: `{tasks.get('pending', 0)}`",
        f"- tasks.skipped: `{tasks.get('skipped', 0)}`",
        f"- tasks.deferred: `{tasks.get('deferred', 0)}`",
    ]

    if plan_approval:
        counts = _safe_dict(plan_approval.get("counts"))
        trigger_reasons = [
            str(item).strip()
            for item in _safe_list(plan_approval.get("trigger_reasons"))
            if str(item).strip()
        ]
        lines.extend(
            [
                "",
                "## Plan Approval",
                "",
                f"- status: `{plan_approval.get('status', '')}`",
                f"- requires_approval: `{bool(plan_approval.get('requires_approval', False))}`",
                f"- plan_hash: `{plan_approval.get('plan_hash', '')}`",
                f"- risk_fingerprint: `{plan_approval.get('risk_fingerprint', '')}`",
                (
                    "- counts: "
                    f"tasks={int(counts.get('tasks_total', 0) or 0)}, "
                    f"waves={int(counts.get('waves_total', 0) or 0)}, "
                    f"project_wide_tasks={int(counts.get('project_wide_tasks', 0) or 0)}, "
                    f"project_wide_paths={int(counts.get('project_wide_unique_paths', 0) or 0)}, "
                    f"retries={int(counts.get('observed_retries', 0) or 0)}"
                ),
            ]
        )
        if trigger_reasons:
            lines.append("- trigger_reasons:")
            lines.extend(f"  - {item}" for item in trigger_reasons)
        action_mode = (
            "refresh" if plan_approval.get("status") == "superseded" else "approve"
        )
        next_commands = approval_next_commands_for_mode(
            plan_approval,
            action_mode=action_mode,
        )
        if next_commands:
            lines.append("- next_commands:")
            lines.extend(f"  - {item}" for item in next_commands)

    if uat:
        lines.extend(
            [
                "",
                "## UAT",
                "",
                f"- required: `{bool(uat.get('required', False))}`",
                f"- required_by: `{uat.get('required_by', 'none')}`",
                f"- status: `{uat.get('status', 'not_required')}`",
                f"- artifact_path: `{uat.get('artifact_path', '')}`",
            ]
        )

    if critical_path:
        lines.extend(
            [
                "",
                "## Critical Path",
                "",
                f"- status: `{critical_path.get('status', 'unavailable')}`",
                f"- mode: `{critical_path.get('mode', 'unavailable')}`",
                f"- weight: `{critical_path.get('weight', 0)}`",
                f"- duration_seconds: `{critical_path.get('duration_seconds', 0)}`",
                f"- waves: `{', '.join(str(item) for item in _safe_list(critical_path.get('wave_ids')) if str(item).strip()) or '-'}`",
                f"- tasks: `{', '.join(str(item) for item in _safe_list(critical_path.get('task_ids')) if str(item).strip()) or '-'}`",
                f"- basis: {critical_path.get('basis_note', '')}",
            ]
        )

    lines.extend(["", "## Wave Details", ""])
    if observability_waves:
        for entry in observability_waves:
            if not isinstance(entry, dict):
                continue
            lines.extend(
                [
                    (
                        f"- wave {entry.get('wave', '?')}: status={entry.get('status', 'unknown')} "
                        f"duration={entry.get('duration_seconds', 0)}s retries={entry.get('retries_used', 0)}"
                        + (
                            " retry_pending=yes"
                            if bool(entry.get("retry_pending"))
                            else ""
                        )
                        + (
                            " critical_path=yes"
                            if bool(entry.get("critical_path"))
                            else ""
                        )
                    ),
                    f"  tasks: {', '.join(str(item) for item in _safe_list(entry.get('tasks')) if str(item).strip()) or '-'}",
                    f"  retry_reasons.current: {', '.join(str(item) for item in _safe_list(entry.get('current_retry_reasons')) if str(item).strip()) or 'none'}",
                    f"  retry_reasons.history: {', '.join(str(item) for item in _safe_list(entry.get('retry_reasons')) if str(item).strip()) or 'none'}",
                    f"  blocked_tasks: {', '.join(str(item) for item in _safe_list(entry.get('blocked_task_ids')) if str(item).strip()) or 'none'}",
                    f"  deferred_tasks: {', '.join(str(item) for item in _safe_list(entry.get('deferred_task_ids')) if str(item).strip()) or 'none'}",
                    f"  skipped_tasks: {', '.join(str(item) for item in _safe_list(entry.get('skipped_task_ids')) if str(item).strip()) or 'none'}",
                    f"  out_of_contract_paths: {', '.join(str(item) for item in _safe_list(entry.get('out_of_contract_paths')) if str(item).strip()) or 'none'}",
                ]
            )
    else:
        lines.append("- unavailable")

    lines.extend(["", "## File Conflicts", ""])
    if observability_conflicts:
        for entry in observability_conflicts:
            if not isinstance(entry, dict):
                continue
            lines.append(
                (
                    f"- wave {entry.get('wave', '?')}: {entry.get('kind', 'conflict')} "
                    f"tasks={', '.join(str(item) for item in _safe_list(entry.get('tasks')) if str(item).strip()) or '-'} "
                    f"paths={', '.join(str(item) for item in _safe_list(entry.get('paths')) if str(item).strip()) or '-'} "
                    f"group={entry.get('conflict_group', '') or '-'} "
                    f"detail={entry.get('detail', '')}"
                )
            )
    else:
        lines.append("- none")

    lines.extend(["", "## Task Evidence", ""])
    evidence_rows = 0
    for entry in observability_tasks:
        if not isinstance(entry, dict):
            continue
        evidence = _safe_dict(entry.get("evidence_summary"))
        lines.append(
            (
                f"- {entry.get('task_id', '')}: status={entry.get('status', 'unknown')} "
                f"reason={entry.get('reason_code', '')} "
                f"evidence={evidence.get('text', '') or 'n/a'}"
            )
        )
        evidence_rows += 1
        if evidence_rows >= 20:
            remaining = len(observability_tasks) - evidence_rows
            if remaining > 0:
                lines.append(f"- ... and {remaining} more task evidence rows")
            break

    lines.extend(
        [
            "",
            "## Verification",
            "",
            f"- generated_at: `{verifier.get('generated_at', '')}`",
            f"- stage_effective: `{verifier.get('stage_effective', '')}`",
            f"- passed: `{verifier.get('passed', False)}`",
            f"- message: {verifier.get('message', '')}",
            "",
            "## Blocking Failures",
        ]
    )
    if blockers:
        lines.extend(f"- {entry}" for entry in blockers)
    else:
        lines.append("- none")

    lines.extend(["", "## Pending Human Decisions"])
    if decisions:
        lines.extend(f"- {entry}" for entry in decisions)
    else:
        lines.append("- none")

    lines.extend(["", "## Changed Since Last Green Point"])
    if changed_files:
        lines.extend(f"- `{path}`" for path in changed_files[:200])
        extra = len(changed_files) - min(len(changed_files), 200)
        if extra > 0:
            lines.append(f"- ... and {extra} more")
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## Resume",
            "",
            f"- recommended_next_command: `{recommended.get('command', '')}`",
            f"- recommendation_reason: {recommended.get('reason', '')}",
            f"- recommended_executable: `{recommended.get('executable', False)}`",
            f"- safe_resume_status: `{safe_resume.get('status', 'blocked')}`",
            f"- safe_resume_command: `{safe_resume.get('command', '')}`",
        ]
    )
    preconditions = _safe_list(safe_resume.get("preconditions"))
    if preconditions:
        lines.append("- safe_resume_preconditions:")
        lines.extend(
            f"  - {str(item).strip()}" for item in preconditions if str(item).strip()
        )
    else:
        lines.append("- safe_resume_preconditions: none")

    # Recovery section
    rot_flags = _safe_list(payload.get("context_rot_flags"))
    last_cps = _safe_list(payload.get("last_good_checkpoints"))
    rewind_targets = _safe_list(payload.get("recommended_rewind_targets"))
    drift = payload.get("artifact_drift_summary", {})
    if rot_flags or last_cps or rewind_targets:
        lines.extend(["", "## Recovery"])
        if rot_flags:
            lines.append("- context_rot_flags:")
            lines.extend(f"  - {f}" for f in rot_flags)
        if isinstance(drift, dict) and any(drift.values()):
            lines.append("- artifact_drift:")
            for key in ("modified", "missing", "stale_sidecars"):
                items = drift.get(key, [])
                if items:
                    lines.append(f"  - {key}: {', '.join(str(i) for i in items)}")
        if isinstance(last_cps, list) and last_cps:
            lines.append("- last_good_checkpoints:")
            for cp in last_cps[:3]:
                if isinstance(cp, dict):
                    lines.append(
                        f"  - {cp.get('checkpoint_id', '')} "
                        f"stage={cp.get('stage', '')} "
                        f"at={cp.get('created_at', '')}"
                    )
        if rewind_targets:
            lines.append(
                f"- recommended_rewind_targets: {', '.join(str(t) for t in rewind_targets)}"
            )

    lines.extend(["", "## Observability Diagnostics"])
    if observability_diagnostics:
        lines.extend(f"- {entry}" for entry in observability_diagnostics)
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## Raw Snapshot",
            "",
            "```json",
            _compact_json(payload, max_chars=12000),
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def refresh_handoff(state_path: Path) -> HandoffArtifacts:
    resolved_state_path = state_path.expanduser().resolve()
    repo_root = _resolve_repo_root(resolved_state_path)
    autolab_dir = repo_root / ".autolab"
    autolab_dir.mkdir(parents=True, exist_ok=True)

    state = _normalize_state(_safe_dict(_load_json_if_exists(resolved_state_path)))
    iteration_id = str(state.get("iteration_id", "")).strip()
    experiment_id = str(state.get("experiment_id", "")).strip()
    stage = str(state.get("stage", "")).strip()

    current_scope, scope_root, iteration_dir = _resolve_scope_context(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
    )

    existing_handoff = _load_existing_handoff(autolab_dir)
    latest_verification, latest_passed_at = _latest_verification_summary(repo_root)
    changed_files, current_snapshot, last_green_at = _compute_changed_since_green(
        repo_root=repo_root,
        existing_handoff=existing_handoff,
        latest_passed_at=latest_passed_at,
    )

    block_reason = _safe_dict(_load_json_if_exists(autolab_dir / "block_reason.json"))
    guardrail_breach = _safe_dict(
        _load_json_if_exists(autolab_dir / "guardrail_breach.json")
    )
    plan_approval: dict[str, Any] = {}
    plan_approval_action_mode = "none"
    plan_approval_error = ""
    if iteration_dir and stage == "implementation":
        (
            plan_approval,
            plan_approval_error,
            plan_approval_action_mode,
        ) = resolve_plan_approval_state(repo_root, iteration_dir)
    base_plan_approval = plan_approval
    if iteration_dir and not base_plan_approval:
        loaded_plan_approval = load_plan_approval(iteration_dir)
        if loaded_plan_approval:
            base_plan_approval = loaded_plan_approval

    uat_summary: dict[str, Any] = {
        "required": False,
        "required_by": "none",
        "artifact_path": "",
        "status": "not_required",
    }
    if iteration_dir:
        resolved_uat = resolve_uat_requirement(
            repo_root,
            iteration_dir,
            plan_approval_payload=base_plan_approval if base_plan_approval else None,
        )
        uat_summary = {
            "required": bool(resolved_uat.get("effective_required", False)),
            "required_by": str(resolved_uat.get("required_by", "none")).strip()
            or "none",
            "artifact_path": str(resolved_uat.get("artifact_path", "")).strip(),
            "status": str(resolved_uat.get("status", "not_required")).strip()
            or "not_required",
        }

    blocking_failures = []
    blocking_failures.extend(_verification_blockers(repo_root))
    blocking_failures.extend(_review_blockers(iteration_dir))
    if plan_approval_error:
        blocking_failures.append(plan_approval_error)
    if block_reason:
        reason = str(block_reason.get("reason", "")).strip()
        if reason:
            blocking_failures.append(f"block_reason: {reason}")
    if guardrail_breach:
        rule = str(guardrail_breach.get("rule", "")).strip()
        if rule:
            blocking_failures.append(f"guardrail_breach: {rule}")
    if (
        stage in {"implementation_review", "launch"}
        and bool(uat_summary.get("required", False))
        and str(uat_summary.get("status", "")).strip().lower() != "pass"
    ):
        artifact_path = str(uat_summary.get("artifact_path", "")).strip()
        required_by = str(uat_summary.get("required_by", "none")).strip() or "none"
        status_value = (
            str(uat_summary.get("status", "not_required")).strip() or "not_required"
        )
        blocking_failures.append(
            f"UAT required ({required_by}) at {artifact_path}; current status={status_value}"
        )
    blocking_failures = _unique_list(blocking_failures)

    pending_decisions = _pending_human_decisions(
        state=state,
        iteration_dir=iteration_dir,
        block_reason_payload=block_reason,
        plan_approval=plan_approval,
        plan_approval_action_mode=plan_approval_action_mode,
        uat=uat_summary,
    )

    wave_observability = _compute_wave_observability(
        repo_root=repo_root,
        iteration_dir=iteration_dir,
    )
    wave_summary = _safe_dict(wave_observability.get("wave_summary"))
    task_summary = _safe_dict(wave_observability.get("task_summary"))
    handoff_md_path = scope_root / _HANDOFF_MD_FILENAME
    handoff_json_path = autolab_dir / _HANDOFF_FILENAME

    recommended_next, safe_resume = _recommended_command(
        state=state,
        blockers=blocking_failures,
        pending_decisions=pending_decisions,
        latest_verification=latest_verification,
        plan_approval=plan_approval,
        plan_approval_action_mode=plan_approval_action_mode,
        uat=uat_summary,
    )
    if latest_verification:
        latest_verification_summary = {
            "generated_at": str(latest_verification.get("generated_at", "")).strip(),
            "stage_effective": str(
                latest_verification.get("stage_effective", "")
            ).strip(),
            "passed": bool(latest_verification.get("passed", False)),
            "message": str(latest_verification.get("message", "")).strip(),
        }
    else:
        latest_verification_summary = {
            "generated_at": "",
            "stage_effective": "",
            "passed": None,
            "message": "",
        }

    # Context-rot detection
    try:
        from autolab.checkpoint import detect_context_rot, list_checkpoints

        context_rot = detect_context_rot(
            repo_root,
            state_path=resolved_state_path,
            iteration_id=iteration_id,
            experiment_id=experiment_id,
        )
        recent_checkpoints = list_checkpoints(repo_root, iteration_id=iteration_id)[:3]
    except Exception:
        context_rot = {"has_rot": False, "context_rot_flags": []}
        recent_checkpoints = []

    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "state_file": str(resolved_state_path),
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "current_scope": current_scope,
        "scope_root": str(scope_root),
        "current_stage": stage,
        "wave": wave_summary,
        "task_status": task_summary,
        "wave_observability": wave_observability,
        "latest_verifier_summary": latest_verification_summary,
        "blocking_failures": blocking_failures,
        "pending_human_decisions": pending_decisions,
        "files_changed_since_last_green_point": changed_files,
        "recommended_next_command": recommended_next,
        "safe_resume_point": safe_resume,
        "last_green_at": last_green_at,
        "baseline_snapshot": current_snapshot,
        "handoff_json_path": str(handoff_json_path),
        "handoff_markdown_path": str(handoff_md_path),
        "uat": uat_summary,
        "context_rot_flags": context_rot.get("context_rot_flags", []),
        "last_good_checkpoints": [
            {
                "checkpoint_id": c.get("checkpoint_id", ""),
                "stage": c.get("stage", ""),
                "created_at": c.get("created_at", ""),
            }
            for c in recent_checkpoints
        ],
        "recommended_rewind_targets": context_rot.get("recommended_rewind_targets", []),
        "artifact_drift_summary": context_rot.get("artifact_drift_summary", {}),
    }
    if plan_approval:
        payload["plan_approval"] = plan_approval
    _write_json(handoff_json_path, payload)
    handoff_md_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_md_path.write_text(_render_handoff_markdown(payload), encoding="utf-8")
    return HandoffArtifacts(
        payload=payload,
        handoff_json_path=handoff_json_path,
        handoff_md_path=handoff_md_path,
    )
