"""Shared helpers for autolab verifier scripts.

Centralises constants and utilities that were duplicated across all 11
verifier scripts (REPO_ROOT, EXPERIMENT_TYPES, iteration dir resolution,
state loading, file loaders, and JSON envelope construction).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml
except Exception:  # pragma: no cover
    _yaml = None

# ---------------------------------------------------------------------------
# Repository layout constants
# ---------------------------------------------------------------------------

REPO_ROOT: Path = Path(__file__).resolve().parents[2]
STATE_FILE: Path = REPO_ROOT / ".autolab" / "state.json"
EXPERIMENT_TYPES: tuple[str, ...] = ("plan", "in_progress", "done")
DEFAULT_EXPERIMENT_TYPE: str = "plan"

# ---------------------------------------------------------------------------
# Canonical status vocabularies
# ---------------------------------------------------------------------------

SYNC_SUCCESS_STATUSES: frozenset[str] = frozenset(
    {"ok", "completed", "success", "passed"}
)
COMPLETION_LIKE_STATUSES: frozenset[str] = frozenset({"completed", "failed"})
IN_PROGRESS_STATUSES: frozenset[str] = frozenset(
    {"pending", "submitted", "running", "synced"}
)
RUN_MANIFEST_STATUSES: frozenset[str] = frozenset(
    {
        "pending",
        "submitted",
        "running",
        "synced",
        "completed",
        "failed",
        "partial",
    }
)


# ---------------------------------------------------------------------------
# Iteration directory resolution
# ---------------------------------------------------------------------------


def resolve_iteration_dir(iteration_id: str) -> Path:
    """Locate the experiment directory for *iteration_id*.

    Searches ``experiments/<type>/<iteration_id>`` for each experiment type
    and returns the first match, falling back to ``plan/``.
    """
    normalized = str(iteration_id).strip()
    if not normalized:
        raise RuntimeError("iteration_id is required")
    experiments_root = REPO_ROOT / "experiments"
    for experiment_type in EXPERIMENT_TYPES:
        candidate = experiments_root / experiment_type / normalized
        if candidate.exists():
            return candidate
    return experiments_root / DEFAULT_EXPERIMENT_TYPE / normalized


# ---------------------------------------------------------------------------
# File loaders
# ---------------------------------------------------------------------------


def load_state() -> dict[str, Any]:
    """Load and validate ``.autolab/state.json``."""
    if not STATE_FILE.exists():
        raise RuntimeError("Missing .autolab/state.json")
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("state.json must contain an object")
    return data


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON file and ensure it contains an object."""
    if not path.exists():
        raise RuntimeError(f"{path} is missing")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return payload


def load_yaml(path: Path) -> dict[str, Any]:
    """Load a YAML file and ensure it contains a mapping."""
    if _yaml is None:
        raise RuntimeError("PyYAML is required")
    if not path.exists():
        raise RuntimeError(f"{path} is missing")
    payload = _yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping")
    return payload


# ---------------------------------------------------------------------------
# JSON envelope helpers
# ---------------------------------------------------------------------------


def make_result(
    verifier: str,
    stage: str,
    checks: list[dict[str, Any]],
    errors: list[str],
) -> dict[str, Any]:
    """Build a standard ``--json`` verifier envelope."""
    return {
        "status": "fail" if errors else "pass",
        "verifier": verifier,
        "stage": stage,
        "checks": checks,
        "errors": errors,
    }


def print_result(result: dict[str, Any], *, as_json: bool) -> None:
    """Print *result* in JSON or human-friendly format."""
    verifier = result.get("verifier", "verifier")
    if as_json:
        print(json.dumps(result))
    else:
        errors = result.get("errors", [])
        if errors:
            print(f"{verifier}: FAIL")
            for err in errors:
                print(err)
        else:
            print(f"{verifier}: PASS")
