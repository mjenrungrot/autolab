#!/usr/bin/env python3
"""Run health checks for launch and sync status."""

from __future__ import annotations

import pathlib
import sys

_VERIFIER_DIR = pathlib.Path(__file__).resolve().parent
if str(_VERIFIER_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFIER_DIR))

import argparse
from pathlib import Path

from verifier_lib import (
    COMPLETION_LIKE_STATUSES,
    IN_PROGRESS_STATUSES,
    REPO_ROOT,
    RUN_MANIFEST_STATUSES,
    load_json,
    load_state,
    make_result,
    normalize_sync_status,
    print_result,
    resolve_iteration_dir,
)


def _launch_execute_enabled() -> bool:
    policy_path = REPO_ROOT / ".autolab" / "verifier_policy.yaml"
    try:
        import yaml  # type: ignore
    except Exception:
        return True
    if not policy_path.exists():
        return True
    try:
        payload = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except Exception:
        return True
    if not isinstance(payload, dict):
        return True
    launch = payload.get("launch")
    if not isinstance(launch, dict):
        return True
    raw = str(launch.get("execute", "true")).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _check_launch_artifacts(
    iteration_id: str, run_id: str, *, launch_execute: bool
) -> list[str]:
    failures: list[str] = []
    run_dir = resolve_iteration_dir(iteration_id) / "runs" / run_id
    manifest_path = run_dir / "run_manifest.json"
    manifest = load_json(manifest_path)
    manifest_status = str(manifest.get("status", "")).strip().lower()
    completion_like_statuses = COMPLETION_LIKE_STATUSES
    in_progress_statuses = IN_PROGRESS_STATUSES

    run_id_in_manifest = str(manifest.get("run_id", "")).strip()
    if run_id_in_manifest and run_id_in_manifest != run_id:
        failures.append(
            f"{manifest_path} run_id mismatch: expected {run_id}, found {run_id_in_manifest}"
        )

    host_mode = (
        str(
            manifest.get(
                "host_mode",
                manifest.get("launch_mode", manifest.get("location", "local")),
            )
        )
        .strip()
        .lower()
        or "local"
    )
    if host_mode not in {"local", "slurm"}:
        failures.append(f"{manifest_path} host_mode must be local or slurm")

    sync = manifest.get("sync", {})
    sync_to_local = manifest.get("artifact_sync_to_local", sync)
    if host_mode == "slurm":
        if not isinstance(sync_to_local, dict):
            failures.append(f"{manifest_path} artifact_sync_to_local must be a mapping")
        else:
            raw_sync_status = str(sync_to_local.get("status", "")).strip().lower()
            sync_status = normalize_sync_status(raw_sync_status)
            if not raw_sync_status:
                failures.append(
                    f"{manifest_path} artifact_sync_to_local.status is required for slurm runs"
                )
            elif not sync_status:
                failures.append(
                    f"{manifest_path} unsupported artifact_sync_to_local.status '{raw_sync_status}' "
                    "(expected canonical pending|syncing|ok|failed or known synonym)"
                )
                sync_status = ""
            if manifest_status in completion_like_statuses:
                if sync_status and sync_status != "ok":
                    failures.append(
                        f"{manifest_path} requires canonical artifact sync status 'ok' when run is completion-like, "
                        f"found '{raw_sync_status}'"
                    )
            else:
                in_progress_sync_statuses = {"pending", "syncing", "ok"}
                if sync_status and sync_status not in in_progress_sync_statuses:
                    failures.append(
                        f"{manifest_path} has invalid canonical artifact sync status '{sync_status}' "
                        "for non-completion launch state (expected pending|syncing|ok)"
                    )
        slurm_ledger_path = REPO_ROOT / "docs" / "slurm_job_list.md"
        if not slurm_ledger_path.exists():
            failures.append(
                f"{slurm_ledger_path} is required for slurm launch tracking"
            )
        else:
            ledger_text = slurm_ledger_path.read_text(encoding="utf-8")
            if f"run_id={run_id}" not in ledger_text:
                failures.append(f"{slurm_ledger_path} is missing run_id={run_id} entry")
    else:
        command = manifest.get("command", "")
        if not command:
            failures.append(f"{manifest_path} command is required")

    resource_request = manifest.get("resource_request")
    if not isinstance(resource_request, dict):
        failures.append(f"{manifest_path} resource_request must be a mapping")
    if host_mode == "slurm" and not resource_request:
        failures.append(f"{manifest_path} slurm run must include resource_request")

    timestamps = manifest.get("timestamps", {})
    if not isinstance(timestamps, dict):
        failures.append(f"{manifest_path} timestamps must be a mapping")
    else:
        started_at = str(timestamps.get("started_at", "")).strip()
        if not started_at:
            failures.append(f"{manifest_path} timestamps.started_at is required")
        if manifest_status in completion_like_statuses:
            completed_at = str(timestamps.get("completed_at", "")).strip()
            if not completed_at:
                failures.append(
                    f"{manifest_path} timestamps.completed_at is required when status is completion-like"
                )

    run_status = manifest_status
    if run_status in {"failed", "error"}:
        failures.append(f"{manifest_path} has failed status")
    if (
        run_status
        and run_status not in completion_like_statuses
        and run_status not in in_progress_statuses
    ):
        failures.append(
            f"{manifest_path} status '{run_status}' is not recognized "
            f"(expected completion-like or in-progress states)"
        )

    if launch_execute and not (run_dir / "logs").exists():
        failures.append(f"{run_dir / 'logs'} is missing")
        return failures

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON envelope",
    )
    args = parser.parse_args()

    failures: list[str] = []

    try:
        state = load_state()
    except Exception as exc:
        result = make_result("run_health", "", [], [str(exc)])
        print_result(result, as_json=args.json)
        return 1

    stage = str(state.get("stage", "")).strip()
    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()

    if stage != "launch":
        result = make_result(
            "run_health",
            stage,
            [
                {
                    "name": "run_health",
                    "status": "pass",
                    "detail": f"skipped for stage={stage}",
                }
            ],
            [],
        )
        print_result(result, as_json=args.json)
        return 0

    if not iteration_id or not run_id or run_id.startswith("<"):
        result = make_result(
            "run_health", stage, [], ["missing iteration_id/last_run_id"]
        )
        print_result(result, as_json=args.json)
        return 1

    try:
        failures.extend(
            _check_launch_artifacts(
                iteration_id,
                run_id,
                launch_execute=_launch_execute_enabled(),
            )
        )
    except Exception as exc:
        result = make_result("run_health", stage, [], [str(exc)])
        print_result(result, as_json=args.json)
        return 1

    passed = not failures

    checks = [{"name": f, "status": "fail", "detail": f} for f in failures]
    if passed:
        checks = [
            {
                "name": "run_health",
                "status": "pass",
                "detail": "all run health checks passed",
            }
        ]
    result = make_result("run_health", stage, checks, failures)
    print_result(result, as_json=args.json)

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
