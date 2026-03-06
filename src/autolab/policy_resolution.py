"""Effective policy resolution engine — pure logic, zero autolab imports.

Merges scaffold defaults → preset → host → scope → stage → risk → repo-local
overlays and tracks provenance for each contributed key.
"""

from __future__ import annotations

import fnmatch
from typing import Any


def _deep_merge_with_provenance(
    base: dict[str, Any],
    overlay: dict[str, Any],
    layer_name: str,
) -> tuple[dict[str, Any], list[str]]:
    """Deep-merge *overlay* into *base*, recording which top-level keys changed.

    Merge semantics (same as ``_deep_merge_dict`` in support.py):
    - dicts → recursive deep-merge
    - everything else (scalars, lists, bools) → last-wins
    """
    merged: dict[str, Any] = dict(base)
    keys_contributed: list[str] = []
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            inner_merged, _ = _deep_merge_with_provenance(
                dict(merged[key]),
                value,
                layer_name,  # type: ignore[arg-type]
            )
            if inner_merged != merged[key]:
                keys_contributed.append(key)
            merged[key] = inner_merged
        else:
            if merged.get(key) != value:
                keys_contributed.append(key)
            merged[key] = value
    return merged, keys_contributed


def resolve_effective_policy(
    scaffold_defaults: dict[str, Any],
    preset_policy: dict[str, Any],
    host_overlay: dict[str, Any],
    scope_overlay: dict[str, Any],
    stage_overlay: dict[str, Any],
    risk_overlay: dict[str, Any],
    repo_local_overrides: dict[str, Any],
) -> tuple[dict[str, Any], list[tuple[str, str, list[str]]]]:
    """Merge policy layers in precedence order.

    Returns ``(merged_dict, sources)`` where *sources* is a list of
    ``(layer_name, dimension_key, keys_contributed)`` tuples.
    """
    layers: list[tuple[str, str, dict[str, Any]]] = [
        ("scaffold_default", "", scaffold_defaults),
        ("preset", "", preset_policy),
        ("host", "", host_overlay),
        ("scope", "", scope_overlay),
        ("stage", "", stage_overlay),
        ("risk", "", risk_overlay),
        ("repo_local", "", repo_local_overrides),
    ]

    merged: dict[str, Any] = {}
    sources: list[tuple[str, str, list[str]]] = []

    for layer_name, dimension_key, overlay in layers:
        if not overlay:
            continue
        merged, keys = _deep_merge_with_provenance(merged, overlay, layer_name)
        if keys:
            sources.append((layer_name, dimension_key, keys))

    return merged, sources


def extract_overlay(
    policy_dict: dict[str, Any], dimension: str, key: str
) -> dict[str, Any]:
    """Read ``policy_overlays.<dimension>.<key>`` from *policy_dict*."""
    overlays = policy_dict.get("policy_overlays")
    if not isinstance(overlays, dict):
        return {}
    dim_section = overlays.get(dimension)
    if not isinstance(dim_section, dict):
        return {}
    entry = dim_section.get(key)
    if not isinstance(entry, dict):
        return {}
    return dict(entry)


def derive_risk_flags(
    host_mode: str,
    scope_kind: str,
    profile_mode: str,
    project_wide_unique_paths: list[str],
    uat_surface_patterns: list[str],
    plan_approval_required: bool,
) -> dict[str, bool]:
    """Derive runtime risk flags from context dimensions."""
    uat_required = scope_kind == "project_wide" and _any_path_matches(
        project_wide_unique_paths, uat_surface_patterns
    )
    remote_profile_required = host_mode == "slurm" and profile_mode != "shared_fs"
    return {
        "plan_approval_required": plan_approval_required,
        "uat_required": uat_required,
        "remote_profile_required": remote_profile_required,
    }


def _any_path_matches(paths: list[str], patterns: list[str]) -> bool:
    """Return True if any path matches any glob pattern."""
    for path in paths:
        for pattern in patterns:
            if fnmatch.fnmatch(path, pattern):
                return True
    return False


def build_effective_artifact(
    merged: dict[str, Any],
    sources: list[tuple[str, str, list[str]]],
    preset: str,
    host_mode: str,
    scope_kind: str,
    stage: str,
    risk_flags: dict[str, bool],
    *,
    generated_at: str = "",
) -> dict[str, Any]:
    """Build the ``effective_policy.json`` artifact structure."""
    source_entries = [
        {
            "layer": layer,
            "name": name,
            "keys_contributed": keys,
        }
        for layer, name, keys in sources
    ]
    return {
        "schema_version": "1.0",
        "generated_at": generated_at,
        "preset": preset,
        "host_mode": host_mode,
        "scope_kind": scope_kind,
        "stage": stage,
        "risk_flags": risk_flags,
        "sources": source_entries,
        "merged": merged,
    }
