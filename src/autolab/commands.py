from __future__ import annotations

import argparse
import importlib.resources as importlib_resources
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from autolab.constants import (
    DECISION_STAGES,
    DEFAULT_BACKLOG_TEMPLATE,
    DEFAULT_EXPERIMENT_TYPE,
    DEFAULT_MAX_HOURS,
    DEFAULT_VERIFIER_POLICY,
    LOCK_STALE_SECONDS,
    STAGE_PROMPT_FILES,
    TERMINAL_STAGES,
)
from autolab.models import RunOutcome, StateError
from autolab.config import _resolve_run_agent_mode
from autolab.run_standard import _run_once_standard
from autolab.run_assistant import _run_once_assistant
from autolab.state import (
    _acquire_lock,
    _bootstrap_iteration_id,
    _default_agent_result,
    _default_state,
    _heartbeat_lock,
    _load_state,
    _mark_backlog_experiment_completed,
    _normalize_state,
    _parse_iteration_from_backlog,
    _release_lock,
    _resolve_autolab_dir,
    _resolve_repo_root,
    _resolve_scaffold_source,
    _sync_scaffold_bundle,
    _resolve_experiment_type_from_backlog,
    _ensure_iteration_skeleton,
)
from autolab.utils import (
    _append_log,
    _collect_change_snapshot,
    _ensure_json_file,
    _ensure_text_file,
    _outcome_payload,
    _persist_agent_result,
    _prepare_standard_commit_outcome,
    _safe_todo_pre_sync,
    _todo_open_count,
    _try_auto_commit,
    _utc_now,
    _write_json,
)
from autolab.prompts import _default_stage_prompt_text
from autolab.validators import _run_verification_step_detailed
from autolab.slurm_job_list import (
    append_entry_idempotent,
    canonical_slurm_job_bullet,
    is_slurm_manifest,
    ledger_contains_entry,
    ledger_contains_run_id,
    required_run_id,
    required_slurm_job_id,
)


# ---------------------------------------------------------------------------
# Skill installer helpers
# ---------------------------------------------------------------------------

def _load_packaged_skill_template_text(provider: str) -> str:
    normalized_provider = str(provider).strip().lower()
    if normalized_provider != "codex":
        raise RuntimeError(f"unsupported skill provider '{provider}'")

    resource = importlib_resources.files("autolab").joinpath(
        "skills",
        normalized_provider,
        "autolab",
        "SKILL.md",
    )
    if not resource.is_file():
        raise RuntimeError(
            "bundled skill template is unavailable at package://autolab/skills/codex/autolab/SKILL.md"
        )
    return resource.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Internal helper: dispatch a single run to standard or assistant runner
# ---------------------------------------------------------------------------

def _run_once(
    state_path: Path,
    decision: str | None,
    *,
    run_agent_mode: str = "policy",
    verify_before_evaluate: bool = False,
    assistant: bool = False,
    auto_mode: bool = False,
    auto_decision: bool = False,
    strict_implementation_progress: bool = True,
) -> RunOutcome:
    if assistant:
        return _run_once_assistant(state_path, run_agent_mode=run_agent_mode, auto_mode=auto_mode)
    return _run_once_standard(
        state_path,
        decision,
        run_agent_mode=run_agent_mode,
        verify_before_evaluate=verify_before_evaluate,
        auto_decision=auto_decision,
        auto_mode=auto_mode,
        strict_implementation_progress=strict_implementation_progress,
    )


# ---------------------------------------------------------------------------
# Overnight summary helper (used by _cmd_loop)
# ---------------------------------------------------------------------------

def _write_overnight_summary(
    repo_root: Path,
    *,
    state_path: Path,
    started_at: str,
    ended_at: str,
    elapsed_seconds: float,
    max_iterations: int,
    max_hours: float,
    auto_decision_count: int,
    retry_escalation_count: int,
    todo_open_before: int,
    todo_open_after: int,
    terminal_reason: str,
    final_stage: str,
    exit_code: int,
    rows: list[dict[str, Any]],
) -> Path:
    summary_path = repo_root / ".autolab" / "logs" / "overnight_summary.md"
    lines = [
        "# Overnight Autolab Summary",
        "",
        f"- started_at: `{started_at}`",
        f"- ended_at: `{ended_at}`",
        f"- elapsed_seconds: `{elapsed_seconds:.2f}`",
        f"- state_file: `{state_path}`",
        f"- max_iterations: `{max_iterations}`",
        f"- max_hours: `{max_hours}`",
        f"- auto_decisions: `{auto_decision_count}`",
        f"- retry_escalations: `{retry_escalation_count}`",
        f"- todo_open_before: `{todo_open_before}`",
        f"- todo_open_after: `{todo_open_after}`",
        f"- terminal_reason: `{terminal_reason}`",
        f"- final_stage: `{final_stage}`",
        f"- exit_code: `{exit_code}`",
        "",
        "## Iterations",
    ]
    if rows:
        lines.extend(
            [
                "| i | before | after | transitioned | exit | decision | message |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for row in rows:
            lines.append(
                "| {i} | {before} | {after} | {transitioned} | {exit} | {decision} | {message} |".format(
                    i=row.get("index", ""),
                    before=str(row.get("stage_before", "")).replace("|", "/"),
                    after=str(row.get("stage_after", "")).replace("|", "/"),
                    transitioned=row.get("transitioned", ""),
                    exit=row.get("exit_code", ""),
                    decision=str(row.get("decision", "-")).replace("|", "/"),
                    message=str(row.get("message", "")).replace("|", "/"),
                )
            )
    else:
        lines.append("No iterations were executed.")

    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return summary_path


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------

def _cmd_status(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    try:
        state = _load_state(state_path)
    except RuntimeError as exc:
        print(f"autolab status: ERROR {exc}", file=sys.stderr)
        return 1

    print("autolab status")
    print(f"state_file: {state_path}")
    for key in (
        "iteration_id",
        "experiment_id",
        "stage",
        "stage_attempt",
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

    # Phase 7b: human_review banner
    if state.get("stage") == "human_review":
        print("\n*** HUMAN REVIEW REQUIRED ***")
        print("Run `autolab review --status=pass|retry|stop` to record your decision.")

    return 0


def _cmd_sync_scaffold(args: argparse.Namespace) -> int:
    try:
        source_root = _resolve_scaffold_source()
    except RuntimeError as exc:
        print(f"autolab sync-scaffold: ERROR {exc}", file=sys.stderr)
        return 1

    destination = Path(args.dest).expanduser().resolve()
    copied, skipped = _sync_scaffold_bundle(
        source_root,
        destination,
        overwrite=bool(args.force),
    )
    print("autolab sync-scaffold")
    print(f"source: {source_root}")
    print(f"destination: {destination}")
    print(f"copied_files: {copied}")
    print(f"skipped_files: {skipped}")
    if not args.force and skipped and copied == 0:
        print("No files copied. Add --force to overwrite existing files.")
    return 0


def _cmd_install_skill(args: argparse.Namespace) -> int:
    provider = str(getattr(args, "provider", "")).strip().lower()
    project_root = Path(getattr(args, "project_root", ".")).expanduser().resolve()
    destination = project_root / ".codex" / "skills" / "autolab" / "SKILL.md"

    try:
        template_text = _load_packaged_skill_template_text(provider)
    except Exception as exc:
        print(f"autolab install-skill: ERROR {exc}", file=sys.stderr)
        return 1

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(template_text, encoding="utf-8")
    except Exception as exc:
        print(f"autolab install-skill: ERROR writing {destination}: {exc}", file=sys.stderr)
        return 1

    print("autolab install-skill")
    print(f"provider: {provider}")
    print("source: package://autolab/skills/codex/autolab/SKILL.md")
    print(f"destination: {destination}")
    print("status: installed (overwritten if existing)")
    return 0


def _cmd_slurm_job_list(args: argparse.Namespace) -> int:
    action = str(getattr(args, "action", "")).strip().lower()
    manifest_path = Path(args.manifest).expanduser()
    doc_path = Path(args.doc).expanduser()
    if action not in {"append", "verify"}:
        print(
            f"autolab slurm-job-list: invalid action '{action}' (expected append|verify)",
            file=sys.stderr,
        )
        return 1

    try:
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"autolab slurm-job-list: ERROR loading manifest {manifest_path}: {exc}", file=sys.stderr)
        return 1
    if not isinstance(manifest_payload, dict):
        print(f"autolab slurm-job-list: ERROR manifest {manifest_path} must be a JSON object", file=sys.stderr)
        return 1

    if action == "append":
        try:
            if not is_slurm_manifest(manifest_payload):
                print(
                    f"autolab slurm-job-list: manifest is non-SLURM; append skipped for {manifest_path}"
                )
                return 0
            if doc_path.parent != manifest_path.parent:
                doc_path.parent.mkdir(parents=True, exist_ok=True)
            run_id = required_run_id(manifest_payload)
            canonical = canonical_slurm_job_bullet(manifest_payload)
            existing_text = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""
            next_text, updated = append_entry_idempotent(existing_text, canonical, run_id)
            if updated:
                doc_path.write_text(next_text, encoding="utf-8")
                print(f"autolab slurm-job-list: appended run_id={run_id} -> {doc_path}")
            else:
                print(f"autolab slurm-job-list: run_id={run_id} already present in {doc_path}")
            return 0
        except Exception as exc:
            print(f"autolab slurm-job-list: ERROR {exc}", file=sys.stderr)
            return 1

    try:
        if not is_slurm_manifest(manifest_payload):
            print(f"autolab slurm-job-list: manifest is non-SLURM; verify skipped for {manifest_path}")
            return 0
        run_id = required_run_id(manifest_payload)
        job_id = required_slurm_job_id(manifest_payload)
        expected = canonical_slurm_job_bullet(manifest_payload)
        ledger_text = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""
        if not ledger_contains_entry(ledger_text, expected):
            print(
                f"autolab slurm-job-list: FAIL run_id={run_id}, job_id={job_id}, missing ledger entry in {doc_path}"
            )
            return 1
        print(f"autolab slurm-job-list: PASS job_id={job_id}, run_id={run_id}")
        return 0
    except Exception as exc:
        print(f"autolab slurm-job-list: ERROR verifying {manifest_path}: {exc}", file=sys.stderr)
        return 1


def _cmd_init(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = repo_root / ".autolab"
    created: list[Path] = []

    for directory in (
        autolab_dir,
        autolab_dir / "logs",
        autolab_dir / "logs" / "iterations",
        autolab_dir / "prompts" / "shared",
        autolab_dir / "schemas",
        autolab_dir / "verifiers",
        repo_root / "experiments",
    ):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(directory)

    backlog_path = autolab_dir / "backlog.yaml"
    verifier_policy_path = autolab_dir / "verifier_policy.yaml"
    agent_result_path = autolab_dir / "agent_result.json"
    scaffold_copied = 0
    scaffold_skipped = 0

    try:
        scaffold_source = _resolve_scaffold_source()
    except RuntimeError:
        scaffold_source = None
    if scaffold_source is not None:
        copied, skipped = _sync_scaffold_bundle(scaffold_source, autolab_dir, overwrite=False)
        scaffold_copied = copied
        scaffold_skipped = skipped

    iteration_id = ""
    if state_path.exists():
        try:
            state = _normalize_state(_load_state(state_path))
        except StateError as exc:
            print(f"autolab init: ERROR {exc}", file=sys.stderr)
            return 1
        iteration_id = state["iteration_id"]
    else:
        iteration_id = _parse_iteration_from_backlog(backlog_path)
        if not iteration_id:
            iteration_id = _bootstrap_iteration_id()
        _ensure_json_file(state_path, _default_state(iteration_id), created)

    _ensure_text_file(backlog_path, DEFAULT_BACKLOG_TEMPLATE.format(iteration_id=iteration_id), created)
    _ensure_text_file(verifier_policy_path, DEFAULT_VERIFIER_POLICY, created)
    _ensure_json_file(agent_result_path, _default_agent_result(), created)
    if scaffold_source is None:
        for stage, prompt_file in STAGE_PROMPT_FILES.items():
            _ensure_text_file(
                autolab_dir / "prompts" / prompt_file,
                _default_stage_prompt_text(stage),
                created,
            )
    init_experiment_type = _resolve_experiment_type_from_backlog(
        repo_root,
        iteration_id=iteration_id,
        experiment_id="",
    ) or DEFAULT_EXPERIMENT_TYPE
    _ensure_iteration_skeleton(
        repo_root,
        iteration_id,
        created,
        experiment_type=init_experiment_type,
    )
    try:
        init_state = _normalize_state(_load_state(state_path))
    except StateError:
        init_state = None
    todo_sync_changed, _ = _safe_todo_pre_sync(repo_root, init_state)
    for path in todo_sync_changed:
        if path not in created:
            created.append(path)

    _append_log(repo_root, f"init completed for iteration {iteration_id}; created={len(created)}")

    print("autolab init")
    print(f"state_file: {state_path}")
    print(f"iteration_id: {iteration_id}")
    print(f"created_entries: {len(created)}")
    print(f"scaffold_copied_files: {scaffold_copied}")
    print(f"scaffold_skipped_files: {scaffold_skipped}")
    for path in created:
        print(f"- {path}")

    # Phase 7c: placeholder detection reminder
    print("\nReminder: Review and customize the following before your first run:")
    print("  - .autolab/backlog.yaml (update hypothesis titles and metrics)")
    print("  - .autolab/prompts/stage_*.md (add project-specific instructions)")

    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)

    try:
        source_root = _resolve_scaffold_source()
    except RuntimeError as exc:
        print(f"autolab reset: ERROR {exc}", file=sys.stderr)
        return 1

    if autolab_dir.exists():
        try:
            shutil.rmtree(autolab_dir)
        except Exception as exc:
            print(f"autolab reset: ERROR removing {autolab_dir}: {exc}", file=sys.stderr)
            return 1

    copied, skipped = _sync_scaffold_bundle(
        source_root,
        autolab_dir,
        overwrite=True,
    )
    backlog_path = autolab_dir / "backlog.yaml"
    iteration_id = _parse_iteration_from_backlog(backlog_path)
    if not iteration_id:
        iteration_id = _bootstrap_iteration_id()

    try:
        _write_json(state_path, _default_state(iteration_id))
    except OSError as exc:
        print(f"autolab reset: ERROR writing state file {state_path}: {exc}", file=sys.stderr)
        return 1

    print("autolab reset")
    print(f"state_file: {state_path}")
    print(f"autolab_dir: {autolab_dir}")
    print(f"copied_files: {copied}")
    print(f"skipped_files: {skipped}")
    return 0


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
    effective_stage = str(details.get("stage", "")).strip() or str(state.get("stage", "")).strip() or "unknown"
    safe_stage = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in effective_stage) or "unknown"
    timestamp = _utc_now().replace("-", "").replace(":", "").replace(".", "")
    summary_path = repo_root / ".autolab" / "logs" / f"verification_{timestamp}_{safe_stage}.json"
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
    _append_log(
        repo_root,
        (
            f"verify stage={effective_stage} passed={passed} "
            f"summary={summary_path} message={message}"
        ),
    )

    print("autolab verify")
    print(f"state_file: {state_path}")
    print(f"stage: {effective_stage}")
    print(f"passed: {passed}")
    print(f"message: {message}")
    print(f"result: {canonical_result_path}")
    print(f"summary: {summary_path}")
    if not passed:
        print(f"autolab verify: ERROR {message}", file=sys.stderr)
        return 1
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    run_agent_mode = _resolve_run_agent_mode(getattr(args, "run_agent_mode", "policy"))
    assistant_mode = bool(getattr(args, "assistant", False))
    baseline_snapshot = _collect_change_snapshot(repo_root)
    outcome = _run_once(
        state_path,
        args.decision,
        run_agent_mode=run_agent_mode,
        verify_before_evaluate=bool(getattr(args, "verify", False)),
        assistant=assistant_mode,
        auto_mode=False,
        auto_decision=bool(getattr(args, "auto_decision", False)),
        strict_implementation_progress=bool(getattr(args, "strict_implementation_progress", True)),
    )
    commit_outcome = _prepare_standard_commit_outcome(
        repo_root,
        outcome,
        baseline_snapshot,
        assistant=assistant_mode,
        strict_implementation_progress=bool(getattr(args, "strict_implementation_progress", True)),
    )
    commit_summary = _try_auto_commit(repo_root, outcome=commit_outcome)
    print("autolab run")
    print(f"state_file: {state_path}")
    print(f"run_agent_mode: {run_agent_mode}")
    print(f"assistant: {bool(getattr(args, 'assistant', False))}")
    print(f"verify_before_evaluate: {bool(getattr(args, 'verify', False))}")
    print(f"auto_decision: {bool(getattr(args, 'auto_decision', False))}")
    print(f"stage_before: {outcome.stage_before}")
    print(f"stage_after: {outcome.stage_after}")
    print(f"transitioned: {outcome.transitioned}")
    print(f"message: {outcome.message}")
    print(commit_summary)
    if outcome.exit_code != 0:
        print(f"autolab run: ERROR {outcome.message}", file=sys.stderr)

        # Phase 7a: manual mode hint
        stage = outcome.stage_before
        prompt_file = STAGE_PROMPT_FILES.get(stage)
        if prompt_file:
            print(f"\nHint: Follow instructions in .autolab/prompts/{prompt_file} to complete the '{stage}' stage.")

    return outcome.exit_code


def _cmd_loop(args: argparse.Namespace) -> int:
    if args.max_iterations <= 0:
        print("autolab loop: ERROR --max-iterations must be > 0", file=sys.stderr)
        return 2
    if args.auto and args.max_hours <= 0:
        print("autolab loop: ERROR --max-hours must be > 0 when --auto is enabled", file=sys.stderr)
        return 2

    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    effective_max_iterations = int(args.max_iterations)
    try:
        state_for_limit = _normalize_state(_load_state(state_path))
        state_limit = int(state_for_limit.get("max_total_iterations", effective_max_iterations))
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
    overall_exit_code = 0
    lock_acquired = False

    print("autolab loop")
    print(f"state_file: {state_path}")
    print(f"max_iterations: {effective_max_iterations}")
    if effective_max_iterations != int(args.max_iterations):
        print(f"max_iterations_clamped_by_state: {state_for_limit['max_total_iterations']}")
    run_agent_mode = _resolve_run_agent_mode(getattr(args, "run_agent_mode", "policy"))
    auto_decision_enabled = bool(args.auto or run_agent_mode == "force_on")
    assistant_mode = bool(getattr(args, "assistant", False))
    print(f"run_agent_mode: {run_agent_mode}")
    print(f"assistant: {assistant_mode}")
    print(f"verify_before_evaluate: {bool(getattr(args, 'verify', False))}")
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
        _append_log(repo_root, f"auto loop lock acquired: {lock_msg}")

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
                strict_implementation_progress=bool(getattr(args, "strict_implementation_progress", True)),
            )
            commit_outcome = _prepare_standard_commit_outcome(
                repo_root,
                outcome,
                baseline_snapshot,
                assistant=assistant_mode,
                strict_implementation_progress=bool(getattr(args, "strict_implementation_progress", True)),
            )
            commit_summary = _try_auto_commit(repo_root, outcome=commit_outcome)
            if "escalating to human_review" in outcome.message:
                retry_escalation_count += 1
            loop_rows.append(
                {
                    "index": index,
                    "stage_before": outcome.stage_before,
                    "stage_after": outcome.stage_after,
                    "transitioned": outcome.transitioned,
                    "exit_code": outcome.exit_code,
                    "decision": "auto" if args.auto and current_stage == "decide_repeat" else "-",
                    "message": outcome.message,
                }
            )
            print(
                f"iteration {index}: {outcome.stage_before} -> {outcome.stage_after} "
                f"(transitioned={outcome.transitioned}, exit={outcome.exit_code})"
            )
            print(f"iteration {index}: {commit_summary}")
            if outcome.exit_code != 0:
                print(f"autolab loop: ERROR {outcome.message}", file=sys.stderr)
                overall_exit_code = outcome.exit_code
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
            if not outcome.transitioned:
                if assistant_mode and outcome.exit_code == 0:
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
                    todo_open_before=todo_open_before,
                    todo_open_after=todo_open_after,
                    terminal_reason=terminal_reason,
                    final_stage=final_stage,
                    exit_code=overall_exit_code,
                    rows=loop_rows,
                )
            except Exception as exc:
                print(f"autolab loop: WARN failed to write overnight summary: {exc}", file=sys.stderr)
            if lock_acquired:
                _release_lock(lock_path)


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
        print(f"autolab review: ERROR current stage is '{state['stage']}', not 'human_review'", file=sys.stderr)
        return 1
    status = args.status
    if status == "pass":
        state["stage"] = "launch"
        state["stage_attempt"] = 0
        message = "review decision: pass — advancing to launch"
    elif status == "retry":
        state["stage"] = "implementation"
        state["stage_attempt"] = 0
        message = "review decision: retry — returning to implementation"
    elif status == "stop":
        state["stage"] = "stop"
        state["stage_attempt"] = 0
        state["current_task_id"] = ""
        state["task_cycle_stage"] = "done"
        state["task_change_baseline"] = {}
        completed, backlog_path, completion_summary = _mark_backlog_experiment_completed(
            repo_root, str(state.get("experiment_id", "")).strip(),
        )
        message = f"review decision: stop — experiment ended"
        if completed:
            message = f"{message}; {completion_summary}"
    else:
        print(f"autolab review: ERROR invalid status '{status}'", file=sys.stderr)
        return 1
    _write_json(state_path, state)
    _persist_agent_result(repo_root, status="complete", summary=message, changed_files=[state_path])
    _append_log(repo_root, f"review command: {message}")
    print(f"autolab review: {message}")
    return 0


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="autolab command line interface")
    subparsers = parser.add_subparsers(dest="command")

    init = subparsers.add_parser("init", help="Initialize autolab scaffold and state files")
    init.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    init.set_defaults(handler=_cmd_init)

    reset = subparsers.add_parser("reset", help="Reset autolab scaffold and state to defaults")
    reset.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    reset.set_defaults(handler=_cmd_reset)

    verify = subparsers.add_parser("verify", help="Run stage-relevant verifier checks and write a summary artifact")
    verify.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    verify.add_argument(
        "--stage",
        default=None,
        help="Override stage for verification command resolution (default: state.stage)",
    )
    verify.set_defaults(handler=_cmd_verify)

    run = subparsers.add_parser("run", help="Run one deterministic stage transition")
    run.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    run.add_argument(
        "--decision",
        choices=DECISION_STAGES,
        default=None,
        help="Manual decision target when current stage is decide_repeat",
    )
    run.add_argument(
        "--assistant",
        action="store_true",
        help="Enable engineer-assistant task cycle mode for this run.",
    )
    run.add_argument(
        "--auto-decision",
        action="store_true",
        help="Allow decide_repeat to auto-select from todo/backlog when --decision is not provided.",
    )
    run.add_argument(
        "--verify",
        action="store_true",
        help="Run policy-driven verification before stage evaluation in this run.",
    )
    run.add_argument(
        "--strict-implementation-progress",
        dest="strict_implementation_progress",
        action="store_true",
        help="Require meaningful implementation progress checks (default).",
    )
    run.add_argument(
        "--no-strict-implementation-progress",
        dest="strict_implementation_progress",
        action="store_false",
        help="Disable meaningful implementation progress checks.",
    )
    run_runner_group = run.add_mutually_exclusive_group()
    run_runner_group.add_argument(
        "--run-agent",
        dest="run_agent_mode",
        action="store_const",
        const="force_on",
        help="Force agent_runner invocation for eligible stages.",
    )
    run_runner_group.add_argument(
        "--no-run-agent",
        dest="run_agent_mode",
        action="store_const",
        const="force_off",
        help="Disable agent_runner invocation even if enabled in policy.",
    )
    run.set_defaults(run_agent_mode="policy")
    run.set_defaults(strict_implementation_progress=True)
    run.set_defaults(handler=_cmd_run)

    loop = subparsers.add_parser("loop", help="Run bounded stage transitions in sequence")
    loop.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    loop.add_argument(
        "--max-iterations",
        type=int,
        default=1,
        help="Maximum number of run iterations to execute (must be > 0)",
    )
    loop.add_argument(
        "--auto",
        action="store_true",
        help="Enable unattended loop mode with automatic decide_repeat decisions and lock enforcement.",
    )
    loop.add_argument(
        "--assistant",
        action="store_true",
        help="Enable engineer-assistant task cycle mode for unattended feature delivery.",
    )
    loop.add_argument(
        "--verify",
        action="store_true",
        help="Run policy-driven verification before stage evaluation on each loop iteration.",
    )
    loop.add_argument(
        "--strict-implementation-progress",
        dest="strict_implementation_progress",
        action="store_true",
        help="Require meaningful implementation progress checks (default).",
    )
    loop.add_argument(
        "--no-strict-implementation-progress",
        dest="strict_implementation_progress",
        action="store_false",
        help="Disable meaningful implementation progress checks.",
    )
    loop.add_argument(
        "--max-hours",
        type=float,
        default=DEFAULT_MAX_HOURS,
        help="Maximum wall-clock runtime in hours for --auto mode (must be > 0).",
    )
    loop_runner_group = loop.add_mutually_exclusive_group()
    loop_runner_group.add_argument(
        "--run-agent",
        dest="run_agent_mode",
        action="store_const",
        const="force_on",
        help="Force agent_runner invocation for eligible stages.",
    )
    loop_runner_group.add_argument(
        "--no-run-agent",
        dest="run_agent_mode",
        action="store_const",
        const="force_off",
        help="Disable agent_runner invocation even if enabled in policy.",
    )
    loop.set_defaults(run_agent_mode="policy")
    loop.set_defaults(strict_implementation_progress=True)
    loop.set_defaults(handler=_cmd_loop)

    status = subparsers.add_parser("status", help="Show current .autolab state")
    status.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    status.set_defaults(handler=_cmd_status)

    sync_scaffold = subparsers.add_parser(
        "sync-scaffold",
        help="Sync bundled autolab scaffold files into the repository",
    )
    sync_scaffold.add_argument(
        "--dest",
        default=".autolab",
        help="Target directory for scaffold files (default: .autolab)",
    )
    sync_scaffold.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing scaffold files.",
    )
    sync_scaffold.set_defaults(handler=_cmd_sync_scaffold)

    install_skill = subparsers.add_parser(
        "install-skill",
        help="Install bundled skill templates into the project-local .codex directory.",
    )
    install_skill.add_argument(
        "provider",
        choices=("codex",),
        help="Skill provider to install (currently only: codex).",
    )
    install_skill.add_argument(
        "--project-root",
        default=".",
        help="Project root where .codex/skills will be created (default: current directory).",
    )
    install_skill.set_defaults(handler=_cmd_install_skill)

    slurm_job_list = subparsers.add_parser(
        "slurm-job-list",
        help="Maintain or verify docs/slurm_job_list.md ledger entries for run manifests.",
    )
    slurm_job_list.add_argument(
        "action",
        choices=("append", "verify"),
        help="Action to perform against a run manifest.",
    )
    slurm_job_list.add_argument(
        "--manifest",
        required=True,
        help="Path to experiments/<type>/<iteration_id>/runs/<run_id>/run_manifest.json",
    )
    slurm_job_list.add_argument(
        "--doc",
        required=True,
        help="Path to docs/slurm_job_list.md.",
    )
    slurm_job_list.set_defaults(handler=_cmd_slurm_job_list)

    # Phase 6b: review subcommand
    review = subparsers.add_parser("review", help="Record a TA/instructor review decision")
    review.add_argument("--state-file", default=".autolab/state.json",
                        help="Path to autolab state JSON (default: .autolab/state.json)")
    review.add_argument("--status", required=True, choices=("pass", "retry", "stop"),
                        help="Review decision: pass (continue), retry (back to implementation), stop (end experiment)")
    review.set_defaults(handler=_cmd_review)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))
