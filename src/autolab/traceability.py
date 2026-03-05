from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from autolab.state import _resolve_iteration_directory
from autolab.utils import _load_json_if_exists, _safe_read_text, _utc_now, _write_json


_TRACEABILITY_SCHEMA_VERSION = "1.0"
_CLAIM_ID = "C1"


@dataclass(frozen=True)
class TraceabilityBuildResult:
    coverage_payload: dict[str, Any]
    coverage_path: Path
    latest_payload: dict[str, Any]
    latest_path: Path


def _relative_path(repo_root: Path, path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.relative_to(repo_root).as_posix()
    except ValueError:
        return path.as_posix()


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    payload = _load_json_if_exists(path)
    if isinstance(payload, dict):
        return payload
    return None


def _load_yaml_dict(path: Path) -> tuple[dict[str, Any] | None, str]:
    if yaml is None:
        return (None, f"PyYAML unavailable; cannot parse {path}")
    if not path.exists():
        return (None, f"missing {path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return (None, f"invalid YAML at {path}: {exc}")
    if not isinstance(loaded, dict):
        return (None, f"expected YAML mapping at {path}")
    return (loaded, "")


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _coerce_int(value: Any, *, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def _normalize_pointer(value: Any) -> str:
    normalized = str(value or "").strip().replace("\\", "/").lower()
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def _contains_identifier_token(text: str, token: str) -> bool:
    normalized_text = str(text or "").lower()
    normalized_token = str(token or "").strip()
    if not normalized_text or not normalized_token:
        return False
    pattern = re.compile(
        rf"(?<![a-z0-9_]){re.escape(normalized_token.lower())}(?![a-z0-9_])"
    )
    return pattern.search(normalized_text) is not None


def _pointers_match(pointer: str, target: str) -> bool:
    normalized_pointer = _normalize_pointer(pointer)
    normalized_target = _normalize_pointer(target)
    if not normalized_pointer or not normalized_target:
        return False
    if normalized_pointer == normalized_target:
        return True
    return normalized_pointer.endswith(
        f"/{normalized_target}"
    ) or normalized_target.endswith(f"/{normalized_pointer}")


def _extract_markdown_section(text: str, heading: str) -> str:
    lines = text.splitlines()
    target = heading.strip().lower()
    collecting = False
    collected: list[str] = []

    for raw_line in lines:
        line = raw_line.rstrip("\n")
        stripped = line.strip()
        if stripped.startswith("#"):
            normalized = stripped.lstrip("#").strip().lower()
            if collecting and normalized != target:
                break
            collecting = normalized == target
            continue
        if collecting:
            collected.append(line)
    return "\n".join(collected).strip()


def _extract_canonical_claim(hypothesis_text: str) -> str:
    text = str(hypothesis_text or "").strip()
    if not text:
        return ""

    section = _extract_markdown_section(text, "Hypothesis Statement")
    candidate = section or text

    for paragraph in re.split(r"\n\s*\n", candidate):
        lines = [line.strip() for line in paragraph.splitlines() if line.strip()]
        if not lines:
            continue
        filtered = [
            line
            for line in lines
            if not line.startswith("#")
            and not line.lower().startswith("primarymetric:")
        ]
        if not filtered:
            continue
        merged = " ".join(filtered)
        if merged.startswith("- "):
            merged = merged[2:].strip()
        if merged.startswith("* "):
            merged = merged[2:].strip()
        claim = " ".join(merged.split())
        if claim:
            return claim[:600]
    return ""


def _extract_requirements(design_payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_requirements = design_payload.get("implementation_requirements")
    if not isinstance(raw_requirements, list):
        return []
    rows: list[dict[str, Any]] = []
    for entry in raw_requirements:
        if not isinstance(entry, dict):
            continue
        requirement_id = str(entry.get("requirement_id", "")).strip()
        if not requirement_id:
            continue
        expected_artifacts = entry.get("expected_artifacts", [])
        if not isinstance(expected_artifacts, list):
            expected_artifacts = []
        rows.append(
            {
                "requirement_id": requirement_id,
                "description": str(entry.get("description", "")).strip(),
                "scope_kind": str(entry.get("scope_kind", "")).strip(),
                "expected_artifacts": [
                    str(item).strip()
                    for item in expected_artifacts
                    if str(item).strip()
                ],
            }
        )
    return rows


def _extract_plan_mappings(
    plan_contract_payload: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    tasks_raw = plan_contract_payload.get("tasks")
    if not isinstance(tasks_raw, list):
        return ({}, {})

    tasks: dict[str, dict[str, Any]] = {}
    requirement_to_tasks: dict[str, list[str]] = {}
    for entry in tasks_raw:
        if not isinstance(entry, dict):
            continue
        task_id = str(entry.get("task_id", "")).strip()
        if not task_id:
            continue
        tasks[task_id] = entry
        covers = entry.get("covers_requirements", [])
        if not isinstance(covers, list):
            covers = []
        for raw_requirement_id in covers:
            requirement_id = str(raw_requirement_id).strip()
            if not requirement_id:
                continue
            requirement_to_tasks.setdefault(requirement_id, [])
            if task_id not in requirement_to_tasks[requirement_id]:
                requirement_to_tasks[requirement_id].append(task_id)
    return (tasks, requirement_to_tasks)


def _extract_task_details(
    plan_execution_summary_payload: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    raw_rows = plan_execution_summary_payload.get("task_details")
    if not isinstance(raw_rows, list):
        return {}
    rows: dict[str, dict[str, Any]] = {}
    for entry in raw_rows:
        if not isinstance(entry, dict):
            continue
        task_id = str(entry.get("task_id", "")).strip()
        if not task_id:
            continue
        rows[task_id] = entry
    return rows


def _resolve_metrics_payload(
    iteration_dir: Path,
    *,
    run_id: str,
) -> tuple[dict[str, Any] | None, Path | None, str]:
    normalized_run_id = str(run_id).strip()
    if normalized_run_id:
        explicit_path = iteration_dir / "runs" / normalized_run_id / "metrics.json"
        payload = _load_json_dict(explicit_path)
        if payload is not None:
            return (payload, explicit_path, normalized_run_id)

    candidates = sorted(iteration_dir.glob("runs/*/metrics.json"), reverse=True)
    for candidate in candidates:
        payload = _load_json_dict(candidate)
        if payload is None:
            continue
        resolved_run_id = (
            str(payload.get("run_id", "")).strip() or candidate.parent.name
        )
        return (payload, candidate, resolved_run_id)
    return (None, None, normalized_run_id)


def _decision_evidence_matches(
    evidence_rows: list[dict[str, Any]],
    *,
    requirement_id: str,
    task_id: str,
    metrics_pointer: str,
) -> int:
    requirement_token = str(requirement_id).strip().lower()
    task_token = str(task_id).strip().lower()
    matches = 0
    for row in evidence_rows:
        explicit_requirement = str(row.get("requirement_id", "")).strip().lower()
        if requirement_token and explicit_requirement == requirement_token:
            matches += 1
            continue
        explicit_task = str(row.get("task_id", "")).strip().lower()
        if task_token and explicit_task == task_token:
            matches += 1
            continue
        pointer = str(row.get("pointer", "")).strip()
        summary = str(row.get("summary", "")).strip()
        source = str(row.get("source", "")).strip()
        combined = f"{pointer} {summary} {source}"
        if _contains_identifier_token(combined, requirement_id):
            matches += 1
            continue
        if _contains_identifier_token(combined, task_id):
            matches += 1
            continue
        if _pointers_match(pointer, metrics_pointer):
            matches += 1
    return matches


def _normalize_task_status(raw_status: Any) -> str:
    status = str(raw_status or "").strip().lower()
    if status in {"completed", "failed", "blocked", "pending"}:
        return status
    if status in {"running", "in_progress"}:
        return "pending"
    if not status:
        return "unknown"
    return status


def _normalize_metrics_status(raw_status: Any) -> str:
    status = str(raw_status or "").strip().lower()
    if not status:
        return "missing"
    if status in {"completed", "partial", "failed", "missing"}:
        return status
    return "unknown"


def build_traceability_coverage(
    repo_root: Path,
    state: dict[str, Any],
    *,
    write_outputs: bool = True,
) -> TraceabilityBuildResult:
    iteration_id = str(state.get("iteration_id", "")).strip()
    if not iteration_id:
        raise ValueError("state.iteration_id is required for traceability")

    experiment_id = str(state.get("experiment_id", "")).strip()
    last_run_id = str(state.get("last_run_id", "")).strip()

    iteration_dir, _iteration_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        require_exists=False,
    )

    coverage_path = iteration_dir / "traceability_coverage.json"
    latest_path = repo_root / ".autolab" / "traceability_latest.json"

    diagnostics: list[str] = []

    hypothesis_path = iteration_dir / "hypothesis.md"
    design_path = iteration_dir / "design.yaml"
    iteration_contract_path = iteration_dir / "plan_contract.json"
    canonical_contract_path = repo_root / ".autolab" / "plan_contract.json"
    execution_summary_path = iteration_dir / "plan_execution_summary.json"
    verification_result_path = repo_root / ".autolab" / "verification_result.json"
    decision_result_path = iteration_dir / "decision_result.json"

    hypothesis_text = _safe_read_text(hypothesis_path, max_chars=40000)
    canonical_claim_text = _extract_canonical_claim(hypothesis_text)
    claim_status = "available" if canonical_claim_text else "missing"
    if claim_status != "available":
        diagnostics.append("canonical claim could not be extracted from hypothesis.md")

    design_payload, design_error = _load_yaml_dict(design_path)
    if design_payload is None:
        diagnostics.append(f"design unavailable: {design_error}")
        design_payload = {}
    requirements = _extract_requirements(design_payload)
    if not requirements:
        diagnostics.append("design has no implementation_requirements entries")

    plan_contract_payload = _load_json_dict(iteration_contract_path)
    plan_contract_path_used = iteration_contract_path
    if plan_contract_payload is None:
        fallback_payload = _load_json_dict(canonical_contract_path)
        if fallback_payload is not None:
            fallback_iteration_id = str(
                fallback_payload.get("iteration_id", "")
            ).strip()
            if fallback_iteration_id and fallback_iteration_id != iteration_id:
                diagnostics.append(
                    "plan_contract fallback iteration_id mismatch; ignoring .autolab/plan_contract.json"
                )
            else:
                plan_contract_payload = fallback_payload
                plan_contract_path_used = canonical_contract_path
    if plan_contract_payload is None:
        diagnostics.append("plan_contract.json unavailable in iteration or .autolab")
        plan_contract_payload = {}

    task_map, requirement_to_tasks = _extract_plan_mappings(plan_contract_payload)

    execution_summary_payload = _load_json_dict(execution_summary_path)
    if execution_summary_payload is None:
        diagnostics.append("plan_execution_summary.json unavailable")
        execution_summary_payload = {}
    task_details = _extract_task_details(execution_summary_payload)

    verification_payload = _load_json_dict(verification_result_path)
    if verification_payload is None:
        diagnostics.append(".autolab/verification_result.json unavailable")
        verification_payload = {}
    stage_verifier_passed = bool(verification_payload.get("passed", False))
    stage_verifier_message = str(verification_payload.get("message", "")).strip()

    metrics_payload, metrics_path, resolved_run_id = _resolve_metrics_payload(
        iteration_dir,
        run_id=last_run_id,
    )
    if metrics_payload is None:
        diagnostics.append("metrics.json unavailable for active iteration")

    decision_payload = _load_json_dict(decision_result_path)
    decision_status = "available" if isinstance(decision_payload, dict) else "missing"
    if decision_payload is None:
        diagnostics.append("decision_result.json unavailable")
        decision_payload = {}

    decision_value = str(decision_payload.get("decision", "")).strip()
    decision_rationale = str(decision_payload.get("rationale", "")).strip()
    raw_decision_evidence = decision_payload.get("evidence")
    decision_evidence_rows: list[dict[str, Any]] = []
    if isinstance(raw_decision_evidence, list):
        for entry in raw_decision_evidence:
            if isinstance(entry, dict):
                decision_evidence_rows.append(entry)

    metrics_pointer = _relative_path(repo_root, metrics_path)
    decision_pointer = _relative_path(repo_root, decision_result_path)
    execution_summary_pointer = _relative_path(repo_root, execution_summary_path)
    verification_pointer = _relative_path(repo_root, verification_result_path)

    rows: list[dict[str, Any]] = []
    for requirement in requirements:
        requirement_id = str(requirement.get("requirement_id", "")).strip()
        mapped_tasks = sorted(set(requirement_to_tasks.get(requirement_id, [])))
        if not mapped_tasks:
            rows.append(
                {
                    "row_id": f"{_CLAIM_ID}:{requirement_id}:unmapped",
                    "claim_id": _CLAIM_ID,
                    "requirement_id": requirement_id,
                    "requirement_description": str(
                        requirement.get("description", "")
                    ).strip(),
                    "scope_kind": str(requirement.get("scope_kind", "")).strip(),
                    "task_id": "",
                    "task_status": "unmapped",
                    "verification": {
                        "task_evidence_pointer": "",
                        "stage_verifier_pointer": verification_pointer,
                        "stage_verifier_passed": stage_verifier_passed,
                        "stage_verifier_message": stage_verifier_message,
                    },
                    "measurement": {
                        "run_id": resolved_run_id,
                        "metrics_pointer": metrics_pointer,
                        "metrics_status": "missing",
                        "primary_metric_name": "",
                        "primary_metric_value": None,
                        "delta_vs_baseline": None,
                    },
                    "decision": {
                        "decision": decision_value,
                        "decision_pointer": decision_pointer,
                        "decision_status": "missing"
                        if decision_status != "available"
                        else "unlinked",
                        "matched_evidence_count": 0,
                    },
                    "coverage_status": "failed",
                    "failure_class": "design",
                    "failure_reason": "requirement is not mapped by any plan task",
                }
            )
            continue

        for task_id in mapped_tasks:
            task_payload = task_map.get(task_id, {})
            task_detail = task_details.get(task_id, {})
            task_status = _normalize_task_status(task_detail.get("status", "unknown"))
            task_pointer = (
                f"{execution_summary_pointer}#task:{task_id}"
                if execution_summary_pointer
                else ""
            )

            primary_metric = (
                metrics_payload.get("primary_metric", {})
                if isinstance(metrics_payload, dict)
                else {}
            )
            if not isinstance(primary_metric, dict):
                primary_metric = {}

            metrics_status = (
                _normalize_metrics_status(metrics_payload.get("status"))
                if isinstance(metrics_payload, dict)
                else "missing"
            )
            metric_name = str(primary_metric.get("name", "")).strip()
            metric_value = _coerce_float(primary_metric.get("value"))
            metric_delta = _coerce_float(primary_metric.get("delta_vs_baseline"))

            matched_evidence_count = _decision_evidence_matches(
                decision_evidence_rows,
                requirement_id=requirement_id,
                task_id=task_id,
                metrics_pointer=metrics_pointer,
            )
            decision_link_status = "missing"
            if decision_status == "available":
                if matched_evidence_count > 0:
                    decision_link_status = "linked"
                else:
                    decision_link_status = "unlinked"

            coverage_status = "covered"
            failure_class = "none"
            failure_reason = ""

            if task_status == "failed":
                coverage_status = "failed"
                failure_class = "execution"
                failure_reason = "task execution failed"
            elif task_status in {"blocked", "pending", "unknown"}:
                coverage_status = "untested"
                failure_class = "execution"
                failure_reason = f"task status is '{task_status}'"
            elif not stage_verifier_passed:
                coverage_status = "failed"
                failure_class = "execution"
                failure_reason = "stage verifier failed"
            else:
                if not isinstance(metrics_payload, dict):
                    coverage_status = "failed"
                    failure_class = "measurement"
                    failure_reason = "metrics artifact is missing"
                elif metrics_status != "completed":
                    coverage_status = "failed"
                    failure_class = "measurement"
                    failure_reason = (
                        f"metrics status is '{metrics_status or 'unknown'}'"
                    )
                elif metric_value is None:
                    coverage_status = "failed"
                    failure_class = "measurement"
                    failure_reason = "primary metric value is missing/non-numeric"

            rows.append(
                {
                    "row_id": f"{_CLAIM_ID}:{requirement_id}:{task_id}",
                    "claim_id": _CLAIM_ID,
                    "requirement_id": requirement_id,
                    "requirement_description": str(
                        requirement.get("description", "")
                    ).strip(),
                    "scope_kind": str(requirement.get("scope_kind", "")).strip(),
                    "task_id": task_id,
                    "task_status": task_status,
                    "verification": {
                        "task_evidence_pointer": task_pointer,
                        "attempts": _coerce_int(task_detail.get("attempts", 0)),
                        "retries_used": _coerce_int(task_detail.get("retries_used", 0)),
                        "last_error": str(task_detail.get("last_error", "")).strip(),
                        "verification_commands": [
                            str(command).strip()
                            for command in task_payload.get("verification_commands", [])
                            if str(command).strip()
                        ]
                        if isinstance(task_payload.get("verification_commands"), list)
                        else [],
                        "stage_verifier_pointer": verification_pointer,
                        "stage_verifier_passed": stage_verifier_passed,
                        "stage_verifier_message": stage_verifier_message,
                    },
                    "measurement": {
                        "run_id": resolved_run_id,
                        "metrics_pointer": metrics_pointer,
                        "metrics_status": metrics_status or "missing",
                        "primary_metric_name": metric_name,
                        "primary_metric_value": metric_value,
                        "delta_vs_baseline": metric_delta,
                    },
                    "decision": {
                        "decision": decision_value,
                        "decision_pointer": decision_pointer,
                        "decision_status": decision_link_status,
                        "matched_evidence_count": matched_evidence_count,
                    },
                    "coverage_status": coverage_status,
                    "failure_class": failure_class,
                    "failure_reason": failure_reason,
                }
            )

    rows_total = len(rows)
    rows_covered = sum(1 for row in rows if row["coverage_status"] == "covered")
    rows_untested = sum(1 for row in rows if row["coverage_status"] == "untested")
    rows_failed = sum(1 for row in rows if row["coverage_status"] == "failed")

    failed_by_class = {"design": 0, "execution": 0, "measurement": 0}
    untested_by_class = {"execution": 0}
    for row in rows:
        status = str(row.get("coverage_status", "")).strip()
        failure_class = str(row.get("failure_class", "")).strip()
        if status == "failed" and failure_class in failed_by_class:
            failed_by_class[failure_class] += 1
        if status == "untested" and failure_class in untested_by_class:
            untested_by_class[failure_class] += 1

    requirement_status: dict[str, str] = {}
    for requirement in requirements:
        requirement_id = str(requirement.get("requirement_id", "")).strip()
        requirement_rows = [
            row
            for row in rows
            if str(row.get("requirement_id", "")).strip() == requirement_id
        ]
        if any(row.get("coverage_status") == "failed" for row in requirement_rows):
            requirement_status[requirement_id] = "failed"
        elif any(row.get("coverage_status") == "untested" for row in requirement_rows):
            requirement_status[requirement_id] = "untested"
        elif requirement_rows:
            requirement_status[requirement_id] = "covered"
        else:
            requirement_status[requirement_id] = "failed"

    requirements_total = len(requirements)
    requirements_covered = sum(
        1 for status in requirement_status.values() if status == "covered"
    )
    requirements_untested = sum(
        1 for status in requirement_status.values() if status == "untested"
    )
    requirements_failed = sum(
        1 for status in requirement_status.values() if status == "failed"
    )
    decision_linked_rows = sum(
        1
        for row in rows
        if str(row.get("decision", {}).get("decision_status", "")).strip() == "linked"
    )

    pointers = {
        "hypothesis_path": _relative_path(repo_root, hypothesis_path),
        "design_path": _relative_path(repo_root, design_path),
        "plan_contract_path": _relative_path(repo_root, plan_contract_path_used),
        "plan_execution_summary_path": execution_summary_pointer,
        "verification_result_path": verification_pointer,
        "metrics_path": metrics_pointer,
        "decision_result_path": decision_pointer,
    }

    coverage_payload: dict[str, Any] = {
        "schema_version": _TRACEABILITY_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "run_id": resolved_run_id,
        "claim": {
            "claim_id": _CLAIM_ID,
            "text": canonical_claim_text,
            "status": claim_status,
            "source_pointer": _relative_path(repo_root, hypothesis_path),
        },
        "decision": {
            "status": decision_status,
            "decision": decision_value,
            "rationale": decision_rationale,
            "pointer": decision_pointer,
            "evidence_count": len(decision_evidence_rows),
        },
        "links": rows,
        "summary": {
            "rows_total": rows_total,
            "rows_covered": rows_covered,
            "rows_untested": rows_untested,
            "rows_failed": rows_failed,
            "requirements_total": requirements_total,
            "requirements_covered": requirements_covered,
            "requirements_untested": requirements_untested,
            "requirements_failed": requirements_failed,
            "failed_by_class": failed_by_class,
            "untested_by_class": untested_by_class,
            "decision_linked_rows": decision_linked_rows,
        },
        "pointers": pointers,
        "diagnostics": diagnostics,
    }

    latest_payload = {
        "schema_version": _TRACEABILITY_SCHEMA_VERSION,
        "generated_at": _utc_now(),
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "run_id": resolved_run_id,
        "traceability_path": _relative_path(repo_root, coverage_path),
        "decision": {
            "status": decision_status,
            "decision": decision_value,
            "pointer": decision_pointer,
        },
        "summary": coverage_payload["summary"],
    }

    if write_outputs:
        _write_json(coverage_path, coverage_payload)
        _write_json(latest_path, latest_payload)

    return TraceabilityBuildResult(
        coverage_payload=coverage_payload,
        coverage_path=coverage_path,
        latest_payload=latest_payload,
        latest_path=latest_path,
    )


__all__ = ["TraceabilityBuildResult", "build_traceability_coverage"]
