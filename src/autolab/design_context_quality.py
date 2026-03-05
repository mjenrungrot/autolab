from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from autolab.sidecar_context import resolve_context_sidecars
from autolab.sidecar_tools import (
    DISCUSS_COLLECTIONS,
    RESEARCH_COLLECTIONS,
    resolve_context_ref,
)
from autolab.state import _resolve_iteration_directory
from autolab.utils import _utc_now, _write_json


@dataclass(frozen=True)
class DesignContextQualityResult:
    report_path: Path
    payload: dict[str, Any]


def _load_design_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        return {}
    if not path.exists():
        return {}
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def _count_effective_items(
    payload: dict[str, Any], collections: tuple[str, ...]
) -> int:
    total = 0
    for collection_name in collections:
        entries = payload.get(collection_name)
        if isinstance(entries, list):
            total += sum(1 for entry in entries if isinstance(entry, dict))
    return total


def build_design_context_quality(
    repo_root: Path,
    state: dict[str, Any],
    *,
    write_outputs: bool,
) -> DesignContextQualityResult:
    iteration_id = str(state.get("iteration_id", "")).strip()
    experiment_id = str(state.get("experiment_id", "")).strip()
    iteration_dir, _iteration_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        require_exists=False,
    )
    report_path = iteration_dir / "design_context_quality.json"
    design_payload = _load_design_yaml(iteration_dir / "design.yaml")
    resolution_scope_kind = "experiment" if iteration_id else "project_wide"
    context_resolution = resolve_context_sidecars(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        scope_kind=resolution_scope_kind,
    )

    effective_discuss = context_resolution.get("effective_discuss")
    if not isinstance(effective_discuss, dict):
        effective_discuss = {name: [] for name in DISCUSS_COLLECTIONS}
    effective_research = context_resolution.get("effective_research")
    if not isinstance(effective_research, dict):
        effective_research = {name: [] for name in RESEARCH_COLLECTIONS}

    discuss_items = _count_effective_items(effective_discuss, DISCUSS_COLLECTIONS)
    research_items = _count_effective_items(effective_research, RESEARCH_COLLECTIONS)
    open_questions = len(
        [
            entry
            for entry in effective_discuss.get("open_questions", [])
            if isinstance(entry, dict)
        ]
    )
    promotion_candidates = len(
        [
            entry
            for entry in effective_discuss.get("promotion_candidates", [])
            if isinstance(entry, dict)
        ]
    )
    diagnostics = [
        str(item).strip()
        for item in context_resolution.get("diagnostics", [])
        if str(item).strip()
    ]

    raw_requirements = design_payload.get("implementation_requirements")
    requirements = raw_requirements if isinstance(raw_requirements, list) else []
    requirements_total = 0
    requirements_with_context_refs = 0
    context_refs_total = 0
    resolved_context_refs = 0
    promoted_constraints_total = 0
    resolved_promoted_constraints = 0
    resolved_discuss_context_refs = 0
    resolved_research_context_refs = 0
    requirements_with_resolved_context = 0

    for requirement in requirements:
        if not isinstance(requirement, dict):
            continue
        requirements_total += 1
        requirement_has_resolved_context = False
        requirement_scope_kind = str(requirement.get("scope_kind", "")).strip()
        context_refs = requirement.get("context_refs")
        if isinstance(context_refs, list) and context_refs:
            requirements_with_context_refs += 1
            for raw_ref in context_refs:
                ref = str(raw_ref).strip()
                if not ref:
                    continue
                context_refs_total += 1
                resolved_ref = resolve_context_ref(
                    repo_root,
                    iteration_id=iteration_id,
                    experiment_id=experiment_id,
                    raw_ref=ref,
                    design_payload=design_payload,
                    scope_kind=requirement_scope_kind,
                    context_resolution=context_resolution,
                )
                if resolved_ref is not None:
                    resolved_context_refs += 1
                    requirement_has_resolved_context = True
                    if resolved_ref.get("kind") == "sidecar":
                        if (
                            str(resolved_ref.get("sidecar_kind", "")).strip()
                            == "discuss"
                        ):
                            resolved_discuss_context_refs += 1
                        elif (
                            str(resolved_ref.get("sidecar_kind", "")).strip()
                            == "research"
                        ):
                            resolved_research_context_refs += 1
                else:
                    diagnostics.append(f"unresolved design context_ref: {ref}")
        promoted_constraints = requirement.get("promoted_constraints")
        if isinstance(promoted_constraints, list):
            for entry in promoted_constraints:
                if not isinstance(entry, dict):
                    continue
                promoted_constraints_total += 1
                source_ref = str(entry.get("source_ref", "")).strip()
                if not source_ref:
                    continue
                resolved_source_ref = resolve_context_ref(
                    repo_root,
                    iteration_id=iteration_id,
                    experiment_id=experiment_id,
                    raw_ref=source_ref,
                    design_payload=design_payload,
                    scope_kind="experiment",
                    context_resolution=context_resolution,
                )
                if resolved_source_ref is None:
                    diagnostics.append(
                        f"unresolved promoted constraint source_ref: {source_ref}"
                    )
                    continue
                resolved_promoted_constraints += 1
                requirement_has_resolved_context = True

        if requirement_has_resolved_context:
            requirements_with_resolved_context += 1

    context_mode = "present" if (discuss_items + research_items) > 0 else "absent"
    score_max = max(1, requirements_total * 2)
    score_value = min(
        score_max,
        requirements_with_resolved_context
        + resolved_discuss_context_refs
        + resolved_promoted_constraints,
    )

    payload = {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "context_mode": context_mode,
        "available": {
            "discuss_items": discuss_items,
            "research_items": research_items,
            "open_questions": open_questions,
            "promotion_candidates": promotion_candidates,
        },
        "uptake": {
            "requirements_total": requirements_total,
            "requirements_with_context_refs": requirements_with_context_refs,
            "requirements_with_resolved_context": requirements_with_resolved_context,
            "context_refs_total": context_refs_total,
            "resolved_context_refs": resolved_context_refs,
            "resolved_discuss_context_refs": resolved_discuss_context_refs,
            "resolved_research_context_refs": resolved_research_context_refs,
            "promoted_constraints_total": promoted_constraints_total,
            "resolved_promoted_constraints": resolved_promoted_constraints,
        },
        "score": {
            "value": score_value,
            "max": score_max,
        },
        "diagnostics": sorted(set(diagnostics)),
    }
    if write_outputs:
        _write_json(report_path, payload)
    return DesignContextQualityResult(report_path=report_path, payload=payload)


__all__ = ["DesignContextQualityResult", "build_design_context_quality"]
