from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Mapping


_RUN_ID_TIMESTAMP_PATTERN = re.compile(r"(20\d{2})(\d{2})(\d{2})T\d{6}Z")
_DATE_PREFIX_PATTERN = re.compile(r"^(20\d{2}-\d{2}-\d{2})")


def _read_nested(mapping: Mapping[str, Any], *path: str) -> Any:
    current: Any = mapping
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def is_slurm_manifest(manifest: Mapping[str, Any]) -> bool:
    """Return True when run metadata indicates SLURM execution."""
    mode_candidates = (
        manifest.get("location"),
        manifest.get("launch_mode"),
        manifest.get("host_mode"),
        manifest.get("detected_host_mode"),
        _read_nested(manifest, "launch", "mode"),
        _read_nested(manifest, "host", "mode"),
        _read_nested(manifest, "resource_request", "mode"),
        _read_nested(manifest, "resource_request", "location"),
    )
    for candidate in mode_candidates:
        value = str(candidate or "").strip().lower()
        if value == "slurm":
            return True

    slurm_sections = (
        manifest.get("slurm"),
        _read_nested(manifest, "resource_request", "slurm"),
    )
    return any(isinstance(section, Mapping) and bool(section) for section in slurm_sections)


def required_slurm_job_id(manifest: Mapping[str, Any]) -> str:
    """Extract required SLURM job id for a SLURM manifest."""
    if not is_slurm_manifest(manifest):
        raise ValueError("manifest is not a SLURM run")

    candidates = (
        _read_nested(manifest, "slurm", "job_id"),
        _read_nested(manifest, "resource_request", "slurm", "job_id"),
        _read_nested(manifest, "resource_request", "job_id"),
        manifest.get("job_id"),
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if value:
            return value

    raise ValueError("SLURM run manifest is missing required job_id")


def required_run_id(manifest: Mapping[str, Any]) -> str:
    run_id = str(manifest.get("run_id", "")).strip()
    if not run_id:
        raise ValueError("run manifest is missing required run_id")
    return run_id


def required_iteration_id(manifest: Mapping[str, Any]) -> str:
    iteration_id = str(manifest.get("iteration_id", "")).strip()
    if not iteration_id:
        raise ValueError("run manifest is missing required iteration_id")
    return iteration_id


def _parse_iso_date(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None

    prefix_match = _DATE_PREFIX_PATTERN.match(text)
    if prefix_match:
        return prefix_match.group(1)

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).date().isoformat()


def submission_date_yyyy_mm_dd(manifest: Mapping[str, Any]) -> str:
    """Return canonical submission date from run_manifest metadata."""
    candidates = (
        manifest.get("timestamp_utc"),
        _read_nested(manifest, "launch_snapshot", "launched_at_utc"),
        manifest.get("started_at"),
        manifest.get("created_at"),
        manifest.get("timestamp"),
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        parsed = _parse_iso_date(value)
        if parsed:
            return parsed

    run_id = str(manifest.get("run_id", "")).strip()
    match = _RUN_ID_TIMESTAMP_PATTERN.search(run_id)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month}-{day}"

    raise ValueError("run manifest is missing a parseable canonical launch timestamp")


def canonical_slurm_job_bullet(manifest: Mapping[str, Any]) -> str:
    """Build canonical SLURM ledger bullet line for a run manifest."""
    date_text = submission_date_yyyy_mm_dd(manifest)
    job_id = required_slurm_job_id(manifest)
    iteration_id = required_iteration_id(manifest)
    run_id = required_run_id(manifest)
    status = str(manifest.get("status", "")).strip() or "unknown"
    return (
        f"- {date_text} | job_id={job_id} | iteration_id={iteration_id} | "
        f"run_id={run_id} | status={status}"
    )


def ledger_contains_run_id(ledger_text: str, run_id: str) -> bool:
    token = f"run_id={run_id}"
    return token in ledger_text


def ledger_contains_entry(ledger_text: str, entry: str) -> bool:
    target = entry.strip()
    return any(line.strip() == target for line in ledger_text.splitlines())


def append_entry_idempotent(ledger_text: str, entry: str, run_id: str) -> tuple[str, bool]:
    """Append canonical entry only when run_id is not already present."""
    if ledger_contains_run_id(ledger_text, run_id):
        return ledger_text, False

    base = ledger_text.rstrip("\n")
    if not base:
        return f"{entry}\n", True
    return f"{base}\n{entry}\n", True
