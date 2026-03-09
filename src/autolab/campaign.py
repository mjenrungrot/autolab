"""Campaign state helpers for autonomous research sessions."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
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
    _load_campaign_governance_config,
    _load_meaningful_change_config,
)
from autolab.launch_runtime import _parse_memory_to_mb, _resolve_launch_mode
from autolab.oracle_runtime import load_oracle_state, write_oracle_state
from autolab.scope import _resolve_project_wide_root
from autolab.state import _resolve_iteration_directory
from autolab.utils import (
    _collect_git_status_entries,
    _detect_priority_host_mode,
    _load_json_if_exists,
    _manifest_timestamp,
    _normalize_space,
    _parse_utc,
    _path_fingerprint,
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
_CAMPAIGN_RESULT_STATUSES = ("keep", "discard", "crash", "partial")
_CAMPAIGN_IDEA_ENTRY_STATUSES = ("active", "keep", "discard", "crash")
_CAMPAIGN_IDEA_FINAL_STATUSES = ("keep", "discard", "crash")
_CAMPAIGN_IDEA_JOURNAL_RETAIN_LIMIT = 100
_CAMPAIGN_IDEA_RECENT_FAMILY_LIMIT = 3
_CAMPAIGN_IDEA_RECENT_ENTRY_LIMIT = 5
_CAMPAIGN_IDEA_SAMPLE_SURFACE_LIMIT = 5
_CAMPAIGN_IDEA_SURFACE_LIMIT = 12
_CAMPAIGN_RESULTS_TSV_FILENAME = "results.tsv"
_CAMPAIGN_RESULTS_MD_FILENAME = "results.md"
_CAMPAIGN_MORNING_REPORT_FILENAME = "morning_report.md"
_CAMPAIGN_ORACLE_FEEDBACK_SIGNALS = {"none", "stop", "rethink"}
_CAMPAIGN_COMPARE_LOG_PATTERN = re.compile(
    r"^campaign (?P<action>promote|discard): champion=(?P<champion>\S+) "
    r"challenger=(?P<challenger>\S+); ?(?P<summary>.*)$"
)
_CAMPAIGN_DECISION_STAGES = {"implementation", "design"}
_CAMPAIGN_LOCK_CONTRACT_FIELDS = (
    "captured_at",
    "hypothesis_path",
    "hypothesis_fingerprint",
    "design_path",
    "design_fingerprint",
    "extract_parser_fingerprint",
    "evaluator_fingerprint",
    "remote_profile_name",
    "remote_profile_mode",
    "remote_profile_config_fingerprint",
)
_CAMPAIGN_EVALUATOR_CONTRACT_PATHS = (
    ".autolab/workflow.yaml",
    ".autolab/verifier_policy.yaml",
    ".autolab/schemas/decision_result.schema.json",
    ".autolab/schemas/metrics.schema.json",
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


def _campaign_default_lock_contract() -> dict[str, str]:
    return {field: "" for field in _CAMPAIGN_LOCK_CONTRACT_FIELDS}


def _normalize_campaign_lock_contract(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        payload = {}
    normalized = _campaign_default_lock_contract()
    for field in _CAMPAIGN_LOCK_CONTRACT_FIELDS:
        normalized[field] = str(payload.get(field, "")).strip()
    return normalized


def _normalize_campaign_oracle_feedback_entry(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        payload = {}
    signal = str(payload.get("signal", "")).strip().lower()
    if signal not in _CAMPAIGN_ORACLE_FEEDBACK_SIGNALS:
        signal = "none"
    summary = str(payload.get("summary", "")).strip()
    detail = str(payload.get("detail", "")).strip() or summary
    return {
        "applied_at": str(payload.get("applied_at", "")).strip(),
        "source": str(payload.get("source", "")).strip(),
        "summary": summary,
        "detail": detail,
        "signal": signal,
    }


def _normalize_campaign_oracle_feedback(payload: Any) -> list[dict[str, str]]:
    if not isinstance(payload, list):
        payload = []
    normalized: list[dict[str, str]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for raw_entry in payload:
        entry = _normalize_campaign_oracle_feedback_entry(raw_entry)
        if not entry["summary"] and not entry["detail"]:
            continue
        dedupe_key = (
            entry["source"],
            entry["summary"],
            entry["detail"],
            entry["signal"],
        )
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        normalized.append(entry)
    return normalized


def _campaign_lock_mode(payload: dict[str, Any]) -> str:
    if bool(payload.get("harness_locked", False)):
        return "harness"
    if bool(payload.get("design_locked", False)):
        return "design"
    return "none"


def _campaign_required_lock_fields(mode: str) -> tuple[str, ...]:
    if mode == "harness":
        return _CAMPAIGN_LOCK_CONTRACT_FIELDS
    if mode == "design":
        return (
            "captured_at",
            "hypothesis_path",
            "hypothesis_fingerprint",
            "design_path",
            "design_fingerprint",
        )
    return ()


def _campaign_missing_lock_fields(payload: dict[str, Any]) -> list[str]:
    mode = _campaign_lock_mode(payload)
    if mode == "none":
        return []
    contract = _normalize_campaign_lock_contract(payload.get("lock_contract"))
    missing: list[str] = []
    for field in _campaign_required_lock_fields(mode):
        if str(contract.get(field, "")).strip():
            continue
        missing.append(field)
    return missing


def _campaign_structured_fingerprint(payload: Any) -> str:
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(rendered.encode("utf-8")).hexdigest()


def _campaign_default_active_candidate() -> dict[str, Any]:
    return {
        "decision": "",
        "started_at": "",
        "run_id": "",
        "fix_attempts": 0,
        "timeout_reference_seconds": 0.0,
        "journal_entry_id": "",
        "family_hint": "",
        "thesis_hint": "",
    }


def _normalize_campaign_active_candidate(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    normalized = _campaign_default_active_candidate()
    normalized["decision"] = str(payload.get("decision", "")).strip().lower()
    if normalized["decision"] not in _CAMPAIGN_DECISION_STAGES:
        normalized["decision"] = ""
    normalized["started_at"] = str(payload.get("started_at", "")).strip()
    normalized["run_id"] = str(payload.get("run_id", "")).strip()
    normalized["fix_attempts"] = _campaign_non_negative_int(payload, "fix_attempts")
    try:
        timeout_reference_seconds = float(payload.get("timeout_reference_seconds", 0.0))
    except Exception as exc:
        raise CampaignError(
            ".autolab/campaign.json field 'active_candidate.timeout_reference_seconds' must be numeric"
        ) from exc
    if timeout_reference_seconds < 0:
        raise CampaignError(
            ".autolab/campaign.json field 'active_candidate.timeout_reference_seconds' must be >= 0"
        )
    normalized["timeout_reference_seconds"] = timeout_reference_seconds
    normalized["journal_entry_id"] = str(payload.get("journal_entry_id", "")).strip()
    normalized["family_hint"] = str(payload.get("family_hint", "")).strip()
    normalized["thesis_hint"] = str(payload.get("thesis_hint", "")).strip()
    return normalized


def _campaign_string_list(
    payload: Any,
    *,
    limit: int | None = None,
) -> list[str]:
    if not isinstance(payload, list):
        payload = []
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_item in payload:
        text = str(raw_item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        normalized.append(text)
        if limit is not None and len(normalized) >= limit:
            break
    return normalized


def _campaign_default_idea_journal_entry() -> dict[str, Any]:
    return {
        "entry_id": "",
        "decision": "",
        "started_at": "",
        "updated_at": "",
        "completed_at": "",
        "status": "active",
        "attempt_count": 1,
        "run_ids": [],
        "thesis": "",
        "thesis_source": "",
        "family_key": "",
        "family_label": "",
        "family_source": "",
        "touched_surfaces": [],
        "family_surfaces": [],
        "near_miss": False,
        "outcome_reason": "",
        "champion_before_run_id": "",
        "champion_after_run_id": "",
    }


def _normalize_campaign_idea_journal_entry(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    normalized = _campaign_default_idea_journal_entry()
    normalized["entry_id"] = str(payload.get("entry_id", "")).strip()
    normalized["decision"] = str(payload.get("decision", "")).strip().lower()
    if normalized["decision"] not in _CAMPAIGN_DECISION_STAGES:
        normalized["decision"] = ""
    normalized["started_at"] = str(payload.get("started_at", "")).strip()
    normalized["updated_at"] = str(payload.get("updated_at", "")).strip()
    normalized["completed_at"] = str(payload.get("completed_at", "")).strip()
    status = str(payload.get("status", "active")).strip().lower() or "active"
    if status not in _CAMPAIGN_IDEA_ENTRY_STATUSES:
        status = "active"
    normalized["status"] = status
    try:
        attempt_count = int(payload.get("attempt_count", 1))
    except Exception as exc:
        raise CampaignError(
            ".autolab/campaign.json field 'idea_journal.entries[].attempt_count' must be an integer"
        ) from exc
    normalized["attempt_count"] = max(1, attempt_count)
    normalized["run_ids"] = _campaign_string_list(
        payload.get("run_ids"),
        limit=_CAMPAIGN_IDEA_SURFACE_LIMIT,
    )
    normalized["thesis"] = str(payload.get("thesis", "")).strip()
    normalized["thesis_source"] = str(payload.get("thesis_source", "")).strip()
    normalized["family_key"] = str(payload.get("family_key", "")).strip()
    normalized["family_label"] = str(payload.get("family_label", "")).strip()
    normalized["family_source"] = str(payload.get("family_source", "")).strip()
    normalized["touched_surfaces"] = _campaign_string_list(
        payload.get("touched_surfaces"),
        limit=_CAMPAIGN_IDEA_SURFACE_LIMIT,
    )
    normalized["family_surfaces"] = _campaign_string_list(
        payload.get("family_surfaces"),
        limit=_CAMPAIGN_IDEA_SAMPLE_SURFACE_LIMIT,
    )
    normalized["near_miss"] = bool(payload.get("near_miss", False))
    normalized["outcome_reason"] = str(payload.get("outcome_reason", "")).strip()
    normalized["champion_before_run_id"] = str(
        payload.get("champion_before_run_id", "")
    ).strip()
    normalized["champion_after_run_id"] = str(
        payload.get("champion_after_run_id", "")
    ).strip()
    return normalized


def _campaign_default_family_stats_entry() -> dict[str, Any]:
    return {
        "family_label": "",
        "first_seen_at": "",
        "last_seen_at": "",
        "counts": {status: 0 for status in _CAMPAIGN_IDEA_FINAL_STATUSES},
        "near_miss_count": 0,
        "last_outcome": "",
        "last_thesis": "",
        "sample_surfaces": [],
    }


def _normalize_campaign_family_stats_entry(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    normalized = _campaign_default_family_stats_entry()
    normalized["family_label"] = str(payload.get("family_label", "")).strip()
    normalized["first_seen_at"] = str(payload.get("first_seen_at", "")).strip()
    normalized["last_seen_at"] = str(payload.get("last_seen_at", "")).strip()
    counts_payload = payload.get("counts")
    if not isinstance(counts_payload, dict):
        counts_payload = {}
    counts: dict[str, int] = {}
    for status in _CAMPAIGN_IDEA_FINAL_STATUSES:
        counts[status] = _campaign_non_negative_int(counts_payload, status)
    normalized["counts"] = counts
    normalized["near_miss_count"] = _campaign_non_negative_int(
        payload, "near_miss_count"
    )
    last_outcome = str(payload.get("last_outcome", "")).strip().lower()
    if last_outcome not in _CAMPAIGN_IDEA_FINAL_STATUSES:
        last_outcome = ""
    normalized["last_outcome"] = last_outcome
    normalized["last_thesis"] = str(payload.get("last_thesis", "")).strip()
    normalized["sample_surfaces"] = _campaign_string_list(
        payload.get("sample_surfaces"),
        limit=_CAMPAIGN_IDEA_SAMPLE_SURFACE_LIMIT,
    )
    return normalized


def _campaign_default_idea_journal() -> dict[str, Any]:
    return {
        "active_entry_id": "",
        "next_entry_seq": 1,
        "retained_entry_limit": _CAMPAIGN_IDEA_JOURNAL_RETAIN_LIMIT,
        "entries": [],
        "family_stats": {},
    }


def _campaign_trim_idea_journal_entries(
    payload: dict[str, Any],
) -> dict[str, Any]:
    normalized = _campaign_default_idea_journal()
    normalized.update(payload)
    retained_entry_limit = max(
        1,
        int(
            normalized.get("retained_entry_limit", _CAMPAIGN_IDEA_JOURNAL_RETAIN_LIMIT)
        ),
    )
    active_entry_id = str(normalized.get("active_entry_id", "")).strip()
    entries = normalized.get("entries")
    if not isinstance(entries, list):
        entries = []
    completed_indices = [
        index
        for index, entry in enumerate(entries)
        if isinstance(entry, dict)
        and str(entry.get("entry_id", "")).strip() != active_entry_id
        and str(entry.get("status", "")).strip().lower()
        in _CAMPAIGN_IDEA_FINAL_STATUSES
    ]
    overflow = len(completed_indices) - retained_entry_limit
    if overflow <= 0:
        normalized["entries"] = entries
        return normalized
    drop_indices = set(completed_indices[:overflow])
    normalized["entries"] = [
        entry for index, entry in enumerate(entries) if index not in drop_indices
    ]
    return normalized


def _normalize_campaign_idea_journal(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    normalized = _campaign_default_idea_journal()
    normalized["active_entry_id"] = str(payload.get("active_entry_id", "")).strip()
    try:
        next_entry_seq = int(payload.get("next_entry_seq", 1))
    except Exception as exc:
        raise CampaignError(
            ".autolab/campaign.json field 'idea_journal.next_entry_seq' must be an integer"
        ) from exc
    normalized["next_entry_seq"] = max(1, next_entry_seq)
    try:
        retained_entry_limit = int(
            payload.get("retained_entry_limit", _CAMPAIGN_IDEA_JOURNAL_RETAIN_LIMIT)
        )
    except Exception as exc:
        raise CampaignError(
            ".autolab/campaign.json field 'idea_journal.retained_entry_limit' must be an integer"
        ) from exc
    normalized["retained_entry_limit"] = max(1, retained_entry_limit)

    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raw_entries = []
    entries: list[dict[str, Any]] = []
    seen_entry_ids: set[str] = set()
    for raw_entry in raw_entries:
        entry = _normalize_campaign_idea_journal_entry(raw_entry)
        entry_id = entry["entry_id"]
        if not entry_id or entry_id in seen_entry_ids:
            continue
        seen_entry_ids.add(entry_id)
        entries.append(entry)
    if (
        normalized["active_entry_id"]
        and normalized["active_entry_id"] not in seen_entry_ids
    ):
        normalized["active_entry_id"] = ""
    if not normalized["active_entry_id"]:
        for entry in reversed(entries):
            if entry["status"] == "active":
                normalized["active_entry_id"] = entry["entry_id"]
                break
    normalized["next_entry_seq"] = max(
        normalized["next_entry_seq"],
        len(entries) + 1,
    )
    normalized["entries"] = entries

    family_stats_payload = payload.get("family_stats")
    if not isinstance(family_stats_payload, dict):
        family_stats_payload = {}
    family_stats: dict[str, dict[str, Any]] = {}
    for raw_key, raw_entry in family_stats_payload.items():
        family_key = str(raw_key or "").strip()
        if not family_key:
            continue
        family_stats[family_key] = _normalize_campaign_family_stats_entry(raw_entry)
    normalized["family_stats"] = family_stats
    return _campaign_trim_idea_journal_entries(normalized)


def _campaign_default_last_governance_event() -> dict[str, str]:
    return {
        "at": "",
        "category": "",
        "run_id": "",
        "reason": "",
    }


def _normalize_campaign_last_governance_event(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        payload = {}
    normalized = _campaign_default_last_governance_event()
    for key in normalized:
        normalized[key] = str(payload.get(key, "")).strip()
    return normalized


def _campaign_has_active_candidate(payload: dict[str, Any]) -> bool:
    candidate = _normalize_campaign_active_candidate(payload.get("active_candidate"))
    return bool(candidate["decision"] and candidate["started_at"])


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
    harness_locked = bool(payload.get("harness_locked", False))
    design_locked = bool(payload.get("design_locked", False)) or harness_locked
    lock_contract = _normalize_campaign_lock_contract(payload.get("lock_contract"))
    return {
        "campaign_id": _campaign_required_string(payload, "campaign_id"),
        "label": _campaign_required_string(payload, "label"),
        "scope_kind": scope_kind,
        "iteration_id": iteration_id,
        "objective_metric": _campaign_required_string(payload, "objective_metric"),
        "objective_mode": objective_mode,
        "status": status,
        "design_locked": design_locked,
        "harness_locked": harness_locked,
        "lock_contract": lock_contract,
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
        "oracle_feedback": _normalize_campaign_oracle_feedback(
            payload.get("oracle_feedback")
        ),
        "active_candidate": _normalize_campaign_active_candidate(
            payload.get("active_candidate")
        ),
        "last_governance_event": _normalize_campaign_last_governance_event(
            payload.get("last_governance_event")
        ),
        "idea_journal": _normalize_campaign_idea_journal(payload.get("idea_journal")),
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


def _campaign_find_idea_entry_index(
    idea_journal: dict[str, Any],
    entry_id: str,
) -> int:
    normalized_entry_id = str(entry_id or "").strip()
    if not normalized_entry_id:
        return -1
    entries = idea_journal.get("entries")
    if not isinstance(entries, list):
        return -1
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        if str(entry.get("entry_id", "")).strip() == normalized_entry_id:
            return index
    return -1


def _campaign_active_idea_entry_info(
    campaign: dict[str, Any],
) -> tuple[int, dict[str, Any] | None]:
    normalized = _normalize_campaign(campaign)
    idea_journal = _normalize_campaign_idea_journal(normalized.get("idea_journal"))
    candidate = _normalize_campaign_active_candidate(normalized.get("active_candidate"))
    entry_id = candidate["journal_entry_id"] or idea_journal["active_entry_id"]
    entry_index = _campaign_find_idea_entry_index(idea_journal, entry_id)
    if entry_index < 0:
        return (-1, None)
    entries = idea_journal.get("entries")
    if not isinstance(entries, list) or entry_index >= len(entries):
        return (-1, None)
    entry = entries[entry_index]
    if not isinstance(entry, dict):
        return (-1, None)
    return (entry_index, dict(entry))


def _campaign_collapse_family_surface(rel_path: str) -> str:
    normalized = str(rel_path or "").strip().strip("/")
    if not normalized:
        return ""
    parts = Path(normalized).parts
    if len(parts) >= 2:
        return "/".join(parts[:2])
    return normalized


def _campaign_derive_idea_entry_surfaces(
    repo_root: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
) -> tuple[list[str], list[str]]:
    touched_surfaces = _campaign_filter_surface_paths(
        _campaign_current_changed_paths(repo_root),
        repo_root=repo_root,
        state=state,
        campaign=campaign,
    )
    touched_surfaces = touched_surfaces[:_CAMPAIGN_IDEA_SURFACE_LIMIT]
    family_surfaces = _campaign_string_list(
        [_campaign_collapse_family_surface(item) for item in touched_surfaces],
        limit=_CAMPAIGN_IDEA_SAMPLE_SURFACE_LIMIT,
    )
    return (touched_surfaces, family_surfaces)


def _campaign_family_key(
    *,
    decision: str,
    family_hint: str,
    family_surfaces: list[str],
    touched_surfaces: list[str],
) -> tuple[str, str]:
    normalized_family_hint = str(family_hint or "").strip()
    if normalized_family_hint:
        return (
            f"{decision or 'unknown'}:{_campaign_structured_fingerprint({'hint': normalized_family_hint.lower()})[:12]}",
            "hint",
        )
    seed = {
        "decision": str(decision or "").strip().lower(),
        "family_surfaces": list(family_surfaces),
        "touched_surfaces": list(
            touched_surfaces[:_CAMPAIGN_IDEA_SAMPLE_SURFACE_LIMIT]
        ),
    }
    return (
        f"{decision or 'unknown'}:{_campaign_structured_fingerprint(seed)[:12]}",
        "heuristic",
    )


def _campaign_family_label(
    *,
    decision: str,
    family_hint: str,
    family_surfaces: list[str],
) -> str:
    normalized_family_hint = str(family_hint or "").strip()
    if normalized_family_hint:
        return normalized_family_hint
    if family_surfaces:
        return ", ".join(family_surfaces[:_CAMPAIGN_IDEA_RECENT_FAMILY_LIMIT])
    normalized_decision = str(decision or "").strip()
    return f"{normalized_decision} search" if normalized_decision else "unknown search"


def _campaign_thesis_text(
    *,
    decision: str,
    thesis_hint: str,
    family_surfaces: list[str],
    touched_surfaces: list[str],
) -> tuple[str, str]:
    normalized_thesis_hint = str(thesis_hint or "").strip()
    if normalized_thesis_hint:
        return (normalized_thesis_hint, "hint")
    normalized_decision = str(decision or "").strip() or "implementation"
    surface_basis = (
        family_surfaces or touched_surfaces[:_CAMPAIGN_IDEA_RECENT_FAMILY_LIMIT]
    )
    if surface_basis:
        return (
            f"{normalized_decision} search touching {', '.join(surface_basis[:_CAMPAIGN_IDEA_RECENT_FAMILY_LIMIT])}",
            "heuristic",
        )
    return (f"{normalized_decision} search", "heuristic")


def _campaign_next_idea_entry_id(idea_journal: dict[str, Any]) -> str:
    next_entry_seq = int(idea_journal.get("next_entry_seq", 1) or 1)
    return f"idea_{next_entry_seq:04d}"


def _campaign_merge_family_stats(
    idea_journal: dict[str, Any],
    entry: dict[str, Any],
) -> dict[str, Any]:
    family_key = str(entry.get("family_key", "")).strip()
    status = str(entry.get("status", "")).strip().lower()
    if not family_key or status not in _CAMPAIGN_IDEA_FINAL_STATUSES:
        return idea_journal
    family_stats = idea_journal.get("family_stats")
    if not isinstance(family_stats, dict):
        family_stats = {}
    stats = _normalize_campaign_family_stats_entry(family_stats.get(family_key))
    family_label = str(entry.get("family_label", "")).strip()
    if family_label:
        stats["family_label"] = family_label
    stats["first_seen_at"] = (
        stats["first_seen_at"]
        or str(entry.get("started_at", "")).strip()
        or str(entry.get("updated_at", "")).strip()
    )
    stats["last_seen_at"] = (
        str(entry.get("completed_at", "")).strip()
        or str(entry.get("updated_at", "")).strip()
        or stats["last_seen_at"]
    )
    counts = stats.get("counts")
    if not isinstance(counts, dict):
        counts = {item: 0 for item in _CAMPAIGN_IDEA_FINAL_STATUSES}
    counts[status] = int(counts.get(status, 0) or 0) + 1
    stats["counts"] = counts
    if bool(entry.get("near_miss", False)):
        stats["near_miss_count"] = int(stats.get("near_miss_count", 0) or 0) + 1
    stats["last_outcome"] = status
    stats["last_thesis"] = str(entry.get("thesis", "")).strip()
    existing_sample_surfaces = (
        list(stats.get("sample_surfaces", []))
        if isinstance(stats.get("sample_surfaces"), list)
        else []
    )
    entry_family_surfaces = (
        list(entry.get("family_surfaces", []))
        if isinstance(entry.get("family_surfaces"), list)
        else []
    )
    stats["sample_surfaces"] = _campaign_string_list(
        [*existing_sample_surfaces, *entry_family_surfaces],
        limit=_CAMPAIGN_IDEA_SAMPLE_SURFACE_LIMIT,
    )
    family_stats[family_key] = stats
    idea_journal["family_stats"] = family_stats
    return idea_journal


def _campaign_sync_active_idea_journal(
    repo_root: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    if not _campaign_has_active_candidate(normalized):
        return normalized

    candidate = _normalize_campaign_active_candidate(normalized.get("active_candidate"))
    idea_journal = _normalize_campaign_idea_journal(normalized.get("idea_journal"))
    entry_index, entry = _campaign_active_idea_entry_info(normalized)
    timestamp = _utc_now()
    if entry is None:
        entry = _campaign_default_idea_journal_entry()
        entry["entry_id"] = _campaign_next_idea_entry_id(idea_journal)
        entry["started_at"] = candidate["started_at"] or timestamp
        entry["updated_at"] = entry["started_at"]
        entry["decision"] = candidate["decision"]
        entry["champion_before_run_id"] = str(
            normalized.get("champion_run_id", "")
        ).strip()
        idea_journal["active_entry_id"] = entry["entry_id"]
        idea_journal["next_entry_seq"] = (
            int(idea_journal.get("next_entry_seq", 1) or 1) + 1
        )
        entries = idea_journal.get("entries")
        if not isinstance(entries, list):
            entries = []
        entries.append(entry)
        idea_journal["entries"] = entries
        candidate["journal_entry_id"] = entry["entry_id"]
        entry_index = len(entries) - 1
    else:
        candidate["journal_entry_id"] = str(entry.get("entry_id", "")).strip()

    touched_surfaces, family_surfaces = _campaign_derive_idea_entry_surfaces(
        repo_root,
        state,
        normalized,
    )
    family_key, family_source = _campaign_family_key(
        decision=candidate["decision"],
        family_hint=candidate["family_hint"],
        family_surfaces=family_surfaces,
        touched_surfaces=touched_surfaces,
    )
    thesis, thesis_source = _campaign_thesis_text(
        decision=candidate["decision"],
        thesis_hint=candidate["thesis_hint"],
        family_surfaces=family_surfaces,
        touched_surfaces=touched_surfaces,
    )
    run_ids = (
        list(entry.get("run_ids", [])) if isinstance(entry.get("run_ids"), list) else []
    )
    run_id = candidate["run_id"]
    if run_id and run_id not in run_ids:
        run_ids.append(run_id)

    entry["decision"] = candidate["decision"]
    entry["updated_at"] = timestamp
    entry["status"] = "active"
    entry["attempt_count"] = max(
        int(entry.get("attempt_count", 1) or 1),
        int(candidate.get("fix_attempts", 0) or 0) + 1,
        1,
    )
    entry["run_ids"] = _campaign_string_list(
        run_ids,
        limit=_CAMPAIGN_IDEA_SURFACE_LIMIT,
    )
    entry["thesis"] = thesis
    entry["thesis_source"] = thesis_source
    entry["family_key"] = family_key
    entry["family_label"] = _campaign_family_label(
        decision=candidate["decision"],
        family_hint=candidate["family_hint"],
        family_surfaces=family_surfaces,
    )
    entry["family_source"] = family_source
    entry["touched_surfaces"] = touched_surfaces
    entry["family_surfaces"] = family_surfaces
    if not str(entry.get("champion_before_run_id", "")).strip():
        entry["champion_before_run_id"] = str(
            normalized.get("champion_run_id", "")
        ).strip()

    entries = idea_journal.get("entries")
    if not isinstance(entries, list):
        entries = []
    entries[entry_index] = entry
    idea_journal["entries"] = entries
    idea_journal["active_entry_id"] = candidate["journal_entry_id"]
    normalized["active_candidate"] = candidate
    normalized["idea_journal"] = _campaign_trim_idea_journal_entries(idea_journal)
    return normalized


def _campaign_finalize_active_idea_journal(
    repo_root: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
    *,
    status: str,
    reason: str,
    near_miss: bool = False,
    champion_after_run_id: str = "",
    run_id: str = "",
) -> dict[str, Any]:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in _CAMPAIGN_IDEA_FINAL_STATUSES:
        raise CampaignError(
            f"unsupported idea journal final status '{normalized_status or status}'"
        )
    normalized = _normalize_campaign(campaign)
    if not _campaign_has_active_candidate(normalized):
        fallback_decision = str(state.get("stage", "")).strip().lower()
        if fallback_decision not in _CAMPAIGN_DECISION_STAGES:
            fallback_decision = "implementation"
        fallback_run_id = str(
            run_id or _campaign_candidate_run_id_from_state(state, normalized)
        ).strip()
        normalized = _campaign_set_active_candidate(
            normalized,
            decision=fallback_decision,
            run_id=fallback_run_id,
        )
    normalized = _campaign_sync_active_idea_journal(repo_root, state, normalized)
    idea_journal = _normalize_campaign_idea_journal(normalized.get("idea_journal"))
    candidate = _normalize_campaign_active_candidate(normalized.get("active_candidate"))
    entry_index = _campaign_find_idea_entry_index(
        idea_journal,
        candidate["journal_entry_id"] or idea_journal.get("active_entry_id", ""),
    )
    if entry_index < 0:
        return normalized
    entries = idea_journal.get("entries")
    if not isinstance(entries, list):
        return normalized
    entry = entries[entry_index]
    if not isinstance(entry, dict):
        return normalized

    timestamp = _utc_now()
    finalized = dict(entry)
    finalized["updated_at"] = timestamp
    finalized["completed_at"] = timestamp
    finalized["status"] = normalized_status
    finalized["near_miss"] = (
        bool(near_miss) if normalized_status == "discard" else False
    )
    finalized["outcome_reason"] = str(reason or "").strip()
    effective_run_id = str(run_id or candidate["run_id"]).strip()
    finalized_run_ids = (
        list(finalized.get("run_ids", []))
        if isinstance(finalized.get("run_ids"), list)
        else []
    )
    finalized["run_ids"] = _campaign_string_list(
        [*finalized_run_ids, effective_run_id],
        limit=_CAMPAIGN_IDEA_SURFACE_LIMIT,
    )
    finalized["champion_after_run_id"] = (
        str(champion_after_run_id or "").strip()
        or str(finalized.get("champion_after_run_id", "")).strip()
        or str(normalized.get("champion_run_id", "")).strip()
    )
    entries[entry_index] = finalized
    idea_journal["entries"] = entries
    idea_journal["active_entry_id"] = ""
    idea_journal = _campaign_merge_family_stats(idea_journal, finalized)
    normalized["idea_journal"] = _campaign_trim_idea_journal_entries(idea_journal)
    return normalized


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


def _campaign_extract_parser_fingerprint(
    repo_root: Path,
    *,
    iteration_dir: Path,
    design_payload: dict[str, Any],
) -> str:
    parser_capabilities_path = iteration_dir / "parser_capabilities.json"
    parser_capabilities_rel = (
        _campaign_iteration_relpath(repo_root, parser_capabilities_path)
        if parser_capabilities_path.exists()
        else _campaign_iteration_relpath(repo_root, parser_capabilities_path)
    )
    payload = {
        "extract_parser": design_payload.get("extract_parser", {}),
        "parser_capabilities_path": parser_capabilities_rel,
        "parser_capabilities_fingerprint": _path_fingerprint(
            repo_root,
            parser_capabilities_rel,
        ),
    }
    return _campaign_structured_fingerprint(payload)


def _campaign_evaluator_fingerprint(repo_root: Path) -> str:
    payload = [
        {
            "path": rel_path,
            "fingerprint": _path_fingerprint(repo_root, rel_path),
        }
        for rel_path in _CAMPAIGN_EVALUATOR_CONTRACT_PATHS
    ]
    return _campaign_structured_fingerprint(payload)


def _campaign_remote_profile_contract(
    repo_root: Path,
    *,
    design_payload: dict[str, Any],
) -> tuple[str, str, str]:
    launch_mode = _resolve_launch_mode(design_payload)
    if launch_mode != "slurm":
        return ("none", "none", "none")

    remote_profiles_rel = ".autolab/remote_profiles.yaml"
    remote_profiles_fingerprint = _path_fingerprint(repo_root, remote_profiles_rel)
    try:
        from autolab.remote_profiles import resolve_remote_profile

        profile = resolve_remote_profile(
            repo_root,
            host_mode=_detect_priority_host_mode(),
        )
    except Exception:
        return ("none", "none", remote_profiles_fingerprint)

    return (
        str(getattr(profile, "name", "")).strip() or "none",
        str(getattr(profile, "mode", "")).strip() or "none",
        remote_profiles_fingerprint,
    )


def _campaign_capture_lock_contract(
    repo_root: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
) -> dict[str, str]:
    normalized = _normalize_campaign(campaign)
    if _campaign_lock_mode(normalized) == "none":
        return _campaign_default_lock_contract()

    design_payload, iteration_dir = _load_design_payload_for_state(repo_root, state)
    hypothesis_path = iteration_dir / "hypothesis.md"
    if not hypothesis_path.exists():
        raise CampaignError(
            f"campaign lock requires hypothesis.md at {hypothesis_path}"
        )
    design_path = iteration_dir / "design.yaml"
    hypothesis_rel = _campaign_iteration_relpath(repo_root, hypothesis_path)
    design_rel = _campaign_iteration_relpath(repo_root, design_path)
    hypothesis_fingerprint = _path_fingerprint(repo_root, hypothesis_rel)
    design_fingerprint = _path_fingerprint(repo_root, design_rel)
    if hypothesis_fingerprint == "<missing>":
        raise CampaignError(f"campaign lock requires readable {hypothesis_path}")
    if design_fingerprint == "<missing>":
        raise CampaignError(f"campaign lock requires readable {design_path}")

    remote_profile_name, remote_profile_mode, remote_profile_config_fingerprint = (
        _campaign_remote_profile_contract(
            repo_root,
            design_payload=design_payload,
        )
    )
    return {
        "captured_at": _utc_now(),
        "hypothesis_path": hypothesis_rel,
        "hypothesis_fingerprint": hypothesis_fingerprint,
        "design_path": design_rel,
        "design_fingerprint": design_fingerprint,
        "extract_parser_fingerprint": _campaign_extract_parser_fingerprint(
            repo_root,
            iteration_dir=iteration_dir,
            design_payload=design_payload,
        ),
        "evaluator_fingerprint": _campaign_evaluator_fingerprint(repo_root),
        "remote_profile_name": remote_profile_name,
        "remote_profile_mode": remote_profile_mode,
        "remote_profile_config_fingerprint": remote_profile_config_fingerprint,
    }


def _campaign_lock_overview(
    repo_root: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    mode = _campaign_lock_mode(normalized)
    overview = {
        "lock_mode": mode,
        "design_locked": bool(normalized.get("design_locked", False)),
        "harness_locked": bool(normalized.get("harness_locked", False)),
        "lock_ok": True,
        "lock_drift": "",
        "lock_summary": "campaign lock mode inactive",
    }
    if mode == "none":
        return overview

    missing = _campaign_missing_lock_fields(normalized)
    if missing:
        drift = "campaign lock contract is missing required fields: " + ", ".join(
            sorted(missing)
        )
        overview["lock_ok"] = False
        overview["lock_drift"] = drift
        overview["lock_summary"] = drift
        return overview

    contract = _normalize_campaign_lock_contract(normalized.get("lock_contract"))
    try:
        current = _campaign_capture_lock_contract(repo_root, state, normalized)
    except CampaignError as exc:
        drift = str(exc)
        overview["lock_ok"] = False
        overview["lock_drift"] = drift
        overview["lock_summary"] = f"{mode} lock drift: {drift}"
        return overview

    comparisons = [
        (
            "hypothesis_path",
            "hypothesis binding changed",
        ),
        (
            "hypothesis_fingerprint",
            "hypothesis.md changed",
        ),
        (
            "design_path",
            "design binding changed",
        ),
        (
            "design_fingerprint",
            "design.yaml changed",
        ),
    ]
    if mode == "harness":
        comparisons.extend(
            [
                (
                    "extract_parser_fingerprint",
                    "extract parser contract changed",
                ),
                (
                    "evaluator_fingerprint",
                    "evaluator contract changed",
                ),
                (
                    "remote_profile_name",
                    "remote profile selection changed",
                ),
                (
                    "remote_profile_mode",
                    "remote profile mode changed",
                ),
                (
                    "remote_profile_config_fingerprint",
                    "remote profile config changed",
                ),
            ]
        )

    for field, reason in comparisons:
        if str(contract.get(field, "")).strip() == str(current.get(field, "")).strip():
            continue
        overview["lock_ok"] = False
        overview["lock_drift"] = reason
        overview["lock_summary"] = f"{mode} lock drift: {reason}"
        return overview

    if mode == "design":
        overview["lock_summary"] = "design lock active: hypothesis/design unchanged"
    else:
        overview["lock_summary"] = (
            "harness lock active: design, parser, evaluator, and remote profile unchanged"
        )
    return overview


def _campaign_locked_auto_decision(campaign: dict[str, Any]) -> str:
    normalized = _normalize_campaign(campaign)
    if _campaign_lock_mode(normalized) == "none":
        return ""
    return "implementation"


def _campaign_allowed_decisions(campaign: dict[str, Any]) -> tuple[str, ...]:
    normalized = _normalize_campaign(campaign)
    if _campaign_lock_mode(normalized) == "none":
        return ("hypothesis", "design", "implementation", "stop", "human_review")
    return ("implementation", "design", "human_review", "stop")


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
    lock_modes: list[str] | tuple[str, ...] = (),
) -> dict[str, Any]:
    normalized_label = str(label).strip()
    if not normalized_label:
        raise CampaignError("campaign label must be non-empty")
    normalized_scope_kind = str(scope_kind).strip().lower()
    if normalized_scope_kind not in {"experiment", "project_wide"}:
        raise CampaignError("campaign scope must be 'experiment' or 'project_wide'")
    normalized_lock_modes = {
        str(item).strip().lower() for item in lock_modes if str(item).strip()
    }
    invalid_lock_modes = sorted(
        item for item in normalized_lock_modes if item not in {"design", "harness"}
    )
    if invalid_lock_modes:
        raise CampaignError(
            "campaign lock mode must be 'design' or 'harness' "
            f"(got {', '.join(invalid_lock_modes)})"
        )
    harness_locked = "harness" in normalized_lock_modes
    design_locked = harness_locked or "design" in normalized_lock_modes

    objective_metric, objective_mode = _resolve_campaign_objective(repo_root, state)
    champion_run_id, champion_revision_label = _resolve_campaign_baseline(
        repo_root,
        state,
        objective_metric=objective_metric,
    )
    payload = {
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
        "design_locked": design_locked,
        "harness_locked": harness_locked,
        "lock_contract": _campaign_default_lock_contract(),
        "champion_run_id": champion_run_id,
        "champion_revision_label": champion_revision_label,
        "no_improvement_streak": 0,
        "crash_streak": 0,
        "started_at": _utc_now(),
        "last_oracle_at": "",
        "oracle_feedback": [],
        "active_candidate": _campaign_default_active_candidate(),
        "last_governance_event": _campaign_default_last_governance_event(),
        "idea_journal": _campaign_default_idea_journal(),
    }
    if design_locked:
        payload["lock_contract"] = _campaign_capture_lock_contract(
            repo_root,
            state,
            payload,
        )
    return payload


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


def _campaign_entry_family_display(entry: dict[str, Any]) -> str:
    family_label = str(entry.get("family_label", "")).strip()
    if family_label:
        return family_label
    family_key = str(entry.get("family_key", "")).strip()
    if family_key:
        return family_key
    decision = str(entry.get("decision", "")).strip()
    return f"{decision} search" if decision else ""


def _campaign_recent_family_labels(
    entries: list[dict[str, Any]],
    *,
    predicate,
    limit: int = _CAMPAIGN_IDEA_RECENT_FAMILY_LIMIT,
) -> list[str]:
    labels: list[str] = []
    seen: set[str] = set()
    for entry in reversed(entries):
        if not predicate(entry):
            continue
        label = _campaign_entry_family_display(entry)
        if not label or label in seen:
            continue
        seen.add(label)
        labels.append(label)
        if len(labels) >= limit:
            break
    return labels


def _campaign_novelty_summary(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_campaign(payload)
    idea_journal = _normalize_campaign_idea_journal(normalized.get("idea_journal"))
    entries = [
        dict(entry)
        for entry in idea_journal.get("entries", [])
        if isinstance(entry, dict)
    ]
    active_entry_id = str(idea_journal.get("active_entry_id", "")).strip()
    active_entry = next(
        (
            dict(entry)
            for entry in entries
            if str(entry.get("entry_id", "")).strip() == active_entry_id
        ),
        {},
    )
    completed_entries = [
        dict(entry)
        for entry in entries
        if str(entry.get("status", "")).strip().lower() in _CAMPAIGN_IDEA_FINAL_STATUSES
    ]
    last_completed = dict(completed_entries[-1]) if completed_entries else {}

    active_family_key = str(active_entry.get("family_key", "")).strip()
    if not active_family_key and last_completed:
        active_family_key = str(last_completed.get("family_key", "")).strip()
    same_family_streak = 0
    if active_family_key:
        for entry in reversed(entries):
            if str(entry.get("family_key", "")).strip() != active_family_key:
                break
            same_family_streak += 1

    recent_entries = [
        dict(entry) for entry in entries[-_CAMPAIGN_IDEA_RECENT_ENTRY_LIMIT:]
    ]
    return {
        "entry_count": len(entries),
        "family_count": len(
            [
                key
                for key in idea_journal.get("family_stats", {}).keys()
                if str(key).strip()
            ]
        )
        if isinstance(idea_journal.get("family_stats"), dict)
        else 0,
        "active_family": _campaign_entry_family_display(active_entry),
        "active_thesis": str(active_entry.get("thesis", "")).strip(),
        "same_family_streak": same_family_streak,
        "last_completed_status": str(last_completed.get("status", "")).strip(),
        "last_completed_family": _campaign_entry_family_display(last_completed),
        "recent_failed_families": _campaign_recent_family_labels(
            completed_entries,
            predicate=lambda entry: (
                str(entry.get("status", "")).strip().lower() in {"discard", "crash"}
            ),
        ),
        "recent_near_miss_families": _campaign_recent_family_labels(
            completed_entries,
            predicate=lambda entry: bool(entry.get("near_miss", False)),
        ),
        "recent_entries": recent_entries,
    }


def _campaign_novelty_summary_text(payload: dict[str, Any]) -> str:
    summary = _campaign_novelty_summary(payload)
    parts = [
        f"same_family_streak={int(summary.get('same_family_streak', 0) or 0)}",
        f"active_family={str(summary.get('active_family', '')).strip() or 'none'}",
        f"active_thesis={str(summary.get('active_thesis', '')).strip() or 'none'}",
        f"last_completed={str(summary.get('last_completed_status', '')).strip() or 'none'}:{str(summary.get('last_completed_family', '')).strip() or 'none'}",
        "recent_failed_families="
        + (
            ", ".join(summary.get("recent_failed_families", []))
            if isinstance(summary.get("recent_failed_families"), list)
            and summary.get("recent_failed_families")
            else "none"
        ),
        "recent_near_miss_families="
        + (
            ", ".join(summary.get("recent_near_miss_families", []))
            if isinstance(summary.get("recent_near_miss_families"), list)
            and summary.get("recent_near_miss_families")
            else "none"
        ),
    ]
    return "; ".join(parts)


def _campaign_summary(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_campaign(payload)
    novelty_summary = _campaign_novelty_summary(normalized)
    summary = {
        "campaign_id": normalized["campaign_id"],
        "label": normalized["label"],
        "scope_kind": normalized["scope_kind"],
        "iteration_id": normalized["iteration_id"],
        "objective_metric": normalized["objective_metric"],
        "objective_mode": normalized["objective_mode"],
        "status": normalized["status"],
        "design_locked": normalized["design_locked"],
        "harness_locked": normalized["harness_locked"],
        "lock_mode": _campaign_lock_mode(normalized),
        "champion_run_id": normalized["champion_run_id"],
        "champion_revision_label": normalized["champion_revision_label"],
        "no_improvement_streak": normalized["no_improvement_streak"],
        "crash_streak": normalized["crash_streak"],
        "started_at": normalized["started_at"],
        "last_oracle_at": normalized["last_oracle_at"],
        "oracle_feedback_count": len(
            _normalize_campaign_oracle_feedback(normalized.get("oracle_feedback"))
        ),
        "resumable": _campaign_is_resumable(normalized),
        "idea_journal_entry_count": int(novelty_summary.get("entry_count", 0) or 0),
        "idea_journal_family_count": int(novelty_summary.get("family_count", 0) or 0),
        "idea_journal_active_family": str(
            novelty_summary.get("active_family", "")
        ).strip(),
        "idea_journal_active_thesis": str(
            novelty_summary.get("active_thesis", "")
        ).strip(),
        "idea_journal_same_family_streak": int(
            novelty_summary.get("same_family_streak", 0) or 0
        ),
        "idea_journal_last_completed_status": str(
            novelty_summary.get("last_completed_status", "")
        ).strip(),
        "idea_journal_last_completed_family": str(
            novelty_summary.get("last_completed_family", "")
        ).strip(),
        "idea_journal_recent_failed_families": list(
            novelty_summary.get("recent_failed_families", [])
        )
        if isinstance(novelty_summary.get("recent_failed_families"), list)
        else [],
        "idea_journal_recent_near_miss_families": list(
            novelty_summary.get("recent_near_miss_families", [])
        )
        if isinstance(novelty_summary.get("recent_near_miss_families"), list)
        else [],
    }
    candidate = _normalize_campaign_active_candidate(normalized.get("active_candidate"))
    last_event = _normalize_campaign_last_governance_event(
        normalized.get("last_governance_event")
    )
    summary.update(
        {
            "active_candidate_decision": candidate["decision"],
            "active_candidate_started_at": candidate["started_at"],
            "active_candidate_run_id": candidate["run_id"],
            "active_candidate_fix_attempts": candidate["fix_attempts"],
            "active_candidate_timeout_reference_seconds": candidate[
                "timeout_reference_seconds"
            ],
            "last_governance_event_at": last_event["at"],
            "last_governance_event_category": last_event["category"],
            "last_governance_event_run_id": last_event["run_id"],
            "last_governance_event_reason": last_event["reason"],
        }
    )
    return summary


def _campaign_summary_with_governance(
    repo_root: Path,
    payload: dict[str, Any],
) -> dict[str, Any]:
    summary = _campaign_summary(payload)
    governance = _load_campaign_governance_config(repo_root)
    summary.update(
        {
            "max_fix_attempts_per_idea": governance.max_fix_attempts_per_idea,
            "max_timeout_factor": governance.max_timeout_factor,
            "max_no_improvement_streak": governance.max_no_improvement_streak,
            "max_crash_streak_before_rethink": (
                governance.max_crash_streak_before_rethink
            ),
        }
    )
    return summary


def _campaign_set_last_governance_event(
    campaign: dict[str, Any],
    *,
    category: str,
    reason: str,
    run_id: str = "",
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    normalized["last_governance_event"] = {
        "at": _utc_now(),
        "category": str(category).strip(),
        "run_id": str(run_id).strip(),
        "reason": str(reason).strip(),
    }
    return normalized


def _campaign_reset_active_candidate(
    campaign: dict[str, Any],
    *,
    repo_root: Path | None = None,
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    normalized["active_candidate"] = _campaign_default_active_candidate()
    if repo_root is not None:
        oracle_state = load_oracle_state(repo_root)
        if str(oracle_state.get("disfavored_family", "")).strip():
            oracle_state["disfavored_family"] = ""
            write_oracle_state(repo_root, oracle_state)
    return normalized


def _campaign_set_active_candidate(
    campaign: dict[str, Any],
    *,
    decision: str,
    started_at: str = "",
    run_id: str = "",
    fix_attempts: int = 0,
    timeout_reference_seconds: float = 0.0,
    journal_entry_id: str = "",
    family_hint: str = "",
    thesis_hint: str = "",
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    candidate = _campaign_default_active_candidate()
    candidate["decision"] = str(decision).strip().lower()
    candidate["started_at"] = str(started_at or _utc_now()).strip()
    candidate["run_id"] = str(run_id).strip()
    candidate["fix_attempts"] = max(0, int(fix_attempts))
    candidate["timeout_reference_seconds"] = max(
        0.0, float(timeout_reference_seconds or 0.0)
    )
    candidate["journal_entry_id"] = str(journal_entry_id).strip()
    candidate["family_hint"] = str(family_hint).strip()
    candidate["thesis_hint"] = str(thesis_hint).strip()
    normalized["active_candidate"] = candidate
    return normalized


def _campaign_bump_active_candidate_fix_attempts(
    campaign: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    candidate = _normalize_campaign_active_candidate(normalized.get("active_candidate"))
    candidate["fix_attempts"] = int(candidate.get("fix_attempts", 0) or 0) + 1
    normalized["active_candidate"] = candidate
    return normalized


def _mark_campaign_oracle_exported(repo_root: Path) -> dict[str, Any] | None:
    campaign = _load_campaign(repo_root)
    if campaign is None:
        return None
    campaign["last_oracle_at"] = _utc_now()
    _write_campaign(repo_root, campaign)
    return campaign


def _append_campaign_oracle_feedback(
    repo_root: Path,
    *,
    source: str,
    summary: str,
    detail: str = "",
    signal: str = "",
) -> dict[str, Any] | None:
    campaign = _load_campaign(repo_root)
    if campaign is None:
        return None

    normalized = _normalize_campaign(campaign)
    normalized_signal = str(signal).strip().lower()
    if normalized_signal not in _CAMPAIGN_ORACLE_FEEDBACK_SIGNALS:
        normalized_signal = "none"
    entry = _normalize_campaign_oracle_feedback_entry(
        {
            "applied_at": _utc_now(),
            "source": source,
            "summary": summary,
            "detail": detail,
            "signal": normalized_signal,
        }
    )
    if not entry["summary"] and not entry["detail"]:
        return normalized

    existing_feedback = _normalize_campaign_oracle_feedback(
        normalized.get("oracle_feedback")
    )
    dedupe_key = (
        entry["source"],
        entry["summary"],
        entry["detail"],
        entry["signal"],
    )
    seen = {
        (
            str(item.get("source", "")).strip(),
            str(item.get("summary", "")).strip(),
            str(item.get("detail", "")).strip(),
            str(item.get("signal", "")).strip(),
        )
        for item in existing_feedback
        if isinstance(item, dict)
    }
    if dedupe_key not in seen:
        existing_feedback.append(entry)
        normalized["oracle_feedback"] = existing_feedback

    if normalized_signal == "rethink":
        normalized["status"] = "needs_rethink"
    elif normalized_signal == "stop":
        normalized["status"] = "stop_requested"

    _write_campaign(repo_root, normalized)
    return normalized


def _campaign_scope_root(repo_root: Path, campaign: dict[str, Any]) -> Path:
    normalized = _normalize_campaign(campaign)
    if normalized["scope_kind"] == "project_wide":
        try:
            return _resolve_project_wide_root(repo_root)
        except Exception as exc:
            raise CampaignError(
                f"campaign results require a valid project-wide scope root: {exc}"
            ) from exc
    iteration_dir, _iteration_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=normalized["iteration_id"],
        require_exists=False,
    )
    return iteration_dir


def _campaign_results_tsv_path(repo_root: Path, campaign: dict[str, Any]) -> Path:
    return _campaign_scope_root(repo_root, campaign) / _CAMPAIGN_RESULTS_TSV_FILENAME


def _campaign_results_markdown_path(repo_root: Path, campaign: dict[str, Any]) -> Path:
    return _campaign_scope_root(repo_root, campaign) / _CAMPAIGN_RESULTS_MD_FILENAME


def _campaign_iteration_dir_for_results(
    repo_root: Path, campaign: dict[str, Any]
) -> Path:
    normalized = _normalize_campaign(campaign)
    if normalized["scope_kind"] == "experiment":
        iteration_dir, _iteration_type = _resolve_iteration_directory(
            repo_root,
            iteration_id=normalized["iteration_id"],
            require_exists=False,
        )
        return iteration_dir

    champion_run_id = normalized["champion_run_id"]
    candidates: list[Path] = []
    seen: set[str] = set()
    for run_dir in repo_root.glob(f"experiments/*/*/runs/{champion_run_id}"):
        if not run_dir.is_dir():
            continue
        iteration_dir = run_dir.parent.parent
        key = iteration_dir.resolve(strict=False).as_posix()
        if key in seen:
            continue
        seen.add(key)
        candidates.append(iteration_dir)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise CampaignError(
            "campaign results could not resolve a baseline iteration for "
            f"champion run_id={champion_run_id}"
        )
    candidate_text = ", ".join(
        _campaign_iteration_relpath(repo_root, item) for item in candidates
    )
    raise CampaignError(
        "campaign results found multiple possible baseline iterations for "
        f"champion run_id={champion_run_id}: {candidate_text}"
    )


def _campaign_results_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in _CAMPAIGN_RESULT_STATUSES}
    for row in rows:
        status = str(row.get("status", "")).strip().lower()
        if status in counts:
            counts[status] += 1
    return counts


def _campaign_read_results_tsv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    if not lines:
        return []
    header = lines[0].split("\t")
    expected = [
        "revision_label",
        "run_id",
        "primary_metric",
        "memory_gb",
        "status",
        "summary",
    ]
    if header != expected:
        return []
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        values = line.split("\t")
        if len(values) < len(expected):
            values.extend([""] * (len(expected) - len(values)))
        rows.append(dict(zip(expected, values[: len(expected)], strict=False)))
    return rows


def _campaign_results_overview(
    repo_root: Path,
    campaign: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    try:
        tsv_path = _campaign_results_tsv_path(repo_root, normalized)
        md_path = _campaign_results_markdown_path(repo_root, normalized)
    except CampaignError as exc:
        return {
            "results_tsv_path": "",
            "results_tsv_exists": False,
            "results_md_path": "",
            "results_md_exists": False,
            "counts": {status: 0 for status in _CAMPAIGN_RESULT_STATUSES},
            "diagnostic": str(exc),
        }

    rows = _campaign_read_results_tsv(tsv_path)
    return {
        "results_tsv_path": _campaign_iteration_relpath(repo_root, tsv_path),
        "results_tsv_exists": tsv_path.exists(),
        "results_md_path": _campaign_iteration_relpath(repo_root, md_path),
        "results_md_exists": md_path.exists(),
        "counts": _campaign_results_counts(rows),
        "diagnostic": "",
    }


def _campaign_morning_report_path(repo_root: Path, campaign: dict[str, Any]) -> Path:
    return _campaign_scope_root(repo_root, campaign) / _CAMPAIGN_MORNING_REPORT_FILENAME


def _campaign_timestamp_as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    return _parse_utc(text)


def _campaign_timestamp_text(value: Any) -> str:
    timestamp = _campaign_timestamp_as_datetime(value)
    if timestamp is None:
        return ""
    return timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _campaign_row_primary_metric_value(row: dict[str, Any]) -> float | None:
    text = str(row.get("primary_metric", "")).strip()
    if not text:
        return None
    try:
        return float(text)
    except Exception:
        return None


def _campaign_metric_delta(
    candidate_value: float | None,
    baseline_value: float | None,
    objective_mode: str,
) -> float | None:
    if candidate_value is None or baseline_value is None:
        return None
    mode = str(objective_mode or "").strip().lower()
    if mode == "minimize":
        return baseline_value - candidate_value
    return candidate_value - baseline_value


def _campaign_candidate_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in _CAMPAIGN_RESULT_STATUSES}
    for row in rows:
        if bool(row.get("is_baseline", False)):
            continue
        status = str(row.get("status", "")).strip().lower()
        if status in counts:
            counts[status] += 1
    return counts


def _campaign_recent_rows(
    campaign: dict[str, Any],
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    started_at = _campaign_timestamp_as_datetime(campaign.get("started_at"))
    now = _campaign_timestamp_as_datetime(_utc_now())
    if started_at is None or now is None:
        return (list(rows), "")
    cutoff = max(started_at, now - timedelta(hours=24))
    filtered = [
        row
        for row in rows
        if (
            (timestamp := _campaign_timestamp_as_datetime(row.get("timestamp")))
            is not None
            and timestamp >= cutoff
        )
    ]
    if filtered:
        return (filtered, _campaign_timestamp_text(cutoff))
    return (list(rows), _campaign_timestamp_text(cutoff))


def _campaign_failure_themes(
    campaign: dict[str, Any],
    rows: list[dict[str, Any]],
) -> list[str]:
    themes: list[str] = []
    novelty_summary = _campaign_novelty_summary(campaign)
    recent_failed_families = novelty_summary.get("recent_failed_families")
    if isinstance(recent_failed_families, list):
        for family in recent_failed_families:
            text = str(family).strip()
            if text:
                themes.append(f"failed family: {text}")
    recent_near_miss_families = novelty_summary.get("recent_near_miss_families")
    if isinstance(recent_near_miss_families, list):
        for family in recent_near_miss_families:
            text = str(family).strip()
            if text:
                themes.append(f"near miss: {text}")
    for row in rows:
        if bool(row.get("is_baseline", False)):
            continue
        status = str(row.get("status", "")).strip().lower()
        if status not in {"discard", "crash", "partial"}:
            continue
        summary = _campaign_compact_text(row.get("summary", ""))
        if not summary:
            continue
        themes.append(f"{status}: {summary}")
    deduped: list[str] = []
    seen: set[str] = set()
    for theme in themes:
        normalized = _campaign_compact_text(theme)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
        if len(deduped) >= 5:
            break
    return deduped


def _campaign_report_next_action(
    handoff_payload: dict[str, Any],
    *,
    oracle_required: bool,
    oracle_reason: str,
) -> tuple[str, str, str, list[str]]:
    continuation_packet = handoff_payload.get("continuation_packet")
    if not isinstance(continuation_packet, dict):
        continuation_packet = {}
    next_action = continuation_packet.get("next_action")
    if not isinstance(next_action, dict):
        next_action = {}
    safe_resume = handoff_payload.get("safe_resume_point")
    if not isinstance(safe_resume, dict):
        safe_resume = {}
    preconditions = [
        str(item).strip()
        for item in safe_resume.get("preconditions", [])
        if str(item).strip()
    ]
    if oracle_required:
        return ("autolab oracle", oracle_reason, "blocked", preconditions)
    command = (
        str(next_action.get("recommended_command", "")).strip()
        or str(safe_resume.get("command", "")).strip()
    )
    reason = (
        str(next_action.get("reason", "")).strip() or "No recommendation available."
    )
    status = (
        str(next_action.get("safe_status", "")).strip()
        or str(safe_resume.get("status", "blocked")).strip()
        or "blocked"
    )
    return (command, reason, status, preconditions)


def _campaign_build_morning_report_payload(
    repo_root: Path,
    campaign: dict[str, Any],
    *,
    results_payload: dict[str, Any] | None = None,
    handoff_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    if results_payload is None:
        results_payload = _refresh_campaign_results(repo_root, normalized)
    if handoff_payload is None:
        handoff_payload = {}
    rows_raw = results_payload.get("rows")
    rows = [dict(item) for item in rows_raw] if isinstance(rows_raw, list) else []
    baseline_run_id = str(results_payload.get("baseline_run_id", "")).strip()
    if not baseline_run_id:
        baseline_run_id = str(normalized.get("champion_run_id", "")).strip()
    baseline_row = next(
        (
            row
            for row in rows
            if bool(row.get("is_baseline", False))
            or str(row.get("run_id", "")).strip() == baseline_run_id
        ),
        {},
    )
    champion_run_id = str(normalized.get("champion_run_id", "")).strip()
    champion_row = next(
        (row for row in rows if str(row.get("run_id", "")).strip() == champion_run_id),
        baseline_row,
    )
    candidate_rows = [row for row in rows if not bool(row.get("is_baseline", False))]
    recent_rows, window_started_at = _campaign_recent_rows(normalized, candidate_rows)
    baseline_metric = _campaign_row_primary_metric_value(baseline_row)
    objective_mode = str(normalized.get("objective_mode", "")).strip().lower()
    best_delta: float | None = None
    best_delta_row: dict[str, Any] = {}
    for row in recent_rows:
        delta = _campaign_metric_delta(
            _campaign_row_primary_metric_value(row),
            baseline_metric,
            objective_mode,
        )
        if delta is None:
            continue
        if best_delta is None or delta > best_delta:
            best_delta = delta
            best_delta_row = row
    if best_delta is not None and best_delta <= 0:
        best_delta = None
        best_delta_row = {}
    last_event = _normalize_campaign_last_governance_event(
        normalized.get("last_governance_event")
    )
    last_oracle_at = _campaign_timestamp_as_datetime(normalized.get("last_oracle_at"))
    last_event_at = _campaign_timestamp_as_datetime(last_event.get("at"))
    oracle_required = str(normalized.get("status", "")).strip() == "needs_rethink"
    oracle_reason = "campaign marked needs_rethink"
    category = str(last_event.get("category", "")).strip()
    if category in {"crash_rethink", "stagnation_rethink"} and (
        last_event_at is not None
        and (last_oracle_at is None or last_oracle_at < last_event_at)
    ):
        oracle_required = True
        oracle_reason = str(last_event.get("reason", "")).strip() or category
    pending_human_decisions = [
        str(item).strip()
        for item in handoff_payload.get("pending_human_decisions", [])
        if str(item).strip()
    ]
    blocking_failures = [
        str(item).strip()
        for item in handoff_payload.get("blocking_failures", [])
        if str(item).strip()
    ]
    current_stage = str(
        handoff_payload.get("current_stage", handoff_payload.get("stage", ""))
    ).strip()
    pending_review = bool(pending_human_decisions) or current_stage == "human_review"
    next_command, next_reason, safe_status, preconditions = (
        _campaign_report_next_action(
            handoff_payload,
            oracle_required=oracle_required,
            oracle_reason=oracle_reason,
        )
    )
    return {
        "campaign": _campaign_summary(normalized),
        "results_tsv_path": _campaign_iteration_relpath(
            repo_root, Path(results_payload.get("results_tsv_path", ""))
        )
        if results_payload.get("results_tsv_path")
        else "",
        "results_md_path": _campaign_iteration_relpath(
            repo_root, Path(results_payload.get("results_md_path", ""))
        )
        if results_payload.get("results_md_path")
        else "",
        "morning_report_path": _campaign_iteration_relpath(
            repo_root, _campaign_morning_report_path(repo_root, normalized)
        ),
        "oracle_path": _campaign_iteration_relpath(
            repo_root, _campaign_scope_root(repo_root, normalized) / "oracle.md"
        ),
        "baseline_run_id": baseline_run_id,
        "baseline_row": baseline_row,
        "champion_row": champion_row,
        "candidate_counts": _campaign_candidate_counts(candidate_rows),
        "recent_candidate_counts": _campaign_candidate_counts(recent_rows),
        "recent_window_started_at": window_started_at,
        "candidate_total": len(candidate_rows),
        "recent_candidate_total": len(recent_rows),
        "best_delta": best_delta,
        "best_delta_row": best_delta_row,
        "failure_themes": _campaign_failure_themes(normalized, recent_rows),
        "oracle_required": oracle_required,
        "oracle_reason": oracle_reason,
        "pending_review": pending_review,
        "pending_human_decisions": pending_human_decisions,
        "blocking_failures": blocking_failures,
        "recommended_command": next_command,
        "recommended_reason": next_reason,
        "safe_status": safe_status,
        "preconditions": preconditions,
    }


def _campaign_render_morning_report(
    repo_root: Path,
    campaign: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    normalized = _normalize_campaign(campaign)
    summary = payload.get("campaign")
    if not isinstance(summary, dict):
        summary = _campaign_summary(normalized)
    champion_row = payload.get("champion_row")
    if not isinstance(champion_row, dict):
        champion_row = {}
    baseline_row = payload.get("baseline_row")
    if not isinstance(baseline_row, dict):
        baseline_row = {}
    counts = payload.get("candidate_counts")
    if not isinstance(counts, dict):
        counts = {status: 0 for status in _CAMPAIGN_RESULT_STATUSES}
    recent_counts = payload.get("recent_candidate_counts")
    if not isinstance(recent_counts, dict):
        recent_counts = counts
    best_delta = payload.get("best_delta")
    best_delta_text = "none"
    if isinstance(best_delta, (int, float)):
        best_delta_row = payload.get("best_delta_row")
        if not isinstance(best_delta_row, dict):
            best_delta_row = {}
        best_delta_text = (
            f"{best_delta:+.6g} via "
            f"{str(best_delta_row.get('run_id', '')).strip() or 'unknown'} "
            f"({str(best_delta_row.get('status', '')).strip() or 'unknown'})"
        )
    failure_themes = payload.get("failure_themes")
    if not isinstance(failure_themes, list):
        failure_themes = []
    pending_decisions = payload.get("pending_human_decisions")
    if not isinstance(pending_decisions, list):
        pending_decisions = []
    blocking_failures = payload.get("blocking_failures")
    if not isinstance(blocking_failures, list):
        blocking_failures = []
    preconditions = payload.get("preconditions")
    if not isinstance(preconditions, list):
        preconditions = []
    lines = [
        "# Campaign Morning Report",
        "",
        f"- generated_at: `{_utc_now()}`",
        f"- campaign_id: `{summary.get('campaign_id', '')}`",
        f"- label: `{summary.get('label', '')}`",
        f"- scope_kind: `{summary.get('scope_kind', '')}`",
        f"- status: `{summary.get('status', '')}`",
        f"- objective_metric: `{summary.get('objective_metric', '')}`",
        f"- objective_mode: `{summary.get('objective_mode', '')}`",
        f"- report_window_started_at: `{str(payload.get('recent_window_started_at', '')).strip() or summary.get('started_at', '')}`",
        "",
        "## Current Champion",
        "",
        f"- run_id: `{summary.get('champion_run_id', '')}`",
        f"- revision_label: `{str(champion_row.get('revision_label', '')).strip() or summary.get('champion_revision_label', '')}`",
        f"- primary_metric: `{str(champion_row.get('primary_metric', '')).strip() or 'unknown'}`",
        f"- baseline_metric: `{str(baseline_row.get('primary_metric', '')).strip() or 'unknown'}`",
        "",
        "## Overnight Summary",
        "",
        f"- total_candidates: `{int(payload.get('candidate_total', 0) or 0)}`",
        f"- recent_candidates: `{int(payload.get('recent_candidate_total', 0) or 0)}`",
        f"- keeps: `{int(counts.get('keep', 0) or 0)}`",
        f"- discards: `{int(counts.get('discard', 0) or 0)}`",
        f"- crashes: `{int(counts.get('crash', 0) or 0)}`",
        f"- partials: `{int(counts.get('partial', 0) or 0)}`",
        f"- recent_keeps: `{int(recent_counts.get('keep', 0) or 0)}`",
        f"- recent_discards: `{int(recent_counts.get('discard', 0) or 0)}`",
        f"- recent_crashes: `{int(recent_counts.get('crash', 0) or 0)}`",
        f"- recent_partials: `{int(recent_counts.get('partial', 0) or 0)}`",
        f"- best_primary_delta: `{best_delta_text}`",
        "",
        "## Failure Themes",
        "",
    ]
    if failure_themes:
        lines.extend(
            f"- {str(item).strip()}" for item in failure_themes if str(item).strip()
        )
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## Steering",
            "",
            f"- oracle_required: `{bool(payload.get('oracle_required', False))}`",
            f"- oracle_reason: {str(payload.get('oracle_reason', '')).strip() or 'none'}",
            f"- pending_review: `{bool(payload.get('pending_review', False))}`",
            f"- recommended_command: `{str(payload.get('recommended_command', '')).strip() or 'none'}`",
            f"- safe_status: `{str(payload.get('safe_status', '')).strip() or 'blocked'}`",
            f"- recommendation_reason: {str(payload.get('recommended_reason', '')).strip() or 'none'}",
            "",
            "## Pointers",
            "",
            f"- results_md: `{str(payload.get('results_md_path', '')).strip() or 'missing'}`",
            f"- results_tsv: `{str(payload.get('results_tsv_path', '')).strip() or 'missing'}`",
            f"- oracle_md: `{str(payload.get('oracle_path', '')).strip() or 'missing'}`",
            f"- morning_report: `{str(payload.get('morning_report_path', '')).strip() or 'missing'}`",
        ]
    )
    if pending_decisions:
        lines.extend(["", "## Pending Human Decisions", ""])
        lines.extend(f"- {item}" for item in pending_decisions)
    if blocking_failures:
        lines.extend(["", "## Blocking Failures", ""])
        lines.extend(f"- {item}" for item in blocking_failures[:5])
    if preconditions:
        lines.extend(["", "## Preconditions", ""])
        lines.extend(f"- {item}" for item in preconditions)
    return "\n".join(lines).rstrip() + "\n"


def _campaign_compact_text(value: Any) -> str:
    return " ".join(
        str(value or "").replace("\t", " ").replace("\n", " ").split()
    ).strip()


def _campaign_primary_metric_text(payload: dict[str, Any]) -> str:
    primary_metric = payload.get("primary_metric")
    if not isinstance(primary_metric, dict):
        return ""
    value = primary_metric.get("value")
    if value is None:
        return ""
    return str(value)


def _campaign_memory_gb_text(payload: dict[str, Any]) -> str:
    memory_mb = _campaign_manifest_memory_mb(payload)
    if memory_mb is None or memory_mb <= 0:
        return ""
    value = memory_mb / 1024.0
    return f"{value:.3f}".rstrip("0").rstrip(".")


def _campaign_revision_label_from_manifest(
    payload: dict[str, Any],
    *,
    fallback: str = "",
) -> str:
    workspace_revision = payload.get("workspace_revision")
    if isinstance(workspace_revision, dict):
        label = str(workspace_revision.get("label", "")).strip()
        if label:
            return label
    remote_execution = payload.get("remote_execution")
    if isinstance(remote_execution, dict):
        code_sync = remote_execution.get("code_sync")
        if isinstance(code_sync, dict):
            for key in ("resolved_remote_revision_label", "requested_revision_label"):
                label = str(code_sync.get(key, "")).strip()
                if label:
                    return label
    revision_label = str(payload.get("revision_label", "")).strip()
    if revision_label:
        return revision_label
    return str(fallback).strip() or "unversioned-worktree"


def _campaign_run_window_membership(
    *,
    run_id: str,
    timestamp,
    started_at,
    baseline_run_id: str,
    comparison_events: dict[str, dict[str, Any]],
    kept_checkpoints: dict[str, dict[str, Any]],
) -> bool:
    if run_id == baseline_run_id:
        return True
    if run_id in comparison_events or run_id in kept_checkpoints:
        return True
    if started_at is None:
        return True
    if timestamp is None:
        return False
    return bool(timestamp >= started_at)


def _campaign_collect_iteration_runs(iteration_dir: Path) -> list[dict[str, Any]]:
    runs_root = iteration_dir / "runs"
    if not runs_root.exists():
        return []

    rows: dict[str, dict[str, Any]] = {}
    for manifest_path in runs_root.glob("*/run_manifest.json"):
        payload = _load_json_if_exists(manifest_path)
        if not isinstance(payload, dict):
            payload = {}
        run_id = str(payload.get("run_id", "")).strip() or manifest_path.parent.name
        if not run_id:
            continue
        row = rows.setdefault(
            run_id,
            {
                "run_id": run_id,
                "manifest_path": None,
                "manifest_payload": {},
                "metrics_path": None,
                "metrics_payload": {},
            },
        )
        row["manifest_path"] = manifest_path
        row["manifest_payload"] = payload

    for metrics_path in runs_root.glob("*/metrics.json"):
        payload = _load_json_if_exists(metrics_path)
        if not isinstance(payload, dict):
            payload = {}
        run_id = str(payload.get("run_id", "")).strip() or metrics_path.parent.name
        if not run_id:
            continue
        row = rows.setdefault(
            run_id,
            {
                "run_id": run_id,
                "manifest_path": None,
                "manifest_payload": {},
                "metrics_path": None,
                "metrics_payload": {},
            },
        )
        row["metrics_path"] = metrics_path
        row["metrics_payload"] = payload

    result: list[dict[str, Any]] = []
    for row in rows.values():
        manifest_payload = row.get("manifest_payload")
        if not isinstance(manifest_payload, dict):
            manifest_payload = {}
        run_id = str(row.get("run_id", "")).strip()
        result.append(
            {
                **row,
                "timestamp": _manifest_timestamp(manifest_payload, run_id),
            }
        )
    result.sort(
        key=lambda item: (
            item.get("timestamp") is None,
            item.get("timestamp"),
            str(item.get("run_id", "")).strip(),
        )
    )
    return result


def _campaign_comparison_events(
    repo_root: Path,
    campaign: dict[str, Any],
) -> list[dict[str, Any]]:
    normalized = _normalize_campaign(campaign)
    log_path = repo_root / ".autolab" / "logs" / "orchestrator.log"
    if not log_path.exists():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    started_at = _parse_utc(normalized["started_at"])
    events: list[dict[str, Any]] = []
    for line in lines:
        timestamp_text, separator, message = line.partition(" ")
        if not separator:
            continue
        timestamp = _parse_utc(timestamp_text)
        if started_at is not None and timestamp is not None and timestamp < started_at:
            continue
        match = _CAMPAIGN_COMPARE_LOG_PATTERN.match(message.strip())
        if not match:
            continue
        action = "keep" if match.group("action") == "promote" else "discard"
        challenger = match.group("challenger").strip()
        if not challenger:
            continue
        events.append(
            {
                "run_id": challenger,
                "status": action,
                "summary": _campaign_compact_text(match.group("summary")),
                "timestamp": timestamp,
                "champion_before": match.group("champion").strip(),
            }
        )
    return events


def _campaign_comparison_event_map(
    events: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for event in events:
        run_id = str(event.get("run_id", "")).strip()
        if not run_id:
            continue
        rows[run_id] = event
    return rows


def _campaign_baseline_run_id(
    campaign: dict[str, Any],
    comparison_events: list[dict[str, Any]],
    kept_rows: list[dict[str, Any]],
) -> str:
    normalized = _normalize_campaign(campaign)
    for event in comparison_events:
        champion_before = str(event.get("champion_before", "")).strip()
        if champion_before:
            return champion_before
    for row in kept_rows:
        run_id = str(row.get("run_id", "")).strip()
        if run_id:
            return run_id
    return normalized["champion_run_id"]


def _campaign_checkpoint_run_artifacts(
    repo_root: Path,
    checkpoint_id: str,
    *,
    iteration_dir: Path,
    run_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_checkpoint_id = str(checkpoint_id).strip()
    normalized_run_id = str(run_id).strip()
    if not normalized_checkpoint_id or not normalized_run_id:
        return ({}, {})
    run_root_rel = _campaign_iteration_relpath(
        repo_root,
        iteration_dir / "runs" / normalized_run_id,
    )
    manifest_payload = _campaign_checkpoint_json_artifact(
        repo_root,
        normalized_checkpoint_id,
        f"{run_root_rel}/run_manifest.json",
    )
    metrics_payload = _campaign_checkpoint_json_artifact(
        repo_root,
        normalized_checkpoint_id,
        f"{run_root_rel}/metrics.json",
    )
    return (manifest_payload, metrics_payload)


def _campaign_kept_run_checkpoints(
    repo_root: Path,
    campaign: dict[str, Any],
) -> list[dict[str, Any]]:
    normalized = _normalize_campaign(campaign)
    rows: list[dict[str, Any]] = []
    for entry in _campaign_champion_checkpoint_entries(repo_root, normalized):
        checkpoint_id = str(entry.get("checkpoint_id", "")).strip()
        if not checkpoint_id:
            continue
        manifest = _campaign_checkpoint_manifest(repo_root, checkpoint_id)
        state_snapshot = manifest.get("state_snapshot")
        if not isinstance(state_snapshot, dict):
            state_snapshot = {}
        run_id = str(state_snapshot.get("last_run_id", "")).strip()
        if not run_id:
            continue
        created_at = str(manifest.get("created_at", "")).strip()
        rows.append(
            {
                "checkpoint_id": checkpoint_id,
                "run_id": run_id,
                "created_at": created_at,
                "timestamp": _parse_utc(created_at),
                "revision_label": str(manifest.get("revision_label", "")).strip()
                or normalized["champion_revision_label"],
            }
        )
    rows.sort(
        key=lambda item: (
            item.get("timestamp") is None,
            item.get("timestamp"),
            str(item.get("checkpoint_id", "")).strip(),
        )
    )
    deduped: list[dict[str, Any]] = []
    last_run_id = ""
    for row in rows:
        run_id = str(row.get("run_id", "")).strip()
        if not run_id or run_id == last_run_id:
            continue
        deduped.append(row)
        last_run_id = run_id
    if not deduped:
        deduped.append(
            {
                "checkpoint_id": "",
                "run_id": normalized["champion_run_id"],
                "created_at": normalized["started_at"],
                "timestamp": _parse_utc(normalized["started_at"]),
                "revision_label": normalized["champion_revision_label"],
            }
        )
    return deduped


def _campaign_row_status_and_summary(
    *,
    run_id: str,
    metrics_payload: dict[str, Any],
    manifest_payload: dict[str, Any],
    baseline_run_id: str,
    comparison_events: dict[str, dict[str, Any]],
) -> tuple[str, str]:
    if run_id == baseline_run_id:
        return ("keep", "accepted baseline")

    event = comparison_events.get(run_id)
    if isinstance(event, dict):
        status = str(event.get("status", "")).strip().lower()
        summary = _campaign_compact_text(event.get("summary", ""))
        if status in {"keep", "discard"}:
            return (status, summary or f"campaign {status}")

    metrics_status = str(metrics_payload.get("status", "")).strip().lower()
    manifest_status = str(manifest_payload.get("status", "")).strip().lower()
    if metrics_status == "partial" or manifest_status == "partial":
        return (
            "partial",
            _campaign_compact_text(
                f"partial run evidence (manifest={manifest_status or 'missing'}, "
                f"metrics={metrics_status or 'missing'})"
            ),
        )
    if manifest_status == "failed":
        timeout_payload = manifest_payload.get("campaign_timeout")
        if isinstance(timeout_payload, dict):
            timeout_reason = _campaign_compact_text(timeout_payload.get("reason", ""))
            elapsed_seconds = timeout_payload.get("elapsed_seconds")
            timeout_reference_seconds = timeout_payload.get("timeout_reference_seconds")
            if timeout_reason and elapsed_seconds is not None:
                return (
                    "crash",
                    _campaign_compact_text(
                        f"{timeout_reason} (elapsed={elapsed_seconds}s, "
                        f"reference={timeout_reference_seconds}s)"
                    ),
                )
            if timeout_reason:
                return ("crash", timeout_reason)
        return ("crash", "run manifest status=failed")
    if not metrics_payload:
        return ("crash", "metrics artifact missing")
    if metrics_status != "completed":
        return ("crash", f"metrics status={metrics_status or 'missing'}")
    primary_metric = metrics_payload.get("primary_metric")
    metric_value = None
    if isinstance(primary_metric, dict):
        metric_value = primary_metric.get("value")
    if metric_value is None:
        return ("crash", "primary metric value missing")
    return ("partial", "campaign outcome unavailable")


def _campaign_render_results_markdown(
    *,
    repo_root: Path,
    campaign: dict[str, Any],
    rows: list[dict[str, Any]],
    diagnostics: list[str],
    tsv_path: Path,
) -> str:
    normalized = _normalize_campaign(campaign)
    counts = _campaign_results_counts(rows)
    novelty_summary = _campaign_novelty_summary(normalized)
    recent_entries = novelty_summary.get("recent_entries", [])
    if not isinstance(recent_entries, list):
        recent_entries = []
    lines = [
        "# Campaign Results",
        "",
        f"- generated_at: `{_utc_now()}`",
        f"- campaign_id: `{normalized['campaign_id']}`",
        f"- label: `{normalized['label']}`",
        f"- scope_kind: `{normalized['scope_kind']}`",
        f"- iteration_id: `{normalized['iteration_id']}`",
        f"- objective_metric: `{normalized['objective_metric']}`",
        f"- objective_mode: `{normalized['objective_mode']}`",
        f"- status: `{normalized['status']}`",
        f"- champion_run_id: `{normalized['champion_run_id']}`",
        f"- champion_revision_label: `{normalized['champion_revision_label']}`",
        f"- results_tsv: `{_campaign_iteration_relpath(repo_root, tsv_path)}`",
        "",
        "## Totals",
        "",
        f"- keep: `{counts['keep']}`",
        f"- discard: `{counts['discard']}`",
        f"- crash: `{counts['crash']}`",
        f"- partial: `{counts['partial']}`",
        "",
        "## Idea Journal",
        "",
        f"- active_family: `{str(novelty_summary.get('active_family', '')).strip() or 'none'}`",
        f"- active_thesis: {str(novelty_summary.get('active_thesis', '')).strip() or 'none'}",
        f"- same_family_streak: `{int(novelty_summary.get('same_family_streak', 0) or 0)}`",
        f"- last_completed: `{str(novelty_summary.get('last_completed_status', '')).strip() or 'none'}` / `{str(novelty_summary.get('last_completed_family', '')).strip() or 'none'}`",
        f"- recent_failed_families: `{', '.join(novelty_summary.get('recent_failed_families', [])) if novelty_summary.get('recent_failed_families') else 'none'}`",
        f"- recent_near_miss_families: `{', '.join(novelty_summary.get('recent_near_miss_families', [])) if novelty_summary.get('recent_near_miss_families') else 'none'}`",
        "",
        "### Recent Ideas",
        "",
        "| entry_id | family | status | attempts | runs | thesis | reason |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for entry in recent_entries:
        if not isinstance(entry, dict):
            continue
        lines.append(
            "| {entry_id} | {family} | {status} | {attempts} | {runs} | {thesis} | {reason} |".format(
                entry_id=str(entry.get("entry_id", "")).replace("|", "/"),
                family=_campaign_entry_family_display(entry).replace("|", "/"),
                status=str(entry.get("status", "")).replace("|", "/"),
                attempts=str(entry.get("attempt_count", "")).replace("|", "/"),
                runs=", ".join(
                    _campaign_string_list(entry.get("run_ids"), limit=3)
                ).replace("|", "/"),
                thesis=str(entry.get("thesis", "")).replace("|", "/"),
                reason=str(entry.get("outcome_reason", "")).replace("|", "/"),
            )
        )
    if not recent_entries:
        lines.append("| none | none | none | 0 | none | none | none |")
    lines.extend(
        [
            "",
            "## Results",
            "",
            "| revision_label | run_id | primary_metric | memory_gb | status | summary |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for row in rows:
        lines.append(
            "| {revision_label} | {run_id} | {primary_metric} | {memory_gb} | {status} | {summary} |".format(
                revision_label=str(row.get("revision_label", "")).replace("|", "/"),
                run_id=str(row.get("run_id", "")).replace("|", "/"),
                primary_metric=str(row.get("primary_metric", "")).replace("|", "/"),
                memory_gb=str(row.get("memory_gb", "")).replace("|", "/"),
                status=str(row.get("status", "")).replace("|", "/"),
                summary=str(row.get("summary", "")).replace("|", "/"),
            )
        )
    if diagnostics:
        lines.extend(["", "## Diagnostics", ""])
        lines.extend(f"- {item}" for item in diagnostics)
    return "\n".join(lines).rstrip() + "\n"


def _refresh_campaign_results(
    repo_root: Path,
    campaign: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    iteration_dir = _campaign_iteration_dir_for_results(repo_root, normalized)
    scope_root = _campaign_scope_root(repo_root, normalized)
    results_tsv_path = _campaign_results_tsv_path(repo_root, normalized)
    results_md_path = _campaign_results_markdown_path(repo_root, normalized)
    started_at = _parse_utc(normalized["started_at"])
    comparison_events = _campaign_comparison_events(repo_root, normalized)
    comparison_event_map = _campaign_comparison_event_map(comparison_events)
    kept_rows = _campaign_kept_run_checkpoints(repo_root, normalized)
    kept_by_run_id = {
        str(item.get("run_id", "")).strip(): item
        for item in kept_rows
        if item.get("run_id")
    }
    baseline_run_id = _campaign_baseline_run_id(
        normalized,
        comparison_events,
        kept_rows,
    )
    iteration_runs = _campaign_collect_iteration_runs(iteration_dir)
    selected_runs: dict[str, dict[str, Any]] = {}
    for entry in iteration_runs:
        run_id = str(entry.get("run_id", "")).strip()
        if not run_id:
            continue
        if not _campaign_run_window_membership(
            run_id=run_id,
            timestamp=entry.get("timestamp"),
            started_at=started_at,
            baseline_run_id=baseline_run_id,
            comparison_events=comparison_event_map,
            kept_checkpoints=kept_by_run_id,
        ):
            continue
        selected_runs[run_id] = dict(entry)

    if baseline_run_id and baseline_run_id not in selected_runs:
        selected_runs[baseline_run_id] = {
            "run_id": baseline_run_id,
            "manifest_path": None,
            "manifest_payload": {},
            "metrics_path": None,
            "metrics_payload": {},
            "timestamp": None,
        }

    for kept_info in kept_rows:
        run_id = str(kept_info.get("run_id", "")).strip()
        if not run_id or run_id in selected_runs:
            continue
        selected_runs[run_id] = {
            "run_id": run_id,
            "manifest_path": None,
            "manifest_payload": {},
            "metrics_path": None,
            "metrics_payload": {},
            "timestamp": kept_info.get("timestamp"),
        }

    for event in comparison_events:
        run_id = str(event.get("run_id", "")).strip()
        if not run_id or run_id in selected_runs:
            continue
        selected_runs[run_id] = {
            "run_id": run_id,
            "manifest_path": None,
            "manifest_payload": {},
            "metrics_path": None,
            "metrics_payload": {},
            "timestamp": event.get("timestamp"),
        }

    baseline_first: list[dict[str, Any]] = []
    ordered_run_ids: set[str] = set()
    if baseline_run_id and baseline_run_id in selected_runs:
        baseline_first.append(selected_runs[baseline_run_id])
        ordered_run_ids.add(baseline_run_id)

    compared_runs: list[dict[str, Any]] = []
    for event in comparison_events:
        run_id = str(event.get("run_id", "")).strip()
        if not run_id or run_id in ordered_run_ids:
            continue
        entry = selected_runs.get(run_id)
        if entry is None:
            continue
        compared_runs.append(entry)
        ordered_run_ids.add(run_id)

    non_compared = [
        entry
        for run_id, entry in selected_runs.items()
        if run_id not in ordered_run_ids
    ]
    non_compared.sort(
        key=lambda item: (
            item.get("timestamp") is None,
            item.get("timestamp"),
            str(item.get("run_id", "")).strip(),
        )
    )
    ordered_runs = baseline_first + compared_runs + non_compared

    rows: list[dict[str, Any]] = []
    diagnostics: list[str] = []
    for entry in ordered_runs:
        run_id = str(entry.get("run_id", "")).strip()
        kept_info = kept_by_run_id.get(run_id, {})
        checkpoint_id = str(kept_info.get("checkpoint_id", "")).strip()
        manifest_payload = entry.get("manifest_payload")
        if not isinstance(manifest_payload, dict):
            manifest_payload = {}
        metrics_payload = entry.get("metrics_payload")
        if not isinstance(metrics_payload, dict):
            metrics_payload = {}
        if checkpoint_id and (not manifest_payload or not metrics_payload):
            checkpoint_manifest, checkpoint_metrics = (
                _campaign_checkpoint_run_artifacts(
                    repo_root,
                    checkpoint_id,
                    iteration_dir=iteration_dir,
                    run_id=run_id,
                )
            )
            if not manifest_payload:
                manifest_payload = checkpoint_manifest
            if not metrics_payload:
                metrics_payload = checkpoint_metrics
        event = comparison_event_map.get(run_id, {})
        if entry.get("timestamp") is None and isinstance(event, dict):
            entry["timestamp"] = event.get("timestamp")
        status, summary = _campaign_row_status_and_summary(
            run_id=run_id,
            metrics_payload=metrics_payload,
            manifest_payload=manifest_payload,
            baseline_run_id=baseline_run_id,
            comparison_events=comparison_event_map,
        )
        revision_fallback = str(kept_info.get("revision_label", "")).strip()
        if run_id == baseline_run_id and not revision_fallback:
            revision_fallback = normalized["champion_revision_label"]
        rows.append(
            {
                "revision_label": _campaign_revision_label_from_manifest(
                    manifest_payload,
                    fallback=revision_fallback,
                ),
                "run_id": run_id,
                "primary_metric": _campaign_primary_metric_text(metrics_payload),
                "memory_gb": _campaign_memory_gb_text(manifest_payload),
                "status": status,
                "summary": _campaign_compact_text(summary),
                "timestamp": _campaign_timestamp_text(entry.get("timestamp")),
                "is_baseline": run_id == baseline_run_id,
            }
        )
        if (
            run_id != baseline_run_id
            and run_id not in comparison_event_map
            and status == "partial"
        ):
            diagnostics.append(
                f"run_id={run_id} has no recorded campaign compare outcome; rendered as partial"
            )
        if (
            not manifest_payload
            and not metrics_payload
            and run_id in comparison_event_map
        ):
            diagnostics.append(
                f"run_id={run_id} is reconstructed from campaign compare history without run artifacts"
            )

    results_tsv_path.parent.mkdir(parents=True, exist_ok=True)
    tsv_lines = [
        "\t".join(
            [
                "revision_label",
                "run_id",
                "primary_metric",
                "memory_gb",
                "status",
                "summary",
            ]
        )
    ]
    for row in rows:
        tsv_lines.append(
            "\t".join(
                [
                    _campaign_compact_text(row.get("revision_label", "")),
                    _campaign_compact_text(row.get("run_id", "")),
                    _campaign_compact_text(row.get("primary_metric", "")),
                    _campaign_compact_text(row.get("memory_gb", "")),
                    _campaign_compact_text(row.get("status", "")),
                    _campaign_compact_text(row.get("summary", "")),
                ]
            )
        )
    results_tsv_path.write_text("\n".join(tsv_lines).rstrip() + "\n", encoding="utf-8")
    results_md_path.write_text(
        _campaign_render_results_markdown(
            repo_root=repo_root,
            campaign=normalized,
            rows=rows,
            diagnostics=diagnostics,
            tsv_path=results_tsv_path,
        ),
        encoding="utf-8",
    )
    return {
        "results_tsv_path": results_tsv_path,
        "results_md_path": results_md_path,
        "row_count": len(rows),
        "counts": _campaign_results_counts(rows),
        "diagnostics": diagnostics,
        "iteration_dir": iteration_dir,
        "scope_root": scope_root,
        "baseline_run_id": baseline_run_id,
        "rows": rows,
    }


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


def _campaign_has_lock_contract(campaign: dict[str, Any]) -> bool:
    normalized = _normalize_campaign(campaign)
    return not bool(_campaign_missing_lock_fields(normalized))


def _campaign_backfill_lock_contract(
    repo_root: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    if _campaign_lock_mode(normalized) == "none":
        return normalized
    normalized["lock_contract"] = _campaign_capture_lock_contract(
        repo_root,
        state,
        normalized,
    )
    _write_campaign(repo_root, normalized)
    return normalized


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


def _campaign_candidate_run_id_from_state(
    state: dict[str, Any],
    campaign: dict[str, Any],
) -> str:
    champion_run_id = str(campaign.get("champion_run_id", "")).strip()
    for key in ("pending_run_id", "last_run_id"):
        run_id = str(state.get(key, "")).strip()
        if run_id and run_id != champion_run_id:
            return run_id
    return ""


def _campaign_sync_active_candidate_from_state(
    campaign: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    if not _campaign_has_active_candidate(normalized):
        return normalized
    candidate = _normalize_campaign_active_candidate(normalized.get("active_candidate"))
    if candidate["run_id"]:
        return normalized
    run_id = _campaign_candidate_run_id_from_state(state, normalized)
    if not run_id:
        return normalized
    candidate["run_id"] = run_id
    normalized["active_candidate"] = candidate
    return normalized


def _campaign_manifest_timestamps(payload: dict[str, Any]) -> tuple[str, str]:
    timestamps = payload.get("timestamps")
    if not isinstance(timestamps, dict):
        timestamps = {}
    started_at = (
        str(timestamps.get("started_at", "")).strip()
        or str(payload.get("started_at", "")).strip()
    )
    completed_at = (
        str(timestamps.get("completed_at", "")).strip()
        or str(payload.get("completed_at", "")).strip()
    )
    return (started_at, completed_at)


def _campaign_run_duration_seconds_from_manifest(
    payload: dict[str, Any],
    *,
    allow_incomplete: bool = False,
) -> float | None:
    started_at_text, completed_at_text = _campaign_manifest_timestamps(payload)
    started_at = _parse_utc(started_at_text)
    if started_at is None:
        return None
    completed_at = _parse_utc(completed_at_text)
    if completed_at is None:
        if not allow_incomplete:
            return None
        completed_at = _parse_utc(_utc_now())
        if completed_at is None:
            return None
    elapsed_seconds = (completed_at - started_at).total_seconds()
    if elapsed_seconds < 0:
        return None
    return elapsed_seconds


def _campaign_run_duration_seconds(
    repo_root: Path,
    state: dict[str, Any],
    *,
    run_id: str,
    allow_incomplete: bool = False,
) -> float | None:
    manifest_payload = _campaign_load_manifest_payload(repo_root, state, run_id=run_id)
    if not manifest_payload:
        return None
    return _campaign_run_duration_seconds_from_manifest(
        manifest_payload,
        allow_incomplete=allow_incomplete,
    )


def _campaign_manifest_job_id(payload: dict[str, Any]) -> str:
    candidates = (
        payload.get("job_id"),
        (payload.get("slurm") or {}).get("job_id")
        if isinstance(payload.get("slurm"), dict)
        else "",
        (payload.get("resource_request") or {}).get("job_id")
        if isinstance(payload.get("resource_request"), dict)
        else "",
        (
            ((payload.get("resource_request") or {}).get("slurm") or {}).get("job_id")
            if isinstance((payload.get("resource_request") or {}).get("slurm"), dict)
            else ""
        ),
    )
    for raw in candidates:
        value = str(raw or "").strip()
        if value:
            return value
    return ""


def _campaign_cancel_slurm_run_for_timeout(
    repo_root: Path,
    state: dict[str, Any],
    *,
    run_id: str,
    elapsed_seconds: float,
    timeout_reference_seconds: float,
    max_timeout_factor: float,
) -> dict[str, Any]:
    iteration_dir = _campaign_iteration_dir(repo_root, state)
    manifest_path = iteration_dir / "runs" / run_id / "run_manifest.json"
    payload = _load_json_if_exists(manifest_path)
    if not isinstance(payload, dict):
        raise CampaignError(
            f"campaign timeout cancellation requires run manifest at {manifest_path}"
        )

    host_mode = (
        str(
            payload.get("host_mode")
            or payload.get("launch_mode")
            or _detect_priority_host_mode()
        )
        .strip()
        .lower()
    )
    if host_mode == "slurm_interactive":
        host_mode = "slurm"
    if host_mode != "slurm":
        raise CampaignError(
            f"campaign timeout cancellation only supports slurm manifests (run_id={run_id})"
        )

    job_id = _campaign_manifest_job_id(payload)
    if not job_id:
        raise CampaignError(
            f"campaign timeout cancellation requires job_id for run_id={run_id}"
        )

    command_text = ""
    stdout_text = ""
    stderr_text = ""
    remote_execution = payload.get("remote_execution")
    if isinstance(remote_execution, dict):
        remote_profile_name = str(remote_execution.get("profile", "")).strip()
        remote_profile_mode = str(remote_execution.get("mode", "")).strip().lower()
    else:
        remote_profile_name = ""
        remote_profile_mode = ""

    if remote_profile_name and remote_profile_mode not in {"", "shared_fs"}:
        from autolab.remote_profiles import cancel_remote_job, resolve_remote_profile

        profile = resolve_remote_profile(
            repo_root,
            host_mode="slurm",
            profile_name=remote_profile_name,
        )
        stdout_text, stderr_text = cancel_remote_job(
            profile,
            job_id=job_id,
            timeout_seconds=30.0,
        )
        command_text = f"ssh {profile.login_host} scancel {job_id}"
    else:
        try:
            proc = subprocess.run(
                ["scancel", job_id],
                cwd=repo_root,
                text=True,
                capture_output=True,
                check=False,
                timeout=30.0,
            )
        except FileNotFoundError as exc:
            raise CampaignError(
                f"campaign timeout cancellation requires scancel for run_id={run_id}: {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CampaignError(
                f"campaign timeout cancellation timed out for run_id={run_id}: {exc}"
            ) from exc
        stdout_text = str(proc.stdout or "")
        stderr_text = str(proc.stderr or "")
        if proc.returncode != 0:
            detail = stderr_text.strip() or stdout_text.strip() or "unknown error"
            raise CampaignError(
                f"campaign timeout cancellation failed for run_id={run_id}: {detail}"
            )
        command_text = f"scancel {job_id}"

    artifact_sync = payload.get("artifact_sync_to_local")
    if not isinstance(artifact_sync, dict):
        artifact_sync = {}
    artifact_sync["status"] = "failed"
    payload["artifact_sync_to_local"] = artifact_sync
    payload["status"] = "failed"

    timestamps = payload.get("timestamps")
    if not isinstance(timestamps, dict):
        timestamps = {}
    started_at_text, completed_at_text = _campaign_manifest_timestamps(payload)
    if not started_at_text:
        started_at_text = _utc_now()
    if not completed_at_text:
        completed_at_text = _utc_now()
    timestamps["started_at"] = started_at_text
    timestamps["completed_at"] = completed_at_text
    payload["timestamps"] = timestamps
    payload["started_at"] = started_at_text
    payload["completed_at"] = completed_at_text
    payload["campaign_timeout"] = {
        "detected_at": _utc_now(),
        "elapsed_seconds": round(float(elapsed_seconds), 3),
        "timeout_reference_seconds": round(float(timeout_reference_seconds), 3),
        "max_timeout_factor": float(max_timeout_factor),
        "reason": (
            "campaign timeout governance aborted challenger after exceeding "
            "champion-relative duration budget"
        ),
        "cancel_command": command_text,
        "cancel_stdout": stdout_text.strip(),
        "cancel_stderr": stderr_text.strip(),
    }
    _write_json(manifest_path, payload)
    return {
        "run_id": run_id,
        "job_id": job_id,
        "manifest_path": str(manifest_path),
        "cancel_command": command_text,
    }


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
            "decision_basis": "primary_metric",
            "near_miss": False,
            "summary": (
                "primary metric improved "
                f"({challenger_run_id}={challenger_value} vs {champion_run_id}={champion_value})"
            ),
        }
    if metric_decision < 0:
        return {
            "winner": "champion",
            "decision_basis": "primary_metric_regression",
            "near_miss": False,
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
            "decision_basis": "memory_tie_break",
            "near_miss": winner == "champion",
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
                "decision_basis": "complexity_tie_break",
                "near_miss": winner == "champion",
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
            "decision_basis": "policy_risk_tie_break",
            "near_miss": winner == "champion",
            "summary": (
                "all prior tie-breaks tied; policy-risk tie-break "
                f"({challenger_run_id}={challenger_risk} vs "
                f"{champion_run_id}={champion_risk})"
            ),
        }

    return {
        "winner": "champion",
        "decision_basis": "all_tied_keep_champion",
        "near_miss": False,
        "summary": (
            "all campaign comparisons tied; keeping existing champion "
            f"({champion_run_id})"
        ),
    }


def _campaign_apply_crash_outcome(
    repo_root: Path,
    state_path: Path,
    state: dict[str, Any],
    campaign: dict[str, Any],
    *,
    reason: str,
    category: str = "crash_discard",
    run_id: str = "",
) -> dict[str, Any]:
    normalized = _normalize_campaign(campaign)
    checkpoint_id = _campaign_latest_champion_checkpoint_id(repo_root, normalized)
    if not checkpoint_id:
        raise CampaignError(
            "campaign champion snapshot is missing; rerun from decide_repeat to reseed"
        )
    crash_run_id = str(run_id).strip() or _campaign_candidate_run_id_from_state(
        state, normalized
    )
    updated = _campaign_finalize_active_idea_journal(
        repo_root,
        state,
        normalized,
        status="crash",
        reason=reason,
        champion_after_run_id=str(normalized.get("champion_run_id", "")).strip(),
        run_id=crash_run_id,
    )
    _campaign_restore_champion_state(
        repo_root,
        state_path,
        normalized,
        checkpoint_id=checkpoint_id,
    )
    updated["no_improvement_streak"] = (
        int(updated.get("no_improvement_streak", 0) or 0) + 1
    )
    updated["crash_streak"] = int(updated.get("crash_streak", 0) or 0) + 1
    updated = _campaign_reset_active_candidate(updated, repo_root=repo_root)
    updated = _campaign_set_last_governance_event(
        updated,
        category=category,
        run_id=crash_run_id,
        reason=reason,
    )
    _write_campaign(repo_root, updated)
    return {
        "action": category,
        "campaign": updated,
        "summary": str(reason).strip(),
        "run_id": crash_run_id,
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
    near_miss = bool(comparison.get("near_miss", False))

    if winner == "challenger":
        updated = _campaign_finalize_active_idea_journal(
            repo_root,
            state,
            normalized,
            status="keep",
            reason=summary,
            champion_after_run_id=challenger_run_id,
            run_id=challenger_run_id,
        )
        _campaign_seed_champion_snapshot(repo_root, state_path, normalized)
        updated["champion_run_id"] = challenger_run_id
        updated["champion_revision_label"] = _resolve_revision_label(repo_root)
        updated["no_improvement_streak"] = 0
        updated["crash_streak"] = 0
        updated = _campaign_reset_active_candidate(updated, repo_root=repo_root)
        updated = _campaign_set_last_governance_event(
            updated,
            category="promotion",
            run_id=challenger_run_id,
            reason=summary,
        )
        _write_campaign(repo_root, updated)
        return {
            "action": "promote",
            "campaign": updated,
            "summary": summary,
        }

    updated = _campaign_finalize_active_idea_journal(
        repo_root,
        state,
        normalized,
        status="discard",
        reason=summary,
        near_miss=near_miss,
        champion_after_run_id=str(normalized.get("champion_run_id", "")).strip(),
        run_id=challenger_run_id,
    )
    _campaign_restore_champion_state(
        repo_root,
        state_path,
        normalized,
        checkpoint_id=checkpoint_id,
    )
    updated["no_improvement_streak"] = (
        int(updated.get("no_improvement_streak", 0) or 0) + 1
    )
    updated["crash_streak"] = 0
    updated = _campaign_reset_active_candidate(updated, repo_root=repo_root)
    updated = _campaign_set_last_governance_event(
        updated,
        category="metric_discard",
        run_id=challenger_run_id,
        reason=summary,
    )
    _write_campaign(repo_root, updated)
    return {
        "action": "discard",
        "campaign": updated,
        "summary": summary,
    }


__all__ = [name for name in globals() if not name.startswith("__")]
