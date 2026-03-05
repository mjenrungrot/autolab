#!/usr/bin/env python3
"""Verifier for machine-checked implementation plan contracts."""

from __future__ import annotations

import pathlib
import sys

_VERIFIER_DIR = pathlib.Path(__file__).resolve().parent
if str(_VERIFIER_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFIER_DIR))

import argparse
import json
from typing import Iterable, Optional

from verifier_lib import load_state

from autolab.plan_contract import PlanContractError, check_implementation_plan_contract


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=None, help="Override current stage")
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON envelope",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        state = load_state()
    except Exception as exc:
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "fail",
                        "verifier": "implementation_plan_contract",
                        "stage": "",
                        "checks": [],
                        "errors": [str(exc)],
                    }
                )
            )
        else:
            print(f"implementation_plan_contract: ERROR {exc}")
        return 1

    stage = str(args.stage or state.get("stage", "")).strip()
    if stage != "implementation":
        if args.json:
            print(
                json.dumps(
                    {
                        "status": "pass",
                        "verifier": "implementation_plan_contract",
                        "stage": stage,
                        "checks": [
                            {
                                "name": "stage_skip",
                                "status": "pass",
                                "detail": f"skipped for stage={stage}",
                            }
                        ],
                        "errors": [],
                    }
                )
            )
        else:
            print(f"implementation_plan_contract: SKIP stage={stage}")
        return 0

    repo_root = pathlib.Path.cwd()
    try:
        passed, message, details = check_implementation_plan_contract(
            repo_root,
            state,
            stage_override="implementation",
            write_outputs=True,
        )
    except PlanContractError as exc:
        passed = False
        message = f"implementation plan contract check failed: {exc}"
        details = {
            "errors": [str(exc)],
            "warnings": [],
            "rule_results": [],
        }

    errors = details.get("errors", [])
    if not isinstance(errors, list):
        errors = [str(errors)]
    warnings = details.get("warnings", [])
    if not isinstance(warnings, list):
        warnings = [str(warnings)]
    rule_results = details.get("rule_results", [])
    if not isinstance(rule_results, list):
        rule_results = []

    if args.json:
        checks = [
            {
                "name": str(item.get("rule", "rule")),
                "status": str(item.get("status", "fail")),
                "detail": str(item.get("detail", "")).strip(),
            }
            for item in rule_results
            if isinstance(item, dict)
        ]
        if not checks:
            checks = [
                {
                    "name": "implementation_plan_contract",
                    "status": "pass" if passed else "fail",
                    "detail": message,
                }
            ]
        print(
            json.dumps(
                {
                    "status": "pass" if passed else "fail",
                    "verifier": "implementation_plan_contract",
                    "stage": "implementation",
                    "checks": checks,
                    "errors": [str(item) for item in errors],
                    "warnings": [str(item) for item in warnings],
                }
            )
        )
    else:
        if passed:
            print("implementation_plan_contract: PASS")
            for warning in warnings:
                print(f"  WARN: {warning}")
        else:
            print(f"implementation_plan_contract: FAIL issues={len(errors)}")
            for issue in errors:
                print(f"  {issue}")
            for warning in warnings:
                print(f"  WARN: {warning}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
