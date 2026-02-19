"""Shared cross-artifact invariant helpers for scaffold verifiers."""

from __future__ import annotations

from typing import Any

from verifier_lib import normalize_sync_status


def check_design_manifest_host_mode(
    design_payload: dict[str, Any] | None,
    manifest_payload: dict[str, Any] | None,
) -> list[str]:
    """Validate design.compute.location and manifest host mode alignment."""
    if not isinstance(design_payload, dict) or not isinstance(manifest_payload, dict):
        return []

    design_location = ""
    design_compute = design_payload.get("compute")
    if isinstance(design_compute, dict):
        design_location = str(design_compute.get("location", "")).strip().lower()

    manifest_host_mode = (
        str(
            manifest_payload.get("host_mode")
            or manifest_payload.get("launch_mode")
            or (manifest_payload.get("launch") or {}).get("mode")
        )
        .strip()
        .lower()
    )
    if design_location and manifest_host_mode and design_location != manifest_host_mode:
        return [
            f"design.compute.location='{design_location}' does not match run_manifest host/launch mode '{manifest_host_mode}'"
        ]
    return []


def check_metric_name_match(
    design_payload: dict[str, Any] | None,
    metrics_payload: dict[str, Any] | None,
) -> list[str]:
    """Validate metrics.primary_metric.name matches design.metrics.primary.name."""
    if not isinstance(design_payload, dict) or not isinstance(metrics_payload, dict):
        return []

    design_metric_name = ""
    design_metrics = design_payload.get("metrics")
    if isinstance(design_metrics, dict):
        primary = design_metrics.get("primary")
        if isinstance(primary, dict):
            design_metric_name = str(primary.get("name", "")).strip()

    metrics_metric_name = ""
    primary_metric = metrics_payload.get("primary_metric")
    if isinstance(primary_metric, dict):
        metrics_metric_name = str(primary_metric.get("name", "")).strip()

    if (
        design_metric_name
        and metrics_metric_name
        and design_metric_name != metrics_metric_name
    ):
        return [
            f"metrics.primary_metric.name='{metrics_metric_name}' does not match design.metrics.primary.name='{design_metric_name}'"
        ]
    return []


def check_state_run_scoped_fields(
    *,
    state: dict[str, Any],
    manifest_payload: dict[str, Any] | None,
    metrics_payload: dict[str, Any] | None,
) -> list[str]:
    """Validate iteration_id/run_id consistency between state and run artifacts."""
    failures: list[str] = []
    state_iteration_id = str(state.get("iteration_id", "")).strip()
    state_run_id = str(state.get("last_run_id", "")).strip()

    if isinstance(manifest_payload, dict):
        manifest_iteration_id = str(manifest_payload.get("iteration_id", "")).strip()
        if (
            state_iteration_id
            and manifest_iteration_id
            and state_iteration_id != manifest_iteration_id
        ):
            failures.append(
                f"run_manifest iteration_id '{manifest_iteration_id}' does not match state.iteration_id '{state_iteration_id}'"
            )
        manifest_run_id = str(manifest_payload.get("run_id", "")).strip()
        if state_run_id and manifest_run_id and state_run_id != manifest_run_id:
            failures.append(
                f"run_manifest run_id '{manifest_run_id}' does not match state.last_run_id '{state_run_id}'"
            )

    if isinstance(metrics_payload, dict):
        metrics_iteration_id = str(metrics_payload.get("iteration_id", "")).strip()
        if (
            state_iteration_id
            and metrics_iteration_id
            and state_iteration_id != metrics_iteration_id
        ):
            failures.append(
                f"metrics iteration_id '{metrics_iteration_id}' does not match state.iteration_id '{state_iteration_id}'"
            )
        metrics_run_id = str(metrics_payload.get("run_id", "")).strip()
        if state_run_id and metrics_run_id and state_run_id != metrics_run_id:
            failures.append(
                f"metrics run_id '{metrics_run_id}' does not match state.last_run_id '{state_run_id}'"
            )
    return failures


def check_manifest_sync_status(
    manifest_payload: dict[str, Any] | None,
    *,
    require_success: bool,
    context: str,
) -> list[str]:
    """Validate artifact_sync_to_local.status against canonical vocabulary."""
    if not isinstance(manifest_payload, dict):
        return []

    sync = manifest_payload.get("artifact_sync_to_local")
    if not isinstance(sync, dict):
        return ["run_manifest artifact_sync_to_local must be a mapping"]

    raw_status = str(sync.get("status", "")).strip().lower()
    if not raw_status:
        return ["run_manifest artifact_sync_to_local.status is required"]
    normalized = normalize_sync_status(raw_status)
    if not normalized:
        return [
            (
                "run_manifest artifact_sync_to_local.status has unsupported value "
                f"'{raw_status}' (expected canonical pending|syncing|ok|failed or known synonym)"
            )
        ]
    if require_success and normalized != "ok":
        return [
            (
                "run_manifest artifact_sync_to_local.status must resolve to canonical "
                f"'ok' for {context}; got '{raw_status}'"
            )
        ]
    return []
