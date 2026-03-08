"""Campaign-mode CLI handlers."""

from __future__ import annotations

from autolab.campaign import (
    CampaignError,
    _campaign_apply_challenger_outcome,
    _campaign_has_champion_snapshot,
    _campaign_is_resumable,
    _campaign_path,
    _campaign_results_overview,
    _campaign_seed_champion_snapshot,
    _campaign_summary,
    _create_campaign_payload,
    _load_campaign,
    _normalize_campaign,
    _refresh_campaign_results,
    _validate_campaign_binding,
    _write_campaign,
)
from autolab.cli.support import *
from autolab.cli.handlers_observe import _safe_refresh_handoff


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
    elif status == "running":
        updated["crash_streak"] = 0
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
            updated = _campaign_update_status(
                repo_root, campaign, status="needs_rethink"
            )
            _campaign_refresh_results_best_effort(
                repo_root,
                updated,
                context="binding_error",
            )
            print(f"autolab campaign: stop ({exc})", file=sys.stderr)
            return 1
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
                    updated = _campaign_update_status(
                        repo_root,
                        campaign,
                        status="needs_rethink",
                    )
                    _campaign_refresh_results_best_effort(
                        repo_root,
                        updated,
                        context="compare_error",
                    )
                    print(f"autolab campaign: stop ({exc})", file=sys.stderr)
                    return 1
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
                _campaign_refresh_results_best_effort(
                    repo_root,
                    updated_campaign,
                    context=action,
                )
                if summary:
                    print(f"autolab campaign: {action} ({summary})")
                else:
                    print(f"autolab campaign: {action}")
                handoff_payload, handoff_error = _safe_refresh_handoff(state_path)
                if handoff_payload is None:
                    updated = _campaign_update_status(
                        repo_root,
                        updated_campaign,
                        status="error",
                        crash_delta=1,
                    )
                    _campaign_refresh_results_best_effort(
                        repo_root,
                        updated,
                        context="handoff_refresh_error_after_compare",
                    )
                    print(
                        f"autolab campaign: ERROR failed to refresh handoff snapshot: {handoff_error}",
                        file=sys.stderr,
                    )
                    return 1
                continue

        loop_exit_code = _cmd_loop(_campaign_loop_args(state_path))

        handoff_payload, handoff_error = _safe_refresh_handoff(state_path)
        if handoff_payload is None:
            updated = _campaign_update_status(
                repo_root,
                campaign,
                status="error",
                crash_delta=1,
            )
            _campaign_refresh_results_best_effort(
                repo_root,
                updated,
                context="handoff_refresh_error",
            )
            print(
                f"autolab campaign: ERROR failed to refresh handoff snapshot: {handoff_error}",
                file=sys.stderr,
            )
            return 1

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
            updated = _campaign_update_status(
                repo_root,
                campaign,
                status="error",
                crash_delta=1,
            )
            _campaign_refresh_results_best_effort(
                repo_root,
                updated,
                context="post_loop_state_error",
            )
            print(f"autolab campaign: ERROR {exc}", file=sys.stderr)
            return 1

        rethink_reason = _campaign_rethink_reason(state, handoff_payload)
        if rethink_reason:
            updated = _campaign_update_status(
                repo_root,
                campaign,
                status="needs_rethink",
            )
            _campaign_refresh_results_best_effort(
                repo_root,
                updated,
                context="needs_rethink",
            )
            print(f"autolab campaign: stop ({rethink_reason})", file=sys.stderr)
            return 1

        if loop_exit_code != 0:
            updated = _campaign_update_status(
                repo_root,
                campaign,
                status="error",
                crash_delta=1,
            )
            _campaign_refresh_results_best_effort(
                repo_root,
                updated,
                context="loop_exit_error",
            )
            print(
                "autolab campaign: ERROR loop exited before campaign could continue",
                file=sys.stderr,
            )
            return int(loop_exit_code or 1)

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

    summary = _campaign_summary(campaign)
    print(f"campaign_file: {_campaign_path(repo_root)}")
    for key in (
        "campaign_id",
        "label",
        "scope_kind",
        "iteration_id",
        "objective_metric",
        "objective_mode",
        "status",
        "design_locked",
        "champion_run_id",
        "champion_revision_label",
        "no_improvement_streak",
        "crash_streak",
        "started_at",
        "last_oracle_at",
        "resumable",
    ):
        print(f"{key}: {summary.get(key, '')}")
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

    try:
        state = _normalize_state(_load_state(state_path))
    except StateError:
        state = {}
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
