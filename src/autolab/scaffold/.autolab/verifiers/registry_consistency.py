#!/usr/bin/env python3
"""Verify workflow registry capabilities are consistent with policy requirements."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_PATH = REPO_ROOT / ".autolab" / "workflow.yaml"
POLICY_PATH = REPO_ROOT / ".autolab" / "verifier_policy.yaml"


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError("PyYAML is required for registry consistency checks")
    if not path.exists():
        raise RuntimeError(f"missing file: {path}")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"could not parse {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a mapping")
    return payload


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
            failures.append(
                f"requirements_by_stage.{stage} must be a mapping"
            )
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=None, help="Optional stage label for envelope context.")
    parser.add_argument("--json", action="store_true", default=False, help="Output machine-readable JSON envelope")
    args = parser.parse_args()
    stage = str(args.stage or "").strip()

    try:
        workflow = _load_yaml_mapping(WORKFLOW_PATH)
        policy = _load_yaml_mapping(POLICY_PATH)
    except Exception as exc:
        if args.json:
            envelope = {
                "status": "fail",
                "verifier": "registry_consistency",
                "stage": stage,
                "checks": [],
                "errors": [str(exc)],
            }
            print(json.dumps(envelope))
        else:
            print(f"registry_consistency: ERROR {exc}")
        return 1

    failures = _check_subset_constraints(workflow, policy)
    passed = not failures

    if args.json:
        checks = [{"name": issue, "status": "fail", "detail": issue} for issue in failures]
        if passed:
            checks = [
                {
                    "name": "registry_consistency",
                    "status": "pass",
                    "detail": "policy requirements are a subset of registry capabilities",
                }
            ]
        envelope = {
            "status": "pass" if passed else "fail",
            "verifier": "registry_consistency",
            "stage": stage,
            "checks": checks,
            "errors": failures,
        }
        print(json.dumps(envelope))
    else:
        if passed:
            print("registry_consistency: PASS")
        else:
            print("registry_consistency: FAIL")
            for issue in failures:
                print(issue)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
