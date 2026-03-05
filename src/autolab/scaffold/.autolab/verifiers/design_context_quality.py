#!/usr/bin/env python3
"""Advisory verifier for design context uptake quality."""

from __future__ import annotations

import pathlib
import sys

_VERIFIER_DIR = pathlib.Path(__file__).resolve().parent
if str(_VERIFIER_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFIER_DIR))

import argparse
import importlib.util
import json
from typing import Any, Iterable, Optional

from autolab.design_context_quality import build_design_context_quality


def _load_module_from_path(module_name: str, module_path: pathlib.Path) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:  # pragma: no cover - import failure guard
        raise ImportError(f"cannot load {module_name} from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_local_verifier_dependencies() -> tuple[Any, Any]:
    verifier_key = f"_autolab_verifier_lib_{_VERIFIER_DIR}"
    schema_key = f"_autolab_schema_checks_{_VERIFIER_DIR}"
    previous_verifier = sys.modules.get("verifier_lib")
    previous_schema = sys.modules.get("schema_checks")
    try:
        verifier_lib = _load_module_from_path(
            verifier_key, _VERIFIER_DIR / "verifier_lib.py"
        )
        sys.modules["verifier_lib"] = verifier_lib
        schema_checks = _load_module_from_path(
            schema_key, _VERIFIER_DIR / "schema_checks.py"
        )
        sys.modules["schema_checks"] = schema_checks
        return verifier_lib.load_state, schema_checks._schema_validate
    finally:
        if previous_verifier is None:
            sys.modules.pop("verifier_lib", None)
        else:
            sys.modules["verifier_lib"] = previous_verifier
        if previous_schema is None:
            sys.modules.pop("schema_checks", None)
        else:
            sys.modules["schema_checks"] = previous_schema


load_state, _schema_validate = _load_local_verifier_dependencies()

_SCHEMA_FAILURE_PREFIX = "design_context_quality.json schema violation"


def _normalize_schema_failure(
    error: object, *, schema_hint: bool = False
) -> str | None:
    message = str(error).strip()
    if message.startswith(_SCHEMA_FAILURE_PREFIX):
        return message
    lower_message = message.lower()
    marker = lower_message.find("schema violation")
    if marker >= 0:
        suffix = message[marker + len("schema violation") :]
        return f"{_SCHEMA_FAILURE_PREFIX}{suffix}"
    if schema_hint:
        detail = message or type(error).__name__
        return f"{_SCHEMA_FAILURE_PREFIX}: {detail}"
    return None


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
    try:
        result = build_design_context_quality(repo_root, state, write_outputs=True)
    except Exception as exc:
        error_message = _normalize_schema_failure(
            exc,
            schema_hint=(type(exc).__name__ == "ValidationError"),
        ) or str(exc)
        payload = {
            "status": "fail",
            "verifier": "design_context_quality",
            "stage": "design",
            "checks": [],
            "errors": [error_message],
        }
        if args.json:
            print(json.dumps(payload))
        else:
            if error_message.startswith(_SCHEMA_FAILURE_PREFIX):
                print("design_context_quality: FAIL report schema invalid")
                print(f"  ERROR: {error_message}")
            else:
                print(f"design_context_quality: ERROR {error_message}")
        return 1
    diagnostics = result.payload.get("diagnostics", [])
    if not isinstance(diagnostics, list):
        diagnostics = [str(diagnostics)]
    schema_failures = [
        _normalize_schema_failure(failure) or str(failure)
        for failure in _schema_validate(
            result.payload,
            schema_key="design_context_quality",
            path=result.report_path,
        )
    ]
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
