"""Campaign state helpers for autonomous research sessions."""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from autolab.checkpoint import (
    _collect_canonical_artifacts,
    _resolve_revision_label,
    create_checkpoint,
    list_checkpoints,
    restore_checkpoint,
    set_checkpoint_pinned,
    verify_checkpoint,
)
from autolab.config import (
    _load_campaign_comparison_config,
    _load_meaningful_change_config,
)
from autolab.launch_runtime import _parse_memory_to_mb
from autolab.state import _resolve_iteration_directory
from autolab.utils import (
    _collect_git_status_entries,
    _load_json_if_exists,
    _normalize_space,
    _path_matches_any,
    _run_git,
    _utc_now,
    _write_json,
)

CAMPAIGN_FILENAME = "campaign.json"
CAMPAIGN_STATUSES = {
    "running",
    "stop_requested",
    "stopped",
    "needs_rethink",
    "error",
}
_CAMPAIGN_CHECKPOINT_LABEL_PREFIX = "campaign_champion_"
_CAMPAIGN_WORKTREE_EXCLUDE_PATTERNS = (
    ".autolab/**",
    ".autolab",
)
_RISK_SCORE_FIELDS = (
    "project_wide_tasks",
    "project_wide_unique_paths",
    "tasks_total",
    "waves_total",
    "observed_retries",
    "stage_attempt",
)


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


def _campaign_champion_checkpoint_label(campaign_id: str) -> str:
    normalized = str(campaign_id or "").strip()
    if not normalized:
        raise CampaignError("campaign_id is required for champion snapshots")
    return f"{_CAMPAIGN_CHECKPOINT_LABEL_PREFIX}{normalized}"


def _campaign_iteration_dir(repo_root: Path, state: dict[str, Any]) -> Path:
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
    return iteration_dir


def _campaign_iteration_relpath(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(repo_root.resolve()).as_posix()
    except Exception:
        try:
            return path.relative_to(repo_root).as_posix()
        except Exception as exc:
            raise CampaignError(
                f"path {path} is outside repository root {repo_root}"
            ) from exc


def _campaign_current_changed_paths(repo_root: Path) -> list[str]:
    rows: list[str] = []
    seen: set[str] = set()
    for rel_path, _status in _collect_git_status_entries(repo_root):
        if _path_matches_any(rel_path, _CAMPAIGN_WORKTREE_EXCLUDE_PATTERNS):
            continue
        if rel_path in seen:
            continue
        seen.add(rel_path)
        rows.append(rel_path)
    return rows


def _campaign_champion_checkpoint_entries(
    repo_root: Path,
    campaign: dict[str, Any],
) -> list[dict[str, Any]]:
    normalized = _normalize_campaign(campaign)
    label = _campaign_champion_checkpoint_label(normalized["campaign_id"])
    iteration_id = (
        normalized["iteration_id"] if normalized["scope_kind"] == "experiment" else ""
    )
    checkpoints = list_checkpoints(repo_root, iteration_id=iteration_id)
    return [
        entry for entry in checkpoints if str(entry.get("label", "")).strip() == label
    ]


def _campaign_latest_champion_checkpoint_id(
    repo_root: Path,
    campaign: dict[str, Any],
) -> str:
    entries = _campaign_champion_checkpoint_entries(repo_root, campaign)
    if not entries:
        return ""
    return str(entries[0].get("checkpoint_id", "")).strip()


def _campaign_has_champion_snapshot(
    repo_root: Path,
    campaign: dict[str, Any],
) -> bool:
    checkpoint_id = _campaign_latest_champion_checkpoint_id(repo_root, campaign)
    if not checkpoint_id:
        return False
    valid, _issues = verify_checkpoint(repo_root, checkpoint_id)
    return bool(valid)


def _campaign_seed_champion_snapshot(
    repo_root: Path,
    state_path: Path,
    campaign: dict[str, Any],
) -> str:
    normalized = _normalize_campaign(campaign)
    state_payload = _load_json_if_exists(state_path)
    if not isinstance(state_payload, dict):
        raise CampaignError(
            f"campaign mode requires readable state file at {state_path}"
        )
    if str(state_payload.get("stage", "")).strip() != "decide_repeat":
        raise CampaignError(
            "campaign champion snapshot requires current stage 'decide_repeat'"
        )
    checkpoint_label = _campaign_champion_checkpoint_label(normalized["campaign_id"])
    existing = _campaign_champion_checkpoint_entries(repo_root, normalized)
    checkpoint_id, _checkpoint_dir = create_checkpoint(
        repo_root,
        state_path=state_path,
        stage="decide_repeat",
        trigger="auto",
        label=checkpoint_label,
        iteration_id=str(state_payload.get("iteration_id", "")).strip(),
        experiment_id=str(state_payload.get("experiment_id", "")).strip(),
        scope_kind=normalized["scope_kind"],
        pinned=True,
        label_origin="system",
        extra_artifacts=_campaign_current_changed_paths(repo_root),
    )
    for entry in existing:
        stale_id = str(entry.get("checkpoint_id", "")).strip()
        if stale_id and stale_id != checkpoint_id:
            try:
                set_checkpoint_pinned(repo_root, stale_id, pinned=False)
            except Exception:
                continue
    return checkpoint_id


def _campaign_checkpoint_manifest(
    repo_root: Path, checkpoint_id: str
) -> dict[str, Any]:
    path = repo_root / ".autolab" / "checkpoints" / checkpoint_id / "manifest.json"
    payload = _load_json_if_exists(path)
    if not isinstance(payload, dict):
        raise CampaignError(f"campaign checkpoint {checkpoint_id} has invalid manifest")
    return payload


def _campaign_checkpoint_file_path(
    repo_root: Path,
    checkpoint_id: str,
    relative_path: str,
) -> Path:
    return (
        repo_root / ".autolab" / "checkpoints" / checkpoint_id / "files" / relative_path
    )


def _campaign_checkpoint_json_artifact(
    repo_root: Path,
    checkpoint_id: str,
    relative_path: str,
) -> dict[str, Any]:
    path = _campaign_checkpoint_file_path(repo_root, checkpoint_id, relative_path)
    payload = _load_json_if_exists(path)
    if not isinstance(payload, dict):
        return {}
    return payload


def _campaign_canonical_relpaths(
    repo_root: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
) -> set[str]:
    iteration_dir = _campaign_iteration_dir(repo_root, state)
    return {
        rel_path
        for _abs_path, rel_path in _collect_canonical_artifacts(
            repo_root,
            iteration_dir,
            "decide_repeat",
            str(campaign.get("scope_kind", "experiment")),
        )
    }


def _campaign_generated_surface_excludes(
    repo_root: Path,
    state: dict[str, Any],
) -> tuple[str, ...]:
    iteration_dir = _campaign_iteration_dir(repo_root, state)
    iteration_rel = _campaign_iteration_relpath(repo_root, iteration_dir)
    return (f"{iteration_rel}/runs/**",)


def _campaign_filter_surface_paths(
    paths: list[str],
    *,
    repo_root: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
) -> list[str]:
    meaningful_cfg = _load_meaningful_change_config(repo_root)
    canonical_paths = _campaign_canonical_relpaths(repo_root, state, campaign)
    generated_excludes = _campaign_generated_surface_excludes(repo_root, state)
    filtered: list[str] = []
    seen: set[str] = set()
    for rel_path in paths:
        if not rel_path or rel_path in seen:
            continue
        if rel_path in canonical_paths:
            continue
        if _path_matches_any(rel_path, _CAMPAIGN_WORKTREE_EXCLUDE_PATTERNS):
            continue
        if _path_matches_any(rel_path, generated_excludes):
            continue
        if _path_matches_any(rel_path, meaningful_cfg.exclude_paths):
            continue
        seen.add(rel_path)
        filtered.append(rel_path)
    return filtered


def _campaign_file_size_metric(path: Path, metric: str) -> int:
    if not path.exists() or not path.is_file():
        return 0
    try:
        data = path.read_bytes()
    except OSError:
        return 0
    if metric == "chars":
        return len(data)
    if metric == "lines":
        if not data:
            return 0
        return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
    return 1


def _campaign_surface_score_current(
    repo_root: Path,
    *,
    paths: list[str],
    metric: str,
) -> int:
    total = 0
    for rel_path in paths:
        if metric == "files":
            total += 1
            continue
        total += _campaign_file_size_metric(repo_root / rel_path, metric)
    return total


def _campaign_surface_score_checkpoint(
    repo_root: Path,
    *,
    checkpoint_id: str,
    paths: list[str],
    metric: str,
) -> int:
    total = 0
    for rel_path in paths:
        if metric == "files":
            total += 1
            continue
        total += _campaign_file_size_metric(
            _campaign_checkpoint_file_path(repo_root, checkpoint_id, rel_path),
            metric,
        )
    return total


def _campaign_normalize_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except Exception:
        return 0


def _campaign_load_current_risk_payload(
    repo_root: Path,
    state: dict[str, Any],
) -> dict[str, Any]:
    iteration_dir = _campaign_iteration_dir(repo_root, state)
    plan_approval = _load_json_if_exists(iteration_dir / "plan_approval.json")
    if isinstance(plan_approval, dict):
        return plan_approval
    plan_check = _load_json_if_exists(iteration_dir / "plan_check_result.json")
    if isinstance(plan_check, dict):
        approval_risk = plan_check.get("approval_risk")
        if isinstance(approval_risk, dict):
            return approval_risk
    return {}


def _campaign_load_checkpoint_risk_payload(
    repo_root: Path,
    checkpoint_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    iteration_dir = _campaign_iteration_dir(repo_root, state)
    approval_rel = _campaign_iteration_relpath(
        repo_root, iteration_dir / "plan_approval.json"
    )
    payload = _campaign_checkpoint_json_artifact(repo_root, checkpoint_id, approval_rel)
    if payload:
        return payload
    plan_check_rel = _campaign_iteration_relpath(
        repo_root, iteration_dir / "plan_check_result.json"
    )
    plan_check = _campaign_checkpoint_json_artifact(
        repo_root, checkpoint_id, plan_check_rel
    )
    if isinstance(plan_check.get("approval_risk"), dict):
        return dict(plan_check["approval_risk"])
    return {}


def _campaign_risk_score(payload: dict[str, Any]) -> tuple[int, ...]:
    if not payload:
        return ()
    counts = payload.get("counts")
    if not isinstance(counts, dict):
        counts = {}
    risk_flags = payload.get("risk_flags")
    if not isinstance(risk_flags, dict):
        risk_flags = {}
    trigger_reasons = payload.get("trigger_reasons")
    if not isinstance(trigger_reasons, list):
        trigger_reasons = []
    score = [
        int(bool(payload.get("requires_approval", False))),
        int(bool(risk_flags.get("uat_required", False))),
        int(bool(risk_flags.get("remote_profile_required", False))),
        len([item for item in trigger_reasons if str(item).strip()]),
    ]
    score.extend(
        _campaign_normalize_int(counts.get(field, 0)) for field in _RISK_SCORE_FIELDS
    )
    return tuple(score)


def _campaign_load_metrics_payload(
    repo_root: Path,
    state: dict[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    iteration_dir = _campaign_iteration_dir(repo_root, state)
    payload = _load_json_if_exists(iteration_dir / "runs" / run_id / "metrics.json")
    if not isinstance(payload, dict):
        raise CampaignError(
            f"campaign comparison requires metrics.json for run_id={run_id}"
        )
    return payload


def _campaign_load_manifest_payload(
    repo_root: Path,
    state: dict[str, Any],
    *,
    run_id: str,
) -> dict[str, Any]:
    iteration_dir = _campaign_iteration_dir(repo_root, state)
    payload = _load_json_if_exists(
        iteration_dir / "runs" / run_id / "run_manifest.json"
    )
    if not isinstance(payload, dict):
        return {}
    return payload


def _campaign_primary_metric_value(
    payload: dict[str, Any],
    *,
    run_id: str,
    objective_metric: str,
) -> float | None:
    status = str(payload.get("status", "")).strip().lower()
    primary_metric = payload.get("primary_metric")
    if not isinstance(primary_metric, dict):
        raise CampaignError(
            f"campaign metrics for run_id={run_id} are missing primary_metric"
        )
    metric_name = str(primary_metric.get("name", "")).strip()
    if metric_name != objective_metric:
        raise CampaignError(
            "campaign metrics objective mismatch "
            f"(run_id={run_id}, metric='{metric_name or 'missing'}', expected='{objective_metric}')"
        )
    value = primary_metric.get("value")
    if status != "completed" or value is None:
        return None
    try:
        return float(value)
    except Exception:
        raise CampaignError(
            f"campaign metrics primary_metric.value for run_id={run_id} must be numeric or null"
        )


def _campaign_metric_decision(
    *,
    objective_mode: str,
    challenger_value: float | None,
    champion_value: float | None,
) -> int:
    if champion_value is None:
        raise CampaignError("campaign champion metrics are missing a comparable value")
    if challenger_value is None:
        return -1
    if objective_mode == "maximize":
        if challenger_value > champion_value:
            return 1
        if challenger_value < champion_value:
            return -1
        return 0
    if objective_mode == "minimize":
        if challenger_value < champion_value:
            return 1
        if challenger_value > champion_value:
            return -1
        return 0
    raise CampaignError(f"unsupported campaign objective_mode '{objective_mode}'")


def _campaign_manifest_memory_mb(payload: dict[str, Any]) -> int | None:
    if not payload:
        return None
    resource_request = payload.get("resource_request")
    if not isinstance(resource_request, dict):
        resource_request = {}
    if "memory_mb" in resource_request:
        try:
            parsed = int(resource_request.get("memory_mb"))
        except Exception:
            parsed = 0
        if parsed > 0:
            return parsed
    for raw_value in (
        resource_request.get("memory"),
        resource_request.get("mem"),
        payload.get("memory"),
    ):
        value = str(raw_value or "").strip()
        if not value:
            continue
        parsed = _parse_memory_to_mb(value)
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _campaign_status_groups(
    repo_root: Path,
) -> tuple[list[str], list[str]]:
    tracked: list[str] = []
    untracked: list[str] = []
    for rel_path, status_code in _collect_git_status_entries(repo_root):
        if _path_matches_any(rel_path, _CAMPAIGN_WORKTREE_EXCLUDE_PATTERNS):
            continue
        normalized = status_code.strip()
        if normalized == "??":
            untracked.append(rel_path)
            continue
        tracked.append(rel_path)
    tracked = sorted(dict.fromkeys(tracked))
    untracked = sorted(dict.fromkeys(untracked))
    return (tracked, untracked)


def _campaign_reset_worktree_to_head(repo_root: Path) -> None:
    tracked_paths, untracked_paths = _campaign_status_groups(repo_root)
    if tracked_paths:
        restore_result = _run_git(
            repo_root,
            ["restore", "--staged", "--worktree", "--", *tracked_paths],
        )
        if restore_result.returncode != 0:
            detail = (restore_result.stderr or restore_result.stdout or "").strip()
            raise CampaignError(
                f"campaign restore failed to reset tracked paths to HEAD: {detail or 'git restore failed'}"
            )
    for rel_path in sorted(
        untracked_paths, key=lambda item: len(Path(item).parts), reverse=True
    ):
        candidate = repo_root / rel_path
        if candidate.is_symlink() or candidate.is_file():
            candidate.unlink(missing_ok=True)
            continue
        if candidate.is_dir():
            shutil.rmtree(candidate)


def _campaign_restore_champion_state(
    repo_root: Path,
    state_path: Path,
    campaign: dict[str, Any],
    *,
    checkpoint_id: str,
) -> None:
    valid, issues = verify_checkpoint(repo_root, checkpoint_id)
    if not valid:
        raise CampaignError(
            f"campaign champion checkpoint {checkpoint_id} is invalid: {'; '.join(issues)}"
        )
    _campaign_reset_worktree_to_head(repo_root)
    success, message, _changed = restore_checkpoint(
        repo_root,
        state_path,
        checkpoint_id,
        archive_current=True,
    )
    if not success:
        raise CampaignError(
            f"campaign could not restore champion checkpoint {checkpoint_id}: {message}"
        )


def _campaign_compare_challenger(
    repo_root: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
    *,
    champion_checkpoint_id: str,
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    challenger_run_id = str(state.get("last_run_id", "")).strip()
    champion_run_id = normalized["champion_run_id"]
    if not challenger_run_id:
        raise CampaignError("campaign comparison requires state.last_run_id")
    if challenger_run_id == champion_run_id:
        raise CampaignError(
            "campaign comparison requires a challenger run distinct from the champion"
        )

    challenger_metrics = _campaign_load_metrics_payload(
        repo_root, state, run_id=challenger_run_id
    )
    champion_metrics = _campaign_load_metrics_payload(
        repo_root, state, run_id=champion_run_id
    )
    challenger_value = _campaign_primary_metric_value(
        challenger_metrics,
        run_id=challenger_run_id,
        objective_metric=normalized["objective_metric"],
    )
    champion_value = _campaign_primary_metric_value(
        champion_metrics,
        run_id=champion_run_id,
        objective_metric=normalized["objective_metric"],
    )

    metric_decision = _campaign_metric_decision(
        objective_mode=normalized["objective_mode"],
        challenger_value=challenger_value,
        champion_value=champion_value,
    )
    if metric_decision > 0:
        return {
            "winner": "challenger",
            "summary": (
                "primary metric improved "
                f"({challenger_run_id}={challenger_value} vs {champion_run_id}={champion_value})"
            ),
        }
    if metric_decision < 0:
        return {
            "winner": "champion",
            "summary": (
                "primary metric did not improve "
                f"({challenger_run_id}={challenger_value} vs {champion_run_id}={champion_value})"
            ),
        }

    challenger_manifest = _campaign_load_manifest_payload(
        repo_root, state, run_id=challenger_run_id
    )
    champion_manifest = _campaign_load_manifest_payload(
        repo_root, state, run_id=champion_run_id
    )
    challenger_memory = _campaign_manifest_memory_mb(challenger_manifest)
    champion_memory = _campaign_manifest_memory_mb(champion_manifest)
    if (
        challenger_memory is not None
        and champion_memory is not None
        and challenger_memory != champion_memory
    ):
        winner = "challenger" if challenger_memory < champion_memory else "champion"
        return {
            "winner": winner,
            "summary": (
                "primary metric tied; memory tie-break "
                f"({challenger_run_id}={challenger_memory}MB vs "
                f"{champion_run_id}={champion_memory}MB)"
            ),
        }

    comparison_cfg = _load_campaign_comparison_config(repo_root)
    if comparison_cfg.complexity_proxy != "none":
        current_surface_paths = _campaign_filter_surface_paths(
            _campaign_current_changed_paths(repo_root),
            repo_root=repo_root,
            state=state,
            campaign=normalized,
        )
        champion_manifest_payload = _campaign_checkpoint_manifest(
            repo_root, champion_checkpoint_id
        )
        champion_surface_paths = _campaign_filter_surface_paths(
            [
                str(entry.get("relative_path", "")).strip()
                for entry in champion_manifest_payload.get("artifacts", [])
                if isinstance(entry, dict)
            ],
            repo_root=repo_root,
            state=state,
            campaign=normalized,
        )
        challenger_surface = _campaign_surface_score_current(
            repo_root,
            paths=current_surface_paths,
            metric=comparison_cfg.change_size_metric,
        )
        champion_surface = _campaign_surface_score_checkpoint(
            repo_root,
            checkpoint_id=champion_checkpoint_id,
            paths=champion_surface_paths,
            metric=comparison_cfg.change_size_metric,
        )
        if challenger_surface != champion_surface:
            winner = (
                "challenger" if challenger_surface < champion_surface else "champion"
            )
            return {
                "winner": winner,
                "summary": (
                    "primary metric and memory tied; complexity tie-break "
                    f"({comparison_cfg.change_size_metric}: "
                    f"{challenger_run_id}={challenger_surface} vs "
                    f"{champion_run_id}={champion_surface})"
                ),
            }

    challenger_risk = _campaign_risk_score(
        _campaign_load_current_risk_payload(repo_root, state)
    )
    champion_risk = _campaign_risk_score(
        _campaign_load_checkpoint_risk_payload(
            repo_root,
            champion_checkpoint_id,
            state,
        )
    )
    if challenger_risk and champion_risk and challenger_risk != champion_risk:
        winner = "challenger" if challenger_risk < champion_risk else "champion"
        return {
            "winner": winner,
            "summary": (
                "all prior tie-breaks tied; policy-risk tie-break "
                f"({challenger_run_id}={challenger_risk} vs "
                f"{champion_run_id}={champion_risk})"
            ),
        }

    return {
        "winner": "champion",
        "summary": (
            "all campaign comparisons tied; keeping existing champion "
            f"({champion_run_id})"
        ),
    }


def _campaign_apply_challenger_outcome(
    repo_root: Path,
    state_path: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    checkpoint_id = _campaign_latest_champion_checkpoint_id(repo_root, normalized)
    if not checkpoint_id:
        raise CampaignError(
            "campaign champion snapshot is missing; rerun from decide_repeat to reseed"
        )
    comparison = _campaign_compare_challenger(
        repo_root,
        state,
        normalized,
        champion_checkpoint_id=checkpoint_id,
    )
    winner = str(comparison.get("winner", "")).strip()
    summary = str(comparison.get("summary", "")).strip()
    challenger_run_id = str(state.get("last_run_id", "")).strip()

    if winner == "challenger":
        _campaign_seed_champion_snapshot(repo_root, state_path, normalized)
        updated = dict(normalized)
        updated["champion_run_id"] = challenger_run_id
        updated["champion_revision_label"] = _resolve_revision_label(repo_root)
        updated["no_improvement_streak"] = 0
        _write_campaign(repo_root, updated)
        return {
            "action": "promote",
            "campaign": updated,
            "summary": summary,
        }

    _campaign_restore_champion_state(
        repo_root,
        state_path,
        normalized,
        checkpoint_id=checkpoint_id,
    )
    updated = dict(normalized)
    updated["no_improvement_streak"] = (
        int(updated.get("no_improvement_streak", 0) or 0) + 1
    )
    _write_campaign(repo_root, updated)
    return {
        "action": "discard",
        "campaign": updated,
        "summary": summary,
    }


__all__ = [name for name in globals() if not name.startswith("__")]
