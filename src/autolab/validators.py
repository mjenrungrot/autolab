"""Autolab validators — stage-gate checks and file validations."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from autolab.constants import (
    REVIEW_RESULT_CHECK_STATUSES,
    REVIEW_RESULT_REQUIRED_CHECKS,
    SLURM_JOB_LIST_PATH,
    VERIFIER_COMMAND_TIMEOUT_SECONDS,
)
from autolab.models import StageCheckError, _coerce_bool
from autolab.slurm_job_list import (
    canonical_slurm_job_bullet,
    is_slurm_manifest,
    ledger_contains_run_id,
)


# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------


def _replace_iteration_placeholders(command: str, iteration_id: str, iteration_path: str) -> str:
    return (
        command
        .replace("<ITERATION_ID>", iteration_id)
        .replace("{{iteration_id}}", iteration_id)
        .replace("<ITERATION_PATH>", iteration_path)
        .replace("{{iteration_path}}", iteration_path)
    )


def _require_non_empty(path: Path, label: str) -> None:
    if not path.exists():
        raise StageCheckError(
            f"{label} is missing at {path}"
            " Please create this file before proceeding."
        )
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise StageCheckError(
            f"{label} is empty at {path}"
            " Please populate this file with required content."
        )


def _load_dict_json(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise StageCheckError(f"{label} is missing at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise StageCheckError(f"{label} is not valid JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StageCheckError(f"{label} must contain a JSON object at {path}")
    return payload


# ---------------------------------------------------------------------------
# Design validation
# ---------------------------------------------------------------------------


def _validate_design(path: Path, iteration_id: str) -> None:
    if yaml is None:
        raise StageCheckError("design validation requires PyYAML")
    if not path.exists():
        raise StageCheckError(f"design.yaml is missing at {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageCheckError(f"design.yaml is not valid YAML at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StageCheckError("design.yaml must contain a mapping")

    required = {"id", "iteration_id", "hypothesis_id", "entrypoint", "compute", "metrics", "baselines"}
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise StageCheckError(
            f"design.yaml missing required keys: {missing}"
            " Ensure design.yaml includes all required fields (see .autolab/schemas/design.schema.json)."
        )

    if str(payload.get("iteration_id", "")).strip() != iteration_id:
        raise StageCheckError("design.yaml iteration_id does not match state.iteration_id")

    entrypoint = payload.get("entrypoint")
    if not isinstance(entrypoint, dict) or not str(entrypoint.get("module", "")).strip():
        raise StageCheckError("design.yaml entrypoint.module must be set")

    compute = payload.get("compute")
    if not isinstance(compute, dict) or not str(compute.get("location", "")).strip():
        raise StageCheckError("design.yaml compute.location must be set")

    baselines = payload.get("baselines")
    if not isinstance(baselines, list) or not baselines:
        raise StageCheckError("design.yaml baselines must be a non-empty list")


# ---------------------------------------------------------------------------
# Review-result validation
# ---------------------------------------------------------------------------


def _validate_review_result(path: Path, *, policy_requirements: dict[str, bool] | None = None) -> str:
    payload = _load_dict_json(path, "review_result.json")
    required = {"status", "blocking_findings", "required_checks", "reviewed_at"}
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise StageCheckError(
            f"review_result.json missing required keys: {missing}"
            " See .autolab/schemas/review_result.schema.json for the expected format."
        )

    status = str(payload.get("status", "")).strip()
    if status not in {"pass", "needs_retry", "failed"}:
        raise StageCheckError(f"review_result.json has invalid status '{status}'")

    required_checks = payload.get("required_checks")
    if not isinstance(required_checks, dict):
        raise StageCheckError("review_result.json required_checks must be a mapping")

    missing_checks = sorted(set(REVIEW_RESULT_REQUIRED_CHECKS) - set(required_checks.keys()))
    if missing_checks:
        raise StageCheckError(
            f"review_result.json required_checks missing keys: {missing_checks}"
            " See .autolab/schemas/review_result.schema.json for the expected format."
        )

    for check_name in REVIEW_RESULT_REQUIRED_CHECKS:
        check_status = str(required_checks.get(check_name, "")).strip().lower()
        if check_status not in REVIEW_RESULT_CHECK_STATUSES:
            raise StageCheckError(
                f"review_result.json required_checks['{check_name}'] must be one of {sorted(REVIEW_RESULT_CHECK_STATUSES)}"
            )

    if policy_requirements:
        required_by_policy = sorted(
            check_name
            for check_name in REVIEW_RESULT_REQUIRED_CHECKS
            if policy_requirements.get(check_name, False)
        )
        for check_name in required_by_policy:
            check_status = str(required_checks.get(check_name, "")).strip().lower()
            if status == "pass" and check_status != "pass":
                raise StageCheckError(
                    f"review_result.json status=pass requires required_checks['{check_name}']='pass', got '{check_status}'"
                )
    return status


# ---------------------------------------------------------------------------
# Launch validation
# ---------------------------------------------------------------------------


def _validate_launch(iteration_dir: Path) -> None:
    local_script = iteration_dir / "launch" / "run_local.sh"
    slurm_script = iteration_dir / "launch" / "run_slurm.sbatch"
    if not local_script.exists() and not slurm_script.exists():
        raise StageCheckError("launch requires run_local.sh or run_slurm.sbatch")
    if local_script.exists():
        _require_non_empty(local_script, "launch/run_local.sh")
    if slurm_script.exists():
        _require_non_empty(slurm_script, "launch/run_slurm.sbatch")


# ---------------------------------------------------------------------------
# Extract-results validation
# ---------------------------------------------------------------------------


def _validate_extract(iteration_dir: Path, run_id: str) -> None:
    if not run_id or run_id.startswith("<"):
        raise StageCheckError("state.last_run_id must be set for extract_results")
    run_dir = iteration_dir / "runs" / run_id
    manifest = run_dir / "run_manifest.json"
    metrics = run_dir / "metrics.json"
    _require_non_empty(manifest, "runs/<run_id>/run_manifest.json")
    manifest_payload = _load_dict_json(manifest, "runs/<run_id>/run_manifest.json")
    sync = manifest_payload.get("artifact_sync_to_local")
    if not isinstance(sync, dict):
        raise StageCheckError("runs/<run_id>/run_manifest.json missing artifact_sync_to_local mapping")
    sync_status = str(sync.get("status", "")).strip().lower()
    if sync_status not in {"ok", "completed", "success", "passed"}:
        raise StageCheckError(
            "runs/<run_id>/run_manifest.json artifact_sync_to_local.status must be success-like before extract_results"
        )
    payload = _load_dict_json(metrics, "runs/<run_id>/metrics.json")
    if not payload:
        raise StageCheckError(
            "runs/<run_id>/metrics.json must not be empty"
            " Populate metrics.json with run results before proceeding to extract_results."
        )


# ---------------------------------------------------------------------------
# Update-docs validation
# ---------------------------------------------------------------------------


def _validate_update_docs(repo_root: Path, iteration_dir: Path, run_id: str) -> None:
    _require_non_empty(iteration_dir / "docs_update.md", "docs_update.md")
    _require_non_empty(iteration_dir / "analysis" / "summary.md", "analysis/summary.md")
    normalized_run_id = str(run_id).strip()
    if not normalized_run_id or normalized_run_id.startswith("<"):
        return
    manifest_path = iteration_dir / "runs" / normalized_run_id / "run_manifest.json"
    manifest_payload = _load_dict_json(manifest_path, "runs/<run_id>/run_manifest.json")
    _validate_slurm_job_ledger_entry(
        repo_root,
        manifest_path=manifest_path,
        payload=manifest_payload,
        stage="update_docs",
    )


# ---------------------------------------------------------------------------
# SLURM ledger validation
# ---------------------------------------------------------------------------


def _validate_slurm_job_ledger_entry(
    repo_root: Path,
    *,
    manifest_path: Path,
    payload: dict[str, Any],
    stage: str,
) -> None:
    if not is_slurm_manifest(payload):
        return

    try:
        # Enforces required SLURM identifiers, including strict job_id presence.
        canonical_slurm_job_bullet(payload)
    except ValueError as exc:
        raise StageCheckError(f"{manifest_path} {exc}") from exc

    run_id = str(payload.get("run_id", "")).strip() or manifest_path.parent.name
    doc_path = repo_root / SLURM_JOB_LIST_PATH
    if not doc_path.exists():
        raise StageCheckError(
            f"{stage} requires {SLURM_JOB_LIST_PATH} for SLURM runs; missing at {doc_path}"
        )

    ledger_text = doc_path.read_text(encoding="utf-8")
    if not ledger_contains_run_id(ledger_text, run_id):
        raise StageCheckError(
            f"{stage} requires SLURM ledger entry run_id={run_id} in {SLURM_JOB_LIST_PATH}"
        )


# ---------------------------------------------------------------------------
# Run-state resolution
# ---------------------------------------------------------------------------


def _resolve_latest_run_state(iteration_dir: Path) -> tuple[str, str]:
    # Lazy import to avoid circular dependency — _manifest_timestamp lives in
    # __main__ until the utils module is extracted.
    from autolab.utils import _manifest_timestamp

    manifests = sorted(iteration_dir.glob("runs/*/run_manifest.json"))
    if not manifests:
        raise StageCheckError(f"launch did not produce run_manifest.json under {iteration_dir / 'runs'}")
    manifest_candidates: list[tuple[int, datetime, str, str, dict[str, Any]]] = []
    for manifest_path in manifests:
        payload = _load_dict_json(manifest_path, "runs/<run_id>/run_manifest.json")
        run_dir = manifest_path.parent
        run_id = str(payload.get("run_id", "")).strip() or run_dir.name
        parsed_timestamp = _manifest_timestamp(payload, run_id)
        timestamp = parsed_timestamp or datetime.min.replace(tzinfo=timezone.utc)
        has_timestamp = 1 if parsed_timestamp is not None else 0
        manifest_candidates.append((has_timestamp, timestamp, run_id, str(manifest_path), payload))
    _has_timestamp, _timestamp, run_id, _manifest_path, payload = max(
        manifest_candidates,
        key=lambda item: (item[0], item[1], item[2], item[3]),
    )

    sync_status = "completed"
    sync_payload = payload.get("artifact_sync_to_local")
    if not isinstance(sync_payload, dict):
        sync_root = payload.get("sync")
        if isinstance(sync_root, dict):
            nested_payload = sync_root.get("artifact_sync_to_local")
            if isinstance(nested_payload, dict):
                sync_payload = nested_payload
    if isinstance(sync_payload, dict):
        raw_sync = str(sync_payload.get("status", "")).strip().lower()
        if raw_sync in {"ok", "completed", "success"}:
            sync_status = "completed"
        elif raw_sync:
            sync_status = raw_sync
    launch_payload = payload.get("launch")
    nested_launch_mode = launch_payload.get("mode", "") if isinstance(launch_payload, dict) else ""
    launch_mode = str(
        payload.get("launch_mode")
        or payload.get("host_mode")
        or payload.get("detected_host_mode")
        or nested_launch_mode
    ).strip().lower()
    if launch_mode == "slurm" and sync_status not in {"completed", "ok", "success"}:
        raise StageCheckError(
            f"latest run {run_id} has incomplete artifact synchronization for slurm mode: {sync_status}"
        )
    return (run_id, sync_status)


# ---------------------------------------------------------------------------
# Verification step
# ---------------------------------------------------------------------------


def _run_verification_step(repo_root: Path, state: dict[str, Any]) -> tuple[bool, str]:
    from autolab.config import (
        _load_verifier_policy,
        _resolve_policy_command,
        _resolve_policy_python_bin,
        _resolve_stage_requirements,
    )
    from autolab.state import _resolve_iteration_directory
    from autolab.utils import _append_log, _compact_log_text

    policy = _load_verifier_policy(repo_root)
    iteration_id = str(state.get("iteration_id", "")).strip()
    stage = str(state.get("stage", "")).strip()
    iteration_dir, _iteration_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=str(state.get("experiment_id", "")).strip(),
        require_exists=False,
    )
    iteration_path = iteration_dir.relative_to(repo_root).as_posix()

    stage_requirements = _resolve_stage_requirements(policy, stage)
    python_bin = _resolve_policy_python_bin(policy)
    test_command = _resolve_policy_command(str(policy.get("test_command", "")).strip(), python_bin=python_bin)
    dry_run_command = _resolve_policy_command(str(policy.get("dry_run_command", "")).strip(), python_bin=python_bin)
    if not dry_run_command and stage_requirements.get("dry_run"):
        return (False, "verification dry-run command is required by policy but not configured")
    if not test_command and stage_requirements.get("tests"):
        return (False, "verification test command is required by policy but not configured")

    template_fill_section = policy.get("template_fill", {})
    template_fill_enabled = False
    template_fill_command = ""
    if isinstance(template_fill_section, dict):
        template_fill_enabled = bool(template_fill_section.get("enabled", False))
        if _coerce_bool(template_fill_section.get("enabled"), default=template_fill_enabled):
            raw_template_fill_command = str(template_fill_section.get("command", "")).strip()
            template_fill_command = _resolve_policy_command(raw_template_fill_command, python_bin=python_bin)

    template_fill_by_stage = policy.get("template_fill_by_stage", {})
    if isinstance(template_fill_by_stage, dict):
        stage_template_fill = template_fill_by_stage.get(stage)
        if isinstance(stage_template_fill, str) and stage_template_fill.strip():
            template_fill_command = _resolve_policy_command(stage_template_fill.strip(), python_bin=python_bin)

    command_specs: list[tuple[str, str]] = []

    commands: list[str] = []
    if stage_requirements["tests"] and test_command:
        commands.append(test_command)
    if stage_requirements["dry_run"] and dry_run_command:
        commands.append(_replace_iteration_placeholders(dry_run_command, iteration_id, iteration_path))
    if template_fill_enabled and template_fill_command:
        command_specs.append(("template_fill", template_fill_command))
    closed_guard_path = repo_root / ".autolab" / "verifiers" / "closed_experiment_guard.py"
    if closed_guard_path.exists():
        command_specs.append(
            (
                "closed_experiment_guard",
                f"{python_bin} .autolab/verifiers/closed_experiment_guard.py",
            )
        )
    if stage_requirements["env_smoke"]:
        command_specs.append(("run_health", f"{python_bin} .autolab/verifiers/run_health.py"))
        command_specs.append(("result_sanity", f"{python_bin} .autolab/verifiers/result_sanity.py"))
    if stage_requirements["docs_target_update"] and stage in {"update_docs", "implementation_review"}:
        command_specs.append(
            ("docs_targets", f"{python_bin} .autolab/verifiers/docs_targets.py")
        )
    if stage_requirements["schema"]:
        command_specs.append(("schema_checks", f"{python_bin} .autolab/verifiers/schema_checks.py"))

    for _name, command in command_specs:
        if command:
            commands.append(command)

    if not commands and stage_requirements["tests"]:
        return (
            False,
            "verification command list is empty after policy resolution; update verifier_policy requirements",
        )
    if not commands:
        commands.append("true")

    if not commands:
        return (False, "verification command not configured")

    for command in commands:
        if not command.strip():
            continue
        try:
            process = subprocess.run(
                command,
                cwd=repo_root,
                shell=True,
                text=True,
                capture_output=True,
                check=False,
                timeout=VERIFIER_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            detail = _compact_log_text(f"verification command timed out: {command}")
            _append_log(repo_root, f"assistant verification timeout command={command}")
            return (False, f"verification failed: {detail}")
        except OSError as exc:
            detail = _compact_log_text(f"verification command failed to start: {exc}")
            _append_log(repo_root, f"assistant verification failed command={command} detail={detail}")
            return (False, f"verification failed: {detail}")
        if process.returncode != 0:
            detail = _compact_log_text((process.stderr or process.stdout or "verification failed").strip())
            _append_log(repo_root, f"assistant verification failed command={command} detail={detail}")
            return (False, f"verification failed: {detail}")
    return (True, f"verification passed ({len(commands)} command(s))")
