#!/usr/bin/env python3
"""Schema validation entrypoints for autolab artifacts.

This verifier validates real iteration artifacts with lightweight schema checks.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / ".autolab" / "state.json"


DESIGN_REQUIRED = {"id", "iteration_id", "hypothesis_id", "entrypoint", "compute", "metrics", "baselines"}
AGENT_REQUIRED = {"status", "summary", "changed_files", "completion_token_seen"}


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"{path} is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return payload


def _load_yaml(path: Path) -> dict:
    if yaml is None:
        raise RuntimeError("PyYAML is not available")
    if not path.exists():
        raise RuntimeError(f"{path} is missing")
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"{path} is not valid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping")
    return payload


def _load_state() -> dict:
    return _load_json(STATE_FILE)


def _validate_design(state: dict) -> List[str]:
    stage = str(state.get("stage", "")).strip()
    iteration_id = str(state.get("iteration_id", "")).strip()
    iteration_path = REPO_ROOT / "experiments" / iteration_id
    design_path = iteration_path / "design.yaml"

    failures: List[str] = []
    if stage not in {
        "hypothesis",
        "design",
        "implementation",
        "implementation_review",
        "launch",
        "extract_results",
        "update_docs",
    }:
        return failures

    try:
        data = _load_yaml(design_path)
    except RuntimeError as exc:
        failures.append(str(exc))
        return failures

    missing = sorted(DESIGN_REQUIRED - set(data.keys()))
    if missing:
        failures.append(f"{design_path} missing required keys: {missing}")
        return failures

    if data.get("iteration_id") != iteration_id:
        failures.append(f"{design_path} iteration_id does not match state")

    entrypoint = data.get("entrypoint", {})
    if not isinstance(entrypoint, dict) or "module" not in entrypoint or not str(entrypoint.get("module", "")).strip():
        failures.append(f"{design_path} entrypoint.module must be set")

    compute = data.get("compute", {})
    if not isinstance(compute, dict) or "location" not in compute:
        failures.append(f"{design_path} compute.location must be set")

    baselines = data.get("baselines")
    if not isinstance(baselines, list) or not baselines:
        failures.append(f"{design_path} requires a non-empty baselines list")

    return failures


def _validate_agent_result() -> List[str]:
    path = REPO_ROOT / ".autolab" / "agent_result.json"
    failures: List[str] = []
    data = _load_json(path)

    missing = sorted(AGENT_REQUIRED - set(data.keys()))
    if missing:
        failures.append(f"{path} missing required keys: {missing}")

    status = data.get("status")
    if status not in {"complete", "needs_retry", "failed"}:
        failures.append(f"{path} status must be one of complete/needs_retry/failed")

    summary = data.get("summary")
    if not isinstance(summary, str):
        failures.append(f"{path} summary must be a string")

    changed_files = data.get("changed_files")
    if not isinstance(changed_files, list):
        failures.append(f"{path} changed_files must be a list")

    completion_token_seen = data.get("completion_token_seen")
    if not isinstance(completion_token_seen, bool):
        failures.append(f"{path} completion_token_seen must be a boolean")

    return failures


def main() -> int:
    failures: List[str] = []

    try:
        state = _load_state()
    except Exception as exc:
        print(f"schema_checks: ERROR {exc}")
        return 1

    for reason in _validate_design(state):
        failures.append(reason)
    for reason in _validate_agent_result():
        failures.append(reason)

    if failures:
        print("schema_checks: FAIL")
        for reason in failures:
            print(reason)
        return 1

    print("schema_checks: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
