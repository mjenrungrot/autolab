"""Campaign-mode CLI handlers."""

from __future__ import annotations

from autolab.campaign import (
    CampaignError,
    _campaign_apply_challenger_outcome,
    _campaign_apply_crash_outcome,
    _campaign_backfill_lock_contract,
    _campaign_bump_active_candidate_fix_attempts,
    _campaign_cancel_slurm_run_for_timeout,
    _campaign_candidate_run_id_from_state,
    _campaign_has_lock_contract,
    _campaign_has_active_candidate,
    _campaign_has_champion_snapshot,
    _campaign_is_resumable,
    _campaign_lock_mode,
    _campaign_lock_overview,
    _campaign_path,
    _campaign_results_overview,
    _campaign_run_duration_seconds,
    _campaign_seed_champion_snapshot,
    _campaign_set_active_candidate,
    _campaign_set_last_governance_event,
    _campaign_summary_with_governance,
    _campaign_sync_active_idea_journal,
    _campaign_sync_active_candidate_from_state,
    _create_campaign_payload,
    _load_campaign,
    _normalize_campaign,
    _refresh_campaign_results,
    _validate_campaign_binding,
    _write_campaign,
)
from autolab.cli.support import *
from autolab.cli.handlers_observe import _safe_refresh_handoff
from autolab.config import _load_campaign_governance_config

_CAMPAIGN_ACTIVE_CANDIDATE_STAGES = {"implementation", "design"}


def main(argv: list[str] | None = None) -> int:
    """Late-bind to autolab.commands.main to preserve monkeypatch compatibility."""
    from autolab.commands import main as commands_main

    return int(commands_main(argv))


def _campaign_lock_error(lock_path: Path) -> str:
    info = _inspect_lock(lock_path)
    if info is None:
        return ""
    return (
        f"active lock exists at {lock_path} "
        f"(pid={info.get('pid', '?')}, host={info.get('host', '?')}, "
        f"command={info.get('command', '<unknown>')})"
    )


def _campaign_loop_args(state_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        state_file=str(state_path),
        max_iterations=1,
        max_hours=DEFAULT_MAX_HOURS,
        auto=True,
        run_agent_mode="policy",
        assistant=False,
        verify=True,
        strict_implementation_progress=True,
        plan_only=False,
        execute_approved_plan=False,
    )


def _campaign_rethink_reason(
    state: dict[str, Any],
    handoff_payload: dict[str, Any],
) -> str:
    stage = str(state.get("stage", "")).strip()
    if stage in TERMINAL_STAGES:
        return f"workflow entered terminal stage '{stage}'"

    pending_decisions = handoff_payload.get("pending_human_decisions")
    if isinstance(pending_decisions, list):
        for item in pending_decisions:
            text = str(item).strip()
            if text:
                return f"pending human decision: {text}"

    blocking_failures = handoff_payload.get("blocking_failures")
    if isinstance(blocking_failures, list):
        for item in blocking_failures:
            text = str(item).strip()
            if text:
                return f"blocking failure: {text}"

    safe_resume = handoff_payload.get("safe_resume_point")
    if isinstance(safe_resume, dict):
        safe_status = str(safe_resume.get("status", "")).strip().lower()
        if safe_status and safe_status != "ready":
            return f"safe resume is {safe_status}"
    return ""


def _campaign_update_status(
    repo_root: Path,
    campaign: dict[str, Any],
    *,
    status: str,
    crash_delta: int = 0,
) -> dict[str, Any]:
    updated = dict(_normalize_campaign(campaign))
    updated["status"] = status
    if crash_delta:
        updated["crash_streak"] = int(updated.get("crash_streak", 0) or 0) + crash_delta
    _write_campaign(repo_root, updated)
    return updated


def _campaign_refresh_results_best_effort(
    repo_root: Path,
    campaign: dict[str, Any],
    *,
    context: str,
) -> None:
    try:
        result = _refresh_campaign_results(repo_root, campaign)
    except CampaignError as exc:
        _append_log(repo_root, f"campaign results refresh warning ({context}): {exc}")
        return
    results_tsv_path = result.get("results_tsv_path")
    results_md_path = result.get("results_md_path")
    _append_log(
        repo_root,
        (
            "campaign results refresh: "
            f"context={context} "
            f"rows={int(result.get('row_count', 0) or 0)} "
            f"tsv={results_tsv_path} md={results_md_path}"
        ),
    )


def _campaign_fail_control_plane(
    repo_root: Path,
    campaign: dict[str, Any],
    *,
    context: str,
    message: str,
) -> int:
    updated = _campaign_update_status(
        repo_root,
        campaign,
        status="error",
        crash_delta=1,
    )
    _campaign_refresh_results_best_effort(
        repo_root,
        updated,
        context=context,
    )
    print(message, file=sys.stderr)
    return 1


def _campaign_refresh_handoff_required(
    state_path: Path,
    repo_root: Path,
    campaign: dict[str, Any],
    *,
    context: str,
) -> tuple[dict[str, Any] | None, int | None]:
    handoff_payload, handoff_error = _safe_refresh_handoff(state_path)
    if handoff_payload is not None:
        return (handoff_payload, None)
    return (
        None,
        _campaign_fail_control_plane(
            repo_root,
            campaign,
            context=context,
            message=(
                "autolab campaign: ERROR failed to refresh handoff snapshot: "
                f"{handoff_error}"
            ),
        ),
    )


def _campaign_sync_candidate_state(
    repo_root: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
) -> dict[str, Any]:
    updated = _campaign_sync_active_candidate_from_state(campaign, state)
    stage_name = str(state.get("stage", "")).strip().lower()
    if (
        stage_name in _CAMPAIGN_ACTIVE_CANDIDATE_STAGES
        and not _campaign_has_active_candidate(updated)
    ):
        updated = _campaign_set_active_candidate(
            updated,
            decision=stage_name,
            run_id=_campaign_candidate_run_id_from_state(state, updated),
            timeout_reference_seconds=(
                _campaign_run_duration_seconds(
                    repo_root,
                    state,
                    run_id=str(updated.get("champion_run_id", "")).strip(),
                )
                or 0.0
            ),
        )
    updated = _campaign_sync_active_idea_journal(repo_root, state, updated)
    return updated


def _campaign_governance_threshold_breach(
    repo_root: Path,
    campaign: dict[str, Any],
) -> tuple[str, str] | None:
    governance = _load_campaign_governance_config(repo_root)
    crash_streak = int(campaign.get("crash_streak", 0) or 0)
    if crash_streak >= governance.max_crash_streak_before_rethink:
        return (
            "crash_rethink",
            (
                "campaign crash streak reached "
                f"{crash_streak}/{governance.max_crash_streak_before_rethink}; "
                "exporting oracle packet for rethink"
            ),
        )
    no_improvement_streak = int(campaign.get("no_improvement_streak", 0) or 0)
    if no_improvement_streak >= governance.max_no_improvement_streak:
        return (
            "stagnation_rethink",
            (
                "campaign no-improvement streak reached "
                f"{no_improvement_streak}/{governance.max_no_improvement_streak}; "
                "exporting oracle packet for rethink"
            ),
        )
    return None


def _campaign_export_oracle_and_stop(
    state_path: Path,
    repo_root: Path,
    campaign: dict[str, Any],
    *,
    category: str,
    reason: str,
    run_id: str = "",
    context: str,
) -> int:
    from autolab.cli.handlers_admin import _export_oracle_document

    try:
        output_path, source_count, command_display = _export_oracle_document(
            state_path=state_path,
            repo_root=repo_root,
            timeout_seconds=240.0,
            output_path=None,
        )
    except Exception as exc:
        return _campaign_fail_control_plane(
            repo_root,
            campaign,
            context=f"{context}_oracle_error",
            message=f"autolab campaign: ERROR failed to export oracle packet: {exc}",
        )

    refreshed_campaign = _load_campaign(repo_root) or campaign
    updated = dict(_normalize_campaign(refreshed_campaign))
    updated["status"] = "needs_rethink"
    updated = _campaign_set_last_governance_event(
        updated,
        category=category,
        run_id=run_id,
        reason=reason,
    )
    _write_campaign(repo_root, updated)
    _campaign_refresh_results_best_effort(
        repo_root,
        updated,
        context=context,
    )
    _handoff_payload, error_code = _campaign_refresh_handoff_required(
        state_path,
        repo_root,
        updated,
        context=f"{context}_handoff_refresh",
    )
    if error_code is not None:
        return error_code

    _append_log(
        repo_root,
        (
            "campaign oracle export: "
            f"context={context} output={output_path} "
            f"sources={source_count} llm_command={command_display}"
        ),
    )
    print(f"autolab campaign: stop ({reason})", file=sys.stderr)
    return 1


def _campaign_after_candidate_outcome(
    state_path: Path,
    repo_root: Path,
    campaign: dict[str, Any],
    *,
    context: str,
) -> int | None:
    _campaign_refresh_results_best_effort(
        repo_root,
        campaign,
        context=context,
    )
    threshold = _campaign_governance_threshold_breach(repo_root, campaign)
    if threshold is not None:
        category, reason = threshold
        last_event = campaign.get("last_governance_event")
        if not isinstance(last_event, dict):
            last_event = {}
        return _campaign_export_oracle_and_stop(
            state_path,
            repo_root,
            campaign,
            category=category,
            reason=reason,
            run_id=str(last_event.get("run_id", "")).strip(),
            context=context,
        )
    _handoff_payload, error_code = _campaign_refresh_handoff_required(
        state_path,
        repo_root,
        campaign,
        context=f"{context}_handoff_refresh",
    )
    if error_code is not None:
        return error_code
    return None


def _campaign_check_active_candidate_timeout(
    state_path: Path,
    repo_root: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
) -> tuple[dict[str, Any], int | None]:
    normalized = _normalize_campaign(campaign)
    if not _campaign_has_active_candidate(normalized):
        return (normalized, None)
    if str(state.get("stage", "")).strip().lower() != "slurm_monitor":
        return (normalized, None)

    candidate = normalized.get("active_candidate")
    if not isinstance(candidate, dict):
        candidate = {}
    run_id = str(candidate.get("run_id", "")).strip()
    timeout_reference_seconds = float(
        candidate.get("timeout_reference_seconds", 0.0) or 0.0
    )
    if not run_id or timeout_reference_seconds <= 0:
        return (normalized, None)

    governance = _load_campaign_governance_config(repo_root)
    elapsed_seconds = _campaign_run_duration_seconds(
        repo_root,
        state,
        run_id=run_id,
        allow_incomplete=True,
    )
    if elapsed_seconds is None:
        return (normalized, None)
    timeout_budget_seconds = timeout_reference_seconds * governance.max_timeout_factor
    if elapsed_seconds <= timeout_budget_seconds:
        return (normalized, None)

    try:
        cancel_result = _campaign_cancel_slurm_run_for_timeout(
            repo_root,
            state,
            run_id=run_id,
            elapsed_seconds=elapsed_seconds,
            timeout_reference_seconds=timeout_reference_seconds,
            max_timeout_factor=governance.max_timeout_factor,
        )
    except CampaignError as exc:
        return (
            normalized,
            _campaign_fail_control_plane(
                repo_root,
                normalized,
                context="timeout_cancel_error",
                message=f"autolab campaign: ERROR {exc}",
            ),
        )

    reason = (
        "challenger exceeded timeout budget "
        f"({elapsed_seconds:.1f}s > {timeout_budget_seconds:.1f}s)"
    )
    try:
        crash_result = _campaign_apply_crash_outcome(
            repo_root,
            state_path,
            state,
            normalized,
            reason=reason,
            category="timeout_discard",
            run_id=run_id,
        )
    except CampaignError as exc:
        return (
            normalized,
            _campaign_fail_control_plane(
                repo_root,
                normalized,
                context="timeout_crash_discard_error",
                message=f"autolab campaign: ERROR {exc}",
            ),
        )

    updated_campaign = crash_result.get("campaign")
    if not isinstance(updated_campaign, dict):
        updated_campaign = normalized
    _append_log(
        repo_root,
        (
            "campaign timeout discard: "
            f"run_id={run_id} job_id={cancel_result.get('job_id', '')} "
            f"elapsed_seconds={elapsed_seconds:.3f} "
            f"timeout_reference_seconds={timeout_reference_seconds:.3f}"
        ),
    )
    follow_up = _campaign_after_candidate_outcome(
        state_path,
        repo_root,
        updated_campaign,
        context="timeout_discard",
    )
    if follow_up is not None:
        return (updated_campaign, follow_up)
    return (updated_campaign, 0)


def _run_campaign_session(state_path: Path) -> int:
    from autolab.cli.handlers_run import _cmd_loop

    state_path = Path(state_path).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    campaign_path = _campaign_path(repo_root)
    print(f"campaign_file: {campaign_path}")

    while True:
        try:
            campaign = _load_campaign(repo_root)
        except CampaignError as exc:
            print(f"autolab campaign: ERROR {exc}", file=sys.stderr)
            return 1
        if campaign is None:
            print("autolab campaign: ERROR no active campaign", file=sys.stderr)
            return 1

        if campaign["status"] == "stop_requested":
            updated = _campaign_update_status(repo_root, campaign, status="stopped")
            _campaign_refresh_results_best_effort(
                repo_root,
                updated,
                context="stop_requested",
            )
            print("autolab campaign: stop requested acknowledged")
            return 0

        try:
            state = _normalize_state(_load_state(state_path))
            campaign = _validate_campaign_binding(repo_root, state, campaign)
        except (CampaignError, StateError) as exc:
            return _campaign_fail_control_plane(
                repo_root,
                campaign,
                context="binding_error",
                message=f"autolab campaign: ERROR {exc}",
            )

        lock_overview = _campaign_lock_overview(repo_root, state, campaign)
        if not bool(lock_overview.get("lock_ok", True)):
            updated = _campaign_update_status(
                repo_root,
                campaign,
                status="needs_rethink",
            )
            _campaign_refresh_results_best_effort(
                repo_root,
                updated,
                context="lock_drift",
            )
            print(
                "autolab campaign: stop "
                f"({lock_overview.get('lock_drift', 'campaign lock drift detected')})",
                file=sys.stderr,
            )
            return 1

        synced_campaign = _campaign_sync_candidate_state(repo_root, state, campaign)
        if synced_campaign != campaign:
            _write_campaign(repo_root, synced_campaign)
            campaign = synced_campaign

        if (
            _campaign_has_active_candidate(campaign)
            and str(state.get("stage", "")).strip() == "human_review"
        ):
            try:
                crash_result = _campaign_apply_crash_outcome(
                    repo_root,
                    state_path,
                    state,
                    campaign,
                    reason=(
                        "challenger escalated to human_review during unattended campaign"
                    ),
                    category="human_review_discard",
                )
            except CampaignError as exc:
                return _campaign_fail_control_plane(
                    repo_root,
                    campaign,
                    context="human_review_discard_error",
                    message=f"autolab campaign: ERROR {exc}",
                )
            updated_campaign = crash_result.get("campaign")
            if not isinstance(updated_campaign, dict):
                updated_campaign = campaign
            follow_up = _campaign_after_candidate_outcome(
                state_path,
                repo_root,
                updated_campaign,
                context="human_review_discard",
            )
            if follow_up is not None:
                return follow_up
            continue

        campaign, timeout_exit_code = _campaign_check_active_candidate_timeout(
            state_path,
            repo_root,
            state,
            campaign,
        )
        if timeout_exit_code is not None:
            if timeout_exit_code == 0:
                continue
            return timeout_exit_code

        if str(state.get("stage", "")).strip() == "decide_repeat":
            if not _campaign_has_champion_snapshot(repo_root, campaign):
                updated = _campaign_update_status(
                    repo_root,
                    campaign,
                    status="needs_rethink",
                )
                _campaign_refresh_results_best_effort(
                    repo_root,
                    updated,
                    context="missing_champion_snapshot",
                )
                print(
                    "autolab campaign: stop (champion snapshot is missing; "
                    "restart from decide_repeat to reseed campaign state)",
                    file=sys.stderr,
                )
                return 1
            challenger_run_id = str(state.get("last_run_id", "")).strip()
            if challenger_run_id and challenger_run_id != campaign["champion_run_id"]:
                try:
                    compare_result = _campaign_apply_challenger_outcome(
                        repo_root,
                        state_path,
                        state,
                        campaign,
                    )
                except CampaignError as exc:
                    return _campaign_fail_control_plane(
                        repo_root,
                        campaign,
                        context="compare_error",
                        message=f"autolab campaign: ERROR {exc}",
                    )
                updated_campaign = compare_result.get("campaign")
                if not isinstance(updated_campaign, dict):
                    updated_campaign = campaign
                summary = str(compare_result.get("summary", "")).strip()
                action = str(compare_result.get("action", "")).strip() or "compare"
                _append_log(
                    repo_root,
                    (
                        f"campaign {action}: champion={campaign['champion_run_id']} "
                        f"challenger={challenger_run_id}; {summary or 'no summary'}"
                    ),
                )
                if summary:
                    print(f"autolab campaign: {action} ({summary})")
                else:
                    print(f"autolab campaign: {action}")
                follow_up = _campaign_after_candidate_outcome(
                    state_path,
                    repo_root,
                    updated_campaign,
                    context=action,
                )
                if follow_up is not None:
                    return follow_up
                continue

        loop_exit_code = _cmd_loop(_campaign_loop_args(state_path))

        try:
            campaign = _load_campaign(repo_root)
            if campaign is None:
                print(
                    "autolab campaign: ERROR campaign state disappeared",
                    file=sys.stderr,
                )
                return 1
            if campaign["status"] == "stop_requested":
                _campaign_update_status(repo_root, campaign, status="stopped")
                print("autolab campaign: stop requested acknowledged")
                return 0
            state = _normalize_state(_load_state(state_path))
        except (CampaignError, StateError) as exc:
            return _campaign_fail_control_plane(
                repo_root,
                campaign,
                context="post_loop_state_error",
                message=f"autolab campaign: ERROR {exc}",
            )

        synced_campaign = _campaign_sync_candidate_state(repo_root, state, campaign)
        if synced_campaign != campaign:
            _write_campaign(repo_root, synced_campaign)
            campaign = synced_campaign

        if loop_exit_code != 0:
            if _campaign_has_active_candidate(campaign):
                if str(state.get("stage", "")).strip() == "human_review":
                    try:
                        crash_result = _campaign_apply_crash_outcome(
                            repo_root,
                            state_path,
                            state,
                            campaign,
                            reason=(
                                "challenger escalated to human_review during unattended campaign"
                            ),
                            category="human_review_discard",
                        )
                    except CampaignError as exc:
                        return _campaign_fail_control_plane(
                            repo_root,
                            campaign,
                            context="loop_human_review_discard_error",
                            message=f"autolab campaign: ERROR {exc}",
                        )
                    updated_campaign = crash_result.get("campaign")
                    if not isinstance(updated_campaign, dict):
                        updated_campaign = campaign
                    follow_up = _campaign_after_candidate_outcome(
                        state_path,
                        repo_root,
                        updated_campaign,
                        context="loop_human_review_discard",
                    )
                    if follow_up is not None:
                        return follow_up
                    continue

                governance = _load_campaign_governance_config(repo_root)
                updated_campaign = _campaign_bump_active_candidate_fix_attempts(
                    campaign
                )
                candidate = updated_campaign.get("active_candidate")
                if not isinstance(candidate, dict):
                    candidate = {}
                fix_attempts = int(candidate.get("fix_attempts", 0) or 0)
                run_id = str(candidate.get("run_id", "")).strip()
                if fix_attempts <= governance.max_fix_attempts_per_idea:
                    updated_campaign = _campaign_set_last_governance_event(
                        updated_campaign,
                        category="retry_candidate",
                        run_id=run_id,
                        reason=(
                            "recoverable challenger failure; retrying same idea "
                            f"({fix_attempts}/{governance.max_fix_attempts_per_idea})"
                        ),
                    )
                    updated_campaign = _campaign_sync_active_idea_journal(
                        repo_root,
                        state,
                        updated_campaign,
                    )
                    _write_campaign(repo_root, updated_campaign)
                    _append_log(
                        repo_root,
                        (
                            "campaign retry: "
                            f"run_id={run_id or 'pending'} "
                            f"fix_attempts={fix_attempts}/"
                            f"{governance.max_fix_attempts_per_idea}"
                        ),
                    )
                    continue

                try:
                    crash_result = _campaign_apply_crash_outcome(
                        repo_root,
                        state_path,
                        state,
                        campaign,
                        reason=(
                            "challenger exhausted fix-attempt budget "
                            f"({fix_attempts - 1}/{governance.max_fix_attempts_per_idea})"
                        ),
                        category="crash_discard",
                        run_id=run_id,
                    )
                except CampaignError as exc:
                    return _campaign_fail_control_plane(
                        repo_root,
                        campaign,
                        context="loop_crash_discard_error",
                        message=f"autolab campaign: ERROR {exc}",
                    )
                updated_campaign = crash_result.get("campaign")
                if not isinstance(updated_campaign, dict):
                    updated_campaign = campaign
                follow_up = _campaign_after_candidate_outcome(
                    state_path,
                    repo_root,
                    updated_campaign,
                    context="crash_discard",
                )
                if follow_up is not None:
                    return follow_up
                continue

            return _campaign_fail_control_plane(
                repo_root,
                campaign,
                context="loop_exit_error",
                message=(
                    "autolab campaign: ERROR loop exited before campaign could continue"
                ),
            )

        handoff_payload, error_code = _campaign_refresh_handoff_required(
            state_path,
            repo_root,
            campaign,
            context="handoff_refresh",
        )
        if error_code is not None:
            return error_code
        assert handoff_payload is not None

        rethink_reason = _campaign_rethink_reason(state, handoff_payload)
        if not rethink_reason and _campaign_lock_mode(campaign) != "none":
            stage_name = str(state.get("stage", "")).strip()
            if stage_name == "hypothesis":
                rethink_reason = "locked campaign cannot reopen hypothesis"
            elif stage_name == "design":
                rethink_reason = "locked campaign requires redesign before continuing"
        if rethink_reason:
            return _campaign_export_oracle_and_stop(
                state_path,
                repo_root,
                campaign,
                category="needs_rethink",
                reason=rethink_reason,
                context="needs_rethink",
            )

        updated = _campaign_update_status(repo_root, campaign, status="running")
        _campaign_refresh_results_best_effort(
            repo_root,
            updated,
            context="loop_continue",
        )


def _cmd_campaign_start(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    lock_error = _campaign_lock_error(autolab_dir / "lock")
    if lock_error:
        print(f"autolab campaign start: ERROR {lock_error}", file=sys.stderr)
        return 1

    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        print(f"autolab campaign start: ERROR {exc}", file=sys.stderr)
        return 1
    if str(state.get("stage", "")).strip() in TERMINAL_STAGES:
        print(
            "autolab campaign start: ERROR current stage is terminal; "
            "resume the workflow before starting a campaign",
            file=sys.stderr,
        )
        return 1
    if str(state.get("stage", "")).strip() != "decide_repeat":
        print(
            "autolab campaign start: ERROR current stage must be 'decide_repeat' "
            "to seed the accepted champion baseline",
            file=sys.stderr,
        )
        return 1

    try:
        existing = _load_campaign(repo_root)
    except CampaignError as exc:
        print(f"autolab campaign start: ERROR {exc}", file=sys.stderr)
        return 1
    if existing and existing["status"] in {"running", "stop_requested"}:
        print(
            "autolab campaign start: ERROR an active campaign already exists; "
            "stop it before starting a new one",
            file=sys.stderr,
        )
        return 1

    try:
        payload = _create_campaign_payload(
            repo_root,
            state,
            label=str(args.label),
            scope_kind=str(args.scope),
            lock_modes=getattr(args, "lock", []) or (),
        )
        _campaign_seed_champion_snapshot(repo_root, state_path, payload)
        campaign_path = _write_campaign(repo_root, payload)
    except CampaignError as exc:
        print(f"autolab campaign start: ERROR {exc}", file=sys.stderr)
        return 1

    _append_log(
        repo_root,
        (
            "campaign start: "
            f"id={payload['campaign_id']} label={payload['label']} "
            f"scope={payload['scope_kind']} metric={payload['objective_metric']}"
        ),
    )
    _campaign_refresh_results_best_effort(
        repo_root,
        payload,
        context="start",
    )
    print("autolab campaign start")
    print(f"state_file: {state_path}")
    print(f"campaign_file: {campaign_path}")
    print(f"campaign_id: {payload['campaign_id']}")
    print(f"label: {payload['label']}")
    print(f"scope_kind: {payload['scope_kind']}")
    print(f"iteration_id: {payload['iteration_id']}")
    print(f"objective_metric: {payload['objective_metric']}")
    print(f"objective_mode: {payload['objective_mode']}")
    print(f"lock_mode: {_campaign_lock_mode(payload)}")
    print(f"champion_run_id: {payload['champion_run_id']}")
    print(f"champion_revision_label: {payload['champion_revision_label']}")
    return _run_campaign_session(state_path)


def _cmd_campaign_status(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    print("autolab campaign status")
    print(f"state_file: {state_path}")

    try:
        campaign = _load_campaign(repo_root)
    except CampaignError as exc:
        print(f"autolab campaign status: ERROR {exc}", file=sys.stderr)
        return 1

    if campaign is None:
        print(f"campaign_file: {_campaign_path(repo_root)}")
        print("status: none")
        return 0

    summary = _campaign_summary_with_governance(repo_root, campaign)
    print(f"campaign_file: {_campaign_path(repo_root)}")
    lock_overview: dict[str, object] = {}
    try:
        state = _normalize_state(_load_state(state_path))
    except StateError:
        state = {}
    if state:
        lock_overview = _campaign_lock_overview(repo_root, state, campaign)

    for key in (
        "campaign_id",
        "label",
        "scope_kind",
        "iteration_id",
        "objective_metric",
        "objective_mode",
        "status",
        "lock_mode",
        "design_locked",
        "harness_locked",
        "champion_run_id",
        "champion_revision_label",
        "no_improvement_streak",
        "crash_streak",
        "started_at",
        "last_oracle_at",
        "max_fix_attempts_per_idea",
        "max_timeout_factor",
        "max_no_improvement_streak",
        "max_crash_streak_before_rethink",
        "active_candidate_decision",
        "active_candidate_started_at",
        "active_candidate_run_id",
        "active_candidate_fix_attempts",
        "active_candidate_timeout_reference_seconds",
        "last_governance_event_at",
        "last_governance_event_category",
        "last_governance_event_run_id",
        "last_governance_event_reason",
        "idea_journal_entry_count",
        "idea_journal_family_count",
        "idea_journal_active_family",
        "idea_journal_active_thesis",
        "idea_journal_same_family_streak",
        "idea_journal_last_completed_status",
        "idea_journal_last_completed_family",
        "idea_journal_recent_failed_families",
        "idea_journal_recent_near_miss_families",
        "resumable",
    ):
        print(f"{key}: {summary.get(key, '')}")
    if lock_overview:
        print(f"lock_ok: {lock_overview.get('lock_ok', True)}")
        print(f"lock_drift: {lock_overview.get('lock_drift', '') or 'none'}")
        print(f"lock_summary: {lock_overview.get('lock_summary', '')}")
    results = _campaign_results_overview(repo_root, campaign)
    if results.get("diagnostic"):
        print(f"results: unavailable ({results['diagnostic']})")
    else:
        print(
            "results_tsv: "
            f"{results.get('results_tsv_path', '') or 'missing'} "
            f"[{'present' if results.get('results_tsv_exists', False) else 'missing'}]"
        )
        print(
            "results_md: "
            f"{results.get('results_md_path', '') or 'missing'} "
            f"[{'present' if results.get('results_md_exists', False) else 'missing'}]"
        )
        counts = results.get("counts", {})
        if isinstance(counts, dict):
            print(
                "results_counts: "
                f"keep={int(counts.get('keep', 0) or 0)} "
                f"discard={int(counts.get('discard', 0) or 0)} "
                f"crash={int(counts.get('crash', 0) or 0)} "
                f"partial={int(counts.get('partial', 0) or 0)}"
            )

    lock_info = _inspect_lock(autolab_dir / "lock")
    if lock_info is None:
        print("lock: free")
    else:
        print(
            "lock: held "
            f"pid={lock_info.get('pid', '?')} host={lock_info.get('host', '?')} "
            f"command={lock_info.get('command', '<unknown>')}"
        )

    if state:
        print(f"current_stage: {state.get('stage', '')}")
        print(f"current_iteration_id: {state.get('iteration_id', '')}")
        print(f"last_run_id: {state.get('last_run_id', '')}")
    return 0


def _cmd_campaign_stop(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)

    try:
        campaign = _load_campaign(repo_root)
    except CampaignError as exc:
        print(f"autolab campaign stop: ERROR {exc}", file=sys.stderr)
        return 1
    if campaign is None:
        print("autolab campaign stop: ERROR no active campaign", file=sys.stderr)
        return 1

    lock_info = _inspect_lock(autolab_dir / "lock")
    if lock_info is not None and campaign["status"] in {"running", "stop_requested"}:
        campaign["status"] = "stop_requested"
        _write_campaign(repo_root, campaign)
        _append_log(repo_root, f"campaign stop requested: {campaign['campaign_id']}")
        _campaign_refresh_results_best_effort(
            repo_root,
            campaign,
            context="stop_requested_command",
        )
        print("autolab campaign stop")
        print(f"campaign_id: {campaign['campaign_id']}")
        print("status: stop_requested")
        return 0

    if campaign["status"] in {"stopped", "needs_rethink"}:
        print("autolab campaign stop")
        print(f"campaign_id: {campaign['campaign_id']}")
        print(f"status: {campaign['status']}")
        return 0

    campaign["status"] = "stopped"
    _write_campaign(repo_root, campaign)
    _append_log(repo_root, f"campaign stopped: {campaign['campaign_id']}")
    _campaign_refresh_results_best_effort(
        repo_root,
        campaign,
        context="stop_command",
    )
    print("autolab campaign stop")
    print(f"campaign_id: {campaign['campaign_id']}")
    print("status: stopped")
    return 0


def _cmd_campaign_continue(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    lock_error = _campaign_lock_error(autolab_dir / "lock")
    if lock_error:
        print(f"autolab campaign continue: ERROR {lock_error}", file=sys.stderr)
        return 1

    try:
        campaign = _load_campaign(repo_root)
    except CampaignError as exc:
        print(f"autolab campaign continue: ERROR {exc}", file=sys.stderr)
        return 1
    if campaign is None:
        print("autolab campaign continue: ERROR no active campaign", file=sys.stderr)
        return 1
    if not _campaign_is_resumable(campaign):
        print(
            "autolab campaign continue: ERROR campaign is not resumable "
            f"(status={campaign['status']})",
            file=sys.stderr,
        )
        return 1

    try:
        state = _normalize_state(_load_state(state_path))
        _validate_campaign_binding(repo_root, state, campaign)
    except (CampaignError, StateError) as exc:
        print(f"autolab campaign continue: ERROR {exc}", file=sys.stderr)
        return 1
    if str(state.get("stage", "")).strip() in TERMINAL_STAGES:
        print(
            "autolab campaign continue: ERROR current stage is terminal; "
            "resolve workflow state before resuming campaign",
            file=sys.stderr,
        )
        return 1
    if _campaign_lock_mode(campaign) != "none" and not _campaign_has_lock_contract(
        campaign
    ):
        if (
            str(state.get("stage", "")).strip() == "decide_repeat"
            and str(state.get("last_run_id", "")).strip() == campaign["champion_run_id"]
        ):
            try:
                campaign = _campaign_backfill_lock_contract(repo_root, state, campaign)
            except CampaignError as exc:
                campaign["status"] = "needs_rethink"
                _write_campaign(repo_root, campaign)
                print(f"autolab campaign continue: ERROR {exc}", file=sys.stderr)
                return 1
        else:
            campaign["status"] = "needs_rethink"
            _write_campaign(repo_root, campaign)
            print(
                "autolab campaign continue: ERROR locked campaign is missing its "
                "lock contract and must be reseeded from decide_repeat",
                file=sys.stderr,
            )
            return 1
    lock_overview = _campaign_lock_overview(repo_root, state, campaign)
    if not bool(lock_overview.get("lock_ok", True)):
        campaign["status"] = "needs_rethink"
        _write_campaign(repo_root, campaign)
        print(
            "autolab campaign continue: ERROR "
            f"{lock_overview.get('lock_drift', 'campaign lock drift detected')}",
            file=sys.stderr,
        )
        return 1
    if not _campaign_has_champion_snapshot(repo_root, campaign):
        if (
            str(state.get("stage", "")).strip() == "decide_repeat"
            and str(state.get("last_run_id", "")).strip() == campaign["champion_run_id"]
        ):
            try:
                _campaign_seed_champion_snapshot(repo_root, state_path, campaign)
            except CampaignError as exc:
                campaign["status"] = "needs_rethink"
                _write_campaign(repo_root, campaign)
                print(f"autolab campaign continue: ERROR {exc}", file=sys.stderr)
                return 1
        else:
            campaign["status"] = "needs_rethink"
            _write_campaign(repo_root, campaign)
            print(
                "autolab campaign continue: ERROR campaign is missing its "
                "champion snapshot and must be reseeded from decide_repeat",
                file=sys.stderr,
            )
            return 1

    campaign["status"] = "running"
    _write_campaign(repo_root, campaign)
    _append_log(repo_root, f"campaign continue: {campaign['campaign_id']}")
    _campaign_refresh_results_best_effort(
        repo_root,
        campaign,
        context="continue",
    )
    print("autolab campaign continue")
    print(f"state_file: {state_path}")
    print(f"campaign_id: {campaign['campaign_id']}")
    print(f"label: {campaign['label']}")
    return _run_campaign_session(state_path)


__all__ = [name for name in globals() if not name.startswith("__")]
