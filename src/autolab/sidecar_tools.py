from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from autolab.sidecar_context import resolve_context_sidecars
from autolab.scope import _resolve_scope_context
from autolab.utils import _path_fingerprint

DISCUSS_COLLECTIONS = (
    "locked_decisions",
    "preferences",
    "constraints",
    "open_questions",
    "promotion_candidates",
)
RESEARCH_COLLECTIONS = ("questions", "findings", "recommendations", "sources")
SIDECAR_COLLECTIONS_BY_KIND = {
    "discuss": DISCUSS_COLLECTIONS,
    "research": RESEARCH_COLLECTIONS,
}
SIDE_CAR_SCOPE_KINDS = {"project_wide", "experiment"}
_ARTIFACT_CONTEXT_REFS = {"project_map", "context_delta"}


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


def _safe_load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists() or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _truncate(text: Any, *, max_chars: int = 140) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


def _dedupe_text(items: list[str]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for item in items:
        normalized = str(item).strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(normalized)
    return output


def _effective_resolution_scope_kind(
    *, iteration_id: str, scope_kind: str | None = None
) -> str:
    normalized = str(scope_kind or "").strip().lower()
    if normalized in SIDE_CAR_SCOPE_KINDS:
        return normalized
    return "experiment" if str(iteration_id).strip() else "project_wide"


def _resolve_context_resolution(
    repo_root: Path,
    *,
    iteration_id: str,
    experiment_id: str,
    scope_kind: str | None,
    context_resolution: dict[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(context_resolution, dict):
        return context_resolution
    effective_scope_kind = _effective_resolution_scope_kind(
        iteration_id=iteration_id,
        scope_kind=scope_kind,
    )
    return resolve_context_sidecars(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        scope_kind=effective_scope_kind,
    )


def _selected_component_for_artifact(
    context_resolution: dict[str, Any],
    *,
    artifact_kind: str,
) -> dict[str, Any] | None:
    components = context_resolution.get("components")
    if not isinstance(components, list):
        return None
    for component in components:
        if not isinstance(component, dict):
            continue
        if str(component.get("artifact_kind", "")).strip() != artifact_kind:
            continue
        if not bool(component.get("selected")):
            continue
        if str(component.get("status", "")).strip() != "loaded":
            continue
        return component
    return None


def _effective_entries_for_sidecar_ref(
    context_resolution: dict[str, Any],
    *,
    sidecar_kind: str,
    collection_name: str,
) -> list[dict[str, Any]]:
    payload_key = (
        "effective_discuss" if sidecar_kind == "discuss" else "effective_research"
    )
    payload = context_resolution.get(payload_key)
    if not isinstance(payload, dict):
        return []
    entries = payload.get(collection_name)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def resolve_sidecar_output_paths(
    repo_root: Path,
    *,
    scope_kind: str,
    sidecar_kind: str,
    iteration_id: str,
    experiment_id: str,
) -> dict[str, Any]:
    normalized_scope_kind = str(scope_kind).strip().lower()
    normalized_sidecar_kind = str(sidecar_kind).strip().lower()
    if normalized_scope_kind not in SIDE_CAR_SCOPE_KINDS:
        raise ValueError(f"unsupported sidecar scope_kind '{scope_kind}'")
    if normalized_sidecar_kind not in SIDECAR_COLLECTIONS_BY_KIND:
        raise ValueError(f"unsupported sidecar kind '{sidecar_kind}'")

    effective_scope_kind, scope_root, iteration_dir = _resolve_scope_context(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        scope_kind=normalized_scope_kind,
    )
    if effective_scope_kind != normalized_scope_kind:
        scope_root = (
            iteration_dir if normalized_scope_kind == "experiment" else scope_root
        )
    if normalized_scope_kind == "project_wide":
        json_path = (
            repo_root
            / ".autolab"
            / "context"
            / "sidecars"
            / "project_wide"
            / f"{normalized_sidecar_kind}.json"
        )
    else:
        if iteration_dir is None or not str(iteration_id).strip():
            raise ValueError("experiment scope sidecars require a resolvable iteration")
        json_path = (
            iteration_dir / "context" / "sidecars" / f"{normalized_sidecar_kind}.json"
        )
        scope_root = iteration_dir
    md_path = json_path.with_suffix(".md")
    return {
        "scope_kind": normalized_scope_kind,
        "scope_root": scope_root,
        "iteration_dir": iteration_dir,
        "json_path": json_path,
        "markdown_path": md_path,
    }


def build_sidecar_dependency_refs(
    repo_root: Path,
    context_resolution: dict[str, Any],
    *,
    exclude_paths: set[str] | None = None,
) -> list[dict[str, str]]:
    excluded = {
        str(item).strip() for item in (exclude_paths or set()) if str(item).strip()
    }
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    components = context_resolution.get("components")
    if not isinstance(components, list):
        return output
    for component in components:
        if not isinstance(component, dict):
            continue
        if not bool(component.get("selected")):
            continue
        path_text = str(component.get("path", "")).strip()
        if not path_text or path_text in excluded:
            continue
        reason = (
            str(component.get("component_id", "")).strip()
            or str(component.get("artifact_kind", "")).strip()
        )
        key = (path_text, reason)
        if key in seen:
            continue
        seen.add(key)
        output.append(
            {
                "path": path_text,
                "fingerprint": _path_fingerprint(repo_root, path_text),
                "reason": reason,
            }
        )
    return output


def build_sidecar_markdown(payload: dict[str, Any]) -> str:
    sidecar_kind = str(payload.get("sidecar_kind", "")).strip() or "sidecar"
    scope_kind = str(payload.get("scope_kind", "")).strip() or "unknown"
    scope_root = str(payload.get("scope_root", "")).strip() or "unknown"
    generated_at = str(payload.get("generated_at", "")).strip() or "unknown"
    lines = [
        f"# {sidecar_kind.title()} Sidecar",
        "",
        f"- generated_at: `{generated_at}`",
        f"- scope_kind: `{scope_kind}`",
        f"- scope_root: `{scope_root}`",
    ]
    if str(payload.get("iteration_id", "")).strip():
        lines.append(f"- iteration_id: `{payload['iteration_id']}`")
    if str(payload.get("experiment_id", "")).strip():
        lines.append(f"- experiment_id: `{payload['experiment_id']}`")
    derived_from = payload.get("derived_from")
    if isinstance(derived_from, list) and derived_from:
        lines.append(
            f"- derived_from: `{', '.join(str(row.get('path', '')).strip() for row in derived_from if isinstance(row, dict) and str(row.get('path', '')).strip())}`"
        )
    stale_if = payload.get("stale_if")
    if isinstance(stale_if, list) and stale_if:
        lines.append(
            f"- stale_if: `{', '.join(str(row.get('path', '')).strip() for row in stale_if if isinstance(row, dict) and str(row.get('path', '')).strip())}`"
        )
    lines.append("")

    for collection_name in SIDECAR_COLLECTIONS_BY_KIND.get(sidecar_kind, ()):
        label = collection_name.replace("_", " ").title()
        lines.append(f"## {label}")
        entries = payload.get(collection_name)
        if not isinstance(entries, list) or not entries:
            lines.extend(["- (none)", ""])
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id", "")).strip() or "item"
            summary = _truncate(entry.get("summary", ""), max_chars=220) or "(empty)"
            detail = _truncate(entry.get("detail", ""), max_chars=320)
            metadata_parts: list[str] = []
            for key in (
                "status",
                "target_scope_kind",
                "requirement_hint",
                "kind",
                "path",
            ):
                value = str(entry.get(key, "")).strip()
                if value:
                    metadata_parts.append(f"{key}={value}")
            lines.append(f"- `{item_id}`: {summary}")
            if detail and detail != summary:
                lines.append(f"  - detail: {detail}")
            if metadata_parts:
                lines.append(f"  - metadata: {', '.join(metadata_parts)}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def parse_context_ref(raw_ref: Any) -> dict[str, str] | None:
    ref = str(raw_ref or "").strip()
    if not ref:
        return None
    if ref in _ARTIFACT_CONTEXT_REFS:
        return {"kind": "artifact", "artifact_kind": ref, "ref": ref}
    parts = ref.split(":")
    if len(parts) == 4:
        scope_kind, sidecar_kind, collection_name, item_id = parts
        if (
            scope_kind in SIDE_CAR_SCOPE_KINDS
            and sidecar_kind in SIDECAR_COLLECTIONS_BY_KIND
            and collection_name in SIDECAR_COLLECTIONS_BY_KIND[sidecar_kind]
            and item_id
        ):
            return {
                "kind": "sidecar",
                "scope_kind": scope_kind,
                "sidecar_kind": sidecar_kind,
                "collection": collection_name,
                "item_id": item_id,
                "ref": ref,
            }
    if len(parts) == 3 and parts[0] == "promoted" and parts[1] and parts[2]:
        return {
            "kind": "promoted",
            "requirement_id": parts[1],
            "item_id": parts[2],
            "ref": ref,
        }
    return None


def _iter_promoted_constraints(
    design_payload: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    output: list[tuple[str, dict[str, Any]]] = []
    raw_requirements = design_payload.get("implementation_requirements")
    if not isinstance(raw_requirements, list):
        return output
    for requirement in raw_requirements:
        if not isinstance(requirement, dict):
            continue
        requirement_id = str(requirement.get("requirement_id", "")).strip()
        promoted_constraints = requirement.get("promoted_constraints")
        if not requirement_id or not isinstance(promoted_constraints, list):
            continue
        for entry in promoted_constraints:
            if not isinstance(entry, dict):
                continue
            output.append((requirement_id, entry))
    return output


def resolve_context_ref(
    repo_root: Path,
    *,
    iteration_id: str,
    experiment_id: str,
    raw_ref: Any,
    design_payload: dict[str, Any] | None = None,
    scope_kind: str | None = None,
    context_resolution: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    parsed = parse_context_ref(raw_ref)
    if parsed is None:
        return None
    effective_context_resolution = _resolve_context_resolution(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        scope_kind=scope_kind,
        context_resolution=context_resolution,
    )
    kind = parsed["kind"]
    if kind == "artifact":
        artifact_kind = parsed["artifact_kind"]
        component = _selected_component_for_artifact(
            effective_context_resolution,
            artifact_kind=artifact_kind,
        )
        if not isinstance(component, dict):
            return None
        payload = component.get("_payload")
        if not isinstance(payload, dict):
            return None
        path = str(component.get("path", "")).strip()
        return {
            "ref": parsed["ref"],
            "kind": "artifact",
            "artifact_kind": artifact_kind,
            "path": path,
            "summary": _truncate(
                str(component.get("summary", "")).strip() or path,
                max_chars=200,
            ),
            "detail": json.dumps(payload, indent=2)[:1000]
            if isinstance(payload, dict)
            else "",
        }

    if kind == "sidecar":
        entries = _effective_entries_for_sidecar_ref(
            effective_context_resolution,
            sidecar_kind=parsed["sidecar_kind"],
            collection_name=parsed["collection"],
        )
        for entry in entries:
            if str(entry.get("id", "")).strip() != parsed["item_id"]:
                continue
            if str(entry.get("source_scope_kind", "")).strip() != parsed["scope_kind"]:
                continue
            if (
                str(entry.get("source_artifact_kind", "")).strip()
                != parsed["sidecar_kind"]
            ):
                continue
            return {
                "ref": parsed["ref"],
                "kind": "sidecar",
                "scope_kind": parsed["scope_kind"],
                "sidecar_kind": parsed["sidecar_kind"],
                "collection": parsed["collection"],
                "item_id": parsed["item_id"],
                "path": str(entry.get("source_component_path", "")).strip(),
                "summary": str(entry.get("summary", "")).strip(),
                "detail": str(entry.get("detail", "")).strip(),
                "entry": entry,
            }
        return None

    effective_design = design_payload if isinstance(design_payload, dict) else {}
    for requirement_id, entry in _iter_promoted_constraints(effective_design):
        if (
            requirement_id == parsed["requirement_id"]
            and str(entry.get("id", "")).strip() == parsed["item_id"]
        ):
            return {
                "ref": parsed["ref"],
                "kind": "promoted",
                "requirement_id": requirement_id,
                "item_id": parsed["item_id"],
                "summary": str(entry.get("summary", "")).strip(),
                "detail": str(entry.get("rationale", "")).strip(),
                "entry": entry,
            }
    return None


def collect_context_ref_summaries(
    repo_root: Path,
    *,
    iteration_id: str,
    experiment_id: str,
    refs: list[str],
    design_payload: dict[str, Any] | None = None,
    max_items: int = 4,
) -> list[str]:
    lines: list[str] = []
    for ref in refs:
        resolved = resolve_context_ref(
            repo_root,
            iteration_id=iteration_id,
            experiment_id=experiment_id,
            raw_ref=ref,
            design_payload=design_payload,
        )
        if not isinstance(resolved, dict):
            continue
        summary = _truncate(resolved.get("summary", ""), max_chars=140)
        if not summary:
            summary = _truncate(ref, max_chars=140)
        lines.append(f"{ref}: {summary}")
        if len(lines) >= max_items:
            break
    return _dedupe_text(lines)


def _collection_highlights(
    payload: dict[str, Any],
    collection_name: str,
    *,
    max_items: int,
) -> list[str]:
    entries = payload.get(collection_name)
    if not isinstance(entries, list):
        return []
    prioritized_entries: list[tuple[int, int, dict[str, Any]]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        try:
            precedence = int(entry.get("source_precedence_index", 0) or 0)
        except Exception:
            precedence = 0
        prioritized_entries.append((precedence, index, entry))
    prioritized_entries.sort(key=lambda item: (-item[0], item[1]))
    output: list[str] = []
    for _precedence, _index, entry in prioritized_entries:
        summary = _truncate(entry.get("summary", ""), max_chars=120)
        if summary:
            output.append(summary)
        if len(output) >= max_items:
            break
    return output


def _promoted_constraint_highlights(
    design_payload: dict[str, Any] | None,
    *,
    max_items: int,
) -> list[str]:
    payload = design_payload if isinstance(design_payload, dict) else {}
    output: list[str] = []
    for requirement_id, entry in _iter_promoted_constraints(payload):
        summary = _truncate(entry.get("summary", ""), max_chars=120)
        if summary:
            output.append(f"{requirement_id}: {summary}")
        if len(output) >= max_items:
            break
    return output


def build_context_guidance(
    context_resolution: dict[str, Any],
    *,
    stage: str,
    design_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    effective_discuss = context_resolution.get("effective_discuss")
    if not isinstance(effective_discuss, dict):
        effective_discuss = {name: [] for name in DISCUSS_COLLECTIONS}
    effective_research = context_resolution.get("effective_research")
    if not isinstance(effective_research, dict):
        effective_research = {name: [] for name in RESEARCH_COLLECTIONS}

    stage_name = str(stage).strip().lower()
    if stage_name == "implementation":
        discuss_focus = ("locked_decisions", "constraints", "promotion_candidates")
        research_focus = ("findings", "recommendations")
    else:
        discuss_focus = (
            "locked_decisions",
            "preferences",
            "constraints",
            "open_questions",
            "promotion_candidates",
        )
        research_focus = ("questions", "findings", "recommendations")

    stage_lines: list[str] = []
    brief_items: list[str] = []

    for collection_name in discuss_focus:
        highlights = _collection_highlights(
            effective_discuss,
            collection_name,
            max_items=2,
        )
        if not highlights:
            continue
        label = collection_name.replace("_", " ")
        stage_lines.append(f"- discuss_{collection_name}: {'; '.join(highlights)}")
        if collection_name in {"locked_decisions", "constraints", "open_questions"}:
            brief_items.append(f"{label}: {'; '.join(highlights)}")

    for collection_name in research_focus:
        highlights = _collection_highlights(
            effective_research,
            collection_name,
            max_items=2,
        )
        if not highlights:
            continue
        label = collection_name.replace("_", " ")
        stage_lines.append(f"- research_{collection_name}: {'; '.join(highlights)}")
        if collection_name in {"findings", "recommendations"}:
            brief_items.append(f"{label}: {'; '.join(highlights)}")

    promoted_highlights = _promoted_constraint_highlights(design_payload, max_items=2)
    if promoted_highlights:
        stage_lines.append(f"- promoted_constraints: {'; '.join(promoted_highlights)}")
        brief_items.append(f"promoted constraints: {'; '.join(promoted_highlights)}")

    return {
        "stage_context_lines": _dedupe_text(stage_lines),
        "brief_items": _dedupe_text(brief_items),
    }


def build_task_context_guidance(
    repo_root: Path,
    *,
    iteration_id: str,
    experiment_id: str,
    task_packet: dict[str, Any],
    design_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    context_inputs_raw = task_packet.get("context_inputs")
    context_inputs: list[str] = []
    if isinstance(context_inputs_raw, list):
        for item in context_inputs_raw:
            candidate = str(item).strip()
            if candidate:
                context_inputs.append(candidate)

    resolved_lines = collect_context_ref_summaries(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        refs=context_inputs,
        design_payload=design_payload,
        max_items=6,
    )
    return {
        "context_inputs": context_inputs,
        "resolved_inputs": resolved_lines,
        "compact_summary": "; ".join(resolved_lines[:4]),
    }


__all__ = [
    "DISCUSS_COLLECTIONS",
    "RESEARCH_COLLECTIONS",
    "SIDECAR_COLLECTIONS_BY_KIND",
    "build_context_guidance",
    "build_sidecar_dependency_refs",
    "build_sidecar_markdown",
    "build_task_context_guidance",
    "collect_context_ref_summaries",
    "parse_context_ref",
    "resolve_context_ref",
    "resolve_sidecar_output_paths",
]
