from __future__ import annotations

from pathlib import Path
from typing import Any

_SKILL_INSTALL_ROOT_BY_PROVIDER = {
    "codex": ".codex",
    "claude": ".claude",
}

_DETERMINISTIC_RUNTIME_STAGES = {"launch", "slurm_monitor", "extract_results"}

ROLE_REGISTRY: dict[str, dict[str, Any]] = {
    "researcher": {
        "skill_names": {"codex": "researcher"},
        "summary": "Investigate unresolved questions using only repository-local evidence.",
    },
    "planner": {
        "skill_names": {"codex": "planner"},
        "summary": "Turn implementation scope into verifiable, dependency-safe tasks.",
    },
    "plan_checker": {
        "skill_names": {"codex": "plan-checker"},
        "summary": "Critique plan scope, dependencies, and verification coverage before execution.",
    },
    "reviewer": {
        "skill_names": {"codex": "reviewer"},
        "summary": "Review evidence and required checks before pass/retry decisions.",
    },
}


def _normalize_provider(provider: str) -> str:
    candidate = str(provider or "").strip().lower()
    if candidate in _SKILL_INSTALL_ROOT_BY_PROVIDER:
        return candidate
    return ""


def infer_agent_surface_provider(
    command_argv: list[str] | tuple[str, ...] | None,
) -> str:
    if not isinstance(command_argv, (list, tuple)) or not command_argv:
        return ""
    head = Path(str(command_argv[0]).strip()).name.lower()
    if "codex" in head:
        return "codex"
    if "claude" in head:
        return "claude"
    return ""


def _resolve_recommended_role_ids(
    *,
    stage: str,
    command_name: str,
    assistant_cycle_stage: str,
    task_packet_mode: bool,
) -> list[str]:
    if task_packet_mode:
        return []
    normalized_command = str(command_name).strip().lower()
    if normalized_command == "research":
        return ["researcher"]
    normalized_assistant_stage = str(assistant_cycle_stage).strip().lower()
    if normalized_assistant_stage == "review":
        return ["reviewer"]
    normalized_stage = str(stage).strip().lower()
    if normalized_stage in _DETERMINISTIC_RUNTIME_STAGES:
        return []
    if normalized_stage == "implementation":
        return ["planner", "plan_checker"]
    if normalized_stage in {"implementation_review", "human_review"}:
        return ["reviewer"]
    return []


def _skill_install_path(repo_root: Path, provider: str, skill_name: str) -> Path:
    return (
        repo_root
        / _SKILL_INSTALL_ROOT_BY_PROVIDER[provider]
        / "skills"
        / skill_name
        / "SKILL.md"
    )


def resolve_agent_surface(
    repo_root: Path,
    *,
    provider: str,
    stage: str = "",
    command_name: str = "",
    assistant_cycle_stage: str = "",
    task_packet_mode: bool = False,
) -> dict[str, Any]:
    normalized_provider = _normalize_provider(provider)
    recommended_role_ids = _resolve_recommended_role_ids(
        stage=stage,
        command_name=command_name,
        assistant_cycle_stage=assistant_cycle_stage,
        task_packet_mode=task_packet_mode,
    )
    role_rows: list[dict[str, Any]] = []
    available_roles: list[str] = []

    for role_id, metadata in ROLE_REGISTRY.items():
        provider_skill_name = ""
        installed = False
        skill_path = ""
        if normalized_provider:
            provider_skill_name = str(
                metadata.get("skill_names", {}).get(normalized_provider, "")
            ).strip()
            if provider_skill_name:
                path = _skill_install_path(
                    repo_root, normalized_provider, provider_skill_name
                )
                skill_path = str(path)
                installed = path.exists() and path.is_file()
        if installed:
            available_roles.append(role_id)
        role_rows.append(
            {
                "id": role_id,
                "provider": normalized_provider,
                "skill_name": provider_skill_name,
                "installed": installed,
                "skill_path": skill_path,
                "summary": str(metadata.get("summary", "")).strip(),
                "recommended": role_id in recommended_role_ids,
            }
        )

    primary_role = (
        next(
            (row for row in role_rows if row["id"] == recommended_role_ids[0]),
            {},
        )
        if recommended_role_ids
        else {}
    )
    secondary_roles = [
        row for row in role_rows if row["id"] in set(recommended_role_ids[1:])
    ]
    invocation_hints = [
        f"${row['skill_name']}"
        for row in [primary_role, *secondary_roles]
        if isinstance(row, dict)
        and row.get("installed")
        and str(row.get("skill_name", "")).strip()
    ]
    return {
        "provider": normalized_provider or "unavailable",
        "available": bool(normalized_provider),
        "available_roles": available_roles,
        "recommended_roles": recommended_role_ids,
        "primary_role": primary_role,
        "secondary_roles": secondary_roles,
        "roles": role_rows,
        "invocation_hints": invocation_hints,
        "fallback_mode": "skill_hint" if invocation_hints else "plain_guidance",
    }


def build_agent_surface_guidance(surface: dict[str, Any]) -> dict[str, list[str]]:
    if not isinstance(surface, dict):
        return {"stage_context_lines": [], "brief_items": []}
    recommended = surface.get("recommended_roles")
    if not isinstance(recommended, list) or not recommended:
        return {"stage_context_lines": [], "brief_items": []}

    provider = str(surface.get("provider", "")).strip() or "unavailable"
    available_roles = surface.get("available_roles")
    available_roles_text = (
        ", ".join(str(item).strip() for item in available_roles if str(item).strip())
        if isinstance(available_roles, list)
        else ""
    )
    if not available_roles_text:
        available_roles_text = "none"

    primary_role = surface.get("primary_role")
    if not isinstance(primary_role, dict):
        primary_role = {}
    secondary_roles = surface.get("secondary_roles")
    if not isinstance(secondary_roles, list):
        secondary_roles = []

    lines = [
        f"- semantic_agent_provider: {provider}",
        f"- semantic_agent_available_roles: {available_roles_text}",
    ]
    brief_items: list[str] = []

    def _render_role(role: dict[str, Any], *, label: str) -> str:
        role_id = str(role.get("id", "")).strip()
        summary = str(role.get("summary", "")).strip()
        skill_name = str(role.get("skill_name", "")).strip()
        installed = bool(role.get("installed"))
        skill_hint = f" (${skill_name})" if installed and skill_name else ""
        fallback = "" if installed else " [fallback prose only]"
        return f"- {label}: {role_id}{skill_hint} - {summary}{fallback}"

    if primary_role:
        lines.append(_render_role(primary_role, label="semantic_agent_primary"))
        role_id = str(primary_role.get("id", "")).strip()
        skill_name = str(primary_role.get("skill_name", "")).strip()
        installed = bool(primary_role.get("installed"))
        if installed and skill_name:
            brief_items.append(f"semantic role: {role_id} via ${skill_name}")
        else:
            brief_items.append(f"semantic role: {role_id} with inline guidance only")

    for role in secondary_roles:
        if not isinstance(role, dict):
            continue
        lines.append(_render_role(role, label="semantic_agent_secondary"))
        role_id = str(role.get("id", "")).strip()
        skill_name = str(role.get("skill_name", "")).strip()
        installed = bool(role.get("installed"))
        if installed and skill_name:
            brief_items.append(f"secondary role: {role_id} via ${skill_name}")
        else:
            brief_items.append(f"secondary role: {role_id} with inline guidance only")

    return {
        "stage_context_lines": lines,
        "brief_items": brief_items,
    }


__all__ = [
    "ROLE_REGISTRY",
    "build_agent_surface_guidance",
    "infer_agent_surface_provider",
    "resolve_agent_surface",
]
