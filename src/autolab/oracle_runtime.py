from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import sqlite3
from pathlib import Path
from typing import Any

from autolab.config import _load_effective_policy, _load_oracle_policy
from autolab.models import OraclePolicyConfig
from autolab.utils import _load_json_if_exists, _normalize_space, _utc_now, _write_json

ORACLE_ALLOWED_VERDICTS = (
    "continue_search",
    "switch_family",
    "rethink_design",
    "request_human_review",
    "stop_campaign",
)
ORACLE_PACKET_EPHEMERAL_KEYS = frozenset(
    {
        "generated_at",
        "oracle_epoch",
        "oracle_auto_eligible",
        "oracle_epoch_exhausted",
        "oracle_auto_status",
        "oracle_trigger_reason",
        "oracle_failure_reason",
        "oracle_verdict",
        "oracle_suggested_next_action",
        "oracle_recommended_human_review",
        "oracle_disfavored_family",
        "oracle_attempt_window",
    }
)
ORACLE_DETERMINISTIC_STAGE_BLOCKLIST = frozenset(
    {"launch", "slurm_monitor", "extract_results"}
)


@dataclass(frozen=True)
class OracleReply:
    verdict: str
    rationale: tuple[str, ...]
    recommended_actions: tuple[str, ...]
    risks: tuple[str, ...]
    suggested_next_action: str
    recommended_human_review: bool


def oracle_state_path(repo_root: Path) -> Path:
    return repo_root / ".autolab" / "oracle_state.json"


def oracle_last_response_path(repo_root: Path) -> Path:
    return repo_root / ".autolab" / "oracle_last_response.md"


def _default_oracle_auto_state() -> dict[str, Any]:
    return {
        "eligible": False,
        "attempted": False,
        "status": "not_attempted",
        "trigger_reason": "",
        "engine": "browser",
        "started_at": "",
        "completed_at": "",
        "failure_reason": "",
        "reply_path": "",
        "apply_status": "",
    }


def _default_oracle_state() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "current_epoch": "",
        "auto": _default_oracle_auto_state(),
        "verdict": "",
        "suggested_next_action": "",
        "recommended_human_review": False,
        "disfavored_family": "",
    }


def _normalize_oracle_auto_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    normalized = _default_oracle_auto_state()
    normalized["eligible"] = bool(payload.get("eligible", False))
    normalized["attempted"] = bool(payload.get("attempted", False))
    normalized["status"] = str(payload.get("status", "not_attempted")).strip()
    if not normalized["status"]:
        normalized["status"] = "not_attempted"
    normalized["trigger_reason"] = str(payload.get("trigger_reason", "")).strip()
    normalized["engine"] = str(payload.get("engine", "browser")).strip() or "browser"
    normalized["started_at"] = str(payload.get("started_at", "")).strip()
    normalized["completed_at"] = str(payload.get("completed_at", "")).strip()
    normalized["failure_reason"] = str(payload.get("failure_reason", "")).strip()
    normalized["reply_path"] = str(payload.get("reply_path", "")).strip()
    normalized["apply_status"] = str(payload.get("apply_status", "")).strip()
    return normalized


def normalize_oracle_state(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        payload = {}
    normalized = _default_oracle_state()
    normalized["schema_version"] = (
        str(payload.get("schema_version", "1.0")).strip() or "1.0"
    )
    normalized["current_epoch"] = str(payload.get("current_epoch", "")).strip()
    normalized["auto"] = _normalize_oracle_auto_state(payload.get("auto"))
    verdict = str(payload.get("verdict", "")).strip().lower()
    normalized["verdict"] = verdict if verdict in ORACLE_ALLOWED_VERDICTS else ""
    normalized["suggested_next_action"] = str(
        payload.get("suggested_next_action", "")
    ).strip()
    normalized["recommended_human_review"] = bool(
        payload.get("recommended_human_review", False)
    )
    normalized["disfavored_family"] = str(payload.get("disfavored_family", "")).strip()
    return normalized


def load_oracle_state(repo_root: Path) -> dict[str, Any]:
    return normalize_oracle_state(_load_json_if_exists(oracle_state_path(repo_root)))


def write_oracle_state(repo_root: Path, payload: dict[str, Any]) -> Path:
    normalized = normalize_oracle_state(payload)
    path = oracle_state_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(path, normalized)
    return path


def write_oracle_last_response(repo_root: Path, text: str) -> Path:
    path = oracle_last_response_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(text or "").rstrip() + "\n", encoding="utf-8")
    return path


def _stable_oracle_packet_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _stable_oracle_packet_payload(item)
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
            if str(key) not in ORACLE_PACKET_EPHEMERAL_KEYS
        }
    if isinstance(value, list):
        return [_stable_oracle_packet_payload(item) for item in value]
    return value


def oracle_packet_fingerprint(continuation_packet: dict[str, Any]) -> str:
    rendered = json.dumps(
        _stable_oracle_packet_payload(continuation_packet),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def oracle_epoch(
    *,
    scope_kind: str,
    scope_root: str,
    current_stage: str,
    revision_label: str,
    continuation_packet_fingerprint: str,
    campaign_id: str,
    champion_run_id: str,
) -> str:
    rendered = json.dumps(
        {
            "scope_kind": str(scope_kind).strip(),
            "scope_root": str(scope_root).strip(),
            "current_stage": str(current_stage).strip(),
            "revision_label": str(revision_label).strip(),
            "continuation_packet_fingerprint": str(
                continuation_packet_fingerprint
            ).strip(),
            "campaign_id": str(campaign_id).strip(),
            "champion_run_id": str(champion_run_id).strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def oracle_default_suggested_next_action(verdict: str) -> str:
    return {
        "continue_search": "Continue the current search with the current family.",
        "switch_family": "Avoid the current family on the next candidate.",
        "rethink_design": "Return to design before the next autonomous attempt.",
        "request_human_review": "Escalate to human_review for explicit human input.",
        "stop_campaign": "Stop the current campaign gracefully.",
    }.get(str(verdict).strip().lower(), "")


def oracle_stage_auto_allowed(
    repo_root: Path,
    *,
    stage: str,
    scope_kind: str,
) -> tuple[bool, OraclePolicyConfig]:
    effective = _load_effective_policy(repo_root, scope_kind=scope_kind, stage=stage)
    policy = _load_oracle_policy(repo_root, scope_kind=scope_kind, stage=stage)
    stage_contributed_oracle = any(
        source.layer == "stage" and "oracle" in source.keys_contributed
        for source in effective.sources
    )
    allowed = bool(policy.auto_allowed)
    if stage in ORACLE_DETERMINISTIC_STAGE_BLOCKLIST and not stage_contributed_oracle:
        allowed = False
    return (allowed, policy)


def build_oracle_context(
    repo_root: Path,
    *,
    scope_kind: str,
    scope_root: str,
    current_stage: str,
    continuation_packet: dict[str, Any],
    campaign_summary: dict[str, Any],
) -> dict[str, Any]:
    allowed, policy = oracle_stage_auto_allowed(
        repo_root,
        stage=current_stage,
        scope_kind=scope_kind,
    )
    packet_fingerprint = oracle_packet_fingerprint(continuation_packet)
    epoch = oracle_epoch(
        scope_kind=scope_kind,
        scope_root=scope_root,
        current_stage=current_stage,
        revision_label=str(
            campaign_summary.get("champion_revision_label", "unversioned-worktree")
        ).strip()
        or "unversioned-worktree",
        continuation_packet_fingerprint=packet_fingerprint,
        campaign_id=str(campaign_summary.get("campaign_id", "")).strip(),
        champion_run_id=str(campaign_summary.get("champion_run_id", "")).strip(),
    )
    state = load_oracle_state(repo_root)
    same_epoch = state.get("current_epoch") == epoch
    auto = (
        _normalize_oracle_auto_state(state.get("auto"))
        if same_epoch
        else _default_oracle_auto_state()
    )
    attempted = bool(auto.get("attempted", False))
    epoch_exhausted = attempted and policy.max_auto_attempts_per_epoch <= 1
    status = str(auto.get("status", "not_attempted")).strip() or "not_attempted"
    return {
        "oracle_epoch": epoch,
        "oracle_auto_eligible": bool(allowed and not epoch_exhausted),
        "oracle_epoch_exhausted": bool(epoch_exhausted),
        "oracle_auto_status": status,
        "oracle_trigger_reason": str(auto.get("trigger_reason", "")).strip(),
        "oracle_failure_reason": str(auto.get("failure_reason", "")).strip(),
        "oracle_verdict": str(state.get("verdict", "")).strip() if same_epoch else "",
        "oracle_suggested_next_action": str(
            state.get("suggested_next_action", "")
        ).strip()
        if same_epoch
        else "",
        "oracle_recommended_human_review": bool(
            state.get("recommended_human_review", False)
        )
        if same_epoch
        else False,
        "oracle_disfavored_family": str(state.get("disfavored_family", "")).strip()
        if same_epoch
        else "",
        "oracle_attempt_window": (
            f"{1 if attempted else 0}/{policy.max_auto_attempts_per_epoch} this epoch"
        ),
    }


def start_oracle_attempt(
    repo_root: Path,
    *,
    epoch: str,
    eligible: bool,
    trigger_reason: str,
) -> Path:
    state = load_oracle_state(repo_root)
    state["current_epoch"] = str(epoch).strip()
    state["auto"] = {
        "eligible": bool(eligible),
        "attempted": True,
        "status": "running",
        "trigger_reason": str(trigger_reason).strip(),
        "engine": "browser",
        "started_at": _utc_now(),
        "completed_at": "",
        "failure_reason": "",
        "reply_path": "",
        "apply_status": "",
    }
    state["verdict"] = ""
    state["suggested_next_action"] = ""
    state["recommended_human_review"] = False
    state["disfavored_family"] = ""
    return write_oracle_state(repo_root, state)


def finish_oracle_attempt(
    repo_root: Path,
    *,
    epoch: str,
    eligible: bool,
    status: str,
    trigger_reason: str,
    failure_reason: str = "",
    reply_path: str = "",
    apply_status: str = "",
    verdict: str = "",
    suggested_next_action: str = "",
    recommended_human_review: bool = False,
    disfavored_family: str = "",
) -> Path:
    state = load_oracle_state(repo_root)
    state["current_epoch"] = str(epoch).strip()
    state["auto"] = {
        "eligible": bool(eligible),
        "attempted": True,
        "status": str(status).strip() or "failed",
        "trigger_reason": str(trigger_reason).strip(),
        "engine": "browser",
        "started_at": str(state.get("auto", {}).get("started_at", "")).strip()
        or _utc_now(),
        "completed_at": _utc_now(),
        "failure_reason": str(failure_reason).strip(),
        "reply_path": str(reply_path).strip(),
        "apply_status": str(apply_status).strip(),
    }
    normalized_verdict = str(verdict).strip().lower()
    state["verdict"] = (
        normalized_verdict if normalized_verdict in ORACLE_ALLOWED_VERDICTS else ""
    )
    state["suggested_next_action"] = str(suggested_next_action).strip()
    state["recommended_human_review"] = bool(recommended_human_review)
    state["disfavored_family"] = str(disfavored_family).strip()
    return write_oracle_state(repo_root, state)


def oracle_profile_ready() -> bool:
    oracle_home = Path(os.environ.get("ORACLE_HOME_DIR", "~/.oracle")).expanduser()
    browser_profile = oracle_home / "browser-profile"
    local_state_path = browser_profile / "Local State"
    cookies_db = browser_profile / "Default" / "Cookies"
    sessions_dir = oracle_home / "sessions"
    if not browser_profile.is_dir() or not local_state_path.exists():
        return False
    if not cookies_db.exists():
        return False
    if not sessions_dir.is_dir():
        return False
    try:
        connection = sqlite3.connect(
            f"file:{cookies_db}?mode=ro",
            uri=True,
            timeout=1.0,
        )
    except sqlite3.Error:
        return False
    try:
        row = connection.execute(
            """
            SELECT 1
            FROM cookies
            WHERE host_key LIKE '%chatgpt.com'
               OR host_key LIKE '%openai.com'
            LIMIT 1
            """
        ).fetchone()
    except sqlite3.Error:
        return False
    finally:
        connection.close()
    return bool(row)


def build_oracle_roundtrip_request(
    *,
    handoff_payload: dict[str, Any],
    trigger_reason: str,
) -> str:
    _ = (handoff_payload, trigger_reason)
    verdicts = " | ".join(ORACLE_ALLOWED_VERDICTS)
    return "\n".join(
        [
            "You are an external technical reviewer.",
            "",
            "Review the attached handoff packet only.",
            "Do not browse the web or assume repository access outside the packet.",
            "A free-form review is acceptable.",
            f"If convenient, include a clear recommendation among: {verdicts}.",
            "Answer candidly and directly.",
        ]
    )


def _extract_markdown_section(markdown_text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## \S|\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(markdown_text)
    if not match:
        return ""
    return str(match.group("body") or "").strip()


def _parse_markdown_list(section_text: str) -> tuple[str, ...]:
    items: list[str] = []
    for raw_line in str(section_text or "").splitlines():
        text = raw_line.strip()
        if not text:
            continue
        text = re.sub(r"^[-*]\s+", "", text)
        text = re.sub(r"^\d+\.\s+", "", text)
        text = _normalize_space(text)
        if text:
            items.append(text)
    return tuple(items)


def parse_oracle_reply(reply_text: str) -> OracleReply:
    verdict_match = re.search(
        r"^ReviewerVerdict:\s*(?P<verdict>[A-Za-z_]+)\s*$",
        str(reply_text or ""),
        flags=re.MULTILINE,
    )
    if verdict_match is None:
        raise ValueError("reply is missing required ReviewerVerdict line")
    verdict = str(verdict_match.group("verdict") or "").strip().lower()
    if verdict not in ORACLE_ALLOWED_VERDICTS:
        raise ValueError(f"unsupported ReviewerVerdict '{verdict}'")
    rationale = _parse_markdown_list(_extract_markdown_section(reply_text, "Rationale"))
    recommended_actions = _parse_markdown_list(
        _extract_markdown_section(reply_text, "Recommended Actions")
    )
    risks = _parse_markdown_list(_extract_markdown_section(reply_text, "Risks"))
    suggested_next_action = (
        recommended_actions[0]
        if recommended_actions
        else oracle_default_suggested_next_action(verdict)
    )
    return OracleReply(
        verdict=verdict,
        rationale=rationale,
        recommended_actions=recommended_actions,
        risks=risks,
        suggested_next_action=suggested_next_action,
        recommended_human_review=verdict == "request_human_review",
    )
