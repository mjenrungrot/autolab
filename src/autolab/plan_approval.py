from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

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
    if not approval_requires_action(payload):
        return []
    return [
        "autolab approve-plan --status approve",
        "autolab approve-plan --status retry",
        "autolab approve-plan --status stop",
        "autolab run --execute-approved-plan",
    ]


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
) -> dict[str, Any]:
    payload = load_plan_approval(iteration_dir)
    if not payload:
        raise RuntimeError("plan_approval.json is missing")
    if str(payload.get("status", "")).strip().lower() == "not_required":
        raise RuntimeError("current plan does not require approval")
    normalized_status = str(status).strip().lower()
    if normalized_status not in {"approved", "retry", "stop"}:
        raise RuntimeError(f"invalid approval status '{status}'")
    payload["status"] = normalized_status
    payload["reviewed_at"] = _utc_now()
    payload["reviewed_by"] = reviewed_by or os.environ.get("USER", "")
    payload["notes"] = str(notes).strip()
    json_path = plan_approval_json_path(iteration_dir)
    md_path = plan_approval_markdown_path(iteration_dir)
    _write_json(json_path, payload)
    md_path.write_text(render_plan_approval_markdown(payload), encoding="utf-8")
    return payload
