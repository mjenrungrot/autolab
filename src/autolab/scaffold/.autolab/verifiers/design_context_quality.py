#!/usr/bin/env python3
"""Advisory verifier for design context uptake quality."""

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
from schema_checks import _schema_validate

from autolab.design_context_quality import build_design_context_quality


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
                        "verifier": "design_context_quality",
                        "stage": "",
                        "checks": [],
                        "errors": [str(exc)],
                    }
                )
            )
        else:
            print(f"design_context_quality: ERROR {exc}")
        return 1

    stage = str(args.stage or state.get("stage", "")).strip()
    if stage != "design":
        payload = {
            "status": "pass",
            "verifier": "design_context_quality",
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
        if args.json:
            print(json.dumps(payload))
        else:
            print(f"design_context_quality: SKIP stage={stage}")
        return 0

    repo_root = pathlib.Path.cwd()
    result = build_design_context_quality(repo_root, state, write_outputs=True)
    diagnostics = result.payload.get("diagnostics", [])
    if not isinstance(diagnostics, list):
        diagnostics = [str(diagnostics)]
    schema_failures = _schema_validate(
        result.payload,
        schema_key="design_context_quality",
        path=result.report_path,
    )
    if schema_failures:
        payload = {
            "status": "fail",
            "verifier": "design_context_quality",
            "stage": "design",
            "checks": [
                {
                    "name": "context_mode",
                    "status": "pass",
                    "detail": str(result.payload.get("context_mode", "")),
                }
            ],
            "errors": schema_failures,
            "warnings": [str(item) for item in diagnostics],
            "report_path": result.report_path.as_posix(),
        }
        if args.json:
            print(json.dumps(payload))
        else:
            print("design_context_quality: FAIL report schema invalid")
            for failure in schema_failures:
                print(f"  ERROR: {failure}")
        return 1
    payload = {
        "status": "pass",
        "verifier": "design_context_quality",
        "stage": "design",
        "checks": [
            {
                "name": "context_mode",
                "status": "pass",
                "detail": str(result.payload.get("context_mode", "")),
            },
            {
                "name": "quality_score",
                "status": "pass",
                "detail": (
                    f"{result.payload.get('score', {}).get('value', 0)}/"
                    f"{result.payload.get('score', {}).get('max', 1)}"
                ),
            },
        ],
        "errors": [],
        "warnings": [str(item) for item in diagnostics],
        "report_path": result.report_path.as_posix(),
    }
    if args.json:
        print(json.dumps(payload))
    else:
        print(
            "design_context_quality: PASS "
            f"score={result.payload.get('score', {}).get('value', 0)}/"
            f"{result.payload.get('score', {}).get('max', 1)} "
            f"report={result.report_path}"
        )
        for item in diagnostics:
            print(f"  WARN: {item}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
