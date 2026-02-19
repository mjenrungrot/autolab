#!/usr/bin/env python3
"""Verify workflow registry capabilities are consistent with policy requirements."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from verifier_lib import REPO_ROOT, load_yaml, make_result, print_result

WORKFLOW_PATH = REPO_ROOT / ".autolab" / "workflow.yaml"
POLICY_PATH = REPO_ROOT / ".autolab" / "verifier_policy.yaml"


def _check_subset_constraints(
    workflow: dict[str, Any],
    policy: dict[str, Any],
) -> list[str]:
    failures: list[str] = []
    stages = workflow.get("stages", {})
    if not isinstance(stages, dict):
        return ["workflow.yaml missing 'stages' mapping"]

    requirements_by_stage = policy.get("requirements_by_stage", {})
    if not isinstance(requirements_by_stage, dict):
        return ["verifier_policy.yaml requirements_by_stage must be a mapping"]

    for raw_stage, raw_requirements in requirements_by_stage.items():
        stage = str(raw_stage).strip()
        if not stage:
            continue
        stage_spec = stages.get(stage)
        if not isinstance(stage_spec, dict):
            failures.append(
                f"requirements_by_stage.{stage} has no matching stage in workflow.yaml"
            )
            continue
        verifier_categories = stage_spec.get("verifier_categories", {})
        if not isinstance(verifier_categories, dict):
            failures.append(
                f"workflow.yaml stages.{stage}.verifier_categories must be a mapping"
            )
            continue
        if not isinstance(raw_requirements, dict):
            failures.append(f"requirements_by_stage.{stage} must be a mapping")
            continue

        for raw_key, raw_required in raw_requirements.items():
            requirement_key = str(raw_key).strip()
            if not requirement_key:
                continue
            required = bool(raw_required)
            capability = bool(verifier_categories.get(requirement_key, False))
            if required and not capability:
                failures.append(
                    f"requirements_by_stage.{stage}.{requirement_key}=true "
                    "is not supported by workflow.yaml verifier_categories"
                )
    return failures


def _check_output_contract_placeholders(workflow: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    stages = workflow.get("stages", {})
    if not isinstance(stages, dict):
        return failures

    def _check_output_path(stage: str, path: str) -> None:
        output_path = str(path).strip()
        if not output_path:
            return
        if "{{" in output_path or "}}" in output_path:
            failures.append(
                (
                    f"workflow.yaml stages.{stage} output path '{output_path}' uses prompt-style mustache tokens; "
                    "use pattern tokens like <RUN_ID> for registry/policy path contracts"
                )
            )

    for raw_stage, raw_spec in stages.items():
        stage = str(raw_stage).strip()
        if not stage or not isinstance(raw_spec, dict):
            continue

        required_outputs = raw_spec.get("required_outputs", [])
        if isinstance(required_outputs, list):
            for output in required_outputs:
                _check_output_path(stage, str(output))

        required_outputs_any_of = raw_spec.get("required_outputs_any_of", [])
        if isinstance(required_outputs_any_of, list):
            for group in required_outputs_any_of:
                if not isinstance(group, list):
                    continue
                for output in group:
                    _check_output_path(stage, str(output))

        required_outputs_if = raw_spec.get("required_outputs_if", [])
        if isinstance(required_outputs_if, dict):
            required_outputs_if = [required_outputs_if]
        if isinstance(required_outputs_if, list):
            for rule in required_outputs_if:
                if not isinstance(rule, dict):
                    continue
                outputs = rule.get("outputs", [])
                if not isinstance(outputs, list):
                    continue
                for output in outputs:
                    _check_output_path(stage, str(output))

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", default=None, help="Optional stage label for envelope context."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON envelope",
    )
    args = parser.parse_args()
    stage = str(args.stage or "").strip()

    try:
        workflow = load_yaml(WORKFLOW_PATH)
        policy = load_yaml(POLICY_PATH)
    except Exception as exc:
        result = make_result("registry_consistency", stage, [], [str(exc)])
        print_result(result, as_json=args.json)
        return 1

    failures = _check_subset_constraints(workflow, policy)
    failures.extend(_check_output_contract_placeholders(workflow))
    passed = not failures

    checks = [{"name": issue, "status": "fail", "detail": issue} for issue in failures]
    if passed:
        checks = [
            {
                "name": "registry_consistency",
                "status": "pass",
                "detail": "policy requirements are a subset of registry capabilities",
            }
        ]
    result = make_result("registry_consistency", stage, checks, failures)
    print_result(result, as_json=args.json)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
