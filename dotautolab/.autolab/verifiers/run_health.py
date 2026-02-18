#!/usr/bin/env python3
"""Run health checks for launch and sync status."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / ".autolab" / "state.json"


def _load_state() -> dict:
    if not STATE_FILE.exists():
        raise RuntimeError("Missing .autolab/state.json")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise RuntimeError("state.json must contain an object")
    return state


def _load_json(path: Path) -> dict:
    if not path.exists():
        raise RuntimeError(f"{path} is missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return payload


def _check_launch_artifacts(iteration_id: str, run_id: str) -> list[str]:
    failures: list[str] = []
    run_dir = REPO_ROOT / "experiments" / iteration_id / "runs" / run_id
    manifest_path = run_dir / "run_manifest.json"
    manifest = _load_json(manifest_path)

    run_id_in_manifest = str(manifest.get("run_id", "")).strip()
    if run_id_in_manifest and run_id_in_manifest != run_id:
        failures.append(f"{manifest_path} run_id mismatch: expected {run_id}, found {run_id_in_manifest}")

    location = str(manifest.get("location", "local")).strip().lower() or "local"
    sync = manifest.get("sync", {})
    if location == "slurm":
        sync_status = sync.get("artifact_sync_to_local", {}).get("status", "").lower()
        if sync_status not in {"ok", "completed", "success", "passed"}:
            failures.append(f"{manifest_path} requires slurm artifact sync status 'ok', got '{sync_status or '<missing>'}'")

    # Run-level required outputs for launch health.
    if not (run_dir / "metrics.json").exists():
        failures.append(f"{run_dir / 'metrics.json'} is missing")

    if not (run_dir / "logs").exists():
        failures.append(f"{run_dir / 'logs'} is missing")
        return failures

    if manifest.get("status") == "failed":
        failures.append(f"{manifest_path} has failed status")

    return failures


def main() -> int:
    failures: list[str] = []

    try:
        state = _load_state()
    except Exception as exc:
        print(f"run_health: ERROR {exc}")
        return 1

    stage = str(state.get("stage", "")).strip()
    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()

    if stage != "launch":
        print("run_health: PASS")
        return 0

    if not iteration_id or not run_id or run_id.startswith("<"):
        print("run_health: ERROR missing iteration_id/last_run_id")
        return 1

    try:
        failures.extend(_check_launch_artifacts(iteration_id, run_id))
    except Exception as exc:
        print(f"run_health: ERROR {exc}")
        return 1

    if failures:
        print("run_health: FAIL")
        for reason in failures:
            print(reason)
        return 1

    print("run_health: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
