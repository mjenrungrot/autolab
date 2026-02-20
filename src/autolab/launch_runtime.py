from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from autolab.config import _load_launch_runtime_config
from autolab.constants import (
    COMPLETION_LIKE_STATUSES,
    IN_PROGRESS_STATUSES,
    RUN_MANIFEST_STATUSES,
    SLURM_JOB_LIST_PATH,
)
from autolab.models import StageCheckError
from autolab.slurm_job_list import append_entry_idempotent, canonical_slurm_job_bullet
from autolab.state import _resolve_iteration_directory
from autolab.utils import (
    _append_log,
    _collect_slurm_env_metadata,
    _get_slurm_allocation_resources,
    _is_slurm_interactive_session,
    _manifest_timestamp,
    _utc_now,
)


SBATCH_JOB_ID_PATTERN = re.compile(
    r"submitted\s+batch\s+job\s+(?P<job_id>\d+)", flags=re.IGNORECASE
)


@dataclass(frozen=True)
class LaunchExecutionResult:
    run_id: str
    sync_status: str
    changed_files: tuple[Path, ...]


def _compact_text(text: str, *, limit: int = 240) -> str:
    compact = " ".join(str(text).strip().split())
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit]}..."


def _normalize_status(value: str, *, fallback: str) -> str:
    status = str(value).strip().lower()
    if status in RUN_MANIFEST_STATUSES:
        return status
    return fallback


def _parse_job_id(text: str) -> str:
    match = SBATCH_JOB_ID_PATTERN.search(str(text or ""))
    if not match:
        return ""
    return str(match.group("job_id")).strip()


def _extract_job_id(payload: dict[str, Any]) -> str:
    candidates = (
        payload.get("job_id"),
        (payload.get("slurm") or {}).get("job_id")
        if isinstance(payload.get("slurm"), dict)
        else "",
        (payload.get("resource_request") or {}).get("job_id")
        if isinstance(payload.get("resource_request"), dict)
        else "",
        (
            ((payload.get("resource_request") or {}).get("slurm") or {}).get("job_id")
            if isinstance((payload.get("resource_request") or {}).get("slurm"), dict)
            else ""
        ),
    )
    for raw in candidates:
        value = str(raw or "").strip()
        if value:
            return value
    return ""


def _load_json_object(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _write_json_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, indent=2) + "\n"
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == rendered:
                return False
        except Exception:
            pass
    path.write_text(rendered, encoding="utf-8")
    return True


def _write_text_if_changed(path: Path, text: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except Exception:
            pass
    path.write_text(text, encoding="utf-8")
    return True


def _load_design_payload(iteration_dir: Path) -> dict[str, Any]:
    design_path = iteration_dir / "design.yaml"
    if not design_path.exists():
        raise StageCheckError(f"launch requires design.yaml at {design_path}")
    if yaml is None:
        raise StageCheckError("launch execution requires PyYAML")
    try:
        loaded = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageCheckError(f"could not parse design.yaml: {exc}") from exc
    if not isinstance(loaded, dict):
        raise StageCheckError("design.yaml must contain a mapping")
    return loaded


def _resolve_launch_mode(design_payload: dict[str, Any]) -> str:
    compute = design_payload.get("compute")
    if not isinstance(compute, dict):
        raise StageCheckError("design.yaml compute must be a mapping")
    mode = str(compute.get("location", "")).strip().lower()
    if mode not in {"local", "slurm"}:
        raise StageCheckError(
            "design.yaml compute.location must be 'local' or 'slurm' for launch execution"
        )
    return mode


def _parse_memory_to_mb(memory_str: str) -> int | None:
    """Parse a memory string like ``"4GB"``, ``"16384MB"`` to megabytes."""
    text = str(memory_str).strip().upper()
    if not text:
        return None
    match = re.match(r"^(\d+(?:\.\d+)?)\s*(TB|GB|MB|KB|B)?$", text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2) or "MB"
    multipliers = {
        "TB": 1_048_576,
        "GB": 1024,
        "MB": 1,
        "KB": 1 / 1024,
        "B": 1 / 1_048_576,
    }
    result = value * multipliers.get(unit, 1)
    return int(result) if result >= 0 else None


def _parse_walltime_to_seconds(walltime_str: str) -> int | None:
    """Parse ``HH:MM:SS`` or ``D-HH:MM:SS`` walltime to seconds."""
    text = str(walltime_str).strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        parts = text.split("-", 1)
        try:
            days = int(parts[0])
        except ValueError:
            return None
        text = parts[1]
    segments = text.split(":")
    try:
        int_segments = [int(s) for s in segments]
    except ValueError:
        return None
    if len(int_segments) == 3:
        hours, minutes, seconds = int_segments
    elif len(int_segments) == 2:
        hours = 0
        minutes, seconds = int_segments
    elif len(int_segments) == 1:
        return int_segments[0] + days * 86400
    else:
        return None
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _fits_current_allocation(
    design_payload: dict[str, Any], allocation: dict[str, Any]
) -> bool:
    """Return True if the experiment's compute needs fit within *allocation*.

    Missing allocation fields are treated as "fits" (permissive).  Walltime
    comparison uses a 90% margin to avoid cutting it too close.
    """
    compute = design_payload.get("compute")
    if not isinstance(compute, dict):
        return True

    # CPUs
    if "cpus" in allocation:
        try:
            needed = int(compute.get("cpus", 1))
        except (TypeError, ValueError):
            needed = 1
        if needed > allocation["cpus"]:
            return False

    # Memory
    if "memory_mb" in allocation:
        mem_str = str(
            compute.get("memory") or compute.get("memory_estimate") or ""
        ).strip()
        if mem_str:
            needed_mb = _parse_memory_to_mb(mem_str)
            if needed_mb is not None and needed_mb > allocation["memory_mb"]:
                return False

    # GPUs
    if "gpu_count" in allocation:
        try:
            needed_gpus = int(compute.get("gpus", compute.get("gpu_count", 0)))
        except (TypeError, ValueError):
            needed_gpus = 0
        if needed_gpus > allocation["gpu_count"]:
            return False

    # Walltime (90% margin)
    if "remaining_seconds" in allocation:
        wt_str = str(
            compute.get("walltime") or compute.get("walltime_estimate") or ""
        ).strip()
        if wt_str:
            needed_seconds = _parse_walltime_to_seconds(wt_str)
            if needed_seconds is not None:
                margin = allocation["remaining_seconds"] * 0.9
                if needed_seconds > margin:
                    return False

    return True


def _resolve_manifest_launch_mode(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    mode = (
        str(
            payload.get("host_mode")
            or payload.get("launch_mode")
            or payload.get("location")
            or ""
        )
        .strip()
        .lower()
    )
    if mode in {"local", "slurm"}:
        return mode

    resource_request = payload.get("resource_request")
    if isinstance(resource_request, dict):
        nested_mode = (
            str(
                resource_request.get("mode")
                or resource_request.get("host_mode")
                or resource_request.get("launch_mode")
                or resource_request.get("location")
                or ""
            )
            .strip()
            .lower()
        )
        if nested_mode in {"local", "slurm"}:
            return nested_mode
        nested_slurm = resource_request.get("slurm")
        if (
            isinstance(nested_slurm, dict)
            and str(nested_slurm.get("job_id", "")).strip()
        ):
            return "slurm"

    slurm_payload = payload.get("slurm")
    if isinstance(slurm_payload, dict) and str(slurm_payload.get("job_id", "")).strip():
        return "slurm"
    return ""


def _build_resource_request(
    design_payload: dict[str, Any],
    *,
    launch_mode: str,
    job_id: str = "",
) -> dict[str, Any]:
    compute = design_payload.get("compute")
    if not isinstance(compute, dict):
        compute = {}

    cpus_raw = compute.get("cpus", 1)
    try:
        cpus = int(cpus_raw)
    except Exception:
        cpus = 1
    if cpus < 0:
        cpus = 0

    gpu_raw = compute.get("gpus", compute.get("gpu_count", 0))
    try:
        gpu_count = int(gpu_raw)
    except Exception:
        gpu_count = 0
    if gpu_count < 0:
        gpu_count = 0

    memory = str(
        compute.get("memory")
        or compute.get("memory_estimate")
        or design_payload.get("memory_estimate")
        or ""
    ).strip() or ("4GB" if launch_mode == "local" else "16GB")

    payload: dict[str, Any] = {
        "cpus": cpus,
        "memory": memory,
        "gpu_count": gpu_count,
    }
    for key in ("gpu_type", "partition", "qos"):
        value = str(compute.get(key, "")).strip()
        if value:
            payload[key] = value

    if launch_mode == "slurm" and job_id:
        payload["job_id"] = job_id
        payload["slurm"] = {"job_id": job_id}

    return payload


def _ensure_logs_dir(run_dir: Path, changed_files: list[Path]) -> Path:
    logs_dir = run_dir / "logs"
    if not logs_dir.exists():
        logs_dir.mkdir(parents=True, exist_ok=True)
        changed_files.append(logs_dir)
    return logs_dir


def _check_launch_artifacts(run_dir: Path) -> bool:
    """Return True if *run_dir* contains at least one non-log output file.

    A launch subprocess that exits 0 but produces no usable output (e.g. the
    inner experiment failed with a codec error) should not be treated as a
    successful run.  This helper checks for evidence of real output beyond the
    ``logs/`` subdirectory.
    """
    if not run_dir.is_dir():
        return False
    for child in run_dir.iterdir():
        if child.name == "logs" or child.name == "run_manifest.json":
            continue
        # Any non-log, non-manifest entry counts as a real artifact.
        if child.is_file() and child.stat().st_size > 0:
            return True
        if child.is_dir() and any(child.iterdir()):
            return True
    return False


_FATAL_MARKER_PATTERNS = tuple(
    re.compile(pat)
    for pat in (
        r"RuntimeError:",
        r"Failed to initialize",
        r"Failed to open",
        r"\bFATAL\b",
        r"Segmentation fault",
        r"core dumped",
        r"Traceback \(most recent call last\)",
        r"CUDA error:",
        r"OutOfMemoryError",
        r"\bkilled\b",
    )
)


def _stderr_has_fatal_markers(stderr_text: str) -> str:
    """Return the first fatal marker found in *stderr_text*, or ``""``."""
    for pattern in _FATAL_MARKER_PATTERNS:
        if pattern.search(stderr_text):
            return pattern.pattern
    return ""


def _check_run_id_consistency(run_dir: Path, *, expected_run_id: str) -> str:
    """Check for run-ID drift: sibling dirs created after *run_dir* with wrong name.

    Returns a diagnostic message string (empty if no anomaly detected).
    """
    runs_parent = run_dir.parent  # …/runs/
    if not runs_parent.is_dir():
        return ""
    try:
        run_dir_mtime = run_dir.stat().st_mtime
    except OSError:
        return ""
    drifted: list[str] = []
    for sibling in runs_parent.iterdir():
        if not sibling.is_dir():
            continue
        if sibling.name == expected_run_id:
            continue
        try:
            if sibling.stat().st_mtime >= run_dir_mtime:
                drifted.append(sibling.name)
        except OSError:
            continue
    if not drifted:
        return ""
    return (
        f"run-id drift detected: expected artifacts under '{expected_run_id}' "
        f"but found sibling directories {drifted}"
    )


def _timestamp_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _manifest_payload(
    *,
    run_id: str,
    iteration_id: str,
    launch_mode: str,
    command: str,
    resource_request: dict[str, Any],
    status: str,
    sync_status: str,
    started_at: str,
    completed_at: str = "",
    job_id: str = "",
) -> dict[str, Any]:
    normalized_status = _normalize_status(status, fallback="failed")
    timestamps: dict[str, Any] = {"started_at": started_at}
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "iteration_id": iteration_id,
        "launch_mode": launch_mode,
        "host_mode": launch_mode,
        "command": command,
        "resource_request": resource_request,
        "status": normalized_status,
        "artifact_sync_to_local": {
            "status": str(sync_status).strip().lower() or "pending"
        },
        "timestamps": timestamps,
        "started_at": started_at,
    }
    if completed_at:
        timestamps["completed_at"] = completed_at
        payload["completed_at"] = completed_at
    if launch_mode == "slurm" and job_id:
        payload["job_id"] = job_id
        payload["slurm"] = {"job_id": job_id}
    return payload


def _local_skip_due_to_existing(
    *,
    manifest_payload: dict[str, Any] | None,
    logs_dir: Path,
) -> bool:
    if isinstance(manifest_payload, dict):
        status = str(manifest_payload.get("status", "")).strip().lower()
        timestamps = manifest_payload.get("timestamps")
        if (
            status in COMPLETION_LIKE_STATUSES
            and isinstance(timestamps, dict)
            and str(timestamps.get("started_at", "")).strip()
            and str(timestamps.get("completed_at", "")).strip()
        ):
            return True
    if logs_dir.exists():
        for _entry in logs_dir.iterdir():
            return True
    return False


def _slurm_skip_due_to_existing(manifest_payload: dict[str, Any] | None) -> bool:
    if not isinstance(manifest_payload, dict):
        return False
    status = str(manifest_payload.get("status", "")).strip().lower()
    if status not in IN_PROGRESS_STATUSES and status not in COMPLETION_LIKE_STATUSES:
        return False
    job_id = _extract_job_id(manifest_payload)
    return bool(job_id)


def _latest_existing_run_id(iteration_dir: Path) -> str:
    candidates: list[tuple[int, datetime, str, str]] = []
    for manifest_path in iteration_dir.glob("runs/*/run_manifest.json"):
        payload = _load_json_object(manifest_path)
        if not isinstance(payload, dict):
            continue
        run_id = str(payload.get("run_id", "")).strip() or manifest_path.parent.name
        parsed = _manifest_timestamp(payload, run_id)
        has_timestamp = 1 if parsed is not None else 0
        stamp = parsed or datetime.min.replace(tzinfo=timezone.utc)
        candidates.append((has_timestamp, stamp, run_id, str(manifest_path)))
    if not candidates:
        return ""
    _has_ts, _stamp, run_id, _path = max(
        candidates, key=lambda item: (item[0], item[1], item[2], item[3])
    )
    return run_id


def _append_slurm_ledger_if_needed(
    repo_root: Path,
    *,
    manifest_payload: dict[str, Any],
    changed_files: list[Path],
) -> None:
    doc_path = repo_root / SLURM_JOB_LIST_PATH
    canonical = canonical_slurm_job_bullet(manifest_payload)
    run_id = str(manifest_payload.get("run_id", "")).strip()
    existing_text = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""
    next_text, updated = append_entry_idempotent(existing_text, canonical, run_id)
    if not updated:
        return
    doc_path.parent.mkdir(parents=True, exist_ok=True)
    doc_path.write_text(next_text, encoding="utf-8")
    changed_files.append(doc_path)


def _normalize_local_existing_manifest(
    *,
    existing: dict[str, Any] | None,
    run_id: str,
    iteration_id: str,
    design_payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    status = "completed"
    started_at = _timestamp_now()
    completed_at = _timestamp_now()
    if isinstance(existing, dict):
        status = _normalize_status(
            str(existing.get("status", "")), fallback="completed"
        )
        timestamps = existing.get("timestamps")
        if isinstance(timestamps, dict):
            started_at = str(timestamps.get("started_at", "")).strip() or started_at
            if status in COMPLETION_LIKE_STATUSES:
                completed_at = (
                    str(timestamps.get("completed_at", "")).strip() or completed_at
                )
        else:
            started_at = str(existing.get("started_at", "")).strip() or started_at
            if status in COMPLETION_LIKE_STATUSES:
                completed_at = (
                    str(existing.get("completed_at", "")).strip() or completed_at
                )
        if status not in COMPLETION_LIKE_STATUSES:
            status = "completed"
            completed_at = _timestamp_now()

    payload = _manifest_payload(
        run_id=run_id,
        iteration_id=iteration_id,
        launch_mode="local",
        command="bash launch/run_local.sh",
        resource_request=_build_resource_request(design_payload, launch_mode="local"),
        status=status,
        sync_status="ok" if status == "completed" else "failed",
        started_at=started_at,
        completed_at=completed_at,
    )
    return payload, status != "failed"


def _normalize_slurm_existing_manifest(
    *,
    existing: dict[str, Any],
    run_id: str,
    iteration_id: str,
    design_payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    status = _normalize_status(str(existing.get("status", "")), fallback="submitted")
    job_id = _extract_job_id(existing)
    if not job_id:
        raise StageCheckError(
            f"existing SLURM manifest for run_id={run_id} is missing job_id"
        )

    timestamps = existing.get("timestamps")
    started_at = _timestamp_now()
    completed_at = ""
    if isinstance(timestamps, dict):
        started_at = str(timestamps.get("started_at", "")).strip() or started_at
        if status in COMPLETION_LIKE_STATUSES:
            completed_at = (
                str(timestamps.get("completed_at", "")).strip() or _timestamp_now()
            )
    elif status in COMPLETION_LIKE_STATUSES:
        completed_at = _timestamp_now()

    raw_sync_status = ""
    artifact_sync = existing.get("artifact_sync_to_local")
    if isinstance(artifact_sync, dict):
        raw_sync_status = str(artifact_sync.get("status", "")).strip().lower()
    if not raw_sync_status:
        raw_sync_status = "pending" if status in IN_PROGRESS_STATUSES else "failed"

    payload = _manifest_payload(
        run_id=run_id,
        iteration_id=iteration_id,
        launch_mode="slurm",
        command="sbatch launch/run_slurm.sbatch",
        resource_request=_build_resource_request(
            design_payload, launch_mode="slurm", job_id=job_id
        ),
        status=status,
        sync_status=raw_sync_status,
        started_at=started_at,
        completed_at=completed_at,
        job_id=job_id,
    )
    return payload, status != "failed"


def _execute_local_run(
    *,
    repo_root: Path,
    iteration_dir: Path,
    run_id: str,
    iteration_id: str,
    design_payload: dict[str, Any],
    timeout_seconds: float,
    changed_files: list[Path],
) -> tuple[dict[str, Any], bool]:
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = _ensure_logs_dir(run_dir, changed_files)
    manifest_path = run_dir / "run_manifest.json"
    existing_manifest = _load_json_object(manifest_path)

    if _local_skip_due_to_existing(
        manifest_payload=existing_manifest, logs_dir=logs_dir
    ):
        payload, success = _normalize_local_existing_manifest(
            existing=existing_manifest,
            run_id=run_id,
            iteration_id=iteration_id,
            design_payload=design_payload,
        )
        if _write_json_if_changed(manifest_path, payload):
            changed_files.append(manifest_path)
        if not any(logs_dir.iterdir()):
            note = f"{_utc_now()} launch skipped for run_id={run_id}: existing logs/manifest evidence detected\n"
            marker = logs_dir / "launch.skipped.log"
            if _write_text_if_changed(marker, note):
                changed_files.append(marker)
        _append_log(repo_root, f"launch local execution skipped run_id={run_id}")
        return payload, success

    started_at = _timestamp_now()
    env = os.environ.copy()
    env["AUTOLAB_RUN_ID"] = run_id
    env["RUN_ID"] = run_id
    env["AUTOLAB_ITERATION_ID"] = iteration_id
    command = ["bash", "launch/run_local.sh"]
    stdout_text = ""
    stderr_text = ""
    returncode: int | None = None
    timed_out = False
    try:
        proc = subprocess.run(
            command,
            cwd=iteration_dir,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
        returncode = int(proc.returncode)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_text = str(exc.stdout or "")
        stderr_text = str(exc.stderr or "")
    except OSError as exc:
        stderr_text = str(exc)

    stdout_path = logs_dir / "launch.stdout.log"
    stderr_path = logs_dir / "launch.stderr.log"
    if _write_text_if_changed(stdout_path, stdout_text):
        changed_files.append(stdout_path)
    if _write_text_if_changed(stderr_path, stderr_text):
        changed_files.append(stderr_path)

    command_text = "bash launch/run_local.sh"
    if timed_out or returncode is None or returncode != 0:
        completed_at = _timestamp_now()
        payload = _manifest_payload(
            run_id=run_id,
            iteration_id=iteration_id,
            launch_mode="local",
            command=command_text,
            resource_request=_build_resource_request(
                design_payload, launch_mode="local"
            ),
            status="failed",
            sync_status="failed",
            started_at=started_at,
            completed_at=completed_at,
        )
        if _write_json_if_changed(manifest_path, payload):
            changed_files.append(manifest_path)
        detail = (
            "timeout"
            if timed_out
            else (
                f"exit_code={returncode}"
                if returncode is not None
                else "command execution error"
            )
        )
        _append_log(
            repo_root,
            f"launch local execution failed run_id={run_id} detail={detail}",
        )
        return payload, False

    completed_at = _timestamp_now()
    fatal_marker = _stderr_has_fatal_markers(stderr_text)
    run_id_drift = _check_run_id_consistency(run_dir, expected_run_id=run_id)
    if fatal_marker:
        status = "failed"
        sync_status = "failed"
        _append_log(
            repo_root,
            f"launch local stderr contains fatal marker '{fatal_marker}' run_id={run_id}",
        )
    elif run_id_drift:
        status = "failed"
        sync_status = "failed"
        _append_log(
            repo_root,
            f"launch local {run_id_drift} run_id={run_id}",
        )
    elif _check_launch_artifacts(run_dir):
        status = "completed"
        sync_status = "ok"
    else:
        status = "partial"
        sync_status = "failed"
        _append_log(
            repo_root,
            f"launch local subprocess exited 0 but expected artifacts missing run_id={run_id}",
        )
    payload = _manifest_payload(
        run_id=run_id,
        iteration_id=iteration_id,
        launch_mode="local",
        command=command_text,
        resource_request=_build_resource_request(design_payload, launch_mode="local"),
        status=status,
        sync_status=sync_status,
        started_at=started_at,
        completed_at=completed_at,
    )
    if _write_json_if_changed(manifest_path, payload):
        changed_files.append(manifest_path)
    _append_log(repo_root, f"launch local execution {status} run_id={run_id}")
    return payload, status == "completed"


def _execute_slurm_submit(
    *,
    repo_root: Path,
    iteration_dir: Path,
    run_id: str,
    iteration_id: str,
    design_payload: dict[str, Any],
    timeout_seconds: float,
    changed_files: list[Path],
) -> tuple[dict[str, Any], bool]:
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = _ensure_logs_dir(run_dir, changed_files)
    manifest_path = run_dir / "run_manifest.json"
    existing_manifest = _load_json_object(manifest_path)
    if _slurm_skip_due_to_existing(existing_manifest):
        assert isinstance(existing_manifest, dict)
        payload, success = _normalize_slurm_existing_manifest(
            existing=existing_manifest,
            run_id=run_id,
            iteration_id=iteration_id,
            design_payload=design_payload,
        )
        if _write_json_if_changed(manifest_path, payload):
            changed_files.append(manifest_path)
        _append_slurm_ledger_if_needed(
            repo_root, manifest_payload=payload, changed_files=changed_files
        )
        _append_log(repo_root, f"launch slurm submit skipped run_id={run_id}")
        return payload, success

    started_at = _timestamp_now()
    command = ["sbatch", "launch/run_slurm.sbatch"]
    stdout_text = ""
    stderr_text = ""
    returncode: int | None = None
    timed_out = False
    try:
        proc = subprocess.run(
            command,
            cwd=iteration_dir,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
        returncode = int(proc.returncode)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_text = str(exc.stdout or "")
        stderr_text = str(exc.stderr or "")
    except OSError as exc:
        stderr_text = str(exc)

    stdout_path = logs_dir / "launch.stdout.log"
    stderr_path = logs_dir / "launch.stderr.log"
    if _write_text_if_changed(stdout_path, stdout_text):
        changed_files.append(stdout_path)
    if _write_text_if_changed(stderr_path, stderr_text):
        changed_files.append(stderr_path)

    command_text = "sbatch launch/run_slurm.sbatch"
    combined_output = f"{stdout_text}\n{stderr_text}"
    job_id = _parse_job_id(combined_output)
    if timed_out or returncode is None or returncode != 0 or not job_id:
        completed_at = _timestamp_now()
        payload = _manifest_payload(
            run_id=run_id,
            iteration_id=iteration_id,
            launch_mode="slurm",
            command=command_text,
            resource_request=_build_resource_request(
                design_payload, launch_mode="slurm"
            ),
            status="failed",
            sync_status="failed",
            started_at=started_at,
            completed_at=completed_at,
        )
        if _write_json_if_changed(manifest_path, payload):
            changed_files.append(manifest_path)
        detail = (
            "timeout"
            if timed_out
            else (
                f"exit_code={returncode}"
                if returncode is not None and returncode != 0
                else "missing job_id in sbatch output"
            )
        )
        _append_log(
            repo_root,
            f"launch slurm submit failed run_id={run_id} detail={detail} output={_compact_text(combined_output)}",
        )
        return payload, False

    payload = _manifest_payload(
        run_id=run_id,
        iteration_id=iteration_id,
        launch_mode="slurm",
        command=command_text,
        resource_request=_build_resource_request(
            design_payload, launch_mode="slurm", job_id=job_id
        ),
        status="submitted",
        sync_status="pending",
        started_at=started_at,
        job_id=job_id,
    )
    if _write_json_if_changed(manifest_path, payload):
        changed_files.append(manifest_path)
    _append_slurm_ledger_if_needed(
        repo_root, manifest_payload=payload, changed_files=changed_files
    )
    _append_log(
        repo_root, f"launch slurm submit completed run_id={run_id} job_id={job_id}"
    )
    return payload, True


def _execute_slurm_interactive_run(
    *,
    repo_root: Path,
    iteration_dir: Path,
    run_id: str,
    iteration_id: str,
    design_payload: dict[str, Any],
    timeout_seconds: float,
    changed_files: list[Path],
) -> tuple[dict[str, Any], bool]:
    """Run ``bash launch/run_slurm.sbatch`` directly on the interactive allocation."""
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = _ensure_logs_dir(run_dir, changed_files)
    manifest_path = run_dir / "run_manifest.json"
    existing_manifest = _load_json_object(manifest_path)

    if _local_skip_due_to_existing(
        manifest_payload=existing_manifest, logs_dir=logs_dir
    ):
        payload, success = _normalize_local_existing_manifest(
            existing=existing_manifest if isinstance(existing_manifest, dict) else None,
            run_id=run_id,
            iteration_id=iteration_id,
            design_payload=design_payload,
        )
        # Fixup: host_mode/launch_mode/command must reflect slurm for verifier consistency
        payload["host_mode"] = "slurm"
        payload["launch_mode"] = "slurm"
        payload["command"] = "bash launch/run_slurm.sbatch"
        if _write_json_if_changed(manifest_path, payload):
            changed_files.append(manifest_path)
        _append_log(
            repo_root,
            f"launch slurm-interactive execution skipped run_id={run_id}",
        )
        return payload, success

    started_at = _timestamp_now()
    env = os.environ.copy()
    env["AUTOLAB_RUN_ID"] = run_id
    env["RUN_ID"] = run_id
    env["AUTOLAB_ITERATION_ID"] = iteration_id
    command = ["bash", "launch/run_slurm.sbatch"]
    stdout_text = ""
    stderr_text = ""
    returncode: int | None = None
    timed_out = False
    try:
        proc = subprocess.run(
            command,
            cwd=iteration_dir,
            env=env,
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout_seconds,
        )
        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""
        returncode = int(proc.returncode)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_text = str(exc.stdout or "")
        stderr_text = str(exc.stderr or "")
    except OSError as exc:
        stderr_text = str(exc)

    stdout_path = logs_dir / "launch.stdout.log"
    stderr_path = logs_dir / "launch.stderr.log"
    if _write_text_if_changed(stdout_path, stdout_text):
        changed_files.append(stdout_path)
    if _write_text_if_changed(stderr_path, stderr_text):
        changed_files.append(stderr_path)

    slurm_job_id = os.environ.get("SLURM_JOB_ID", "").strip()
    slurm_env_metadata = _collect_slurm_env_metadata()
    command_text = "bash launch/run_slurm.sbatch"

    if timed_out or returncode is None or returncode != 0:
        completed_at = _timestamp_now()
        payload = _manifest_payload(
            run_id=run_id,
            iteration_id=iteration_id,
            launch_mode="slurm",
            command=command_text,
            resource_request=_build_resource_request(
                design_payload, launch_mode="slurm", job_id=slurm_job_id
            ),
            status="failed",
            sync_status="failed",
            started_at=started_at,
            completed_at=completed_at,
            job_id=slurm_job_id,
        )
        payload["slurm_environment"] = slurm_env_metadata
        if _write_json_if_changed(manifest_path, payload):
            changed_files.append(manifest_path)
        detail = (
            "timeout"
            if timed_out
            else (
                f"exit_code={returncode}"
                if returncode is not None
                else "command execution error"
            )
        )
        _append_log(
            repo_root,
            f"launch slurm-interactive execution failed run_id={run_id} detail={detail}",
        )
        return payload, False

    completed_at = _timestamp_now()
    fatal_marker = _stderr_has_fatal_markers(stderr_text)
    run_id_drift = _check_run_id_consistency(run_dir, expected_run_id=run_id)
    if fatal_marker:
        status = "failed"
        sync_status = "failed"
        _append_log(
            repo_root,
            f"launch slurm-interactive stderr contains fatal marker '{fatal_marker}' run_id={run_id}",
        )
    elif run_id_drift:
        status = "failed"
        sync_status = "failed"
        _append_log(
            repo_root,
            f"launch slurm-interactive {run_id_drift} run_id={run_id}",
        )
    elif _check_launch_artifacts(run_dir):
        status = "completed"
        sync_status = "ok"
    else:
        status = "partial"
        sync_status = "failed"
        _append_log(
            repo_root,
            f"launch slurm-interactive subprocess exited 0 but expected artifacts missing run_id={run_id}",
        )
    payload = _manifest_payload(
        run_id=run_id,
        iteration_id=iteration_id,
        launch_mode="slurm",
        command=command_text,
        resource_request=_build_resource_request(
            design_payload, launch_mode="slurm", job_id=slurm_job_id
        ),
        status=status,
        sync_status=sync_status,
        started_at=started_at,
        completed_at=completed_at,
        job_id=slurm_job_id,
    )
    payload["slurm_environment"] = slurm_env_metadata
    if _write_json_if_changed(manifest_path, payload):
        changed_files.append(manifest_path)
    if status == "completed":
        _append_slurm_ledger_if_needed(
            repo_root, manifest_payload=payload, changed_files=changed_files
        )
    _append_log(
        repo_root,
        f"launch slurm-interactive execution {status} run_id={run_id} job_id={slurm_job_id}",
    )
    return payload, status == "completed"


def _write_group_manifest(
    *,
    repo_root: Path,
    iteration_dir: Path,
    base_run_id: str,
    iteration_id: str,
    launch_mode: str,
    design_payload: dict[str, Any],
    first_payload: dict[str, Any],
    changed_files: list[Path],
) -> dict[str, Any]:
    run_dir = iteration_dir / "runs" / base_run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = _ensure_logs_dir(run_dir, changed_files)
    marker = logs_dir / "launch.group.log"
    marker_text = (
        f"{_utc_now()} grouped manifest for run_id={base_run_id} "
        f"derived from first replicate run_id={first_payload.get('run_id', '')}\n"
    )
    if _write_text_if_changed(marker, marker_text):
        changed_files.append(marker)

    timestamps = first_payload.get("timestamps")
    started_at = _timestamp_now()
    completed_at = ""
    if isinstance(timestamps, dict):
        started_at = str(timestamps.get("started_at", "")).strip() or started_at
        completed_at = str(timestamps.get("completed_at", "")).strip()

    if launch_mode == "local":
        status = "completed"
        sync_status = "ok"
        if not completed_at:
            completed_at = _timestamp_now()
        job_id = ""
    else:
        status = "submitted"
        sync_status = "pending"
        completed_at = ""
        job_id = _extract_job_id(first_payload)

    payload = _manifest_payload(
        run_id=base_run_id,
        iteration_id=iteration_id,
        launch_mode=launch_mode,
        command=(
            "bash launch/run_local.sh"
            if launch_mode == "local"
            else "sbatch launch/run_slurm.sbatch"
        ),
        resource_request=_build_resource_request(
            design_payload, launch_mode=launch_mode, job_id=job_id
        ),
        status=status,
        sync_status=sync_status,
        started_at=started_at,
        completed_at=completed_at,
        job_id=job_id,
    )
    manifest_path = run_dir / "run_manifest.json"
    if _write_json_if_changed(manifest_path, payload):
        changed_files.append(manifest_path)
    if launch_mode == "slurm":
        _append_slurm_ledger_if_needed(
            repo_root, manifest_payload=payload, changed_files=changed_files
        )
    return payload


def _dedupe_paths(paths: list[Path]) -> tuple[Path, ...]:
    ordered: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return tuple(ordered)


def _maybe_adopt_existing_run_id(
    *,
    repo_root: Path,
    state: dict[str, Any],
    iteration_dir: Path,
    base_run_id: str,
    run_group: list[str],
) -> str:
    if run_group:
        return base_run_id
    if not base_run_id:
        return base_run_id
    if int(state.get("stage_attempt", 0) or 0) != 0:
        return base_run_id
    if str(state.get("last_run_id", "")).strip():
        return base_run_id
    manifest_for_pending = iteration_dir / "runs" / base_run_id / "run_manifest.json"
    if manifest_for_pending.exists():
        return base_run_id

    existing_run_id = _latest_existing_run_id(iteration_dir)
    if not existing_run_id:
        return base_run_id
    state["pending_run_id"] = existing_run_id
    _append_log(
        repo_root,
        f"launch adopted existing run_id={existing_run_id} to avoid duplicate execution",
    )
    return existing_run_id


def _sync_status_from_manifest(payload: dict[str, Any], launch_mode: str) -> str:
    sync = payload.get("artifact_sync_to_local")
    sync_status = ""
    if isinstance(sync, dict):
        sync_status = str(sync.get("status", "")).strip().lower()
    if launch_mode == "local":
        return (
            "completed"
            if sync_status in {"ok", "completed", "success", "passed"}
            else (sync_status or "failed")
        )
    return sync_status or "pending"


def _execute_launch_runtime(
    repo_root: Path, *, state: dict[str, Any]
) -> LaunchExecutionResult:
    config = _load_launch_runtime_config(repo_root)
    if not config.execute:
        run_id = (
            str(state.get("pending_run_id", "")).strip()
            or str(state.get("last_run_id", "")).strip()
        )
        if not run_id:
            raise StageCheckError("launch execution disabled but run_id is missing")
        _append_log(repo_root, f"launch execution disabled by policy run_id={run_id}")
        return LaunchExecutionResult(
            run_id=run_id,
            sync_status=str(state.get("sync_status", "")).strip() or "na",
            changed_files=(),
        )

    iteration_id = str(state.get("iteration_id", "")).strip()
    if not iteration_id:
        raise StageCheckError("launch execution requires state.iteration_id")
    iteration_dir, _iteration_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=str(state.get("experiment_id", "")).strip(),
        require_exists=False,
    )
    base_run_id = (
        str(state.get("pending_run_id", "")).strip()
        or str(state.get("last_run_id", "")).strip()
    )
    if not base_run_id:
        raise StageCheckError("launch execution requires pending_run_id or last_run_id")

    run_group_raw = state.get("run_group", [])
    run_group = (
        [str(rid).strip() for rid in run_group_raw if str(rid).strip()]
        if isinstance(run_group_raw, list)
        else []
    )
    base_run_id = _maybe_adopt_existing_run_id(
        repo_root=repo_root,
        state=state,
        iteration_dir=iteration_dir,
        base_run_id=base_run_id,
        run_group=run_group,
    )
    run_ids = run_group if run_group else [base_run_id]
    if not run_ids:
        raise StageCheckError("launch execution resolved empty run id list")

    design_payload: dict[str, Any] = {}
    launch_mode = ""
    design_error: StageCheckError | None = None
    try:
        design_payload = _load_design_payload(iteration_dir)
        launch_mode = _resolve_launch_mode(design_payload)
    except StageCheckError as exc:
        design_error = exc

    if not launch_mode:
        for run_id in run_ids:
            manifest_payload = _load_json_object(
                iteration_dir / "runs" / run_id / "run_manifest.json"
            )
            launch_mode = _resolve_manifest_launch_mode(manifest_payload)
            if launch_mode:
                _append_log(
                    repo_root,
                    (
                        f"launch execution using manifest-derived mode={launch_mode} "
                        f"run_id={run_id} (design unavailable)"
                    ),
                )
                break
    if not launch_mode:
        assert design_error is not None
        raise design_error

    local_script = iteration_dir / "launch" / "run_local.sh"
    slurm_script = iteration_dir / "launch" / "run_slurm.sbatch"
    if launch_mode == "local" and not local_script.exists():
        raise StageCheckError(f"local launch script missing at {local_script}")
    if launch_mode == "slurm" and not slurm_script.exists():
        raise StageCheckError(f"slurm launch script missing at {slurm_script}")

    # Determine if we should run directly on an interactive SLURM allocation
    use_slurm_interactive = False
    if launch_mode == "slurm":
        allocation = _get_slurm_allocation_resources()
        if (
            _is_slurm_interactive_session()
            and allocation
            and _fits_current_allocation(design_payload, allocation)
        ):
            use_slurm_interactive = True
            _append_log(
                repo_root,
                "launch: SLURM interactive session detected, resources fit — running directly",
            )

    changed_files: list[Path] = []
    per_run_payloads: list[dict[str, Any]] = []
    for run_id in run_ids:
        if launch_mode == "local":
            payload, success = _execute_local_run(
                repo_root=repo_root,
                iteration_dir=iteration_dir,
                run_id=run_id,
                iteration_id=iteration_id,
                design_payload=design_payload,
                timeout_seconds=float(config.local_timeout_seconds),
                changed_files=changed_files,
            )
        elif use_slurm_interactive:
            payload, success = _execute_slurm_interactive_run(
                repo_root=repo_root,
                iteration_dir=iteration_dir,
                run_id=run_id,
                iteration_id=iteration_id,
                design_payload=design_payload,
                timeout_seconds=float(config.local_timeout_seconds),
                changed_files=changed_files,
            )
        else:
            payload, success = _execute_slurm_submit(
                repo_root=repo_root,
                iteration_dir=iteration_dir,
                run_id=run_id,
                iteration_id=iteration_id,
                design_payload=design_payload,
                timeout_seconds=float(config.slurm_submit_timeout_seconds),
                changed_files=changed_files,
            )
        per_run_payloads.append(payload)
        if not success:
            state["last_run_id"] = base_run_id
            state["sync_status"] = _sync_status_from_manifest(payload, launch_mode)
            raise StageCheckError(
                f"launch execution failed for run_id={run_id} status={payload.get('status', 'failed')}"
            )

    base_payload = per_run_payloads[0]
    if run_group and base_run_id not in run_group:
        base_payload = _write_group_manifest(
            repo_root=repo_root,
            iteration_dir=iteration_dir,
            base_run_id=base_run_id,
            iteration_id=iteration_id,
            launch_mode=launch_mode,
            design_payload=design_payload,
            first_payload=per_run_payloads[0],
            changed_files=changed_files,
        )

    state["last_run_id"] = base_run_id
    state["sync_status"] = _sync_status_from_manifest(base_payload, launch_mode)
    _append_log(
        repo_root,
        f"launch runtime complete run_id={base_run_id} mode={launch_mode} sync_status={state['sync_status']}",
    )
    return LaunchExecutionResult(
        run_id=base_run_id,
        sync_status=str(state["sync_status"]).strip() or "na",
        changed_files=_dedupe_paths(changed_files),
    )
