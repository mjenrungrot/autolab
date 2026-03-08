"""Campaign state helpers for autonomous research sessions."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from autolab.checkpoint import _resolve_revision_label
from autolab.state import _resolve_iteration_directory
from autolab.utils import _load_json_if_exists, _normalize_space, _utc_now, _write_json

CAMPAIGN_FILENAME = "campaign.json"
CAMPAIGN_STATUSES = {
    "running",
    "stop_requested",
    "stopped",
    "needs_rethink",
    "error",
}


class CampaignError(RuntimeError):
    """Raised when campaign state is missing or invalid."""


def _campaign_path(repo_root: Path) -> Path:
    return repo_root / ".autolab" / CAMPAIGN_FILENAME


def _generate_campaign_id() -> str:
    timestamp = (
        _utc_now()
        .replace("-", "")
        .replace(":", "")
        .replace(".", "")
        .replace("T", "_")
        .replace("Z", "Z")
    )
    return f"campaign_{timestamp}_{uuid.uuid4().hex[:8]}"


def _campaign_required_string(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key, "")).strip()
    if not value:
        raise CampaignError(f".autolab/campaign.json missing required field '{key}'")
    return value


def _campaign_non_negative_int(payload: dict[str, Any], key: str) -> int:
    try:
        value = int(payload.get(key, 0))
    except Exception as exc:
        raise CampaignError(
            f".autolab/campaign.json field '{key}' must be an integer"
        ) from exc
    if value < 0:
        raise CampaignError(f".autolab/campaign.json field '{key}' must be >= 0")
    return value


def _normalize_campaign(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise CampaignError(".autolab/campaign.json must contain a JSON object")

    scope_kind = _campaign_required_string(payload, "scope_kind").lower()
    if scope_kind not in {"experiment", "project_wide"}:
        raise CampaignError(
            ".autolab/campaign.json field 'scope_kind' must be 'experiment' or 'project_wide'"
        )

    objective_mode = _campaign_required_string(payload, "objective_mode").lower()
    status = _campaign_required_string(payload, "status").lower()
    if status not in CAMPAIGN_STATUSES:
        raise CampaignError(
            f".autolab/campaign.json field 'status' has unsupported value '{status}'"
        )

    iteration_id = str(payload.get("iteration_id", "")).strip()
    if scope_kind == "experiment" and not iteration_id:
        raise CampaignError(
            ".autolab/campaign.json field 'iteration_id' is required for experiment campaigns"
        )
    if scope_kind == "project_wide":
        iteration_id = ""

    started_at = _campaign_required_string(payload, "started_at")
    return {
        "campaign_id": _campaign_required_string(payload, "campaign_id"),
        "label": _campaign_required_string(payload, "label"),
        "scope_kind": scope_kind,
        "iteration_id": iteration_id,
        "objective_metric": _campaign_required_string(payload, "objective_metric"),
        "objective_mode": objective_mode,
        "status": status,
        "design_locked": bool(payload.get("design_locked", False)),
        "champion_run_id": _campaign_required_string(payload, "champion_run_id"),
        "champion_revision_label": _campaign_required_string(
            payload, "champion_revision_label"
        ),
        "no_improvement_streak": _campaign_non_negative_int(
            payload, "no_improvement_streak"
        ),
        "crash_streak": _campaign_non_negative_int(payload, "crash_streak"),
        "started_at": started_at,
        "last_oracle_at": str(payload.get("last_oracle_at", "")).strip(),
    }


def _load_campaign(repo_root: Path) -> dict[str, Any] | None:
    payload = _load_json_if_exists(_campaign_path(repo_root))
    if payload is None:
        return None
    return _normalize_campaign(payload)


def _write_campaign(repo_root: Path, payload: dict[str, Any]) -> Path:
    path = _campaign_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, _normalize_campaign(payload))
    return path


def _load_design_payload_for_state(
    repo_root: Path,
    state: dict[str, Any],
) -> tuple[dict[str, Any], Path]:
    if yaml is None:
        raise CampaignError("campaign mode requires PyYAML to read design.yaml")
    iteration_id = _normalize_space(str(state.get("iteration_id", "")))
    experiment_id = _normalize_space(str(state.get("experiment_id", "")))
    if not iteration_id:
        raise CampaignError("campaign mode requires state.iteration_id")
    iteration_dir, _iteration_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        require_exists=False,
    )
    design_path = iteration_dir / "design.yaml"
    if not design_path.exists():
        raise CampaignError(f"campaign mode requires {design_path}")
    try:
        payload = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise CampaignError(f"could not parse {design_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise CampaignError(f"{design_path} must contain a YAML mapping")
    return (payload, iteration_dir)


def _resolve_campaign_objective(
    repo_root: Path,
    state: dict[str, Any],
) -> tuple[str, str]:
    design_payload, _iteration_dir = _load_design_payload_for_state(repo_root, state)
    metrics = design_payload.get("metrics")
    if not isinstance(metrics, dict):
        raise CampaignError("design.yaml is missing metrics.primary")
    primary = metrics.get("primary")
    if not isinstance(primary, dict):
        raise CampaignError("design.yaml is missing metrics.primary")
    objective_metric = str(primary.get("name", "")).strip()
    objective_mode = str(primary.get("mode", "")).strip().lower()
    if not objective_metric:
        raise CampaignError("design.yaml metrics.primary.name must be non-empty")
    if not objective_mode:
        raise CampaignError("design.yaml metrics.primary.mode must be non-empty")
    return (objective_metric, objective_mode)


def _resolve_campaign_baseline(
    repo_root: Path,
    state: dict[str, Any],
    *,
    objective_metric: str,
) -> tuple[str, str]:
    _design_payload, iteration_dir = _load_design_payload_for_state(repo_root, state)
    run_id = str(state.get("last_run_id", "")).strip()
    if not run_id:
        raise CampaignError(
            "campaign mode requires an accepted baseline run in state.last_run_id"
        )
    metrics_path = iteration_dir / "runs" / run_id / "metrics.json"
    if not metrics_path.exists():
        raise CampaignError(
            f"campaign mode requires completed baseline metrics at {metrics_path}"
        )
    payload = _load_json_if_exists(metrics_path)
    if not isinstance(payload, dict):
        raise CampaignError(f"{metrics_path} must contain a JSON object")
    metrics_status = str(payload.get("status", "")).strip().lower()
    if metrics_status != "completed":
        raise CampaignError(
            "campaign mode requires baseline metrics status=completed "
            f"(got '{metrics_status or 'missing'}')"
        )
    primary_metric = payload.get("primary_metric")
    if not isinstance(primary_metric, dict):
        raise CampaignError(f"{metrics_path} is missing primary_metric")
    primary_metric_name = str(primary_metric.get("name", "")).strip()
    if primary_metric_name != objective_metric:
        raise CampaignError(
            "baseline metrics primary_metric.name does not match design objective "
            f"('{primary_metric_name or 'missing'}' != '{objective_metric}')"
        )
    return (run_id, _resolve_revision_label(repo_root))


def _create_campaign_payload(
    repo_root: Path,
    state: dict[str, Any],
    *,
    label: str,
    scope_kind: str,
) -> dict[str, Any]:
    normalized_label = str(label).strip()
    if not normalized_label:
        raise CampaignError("campaign label must be non-empty")
    normalized_scope_kind = str(scope_kind).strip().lower()
    if normalized_scope_kind not in {"experiment", "project_wide"}:
        raise CampaignError("campaign scope must be 'experiment' or 'project_wide'")

    objective_metric, objective_mode = _resolve_campaign_objective(repo_root, state)
    champion_run_id, champion_revision_label = _resolve_campaign_baseline(
        repo_root,
        state,
        objective_metric=objective_metric,
    )
    return {
        "campaign_id": _generate_campaign_id(),
        "label": normalized_label,
        "scope_kind": normalized_scope_kind,
        "iteration_id": (
            _normalize_space(str(state.get("iteration_id", "")))
            if normalized_scope_kind == "experiment"
            else ""
        ),
        "objective_metric": objective_metric,
        "objective_mode": objective_mode,
        "status": "running",
        "design_locked": False,
        "champion_run_id": champion_run_id,
        "champion_revision_label": champion_revision_label,
        "no_improvement_streak": 0,
        "crash_streak": 0,
        "started_at": _utc_now(),
        "last_oracle_at": "",
    }


def _validate_campaign_binding(
    repo_root: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    if normalized["scope_kind"] == "experiment":
        current_iteration_id = _normalize_space(str(state.get("iteration_id", "")))
        if current_iteration_id != normalized["iteration_id"]:
            raise CampaignError(
                "campaign iteration binding no longer matches current state "
                f"('{current_iteration_id or 'missing'}' != '{normalized['iteration_id']}')"
            )
    objective_metric, objective_mode = _resolve_campaign_objective(repo_root, state)
    if objective_metric != normalized["objective_metric"]:
        raise CampaignError(
            "campaign objective metric no longer matches design "
            f"('{objective_metric}' != '{normalized['objective_metric']}')"
        )
    if objective_mode != normalized["objective_mode"]:
        raise CampaignError(
            "campaign objective mode no longer matches design "
            f"('{objective_mode}' != '{normalized['objective_mode']}')"
        )
    return normalized


def _campaign_is_resumable(payload: dict[str, Any]) -> bool:
    normalized = _normalize_campaign(payload)
    return normalized["status"] in {"stopped", "error"}


def _campaign_summary(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_campaign(payload)
    return {
        "campaign_id": normalized["campaign_id"],
        "label": normalized["label"],
        "scope_kind": normalized["scope_kind"],
        "iteration_id": normalized["iteration_id"],
        "objective_metric": normalized["objective_metric"],
        "objective_mode": normalized["objective_mode"],
        "status": normalized["status"],
        "design_locked": normalized["design_locked"],
        "champion_run_id": normalized["champion_run_id"],
        "champion_revision_label": normalized["champion_revision_label"],
        "no_improvement_streak": normalized["no_improvement_streak"],
        "crash_streak": normalized["crash_streak"],
        "started_at": normalized["started_at"],
        "last_oracle_at": normalized["last_oracle_at"],
        "resumable": _campaign_is_resumable(normalized),
    }


def _mark_campaign_oracle_exported(repo_root: Path) -> dict[str, Any] | None:
    campaign = _load_campaign(repo_root)
    if campaign is None:
        return None
    campaign["last_oracle_at"] = _utc_now()
    _write_campaign(repo_root, campaign)
    return campaign
