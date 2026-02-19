"""Shared helpers for autolab verifier scripts.

Centralises constants and utilities that were duplicated across all 11
verifier scripts (REPO_ROOT, EXPERIMENT_TYPES, iteration dir resolution,
state loading, file loaders, and JSON envelope construction).
"""

from __future__ import annotations

import json
import re
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
SYNC_STATUS_CANONICAL: frozenset[str] = frozenset(
    {"pending", "syncing", "ok", "failed"}
)
SYNC_STATUS_SYNONYMS: dict[str, str] = {
    "queued": "pending",
    "submitted": "pending",
    "not_started": "pending",
    "na": "pending",
    "running": "syncing",
    "in_progress": "syncing",
    "completed": "ok",
    "success": "ok",
    "passed": "ok",
    "error": "failed",
    "fail": "failed",
}
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


def normalize_sync_status(value: object) -> str:
    """Normalize artifact_sync_to_local.status into canonical vocabulary.

    Canonical values are ``pending``, ``syncing``, ``ok``, and ``failed``.
    Returns an empty string for missing/unknown values.
    """
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    if raw in SYNC_STATUS_CANONICAL:
        return raw
    return SYNC_STATUS_SYNONYMS.get(raw, "")


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


_HINT_RULES: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (
        re.compile(r"unsupported token|missing required token|prompt", re.IGNORECASE),
        "Align prompt token usage with stage required/optional token contracts in workflow and prompt files.",
        "python .autolab/verifiers/prompt_lint.py --stage {stage} --json",
    ),
    (
        re.compile(
            r"schema violation|schema_version|required_checks|must contain|missing required|required key",
            re.IGNORECASE,
        ),
        "Fix artifact fields/types to satisfy schema and stage contract requirements.",
        "python .autolab/verifiers/schema_checks.py --stage {stage} --json",
    ),
    (
        re.compile(
            r"artifact_sync_to_local|run_manifest|host_mode|slurm|job_list|ledger",
            re.IGNORECASE,
        ),
        "Reconcile manifest host/sync status and SLURM ledger entries before advancing stages.",
        "python .autolab/verifiers/run_health.py --json",
    ),
    (
        re.compile(
            r"docs_update|paper target|paper_targets|primary metric|metrics artifact|manifest artifact",
            re.IGNORECASE,
        ),
        "Ensure docs_update includes primary metric triplet and exact run artifact paths.",
        "python .autolab/verifiers/docs_targets.py --json",
    ),
    (
        re.compile(
            r"implementation_plan|depends_on|touches|scope_ok|change summary|outside allowed scope",
            re.IGNORECASE,
        ),
        "Repair implementation plan task-block fields and scope evidence.",
        "python .autolab/verifiers/implementation_plan_lint.py --stage implementation --json",
    ),
    (
        re.compile(r"placeholder|todo|tbd|fixme|ellipsis|template", re.IGNORECASE),
        "Replace placeholders/template boilerplate with concrete stage artifacts.",
        "python .autolab/verifiers/template_fill.py --stage {stage} --json",
    ),
    (
        re.compile(r"consistency|evidence pointer|run_id mismatch", re.IGNORECASE),
        "Resolve cross-artifact IDs/metric names/evidence pointers.",
        "python .autolab/verifiers/consistency_checks.py --stage {stage} --json",
    ),
)


def suggest_fix_hints(
    errors: list[str],
    *,
    stage: str = "",
    verifier: str = "",
) -> list[str]:
    """Map verifier error text to actionable fix hints."""
    if not errors:
        return []

    resolved_stage = stage or "design"
    hints: list[str] = []
    for pattern, hint, command in _HINT_RULES:
        if not any(pattern.search(str(error)) for error in errors):
            continue
        command_text = command.format(stage=resolved_stage)
        rendered = f"{hint} Next: `{command_text}`"
        if rendered not in hints:
            hints.append(rendered)

    if hints:
        return hints
    fallback_command = (
        f"autolab verify --stage {resolved_stage}"
        if resolved_stage
        else "autolab verify"
    )
    return [
        (
            "Re-run stage verification and inspect the first failing verifier output in detail. "
            f"Next: `{fallback_command}`"
        )
    ]


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
            stage = str(result.get("stage", "")).strip()
            hints = suggest_fix_hints(
                [str(error) for error in errors],
                stage=stage,
                verifier=verifier,
            )
            if hints:
                print("\nMost likely fixes:")
                for hint in hints:
                    print(f"- {hint}")
        else:
            print(f"{verifier}: PASS")
