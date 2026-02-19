"""Autolab validators — stage-gate checks and file validations."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from autolab.constants import (
    ACTIVE_STAGES,
    PROMPT_SHARED_INCLUDE_PATTERN,
    PROMPT_TOKEN_PATTERN,
    REVIEW_RESULT_CHECK_STATUSES,
    REVIEW_RESULT_REQUIRED_CHECKS,
    SLURM_JOB_LIST_PATH,
    SYNC_SUCCESS_STATUSES,
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


def _replace_iteration_placeholders(
    command: str, iteration_id: str, iteration_path: str
) -> str:
    return (
        command.replace("<ITERATION_ID>", iteration_id)
        .replace("{{iteration_id}}", iteration_id)
        .replace("<ITERATION_PATH>", iteration_path)
        .replace("{{iteration_path}}", iteration_path)
    )


def _require_non_empty(path: Path, label: str) -> None:
    if not path.exists():
        raise StageCheckError(
            f"{label} is missing at {path} Please create this file before proceeding."
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


def _collect_prompt_token_origins(
    repo_root: Path,
    *,
    prompt_path: Path,
    stage: str,
    seen_paths: set[Path] | None = None,
) -> dict[str, set[str]]:
    """Collect template token origin files (including shared includes)."""
    seen = seen_paths if seen_paths is not None else set()
    if prompt_path in seen:
        return {}
    seen.add(prompt_path)

    try:
        text = prompt_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise StageCheckError(
            f"stage readiness could not read prompt template '{prompt_path}': {exc}"
        ) from exc

    try:
        prompt_label = prompt_path.relative_to(repo_root).as_posix()
    except ValueError:
        prompt_label = str(prompt_path)

    origins: dict[str, set[str]] = {}
    for match in PROMPT_TOKEN_PATTERN.finditer(text):
        token = match.group(1).strip()
        if not token:
            continue
        origins.setdefault(token, set()).add(prompt_label)

    for include in PROMPT_SHARED_INCLUDE_PATTERN.finditer(text):
        shared_name = include.group(1).strip()
        if not shared_name:
            continue
        shared_path = repo_root / ".autolab" / "prompts" / "shared" / shared_name
        if not shared_path.exists():
            raise StageCheckError(
                f"stage readiness missing shared prompt include '{shared_name}' for stage '{stage}'"
            )
        nested = _collect_prompt_token_origins(
            repo_root,
            prompt_path=shared_path,
            stage=stage,
            seen_paths=seen,
        )
        for token, sources in nested.items():
            if token not in origins:
                origins[token] = set()
            origins[token].update(sources)
    return origins


def _validate_stage_readiness(
    repo_root: Path,
    state: dict[str, Any],
    *,
    stage_override: str | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Validate prompt-token readiness before runner execution/evaluation."""
    from autolab.prompts import _build_prompt_context, _resolve_stage_prompt_path
    from autolab.registry import load_registry, registry_required_tokens

    stage = _resolve_verification_stage(state, stage_override=stage_override)
    if stage in {"decide_repeat", "human_review", "stop"}:
        return (
            True,
            "readiness check skipped for non-active stage",
            {"stage": stage, "missing_tokens": []},
        )
    if stage not in ACTIVE_STAGES:
        return (
            True,
            "readiness check skipped for unsupported stage",
            {"stage": stage, "missing_tokens": []},
        )

    registry = load_registry(repo_root)
    if not registry:
        return (
            True,
            "readiness check skipped: workflow registry not found",
            {
                "stage": stage,
                "missing_tokens": [],
                "reason": "workflow_registry_missing",
            },
        )

    template_path = _resolve_stage_prompt_path(repo_root, stage)
    origins = _collect_prompt_token_origins(
        repo_root,
        prompt_path=template_path,
        stage=stage,
    )

    required_by_stage = registry_required_tokens(registry)
    required_tokens = sorted(required_by_stage.get(stage, {"iteration_id"}))

    context_payload = _build_prompt_context(
        repo_root,
        state=state,
        stage=stage,
        runner_scope=None,
    )

    missing: list[dict[str, Any]] = []
    for token in required_tokens:
        raw = context_payload.get(token)
        value = str(raw if raw is not None else "").strip()
        if not value or value.startswith("unavailable:"):
            source_files = sorted(origins.get(token, {template_path.as_posix()}))
            missing.append(
                {
                    "token": token,
                    "source_files": source_files,
                }
            )

    details: dict[str, Any] = {
        "stage": stage,
        "template_path": template_path.as_posix(),
        "missing_tokens": missing,
        "required_tokens": required_tokens,
    }
    if missing:
        return (
            False,
            "stage readiness failed: required prompt token(s) unresolved",
            details,
        )
    return (True, "stage readiness passed", details)


_HYPOTHESIS_KEY_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?([A-Za-z][A-Za-z0-9 _-]{0,48})\s*:\s*(.+)$"
)
_HYPOTHESIS_PRIMARY_METRIC_PATTERN = re.compile(
    r"^PrimaryMetric:\s*[^;]+;\s*Unit:\s*[^;]+;\s*Success:\s*.+$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_HYPOTHESIS_NUMBER_PATTERN = re.compile(r"[-+]?\s*\d+(?:\.\d+)?")
_MARKDOWN_DRY_RUN_SECTION_PATTERN = re.compile(
    r"^#{2,6}\s*dry[\s_-]?run\b", flags=re.IGNORECASE | re.MULTILINE
)


def _extract_markdown_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = _HYPOTHESIS_KEY_PATTERN.match(line)
        if not match:
            continue
        raw_key = match.group(1).strip().lower().replace(" ", "_").replace("-", "_")
        raw_value = match.group(2).strip()
        if raw_key and raw_value and raw_key not in values:
            values[raw_key] = raw_value
    return values


def _parse_numeric_delta(value: str) -> float | None:
    match = _HYPOTHESIS_NUMBER_PATTERN.search(str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(" ", ""))
    except Exception:
        return None


def _validate_hypothesis(path: Path) -> None:
    _require_non_empty(path, "hypothesis.md")
    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    key_values = _extract_markdown_key_values(text)

    has_metric = (
        "metric" in key_values
        or "primary_metric" in key_values
        or bool(_HYPOTHESIS_PRIMARY_METRIC_PATTERN.search(text))
    )
    has_target_delta = (
        "target_delta" in key_values
        or "expected_delta" in key_values
        or "success_delta" in key_values
        or "target delta" in lowered
        or "expected delta" in lowered
    )
    has_criteria = (
        "criteria" in key_values
        or "success_criteria" in key_values
        or "operational_success_criteria" in key_values
        or "operational success criteria" in lowered
        or "success criteria" in lowered
    )

    missing: list[str] = []
    if not has_metric:
        missing.append("metric")
    if not has_target_delta:
        missing.append("target_delta")
    if not has_criteria:
        missing.append("criteria")
    if missing:
        raise StageCheckError(
            "hypothesis.md is missing required hypothesis contract field(s): "
            f"{', '.join(missing)}"
        )

    raw_metric_mode = str(key_values.get("metric_mode", "")).strip().lower()
    if raw_metric_mode and raw_metric_mode not in {"maximize", "minimize"}:
        raise StageCheckError(
            "hypothesis.md metric_mode must be either 'maximize' or 'minimize'"
        )
    if not raw_metric_mode:
        raise StageCheckError(
            "hypothesis.md must define metric_mode ('maximize' or 'minimize')"
        )

    target_delta_raw = (
        key_values.get("target_delta")
        or key_values.get("expected_delta")
        or key_values.get("success_delta")
        or ""
    )
    parsed_target_delta = _parse_numeric_delta(target_delta_raw)
    if parsed_target_delta is None:
        raise StageCheckError("hypothesis.md target_delta must include a numeric value")
    if raw_metric_mode == "maximize" and parsed_target_delta <= 0:
        raise StageCheckError(
            "hypothesis.md target_delta must be positive when metric_mode=maximize"
        )
    if raw_metric_mode == "minimize" and parsed_target_delta >= 0:
        raise StageCheckError(
            "hypothesis.md target_delta must be negative when metric_mode=minimize"
        )


def _validate_implementation_plan(path: Path, *, require_dry_run: bool) -> None:
    _require_non_empty(path, "implementation_plan.md")
    if not require_dry_run:
        return
    text = path.read_text(encoding="utf-8")
    if not _MARKDOWN_DRY_RUN_SECTION_PATTERN.search(text):
        raise StageCheckError(
            "implementation_plan.md must include a dedicated dry-run section heading "
            "because verifier policy requires dry_run for this stage"
        )


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
        raise StageCheckError(
            f"design.yaml is not valid YAML at {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise StageCheckError("design.yaml must contain a mapping")

    required = {
        "id",
        "iteration_id",
        "hypothesis_id",
        "entrypoint",
        "compute",
        "metrics",
        "baselines",
    }
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise StageCheckError(
            f"design.yaml missing required keys: {missing}"
            " Ensure design.yaml includes all required fields (see .autolab/schemas/design.schema.json)."
        )

    if str(payload.get("iteration_id", "")).strip() != iteration_id:
        raise StageCheckError(
            "design.yaml iteration_id does not match state.iteration_id"
        )

    entrypoint = payload.get("entrypoint")
    if (
        not isinstance(entrypoint, dict)
        or not str(entrypoint.get("module", "")).strip()
    ):
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


def _validate_review_result(
    path: Path, *, policy_requirements: dict[str, bool] | None = None
) -> str:
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

    missing_checks = sorted(
        set(REVIEW_RESULT_REQUIRED_CHECKS) - set(required_checks.keys())
    )
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
        raise StageCheckError(
            "runs/<run_id>/run_manifest.json missing artifact_sync_to_local mapping"
        )
    sync_status = str(sync.get("status", "")).strip().lower()
    if sync_status not in SYNC_SUCCESS_STATUSES:
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
    docs_update_path = iteration_dir / "docs_update.md"
    _require_non_empty(docs_update_path, "docs_update.md")
    _require_non_empty(iteration_dir / "analysis" / "summary.md", "analysis/summary.md")
    docs_update_text = docs_update_path.read_text(encoding="utf-8")
    normalized_run_id = str(run_id).strip()
    if not normalized_run_id or normalized_run_id.startswith("<"):
        return
    if normalized_run_id not in docs_update_text:
        raise StageCheckError(
            "docs_update.md must reference state.last_run_id to keep documentation traceable"
        )
    metrics_artifact = f"runs/{normalized_run_id}/metrics.json"
    if (
        metrics_artifact not in docs_update_text
        and "metrics.json" not in docs_update_text
    ):
        raise StageCheckError(
            "docs_update.md must reference metrics artifacts (expected runs/<run_id>/metrics.json)"
        )
    manifest_artifact = f"runs/{normalized_run_id}/run_manifest.json"
    if (
        manifest_artifact not in docs_update_text
        and "run_manifest.json" not in docs_update_text
    ):
        raise StageCheckError(
            "docs_update.md must reference run artifacts (expected runs/<run_id>/run_manifest.json)"
        )
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


def _resolve_latest_run_state(
    iteration_dir: Path, *, preferred_run_id: str = ""
) -> tuple[str, str]:
    # Lazy import to avoid circular dependency — _manifest_timestamp lives in
    # __main__ until the utils module is extracted.
    from autolab.utils import _manifest_timestamp

    preferred = str(preferred_run_id).strip()
    manifests: list[Path]
    if preferred:
        preferred_manifest = iteration_dir / "runs" / preferred / "run_manifest.json"
        if preferred_manifest.exists():
            manifests = [preferred_manifest]
        else:
            # Backward compatibility: older artifacts may not use orchestrator-owned
            # run IDs yet. Fall back to latest available manifest when present.
            manifests = sorted(iteration_dir.glob("runs/*/run_manifest.json"))
            if not manifests:
                raise StageCheckError(
                    f"state.last_run_id='{preferred}' but run manifest is missing at {preferred_manifest}"
                )
    else:
        manifests = sorted(iteration_dir.glob("runs/*/run_manifest.json"))
        if not manifests:
            raise StageCheckError(
                f"launch did not produce run_manifest.json under {iteration_dir / 'runs'}"
            )
    manifest_candidates: list[tuple[int, datetime, str, str, dict[str, Any]]] = []
    for manifest_path in manifests:
        payload = _load_dict_json(manifest_path, "runs/<run_id>/run_manifest.json")
        run_dir = manifest_path.parent
        run_id = str(payload.get("run_id", "")).strip() or run_dir.name
        parsed_timestamp = _manifest_timestamp(payload, run_id)
        timestamp = parsed_timestamp or datetime.min.replace(tzinfo=timezone.utc)
        has_timestamp = 1 if parsed_timestamp is not None else 0
        manifest_candidates.append(
            (has_timestamp, timestamp, run_id, str(manifest_path), payload)
        )
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
        if raw_sync in SYNC_SUCCESS_STATUSES:
            sync_status = "completed"
        elif raw_sync:
            sync_status = raw_sync
    launch_payload = payload.get("launch")
    nested_launch_mode = (
        launch_payload.get("mode", "") if isinstance(launch_payload, dict) else ""
    )
    launch_mode = (
        str(
            payload.get("launch_mode")
            or payload.get("host_mode")
            or payload.get("detected_host_mode")
            or nested_launch_mode
        )
        .strip()
        .lower()
    )
    if launch_mode == "slurm" and sync_status not in {"completed", "ok", "success"}:
        raise StageCheckError(
            f"latest run {run_id} has incomplete artifact synchronization for slurm mode: {sync_status}"
        )
    return (run_id, sync_status)


# ---------------------------------------------------------------------------
# Verification step
# ---------------------------------------------------------------------------


def _resolve_verification_stage(
    state: dict[str, Any], stage_override: str | None = None
) -> str:
    stage = str(stage_override or state.get("stage", "")).strip()
    if not stage:
        raise StageCheckError("verification stage is missing from state")
    return stage


def _persist_verification_result(
    repo_root: Path,
    *,
    state: dict[str, Any],
    stage_requested: str,
    stage_effective: str,
    passed: bool,
    message: str,
    details: dict[str, Any],
) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "iteration_id": str(state.get("iteration_id", "")).strip(),
        "experiment_id": str(state.get("experiment_id", "")).strip(),
        "state_stage": str(state.get("stage", "")).strip(),
        "stage_requested": stage_requested,
        "stage_effective": stage_effective,
        "passed": bool(passed),
        "message": str(message).strip(),
        "details": details,
    }
    result_path = repo_root / ".autolab" / "verification_result.json"
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _persist_structured_verifier_results(
    state: dict[str, Any],
    stage: str,
    results: list[dict[str, Any]],
) -> None:
    """Best-effort persist per-verifier structured JSON output to iteration verification dir."""
    try:
        from autolab.state import _resolve_iteration_directory

        iteration_id = str(state.get("iteration_id", "")).strip()
        experiment_id = str(state.get("experiment_id", "")).strip()
        if not iteration_id:
            return
        iteration_dir, _iteration_type = _resolve_iteration_directory(
            Path.cwd(),
            iteration_id=iteration_id,
            experiment_id=experiment_id,
            require_exists=False,
        )
        verification_dir = iteration_dir / "verification"
        verification_dir.mkdir(parents=True, exist_ok=True)
        for result in results:
            structured = result.get("structured")
            if not isinstance(structured, dict):
                continue
            verifier_name = str(
                structured.get("verifier", result.get("name", "unknown"))
            ).strip()
            filename = f"{stage}_{verifier_name}.json"
            output_path = verification_dir / filename
            output_path.write_text(
                json.dumps(structured, indent=2) + "\n", encoding="utf-8"
            )
    except Exception:
        pass


def _build_verification_command_specs(
    repo_root: Path,
    state: dict[str, Any],
    *,
    stage_override: str | None = None,
    auto_mode: bool = False,
) -> tuple[str, dict[str, bool], list[tuple[str, str]]]:
    from autolab.config import (
        _load_verifier_policy,
        _resolve_policy_command,
        _resolve_policy_python_bin,
        _resolve_stage_requirements,
    )
    from autolab.registry import load_registry
    from autolab.state import _resolve_iteration_directory

    policy = _load_verifier_policy(repo_root)
    iteration_id = str(state.get("iteration_id", "")).strip()
    stage = _resolve_verification_stage(state, stage_override=stage_override)
    iteration_dir, _iteration_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=str(state.get("experiment_id", "")).strip(),
        require_exists=False,
    )
    try:
        iteration_path = iteration_dir.relative_to(repo_root).as_posix()
    except ValueError:
        iteration_path = iteration_dir.as_posix()

    registry = load_registry(repo_root)
    reg_verifier_cats = (
        registry[stage].verifier_categories if stage in registry else None
    )
    stage_requirements = _resolve_stage_requirements(
        policy, stage, registry_verifier_categories=reg_verifier_cats
    )
    python_bin = _resolve_policy_python_bin(policy)
    test_command = _resolve_policy_command(
        str(policy.get("test_command", "")).strip(), python_bin=python_bin
    )
    dry_run_command = _resolve_policy_command(
        str(policy.get("dry_run_command", "")).strip(), python_bin=python_bin
    )
    if not dry_run_command and stage_requirements.get("dry_run"):
        raise StageCheckError(
            "verification dry-run command is required by policy but not configured"
        )
    if not test_command and stage_requirements.get("tests"):
        raise StageCheckError(
            "verification test command is required by policy but not configured"
        )

    template_fill_section = policy.get("template_fill", {})
    template_fill_enabled = False
    template_fill_command = ""
    if isinstance(template_fill_section, dict):
        template_fill_enabled = _coerce_bool(
            template_fill_section.get("enabled"), default=False
        )
        stage_enabled = True
        template_fill_stages = template_fill_section.get("stages")
        if isinstance(template_fill_stages, dict):
            stage_enabled = _coerce_bool(template_fill_stages.get(stage), default=True)
        if template_fill_enabled and stage_enabled:
            raw_template_fill_command = str(
                template_fill_section.get("command", "")
            ).strip()
            template_fill_command = _resolve_policy_command(
                raw_template_fill_command, python_bin=python_bin
            )

    template_fill_by_stage = policy.get("template_fill_by_stage", {})
    if isinstance(template_fill_by_stage, dict):
        stage_template_fill = template_fill_by_stage.get(stage)
        if isinstance(stage_template_fill, str) and stage_template_fill.strip():
            template_fill_enabled = True
            template_fill_command = _resolve_policy_command(
                stage_template_fill.strip(), python_bin=python_bin
            )

    command_specs: list[tuple[str, str]] = []
    if stage_requirements["tests"] and test_command:
        command_specs.append(("tests", test_command))
    if stage_requirements["dry_run"] and dry_run_command:
        command_specs.append(
            (
                "dry_run",
                _replace_iteration_placeholders(
                    dry_run_command, iteration_id, iteration_path
                ),
            )
        )
    if template_fill_enabled and template_fill_command:
        command_specs.append(("template_fill", f"{template_fill_command} --json"))

    registry_consistency_path = (
        repo_root / ".autolab" / "verifiers" / "registry_consistency.py"
    )
    if registry_consistency_path.exists():
        command_specs.append(
            (
                "registry_consistency",
                f"{python_bin} .autolab/verifiers/registry_consistency.py --stage {shlex.quote(stage)} --json",
            )
        )

    prompt_registry_contract_path = (
        repo_root / ".autolab" / "verifiers" / "prompt_registry_contract.py"
    )
    if prompt_registry_contract_path.exists():
        command_specs.append(
            (
                "prompt_registry_contract",
                f"{python_bin} .autolab/verifiers/prompt_registry_contract.py --stage {shlex.quote(stage)} --json",
            )
        )

    closed_guard_path = (
        repo_root / ".autolab" / "verifiers" / "closed_experiment_guard.py"
    )
    if closed_guard_path.exists():
        command_specs.append(
            (
                "closed_experiment_guard",
                f"{python_bin} .autolab/verifiers/closed_experiment_guard.py --json",
            )
        )
    if stage_requirements["env_smoke"]:
        command_specs.append(
            ("run_health", f"{python_bin} .autolab/verifiers/run_health.py --json")
        )
        command_specs.append(
            (
                "result_sanity",
                f"{python_bin} .autolab/verifiers/result_sanity.py --json",
            )
        )
    if stage_requirements["docs_target_update"] and stage in {
        "update_docs",
        "implementation_review",
    }:
        command_specs.append(
            ("docs_targets", f"{python_bin} .autolab/verifiers/docs_targets.py --json")
        )
    if stage_requirements.get("consistency", False):
        consistency_checks_path = (
            repo_root / ".autolab" / "verifiers" / "consistency_checks.py"
        )
        if consistency_checks_path.exists():
            command_specs.append(
                (
                    "consistency_checks",
                    f"{python_bin} .autolab/verifiers/consistency_checks.py --stage {shlex.quote(stage)} --json",
                )
            )
    docs_drift_path = repo_root / ".autolab" / "verifiers" / "docs_drift.py"
    if docs_drift_path.exists() and stage == "update_docs":
        command_specs.append(
            (
                "docs_drift",
                f"{python_bin} .autolab/verifiers/docs_drift.py --stage {shlex.quote(stage)} --json",
            )
        )
    plan_lint_path = (
        repo_root / ".autolab" / "verifiers" / "implementation_plan_lint.py"
    )
    if plan_lint_path.exists() and stage == "implementation":
        command_specs.append(
            (
                "implementation_plan_lint",
                f"{python_bin} .autolab/verifiers/implementation_plan_lint.py --stage {shlex.quote(stage)} --json",
            )
        )
    prompt_lint_mode = "enforce"
    prompt_lint_config = policy.get("prompt_lint", {})
    prompt_lint_stage_enabled = True
    if isinstance(prompt_lint_config, dict):
        raw_mode = str(prompt_lint_config.get("mode", "enforce")).strip().lower()
        if raw_mode in {"warn", "enforce"}:
            prompt_lint_mode = raw_mode
    # In auto/runner modes, always enforce prompt lint regardless of policy setting
    if auto_mode:
        prompt_lint_mode = "enforce"
        enabled_by_stage = prompt_lint_config.get("enabled_by_stage", {})
        if isinstance(enabled_by_stage, dict) and stage in enabled_by_stage:
            prompt_lint_stage_enabled = _coerce_bool(
                enabled_by_stage.get(stage), default=True
            )

    if stage_requirements.get("prompt_lint", False) and prompt_lint_stage_enabled:
        prompt_lint_path = repo_root / ".autolab" / "verifiers" / "prompt_lint.py"
        if prompt_lint_path.exists():
            lint_name = (
                "prompt_lint_warn" if prompt_lint_mode == "warn" else "prompt_lint"
            )
            command_specs.append(
                (
                    lint_name,
                    f"{python_bin} .autolab/verifiers/prompt_lint.py --stage {shlex.quote(stage)} --json",
                )
            )
    if stage_requirements["schema"]:
        command_specs.append(
            (
                "schema_checks",
                f"{python_bin} .autolab/verifiers/schema_checks.py --stage {shlex.quote(stage)} --json",
            )
        )

    if not command_specs:
        command_specs.append(("noop", "true"))
    return (stage, stage_requirements, command_specs)


def _run_verification_step_detailed(
    repo_root: Path,
    state: dict[str, Any],
    *,
    stage_override: str | None = None,
    auto_mode: bool = False,
) -> tuple[bool, str, dict[str, Any]]:
    from autolab.utils import _append_log, _compact_log_text

    stage_requested = str(stage_override or state.get("stage", "")).strip()
    try:
        stage, stage_requirements, command_specs = _build_verification_command_specs(
            repo_root, state, stage_override=stage_override, auto_mode=auto_mode
        )
    except StageCheckError as exc:
        stage = str(stage_override or state.get("stage", "")).strip()
        details = {
            "stage": stage,
            "requirements": {},
            "commands": [],
        }
        message = f"verification failed: {exc}"
        _persist_verification_result(
            repo_root,
            state=state,
            stage_requested=stage_requested,
            stage_effective=stage,
            passed=False,
            message=message,
            details=details,
        )
        return (False, message, details)

    results: list[dict[str, Any]] = []
    warning_count = 0
    verifier_env = os.environ.copy() if auto_mode else None
    if verifier_env is not None:
        verifier_env["AUTOLAB_AUTO_MODE"] = "1"
    for command_name, command in command_specs:
        if not command.strip():
            continue
        started = time.monotonic()
        try:
            process = subprocess.run(
                command,
                cwd=repo_root,
                shell=True,
                text=True,
                capture_output=True,
                check=False,
                timeout=VERIFIER_COMMAND_TIMEOUT_SECONDS,
                env=verifier_env,
            )
            duration_seconds = round(time.monotonic() - started, 3)
        except subprocess.TimeoutExpired:
            detail = _compact_log_text(f"verification command timed out: {command}")
            _append_log(
                repo_root,
                f"verification timeout command={command_name} detail={detail}",
            )
            results.append(
                {
                    "name": command_name,
                    "command": command,
                    "status": "timeout",
                    "returncode": None,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "detail": detail,
                }
            )
            details = {
                "stage": stage,
                "requirements": stage_requirements,
                "commands": results,
            }
            message = f"verification failed: {detail}"
            _persist_verification_result(
                repo_root,
                state=state,
                stage_requested=stage_requested,
                stage_effective=stage,
                passed=False,
                message=message,
                details=details,
            )
            return (False, message, details)
        except OSError as exc:
            detail = _compact_log_text(f"verification command failed to start: {exc}")
            _append_log(
                repo_root,
                f"verification command failed command={command_name} detail={detail}",
            )
            results.append(
                {
                    "name": command_name,
                    "command": command,
                    "status": "error",
                    "returncode": None,
                    "duration_seconds": round(time.monotonic() - started, 3),
                    "detail": detail,
                }
            )
            details = {
                "stage": stage,
                "requirements": stage_requirements,
                "commands": results,
            }
            message = f"verification failed: {detail}"
            _persist_verification_result(
                repo_root,
                state=state,
                stage_requested=stage_requested,
                stage_effective=stage,
                passed=False,
                message=message,
                details=details,
            )
            return (False, message, details)

        stdout = (process.stdout or "").strip()
        stderr = (process.stderr or "").strip()
        detail = _compact_log_text((stderr or stdout or "").strip())
        non_blocking_warning = command_name == "prompt_lint_warn"
        status = (
            "pass"
            if process.returncode == 0
            else ("warn" if non_blocking_warning else "fail")
        )
        result_payload = {
            "name": command_name,
            "command": command,
            "status": status,
            "returncode": process.returncode,
            "duration_seconds": duration_seconds,
            "stdout": _compact_log_text(stdout, limit=400) if stdout else "",
            "stderr": _compact_log_text(stderr, limit=400) if stderr else "",
        }
        if detail:
            result_payload["detail"] = detail
        # Attempt to parse structured JSON envelope from verifier stdout
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict) and "status" in parsed and "verifier" in parsed:
                result_payload["structured"] = parsed
        except (json.JSONDecodeError, ValueError):
            pass
        results.append(result_payload)

        if process.returncode != 0 and non_blocking_warning:
            warning_count += 1
            _append_log(
                repo_root,
                f"verification warning command={command_name} detail={detail or 'prompt lint warning mode'}",
            )
            continue

        if process.returncode != 0:
            _append_log(
                repo_root, f"verification failed command={command_name} detail={detail}"
            )
            details = {
                "stage": stage,
                "requirements": stage_requirements,
                "commands": results,
            }
            message = f"verification failed: {detail or 'verification command returned non-zero'}"
            _persist_verification_result(
                repo_root,
                state=state,
                stage_requested=stage_requested,
                stage_effective=stage,
                passed=False,
                message=message,
                details=details,
            )
            _persist_structured_verifier_results(state, stage, results)
            return (False, message, details)

    details = {
        "stage": stage,
        "requirements": stage_requirements,
        "commands": results,
    }
    message = f"verification passed ({len(results)} command(s))"
    if warning_count > 0:
        message = f"{message}; warnings={warning_count}"
    _persist_verification_result(
        repo_root,
        state=state,
        stage_requested=stage_requested,
        stage_effective=stage,
        passed=True,
        message=message,
        details=details,
    )
    _persist_structured_verifier_results(state, stage, results)
    return (True, message, details)


def _run_verification_step(
    repo_root: Path,
    state: dict[str, Any],
    *,
    stage_override: str | None = None,
    auto_mode: bool = False,
) -> tuple[bool, str]:
    passed, message, _details = _run_verification_step_detailed(
        repo_root,
        state,
        stage_override=stage_override,
        auto_mode=auto_mode,
    )
    return (passed, message)
