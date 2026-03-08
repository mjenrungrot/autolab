from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from autolab.uat import normalize_uat_required_by
from autolab.utils import _load_json_if_exists, _utc_now, _write_json


def _normalize_json_hash(payload: dict[str, Any]) -> str:
    rendered = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def plan_approval_json_path(iteration_dir: Path) -> Path:
    return iteration_dir / "plan_approval.json"


def plan_approval_markdown_path(iteration_dir: Path) -> Path:
    return iteration_dir / "plan_approval.md"


def load_plan_approval(iteration_dir: Path) -> dict[str, Any]:
    payload = _load_json_if_exists(plan_approval_json_path(iteration_dir))
    if isinstance(payload, dict):
        return payload
    return {}


def _default_source_paths() -> dict[str, str]:
    return {
        "plan_contract": ".autolab/plan_contract.json",
        "plan_graph": ".autolab/plan_graph.json",
        "plan_check_result": ".autolab/plan_check_result.json",
    }


def _policy_uat_required(approval_risk: dict[str, Any]) -> bool:
    risk_flags = approval_risk.get("risk_flags")
    if isinstance(risk_flags, dict):
        return bool(risk_flags.get("uat_required", False))
    return False


def _normalize_uat_block(raw_uat: Any, *, policy_required: bool) -> dict[str, Any]:
    required_by = "none"
    if isinstance(raw_uat, dict):
        required_by = normalize_uat_required_by(raw_uat.get("required_by", ""))
    if policy_required:
        required_by = "policy"
    elif required_by not in {"plan_approval", "manual"}:
        required_by = "none"
    return {
        "policy_required": policy_required,
        "effective_required": required_by != "none",
        "required_by": required_by,
    }


def _existing_policy_uat_required(payload: dict[str, Any]) -> bool:
    raw_uat = payload.get("uat")
    if isinstance(raw_uat, dict):
        return bool(raw_uat.get("policy_required", False))
    return False


def build_plan_hash(
    *,
    contract_payload: dict[str, Any],
    graph_payload: dict[str, Any],
) -> str:
    return _normalize_json_hash(
        {
            "contract": contract_payload,
            "graph": graph_payload,
        }
    )


def build_risk_fingerprint(approval_risk: dict[str, Any]) -> str:
    return _normalize_json_hash(
        {
            "requires_approval": bool(approval_risk.get("requires_approval", False)),
            "trigger_reasons": list(approval_risk.get("trigger_reasons", [])),
            "counts": dict(approval_risk.get("counts", {})),
            "policy": dict(approval_risk.get("policy", {})),
        }
    )


def approval_is_current(
    payload: dict[str, Any],
    *,
    plan_hash: str,
    risk_fingerprint: str,
    require_approved: bool = False,
) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if str(payload.get("plan_hash", "")).strip() != plan_hash:
        return False
    if str(payload.get("risk_fingerprint", "")).strip() != risk_fingerprint:
        return False
    status = str(payload.get("status", "")).strip().lower()
    if require_approved:
        return status == "approved"
    return status in {"approved", "pending", "retry", "stop", "not_required"}


def approval_requires_action(payload: dict[str, Any]) -> bool:
    if not isinstance(payload, dict) or not payload:
        return False
    if not bool(payload.get("requires_approval", False)):
        return False
    return str(payload.get("status", "")).strip().lower() != "approved"


def approval_next_commands(payload: dict[str, Any]) -> list[str]:
    status = str(payload.get("status", "")).strip().lower()
    action_mode = "refresh" if status == "superseded" else "approve"
    return approval_next_commands_for_mode(payload, action_mode=action_mode)


def approval_next_commands_for_mode(
    payload: dict[str, Any],
    *,
    action_mode: str,
) -> list[str]:
    if not approval_requires_action(payload):
        return []
    if action_mode == "refresh":
        return [
            "autolab run --plan-only",
            "autolab run",
        ]
    return [
        "autolab approve-plan --status approve",
        "autolab approve-plan --status retry",
        "autolab approve-plan --status stop",
    ]


def resolve_plan_approval_state(
    repo_root: Path,
    iteration_dir: Path,
) -> tuple[dict[str, Any], str, str]:
    existing = load_plan_approval(iteration_dir)
    base_uat = _normalize_uat_block(
        existing.get("uat"),
        policy_required=_existing_policy_uat_required(existing),
    )
    base_payload = {
        "schema_version": "1.0",
        "generated_at": str(existing.get("generated_at", "")).strip() or _utc_now(),
        "iteration_id": str(existing.get("iteration_id", "")).strip()
        or iteration_dir.name,
        "status": "superseded" if existing else "not_required",
        "requires_approval": bool(existing.get("requires_approval", False)),
        "plan_hash": str(existing.get("plan_hash", "")).strip(),
        "risk_fingerprint": str(existing.get("risk_fingerprint", "")).strip(),
        "trigger_reasons": [
            str(item).strip()
            for item in existing.get("trigger_reasons", [])
            if str(item).strip()
        ],
        "counts": dict(existing.get("counts", {}))
        if isinstance(existing.get("counts"), dict)
        else {},
        "reviewed_by": str(existing.get("reviewed_by", "")).strip(),
        "reviewed_at": str(existing.get("reviewed_at", "")).strip(),
        "notes": str(existing.get("notes", "")).strip(),
        "source_paths": dict(existing.get("source_paths", {}))
        if isinstance(existing.get("source_paths"), dict)
        else _default_source_paths(),
        "uat": base_uat,
    }
    if not base_payload["source_paths"]:
        base_payload["source_paths"] = _default_source_paths()

    contract_payload = _load_json_if_exists(
        repo_root / ".autolab" / "plan_contract.json"
    )
    graph_payload = _load_json_if_exists(repo_root / ".autolab" / "plan_graph.json")
    plan_check_payload = _load_json_if_exists(
        repo_root / ".autolab" / "plan_check_result.json"
    )
    if not isinstance(contract_payload, dict) or not isinstance(graph_payload, dict):
        if existing:
            return (
                base_payload,
                "current planning artifacts are missing or invalid; rerun planning before approval",
                "refresh",
            )
        return ({}, "", "none")
    if not isinstance(plan_check_payload, dict):
        if existing:
            return (
                base_payload,
                "current plan_check_result.json is missing or invalid; rerun planning before approval",
                "refresh",
            )
        return ({}, "", "none")

    approval_risk = plan_check_payload.get("approval_risk")
    if not isinstance(approval_risk, dict):
        if existing:
            return (
                base_payload,
                "current plan_check_result.json is missing approval_risk",
                "refresh",
            )
        return ({}, "current plan_check_result.json is missing approval_risk", "none")

    plan_hash = str(plan_check_payload.get("plan_hash", "")).strip() or build_plan_hash(
        contract_payload=contract_payload,
        graph_payload=graph_payload,
    )
    risk_fingerprint = build_risk_fingerprint(approval_risk)
    requires_approval = bool(approval_risk.get("requires_approval", False))
    current = approval_is_current(
        existing,
        plan_hash=plan_hash,
        risk_fingerprint=risk_fingerprint,
        require_approved=False,
    )
    policy_uat_required = _policy_uat_required(approval_risk)

    payload = {
        "schema_version": "1.0",
        "generated_at": str(existing.get("generated_at", "")).strip() or _utc_now(),
        "iteration_id": str(existing.get("iteration_id", "")).strip()
        or iteration_dir.name,
        "status": "not_required",
        "requires_approval": requires_approval,
        "plan_hash": plan_hash,
        "risk_fingerprint": risk_fingerprint,
        "trigger_reasons": [
            str(item).strip()
            for item in approval_risk.get("trigger_reasons", [])
            if str(item).strip()
        ],
        "counts": dict(approval_risk.get("counts", {})),
        "reviewed_by": "",
        "reviewed_at": "",
        "notes": "",
        "source_paths": dict(existing.get("source_paths", {}))
        if isinstance(existing.get("source_paths"), dict)
        else _default_source_paths(),
        "uat": _normalize_uat_block(
            existing.get("uat"),
            policy_required=policy_uat_required,
        ),
    }
    if not payload["source_paths"]:
        payload["source_paths"] = _default_source_paths()

    if not requires_approval:
        return (payload, "", "none")

    if current:
        payload["status"] = str(existing.get("status", "")).strip().lower() or "pending"
        payload["reviewed_by"] = str(existing.get("reviewed_by", "")).strip()
        payload["reviewed_at"] = str(existing.get("reviewed_at", "")).strip()
        payload["notes"] = str(existing.get("notes", "")).strip()
        return (payload, "", "approve")

    if existing:
        payload["status"] = "superseded"
        return (
            payload,
            "stale plan_approval.json: plan hash or risk fingerprint no longer match current planning artifacts",
            "refresh",
        )

    payload["status"] = "superseded"
    return (
        payload,
        "missing plan_approval.json for the current high-risk plan; rerun planning before approval",
        "refresh",
    )


def _counts_text(counts: dict[str, Any]) -> str:
    return (
        f"tasks={int(counts.get('tasks_total', 0) or 0)}, "
        f"waves={int(counts.get('waves_total', 0) or 0)}, "
        f"project_wide_tasks={int(counts.get('project_wide_tasks', 0) or 0)}, "
        f"project_wide_paths={int(counts.get('project_wide_unique_paths', 0) or 0)}, "
        f"retries={int(counts.get('observed_retries', 0) or 0)}"
    )


def render_plan_approval_markdown(payload: dict[str, Any]) -> str:
    trigger_reasons = [
        str(item).strip()
        for item in payload.get("trigger_reasons", [])
        if str(item).strip()
    ]
    uat = payload.get("uat")
    if not isinstance(uat, dict):
        uat = _normalize_uat_block({}, policy_required=False)
    lines = [
        "# Plan Approval",
        "",
        f"- generated_at: `{payload.get('generated_at', '')}`",
        f"- iteration_id: `{payload.get('iteration_id', '')}`",
        f"- status: `{payload.get('status', '')}`",
        f"- requires_approval: `{bool(payload.get('requires_approval', False))}`",
        f"- plan_hash: `{payload.get('plan_hash', '')}`",
        f"- risk_fingerprint: `{payload.get('risk_fingerprint', '')}`",
        f"- counts: {_counts_text(dict(payload.get('counts', {})))}",
        "",
        "## Trigger Reasons",
    ]
    if trigger_reasons:
        lines.extend(f"- {item}" for item in trigger_reasons)
    else:
        lines.append("- none")
    lines.extend(
        [
            "",
            "## UAT",
            "",
            f"- policy_required: `{bool(uat.get('policy_required', False))}`",
            f"- effective_required: `{bool(uat.get('effective_required', False))}`",
            f"- required_by: `{uat.get('required_by', 'none')}`",
            "",
            "## Review",
            "",
            f"- reviewed_by: `{payload.get('reviewed_by', '')}`",
            f"- reviewed_at: `{payload.get('reviewed_at', '')}`",
            f"- notes: {payload.get('notes', '')}",
            "",
        ]
    )
    return "\n".join(lines)


def write_plan_approval(
    iteration_dir: Path,
    *,
    iteration_id: str,
    approval_risk: dict[str, Any],
    plan_hash: str,
) -> dict[str, Any]:
    existing = load_plan_approval(iteration_dir)
    risk_fingerprint = build_risk_fingerprint(approval_risk)
    requires_approval = bool(approval_risk.get("requires_approval", False))
    counts = dict(approval_risk.get("counts", {}))
    policy_uat_required = _policy_uat_required(approval_risk)
    trigger_reasons = [
        str(item).strip()
        for item in approval_risk.get("trigger_reasons", [])
        if str(item).strip()
    ]

    preserved_status = ""
    preserved_reviewed_by = ""
    preserved_reviewed_at = ""
    preserved_notes = ""
    if approval_is_current(
        existing,
        plan_hash=plan_hash,
        risk_fingerprint=risk_fingerprint,
        require_approved=False,
    ):
        preserved_status = str(existing.get("status", "")).strip().lower()
        preserved_reviewed_by = str(existing.get("reviewed_by", "")).strip()
        preserved_reviewed_at = str(existing.get("reviewed_at", "")).strip()
        preserved_notes = str(existing.get("notes", "")).strip()

    if not requires_approval:
        status = "not_required"
        preserved_reviewed_by = ""
        preserved_reviewed_at = ""
        preserved_notes = ""
    elif preserved_status in {"approved", "retry", "stop"}:
        status = preserved_status
    else:
        status = "pending"

    payload = {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "iteration_id": iteration_id,
        "status": status,
        "requires_approval": requires_approval,
        "plan_hash": plan_hash,
        "risk_fingerprint": risk_fingerprint,
        "trigger_reasons": trigger_reasons,
        "counts": counts,
        "reviewed_by": preserved_reviewed_by,
        "reviewed_at": preserved_reviewed_at,
        "notes": preserved_notes,
        "source_paths": {
            "plan_contract": ".autolab/plan_contract.json",
            "plan_graph": ".autolab/plan_graph.json",
            "plan_check_result": ".autolab/plan_check_result.json",
        },
        "uat": _normalize_uat_block(
            existing.get("uat"),
            policy_required=policy_uat_required,
        ),
    }
    json_path = plan_approval_json_path(iteration_dir)
    md_path = plan_approval_markdown_path(iteration_dir)
    _write_json(json_path, payload)
    md_path.write_text(render_plan_approval_markdown(payload), encoding="utf-8")
    return payload


def record_plan_approval_decision(
    iteration_dir: Path,
    *,
    status: str,
    notes: str = "",
    reviewed_by: str = "",
    require_uat: bool = False,
) -> dict[str, Any]:
    payload = load_plan_approval(iteration_dir)
    if not payload:
        raise RuntimeError("plan_approval.json is missing")
    if str(payload.get("status", "")).strip().lower() == "not_required":
        raise RuntimeError("current plan does not require approval")
    normalized_status = str(status).strip().lower()
    if normalized_status not in {"approved", "retry", "stop"}:
        raise RuntimeError(f"invalid approval status '{status}'")
    if require_uat and normalized_status == "stop":
        raise RuntimeError("cannot mark UAT required when approval status is stop")
    payload["status"] = normalized_status
    payload["reviewed_at"] = _utc_now()
    payload["reviewed_by"] = reviewed_by or os.environ.get("USER", "")
    payload["notes"] = str(notes).strip()
    if require_uat:
        payload["uat"] = _normalize_uat_block(
            {"required_by": "plan_approval"},
            policy_required=_existing_policy_uat_required(payload),
        )
    json_path = plan_approval_json_path(iteration_dir)
    md_path = plan_approval_markdown_path(iteration_dir)
    _write_json(json_path, payload)
    md_path.write_text(render_plan_approval_markdown(payload), encoding="utf-8")
    return payload


def record_manual_uat_request(iteration_dir: Path) -> dict[str, Any]:
    payload = load_plan_approval(iteration_dir)
    if not payload:
        return {}
    payload["uat"] = _normalize_uat_block(
        {"required_by": "manual"},
        policy_required=_existing_policy_uat_required(payload),
    )
    json_path = plan_approval_json_path(iteration_dir)
    md_path = plan_approval_markdown_path(iteration_dir)
    _write_json(json_path, payload)
    md_path.write_text(render_plan_approval_markdown(payload), encoding="utf-8")
    return payload


def append_plan_approval_note(
    iteration_dir: Path,
    *,
    note: str,
    source_label: str = "oracle",
) -> dict[str, Any]:
    payload = load_plan_approval(iteration_dir)
    normalized_note = str(note).strip()
    normalized_label = str(source_label).strip() or "oracle"
    if not payload or not normalized_note:
        return payload

    existing_notes = str(payload.get("notes", "")).strip()
    existing_lines = [
        line.rstrip() for line in existing_notes.splitlines() if str(line).strip()
    ]
    duplicate = any(
        line.startswith(f"[{normalized_label} ") and line.endswith(normalized_note)
        for line in existing_lines
    )
    if duplicate:
        return payload

    existing_lines.append(f"[{normalized_label} {_utc_now()}] {normalized_note}")
    payload["notes"] = "\n".join(existing_lines)
    json_path = plan_approval_json_path(iteration_dir)
    md_path = plan_approval_markdown_path(iteration_dir)
    _write_json(json_path, payload)
    md_path.write_text(render_plan_approval_markdown(payload), encoding="utf-8")
    return payload
