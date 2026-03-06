from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from autolab.config import _load_plan_execution_config
from autolab.plan_approval import build_plan_hash, build_risk_fingerprint
from autolab.registry import load_registry
from autolab.sidecar_tools import parse_context_ref, resolve_context_ref
from autolab.state import _resolve_iteration_directory
from autolab.uat import derive_uat_required, load_uat_surface_patterns

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
_FAILURE_POLICIES = {"fail_fast"}


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


def _is_experiment_scoped_path(path: str) -> bool:
    normalized = _normalize_path(path)
    if not normalized:
        return False
    parts = normalized.split("/")
    return len(parts) >= 3 and parts[0] == "experiments"


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
) -> dict[str, dict[str, Any]]:
    requirements = design_payload.get("implementation_requirements")
    if not isinstance(requirements, list) or not requirements:
        errors.append(
            "design.yaml must include non-empty 'implementation_requirements' for implementation traceability"
        )
        return {}

    requirement_specs: dict[str, dict[str, Any]] = {}
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
        promoted_constraints: list[dict[str, str]] = []
        seen_promoted_ids: set[str] = set()
        raw_promoted_constraints = entry.get("promoted_constraints", [])
        if raw_promoted_constraints is None:
            raw_promoted_constraints = []
        if not isinstance(raw_promoted_constraints, list):
            errors.append(
                f"design.yaml implementation_requirements[{idx}] promoted_constraints must be a list"
            )
            raw_promoted_constraints = []
        for promoted_index, raw_promoted in enumerate(raw_promoted_constraints):
            if not isinstance(raw_promoted, dict):
                errors.append(
                    "design.yaml implementation_requirements"
                    f"[{idx}] promoted_constraints[{promoted_index}] must be a mapping"
                )
                continue
            promoted_id = str(raw_promoted.get("id", "")).strip()
            source_ref = str(raw_promoted.get("source_ref", "")).strip()
            if not promoted_id:
                errors.append(
                    "design.yaml implementation_requirements"
                    f"[{idx}] promoted_constraints[{promoted_index}] missing id"
                )
                continue
            if promoted_id in seen_promoted_ids:
                errors.append(
                    "design.yaml implementation_requirements"
                    f"[{idx}] duplicate promoted_constraints id '{promoted_id}'"
                )
                continue
            if not source_ref:
                errors.append(
                    "design.yaml implementation_requirements"
                    f"[{idx}] promoted_constraints[{promoted_index}] missing source_ref"
                )
                continue
            seen_promoted_ids.add(promoted_id)
            promoted_constraints.append(
                {
                    "id": promoted_id,
                    "source_ref": source_ref,
                }
            )
        seen.add(req_id)
        requirement_specs[req_id] = {
            "scope_kind": scope_kind,
            "promoted_constraints": promoted_constraints,
        }
    return requirement_specs


def _load_observed_retries(iteration_dir: Path) -> int:
    execution_state_path = iteration_dir / "plan_execution_state.json"
    if not execution_state_path.exists():
        return 0
    try:
        payload = _load_json_dict(
            execution_state_path, "plan_execution_state.json approval-risk source"
        )
    except PlanContractError:
        return 0
    observed_retries = 0
    for field in ("task_retry_counts", "wave_retry_counts"):
        raw_counts = payload.get(field)
        if not isinstance(raw_counts, dict):
            continue
        for value in raw_counts.values():
            try:
                observed_retries += max(int(value or 0), 0)
            except Exception:
                continue
    return observed_retries


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
    experiment_id = str(state.get("experiment_id", "")).strip()
    if not iteration_id:
        raise PlanContractError("iteration_id is required in state")

    iteration_dir, _iteration_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
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

    design_requirement_specs = _extract_design_requirements(
        design_payload, errors=errors
    )
    design_requirement_ids = list(design_requirement_specs)
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
    task_context_inputs: dict[str, list[str]] = {}
    task_promotion_source: dict[str, str] = {}
    task_promotion_scope_ok: dict[str, bool] = {}
    task_promoted_refs: dict[str, list[str]] = {}

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
        context_inputs = []
        if "context_inputs" in raw_task:
            context_inputs = _parse_str_list(
                task_id, raw_task, "context_inputs", errors=errors
            )
        promoted_refs: list[str] = []
        objective = str(raw_task.get("objective", "")).strip()
        if not objective:
            errors.append(f"{task_id}: objective must be non-empty")

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
        elif failure_policy not in _FAILURE_POLICIES:
            errors.append(
                f"{task_id}: failure_policy must be one of {sorted(_FAILURE_POLICIES)}"
            )

        can_run_in_parallel = raw_task.get("can_run_in_parallel")
        if not isinstance(can_run_in_parallel, bool):
            errors.append(f"{task_id}: can_run_in_parallel must be boolean")

        manual_only_rationale = str(raw_task.get("manual_only_rationale", "")).strip()
        if not verify_cmds and not manual_only_rationale:
            errors.append(
                f"{task_id}: verification_commands must be non-empty or manual_only_rationale must be provided"
            )

        if scope_kind == "project_wide":
            illegal_reads = [path for path in reads if _is_experiment_scoped_path(path)]
            if illegal_reads:
                errors.append(
                    f"{task_id}: project_wide task may not read experiment-scoped paths ({', '.join(sorted(set(illegal_reads)))})"
                )
            illegal_writes = [
                path for path in [*writes, *touches] if _is_experiment_scoped_path(path)
            ]
            if illegal_writes:
                errors.append(
                    f"{task_id}: project_wide task writes experiment-scoped paths ({', '.join(sorted(set(illegal_writes)))})"
                )
            for context_ref in context_inputs:
                parsed = parse_context_ref(context_ref)
                if parsed is None:
                    errors.append(
                        f"{task_id}: invalid context_inputs ref '{context_ref}'"
                    )
                    continue
                if (
                    parsed.get("kind") == "sidecar"
                    and parsed.get("scope_kind") == "experiment"
                ):
                    errors.append(
                        f"{task_id}: project_wide task may not reference experiment sidecar context '{context_ref}'"
                    )
                if (
                    parsed.get("kind") == "artifact"
                    and parsed.get("artifact_kind") == "context_delta"
                ):
                    errors.append(
                        f"{task_id}: project_wide task may not reference context_delta"
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
                errors.append(
                    f"{task_id}: experiment-scoped task touches project-wide paths ({', '.join(sorted(set(outside_paths)))})"
                )

        for context_ref in context_inputs:
            parsed = parse_context_ref(context_ref)
            if parsed is None:
                errors.append(f"{task_id}: invalid context_inputs ref '{context_ref}'")
                continue
            if parsed.get("kind") == "promoted":
                promoted_refs.append(context_ref)
            if (
                resolve_context_ref(
                    repo_root,
                    iteration_id=iteration_id,
                    experiment_id=experiment_id,
                    raw_ref=context_ref,
                    design_payload=design_payload,
                    scope_kind=scope_kind,
                )
                is None
            ):
                errors.append(
                    f"{task_id}: context_inputs ref '{context_ref}' could not be resolved"
                )

        promotion_source = str(raw_task.get("promotion_source", "")).strip()
        promotion_scope_ok = raw_task.get("promotion_scope_ok")
        if promotion_source:
            parsed = parse_context_ref(promotion_source)
            if parsed is None:
                errors.append(
                    f"{task_id}: promotion_source '{promotion_source}' is invalid"
                )
            elif (
                parsed.get("kind") != "sidecar"
                or parsed.get("scope_kind") != "experiment"
            ):
                errors.append(
                    f"{task_id}: promotion_source '{promotion_source}' must target an experiment sidecar item"
                )
            elif (
                resolve_context_ref(
                    repo_root,
                    iteration_id=iteration_id,
                    experiment_id=experiment_id,
                    raw_ref=promotion_source,
                    design_payload=design_payload,
                    scope_kind="experiment",
                )
                is None
            ):
                errors.append(
                    f"{task_id}: promotion_source '{promotion_source}' could not be resolved"
                )
            if not isinstance(promotion_scope_ok, bool) or not promotion_scope_ok:
                errors.append(
                    f"{task_id}: promotion_source requires promotion_scope_ok=true"
                )
        elif "promotion_scope_ok" in raw_task and not isinstance(
            promotion_scope_ok, bool
        ):
            errors.append(f"{task_id}: promotion_scope_ok must be boolean")

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
        task_context_inputs[task_id] = context_inputs
        task_promotion_source[task_id] = promotion_source
        task_promotion_scope_ok[task_id] = bool(promotion_scope_ok)
        task_promoted_refs[task_id] = promoted_refs

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
    scope_mismatch_requirement_mappings = 0
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
                continue
            expected_scope = str(
                design_requirement_specs.get(req_id, {}).get("scope_kind", "")
            ).strip()
            scope_mismatches = [
                task_id
                for task_id in mapped_tasks
                if task_scope.get(task_id, "") != expected_scope
            ]
            if scope_mismatches:
                scope_mismatch_requirement_mappings += 1
                errors.append(
                    f"design requirement '{req_id}' scope_kind={expected_scope} is mapped by wrong-scope task(s): {', '.join(sorted(scope_mismatches))}"
                )

    if (
        missing_requirement_mappings == 0
        and scope_mismatch_requirement_mappings == 0
        and design_requirement_ids
    ):
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
                "detail": (
                    f"{missing_requirement_mappings} requirement(s) are unmapped; "
                    f"{scope_mismatch_requirement_mappings} requirement(s) have wrong-scope task mappings"
                ),
            }
        )

    promotion_requirement_rows: list[dict[str, Any]] = []
    promotion_failures = 0
    task_illegal_promoted_refs = 0
    promoted_source_mismatches = 0
    for task_id, promoted_refs in task_promoted_refs.items():
        for promoted_ref in promoted_refs:
            parsed = parse_context_ref(promoted_ref)
            if parsed is None:
                continue
            requirement_id = str(parsed.get("requirement_id", "")).strip()
            if not requirement_id:
                continue
            if requirement_id not in task_covers_requirements.get(task_id, []):
                task_illegal_promoted_refs += 1
                errors.append(
                    f"{task_id}: promoted context ref '{promoted_ref}' requires covers_requirements to include '{requirement_id}'"
                )
        if promoted_refs and task_scope.get(task_id, "") == "project_wide":
            if not task_promotion_scope_ok.get(task_id, False):
                task_illegal_promoted_refs += 1
                errors.append(
                    f"{task_id}: project_wide task consuming promoted context requires promotion_scope_ok=true"
                )

    for req_id, requirement_spec in design_requirement_specs.items():
        expected_scope = str(requirement_spec.get("scope_kind", "")).strip()
        promoted_constraints = requirement_spec.get("promoted_constraints", [])
        if expected_scope != "project_wide" or not isinstance(
            promoted_constraints, list
        ):
            continue
        required_promoted_refs = [
            f"promoted:{req_id}:{str(item.get('id', '')).strip()}"
            for item in promoted_constraints
            if str(item.get("id", "")).strip()
        ]
        covering_task_ids = sorted(
            task_id
            for task_id, covered_requirements in task_covers_requirements.items()
            if req_id in covered_requirements
            and task_scope.get(task_id, "") == "project_wide"
        )
        consumed_promoted_refs = sorted(
            {
                context_ref
                for task_id in covering_task_ids
                for context_ref in task_promoted_refs.get(task_id, [])
                if context_ref.startswith(f"promoted:{req_id}:")
            }
        )
        missing_promoted_refs = sorted(
            ref for ref in required_promoted_refs if ref not in consumed_promoted_refs
        )
        illegal_task_refs = sorted(
            task_id
            for task_id, promoted_refs in task_promoted_refs.items()
            if any(ref.startswith(f"promoted:{req_id}:") for ref in promoted_refs)
            and req_id not in task_covers_requirements.get(task_id, [])
        )
        status = "pass"
        if not covering_task_ids:
            promotion_failures += 1
            status = "fail"
            errors.append(
                f"project_wide requirement '{req_id}' with promoted_constraints must be covered by at least one project_wide task"
            )
        if missing_promoted_refs:
            promotion_failures += 1
            status = "fail"
            errors.append(
                f"project_wide requirement '{req_id}' missing promoted context inputs: {', '.join(missing_promoted_refs)}"
            )
        if illegal_task_refs:
            promotion_failures += 1
            status = "fail"
            errors.append(
                f"project_wide requirement '{req_id}' has illegal promoted context consumers: {', '.join(illegal_task_refs)}"
            )
        promotion_requirement_rows.append(
            {
                "requirement_id": req_id,
                "scope_kind": expected_scope,
                "status": status,
                "promoted_constraint_ids": [
                    str(item.get("id", "")).strip()
                    for item in promoted_constraints
                    if str(item.get("id", "")).strip()
                ],
                "covering_task_ids": covering_task_ids,
                "consumed_promoted_refs": consumed_promoted_refs,
                "missing_promoted_refs": missing_promoted_refs,
                "illegal_task_refs": illegal_task_refs,
            }
        )
    for task_id, promotion_source in task_promotion_source.items():
        if not promotion_source:
            continue
        consumed_source_refs: set[str] = set()
        for promoted_ref in task_promoted_refs.get(task_id, []):
            parsed = parse_context_ref(promoted_ref)
            if parsed is None:
                continue
            requirement_id = str(parsed.get("requirement_id", "")).strip()
            promoted_id = str(parsed.get("item_id", "")).strip()
            requirement_spec = design_requirement_specs.get(requirement_id, {})
            promoted_constraints = requirement_spec.get("promoted_constraints", [])
            if not isinstance(promoted_constraints, list):
                continue
            for promoted_constraint in promoted_constraints:
                if str(promoted_constraint.get("id", "")).strip() != promoted_id:
                    continue
                source_ref = str(promoted_constraint.get("source_ref", "")).strip()
                if source_ref:
                    consumed_source_refs.add(source_ref)
        if promotion_source not in consumed_source_refs:
            promoted_source_mismatches += 1
            errors.append(
                f"{task_id}: promotion_source '{promotion_source}' must match a consumed promoted constraint source_ref"
            )

    if (
        promotion_failures == 0
        and task_illegal_promoted_refs == 0
        and promoted_source_mismatches == 0
    ):
        rule_results.append(
            {
                "rule": "promotion_completeness",
                "status": "pass",
                "detail": "promoted cross-scope constraints are fully covered by declared project_wide tasks",
            }
        )
    else:
        rule_results.append(
            {
                "rule": "promotion_completeness",
                "status": "fail",
                "detail": (
                    f"{promotion_failures} promoted requirement coverage issue(s), "
                    f"{task_illegal_promoted_refs} illegal promoted context usage issue(s), "
                    f"{promoted_source_mismatches} promotion_source mismatch(es)"
                ),
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
    plan_hash = build_plan_hash(
        contract_payload=contract_source if isinstance(contract_source, dict) else {},
        graph_payload=graph_payload,
    )

    try:
        approval_cfg = _load_plan_execution_config(repo_root).implementation.approval
    except Exception as exc:  # pragma: no cover - config parsing exercised elsewhere
        raise PlanContractError(str(exc)) from exc

    observed_retries = _load_observed_retries(iteration_dir)
    try:
        stage_attempt = max(int(state.get("stage_attempt", 0) or 0), 0)
    except Exception:
        stage_attempt = 0
    project_wide_task_ids = sorted(
        task_id
        for task_id, scope_kind in task_scope.items()
        if scope_kind == "project_wide"
    )
    project_wide_unique_paths = sorted(
        {
            path
            for task_id, surfaces in task_surfaces.items()
            if task_scope.get(task_id, "") == "project_wide"
            for path in surfaces
            if path
        }
    )
    trigger_reasons: list[str] = []
    if approval_cfg.enabled:
        if approval_cfg.require_for_project_wide_tasks and project_wide_task_ids:
            trigger_reasons.append("project_wide_tasks_present")
        if len(tasks) > approval_cfg.max_tasks_without_approval:
            trigger_reasons.append("task_count_exceeds_threshold")
        if len(wave_rows) > approval_cfg.max_waves_without_approval:
            trigger_reasons.append("wave_count_exceeds_threshold")
        if (
            len(project_wide_unique_paths)
            > approval_cfg.max_project_wide_paths_without_approval
        ):
            trigger_reasons.append("project_wide_blast_radius_exceeds_threshold")
        if approval_cfg.require_after_retries and (
            stage_attempt > 0 or observed_retries > 0
        ):
            trigger_reasons.append("prior_retries_observed")
    approval_risk = {
        "requires_approval": bool(trigger_reasons),
        "trigger_reasons": trigger_reasons,
        "counts": {
            "tasks_total": len(tasks),
            "waves_total": len(wave_rows),
            "project_wide_tasks": len(project_wide_task_ids),
            "project_wide_unique_paths": len(project_wide_unique_paths),
            "observed_retries": observed_retries,
            "stage_attempt": stage_attempt,
        },
        "project_wide_task_ids": project_wide_task_ids,
        "project_wide_unique_paths": project_wide_unique_paths,
        "policy": {
            "enabled": approval_cfg.enabled,
            "require_for_project_wide_tasks": approval_cfg.require_for_project_wide_tasks,
            "max_tasks_without_approval": approval_cfg.max_tasks_without_approval,
            "max_waves_without_approval": approval_cfg.max_waves_without_approval,
            "max_project_wide_paths_without_approval": approval_cfg.max_project_wide_paths_without_approval,
            "require_after_retries": approval_cfg.require_after_retries,
        },
        "plan_hash": plan_hash,
    }
    approval_risk["risk_flags"] = {
        "plan_approval_required": bool(trigger_reasons),
        "uat_required": derive_uat_required(
            "project_wide" if project_wide_task_ids else "experiment",
            project_wide_unique_paths,
            load_uat_surface_patterns(repo_root),
        ),
        "remote_profile_required": False,
    }
    approval_risk["risk_fingerprint"] = build_risk_fingerprint(approval_risk)
    promotion_checks = {
        "status": "pass"
        if promotion_failures == 0
        and task_illegal_promoted_refs == 0
        and promoted_source_mismatches == 0
        else "fail",
        "requirements": promotion_requirement_rows,
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
        "plan_hash": plan_hash,
        "promotion_checks": promotion_checks,
        "approval_risk": approval_risk,
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
        "plan_hash": plan_hash,
        "promotion_checks": promotion_checks,
        "approval_risk": approval_risk,
    }

    if passed:
        message = "implementation plan contract check passed"
    else:
        message = "implementation plan contract check failed: " + (
            errors[0] if errors else "unknown issue"
        )
    return (passed, message, details)
