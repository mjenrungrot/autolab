from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from autolab.registry import load_registry
from autolab.state import _resolve_iteration_directory

_ROOT_SCOPED_PREFIXES = (
    ".autolab/",
    "docs/",
    "paper/",
    "src/",
    "scripts/",
    "tests/",
    "experiments/",
)

_SCOPE_KINDS = {"experiment", "project_wide"}


class PlanContractError(RuntimeError):
    """Raised when plan contract checking cannot run."""


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _normalize_path(raw: Any) -> str:
    text = str(raw or "").strip().replace("\\", "/")
    while "//" in text:
        text = text.replace("//", "/")
    if text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def _path_families(paths: list[str]) -> set[str]:
    families: set[str] = set()
    for raw in paths:
        normalized = _normalize_path(raw)
        if not normalized:
            continue
        head = normalized.split("/", 1)[0]
        if head:
            families.add(head)
    return families


def _paths_overlap(paths_a: list[str], paths_b: list[str]) -> bool:
    for raw_a in paths_a:
        a = _normalize_path(raw_a)
        if not a:
            continue
        for raw_b in paths_b:
            b = _normalize_path(raw_b)
            if not b:
                continue
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


def _is_iteration_local(path: str, *, iteration_prefix: str) -> bool:
    normalized = _normalize_path(path)
    prefix = _normalize_path(iteration_prefix)
    if not normalized or not prefix:
        return False
    return normalized == prefix or normalized.startswith(prefix + "/")


def _load_json_dict(path: Path, label: str) -> dict[str, Any]:
    if not path.exists():
        raise PlanContractError(f"{label} is missing at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - parse branch
        raise PlanContractError(f"{label} is not valid JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise PlanContractError(f"{label} must contain a JSON object at {path}")
    return payload


def _parse_str_list(
    task_id: str,
    raw_task: dict[str, Any],
    field: str,
    *,
    errors: list[str],
) -> list[str]:
    value = raw_task.get(field)
    if not isinstance(value, list):
        errors.append(f"{task_id}: field '{field}' must be a list")
        return []
    output: list[str] = []
    for idx, raw in enumerate(value):
        item = str(raw or "").strip()
        if not item:
            errors.append(f"{task_id}: field '{field}' has empty item at index {idx}")
            continue
        output.append(item)
    return output


def _extract_design_requirements(
    design_payload: dict[str, Any], *, errors: list[str]
) -> list[str]:
    requirements = design_payload.get("implementation_requirements")
    if not isinstance(requirements, list) or not requirements:
        errors.append(
            "design.yaml must include non-empty 'implementation_requirements' for implementation traceability"
        )
        return []

    requirement_ids: list[str] = []
    seen: set[str] = set()
    for idx, entry in enumerate(requirements):
        if not isinstance(entry, dict):
            errors.append(
                f"design.yaml implementation_requirements[{idx}] must be a mapping"
            )
            continue
        req_id = str(entry.get("requirement_id", "")).strip()
        description = str(entry.get("description", "")).strip()
        scope_kind = str(entry.get("scope_kind", "")).strip()
        if not req_id:
            errors.append(
                f"design.yaml implementation_requirements[{idx}] missing requirement_id"
            )
            continue
        if req_id in seen:
            errors.append(f"design.yaml duplicate requirement_id '{req_id}'")
            continue
        if not description:
            errors.append(
                f"design.yaml implementation_requirements[{idx}] missing description"
            )
        if scope_kind not in _SCOPE_KINDS:
            errors.append(
                f"design.yaml implementation_requirements[{idx}] scope_kind must be one of {sorted(_SCOPE_KINDS)}"
            )
        seen.add(req_id)
        requirement_ids.append(req_id)
    return requirement_ids


def _artifact_matches_required(
    required: str,
    *,
    expected_artifacts: set[str],
    iteration_prefix: str,
) -> bool:
    normalized = _normalize_path(required)
    if not normalized:
        return False

    candidates = {normalized}
    if not normalized.startswith(_ROOT_SCOPED_PREFIXES):
        candidates.add(_normalize_path(f"{iteration_prefix}/{normalized}"))
    for candidate in candidates:
        if candidate in expected_artifacts:
            return True
    return False


def _detect_cycles(deps_map: dict[str, list[str]]) -> list[str]:
    issues: list[str] = []
    visited: set[str] = set()
    stack: set[str] = set()

    def visit(node: str) -> None:
        if node in stack:
            issues.append(f"dependency cycle detected involving task '{node}'")
            return
        if node in visited:
            return
        visited.add(node)
        stack.add(node)
        for dep in deps_map.get(node, []):
            if dep in deps_map:
                visit(dep)
        stack.remove(node)

    for task_id in deps_map:
        visit(task_id)
    return sorted(set(issues))


def _compute_depths(deps_map: dict[str, list[str]]) -> dict[str, int]:
    memo: dict[str, int] = {}

    def depth(node: str) -> int:
        if node in memo:
            return memo[node]
        deps = deps_map.get(node, [])
        if not deps:
            memo[node] = 0
            return 0
        max_dep = 0
        for dep in deps:
            if dep in deps_map:
                max_dep = max(max_dep, depth(dep) + 1)
        memo[node] = max_dep
        return max_dep

    for task_id in deps_map:
        depth(task_id)
    return memo


def _write_validation_markdown(
    path: Path,
    *,
    passed: bool,
    errors: list[str],
    warnings: list[str],
) -> None:
    if passed:
        if path.exists():
            path.unlink()
        return

    lines: list[str] = [
        "# Implementation Validation",
        "",
        f"Generated: {_utc_now()}",
        "",
        "## Status",
        "- fail",
        "",
        "## Errors",
    ]
    if errors:
        lines.extend(f"- {item}" for item in errors)
    else:
        lines.append("- (none)")
    lines.extend(["", "## Warnings"])
    if warnings:
        lines.extend(f"- {item}" for item in warnings)
    else:
        lines.append("- (none)")
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def check_implementation_plan_contract(
    repo_root: Path,
    state: dict[str, Any],
    *,
    stage_override: str | None = None,
    write_outputs: bool = True,
) -> tuple[bool, str, dict[str, Any]]:
    stage = str(stage_override or state.get("stage", "")).strip()
    if stage != "implementation":
        details = {
            "status": "pass",
            "stage": stage,
            "errors": [],
            "warnings": [],
            "rule_results": [
                {
                    "rule": "stage_skip",
                    "status": "pass",
                    "detail": f"skipped for stage={stage}",
                }
            ],
        }
        return (
            True,
            f"implementation contract check skipped for stage {stage}",
            details,
        )

    iteration_id = str(state.get("iteration_id", "")).strip()
    if not iteration_id:
        raise PlanContractError("iteration_id is required in state")

    iteration_dir, _iteration_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=str(state.get("experiment_id", "")).strip(),
        require_exists=False,
    )
    try:
        iteration_prefix = iteration_dir.relative_to(repo_root).as_posix()
    except ValueError:
        iteration_prefix = f"experiments/plan/{iteration_id}"

    design_path = iteration_dir / "design.yaml"
    if yaml is None:
        raise PlanContractError("plan contract checking requires PyYAML")
    if not design_path.exists():
        raise PlanContractError(f"design.yaml is missing at {design_path}")
    try:
        design_payload = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover
        raise PlanContractError(
            f"could not parse design.yaml at {design_path}: {exc}"
        ) from exc
    if not isinstance(design_payload, dict):
        raise PlanContractError("design.yaml must contain a mapping")

    contract_path = repo_root / ".autolab" / "plan_contract.json"
    snapshot_path = iteration_dir / "plan_contract.json"
    check_result_path = repo_root / ".autolab" / "plan_check_result.json"
    graph_path = repo_root / ".autolab" / "plan_graph.json"
    validation_path = iteration_dir / "implementation_validation.md"

    errors: list[str] = []
    warnings: list[str] = []
    rule_results: list[dict[str, str]] = []

    design_requirement_ids = _extract_design_requirements(design_payload, errors=errors)
    if design_requirement_ids:
        rule_results.append(
            {
                "rule": "design_requirements_present",
                "status": "pass",
                "detail": f"found {len(design_requirement_ids)} design requirement(s)",
            }
        )
    else:
        rule_results.append(
            {
                "rule": "design_requirements_present",
                "status": "fail",
                "detail": "design implementation_requirements are missing/invalid",
            }
        )

    contract: dict[str, Any] = {}
    snapshot: dict[str, Any] = {}
    try:
        contract = _load_json_dict(contract_path, ".autolab/plan_contract.json")
    except PlanContractError as exc:
        errors.append(str(exc))

    try:
        snapshot = _load_json_dict(
            snapshot_path, "iteration plan_contract.json snapshot"
        )
    except PlanContractError as exc:
        errors.append(str(exc))

    if contract and snapshot:
        snapshot_tasks = snapshot.get("tasks")
        contract_tasks = contract.get("tasks")
        if snapshot_tasks != contract_tasks:
            errors.append(
                "plan_contract snapshot mismatch: experiments/.../plan_contract.json tasks differ from .autolab/plan_contract.json"
            )
        if (
            str(snapshot.get("iteration_id", "")).strip()
            != str(contract.get("iteration_id", "")).strip()
        ):
            errors.append(
                "plan_contract snapshot mismatch: iteration_id differs between snapshot and canonical contract"
            )

    contract_source = contract if contract else snapshot
    tasks_raw = (
        contract_source.get("tasks") if isinstance(contract_source, dict) else None
    )
    if not isinstance(tasks_raw, list) or not tasks_raw:
        errors.append("plan_contract must include non-empty 'tasks' list")
        tasks_raw = []

    schema_version = str(contract_source.get("schema_version", "")).strip()
    if schema_version != "1.0":
        errors.append("plan_contract schema_version must be '1.0'")

    contract_stage = str(contract_source.get("stage", "implementation")).strip()
    if contract_stage and contract_stage != "implementation":
        errors.append("plan_contract stage must be 'implementation'")

    contract_iteration_id = str(contract_source.get("iteration_id", "")).strip()
    if contract_iteration_id != iteration_id:
        errors.append(
            "plan_contract iteration_id must match .autolab/state.json iteration_id"
        )

    tasks: dict[str, dict[str, Any]] = {}
    deps_map: dict[str, list[str]] = {}
    task_surfaces: dict[str, list[str]] = {}
    task_conflict_groups: dict[str, str] = {}
    task_scope: dict[str, str] = {}
    task_reads: dict[str, list[str]] = {}
    task_writes: dict[str, list[str]] = {}
    task_expected_artifacts: dict[str, list[str]] = {}
    task_covers_requirements: dict[str, list[str]] = {}

    for idx, raw_task in enumerate(tasks_raw):
        if not isinstance(raw_task, dict):
            errors.append(f"task at index {idx} must be an object")
            continue
        task_id = str(raw_task.get("task_id", "")).strip()
        if not task_id:
            errors.append(f"task at index {idx} missing task_id")
            continue
        if task_id in tasks:
            errors.append(f"duplicate task_id '{task_id}'")
            continue

        scope_kind = str(raw_task.get("scope_kind", "")).strip()
        if scope_kind not in _SCOPE_KINDS:
            errors.append(
                f"{task_id}: scope_kind must be one of {sorted(_SCOPE_KINDS)}"
            )

        depends_on = _parse_str_list(task_id, raw_task, "depends_on", errors=errors)
        reads = _parse_str_list(task_id, raw_task, "reads", errors=errors)
        writes = _parse_str_list(task_id, raw_task, "writes", errors=errors)
        touches = _parse_str_list(task_id, raw_task, "touches", errors=errors)
        verify_cmds = _parse_str_list(
            task_id, raw_task, "verification_commands", errors=errors
        )
        expected_artifacts = _parse_str_list(
            task_id, raw_task, "expected_artifacts", errors=errors
        )
        covers_requirements = _parse_str_list(
            task_id, raw_task, "covers_requirements", errors=errors
        )

        if not touches:
            errors.append(f"{task_id}: touches must be non-empty")
        if not expected_artifacts:
            errors.append(f"{task_id}: expected_artifacts must be non-empty")
        if not covers_requirements:
            errors.append(f"{task_id}: covers_requirements must be non-empty")

        conflict_group = str(raw_task.get("conflict_group", "")).strip()
        failure_policy = str(raw_task.get("failure_policy", "")).strip()
        if not failure_policy:
            errors.append(f"{task_id}: failure_policy must be non-empty")

        can_run_in_parallel = raw_task.get("can_run_in_parallel")
        if not isinstance(can_run_in_parallel, bool):
            errors.append(f"{task_id}: can_run_in_parallel must be boolean")

        manual_only_rationale = str(raw_task.get("manual_only_rationale", "")).strip()
        if not verify_cmds and not manual_only_rationale:
            errors.append(
                f"{task_id}: verification_commands must be non-empty or manual_only_rationale must be provided"
            )

        if scope_kind == "project_wide":
            illegal_reads = [
                path
                for path in reads
                if _is_iteration_local(path, iteration_prefix=iteration_prefix)
            ]
            if illegal_reads:
                errors.append(
                    f"{task_id}: project_wide task may not read iteration-local outputs ({', '.join(sorted(set(illegal_reads)))})"
                )
            illegal_writes = [
                path
                for path in [*writes, *touches]
                if _is_iteration_local(path, iteration_prefix=iteration_prefix)
            ]
            if illegal_writes:
                warnings.append(
                    f"{task_id}: project_wide task writes iteration-local paths ({', '.join(sorted(set(illegal_writes)))})"
                )

        if scope_kind == "experiment":
            outside_paths = [
                path
                for path in [*writes, *touches]
                if _normalize_path(path)
                and not _is_iteration_local(path, iteration_prefix=iteration_prefix)
                and _normalize_path(path).startswith(_ROOT_SCOPED_PREFIXES)
            ]
            if outside_paths:
                warnings.append(
                    f"{task_id}: experiment-scoped task touches project-wide paths ({', '.join(sorted(set(outside_paths)))})"
                )

        families = _path_families([*reads, *writes, *touches])
        if len(families) > 4:
            warnings.append(
                f"{task_id}: task spans many concerns ({len(families)} path families)"
            )
        if len(touches) > 15:
            warnings.append(f"{task_id}: touches too many files ({len(touches)})")

        tasks[task_id] = raw_task
        deps_map[task_id] = depends_on
        task_surfaces[task_id] = sorted(set([*writes, *touches]))
        task_conflict_groups[task_id] = conflict_group
        task_scope[task_id] = scope_kind
        task_reads[task_id] = reads
        task_writes[task_id] = writes
        task_expected_artifacts[task_id] = expected_artifacts
        task_covers_requirements[task_id] = covers_requirements

    unknown_dependency_errors = 0
    for task_id, deps in deps_map.items():
        for dep in deps:
            if dep not in tasks:
                unknown_dependency_errors += 1
                errors.append(f"{task_id}: depends_on references unknown task '{dep}'")
    if unknown_dependency_errors == 0:
        rule_results.append(
            {
                "rule": "dependency_references",
                "status": "pass",
                "detail": "all depends_on references resolve to known tasks",
            }
        )
    else:
        rule_results.append(
            {
                "rule": "dependency_references",
                "status": "fail",
                "detail": f"{unknown_dependency_errors} dangling dependency reference(s)",
            }
        )

    cycle_errors = _detect_cycles(deps_map)
    if cycle_errors:
        errors.extend(cycle_errors)
        rule_results.append(
            {
                "rule": "dependency_cycles",
                "status": "fail",
                "detail": f"found {len(cycle_errors)} cycle issue(s)",
            }
        )
    else:
        rule_results.append(
            {
                "rule": "dependency_cycles",
                "status": "pass",
                "detail": "dependency graph is acyclic",
            }
        )

    for task_id, deps in deps_map.items():
        if task_scope.get(task_id) != "project_wide":
            continue
        illegal_deps = [dep for dep in deps if task_scope.get(dep) == "experiment"]
        if illegal_deps:
            errors.append(
                f"{task_id}: project_wide task may not depend on experiment-scoped tasks ({', '.join(sorted(set(illegal_deps)))})"
            )

    missing_requirement_mappings = 0
    if design_requirement_ids:
        requirement_to_tasks: dict[str, list[str]] = {
            req_id: [] for req_id in design_requirement_ids
        }
        known_requirements = set(design_requirement_ids)
        for task_id, covered in task_covers_requirements.items():
            for req_id in covered:
                if req_id not in known_requirements:
                    errors.append(
                        f"{task_id}: covers_requirements references unknown design requirement '{req_id}'"
                    )
                    continue
                requirement_to_tasks[req_id].append(task_id)

        for req_id, mapped_tasks in requirement_to_tasks.items():
            if not mapped_tasks:
                missing_requirement_mappings += 1
                errors.append(
                    f"design requirement '{req_id}' is not mapped by any plan task"
                )

    if missing_requirement_mappings == 0 and design_requirement_ids:
        rule_results.append(
            {
                "rule": "design_requirement_mapping",
                "status": "pass",
                "detail": "every design requirement maps to at least one task",
            }
        )
    elif design_requirement_ids:
        rule_results.append(
            {
                "rule": "design_requirement_mapping",
                "status": "fail",
                "detail": f"{missing_requirement_mappings} requirement(s) are unmapped",
            }
        )

    depths = _compute_depths(deps_map) if tasks and not cycle_errors else {}

    # Build wave bins for same-wave conflict checking.
    wave_bins: dict[str, list[str]] = {}
    for task_id in sorted(tasks):
        can_parallel = bool(tasks[task_id].get("can_run_in_parallel", False))
        depth = depths.get(task_id, 0)
        if can_parallel:
            key = f"d{depth}"
        else:
            key = f"s{depth}:{task_id}"
        wave_bins.setdefault(key, []).append(task_id)

    wave_conflicts = 0
    for key, task_ids in wave_bins.items():
        if len(task_ids) < 2:
            continue
        for idx, left in enumerate(task_ids):
            for right in task_ids[idx + 1 :]:
                if _paths_overlap(
                    task_surfaces.get(left, []), task_surfaces.get(right, [])
                ):
                    wave_conflicts += 1
                    errors.append(
                        f"same-wave write conflict: tasks {left} and {right} overlap in writes/touches"
                    )
                left_group = task_conflict_groups.get(left, "")
                right_group = task_conflict_groups.get(right, "")
                if left_group and right_group and left_group == right_group:
                    wave_conflicts += 1
                    errors.append(
                        f"same-wave conflict_group collision: tasks {left} and {right} share '{left_group}'"
                    )

    if wave_conflicts == 0:
        rule_results.append(
            {
                "rule": "same_wave_conflicts",
                "status": "pass",
                "detail": "no same-wave write/conflict-group collisions",
            }
        )
    else:
        rule_results.append(
            {
                "rule": "same_wave_conflicts",
                "status": "fail",
                "detail": f"detected {wave_conflicts} same-wave conflict(s)",
            }
        )

    expected_artifacts_union = {
        _normalize_path(path)
        for artifacts in task_expected_artifacts.values()
        for path in artifacts
        if _normalize_path(path)
    }
    required_outputs: tuple[str, ...] = ()
    registry = load_registry(repo_root)
    implementation_spec = registry.get("implementation")
    if implementation_spec is not None:
        required_outputs = tuple(implementation_spec.required_outputs)

    missing_required_artifacts = 0
    for output in required_outputs:
        if not _artifact_matches_required(
            output,
            expected_artifacts=expected_artifacts_union,
            iteration_prefix=iteration_prefix,
        ):
            missing_required_artifacts += 1
            errors.append(
                f"expected_artifacts do not cover implementation required output '{output}'"
            )

    if missing_required_artifacts == 0:
        rule_results.append(
            {
                "rule": "required_artifact_alignment",
                "status": "pass",
                "detail": "task expected_artifacts cover implementation required outputs",
            }
        )
    else:
        rule_results.append(
            {
                "rule": "required_artifact_alignment",
                "status": "fail",
                "detail": f"{missing_required_artifacts} required output(s) missing from task expected_artifacts",
            }
        )

    if len(tasks) > 20:
        warnings.append(
            f"soft-limit warning: plan has {len(tasks)} tasks; consider splitting concerns"
        )
    all_touches_count = sum(len(paths) for paths in task_surfaces.values())
    if all_touches_count > 120:
        warnings.append(
            f"soft-limit warning: plan touches many files ({all_touches_count})"
        )

    sorted_wave_items = sorted(
        wave_bins.items(),
        key=lambda item: (
            0 if item[0].startswith("d") else 1,
            int(item[0][1:].split(":", 1)[0]),
            item[0],
        ),
    )
    wave_rows: list[dict[str, Any]] = []
    for idx, (_key, task_ids) in enumerate(sorted_wave_items, start=1):
        wave_rows.append({"wave": idx, "tasks": sorted(task_ids)})

    graph_payload = {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "stage": "implementation",
        "iteration_id": iteration_id,
        "nodes": [
            {
                "task_id": task_id,
                "scope_kind": task_scope.get(task_id, ""),
                "depth": int(depths.get(task_id, 0)),
                "can_run_in_parallel": bool(
                    tasks[task_id].get("can_run_in_parallel", False)
                ),
                "conflict_group": task_conflict_groups.get(task_id, ""),
            }
            for task_id in sorted(tasks)
        ],
        "edges": [
            {"from": dep, "to": task_id}
            for task_id in sorted(deps_map)
            for dep in sorted(deps_map.get(task_id, []))
            if dep in tasks
        ],
        "waves": wave_rows,
    }

    passed = not errors
    try:
        snapshot_rel = snapshot_path.relative_to(repo_root).as_posix()
    except ValueError:
        snapshot_rel = snapshot_path.as_posix()

    check_result_payload = {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "stage": "implementation",
        "iteration_id": iteration_id,
        "passed": passed,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "rule_results": rule_results,
        "artifacts": {
            "contract_path": ".autolab/plan_contract.json",
            "snapshot_path": _normalize_path(snapshot_rel),
            "plan_check_result_path": ".autolab/plan_check_result.json",
            "plan_graph_path": ".autolab/plan_graph.json",
        },
    }

    if write_outputs:
        check_result_path.parent.mkdir(parents=True, exist_ok=True)
        check_result_path.write_text(
            json.dumps(check_result_payload, indent=2) + "\n", encoding="utf-8"
        )
        graph_path.parent.mkdir(parents=True, exist_ok=True)
        graph_path.write_text(
            json.dumps(graph_payload, indent=2) + "\n", encoding="utf-8"
        )
        _write_validation_markdown(
            validation_path,
            passed=passed,
            errors=errors,
            warnings=warnings,
        )

    details = {
        "status": "pass" if passed else "fail",
        "stage": "implementation",
        "errors": errors,
        "warnings": warnings,
        "rule_results": rule_results,
        "check_result": check_result_payload,
        "plan_graph": graph_payload,
    }

    if passed:
        message = "implementation plan contract check passed"
    else:
        message = "implementation plan contract check failed: " + (
            errors[0] if errors else "unknown issue"
        )
    return (passed, message, details)
