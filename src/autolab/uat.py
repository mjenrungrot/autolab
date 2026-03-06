from __future__ import annotations

import fnmatch
import re
from pathlib import Path
from typing import Any

from autolab.constants import DEFAULT_UAT_SURFACE_PATTERNS
from autolab.models import StageCheckError
from autolab.scope import _detect_scope_kind_from_plan_contract
from autolab.utils import _detect_priority_host_mode, _load_json_if_exists

UAT_ALLOWED_STATUSES = {"pass", "needs_retry", "blocked"}
UAT_ALLOWED_CHECK_RESULTS = {"pass", "fail", "blocked"}
UAT_REQUIRED_BY_VALUES = {"none", "policy", "plan_approval", "manual"}

_UAT_STATUS_PATTERN = re.compile(r"^\s*UATStatus\s*:\s*(\S.*?)\s*$", re.IGNORECASE)
_UAT_CHECK_HEADING_PATTERN = re.compile(r"^\s*###\s+Check\b", re.IGNORECASE)
_UAT_FIELD_PATTERN = re.compile(
    r"^\s*-\s*(command|expected|observed|result)\s*:\s*(.*?)\s*$",
    re.IGNORECASE,
)


def uat_markdown_path(iteration_dir: Path) -> Path:
    return iteration_dir / "uat.md"


def normalize_uat_required_by(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in UAT_REQUIRED_BY_VALUES:
        return normalized
    return "none"


def load_uat_surface_patterns(repo_root: Path) -> list[str]:
    from autolab.config import _load_verifier_policy

    policy = _load_verifier_policy(repo_root)
    raw_patterns = policy.get("uat_surface_patterns")
    if isinstance(raw_patterns, list):
        patterns = [str(item).strip() for item in raw_patterns if str(item).strip()]
        if patterns:
            return patterns
    return list(DEFAULT_UAT_SURFACE_PATTERNS)


def derive_uat_required(
    scope_kind: str,
    project_wide_unique_paths: list[str],
    patterns: list[str],
) -> bool:
    if str(scope_kind).strip().lower() != "project_wide":
        return False
    for path in project_wide_unique_paths:
        normalized_path = str(path or "").strip().replace("\\", "/")
        if not normalized_path:
            continue
        for pattern in patterns:
            normalized_pattern = str(pattern or "").strip()
            if normalized_pattern and fnmatch.fnmatch(
                normalized_path, normalized_pattern
            ):
                return True
    return False


def _project_wide_paths_from_contract(contract_payload: dict[str, Any]) -> list[str]:
    tasks = contract_payload.get("tasks")
    if not isinstance(tasks, list):
        return []
    paths: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            continue
        if str(task.get("scope_kind", "")).strip().lower() != "project_wide":
            continue
        for field in ("writes", "touches"):
            raw_values = task.get(field)
            if not isinstance(raw_values, list):
                continue
            for raw in raw_values:
                candidate = str(raw or "").strip().replace("\\", "/")
                if candidate:
                    paths.add(candidate)
    return sorted(paths)


def resolve_project_wide_paths(
    repo_root: Path,
    iteration_dir: Path,
) -> tuple[str, list[str]]:
    scope_kind = _detect_scope_kind_from_plan_contract(
        repo_root=repo_root,
        iteration_dir=iteration_dir,
    )
    plan_check_payload = _load_json_if_exists(
        repo_root / ".autolab" / "plan_check_result.json"
    )
    if isinstance(plan_check_payload, dict):
        approval_risk = plan_check_payload.get("approval_risk")
        if isinstance(approval_risk, dict):
            raw_paths = approval_risk.get("project_wide_unique_paths")
            if isinstance(raw_paths, list):
                normalized_paths = [
                    str(item or "").strip().replace("\\", "/")
                    for item in raw_paths
                    if str(item or "").strip()
                ]
                return (scope_kind, sorted(set(normalized_paths)))

    contract_payload = _load_json_if_exists(
        repo_root / ".autolab" / "plan_contract.json"
    )
    if isinstance(contract_payload, dict):
        return (scope_kind, _project_wide_paths_from_contract(contract_payload))
    return (scope_kind, [])


def parse_uat_markdown(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    errors: list[str] = []

    status_matches = [_UAT_STATUS_PATTERN.match(line) for line in lines]
    normalized_status_matches = [match for match in status_matches if match]
    if len(normalized_status_matches) != 1:
        errors.append("expected exactly one 'UATStatus:' line")
        uat_status = ""
    else:
        uat_status = normalized_status_matches[0].group(1).strip().lower()
        if uat_status not in UAT_ALLOWED_STATUSES:
            errors.append(
                "UATStatus must be one of " + "|".join(sorted(UAT_ALLOWED_STATUSES))
            )

    sections: list[list[str]] = []
    current_section: list[str] = []
    for line in lines:
        if _UAT_CHECK_HEADING_PATTERN.match(line):
            if current_section:
                sections.append(current_section)
            current_section = [line]
            continue
        if current_section:
            current_section.append(line)
    if current_section:
        sections.append(current_section)

    if not sections:
        errors.append("expected at least one '### Check' section")

    checks: list[dict[str, str]] = []
    for index, section_lines in enumerate(sections, start=1):
        fields: dict[str, str] = {}
        duplicates: set[str] = set()
        heading = section_lines[0].strip()
        for line in section_lines[1:]:
            match = _UAT_FIELD_PATTERN.match(line)
            if not match:
                continue
            key = match.group(1).strip().lower()
            value = match.group(2).strip()
            if key in fields:
                duplicates.add(key)
            fields[key] = value
        missing = [
            field
            for field in ("command", "expected", "observed", "result")
            if not fields.get(field, "").strip()
        ]
        if missing:
            errors.append(
                f"{heading or f'check {index}'} missing field(s): {', '.join(missing)}"
            )
        if duplicates:
            errors.append(
                f"{heading or f'check {index}'} repeats field(s): {', '.join(sorted(duplicates))}"
            )
        result = fields.get("result", "").strip().lower()
        if result and result not in UAT_ALLOWED_CHECK_RESULTS:
            errors.append(
                f"{heading or f'check {index}'} result must be one of "
                + "|".join(sorted(UAT_ALLOWED_CHECK_RESULTS))
            )
        checks.append(fields)

    return {
        "status": "invalid" if errors else uat_status,
        "uat_status": uat_status,
        "check_count": len(sections),
        "checks": checks,
        "errors": errors,
    }


def summarize_uat_file(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        return {
            "status": "missing" if required else "not_required",
            "errors": ([f"{path} is required but missing"] if required else []),
            "check_count": 0,
            "checks": [],
        }

    parsed = parse_uat_markdown(path)
    return parsed


def resolve_uat_requirement(
    repo_root: Path,
    iteration_dir: Path,
    *,
    plan_approval_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    scope_kind, project_wide_paths = resolve_project_wide_paths(
        repo_root, iteration_dir
    )
    policy_required = derive_uat_required(
        scope_kind,
        project_wide_paths,
        load_uat_surface_patterns(repo_root),
    )

    raw_uat = {}
    if isinstance(plan_approval_payload, dict):
        maybe_uat = plan_approval_payload.get("uat")
        if isinstance(maybe_uat, dict):
            raw_uat = maybe_uat
    required_by = normalize_uat_required_by(raw_uat.get("required_by", ""))
    if policy_required:
        effective_required_by = "policy"
    elif required_by in {"plan_approval", "manual"}:
        effective_required_by = required_by
    elif uat_markdown_path(iteration_dir).exists():
        effective_required_by = "manual"
    else:
        effective_required_by = "none"

    effective_required = effective_required_by != "none"
    artifact_path = uat_markdown_path(iteration_dir)
    summary = summarize_uat_file(artifact_path, required=effective_required)
    return {
        "policy_required": policy_required,
        "effective_required": effective_required,
        "required_by": effective_required_by,
        "artifact_path": str(artifact_path),
        "scope_kind": scope_kind,
        "project_wide_paths": project_wide_paths,
        **summary,
    }


def ensure_uat_pass(
    repo_root: Path,
    iteration_dir: Path,
    *,
    stage_label: str,
    plan_approval_payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    summary = resolve_uat_requirement(
        repo_root,
        iteration_dir,
        plan_approval_payload=plan_approval_payload,
    )
    if not summary["effective_required"]:
        return summary

    status = str(summary.get("status", "")).strip().lower()
    path = str(summary.get("artifact_path", "")).strip()
    required_by = str(summary.get("required_by", "")).strip() or "policy"
    errors = summary.get("errors", [])
    first_error = str(errors[0]).strip() if isinstance(errors, list) and errors else ""

    if status == "pass":
        return summary
    if status == "missing":
        raise StageCheckError(
            f"{path} is required before {stage_label} because UAT is required ({required_by})"
        )
    if status == "invalid":
        detail = f": {first_error}" if first_error else ""
        raise StageCheckError(f"{path} is invalid{detail}")
    raise StageCheckError(
        f"{path} must set UATStatus: pass before {stage_label}; current status is '{status}'"
    )


def render_uat_template(
    *,
    iteration_id: str,
    scope_kind: str,
    required_by: str,
    revision_label: str,
    host_mode: str,
    remote_profile: str,
) -> str:
    return (
        "# User Acceptance Test\n\n"
        "UATStatus: needs_retry\n\n"
        "## Scope\n"
        f"- iteration_id: {iteration_id}\n"
        f"- scope_kind: {scope_kind}\n"
        f"- required_by: {required_by}\n\n"
        "## Preconditions\n"
        f"- revision_label: {revision_label}\n"
        f"- host_mode: {host_mode}\n"
        f"- remote_profile: {remote_profile}\n\n"
        "## Checks\n\n"
        "### Check 1 - replace_me\n"
        "- command: replace with the manual command or UI action\n"
        "- expected: describe the expected operator-visible outcome\n"
        "- observed: record what actually happened\n"
        "- result: blocked\n\n"
        "## Follow-ups\n"
        "- none\n"
    )


def resolve_uat_template_context(repo_root: Path) -> dict[str, str]:
    revision_label = "unversioned-worktree"
    try:
        from autolab.remote_profiles import (
            resolve_remote_profile,
            resolve_workspace_revision,
        )

        revision = resolve_workspace_revision(repo_root)
        revision_label = revision.label or revision_label
        host_mode = _detect_priority_host_mode()
        remote_profile = "none"
        if host_mode == "slurm":
            try:
                remote_profile = resolve_remote_profile(
                    repo_root,
                    host_mode=host_mode,
                ).name
            except Exception:
                remote_profile = "none"
        return {
            "revision_label": revision_label,
            "host_mode": host_mode,
            "remote_profile": remote_profile,
        }
    except Exception:
        return {
            "revision_label": revision_label,
            "host_mode": _detect_priority_host_mode(),
            "remote_profile": "none",
        }
