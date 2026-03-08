from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autolab.campaign import (
    CampaignError,
    _campaign_lock_overview,
    _campaign_path,
    _campaign_results_markdown_path,
    _campaign_results_tsv_path,
    _campaign_summary_with_governance,
    _load_campaign,
    _validate_campaign_binding,
)
from autolab.constants import ACTIVE_STAGES, DECISION_STAGES
from autolab.plan_approval import (
    approval_next_commands_for_mode,
    approval_requires_action,
    load_plan_approval,
    resolve_plan_approval_state,
)
from autolab.scope import _resolve_scope_context
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
            "Provide decide_repeat decision via `autolab run --decision=<hypothesis|design|implementation|stop|human_review>`."
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
        suggested_init = str(uat.get("suggested_init_command", "")).strip()
        suggested_titles = [
            str(item).strip()
            for item in _safe_list(uat.get("suggested_check_titles"))
            if str(item).strip()
        ]
        if uat_status != "pass":
            if uat_status == "missing":
                command = suggested_init or "autolab uat init --suggest"
                detail = (
                    " Suggested checks: " + ", ".join(suggested_titles) + "."
                    if suggested_titles
                    else ""
                )
                decisions.append(
                    f"Create the required UAT artifact via `{command}` and complete {artifact_path}.{detail}"
                )
            else:
                decisions.append(
                    f"Update {artifact_path} and set `UATStatus: pass` before continuing."
                )
    return _unique_list(decisions)


def _resolve_campaign_context(
    repo_root: Path,
    state: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    diagnostics: list[str] = []
    try:
        campaign = _load_campaign(repo_root)
    except CampaignError as exc:
        diagnostics.append(str(exc))
        return ({}, diagnostics)
    if campaign is None:
        return ({}, diagnostics)

    summary = _campaign_summary_with_governance(repo_root, campaign)
    if bool(summary.get("resumable", False)):
        try:
            _validate_campaign_binding(repo_root, state, campaign)
        except CampaignError as exc:
            summary = dict(summary)
            summary["resumable"] = False
            summary["resume_error"] = str(exc)
            diagnostics.append(str(exc))
    try:
        lock_overview = _campaign_lock_overview(repo_root, state, campaign)
    except CampaignError as exc:
        summary = dict(summary)
        summary["lock_ok"] = False
        summary["lock_drift"] = str(exc)
        summary["lock_summary"] = str(exc)
        diagnostics.append(str(exc))
    else:
        summary = dict(summary)
        summary.update(lock_overview)
        if bool(summary.get("resumable", False)) and not bool(
            lock_overview.get("lock_ok", True)
        ):
            summary["resumable"] = False
            summary["resume_error"] = str(
                lock_overview.get("lock_drift", "campaign lock drift detected")
            )
    return (summary, diagnostics)


def _recommended_command(
    *,
    repo_root: Path,
    state: dict[str, Any],
    blockers: list[str],
    pending_decisions: list[str],
    latest_verification: dict[str, Any],
    plan_approval: dict[str, Any],
    plan_approval_action_mode: str,
    uat: dict[str, Any],
    campaign: dict[str, Any],
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
        command = (
            str(uat.get("suggested_init_command", "")).strip()
            or "autolab uat init --suggest"
        )
        reason = (
            str(uat.get("pending_message", "")).strip()
            or "Required UAT artifact is missing."
        )
        executable = True
    elif (
        stage in {"implementation_review", "launch"}
        and bool(uat.get("required", False))
        and str(uat.get("status", "")).strip().lower() != "pass"
    ):
        command = f"autolab verify --stage {stage}"
        reason = (
            str(uat.get("pending_message", "")).strip()
            or "Required UAT is incomplete or not yet marked pass."
        )
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
        command = "autolab run --decision=<hypothesis|design|implementation|stop|human_review>"
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
    if (
        campaign
        and command in {"autolab run", "autolab loop --auto"}
        and executable
        and str(campaign.get("status", "")).strip() in {"stopped", "error"}
        and not pending_decisions
    ):
        resume_command = "autolab campaign continue"
        resume_reason = "A resumable campaign is present; continue the campaign instead of resuming a one-off workflow command."
        recommended = {
            "command": resume_command,
            "reason": resume_reason,
            "executable": True,
        }
        safe_resume = {
            "command": resume_command,
            "status": safe_status,
            "preconditions": _unique_list(preconditions),
        }
    return (recommended, safe_resume)


def _relative_or_absolute_path(repo_root: Path, path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.resolve(strict=False).relative_to(repo_root).as_posix() or "."
    except Exception:
        try:
            return path.relative_to(repo_root).as_posix() or "."
        except Exception:
            return str(path)


def _load_run_status(
    *,
    repo_root: Path,
    iteration_dir: Path | None,
    state: dict[str, Any],
) -> dict[str, Any]:
    run_id = str(state.get("last_run_id", "")).strip()
    manifest_path: Path | None = None
    manifest_payload: dict[str, Any] = {}
    metrics_path: Path | None = None
    metrics_payload: dict[str, Any] = {}

    runs_root = iteration_dir / "runs" if iteration_dir is not None else None
    if runs_root is not None:
        if run_id:
            manifest_path = runs_root / run_id / "run_manifest.json"
        elif runs_root.exists():
            manifests = sorted(runs_root.glob("*/run_manifest.json"), reverse=True)
            if manifests:
                manifest_path = manifests[0]
        if manifest_path is not None and manifest_path.exists():
            manifest_payload = _safe_dict(_load_json_if_exists(manifest_path))
            if not run_id:
                run_id = (
                    str(manifest_payload.get("run_id", "")).strip()
                    or manifest_path.parent.name
                )
        elif manifest_path is not None and not run_id:
            run_id = manifest_path.parent.name

    if manifest_path is not None:
        metrics_path = manifest_path.parent / "metrics.json"
        if metrics_path.exists():
            metrics_payload = _safe_dict(_load_json_if_exists(metrics_path))

    manifest_sync = ""
    if manifest_payload:
        artifact_sync = _safe_dict(manifest_payload.get("artifact_sync_to_local"))
        manifest_sync = str(artifact_sync.get("status", "")).strip()

    return {
        "run_id": run_id,
        "host_mode": str(manifest_payload.get("host_mode", "")).strip()
        or str(manifest_payload.get("launch_mode", "")).strip(),
        "manifest_status": str(manifest_payload.get("status", "")).strip()
        or (
            "missing"
            if manifest_path is not None and not manifest_path.exists()
            else ""
        ),
        "sync_status": str(state.get("sync_status", "")).strip() or manifest_sync,
        "metrics_status": str(metrics_payload.get("status", "")).strip()
        or (
            "missing" if metrics_path is not None and not metrics_path.exists() else ""
        ),
        "manifest_path": _relative_or_absolute_path(repo_root, manifest_path),
        "metrics_path": _relative_or_absolute_path(repo_root, metrics_path),
    }


def _append_artifact_pointer(
    *,
    rows: list[dict[str, Any]],
    seen_paths: set[str],
    repo_root: Path,
    role: str,
    path: Path | None,
    reason: str,
    include_if_missing: bool = False,
    status_override: str = "",
    inline_in_oracle: bool = True,
) -> None:
    if path is None:
        return
    pointer_path = _relative_or_absolute_path(repo_root, path)
    if not pointer_path or pointer_path in seen_paths:
        return
    exists = path.exists()
    status = str(status_override).strip() or ("present" if exists else "missing")
    if status != "present" and not include_if_missing:
        return
    rows.append(
        {
            "role": role,
            "path": pointer_path,
            "status": status,
            "reason": reason,
            "inline_in_oracle": bool(inline_in_oracle),
        }
    )
    seen_paths.add(pointer_path)


def _build_top_blockers(
    *,
    blocking_failures: list[str],
    pending_decisions: list[str],
    limit: int = 6,
) -> list[str]:
    combined = _unique_list([*blocking_failures, *pending_decisions])
    return combined[:limit]


def _build_artifact_pointers(
    *,
    repo_root: Path,
    iteration_dir: Path | None,
    stage: str,
    campaign_summary: dict[str, Any],
    handoff_json_path: Path,
    handoff_md_path: Path,
    uat_summary: dict[str, Any],
    plan_approval: dict[str, Any],
    run_status: dict[str, Any],
    review_blockers: list[str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_paths: set[str] = set()

    _append_artifact_pointer(
        rows=rows,
        seen_paths=seen_paths,
        repo_root=repo_root,
        role="machine_packet",
        path=handoff_json_path,
        reason="Compact continuation source for prompts and tooling.",
        status_override="present",
    )
    _append_artifact_pointer(
        rows=rows,
        seen_paths=seen_paths,
        repo_root=repo_root,
        role="human_handoff",
        path=handoff_md_path,
        reason="Human-readable continuation and takeover snapshot.",
        status_override="present",
    )
    if campaign_summary:
        _append_artifact_pointer(
            rows=rows,
            seen_paths=seen_paths,
            repo_root=repo_root,
            role="campaign",
            path=_campaign_path(repo_root),
            reason="Campaign control-plane state for unattended research mode.",
            include_if_missing=True,
        )
        try:
            results_md_path = _campaign_results_markdown_path(
                repo_root, campaign_summary
            )
            results_tsv_path = _campaign_results_tsv_path(repo_root, campaign_summary)
        except CampaignError:
            results_md_path = None
            results_tsv_path = None
        if results_md_path is not None:
            _append_artifact_pointer(
                rows=rows,
                seen_paths=seen_paths,
                repo_root=repo_root,
                role="campaign_results_markdown",
                path=results_md_path,
                reason="Generated human-readable campaign results ledger.",
                include_if_missing=True,
            )
        if results_tsv_path is not None:
            _append_artifact_pointer(
                rows=rows,
                seen_paths=seen_paths,
                repo_root=repo_root,
                role="campaign_results_tsv",
                path=results_tsv_path,
                reason="Generated tabular campaign results ledger.",
                include_if_missing=True,
                inline_in_oracle=False,
            )

    if iteration_dir is not None:
        stage_primary_path: Path | None = None
        stage_primary_reason = ""
        if stage == "hypothesis":
            stage_primary_path = iteration_dir / "hypothesis.md"
            stage_primary_reason = "Current hypothesis stage artifact."
        elif stage == "design":
            stage_primary_path = iteration_dir / "design.yaml"
            stage_primary_reason = "Current design stage artifact."
        elif stage == "implementation":
            stage_primary_path = iteration_dir / "implementation_plan.md"
            stage_primary_reason = "Current implementation plan artifact."
        elif stage in {"implementation_review", "human_review"}:
            stage_primary_path = iteration_dir / "implementation_review.md"
            stage_primary_reason = "Current implementation review artifact."
        elif stage == "update_docs":
            stage_primary_path = iteration_dir / "docs_update.md"
            stage_primary_reason = "Current documentation update artifact."
        elif stage in {"decide_repeat", "stop"}:
            stage_primary_path = iteration_dir / "decision_result.json"
            stage_primary_reason = "Current decision artifact."
        if stage_primary_path is not None:
            _append_artifact_pointer(
                rows=rows,
                seen_paths=seen_paths,
                repo_root=repo_root,
                role="stage_artifact",
                path=stage_primary_path,
                reason=stage_primary_reason,
                include_if_missing=True,
            )

        if stage == "implementation" or plan_approval:
            _append_artifact_pointer(
                rows=rows,
                seen_paths=seen_paths,
                repo_root=repo_root,
                role="plan_approval",
                path=iteration_dir / "plan_approval.json",
                reason="Implementation plan approval and risk gate snapshot.",
                include_if_missing=(stage == "implementation"),
            )

        if stage in {
            "implementation",
            "implementation_review",
            "launch",
            "slurm_monitor",
            "extract_results",
            "update_docs",
            "decide_repeat",
        }:
            _append_artifact_pointer(
                rows=rows,
                seen_paths=seen_paths,
                repo_root=repo_root,
                role="plan_execution_summary",
                path=iteration_dir / "plan_execution_summary.json",
                reason="Wave/task execution evidence, retries, and verification rollup.",
                include_if_missing=(stage == "implementation"),
            )

        if (
            stage in {"implementation_review", "human_review", "launch"}
            or review_blockers
        ):
            _append_artifact_pointer(
                rows=rows,
                seen_paths=seen_paths,
                repo_root=repo_root,
                role="review_result",
                path=iteration_dir / "review_result.json",
                reason="Implementation review gate result and blocking findings.",
                include_if_missing=(stage in {"implementation_review", "human_review"}),
            )

        run_manifest_path_text = str(run_status.get("manifest_path", "")).strip()
        run_metrics_path_text = str(run_status.get("metrics_path", "")).strip()
        run_manifest_path = (
            repo_root / run_manifest_path_text if run_manifest_path_text else None
        )
        run_metrics_path = (
            repo_root / run_metrics_path_text if run_metrics_path_text else None
        )
        if run_manifest_path is not None:
            _append_artifact_pointer(
                rows=rows,
                seen_paths=seen_paths,
                repo_root=repo_root,
                role="run_manifest",
                path=run_manifest_path,
                reason="Latest run manifest and execution status.",
                include_if_missing=True,
            )
        if run_metrics_path is not None:
            _append_artifact_pointer(
                rows=rows,
                seen_paths=seen_paths,
                repo_root=repo_root,
                role="run_metrics",
                path=run_metrics_path,
                reason="Latest run metrics and completion state.",
                include_if_missing=True,
            )

        if bool(uat_summary.get("required", False)) or bool(
            uat_summary.get("pending", False)
        ):
            artifact_path = str(uat_summary.get("artifact_path", "")).strip()
            uat_path = repo_root / artifact_path if artifact_path else None
            _append_artifact_pointer(
                rows=rows,
                seen_paths=seen_paths,
                repo_root=repo_root,
                role="uat",
                path=uat_path,
                reason="User-acceptance testing gate artifact.",
                include_if_missing=True,
            )

        if stage in {"update_docs", "decide_repeat"}:
            _append_artifact_pointer(
                rows=rows,
                seen_paths=seen_paths,
                repo_root=repo_root,
                role="traceability",
                path=iteration_dir / "traceability_coverage.json",
                reason="End-to-end requirement-to-decision evidence coverage.",
            )

    return rows[:8]


def _build_continuation_packet(
    *,
    repo_root: Path,
    scope_root: Path,
    state: dict[str, Any],
    stage: str,
    current_scope: str,
    handoff_json_path: Path,
    handoff_md_path: Path,
    iteration_dir: Path | None,
    latest_verification_summary: dict[str, Any],
    blocking_failures: list[str],
    pending_decisions: list[str],
    recommended_next: dict[str, Any],
    safe_resume: dict[str, Any],
    campaign_summary: dict[str, Any],
    plan_approval: dict[str, Any],
    uat_summary: dict[str, Any],
    context_rot: dict[str, Any],
    recent_checkpoints: list[dict[str, Any]],
    last_green_at: str,
    block_reason: dict[str, Any],
    guardrail_breach: dict[str, Any],
    wave_observability: dict[str, Any],
    review_blockers: list[str],
) -> dict[str, Any]:
    latest_checkpoint = _safe_dict(recent_checkpoints[0]) if recent_checkpoints else {}
    trigger_reasons = [
        str(item).strip()
        for item in _safe_list(plan_approval.get("trigger_reasons"))
        if str(item).strip()
    ]
    effective_flags: list[str] = []
    if approval_requires_action(plan_approval):
        effective_flags.append("plan_approval_required")
    elif plan_approval:
        effective_flags.append("plan_approval_recorded")
    if bool(uat_summary.get("required", False)):
        effective_flags.append("uat_required")
    if bool(uat_summary.get("pending", False)):
        effective_flags.append("uat_pending")
    if latest_verification_summary.get("passed") is False:
        effective_flags.append("verification_failing")
    if str(block_reason.get("reason", "")).strip():
        effective_flags.append("manual_block")
    if str(guardrail_breach.get("rule", "")).strip():
        effective_flags.append("guardrail_breach")
    if _safe_list(context_rot.get("context_rot_flags")):
        effective_flags.append("context_rot_detected")

    run_status = _load_run_status(
        repo_root=repo_root,
        iteration_dir=iteration_dir,
        state=state,
    )
    diagnostics = _unique_list(
        [
            *[
                str(item).strip()
                for item in _safe_list(wave_observability.get("diagnostics"))
                if str(item).strip()
            ],
            *[
                str(item).strip()
                for item in _safe_list(context_rot.get("context_rot_flags"))
                if str(item).strip()
            ],
        ]
    )

    return {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "active_stage": {
            "stage": stage,
            "stage_attempt": int(state.get("stage_attempt", 0) or 0),
            "max_stage_attempts": int(state.get("max_stage_attempts", 0) or 0),
            "scope_kind": current_scope or "experiment",
            "scope_root": str(scope_root),
        },
        "next_action": {
            "recommended_command": str(recommended_next.get("command", "")).strip(),
            "safe_command": str(safe_resume.get("command", "")).strip(),
            "safe_status": str(safe_resume.get("status", "blocked")).strip()
            or "blocked",
            "preconditions": [
                str(item).strip()
                for item in _safe_list(safe_resume.get("preconditions"))
                if str(item).strip()
            ],
            "reason": str(recommended_next.get("reason", "")).strip(),
            "executable": bool(recommended_next.get("executable", False)),
        },
        "campaign": dict(campaign_summary) if campaign_summary else {},
        "latest_good_checkpoint": {
            "checkpoint_id": str(latest_checkpoint.get("checkpoint_id", "")).strip(),
            "stage": str(latest_checkpoint.get("stage", "")).strip(),
            "created_at": str(latest_checkpoint.get("created_at", "")).strip(),
            "last_green_at": last_green_at,
            "recommended_rewind_targets": [
                str(item).strip()
                for item in _safe_list(context_rot.get("recommended_rewind_targets"))
                if str(item).strip()
            ],
        },
        "policy_and_risk": {
            "plan_approval_status": str(plan_approval.get("status", "")).strip(),
            "plan_requires_approval": bool(
                plan_approval.get("requires_approval", False)
            ),
            "plan_trigger_reasons": trigger_reasons,
            "plan_hash": str(plan_approval.get("plan_hash", "")).strip(),
            "risk_fingerprint": str(plan_approval.get("risk_fingerprint", "")).strip(),
            "guardrail_breach": str(guardrail_breach.get("rule", "")).strip(),
            "block_reason": str(block_reason.get("reason", "")).strip(),
            "context_rot_flags": [
                str(item).strip()
                for item in _safe_list(context_rot.get("context_rot_flags"))
                if str(item).strip()
            ],
            "effective_flags": _unique_list(effective_flags),
        },
        "run_status": run_status,
        "uat_status": {
            "required": bool(uat_summary.get("required", False)),
            "required_by": str(uat_summary.get("required_by", "none")).strip()
            or "none",
            "status": str(uat_summary.get("status", "not_required")).strip()
            or "not_required",
            "artifact_path": str(uat_summary.get("artifact_path", "")).strip(),
            "pending": bool(uat_summary.get("pending", False)),
            "pending_message": str(uat_summary.get("pending_message", "")).strip(),
            "suggested_init_command": str(
                uat_summary.get("suggested_init_command", "")
            ).strip(),
        },
        "top_blockers": _build_top_blockers(
            blocking_failures=blocking_failures,
            pending_decisions=pending_decisions,
        ),
        "artifact_pointers": _build_artifact_pointers(
            repo_root=repo_root,
            iteration_dir=iteration_dir,
            stage=stage,
            campaign_summary=campaign_summary,
            handoff_json_path=handoff_json_path,
            handoff_md_path=handoff_md_path,
            uat_summary=uat_summary,
            plan_approval=plan_approval,
            run_status=run_status,
            review_blockers=review_blockers,
        ),
        "diagnostics": diagnostics,
    }


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
    continuation = _safe_dict(payload.get("continuation_packet"))
    active_stage = _safe_dict(continuation.get("active_stage"))
    next_action = _safe_dict(continuation.get("next_action"))
    campaign = _safe_dict(continuation.get("campaign"))
    latest_good_checkpoint = _safe_dict(continuation.get("latest_good_checkpoint"))
    policy_and_risk = _safe_dict(continuation.get("policy_and_risk"))
    run_status = _safe_dict(continuation.get("run_status"))
    uat_status = _safe_dict(continuation.get("uat_status"))
    top_blockers = [
        str(item).strip()
        for item in _safe_list(continuation.get("top_blockers"))
        if str(item).strip()
    ]
    artifact_pointers = [
        _safe_dict(item)
        for item in _safe_list(continuation.get("artifact_pointers"))
        if isinstance(item, dict)
    ]
    continuation_diagnostics = [
        str(item).strip()
        for item in _safe_list(continuation.get("diagnostics"))
        if str(item).strip()
    ]
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
        "## Continuation Packet",
        "",
        f"- active_stage: `{active_stage.get('stage', current_stage)}`",
        f"- active_attempt: `{active_stage.get('stage_attempt', '-')}`/`{active_stage.get('max_stage_attempts', '-')}`",
        f"- active_scope_kind: `{active_stage.get('scope_kind', current_scope)}`",
        f"- campaign_status: `{campaign.get('status', '')}`",
        f"- campaign_label: `{campaign.get('label', '')}`",
        f"- campaign_active_family: `{campaign.get('idea_journal_active_family', '')}`",
        f"- campaign_same_family_streak: `{campaign.get('idea_journal_same_family_streak', 0)}`",
        f"- campaign_last_completed_idea: `{campaign.get('idea_journal_last_completed_status', '')}` / `{campaign.get('idea_journal_last_completed_family', '')}`",
        f"- campaign_recent_failed_families: `{', '.join(campaign.get('idea_journal_recent_failed_families', [])) if isinstance(campaign.get('idea_journal_recent_failed_families'), list) and campaign.get('idea_journal_recent_failed_families') else 'none'}`",
        f"- next_safe_status: `{next_action.get('safe_status', safe_resume.get('status', 'blocked'))}`",
        f"- next_safe_command: `{next_action.get('safe_command', safe_resume.get('command', ''))}`",
        f"- recommended_command: `{next_action.get('recommended_command', recommended.get('command', ''))}`",
        f"- recommendation_reason: {next_action.get('reason', recommended.get('reason', ''))}",
        f"- latest_good_checkpoint: `{latest_good_checkpoint.get('checkpoint_id', '')}`",
        f"- latest_good_checkpoint_stage: `{latest_good_checkpoint.get('stage', '')}`",
        f"- latest_good_checkpoint_at: `{latest_good_checkpoint.get('created_at', '')}`",
        f"- last_green_at: `{latest_good_checkpoint.get('last_green_at', payload.get('last_green_at', ''))}`",
        f"- run_id: `{run_status.get('run_id', '')}`",
        f"- run_manifest_status: `{run_status.get('manifest_status', '')}`",
        f"- run_metrics_status: `{run_status.get('metrics_status', '')}`",
        f"- run_sync_status: `{run_status.get('sync_status', '')}`",
        f"- uat_required: `{bool(uat_status.get('required', uat.get('required', False)))}`",
        f"- uat_status: `{uat_status.get('status', uat.get('status', 'not_required'))}`",
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
    effective_flags = [
        str(item).strip()
        for item in _safe_list(policy_and_risk.get("effective_flags"))
        if str(item).strip()
    ]
    if effective_flags:
        lines.append("- effective_policy_risk_flags:")
        lines.extend(f"  - {item}" for item in effective_flags)
    plan_trigger_reasons = [
        str(item).strip()
        for item in _safe_list(policy_and_risk.get("plan_trigger_reasons"))
        if str(item).strip()
    ]
    if plan_trigger_reasons:
        lines.append("- plan_trigger_reasons:")
        lines.extend(f"  - {item}" for item in plan_trigger_reasons)
    if top_blockers:
        lines.append("- top_blockers:")
        lines.extend(f"  - {item}" for item in top_blockers)
    if artifact_pointers:
        lines.append("- artifact_pointers:")
        for entry in artifact_pointers:
            lines.append(
                "  - "
                + (
                    f"{entry.get('role', 'artifact')}: "
                    f"{entry.get('path', '')} "
                    f"[{entry.get('status', 'unknown')}] "
                    f"{entry.get('reason', '')}"
                ).strip()
            )
    if continuation_diagnostics:
        lines.append("- continuation_diagnostics:")
        lines.extend(f"  - {item}" for item in continuation_diagnostics)

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
                f"- pending: `{bool(uat.get('pending', False))}`",
            ]
        )
        pending_message = str(uat.get("pending_message", "")).strip()
        if pending_message:
            lines.append(f"- pending_message: {pending_message}")
        suggested_init = str(uat.get("suggested_init_command", "")).strip()
        if suggested_init:
            lines.append(f"- suggested_init_command: `{suggested_init}`")
        suggested_titles = [
            str(item).strip()
            for item in _safe_list(uat.get("suggested_check_titles"))
            if str(item).strip()
        ]
        if suggested_titles:
            lines.append("- suggested_check_titles:")
            lines.extend(f"  - {item}" for item in suggested_titles)

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
    campaign_summary, campaign_diagnostics = _resolve_campaign_context(repo_root, state)

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
        "pending": False,
        "pending_message": "",
        "suggested_init_command": "",
        "suggested_check_titles": [],
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
            "pending": bool(resolved_uat.get("pending", False)),
            "pending_message": str(resolved_uat.get("pending_message", "")).strip(),
            "suggested_init_command": str(
                resolved_uat.get("suggested_init_command", "")
            ).strip(),
            "suggested_check_titles": list(
                resolved_uat.get("suggested_check_titles", [])
            )
            if isinstance(resolved_uat.get("suggested_check_titles"), list)
            else [],
        }

    blocking_failures = []
    blocking_failures.extend(_verification_blockers(repo_root))
    review_blockers = _review_blockers(iteration_dir)
    blocking_failures.extend(review_blockers)
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
        pending_message = str(uat_summary.get("pending_message", "")).strip()
        if pending_message:
            blocking_failures.append(pending_message)
        else:
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
        repo_root=repo_root,
        state=state,
        blockers=blocking_failures,
        pending_decisions=pending_decisions,
        latest_verification=latest_verification,
        plan_approval=plan_approval,
        plan_approval_action_mode=plan_approval_action_mode,
        uat=uat_summary,
        campaign=campaign_summary,
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
        "campaign": campaign_summary,
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
    if campaign_diagnostics:
        payload["campaign_diagnostics"] = campaign_diagnostics
    if plan_approval:
        payload["plan_approval"] = plan_approval
    payload["continuation_packet"] = _build_continuation_packet(
        repo_root=repo_root,
        scope_root=scope_root,
        state=state,
        stage=stage,
        current_scope=current_scope,
        handoff_json_path=handoff_json_path,
        handoff_md_path=handoff_md_path,
        iteration_dir=iteration_dir,
        latest_verification_summary=latest_verification_summary,
        blocking_failures=blocking_failures,
        pending_decisions=pending_decisions,
        recommended_next=recommended_next,
        safe_resume=safe_resume,
        campaign_summary=campaign_summary,
        plan_approval=plan_approval,
        uat_summary=uat_summary,
        context_rot=context_rot,
        recent_checkpoints=recent_checkpoints,
        last_green_at=last_green_at,
        block_reason=block_reason,
        guardrail_breach=guardrail_breach,
        wave_observability=wave_observability,
        review_blockers=review_blockers,
    )
    _write_json(handoff_json_path, payload)
    handoff_md_path.parent.mkdir(parents=True, exist_ok=True)
    handoff_md_path.write_text(_render_handoff_markdown(payload), encoding="utf-8")
    return HandoffArtifacts(
        payload=payload,
        handoff_json_path=handoff_json_path,
        handoff_md_path=handoff_md_path,
    )
