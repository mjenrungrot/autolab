"""Checkpoint management: create, list, restore, verify, and context-rot detection."""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from autolab.scope import _resolve_scope_context
from autolab.state import _resolve_iteration_directory, _resolve_repo_root
from autolab.utils import (
    _append_log,
    _load_json_if_exists,
    _path_fingerprint,
    _utc_now,
    _write_json,
)

# ---------------------------------------------------------------------------
# Stage ordering and canonical artifact map
# ---------------------------------------------------------------------------

STAGE_ORDER = [
    "hypothesis",
    "design",
    "implementation",
    "implementation_review",
    "update_docs",
    "decide_repeat",
]

AUTO_CHECKPOINT_STAGES = set(STAGE_ORDER)

_STAGE_ARTIFACTS: dict[str, list[str]] = {
    "hypothesis": [
        "hypothesis.md",
    ],
    "design": [
        "design.yaml",
        "design_context_quality.json",
    ],
    "implementation": [
        "implementation_plan.md",
        "plan_contract.json",
        "plan_graph.json",
        "plan_check_result.json",
        "plan_execution_state.json",
        "plan_execution_summary.json",
        "plan_approval.json",
    ],
    "implementation_review": [
        "implementation_review.md",
        "review_result.json",
    ],
    "update_docs": [
        "docs_update.md",
    ],
    "decide_repeat": [
        "decision_result.json",
        "traceability_coverage.json",
    ],
}

_OPTIONAL_ARTIFACTS = [
    "uat.md",
    "context/sidecars/discuss.json",
    "context/sidecars/research.json",
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ID_SAFE = re.compile(r"[^a-zA-Z0-9_-]")
_CHECKPOINT_ID_PATTERN = re.compile(r"^cp_[a-zA-Z0-9_-]+$")


def _build_checkpoint_id(stage: str, label: str = "") -> str:
    ts = _utc_now().replace(":", "").replace("-", "").replace("Z", "")
    suffix = uuid.uuid4().hex[:6]
    safe_label = _ID_SAFE.sub("_", label.strip())[:40] if label.strip() else "auto"
    return f"cp_{ts}_{stage}_{safe_label}_{suffix}"


def _validate_checkpoint_id(checkpoint_id: str) -> bool:
    """Return True if checkpoint_id is safe for path construction."""
    return bool(
        checkpoint_id
        and _CHECKPOINT_ID_PATTERN.match(checkpoint_id)
        and ".." not in checkpoint_id
    )


def _checkpoint_has_user_label(entry: dict[str, Any]) -> bool:
    label = str(entry.get("label", "")).strip()
    if not label:
        return False
    label_origin = str(entry.get("label_origin", "")).strip().lower()
    if label_origin == "user":
        return True
    if label_origin == "system":
        return False
    return str(entry.get("trigger", "")).strip().lower() == "manual"


def _is_checkpoint_protected(entry: dict[str, Any]) -> bool:
    if "gc_protected" in entry:
        return bool(entry.get("gc_protected", False))
    return bool(entry.get("pinned", False)) or _checkpoint_has_user_label(entry)


def _safe_rel_path(base: Path, rel: str) -> Path | None:
    """Resolve *rel* under *base*, returning None if it escapes."""
    if not rel or rel.startswith("/") or "\\" in rel:
        return None
    resolved = (base / rel).resolve()
    try:
        resolved.relative_to(base.resolve())
    except ValueError:
        return None
    return resolved


def _resolve_revision_label(repo_root: Path) -> str:
    from autolab.remote_profiles import resolve_workspace_revision

    try:
        revision = resolve_workspace_revision(repo_root)
    except Exception:
        return "unversioned-worktree"
    return revision.label or "unversioned-worktree"


def _collect_canonical_artifacts(
    repo_root: Path,
    iteration_dir: Path | None,
    stage: str,
    scope_kind: str,
) -> list[tuple[Path, str]]:
    """Collect artifacts up to and including *stage*. Returns (abs_path, rel_path) pairs."""
    artifacts: list[tuple[Path, str]] = []
    if iteration_dir is None:
        return artifacts

    stage_idx = STAGE_ORDER.index(stage) if stage in STAGE_ORDER else -1
    for i, s in enumerate(STAGE_ORDER):
        if i > stage_idx:
            break
        for rel in _STAGE_ARTIFACTS.get(s, []):
            abs_path = iteration_dir / rel
            if abs_path.exists() and not abs_path.is_symlink():
                try:
                    rel_to_repo = str(abs_path.relative_to(repo_root))
                except ValueError:
                    continue  # skip artifacts outside repo
                artifacts.append((abs_path, rel_to_repo))

    # Optional artifacts
    for rel in _OPTIONAL_ARTIFACTS:
        abs_path = iteration_dir / rel
        if abs_path.exists() and not abs_path.is_symlink():
            try:
                rel_to_repo = str(abs_path.relative_to(repo_root))
            except ValueError:
                continue
            artifacts.append((abs_path, rel_to_repo))

    # Project-wide sidecars
    if scope_kind == "project_wide":
        pw_sidecars = repo_root / ".autolab" / "context" / "sidecars"
        if pw_sidecars.is_dir():
            for sidecar in sorted(pw_sidecars.iterdir()):
                if (
                    sidecar.is_file()
                    and sidecar.suffix == ".json"
                    and not sidecar.is_symlink()
                ):
                    try:
                        rel_to_repo = str(sidecar.relative_to(repo_root))
                    except ValueError:
                        continue
                    artifacts.append((sidecar, rel_to_repo))

    return artifacts


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_checkpoint(
    repo_root: Path,
    state_path: Path,
    stage: str,
    trigger: str,
    label: str = "",
    *,
    iteration_id: str = "",
    experiment_id: str = "",
    scope_kind: str = "",
    pinned: bool = False,
    label_origin: str = "",
) -> tuple[str, Path]:
    """Create a checkpoint. Returns (checkpoint_id, checkpoint_dir)."""
    state = _load_json_if_exists(state_path)
    if state is None:
        state = {}

    iteration_id = iteration_id or str(state.get("iteration_id", "")).strip()
    experiment_id = experiment_id or str(state.get("experiment_id", "")).strip()

    iteration_dir: Path | None = None
    if iteration_id:
        try:
            iteration_dir, _ = _resolve_iteration_directory(
                repo_root, iteration_id=iteration_id, experiment_id=experiment_id
            )
        except Exception:
            iteration_dir = None

    if not scope_kind:
        try:
            scope_kind, _, _ = _resolve_scope_context(
                repo_root, iteration_id=iteration_id, experiment_id=experiment_id
            )
        except Exception:
            scope_kind = "experiment"

    checkpoint_id = _build_checkpoint_id(stage, label)
    autolab_dir = repo_root / ".autolab"
    checkpoint_dir = autolab_dir / "checkpoints" / checkpoint_id
    checkpoint_dir.mkdir(parents=True, exist_ok=True)

    artifacts = _collect_canonical_artifacts(
        repo_root, iteration_dir, stage, scope_kind
    )
    artifact_entries: list[dict[str, Any]] = []
    files_dir = checkpoint_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    for abs_path, rel_path in artifacts:
        if abs_path.is_symlink():
            continue
        safe_dest = _safe_rel_path(files_dir, rel_path)
        if safe_dest is None:
            continue
        fingerprint = _path_fingerprint(repo_root, rel_path)
        try:
            size_bytes = abs_path.stat().st_size
        except OSError:
            size_bytes = 0
        safe_dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(str(abs_path), str(safe_dest))
        except OSError:
            continue
        artifact_entries.append(
            {
                "relative_path": rel_path,
                "fingerprint": fingerprint,
                "size_bytes": size_bytes,
            }
        )

    # Compute effective policy summary for checkpoint metadata
    effective_policy_summary: dict[str, Any] = {}
    try:
        from autolab.config import _load_effective_policy

        ep_result = _load_effective_policy(
            repo_root, stage=stage, scope_kind=scope_kind
        )
        effective_policy_summary = {
            "preset": ep_result.preset,
            "host_mode": ep_result.host_mode,
            "scope_kind": ep_result.scope_kind,
            "profile_mode": ep_result.profile_mode,
            "stage": ep_result.stage,
            "risk_flags": ep_result.risk_flags,
        }
    except Exception:
        pass

    revision_label = _resolve_revision_label(repo_root)
    manifest: dict[str, Any] = {
        "schema_version": "1.0",
        "checkpoint_id": checkpoint_id,
        "created_at": _utc_now(),
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "stage": stage,
        "trigger": trigger,
        "scope_kind": scope_kind,
        "revision_label": revision_label,
        "artifacts": artifact_entries,
        "state_snapshot": dict(state),
        "effective_policy_summary": effective_policy_summary,
    }
    if label:
        manifest["label"] = label
        manifest["label_origin"] = label_origin.strip().lower() or (
            "user" if trigger == "manual" else "system"
        )
    if pinned or _checkpoint_has_user_label(manifest):
        manifest["gc_protected"] = True
    if pinned:
        manifest["pinned"] = True

    _write_json(checkpoint_dir / "manifest.json", manifest)
    _update_checkpoint_index(autolab_dir, checkpoint_id, manifest)

    if trigger in ("auto", "commit"):
        _prune_auto_checkpoints(autolab_dir)

    return checkpoint_id, checkpoint_dir


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------


def _update_checkpoint_index(
    autolab_dir: Path, checkpoint_id: str, manifest: dict[str, Any]
) -> None:
    index_path = autolab_dir / "checkpoints" / "index.json"
    index = _load_json_if_exists(index_path)
    if not isinstance(index, dict):
        index = {"schema_version": "1.0", "checkpoints": []}
    if not isinstance(index.get("checkpoints"), list):
        index["checkpoints"] = []

    entry = {
        "checkpoint_id": checkpoint_id,
        "created_at": manifest.get("created_at", _utc_now()),
        "stage": manifest.get("stage", ""),
        "trigger": manifest.get("trigger", ""),
        "iteration_id": manifest.get("iteration_id", ""),
        "artifact_count": len(manifest.get("artifacts", [])),
        "revision_label": manifest.get("revision_label", ""),
    }
    if manifest.get("label"):
        entry["label"] = manifest["label"]
    if manifest.get("label_origin"):
        entry["label_origin"] = manifest["label_origin"]
    if manifest.get("experiment_id"):
        entry["experiment_id"] = manifest["experiment_id"]
    if manifest.get("pinned", False):
        entry["pinned"] = True

    index["checkpoints"].append(entry)
    _write_json(index_path, index)


def _rewrite_checkpoint_index_entry(
    autolab_dir: Path, checkpoint_id: str, manifest: dict[str, Any]
) -> None:
    index_path = autolab_dir / "checkpoints" / "index.json"
    index = _load_json_if_exists(index_path)
    if not isinstance(index, dict):
        index = {"schema_version": "1.0", "checkpoints": []}
    checkpoints = index.get("checkpoints", [])
    if not isinstance(checkpoints, list):
        checkpoints = []

    replacement = {
        "checkpoint_id": checkpoint_id,
        "created_at": manifest.get("created_at", _utc_now()),
        "stage": manifest.get("stage", ""),
        "trigger": manifest.get("trigger", ""),
        "iteration_id": manifest.get("iteration_id", ""),
        "artifact_count": len(manifest.get("artifacts", [])),
        "revision_label": manifest.get("revision_label", ""),
    }
    if manifest.get("label"):
        replacement["label"] = manifest["label"]
    if manifest.get("label_origin"):
        replacement["label_origin"] = manifest["label_origin"]
    if "gc_protected" in manifest:
        replacement["gc_protected"] = bool(manifest.get("gc_protected", False))
    if manifest.get("experiment_id"):
        replacement["experiment_id"] = manifest["experiment_id"]
    if manifest.get("pinned", False):
        replacement["pinned"] = True

    updated = False
    for idx, entry in enumerate(checkpoints):
        if entry.get("checkpoint_id") == checkpoint_id:
            checkpoints[idx] = replacement
            updated = True
            break
    if not updated:
        checkpoints.append(replacement)

    index["checkpoints"] = checkpoints
    _write_json(index_path, index)


def _remove_checkpoint_index_entries(
    autolab_dir: Path, checkpoint_ids: set[str]
) -> None:
    if not checkpoint_ids:
        return
    index_path = autolab_dir / "checkpoints" / "index.json"
    index = _load_json_if_exists(index_path)
    if not isinstance(index, dict):
        return
    checkpoints = index.get("checkpoints", [])
    if not isinstance(checkpoints, list):
        return
    index["checkpoints"] = [
        entry
        for entry in checkpoints
        if str(entry.get("checkpoint_id", "")) not in checkpoint_ids
    ]
    _write_json(index_path, index)


def _prune_auto_checkpoints(
    autolab_dir: Path,
    max_auto: int = 20,
    *,
    triggers: tuple[str, ...] = ("auto", "commit"),
) -> None:
    index_path = autolab_dir / "checkpoints" / "index.json"
    index = _load_json_if_exists(index_path)
    if not isinstance(index, dict):
        return
    checkpoints = index.get("checkpoints", [])
    if not isinstance(checkpoints, list):
        return

    prunable_entries = [
        (i, cp)
        for i, cp in enumerate(checkpoints)
        if cp.get("trigger") in triggers and not _is_checkpoint_protected(cp)
    ]
    if len(prunable_entries) <= max_auto:
        return

    to_remove = prunable_entries[: len(prunable_entries) - max_auto]
    remove_indices = {i for i, _ in to_remove}
    for _, cp in to_remove:
        cp_id = cp.get("checkpoint_id", "")
        if not _validate_checkpoint_id(cp_id):
            continue
        cp_dir = autolab_dir / "checkpoints" / cp_id
        if cp_dir.is_dir():
            try:
                shutil.rmtree(cp_dir)
            except OSError:
                pass

    index["checkpoints"] = [
        cp for i, cp in enumerate(checkpoints) if i not in remove_indices
    ]
    _write_json(index_path, index)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


def list_checkpoints(
    repo_root: Path,
    iteration_id: str = "",
    trigger: str = "",
) -> list[dict[str, Any]]:
    index_path = repo_root / ".autolab" / "checkpoints" / "index.json"
    index = _load_json_if_exists(index_path)
    if not isinstance(index, dict):
        return []
    checkpoints = index.get("checkpoints", [])
    if not isinstance(checkpoints, list):
        return []

    filtered = checkpoints
    if iteration_id:
        filtered = [cp for cp in filtered if cp.get("iteration_id") == iteration_id]
    if trigger:
        filtered = [cp for cp in filtered if cp.get("trigger") == trigger]

    return sorted(filtered, key=lambda c: c.get("created_at", ""), reverse=True)


def set_checkpoint_pinned(
    repo_root: Path,
    checkpoint_id: str,
    *,
    pinned: bool,
) -> dict[str, Any]:
    if not _validate_checkpoint_id(checkpoint_id):
        raise ValueError(f"invalid checkpoint id: {checkpoint_id}")

    autolab_dir = repo_root / ".autolab"
    manifest_path = autolab_dir / "checkpoints" / checkpoint_id / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"checkpoint {checkpoint_id} not found")

    manifest = _load_json_if_exists(manifest_path)
    if not isinstance(manifest, dict):
        raise ValueError(f"invalid manifest for {checkpoint_id}")

    if manifest.get("label") and not manifest.get("label_origin"):
        manifest["label_origin"] = (
            "user"
            if str(manifest.get("trigger", "")).strip().lower() == "manual"
            else "system"
        )

    if pinned:
        manifest["pinned"] = True
        manifest["gc_protected"] = True
    else:
        manifest.pop("pinned", None)
        manifest["gc_protected"] = False

    _write_json(manifest_path, manifest)
    _rewrite_checkpoint_index_entry(autolab_dir, checkpoint_id, manifest)
    return manifest


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def verify_checkpoint(repo_root: Path, checkpoint_id: str) -> tuple[bool, list[str]]:
    if not _validate_checkpoint_id(checkpoint_id):
        return False, [f"invalid checkpoint id: {checkpoint_id}"]

    autolab_dir = repo_root / ".autolab"
    checkpoint_dir = autolab_dir / "checkpoints" / checkpoint_id
    issues: list[str] = []

    manifest_path = checkpoint_dir / "manifest.json"
    if not manifest_path.exists():
        return False, [f"manifest.json missing for checkpoint {checkpoint_id}"]

    manifest = _load_json_if_exists(manifest_path)
    if not isinstance(manifest, dict):
        return False, [f"manifest.json invalid for checkpoint {checkpoint_id}"]

    for required in (
        "schema_version",
        "checkpoint_id",
        "created_at",
        "stage",
        "artifacts",
    ):
        if required not in manifest:
            issues.append(f"manifest missing field: {required}")

    artifacts = manifest.get("artifacts", [])
    if not isinstance(artifacts, list):
        issues.append("manifest.artifacts is not a list")
        return len(issues) == 0, issues

    files_dir = checkpoint_dir / "files"
    for art in artifacts:
        rel = art.get("relative_path", "")
        expected_fp = art.get("fingerprint", "")
        stored_path = _safe_rel_path(files_dir, rel)
        if stored_path is None:
            issues.append(f"unsafe relative_path in manifest: {rel}")
            continue
        if not stored_path.exists():
            issues.append(f"stored artifact missing: {rel}")
            continue
        if expected_fp and expected_fp not in ("<missing>", "<dir>", "<unreadable>"):
            import hashlib

            actual_fp = hashlib.sha1(stored_path.read_bytes()).hexdigest()
            if actual_fp != expected_fp:
                issues.append(
                    f"fingerprint mismatch for {rel}: expected {expected_fp}, got {actual_fp}"
                )

    return len(issues) == 0, issues


# Keep backward-compatible alias
_verify_checkpoint = verify_checkpoint


# ---------------------------------------------------------------------------
# Archive & Restore
# ---------------------------------------------------------------------------


def _archive_artifacts(
    repo_root: Path, archive_dir: Path, artifacts: list[tuple[Path, str]]
) -> Path:
    archive_dir.mkdir(parents=True, exist_ok=True)
    files_dir = archive_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    entries: list[dict[str, str]] = []
    for abs_path, rel_path in artifacts:
        if abs_path.exists() and not abs_path.is_symlink():
            dest = _safe_rel_path(files_dir, rel_path)
            if dest is None:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(abs_path), str(dest))
            entries.append({"relative_path": rel_path})

    _write_json(
        archive_dir / "manifest.json",
        {
            "schema_version": "1.0",
            "archived_at": _utc_now(),
            "files": entries,
        },
    )
    return archive_dir


def restore_checkpoint(
    repo_root: Path,
    state_path: Path,
    checkpoint_id: str,
    archive_current: bool = True,
) -> tuple[bool, str, list[str]]:
    """Restore a checkpoint. Returns (success, message, changed_files)."""
    if not _validate_checkpoint_id(checkpoint_id):
        return False, f"invalid checkpoint id: {checkpoint_id}", []

    autolab_dir = repo_root / ".autolab"
    checkpoint_dir = autolab_dir / "checkpoints" / checkpoint_id
    manifest_path = checkpoint_dir / "manifest.json"

    if not manifest_path.exists():
        return False, f"checkpoint {checkpoint_id} not found", []

    manifest = _load_json_if_exists(manifest_path)
    if not isinstance(manifest, dict):
        return False, f"invalid manifest for {checkpoint_id}", []

    valid, issues = verify_checkpoint(repo_root, checkpoint_id)
    if not valid:
        return False, f"checkpoint verification failed: {'; '.join(issues)}", []

    state_snapshot = manifest.get("state_snapshot", {})
    stored_artifacts = manifest.get("artifacts", [])
    iteration_id = manifest.get("iteration_id", "")
    experiment_id = manifest.get("experiment_id", "")

    iteration_dir: Path | None = None
    if iteration_id:
        try:
            iteration_dir, _ = _resolve_iteration_directory(
                repo_root, iteration_id=iteration_id, experiment_id=experiment_id
            )
        except Exception:
            iteration_dir = None

    # Archive current state if requested
    if archive_current and iteration_dir is not None:
        scope_kind = manifest.get("scope_kind", "experiment")
        current_stage = str((_load_json_if_exists(state_path) or {}).get("stage", ""))
        current_artifacts = _collect_canonical_artifacts(
            repo_root, iteration_dir, current_stage, scope_kind
        )
        if current_artifacts:
            ts = _utc_now().replace(":", "").replace("-", "")
            archive_dir = autolab_dir / "reset_archive" / ts
            _archive_artifacts(repo_root, archive_dir, current_artifacts)

    # Restore artifact files
    changed_files: list[str] = []
    files_dir = checkpoint_dir / "files"
    for art in stored_artifacts:
        rel = art.get("relative_path", "")
        source = _safe_rel_path(files_dir, rel)
        if source is None or not source.exists():
            continue
        if source.is_symlink():
            continue
        dest = _safe_rel_path(repo_root, rel)
        if dest is None:
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(source), str(dest))
        changed_files.append(rel)

    # Restore state
    if isinstance(state_snapshot, dict) and state_snapshot:
        _write_json(state_path, state_snapshot)
        try:
            changed_files.append(str(state_path.relative_to(repo_root)))
        except ValueError:
            changed_files.append(str(state_path))

    return True, f"restored checkpoint {checkpoint_id}", changed_files


def rewind_to_stage(
    repo_root: Path,
    state_path: Path,
    target_stage: str,
) -> tuple[bool, str, list[str]]:
    """Find latest checkpoint for target_stage, then restore it."""
    checkpoints = list_checkpoints(repo_root)
    for cp in checkpoints:
        if cp.get("stage") == target_stage:
            return restore_checkpoint(
                repo_root, state_path, cp["checkpoint_id"], archive_current=True
            )
    return False, f"no checkpoint found for stage '{target_stage}'", []


# ---------------------------------------------------------------------------
# Context-rot detection
# ---------------------------------------------------------------------------


def detect_context_rot(
    repo_root: Path,
    state_path: Path,
    iteration_id: str = "",
    experiment_id: str = "",
) -> dict[str, Any]:
    """Compare current artifact fingerprints against latest checkpoint."""
    state = _load_json_if_exists(state_path) or {}
    iteration_id = iteration_id or str(state.get("iteration_id", "")).strip()
    experiment_id = experiment_id or str(state.get("experiment_id", "")).strip()

    _NO_ROT: dict[str, Any] = {
        "has_rot": False,
        "context_rot_flags": [],
        "artifact_drift_summary": {},
        "recommended_rewind_targets": [],
    }

    checkpoints = list_checkpoints(repo_root, iteration_id=iteration_id)
    if not checkpoints:
        return dict(_NO_ROT)

    latest = checkpoints[0]
    cp_id = latest.get("checkpoint_id", "")
    autolab_dir = repo_root / ".autolab"
    manifest_path = autolab_dir / "checkpoints" / cp_id / "manifest.json"
    manifest = _load_json_if_exists(manifest_path)
    if not isinstance(manifest, dict):
        return dict(_NO_ROT)

    stored_artifacts = manifest.get("artifacts", [])
    flags: list[str] = []
    modified: list[str] = []
    missing: list[str] = []
    stale_sidecars: list[str] = []

    for art in stored_artifacts:
        rel = art.get("relative_path", "")
        expected_fp = art.get("fingerprint", "")
        current_fp = _path_fingerprint(repo_root, rel)

        if current_fp == "<missing>":
            missing.append(rel)
            flags.append(f"missing_{Path(rel).stem}")
        elif expected_fp and expected_fp not in ("<missing>", "<dir>", "<unreadable>"):
            if current_fp != expected_fp:
                modified.append(rel)
                if "sidecar" in rel.lower() or "context" in rel.lower():
                    stale_sidecars.append(rel)
                    flags.append(f"stale_{Path(rel).stem}")
                else:
                    flags.append(f"modified_{Path(rel).stem}")

    has_rot = bool(flags)
    recommended_targets: list[str] = []
    if has_rot:
        cp_stage = latest.get("stage", "")
        if cp_stage in STAGE_ORDER:
            idx = STAGE_ORDER.index(cp_stage)
            recommended_targets = STAGE_ORDER[: idx + 1]

    return {
        "has_rot": has_rot,
        "context_rot_flags": flags,
        "artifact_drift_summary": {
            "modified": modified,
            "missing": missing,
            "stale_sidecars": stale_sidecars,
        },
        "recommended_rewind_targets": recommended_targets,
    }


# ---------------------------------------------------------------------------
# Auto-checkpoint trigger
# ---------------------------------------------------------------------------


def try_auto_checkpoint(
    repo_root: Path,
    state_path: Path,
    stage_before: str,
    stage_after: str,
    transitioned: bool,
    agent_status: str,
) -> str | None:
    """Non-blocking auto-checkpoint. Returns checkpoint_id or None."""
    if not transitioned:
        return None
    if agent_status != "complete":
        return None
    if stage_before not in AUTO_CHECKPOINT_STAGES:
        return None

    try:
        cp_id, _ = create_checkpoint(
            repo_root,
            state_path=state_path,
            stage=stage_before,
            trigger="auto",
            label=f"after_{stage_before}",
        )
        return cp_id
    except Exception:
        return None
