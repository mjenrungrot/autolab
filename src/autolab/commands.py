from __future__ import annotations

import argparse
import importlib.resources as importlib_resources
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml_mod
except Exception:
    _yaml_mod = None

from autolab.constants import (
    ACTIVE_STAGES,
    ALL_STAGES,
    DECISION_STAGES,
    DEFAULT_BACKLOG_TEMPLATE,
    DEFAULT_EXPERIMENT_TYPE,
    DEFAULT_MAX_HOURS,
    DEFAULT_VERIFIER_POLICY,
    LOCK_STALE_SECONDS,
    STAGE_PROMPT_FILES,
    TERMINAL_STAGES,
)
from autolab.registry import load_registry, StageSpec
from autolab.models import RunOutcome, StateError
from autolab.config import (
    _load_guardrail_config,
    _load_meaningful_change_config,
    _load_verifier_policy,
    _resolve_policy_python_bin,
    _resolve_run_agent_mode,
)
from autolab.run_standard import _run_once_standard
from autolab.run_assistant import _run_once_assistant
from autolab.state import (
    _acquire_lock,
    _append_state_history,
    _bootstrap_iteration_id,
    _default_agent_result,
    _default_state,
    _find_backlog_experiment_entry,
    _force_break_lock,
    _heartbeat_lock,
    _inspect_lock,
    _load_backlog_yaml,
    _load_state,
    _mark_backlog_experiment_completed,
    _normalize_state,
    _parse_iteration_from_backlog,
    _read_lock_payload,
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
    _load_json_if_exists,
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

POLICY_PRESET_NAMES = ("local_dev", "ci_strict", "slurm")


# ---------------------------------------------------------------------------
# Skill installer helpers
# ---------------------------------------------------------------------------

def _list_bundled_skills(provider: str) -> list[str]:
    normalized_provider = str(provider).strip().lower()
    if normalized_provider != "codex":
        raise RuntimeError(f"unsupported skill provider '{provider}'")
    skills_root = importlib_resources.files("autolab").joinpath("skills", normalized_provider)
    found: list[str] = []
    for child in skills_root.iterdir():
        if child.joinpath("SKILL.md").is_file():
            found.append(child.name)
    return sorted(found)


def _load_packaged_skill_template_text(provider: str, skill_name: str) -> str:
    normalized_provider = str(provider).strip().lower()
    if normalized_provider != "codex":
        raise RuntimeError(f"unsupported skill provider '{provider}'")

    resource = importlib_resources.files("autolab").joinpath(
        "skills",
        normalized_provider,
        skill_name,
        "SKILL.md",
    )
    if not resource.is_file():
        raise RuntimeError(
            f"bundled skill template is unavailable at package://autolab/skills/{normalized_provider}/{skill_name}/SKILL.md"
        )
    return resource.read_text(encoding="utf-8")


def _deep_merge_dict(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dict(dict(merged[key]), value)  # type: ignore[arg-type]
        else:
            merged[key] = value
    return merged


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if _yaml_mod is None:
        raise RuntimeError("PyYAML is required for policy preset operations")
    if not path.exists():
        return {}
    payload = _yaml_mod.safe_load(path.read_text(encoding="utf-8"))
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping")
    return payload


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
        print("Run `autolab review --status=pass|retry|stop` to record your decision.")

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
            if heartbeat_dt is not None and (now - heartbeat_dt).total_seconds() > LOCK_STALE_SECONDS:
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
        except Exception:
            guardrail_cfg = {}
        max_streak = guardrail_cfg.get("max_same_decision_streak", 3)
        max_no_prog = guardrail_cfg.get("max_no_progress_decisions", 2)
        max_docs = guardrail_cfg.get("max_update_docs_cycles", 3)
        on_breach = guardrail_cfg.get("on_breach", "human_review")

        streak = repeat_guard.get("same_decision_streak", 0)
        no_prog = repeat_guard.get("no_progress_decisions", 0)
        docs_cyc = repeat_guard.get("update_docs_cycles", 0)
        print("guardrails:")
        print(f"  same_decision_streak: {streak}/{max_streak} (breach -> {on_breach})")
        print(f"  no_progress_decisions: {no_prog}/{max_no_prog} (breach -> {on_breach})")
        print(f"  update_docs_cycles: {docs_cyc}/{max_docs} (breach -> {on_breach})")

    return 0


def _cmd_guardrails(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)

    try:
        state = _load_state(state_path)
    except RuntimeError as exc:
        print(f"autolab guardrails: ERROR {exc}", file=sys.stderr)
        return 1

    try:
        guardrail_cfg = _load_guardrail_config(repo_root)
    except Exception as exc:
        print(f"autolab guardrails: ERROR loading guardrail config: {exc}", file=sys.stderr)
        return 1

    repeat_guard = state.get("repeat_guard", {})
    if not isinstance(repeat_guard, dict):
        repeat_guard = {}

    print("autolab guardrails")
    print(f"state_file: {state_path}")
    print(f"on_breach: {guardrail_cfg.on_breach}")
    print("")

    # Define the counter/threshold pairs
    counters = [
        (
            "same_decision_streak",
            int(repeat_guard.get("same_decision_streak", 0)),
            guardrail_cfg.max_same_decision_streak,
        ),
        (
            "no_progress_decisions",
            int(repeat_guard.get("no_progress_decisions", 0)),
            guardrail_cfg.max_no_progress_decisions,
        ),
        (
            "update_docs_cycle_count",
            int(repeat_guard.get("update_docs_cycle_count", 0)),
            guardrail_cfg.max_update_docs_cycles,
        ),
    ]

    print("guardrail counters:")
    for name, current, threshold in counters:
        distance = threshold - current
        breach_marker = " [BREACHED]" if distance <= 0 else ""
        print(f"  {name}: {current}/{threshold} (distance: {distance}){breach_marker}")

    print(f"  max_generated_todo_tasks: {guardrail_cfg.max_generated_todo_tasks}")

    # Additional repeat_guard state
    last_decision = str(repeat_guard.get("last_decision", "")).strip()
    last_verification = repeat_guard.get("last_verification_passed", False)
    print("")
    print(f"last_decision: {last_decision or '<none>'}")
    print(f"last_verification_passed: {last_verification}")

    # Show meaningful-change config if available
    try:
        meaningful_cfg = _load_meaningful_change_config(repo_root)
        print("")
        print("meaningful_change config:")
        print(f"  require_verification: {meaningful_cfg.require_verification}")
        print(f"  require_implementation_progress: {meaningful_cfg.require_implementation_progress}")
        print(f"  require_git_for_progress: {meaningful_cfg.require_git_for_progress}")
        print(f"  on_non_git_behavior: {meaningful_cfg.on_non_git_behavior}")
        print(f"  exclude_paths: {list(meaningful_cfg.exclude_paths)}")
    except Exception:
        pass

    return 0


def _cmd_configure(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    check_only = bool(args.check)

    print("autolab configure")
    print(f"state_file: {state_path}")
    print(f"check_only: {check_only}")
    print("")

    all_pass = True
    has_warn = False

    # 1. Check .autolab/ directory exists
    if autolab_dir.exists() and autolab_dir.is_dir():
        print(f"  [PASS] .autolab directory: {autolab_dir}")
    else:
        print(f"  [FAIL] .autolab directory: not found at {autolab_dir}")
        print("         Run `autolab init` to create the project scaffold.")
        all_pass = False

    # 2. Check verifier_policy.yaml exists and is valid YAML
    policy_path = autolab_dir / "verifier_policy.yaml"
    policy: dict[str, Any] = {}
    if policy_path.exists():
        if _yaml_mod is not None:
            try:
                loaded = _yaml_mod.safe_load(policy_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    policy = loaded
                    print(f"  [PASS] verifier_policy.yaml: valid ({policy_path})")
                else:
                    print(f"  [FAIL] verifier_policy.yaml: not a valid YAML mapping ({policy_path})")
                    all_pass = False
            except Exception as exc:
                print(f"  [FAIL] verifier_policy.yaml: parse error: {exc}")
                all_pass = False
        else:
            print(f"  [WARN] verifier_policy.yaml: exists but PyYAML is not installed; cannot validate")
            has_warn = True
    else:
        print(f"  [FAIL] verifier_policy.yaml: not found at {policy_path}")
        all_pass = False

    # 3. Check python_bin is resolvable
    python_bin = _resolve_policy_python_bin(policy)
    try:
        proc = subprocess.run(
            [python_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            version = proc.stdout.strip() or proc.stderr.strip()
            print(f"  [PASS] python_bin: {python_bin} ({version})")
        else:
            print(f"  [FAIL] python_bin: {python_bin} exited with code {proc.returncode}")
            all_pass = False
    except FileNotFoundError:
        print(f"  [FAIL] python_bin: {python_bin} not found on PATH")
        all_pass = False
    except subprocess.TimeoutExpired:
        print(f"  [FAIL] python_bin: {python_bin} timed out")
        all_pass = False
    except Exception as exc:
        print(f"  [FAIL] python_bin: {python_bin} error: {exc}")
        all_pass = False

    # 4. Check test_command is configured
    test_command = str(policy.get("test_command", "")).strip()
    if test_command:
        print(f"  [PASS] test_command: {test_command}")
    else:
        print("  [WARN] test_command: not configured")
        has_warn = True

    # 5. Check dry_run_command is configured
    dry_run_command = str(policy.get("dry_run_command", "")).strip()
    if dry_run_command:
        # Check if it is the default stub that always fails
        if "AUTOLAB DRY-RUN STUB" in dry_run_command:
            print("  [WARN] dry_run_command: using default stub (will fail until customized)")
            has_warn = True
        else:
            print(f"  [PASS] dry_run_command: {dry_run_command}")
    else:
        print("  [WARN] dry_run_command: not configured")
        has_warn = True

    # Summary
    print("")
    if all_pass and not has_warn:
        print("summary: all checks passed")
    elif all_pass and has_warn:
        print("summary: passed with warnings")
    else:
        print("summary: some checks failed")

    # Offer to write missing defaults if not --check
    if not check_only and not all_pass:
        if not autolab_dir.exists():
            print("\nTo create the .autolab scaffold, run: autolab init")
        if not policy_path.exists() and autolab_dir.exists():
            print(f"\nWriting default verifier_policy.yaml to {policy_path}")
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_text(DEFAULT_VERIFIER_POLICY, encoding="utf-8")
            print("  written: verifier_policy.yaml (default)")

    return 0 if all_pass else 1


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
    single_skill = getattr(args, "skill", None)

    if single_skill is not None:
        skill_names = [str(single_skill).strip()]
    else:
        try:
            skill_names = _list_bundled_skills(provider)
        except Exception as exc:
            print(f"autolab install-skill: ERROR {exc}", file=sys.stderr)
            return 1

    print("autolab install-skill")
    print(f"provider: {provider}")

    installed = 0
    for skill_name in skill_names:
        destination = project_root / ".codex" / "skills" / skill_name / "SKILL.md"
        try:
            template_text = _load_packaged_skill_template_text(provider, skill_name)
        except Exception as exc:
            print(f"  {skill_name}: ERROR {exc}", file=sys.stderr)
            return 1

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(template_text, encoding="utf-8")
        except Exception as exc:
            print(f"  {skill_name}: ERROR writing {destination}: {exc}", file=sys.stderr)
            return 1

        print(f"  {skill_name}: installed -> {destination}")
        installed += 1

    print(f"skills_installed: {installed}")
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


def _apply_init_policy_defaults(
    policy_path: Path,
    *,
    interactive: bool,
) -> tuple[bool, str]:
    if _yaml_mod is None or not policy_path.exists():
        return (False, "")
    try:
        policy = _load_yaml_mapping(policy_path)
    except Exception as exc:
        return (False, f"autolab init: WARN could not parse policy for defaults: {exc}")

    original = _yaml_mod.safe_dump(policy, sort_keys=False)
    selected_command = ""
    if interactive:
        print("")
        print("autolab init policy setup")
        print("Configure a dry-run command now (leave empty to skip dry-run for implementation stages).")
        try:
            selected_command = input("dry_run_command> ").strip()
        except EOFError:
            selected_command = ""

    requirements_by_stage = policy.get("requirements_by_stage", {})
    if not isinstance(requirements_by_stage, dict):
        requirements_by_stage = {}
        policy["requirements_by_stage"] = requirements_by_stage

    implementation_cfg = requirements_by_stage.get("implementation", {})
    if not isinstance(implementation_cfg, dict):
        implementation_cfg = {}
    implementation_review_cfg = requirements_by_stage.get("implementation_review", {})
    if not isinstance(implementation_review_cfg, dict):
        implementation_review_cfg = {}

    warning = ""
    if selected_command:
        policy["dry_run_command"] = selected_command
        implementation_cfg["dry_run"] = True
        implementation_review_cfg["dry_run"] = True
    else:
        implementation_cfg["dry_run"] = False
        implementation_review_cfg["dry_run"] = False
        warning = (
            "autolab init: WARN dry_run_command is not configured. "
            "Set verifier_policy.yaml dry_run_command before enabling dry_run requirements."
        )

    requirements_by_stage["implementation"] = implementation_cfg
    requirements_by_stage["implementation_review"] = implementation_review_cfg
    policy["requirements_by_stage"] = requirements_by_stage

    rendered = _yaml_mod.safe_dump(policy, sort_keys=False)
    changed = rendered != original
    if changed:
        policy_path.write_text(rendered, encoding="utf-8")
    return (changed, warning)


def _cmd_policy_apply_preset(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    preset_name = str(args.preset).strip()

    if preset_name not in POLICY_PRESET_NAMES:
        print(
            f"autolab policy apply preset: ERROR unsupported preset '{preset_name}'",
            file=sys.stderr,
        )
        return 1

    if _yaml_mod is None:
        print("autolab policy apply preset: ERROR PyYAML is required", file=sys.stderr)
        return 1

    try:
        scaffold_source = _resolve_scaffold_source()
    except RuntimeError as exc:
        print(f"autolab policy apply preset: ERROR {exc}", file=sys.stderr)
        return 1

    preset_path = scaffold_source / "policy" / f"{preset_name}.yaml"
    if not preset_path.exists():
        print(
            f"autolab policy apply preset: ERROR preset file missing at {preset_path}",
            file=sys.stderr,
        )
        return 1

    policy_path = autolab_dir / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    current_policy: dict[str, Any] = {}
    if policy_path.exists():
        try:
            current_policy = _load_yaml_mapping(policy_path)
        except Exception as exc:
            print(
                f"autolab policy apply preset: ERROR could not parse current policy: {exc}",
                file=sys.stderr,
            )
            return 1
    try:
        preset_policy = _load_yaml_mapping(preset_path)
    except Exception as exc:
        print(
            f"autolab policy apply preset: ERROR could not parse preset: {exc}",
            file=sys.stderr,
        )
        return 1

    merged = _deep_merge_dict(current_policy, preset_policy)
    policy_path.write_text(_yaml_mod.safe_dump(merged, sort_keys=False), encoding="utf-8")

    print("autolab policy apply preset")
    print(f"preset: {preset_name}")
    print(f"policy_file: {policy_path}")
    print("status: applied")
    return 0


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
    interactive = bool(getattr(args, "interactive", False))
    no_interactive = bool(getattr(args, "no_interactive", False))
    if not interactive and not no_interactive:
        interactive = sys.stdin.isatty()
    policy_updated, policy_warning = _apply_init_policy_defaults(
        verifier_policy_path,
        interactive=interactive and not no_interactive,
    )
    if policy_updated and verifier_policy_path not in created:
        created.append(verifier_policy_path)
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
    if policy_warning:
        print(f"\n{policy_warning}")

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
# Lock management
# ---------------------------------------------------------------------------


def _cmd_lock(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    lock_path = repo_root / ".autolab" / "run.lock"
    action = args.action

    if action == "status":
        info = _inspect_lock(lock_path)
        if info is None:
            print("autolab lock: no active lock")
            return 0
        print("autolab lock: active")
        for key in ("pid", "host", "owner_uuid", "started_at", "last_heartbeat_at", "command", "state_file"):
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
    try:
        state = _load_state(state_path)
    except RuntimeError as exc:
        print(f"autolab skip: ERROR {exc}", file=sys.stderr)
        return 1

    current_stage = str(state.get("stage", "")).strip()
    target_stage = args.stage
    reason = args.reason

    if current_stage in TERMINAL_STAGES:
        print(f"autolab skip: ERROR current stage '{current_stage}' is terminal; cannot skip", file=sys.stderr)
        return 1

    if target_stage in TERMINAL_STAGES:
        print(f"autolab skip: ERROR cannot skip to terminal stage '{target_stage}'", file=sys.stderr)
        return 1

    # Validate forward-only skip within ACTIVE_STAGES (includes decide_repeat)
    ordered_stages = list(ACTIVE_STAGES)
    if "decide_repeat" not in ordered_stages:
        ordered_stages.append("decide_repeat")
    if current_stage not in ordered_stages:
        print(f"autolab skip: ERROR current stage '{current_stage}' is not skippable", file=sys.stderr)
        return 1
    if target_stage not in ordered_stages:
        print(f"autolab skip: ERROR target stage '{target_stage}' is not a valid skip target", file=sys.stderr)
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
    _append_log(repo_root, f"skip: {current_stage} -> {target_stage} reason={reason}")
    print(f"autolab skip: {current_stage} -> {target_stage}")
    return 0


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

    passed, detail_message, _details = _run_verification_step_detailed(repo_root, state, stage_override=stage_override)
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
    # Locate golden iteration examples relative to the autolab package source.
    package_dir = Path(__file__).resolve().parent
    golden_root = package_dir.parent.parent / "examples" / "golden_iteration"
    scaffold_source = package_dir / "scaffold" / ".autolab"

    if not golden_root.exists():
        print(
            f"autolab verify-golden: ERROR golden iteration fixtures not found at {golden_root}",
            file=sys.stderr,
        )
        return 1
    if not scaffold_source.exists():
        print(
            f"autolab verify-golden: ERROR scaffold not found at {scaffold_source}",
            file=sys.stderr,
        )
        return 1

    stages = list(ACTIVE_STAGES)
    if "decide_repeat" not in stages:
        stages.append("decide_repeat")
    results: list[tuple[str, bool]] = []

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
        policy_text = policy_text.replace(
            'python_bin: "python3"', f'python_bin: "{sys.executable}"', 1
        )
        policy_lines = policy_text.splitlines()
        for idx, line in enumerate(policy_lines):
            if line.strip().startswith("dry_run_command:"):
                policy_lines[idx] = (
                    'dry_run_command: "{{python_bin}} -c \\"print(\'golden iteration dry-run: OK\')\\""'
                )
                break
        policy_text = "\n".join(policy_lines) + ("\n" if policy_text.endswith("\n") else "")
        policy_path.write_text(policy_text, encoding="utf-8")

        # 3. Copy golden iteration experiments/ and paper/
        shutil.copytree(
            golden_root / "experiments", repo / "experiments", dirs_exist_ok=True
        )
        shutil.copytree(
            golden_root / "paper", repo / "paper", dirs_exist_ok=True
        )

        # 4. Copy golden iteration state.json and backlog.yaml
        shutil.copy2(
            golden_root / ".autolab" / "state.json",
            target_autolab / "state.json",
        )
        shutil.copy2(
            golden_root / ".autolab" / "backlog.yaml",
            target_autolab / "backlog.yaml",
        )

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


def _cmd_explain(args: argparse.Namespace) -> int:
    stage_name = str(args.stage).strip()
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)

    registry = load_registry(repo_root)
    if not registry:
        print("autolab explain: ERROR could not load workflow.yaml registry", file=sys.stderr)
        return 1

    spec = registry.get(stage_name)
    if spec is None:
        print(f"autolab explain: ERROR unknown stage '{stage_name}'", file=sys.stderr)
        print(f"available stages: {', '.join(sorted(registry.keys()))}")
        return 1

    policy = _load_verifier_policy(repo_root)

    print(f"autolab explain stage {stage_name}")
    print("")
    print(f"prompt_file: {spec.prompt_file}")
    print(f"required_tokens: {', '.join(sorted(spec.required_tokens)) or '(none)'}")
    print(f"required_outputs: {', '.join(spec.required_outputs) or '(none)'}")
    print(f"next_stage: {spec.next_stage or '(branching)'}")
    if spec.decision_map:
        print(f"decision_map: {spec.decision_map}")
    print("")

    from autolab.config import _resolve_stage_requirements, _resolve_stage_max_retries
    effective = _resolve_stage_requirements(
        policy,
        stage_name,
        registry_verifier_categories=spec.verifier_categories,
    )
    print("effective verifier requirements:")
    for key in sorted(effective.keys()):
        eff_val = effective[key]
        reg_val = spec.verifier_categories.get(key, False)
        # Determine source annotation
        if eff_val and not reg_val:
            note = "(policy override)"
        elif reg_val and not eff_val:
            note = f"(registry: {reg_val}, policy: {eff_val}) # capable but not required"
        else:
            note = ""
        print(f"  {key}: {eff_val}{' ' + note if note else ''}")

    max_retries = _resolve_stage_max_retries(policy, stage_name)
    print("")
    print(f"retry_policy: max_retries={max_retries}")
    print(f"classifications: active={spec.is_active}, terminal={spec.is_terminal}, decision={spec.is_decision}, runner_eligible={spec.is_runner_eligible}")

    return 0


# ---------------------------------------------------------------------------
# Policy list/show commands
# ---------------------------------------------------------------------------


def _cmd_policy_list(args: argparse.Namespace) -> int:
    try:
        scaffold_source = _resolve_scaffold_source()
    except RuntimeError as exc:
        print(f"autolab policy list: ERROR {exc}", file=sys.stderr)
        return 1

    policy_dir = scaffold_source / "policy"
    if not policy_dir.exists():
        print("autolab policy list: no presets found")
        return 0

    print("autolab policy list")
    print("available presets:")
    for path in sorted(policy_dir.glob("*.yaml")):
        print(f"  {path.stem}")
    return 0


def _cmd_policy_show(args: argparse.Namespace) -> int:
    preset_name = str(args.preset).strip()
    try:
        scaffold_source = _resolve_scaffold_source()
    except RuntimeError as exc:
        print(f"autolab policy show: ERROR {exc}", file=sys.stderr)
        return 1

    preset_path = scaffold_source / "policy" / f"{preset_name}.yaml"
    if not preset_path.exists():
        print(f"autolab policy show: ERROR preset '{preset_name}' not found", file=sys.stderr)
        return 1

    print(f"autolab policy show {preset_name}")
    print(f"file: {preset_path}")
    print("---")
    print(preset_path.read_text(encoding="utf-8").rstrip())
    return 0


# ---------------------------------------------------------------------------
# Docs generate command
# ---------------------------------------------------------------------------


def _cmd_docs_generate(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    registry = load_registry(repo_root)

    if not registry:
        print("autolab docs generate: ERROR could not load workflow.yaml registry", file=sys.stderr)
        return 1

    # 1. Stage flow diagram
    print("# Autolab Stage Flow")
    print("")
    active = [name for name, spec in registry.items() if spec.is_active and not spec.is_terminal]
    flow_parts: list[str] = []
    for name in active:
        spec = registry[name]
        if spec.decision_map:
            targets = ", ".join(sorted(spec.decision_map.values()))
            flow_parts.append(f"{name} -> {{{targets}}}")
        elif spec.next_stage:
            flow_parts.append(f"{name} -> {spec.next_stage}")
        else:
            flow_parts.append(name)
    print(" | ".join(flow_parts))
    print("")

    # 2. Artifact map
    print("## Artifact Map")
    print("")
    print("| Stage | Required Outputs |")
    print("|-------|-----------------|")
    for name, spec in registry.items():
        outputs = ", ".join(spec.required_outputs) if spec.required_outputs else "(none)"
        print(f"| {name} | {outputs} |")
    print("")

    # 3. Token reference
    print("## Token Reference")
    print("")
    print("| Stage | Required Tokens |")
    print("|-------|----------------|")
    for name, spec in registry.items():
        tokens = ", ".join(sorted(spec.required_tokens)) if spec.required_tokens else "(none)"
        print(f"| {name} | {tokens} |")
    print("")

    # 4. Classifications
    print("## Classifications")
    print("")
    print("| Stage | Active | Terminal | Decision | Runner Eligible |")
    print("|-------|--------|----------|----------|----------------|")
    for name, spec in registry.items():
        print(f"| {name} | {spec.is_active} | {spec.is_terminal} | {spec.is_decision} | {spec.is_runner_eligible} |")

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
    init_interactive = init.add_mutually_exclusive_group()
    init_interactive.add_argument(
        "--interactive",
        action="store_true",
        help="Prompt for policy bootstrap values during init.",
    )
    init_interactive.add_argument(
        "--no-interactive",
        action="store_true",
        help="Disable interactive prompts during init.",
    )
    init.set_defaults(handler=_cmd_init)

    configure_parser = subparsers.add_parser("configure", help="Validate and configure autolab settings")
    configure_parser.add_argument("--check", action="store_true", help="Check configuration without modifying")
    configure_parser.add_argument("--state-file", default=".autolab/state.json", help="Path to state file")
    configure_parser.set_defaults(handler=_cmd_configure)

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

    verify_golden = subparsers.add_parser(
        "verify-golden",
        help="Run verifiers against bundled golden iteration fixtures",
    )
    verify_golden.set_defaults(handler=_cmd_verify_golden)

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

    guardrails_parser = subparsers.add_parser("guardrails", help="Show guardrail counters and thresholds")
    guardrails_parser.add_argument("--state-file", default=".autolab/state.json", help="Path to state file")
    guardrails_parser.set_defaults(handler=_cmd_guardrails)

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
        "--skill",
        default=None,
        help="Install only this skill (default: all bundled skills).",
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

    # Lock management
    lock = subparsers.add_parser("lock", help="Inspect or break the autolab run lock")
    lock.add_argument(
        "action",
        choices=("status", "break"),
        help="Action: status (show lock info) or break (force remove lock)",
    )
    lock.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    lock.add_argument(
        "--reason",
        default="manual break",
        help="Reason for breaking the lock (used in audit log)",
    )
    lock.set_defaults(handler=_cmd_lock)

    # Unlock alias (delegates to lock break)
    unlock = subparsers.add_parser("unlock", help="Force-break the autolab run lock (alias for 'lock break')")
    unlock.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    unlock.add_argument(
        "--reason",
        default="manual break",
        help="Reason for breaking the lock (used in audit log)",
    )
    unlock.set_defaults(handler=_cmd_lock, action="break")

    # Skip stage
    skip = subparsers.add_parser("skip", help="Skip the current stage forward with audit trail")
    skip.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    skip.add_argument(
        "--stage",
        required=True,
        help="Target stage to skip to (must be a forward stage in the pipeline)",
    )
    skip.add_argument(
        "--reason",
        required=True,
        help="Reason for skipping (recorded in state history)",
    )
    skip.set_defaults(handler=_cmd_skip)

    # Lint (user-friendly verify alias)
    lint = subparsers.add_parser("lint", help="Run stage verifiers with user-friendly output")
    lint.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    lint.add_argument(
        "--stage",
        default=None,
        help="Override stage for linting (default: state.stage)",
    )
    lint.set_defaults(handler=_cmd_lint)

    # Explain stage
    explain = subparsers.add_parser("explain", help="Show effective configuration for a stage")
    explain_subparsers = explain.add_subparsers(dest="explain_command")
    explain_stage = explain_subparsers.add_parser("stage", help="Show effective stage config")
    explain_stage.add_argument("stage", help="Stage name to explain")
    explain_stage.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    explain_stage.set_defaults(handler=_cmd_explain)

    # Policy management
    policy = subparsers.add_parser(
        "policy",
        help="Manage verifier policy profiles",
    )
    policy_subparsers = policy.add_subparsers(dest="policy_command")

    policy_list = policy_subparsers.add_parser("list", help="List available policy presets")
    policy_list.set_defaults(handler=_cmd_policy_list)

    policy_show = policy_subparsers.add_parser("show", help="Show contents of a policy preset")
    policy_show.add_argument("preset", help="Preset name to show")
    policy_show.set_defaults(handler=_cmd_policy_show)

    policy_apply = policy_subparsers.add_parser("apply", help="Apply policy changes")
    policy_apply_subparsers = policy_apply.add_subparsers(dest="policy_apply_command")
    policy_preset = policy_apply_subparsers.add_parser(
        "preset",
        help="Apply a bundled policy preset",
    )
    policy_preset.add_argument(
        "preset",
        choices=POLICY_PRESET_NAMES,
        help="Preset name to apply.",
    )
    policy_preset.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    policy_preset.set_defaults(handler=_cmd_policy_apply_preset)

    # Docs generation
    docs = subparsers.add_parser("docs", help="Generate documentation from registry")
    docs_subparsers = docs.add_subparsers(dest="docs_command")
    docs_generate = docs_subparsers.add_parser("generate", help="Generate stage flow, artifact map, and token reference")
    docs_generate.add_argument(
        "--state-file",
        default=".autolab/state.json",
        help="Path to autolab state JSON (default: .autolab/state.json)",
    )
    docs_generate.set_defaults(handler=_cmd_docs_generate)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 2
    return int(handler(args))
