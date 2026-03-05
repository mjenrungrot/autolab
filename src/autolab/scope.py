from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from autolab.constants import DEFAULT_PROJECT_WIDE_ROOT
from autolab.models import StageCheckError
from autolab.state import _resolve_iteration_directory
from autolab.utils import _load_json_if_exists


def _load_scope_roots(repo_root: Path) -> dict[str, Any]:
    policy_path = repo_root / ".autolab" / "verifier_policy.yaml"
    if yaml is None or not policy_path.exists():
        return {}
    try:
        loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    scope_roots = loaded.get("scope_roots")
    if not isinstance(scope_roots, dict):
        return {}
    return scope_roots


def _resolve_project_wide_root(
    repo_root: Path, *, scope_roots: dict[str, Any] | None = None
) -> Path:
    config_scope_roots = (
        scope_roots if isinstance(scope_roots, dict) else _load_scope_roots(repo_root)
    )
    raw_value = str(
        config_scope_roots.get("project_wide_root", DEFAULT_PROJECT_WIDE_ROOT)
    ).strip()
    if not raw_value:
        raise StageCheckError("scope_roots.project_wide_root must be non-empty")

    candidate = Path(raw_value)
    if candidate.is_absolute():
        raise StageCheckError(
            "scope_roots.project_wide_root must be repo-relative, not absolute"
        )
    if any(part == ".." for part in candidate.parts):
        raise StageCheckError(
            "scope_roots.project_wide_root must not traverse parent directories"
        )

    resolved_repo_root = repo_root.resolve()
    resolved_root = (repo_root / candidate).resolve()
    try:
        resolved_root.relative_to(resolved_repo_root)
    except ValueError as exc:
        raise StageCheckError(
            "scope_roots.project_wide_root must resolve inside repo root"
        ) from exc
    if not resolved_root.exists() or not resolved_root.is_dir():
        raise StageCheckError(
            (
                "scope_roots.project_wide_root must point to an existing directory "
                f"(got '{raw_value}')"
            )
        )
    return resolved_root


def _detect_scope_kind_from_plan_contract(
    *,
    repo_root: Path,
    iteration_dir: Path | None,
) -> str:
    if iteration_dir is None:
        return "experiment"
    plan_contract_paths = [
        repo_root / ".autolab" / "plan_contract.json",
        iteration_dir / "plan_contract.json",
    ]
    for path in plan_contract_paths:
        payload = _load_json_if_exists(path)
        if not isinstance(payload, dict):
            continue
        tasks = payload.get("tasks")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            scope_kind = str(task.get("scope_kind", "")).strip().lower()
            if scope_kind == "project_wide":
                return "project_wide"
    return "experiment"


def _resolve_scope_context(
    repo_root: Path,
    *,
    iteration_id: str,
    experiment_id: str = "",
    scope_kind: str = "",
) -> tuple[str, Path, Path | None]:
    normalized_scope_kind = str(scope_kind).strip().lower()
    if normalized_scope_kind not in {"experiment", "project_wide"}:
        normalized_scope_kind = ""

    iteration_dir: Path | None = None
    if iteration_id:
        try:
            iteration_dir, _ = _resolve_iteration_directory(
                repo_root,
                iteration_id=iteration_id,
                experiment_id=experiment_id,
                require_exists=False,
            )
        except Exception:
            iteration_dir = None

    effective_scope_kind = (
        normalized_scope_kind
        or _detect_scope_kind_from_plan_contract(
            repo_root=repo_root,
            iteration_dir=iteration_dir,
        )
    )
    if effective_scope_kind == "project_wide":
        scope_root = _resolve_project_wide_root(repo_root)
    else:
        scope_root = iteration_dir if iteration_dir is not None else repo_root
    return (effective_scope_kind, scope_root, iteration_dir)
