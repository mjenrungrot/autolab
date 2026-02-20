"""Autolab stage evaluation — dispatch table and per-stage handlers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover — optional dependency
    yaml = None  # type: ignore[assignment]

from autolab.constants import ACTIVE_STAGES, SYNC_SUCCESS_STATUSES
from autolab.models import EvalResult, StageCheckError
from autolab.config import (
    _load_slurm_lifecycle_strict_policy,
    _load_verifier_policy,
    _resolve_stage_requirements,
)
from autolab.state import _resolve_iteration_directory
from autolab.validators import (
    _require_non_empty,
    _validate_design,
    _validate_extract,
    _validate_hypothesis,
    _validate_implementation_plan,
    _validate_launch,
    _validate_review_result,
    _validate_slurm_job_ledger_entry,
    _validate_update_docs,
    _resolve_latest_run_state,
    _load_dict_json,
)
from autolab.utils import _detect_priority_host_mode


# ---------------------------------------------------------------------------
# Per-stage evaluator functions
# ---------------------------------------------------------------------------


def _finalize_slurm_manifest_after_extract(
    repo_root: Path, iteration_dir: Path, run_id: str
) -> None:
    if not _load_slurm_lifecycle_strict_policy(repo_root):
        return
    if not run_id or run_id.startswith("<"):
        return

    manifest_path = iteration_dir / "runs" / run_id / "run_manifest.json"
    if not manifest_path.exists():
        return
    payload = _load_dict_json(manifest_path, "runs/<run_id>/run_manifest.json")
    host_mode = str(
        payload.get("host_mode") or payload.get("launch_mode") or ""
    ).strip()
    if host_mode.lower() != "slurm":
        return

    status = str(payload.get("status", "")).strip().lower()
    if status in {"failed", "partial"}:
        return
    if status == "completed":
        return
    if status != "synced":
        raise StageCheckError(
            "strict SLURM lifecycle requires run_manifest.status='synced' before extract_results finalizes completion"
        )

    payload["status"] = "completed"
    timestamps = payload.get("timestamps")
    if not isinstance(timestamps, dict):
        timestamps = {}
    completed_at = str(timestamps.get("completed_at", "")).strip()
    if not completed_at:
        timestamps["completed_at"] = (
            datetime.now(timezone.utc)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
    payload["timestamps"] = timestamps
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _eval_hypothesis(
    repo_root: Path,
    state: dict[str, Any],
    iteration_dir: Path,
    iteration_id: str,
) -> EvalResult:
    _validate_hypothesis(iteration_dir / "hypothesis.md")
    return EvalResult("design", "complete", "'hypothesis' checks passed")


def _eval_design(
    repo_root: Path,
    state: dict[str, Any],
    iteration_dir: Path,
    iteration_id: str,
) -> EvalResult:
    _validate_design(iteration_dir / "design.yaml", iteration_id)
    return EvalResult("implementation", "complete", "'design' checks passed")


def _eval_implementation(
    repo_root: Path,
    state: dict[str, Any],
    iteration_dir: Path,
    iteration_id: str,
) -> EvalResult:
    policy_requirements = _resolve_stage_requirements(
        _load_verifier_policy(repo_root), "implementation"
    )
    _validate_implementation_plan(
        iteration_dir / "implementation_plan.md",
        require_dry_run=bool(policy_requirements.get("dry_run", False)),
    )
    return EvalResult(
        "implementation_review", "complete", "'implementation' checks passed"
    )


def _eval_implementation_review(
    repo_root: Path,
    state: dict[str, Any],
    iteration_dir: Path,
    iteration_id: str,
) -> EvalResult:
    _require_non_empty(
        iteration_dir / "implementation_review.md", "implementation_review.md"
    )
    policy_requirements = _resolve_stage_requirements(
        _load_verifier_policy(repo_root), "implementation_review"
    )
    review_status = _validate_review_result(
        iteration_dir / "review_result.json",
        policy_requirements=policy_requirements,
    )
    if review_status == "pass":
        return EvalResult("launch", "complete", "'implementation_review' checks passed")
    if review_status == "needs_retry":
        return EvalResult(
            "implementation",
            "complete",
            "'implementation_review' requested retry",
            needs_retry=True,
        )
    return EvalResult("human_review", "failed", "'implementation_review' failed")


def _eval_launch(
    repo_root: Path,
    state: dict[str, Any],
    iteration_dir: Path,
    iteration_id: str,
) -> EvalResult:
    # -- review_result gate --------------------------------------------------
    review_result_path = iteration_dir / "review_result.json"
    if review_result_path.exists():
        review_status = _validate_review_result(
            review_result_path,
            policy_requirements=_resolve_stage_requirements(
                _load_verifier_policy(repo_root), "implementation_review"
            ),
        )
        if review_status != "pass":
            raise StageCheckError("launch requires review_result.json status=pass")

    # -- design location check -----------------------------------------------
    design_location = ""
    design_path = iteration_dir / "design.yaml"
    if design_path.exists():
        if yaml is None:
            raise StageCheckError("launch validation requires PyYAML")
        try:
            design_payload = yaml.safe_load(design_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise StageCheckError(
                f"launch validation could not parse design.yaml: {exc}"
            ) from exc
        compute = (
            design_payload.get("compute", {})
            if isinstance(design_payload, dict)
            else {}
        )
        design_location = str(compute.get("location", "")).strip().lower()

    # -- launch artefact validation ------------------------------------------
    _validate_launch(iteration_dir)

    # -- run manifest & sync status ------------------------------------------
    preferred_run_id = (
        str(state.get("pending_run_id", "")).strip()
        or str(state.get("last_run_id", "")).strip()
    )
    run_id, sync_status = _resolve_latest_run_state(
        iteration_dir,
        preferred_run_id=preferred_run_id,
    )
    if run_id:
        state["last_run_id"] = run_id
        manifest_path = iteration_dir / "runs" / run_id / "run_manifest.json"
        manifest_payload = _load_dict_json(
            manifest_path, "runs/<run_id>/run_manifest.json"
        )
        manifest_mode = (
            str(
                manifest_payload.get("host_mode")
                or manifest_payload.get("launch_mode")
                or manifest_payload.get("detected_host_mode")
            )
            .strip()
            .lower()
        )
        raw_resolved = manifest_mode or _detect_priority_host_mode()
        # Normalize internal "slurm_interactive" signal to "slurm" for
        # design-location comparison — the manifest uses "slurm" in both cases.
        resolved_launch_mode = (
            "slurm" if raw_resolved == "slurm_interactive" else raw_resolved
        )
        if (
            design_location
            and resolved_launch_mode
            and design_location != resolved_launch_mode
        ):
            raise StageCheckError(
                "launch host-mode mismatch: "
                f"design.compute.location='{design_location}' "
                f"but resolved host mode='{resolved_launch_mode}'"
            )
        _validate_slurm_job_ledger_entry(
            repo_root,
            manifest_path=manifest_path,
            payload=manifest_payload,
            stage="launch",
        )

    state["sync_status"] = sync_status
    return EvalResult("slurm_monitor", "complete", "'launch' checks passed")


def _eval_slurm_monitor(
    repo_root: Path,
    state: dict[str, Any],
    iteration_dir: Path,
    iteration_id: str,
) -> EvalResult:
    """Evaluate slurm_monitor stage -- auto-skip for local runs."""

    def _normalize_sync_status(raw: str) -> str:
        lowered = str(raw).strip().lower()
        if lowered in SYNC_SUCCESS_STATUSES:
            return "completed"
        return lowered

    run_id = (
        str(state.get("pending_run_id", "")).strip()
        or str(state.get("last_run_id", "")).strip()
    )
    if not run_id:
        return EvalResult(
            "extract_results", "complete", "slurm_monitor skipped (no run_id)"
        )

    manifest_path = iteration_dir / "runs" / run_id / "run_manifest.json"
    manifest = (
        _load_dict_json(manifest_path, "run_manifest.json")
        if manifest_path.exists()
        else {}
    )

    raw_host_mode = (
        str(
            manifest.get("host_mode")
            or manifest.get("launch_mode")
            or _detect_priority_host_mode()
        )
        .strip()
        .lower()
    )
    # Normalize internal "slurm_interactive" signal to "slurm" — the manifest
    # uses "slurm" in both interactive and batch cases.
    host_mode = "slurm" if raw_host_mode == "slurm_interactive" else raw_host_mode

    if host_mode != "slurm":
        return EvalResult(
            "extract_results", "complete", "slurm_monitor skipped (local run)"
        )

    run_status = str(manifest.get("status", "")).strip().lower()
    sync_block = manifest.get("artifact_sync_to_local")
    sync_status = ""
    if isinstance(sync_block, dict):
        sync_status = str(sync_block.get("status", "")).strip().lower()
    if sync_status:
        state["sync_status"] = _normalize_sync_status(sync_status)

    strict_lifecycle = _load_slurm_lifecycle_strict_policy(repo_root)
    if strict_lifecycle:
        if run_status in {"synced", "completed"}:
            return EvalResult(
                "extract_results",
                "complete",
                f"slurm_monitor: strict lifecycle ready (status={run_status})",
            )
        if run_status in {"failed", "partial"}:
            return EvalResult(
                "extract_results",
                "complete",
                f"slurm_monitor: terminal status '{run_status}'",
            )
        if (
            sync_status in SYNC_SUCCESS_STATUSES
            and run_status
            and run_status not in {"synced", "completed"}
        ):
            raise StageCheckError(
                "strict SLURM lifecycle requires run_manifest.status='synced' or 'completed' after sync success and before extraction"
            )
    else:
        # Backward-compatible mode: proceed once artifacts are local-ready
        # or terminal failure is recorded.
        if sync_status in SYNC_SUCCESS_STATUSES:
            return EvalResult(
                "extract_results", "complete", "slurm_monitor: artifacts synced"
            )
        if run_status in {"synced", "failed", "partial"}:
            return EvalResult(
                "extract_results",
                "complete",
                f"slurm_monitor: terminal status '{run_status}'",
            )

    return EvalResult(
        "slurm_monitor",
        "complete",
        "slurm_monitor: waiting for scheduler/sync readiness",
    )


def _eval_extract_results(
    repo_root: Path,
    state: dict[str, Any],
    iteration_dir: Path,
    iteration_id: str,
) -> EvalResult:
    run_group = state.get("run_group", [])
    if isinstance(run_group, list) and run_group:
        # Multi-run: validate aggregated metrics at the base run_id
        _validate_extract(iteration_dir, state["last_run_id"])
        _finalize_slurm_manifest_after_extract(
            repo_root, iteration_dir, state["last_run_id"]
        )
        # Also verify per-replicate metrics exist
        missing_replicates = []
        for rid in run_group:
            replicate_metrics = iteration_dir / "runs" / rid / "metrics.json"
            if not replicate_metrics.exists():
                missing_replicates.append(rid)
        if missing_replicates:
            raise StageCheckError(
                f"multi-run extract missing replicate metrics for: {', '.join(missing_replicates)}"
            )
        return EvalResult(
            "update_docs", "complete", "'extract_results' multi-run checks passed"
        )
    _validate_extract(iteration_dir, state["last_run_id"])
    _finalize_slurm_manifest_after_extract(
        repo_root, iteration_dir, state["last_run_id"]
    )
    return EvalResult("update_docs", "complete", "'extract_results' checks passed")


def _eval_update_docs(
    repo_root: Path,
    state: dict[str, Any],
    iteration_dir: Path,
    iteration_id: str,
) -> EvalResult:
    _validate_update_docs(repo_root, iteration_dir, str(state.get("last_run_id", "")))
    return EvalResult("decide_repeat", "complete", "'update_docs' checks passed")


# ---------------------------------------------------------------------------
# Dispatch table — maps canonical stage name to its evaluator function
# ---------------------------------------------------------------------------

_STAGE_EVALUATORS = {
    "hypothesis": _eval_hypothesis,
    "design": _eval_design,
    "implementation": _eval_implementation,
    "implementation_review": _eval_implementation_review,
    "launch": _eval_launch,
    "slurm_monitor": _eval_slurm_monitor,
    "extract_results": _eval_extract_results,
    "update_docs": _eval_update_docs,
}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def _evaluate_stage(repo_root: Path, state: dict[str, Any]) -> EvalResult:
    """Evaluate the current stage and return an :class:`EvalResult`.

    This is the single entry point called by the state-machine driver.  It
    resolves the iteration directory, looks up the per-stage handler in the
    dispatch table, and delegates to it.
    """
    stage = state["stage"]
    iteration_id = state["iteration_id"]
    iteration_dir, _iteration_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=str(state.get("experiment_id", "")).strip(),
        require_exists=True,
    )

    evaluator = _STAGE_EVALUATORS.get(stage)
    if evaluator is None:
        raise StageCheckError(f"unsupported stage '{stage}'")
    return evaluator(repo_root, state, iteration_dir, iteration_id)
