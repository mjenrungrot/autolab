"""Autolab stage evaluation — dispatch table and per-stage handlers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover — optional dependency
    yaml = None  # type: ignore[assignment]

from autolab.constants import ACTIVE_STAGES
from autolab.models import EvalResult, StageCheckError
from autolab.config import _load_verifier_policy, _resolve_stage_requirements
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
    return EvalResult("implementation_review", "complete", "'implementation' checks passed")


def _eval_implementation_review(
    repo_root: Path,
    state: dict[str, Any],
    iteration_dir: Path,
    iteration_id: str,
) -> EvalResult:
    _require_non_empty(iteration_dir / "implementation_review.md", "implementation_review.md")
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
        manifest_mode = str(
            manifest_payload.get("host_mode")
            or manifest_payload.get("launch_mode")
            or manifest_payload.get("detected_host_mode")
        ).strip().lower()
        resolved_launch_mode = manifest_mode or _detect_priority_host_mode()
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
    return EvalResult("extract_results", "complete", "'launch' checks passed")


def _eval_extract_results(
    repo_root: Path,
    state: dict[str, Any],
    iteration_dir: Path,
    iteration_id: str,
) -> EvalResult:
    _validate_extract(iteration_dir, state["last_run_id"])
    return EvalResult("update_docs", "complete", "'extract_results' checks passed")


def _eval_update_docs(
    repo_root: Path,
    state: dict[str, Any],
    iteration_dir: Path,
    iteration_id: str,
) -> EvalResult:
    _validate_update_docs(
        repo_root, iteration_dir, str(state.get("last_run_id", ""))
    )
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
