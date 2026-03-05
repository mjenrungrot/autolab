from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autolab.scope import _resolve_scope_context
from autolab.utils import _path_fingerprint

_PROJECT_MAP_COMPONENT = "project_map"
_CONTEXT_DELTA_COMPONENT = "context_delta"
_PROJECT_DISCUSS_COMPONENT = "project_wide_discuss"
_PROJECT_RESEARCH_COMPONENT = "project_wide_research"
_EXPERIMENT_DISCUSS_COMPONENT = "experiment_discuss"
_EXPERIMENT_RESEARCH_COMPONENT = "experiment_research"

_DISCUSS_COLLECTIONS = (
    "locked_decisions",
    "preferences",
    "constraints",
    "open_questions",
    "promotion_candidates",
)
_RESEARCH_COLLECTIONS = ("questions", "findings", "recommendations", "sources")
_COMPONENT_ORDER_BY_SCOPE = {
    "project_wide": [
        _PROJECT_MAP_COMPONENT,
        _PROJECT_DISCUSS_COMPONENT,
        _PROJECT_RESEARCH_COMPONENT,
    ],
    "experiment": [
        _PROJECT_MAP_COMPONENT,
        _PROJECT_DISCUSS_COMPONENT,
        _PROJECT_RESEARCH_COMPONENT,
        _CONTEXT_DELTA_COMPONENT,
        _EXPERIMENT_DISCUSS_COMPONENT,
        _EXPERIMENT_RESEARCH_COMPONENT,
    ],
}


def _repo_relative(repo_root: Path, path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return (
            path.resolve(strict=False)
            .relative_to(repo_root.resolve(strict=False))
            .as_posix()
        )
    except Exception:
        return str(path)


def _resolve_repo_path(repo_root: Path, raw_path: Any) -> Path | None:
    candidate_text = str(raw_path or "").strip()
    if not candidate_text:
        return None
    try:
        candidate = Path(candidate_text).expanduser()
    except Exception:
        return None
    resolved = (
        candidate.resolve(strict=False)
        if candidate.is_absolute()
        else (repo_root / candidate).resolve(strict=False)
    )
    try:
        resolved.relative_to(repo_root.resolve(strict=False))
    except Exception:
        return None
    return resolved


def _fingerprint_for_path(repo_root: Path, path: Path | None) -> str:
    relative_path = _repo_relative(repo_root, path)
    if not relative_path:
        return "<missing>"
    return _path_fingerprint(repo_root, relative_path)


def _read_json_mapping(
    path: Path | None, *, repo_root: Path | None = None
) -> tuple[dict[str, Any] | None, str]:
    if path is None:
        return (None, "path unavailable")
    resolved_path = path
    if repo_root is not None:
        resolved_path = _resolve_repo_path(repo_root, path)
        if resolved_path is None:
            return (None, "invalid: path escapes repository root")
    if not resolved_path.exists():
        return (None, "missing")
    if not resolved_path.is_file():
        return (None, "invalid: expected regular file")
    try:
        payload = json.loads(resolved_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return (None, f"invalid: {exc}")
    if not isinstance(payload, dict):
        return (None, "invalid: expected JSON object")
    return (payload, "")


def _unique_strings(values: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        output.append(text)
    return output


def _dedupe_dependency_refs(refs: list[dict[str, str]]) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for ref in refs:
        normalized = {
            "path": str(ref.get("path", "")).strip(),
            "fingerprint": str(ref.get("fingerprint", "")).strip(),
            "reason": str(ref.get("reason", "")).strip(),
        }
        key = (
            normalized["path"],
            normalized["fingerprint"],
            normalized["reason"],
        )
        if not normalized["path"] or key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output


def _normalize_dependency_refs(
    raw_value: Any,
) -> tuple[list[dict[str, str]], list[str]]:
    refs: list[dict[str, str]] = []
    errors: list[str] = []
    if raw_value is None:
        return (refs, errors)
    if not isinstance(raw_value, list):
        return (refs, ["must be a list"])
    for index, entry in enumerate(raw_value):
        if not isinstance(entry, dict):
            errors.append(f"[{index}] must be an object")
            continue
        path_text = str(entry.get("path", "")).strip()
        fingerprint = str(entry.get("fingerprint", "")).strip()
        if not path_text:
            errors.append(f"[{index}] missing path")
            continue
        if not fingerprint:
            errors.append(f"[{index}] missing fingerprint")
            continue
        refs.append(
            {
                "path": path_text,
                "fingerprint": fingerprint,
                "reason": str(entry.get("reason", "")).strip(),
            }
        )
    return (_dedupe_dependency_refs(refs), errors)


def _validate_sidecar_items(
    payload: dict[str, Any], *, collections: tuple[str, ...]
) -> list[str]:
    errors: list[str] = []
    for field_name in collections:
        value = payload.get(field_name)
        if not isinstance(value, list):
            errors.append(f"{field_name} must be a list")
            continue
        seen_ids: set[str] = set()
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                errors.append(f"{field_name}[{index}] must be an object")
                continue
            item_id = str(item.get("id", "")).strip()
            summary = str(item.get("summary", "")).strip()
            if not item_id:
                errors.append(f"{field_name}[{index}] missing id")
                continue
            if item_id in seen_ids:
                errors.append(f"{field_name}[{index}] duplicates id '{item_id}'")
            seen_ids.add(item_id)
            if not summary:
                errors.append(f"{field_name}[{index}] missing summary")
    return errors


def _validate_sidecar_payload(
    payload: dict[str, Any],
    *,
    repo_root: Path,
    expected_kind: str,
    expected_scope_kind: str,
    expected_scope_root: Path | None,
    iteration_id: str,
    experiment_id: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[str]]:
    errors: list[str] = []
    if str(payload.get("sidecar_kind", "")).strip() != expected_kind:
        errors.append(f"sidecar_kind must be '{expected_kind}'")
    if str(payload.get("scope_kind", "")).strip() != expected_scope_kind:
        errors.append(f"scope_kind must be '{expected_scope_kind}'")
    raw_scope_root = str(payload.get("scope_root", "")).strip()
    if not raw_scope_root:
        errors.append("scope_root must be non-empty")
    else:
        resolved_scope_root = _resolve_repo_path(repo_root, raw_scope_root)
        if resolved_scope_root is None:
            errors.append("scope_root must resolve inside repo root")
        elif expected_scope_root is not None and (
            resolved_scope_root != expected_scope_root.resolve(strict=False)
        ):
            errors.append(
                "scope_root differs from active scope root "
                f"({resolved_scope_root} != {expected_scope_root.resolve(strict=False)})"
            )
    if not str(payload.get("generated_at", "")).strip():
        errors.append("generated_at must be non-empty")

    payload_iteration_id = str(payload.get("iteration_id", "")).strip()
    payload_experiment_id = str(payload.get("experiment_id", "")).strip()
    if expected_scope_kind == "project_wide":
        if payload_iteration_id:
            errors.append("iteration_id must be omitted for project-wide sidecars")
        if payload_experiment_id:
            errors.append("experiment_id must be omitted for project-wide sidecars")
    else:
        if not payload_iteration_id:
            errors.append("iteration_id must be non-empty for experiment sidecars")
        if not payload_experiment_id:
            errors.append("experiment_id must be non-empty for experiment sidecars")
        if (
            iteration_id
            and payload_iteration_id
            and payload_iteration_id != iteration_id
        ):
            errors.append(
                f"iteration_id differs from requested iteration ({payload_iteration_id} != {iteration_id})"
            )
        if (
            experiment_id
            and payload_experiment_id
            and payload_experiment_id != experiment_id
        ):
            errors.append(
                f"experiment_id differs from requested experiment ({payload_experiment_id} != {experiment_id})"
            )

    derived_from_refs, derived_errors = _normalize_dependency_refs(
        payload.get("derived_from")
    )
    stale_if_refs, stale_if_errors = _normalize_dependency_refs(payload.get("stale_if"))
    if derived_errors:
        errors.append("derived_from " + "; ".join(derived_errors))
    if stale_if_errors:
        errors.append("stale_if " + "; ".join(stale_if_errors))

    collections = (
        _DISCUSS_COLLECTIONS if expected_kind == "discuss" else _RESEARCH_COLLECTIONS
    )
    errors.extend(_validate_sidecar_items(payload, collections=collections))
    return (derived_from_refs, stale_if_refs, errors)


def _evaluate_dependency_staleness(
    repo_root: Path,
    *,
    refs: list[dict[str, str]],
) -> list[str]:
    reasons: list[str] = []
    for ref in refs:
        resolved = _resolve_repo_path(repo_root, ref.get("path", ""))
        path_text = (
            _repo_relative(repo_root, resolved) or str(ref.get("path", "")).strip()
        )
        if resolved is None:
            reasons.append(f"{path_text}: dependency path escapes repository root")
            continue
        current_fingerprint = _fingerprint_for_path(repo_root, resolved)
        recorded_fingerprint = str(ref.get("fingerprint", "")).strip()
        if current_fingerprint != recorded_fingerprint:
            reasons.append(
                f"{path_text}: fingerprint changed ({recorded_fingerprint} != {current_fingerprint})"
            )
    return reasons


def _empty_effective(kind: str) -> dict[str, list[dict[str, Any]]]:
    collections = _DISCUSS_COLLECTIONS if kind == "discuss" else _RESEARCH_COLLECTIONS
    return {field_name: [] for field_name in collections}


def _merge_effective_items(
    *,
    kind: str,
    components: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    output = _empty_effective(kind)
    collections = _DISCUSS_COLLECTIONS if kind == "discuss" else _RESEARCH_COLLECTIONS
    for field_name in collections:
        selected_by_id: dict[str, dict[str, Any]] = {}
        ordered_ids: list[str] = []
        for component in components:
            if not bool(component.get("selected")):
                continue
            payload = component.get("_payload")
            if not isinstance(payload, dict):
                continue
            entries = payload.get(field_name)
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                item_id = str(entry.get("id", "")).strip()
                if not item_id:
                    continue
                if item_id in ordered_ids:
                    ordered_ids = [
                        existing for existing in ordered_ids if existing != item_id
                    ]
                ordered_ids.append(item_id)
                prior = selected_by_id.get(item_id)
                prior_sources: list[str] = []
                if isinstance(prior, dict):
                    prior_sources = list(prior.get("overridden_component_paths", []))
                    prior_source = str(prior.get("source_component_path", "")).strip()
                    if prior_source and prior_source not in prior_sources:
                        prior_sources.append(prior_source)
                selected_by_id[item_id] = {
                    **entry,
                    "source_component_id": str(
                        component.get("component_id", "")
                    ).strip(),
                    "source_component_path": str(component.get("path", "")).strip(),
                    "source_scope_kind": str(component.get("scope_kind", "")).strip(),
                    "source_artifact_kind": str(
                        component.get("artifact_kind", "")
                    ).strip(),
                    "source_precedence_index": int(
                        component.get("precedence_index", 0) or 0
                    ),
                    "overridden_component_paths": prior_sources,
                }
        output[field_name] = [
            selected_by_id[item_id]
            for item_id in ordered_ids
            if item_id in selected_by_id
        ]
    return output


def _selected_effective_ids(payload: dict[str, list[dict[str, Any]]]) -> list[str]:
    ids: list[str] = []
    for entries in payload.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id", "")).strip()
            if item_id:
                ids.append(item_id)
    return _unique_strings(ids)


def _build_compact_render(
    *,
    scope_kind: str,
    scope_root: Path,
    components: list[dict[str, Any]],
    effective_discuss: dict[str, list[dict[str, Any]]],
    effective_research: dict[str, list[dict[str, Any]]],
    diagnostics: list[str],
) -> str:
    lines = [f"scope_kind={scope_kind}", f"scope_root={scope_root}"]
    for component in components:
        component_id = str(component.get("component_id", "")).strip() or "unknown"
        artifact_kind = str(component.get("artifact_kind", "")).strip() or "unknown"
        path = str(component.get("path", "")).strip() or "(none)"
        status = str(component.get("status", "")).strip() or "unknown"
        selection_reason = str(component.get("selection_reason", "")).strip() or "n/a"
        selected = "yes" if bool(component.get("selected")) else "no"
        lines.append(
            f"{component_id}: artifact_kind={artifact_kind} status={status} selected={selected} path={path} why={selection_reason}"
        )
    lines.append(
        "effective_discuss=" + ",".join(_selected_effective_ids(effective_discuss))
    )
    lines.append(
        "effective_research=" + ",".join(_selected_effective_ids(effective_research))
    )
    for diagnostic in diagnostics:
        diagnostic_text = str(diagnostic).strip()
        if diagnostic_text:
            lines.append(f"diagnostic: {diagnostic_text}")
    return "\n".join(lines).rstrip()


def _default_project_map_component(
    repo_root: Path,
    *,
    precedence_index: int,
    diagnostics: list[str],
) -> dict[str, Any]:
    bundle_path = repo_root / ".autolab" / "context" / "bundle.json"
    bundle_payload, bundle_error = _read_json_mapping(bundle_path, repo_root=repo_root)
    bundle_summary = ""
    project_map_path = repo_root / ".autolab" / "context" / "project_map.json"
    selection_reason = "selected default project_map path"
    if isinstance(bundle_payload, dict):
        candidate = _resolve_repo_path(
            repo_root, bundle_payload.get("project_map_path")
        )
        if candidate is not None:
            project_map_path = candidate
            bundle_summary = str(bundle_payload.get("project_map_summary", "")).strip()
            selection_reason = "selected project_map from context bundle"
        elif str(bundle_payload.get("project_map_path", "")).strip():
            diagnostics.append(
                "context bundle project_map_path escapes repository root; falling back to default path"
            )
    elif bundle_error not in {"missing", "path unavailable"}:
        diagnostics.append(
            f"context bundle unavailable for project_map selection: {bundle_error}"
        )

    payload, payload_error = _read_json_mapping(project_map_path, repo_root=repo_root)
    status = (
        "loaded"
        if isinstance(payload, dict)
        else ("missing" if payload_error == "missing" else "invalid")
    )
    selected = status == "loaded"
    if not selected:
        bundle_summary = ""
        if status == "missing":
            selection_reason = "shared base project map is unavailable"
        else:
            selection_reason = f"shared base project map is invalid ({payload_error})"
    return {
        "component_id": _PROJECT_MAP_COMPONENT,
        "artifact_kind": "project_map",
        "scope_kind": "project_wide",
        "path": _repo_relative(repo_root, project_map_path),
        "status": status,
        "selected": selected,
        "selection_reason": selection_reason,
        "precedence_index": precedence_index,
        "fingerprint": _fingerprint_for_path(repo_root, project_map_path),
        "derived_from": [],
        "stale_if": [],
        "stale": False,
        "stale_reasons": [],
        "summary": bundle_summary,
        "_payload": payload,
    }


def _select_context_delta_path(
    repo_root: Path,
    *,
    iteration_id: str,
    experiment_id: str,
    iteration_dir: Path | None,
    diagnostics: list[str],
) -> tuple[Path | None, str, str]:
    default_path = (
        iteration_dir / "context_delta.json" if iteration_dir is not None else None
    )
    bundle_path = repo_root / ".autolab" / "context" / "bundle.json"
    bundle_payload, bundle_error = _read_json_mapping(bundle_path, repo_root=repo_root)
    if not isinstance(bundle_payload, dict):
        if bundle_error not in {"missing", "path unavailable"}:
            diagnostics.append(
                f"context bundle unavailable for context_delta selection: {bundle_error}"
            )
        return (default_path, "selected default iteration context_delta path", "")

    delta_maps = bundle_payload.get("experiment_delta_maps")
    if isinstance(delta_maps, list):
        exact_match: dict[str, Any] | None = None
        iteration_match: dict[str, Any] | None = None
        skipped_mismatched_experiment = False
        for entry in delta_maps:
            if not isinstance(entry, dict):
                continue
            entry_iteration = str(entry.get("iteration_id", "")).strip()
            entry_experiment = str(entry.get("experiment_id", "")).strip()
            if not iteration_id or entry_iteration != iteration_id:
                continue
            if experiment_id:
                if entry_experiment == experiment_id:
                    exact_match = entry
                    break
                if not entry_experiment and iteration_match is None:
                    iteration_match = entry
                    continue
                skipped_mismatched_experiment = True
                continue
            if iteration_match is None:
                iteration_match = entry
        selected_entry = exact_match or iteration_match
        if isinstance(selected_entry, dict):
            resolved = _resolve_repo_path(repo_root, selected_entry.get("path"))
            if resolved is not None:
                return (
                    resolved,
                    "selected context_delta from matching context bundle entry",
                    str(selected_entry.get("summary", "")).strip(),
                )
            diagnostics.append(
                "context bundle experiment_delta_maps path escapes repository root; falling back to default path"
            )
        elif skipped_mismatched_experiment:
            diagnostics.append(
                "context bundle experiment_delta_maps did not contain a matching experiment entry; falling back to default context_delta path"
            )

    selected_path = _resolve_repo_path(
        repo_root, bundle_payload.get("selected_experiment_delta_path")
    )
    focus_iteration_id = str(bundle_payload.get("focus_iteration_id", "")).strip()
    focus_experiment_id = str(bundle_payload.get("focus_experiment_id", "")).strip()
    if (
        selected_path is not None
        and focus_iteration_id == iteration_id
        and (
            not experiment_id
            or not focus_experiment_id
            or focus_experiment_id == experiment_id
        )
    ):
        return (
            selected_path,
            "selected context_delta from context bundle focus pointer",
            str(bundle_payload.get("selected_experiment_delta_summary", "")).strip(),
        )
    if selected_path is not None and (focus_iteration_id or focus_experiment_id):
        diagnostics.append(
            "context bundle focus pointer does not match requested experiment; falling back to default context_delta path"
        )
    return (default_path, "selected default iteration context_delta path", "")


def _default_context_delta_component(
    repo_root: Path,
    *,
    iteration_id: str,
    experiment_id: str,
    iteration_dir: Path | None,
    precedence_index: int,
    diagnostics: list[str],
) -> dict[str, Any]:
    if iteration_dir is None or not iteration_id:
        return {
            "component_id": _CONTEXT_DELTA_COMPONENT,
            "artifact_kind": "context_delta",
            "scope_kind": "experiment",
            "path": "",
            "status": "not_applicable",
            "selected": False,
            "selection_reason": "experiment overlay requires an active iteration_id",
            "precedence_index": precedence_index,
            "fingerprint": "<missing>",
            "derived_from": [],
            "stale_if": [],
            "stale": False,
            "stale_reasons": [],
            "summary": "",
            "_payload": None,
        }

    delta_path, selection_reason, delta_summary = _select_context_delta_path(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        iteration_dir=iteration_dir,
        diagnostics=diagnostics,
    )
    payload, payload_error = _read_json_mapping(delta_path, repo_root=repo_root)
    status = (
        "loaded"
        if isinstance(payload, dict)
        else ("missing" if payload_error == "missing" else "invalid")
    )
    selected = status == "loaded"
    stale = False
    stale_reasons: list[str] = []
    if isinstance(payload, dict):
        payload_iteration_id = str(payload.get("iteration_id", "")).strip()
        if payload_iteration_id and payload_iteration_id != iteration_id:
            stale = True
            selected = False
            status = "stale"
            stale_reasons.append(
                f"context_delta iteration_id differs from requested iteration ({payload_iteration_id} != {iteration_id})"
            )
        payload_experiment_id = str(payload.get("experiment_id", "")).strip()
        if (
            experiment_id
            and payload_experiment_id
            and payload_experiment_id != experiment_id
        ):
            stale = True
            selected = False
            status = "stale"
            stale_reasons.append(
                f"context_delta experiment_id differs from requested experiment ({payload_experiment_id} != {experiment_id})"
            )
    if stale_reasons:
        diagnostics.extend(stale_reasons)
        selection_reason = (
            "experiment-local brownfield delta was ignored because it is stale"
        )
    elif not selected:
        delta_summary = ""
        if status == "missing":
            selection_reason = "experiment-local brownfield delta is unavailable"
        else:
            selection_reason = (
                f"experiment-local brownfield delta is invalid ({payload_error})"
            )
    return {
        "component_id": _CONTEXT_DELTA_COMPONENT,
        "artifact_kind": "context_delta",
        "scope_kind": "experiment",
        "path": _repo_relative(repo_root, delta_path),
        "status": status,
        "selected": selected,
        "selection_reason": selection_reason,
        "precedence_index": precedence_index,
        "fingerprint": _fingerprint_for_path(repo_root, delta_path),
        "derived_from": [],
        "stale_if": [],
        "stale": stale,
        "stale_reasons": stale_reasons,
        "summary": delta_summary,
        "_payload": payload,
    }


def _sidecar_component(
    repo_root: Path,
    *,
    component_id: str,
    sidecar_kind: str,
    sidecar_scope_kind: str,
    expected_scope_root: Path | None,
    path: Path | None,
    precedence_index: int,
    iteration_id: str,
    experiment_id: str,
    selection_reason: str,
    missing_reason: str,
    invalid_reason: str,
) -> dict[str, Any]:
    if path is None:
        return {
            "component_id": component_id,
            "artifact_kind": sidecar_kind,
            "scope_kind": sidecar_scope_kind,
            "path": "",
            "status": "not_applicable",
            "selected": False,
            "selection_reason": "path is unavailable for this scope",
            "precedence_index": precedence_index,
            "fingerprint": "<missing>",
            "derived_from": [],
            "stale_if": [],
            "stale": False,
            "stale_reasons": [],
            "_payload": None,
        }

    payload, payload_error = _read_json_mapping(path, repo_root=repo_root)
    derived_from_refs: list[dict[str, str]] = []
    stale_if_refs: list[dict[str, str]] = []
    stale_reasons: list[str] = []
    status = (
        "loaded"
        if isinstance(payload, dict)
        else ("missing" if payload_error == "missing" else "invalid")
    )
    selected = status == "loaded"
    if isinstance(payload, dict):
        derived_from_refs, stale_if_refs, validation_errors = _validate_sidecar_payload(
            payload,
            repo_root=repo_root,
            expected_kind=sidecar_kind,
            expected_scope_kind=sidecar_scope_kind,
            expected_scope_root=expected_scope_root,
            iteration_id=iteration_id,
            experiment_id=experiment_id,
        )
        if validation_errors:
            status = "invalid"
            selected = False
            stale_reasons = validation_errors
        else:
            dependency_reasons = _evaluate_dependency_staleness(
                repo_root,
                refs=[*derived_from_refs, *stale_if_refs],
            )
            if dependency_reasons:
                status = "stale"
                selected = False
                stale_reasons = dependency_reasons

    if status == "missing":
        selection_reason = missing_reason
    elif status == "invalid":
        if payload_error:
            selection_reason = f"{invalid_reason} ({payload_error})"
        else:
            selection_reason = invalid_reason
    elif status == "stale":
        selection_reason = f"{sidecar_scope_kind} {sidecar_kind} sidecar was ignored because dependency fingerprints changed"

    return {
        "component_id": component_id,
        "artifact_kind": sidecar_kind,
        "scope_kind": sidecar_scope_kind,
        "path": _repo_relative(repo_root, path),
        "status": status,
        "selected": selected,
        "selection_reason": selection_reason,
        "precedence_index": precedence_index,
        "fingerprint": _fingerprint_for_path(repo_root, path),
        "derived_from": derived_from_refs,
        "stale_if": stale_if_refs,
        "stale": status == "stale",
        "stale_reasons": stale_reasons,
        "_payload": payload,
    }


def _compatibility_fields(*, components: list[dict[str, Any]]) -> dict[str, str]:
    def _selected_component_value(component: dict[str, Any], key: str) -> str:
        if not bool(component.get("selected")):
            return ""
        return str(component.get(key, "")).strip()

    project_map_component = next(
        (
            row
            for row in components
            if str(row.get("component_id", "")) == _PROJECT_MAP_COMPONENT
        ),
        {},
    )
    context_delta_component = next(
        (
            row
            for row in components
            if str(row.get("component_id", "")) == _CONTEXT_DELTA_COMPONENT
        ),
        {},
    )
    return {
        "codebase_project_map_path": _selected_component_value(
            project_map_component, "path"
        ),
        "codebase_project_map_summary": _selected_component_value(
            project_map_component, "summary"
        ),
        "codebase_experiment_delta_map_path": _selected_component_value(
            context_delta_component, "path"
        ),
        "codebase_experiment_delta_summary": _selected_component_value(
            context_delta_component, "summary"
        ),
    }


def resolve_context_sidecars(
    repo_root: Path,
    *,
    iteration_id: str,
    experiment_id: str,
    scope_kind: str = "",
) -> dict[str, Any]:
    effective_scope_kind, scope_root, iteration_dir = _resolve_scope_context(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        scope_kind=scope_kind,
    )
    _project_scope_kind, project_wide_root, _project_iteration_dir = (
        _resolve_scope_context(
            repo_root,
            iteration_id=iteration_id,
            experiment_id=experiment_id,
            scope_kind="project_wide",
        )
    )
    component_order = list(_COMPONENT_ORDER_BY_SCOPE.get(effective_scope_kind, []))
    diagnostics: list[str] = []
    components: list[dict[str, Any]] = []

    for index, component_id in enumerate(component_order):
        if component_id == _PROJECT_MAP_COMPONENT:
            components.append(
                _default_project_map_component(
                    repo_root,
                    precedence_index=index,
                    diagnostics=diagnostics,
                )
            )
            continue
        if component_id == _CONTEXT_DELTA_COMPONENT:
            components.append(
                _default_context_delta_component(
                    repo_root,
                    iteration_id=iteration_id,
                    experiment_id=experiment_id,
                    iteration_dir=iteration_dir,
                    precedence_index=index,
                    diagnostics=diagnostics,
                )
            )
            continue
        if component_id == _PROJECT_DISCUSS_COMPONENT:
            components.append(
                _sidecar_component(
                    repo_root,
                    component_id=component_id,
                    sidecar_kind="discuss",
                    sidecar_scope_kind="project_wide",
                    expected_scope_root=project_wide_root,
                    path=repo_root
                    / ".autolab"
                    / "context"
                    / "sidecars"
                    / "project_wide"
                    / "discuss.json",
                    precedence_index=index,
                    iteration_id=iteration_id,
                    experiment_id=experiment_id,
                    selection_reason="shared discuss base for the active scope",
                    missing_reason="shared discuss base is missing",
                    invalid_reason="shared discuss base is invalid",
                )
            )
            continue
        if component_id == _PROJECT_RESEARCH_COMPONENT:
            components.append(
                _sidecar_component(
                    repo_root,
                    component_id=component_id,
                    sidecar_kind="research",
                    sidecar_scope_kind="project_wide",
                    expected_scope_root=project_wide_root,
                    path=repo_root
                    / ".autolab"
                    / "context"
                    / "sidecars"
                    / "project_wide"
                    / "research.json",
                    precedence_index=index,
                    iteration_id=iteration_id,
                    experiment_id=experiment_id,
                    selection_reason="shared research base for the active scope",
                    missing_reason="shared research base is missing",
                    invalid_reason="shared research base is invalid",
                )
            )
            continue
        if component_id == _EXPERIMENT_DISCUSS_COMPONENT:
            experiment_path = (
                iteration_dir / "context" / "sidecars" / "discuss.json"
                if iteration_dir is not None and iteration_id
                else None
            )
            components.append(
                _sidecar_component(
                    repo_root,
                    component_id=component_id,
                    sidecar_kind="discuss",
                    sidecar_scope_kind="experiment",
                    expected_scope_root=iteration_dir,
                    path=experiment_path,
                    precedence_index=index,
                    iteration_id=iteration_id,
                    experiment_id=experiment_id,
                    selection_reason="experiment-local discuss overlay",
                    missing_reason="experiment-local discuss overlay is missing",
                    invalid_reason="experiment-local discuss overlay is invalid",
                )
            )
            continue
        if component_id == _EXPERIMENT_RESEARCH_COMPONENT:
            experiment_path = (
                iteration_dir / "context" / "sidecars" / "research.json"
                if iteration_dir is not None and iteration_id
                else None
            )
            components.append(
                _sidecar_component(
                    repo_root,
                    component_id=component_id,
                    sidecar_kind="research",
                    sidecar_scope_kind="experiment",
                    expected_scope_root=iteration_dir,
                    path=experiment_path,
                    precedence_index=index,
                    iteration_id=iteration_id,
                    experiment_id=experiment_id,
                    selection_reason="experiment-local research overlay",
                    missing_reason="experiment-local research overlay is missing",
                    invalid_reason="experiment-local research overlay is invalid",
                )
            )

    for component in components:
        stale_reasons = component.get("stale_reasons")
        if not isinstance(stale_reasons, list):
            continue
        for reason in stale_reasons:
            reason_text = str(reason).strip()
            if reason_text:
                diagnostics.append(reason_text)
    diagnostics = _unique_strings(diagnostics)

    effective_discuss = _merge_effective_items(kind="discuss", components=components)
    effective_research = _merge_effective_items(kind="research", components=components)
    compatibility = _compatibility_fields(components=components)
    compact_render = _build_compact_render(
        scope_kind=effective_scope_kind,
        scope_root=scope_root,
        components=components,
        effective_discuss=effective_discuss,
        effective_research=effective_research,
        diagnostics=diagnostics,
    )

    public_components = [
        {key: value for key, value in component.items() if not key.startswith("_")}
        for component in components
    ]
    return {
        "scope_kind": effective_scope_kind,
        "scope_root": str(scope_root),
        "component_order": component_order,
        "components": public_components,
        "effective_discuss": effective_discuss,
        "effective_research": effective_research,
        "compact_render": compact_render,
        "diagnostics": diagnostics,
        **compatibility,
    }


__all__ = ["resolve_context_sidecars"]
