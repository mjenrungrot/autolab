"""Artifact retention and pruning helpers for ``autolab gc``."""

from __future__ import annotations

import copy
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from autolab.checkpoint import (
    _is_checkpoint_protected,
    _remove_checkpoint_index_entries,
    _validate_checkpoint_id,
)
from autolab.state import _resolve_iteration_directory
from autolab.utils import _load_json_if_exists, _utc_now, _write_json

GC_ONLY_CHOICES: tuple[str, ...] = (
    "checkpoints",
    "execution",
    "traceability",
    "reset-archives",
    "docs-views",
)

DEFAULT_CHECKPOINT_KEEP_LATEST = 2
DEFAULT_EXECUTION_KEEP_LATEST = 2
DEFAULT_TRACEABILITY_KEEP_LATEST = 2
DEFAULT_RESET_ARCHIVE_MAX_AGE_DAYS = 14
DEFAULT_DOCS_VIEWS_KEEP_LATEST = 1

AUTOLAB_DOCS_MANIFEST_FILENAME = ".autolab_docs_manifest.json"
_DOC_VIEW_FILENAMES = {
    "registry.md",
    "project.md",
    "roadmap.md",
    "state.md",
    "requirements.md",
    "sidecar.md",
}


def _repo_relative(repo_root: Path, path: Path) -> str:
    try:
        return path.resolve(strict=False).relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _safe_repo_path(repo_root: Path, relative_path: str) -> Path | None:
    if not relative_path or relative_path.startswith("/"):
        return None
    resolved = (repo_root / relative_path).resolve(strict=False)
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return resolved


def _parse_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _path_bytes(path: Path) -> int:
    if not path.exists() or path.is_symlink():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = 0
    for child in path.rglob("*"):
        if child.is_symlink() or not child.is_file():
            continue
        try:
            total += child.stat().st_size
        except OSError:
            continue
    return total


def _file_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _summarize_actions(actions: list[dict[str, Any]]) -> dict[str, Any]:
    by_kind: dict[str, dict[str, int]] = {}
    candidate_paths = 0
    bytes_reclaimable = 0
    for action in actions:
        kind = str(action.get("kind", "unknown"))
        bucket = by_kind.setdefault(kind, {"units": 0, "paths": 0, "bytes": 0})
        bucket["units"] += 1
        bucket["paths"] += len(action.get("paths", []))
        bucket["bytes"] += int(action.get("bytes", 0) or 0)
        candidate_paths += len(action.get("paths", []))
        bytes_reclaimable += int(action.get("bytes", 0) or 0)
    return {
        "candidate_units": len(actions),
        "candidate_paths": candidate_paths,
        "bytes_reclaimable": bytes_reclaimable,
        "by_kind": by_kind,
    }


def _load_state(state_path: Path) -> dict[str, Any]:
    payload = _load_json_if_exists(state_path)
    return payload if isinstance(payload, dict) else {}


def _resolve_active_iteration_path(
    repo_root: Path,
    state: dict[str, Any],
) -> tuple[str, str]:
    iteration_id = str(state.get("iteration_id", "")).strip()
    experiment_id = str(state.get("experiment_id", "")).strip()
    if not iteration_id:
        return "", ""
    try:
        iteration_dir, _ = _resolve_iteration_directory(
            repo_root,
            iteration_id=iteration_id,
            experiment_id=experiment_id,
        )
    except Exception:
        return iteration_id, ""
    return iteration_id, _repo_relative(repo_root, iteration_dir)


def _iter_checkpoint_actions(
    repo_root: Path,
    *,
    checkpoint_keep_latest: int,
) -> list[dict[str, Any]]:
    autolab_dir = repo_root / ".autolab"
    index_path = autolab_dir / "checkpoints" / "index.json"
    index = _load_json_if_exists(index_path)
    if not isinstance(index, dict):
        return []

    checkpoints = index.get("checkpoints", [])
    if not isinstance(checkpoints, list):
        return []

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in checkpoints:
        if not isinstance(entry, dict):
            continue
        checkpoint_id = str(entry.get("checkpoint_id", "")).strip()
        if not _validate_checkpoint_id(checkpoint_id):
            continue
        key = (
            str(entry.get("iteration_id", "")).strip(),
            str(entry.get("stage", "")).strip(),
        )
        grouped.setdefault(key, []).append(entry)

    actions: list[dict[str, Any]] = []
    for (iteration_id, stage), entries in grouped.items():
        sorted_entries = sorted(
            entries,
            key=lambda item: str(item.get("created_at", "")),
            reverse=True,
        )
        unprotected = [
            entry for entry in sorted_entries if not _is_checkpoint_protected(entry)
        ]
        for entry in unprotected[checkpoint_keep_latest:]:
            checkpoint_id = str(entry.get("checkpoint_id", "")).strip()
            checkpoint_dir = autolab_dir / "checkpoints" / checkpoint_id
            label = str(entry.get("label", "")).strip()
            parts = [f"checkpoint {checkpoint_id}"]
            if label:
                parts.append(f"label={label}")
            actions.append(
                {
                    "kind": "checkpoints",
                    "label": " ".join(parts),
                    "checkpoint_id": checkpoint_id,
                    "iteration_id": iteration_id,
                    "stage": stage,
                    "paths": [_repo_relative(repo_root, checkpoint_dir)],
                    "bytes": _path_bytes(checkpoint_dir),
                    "reason": (
                        "older unprotected checkpoint beyond "
                        f"keep_latest={checkpoint_keep_latest} for "
                        f"iteration={iteration_id or '(none)'} stage={stage or '(none)'}"
                    ),
                }
            )
    return actions


def _iter_execution_actions(
    repo_root: Path,
    *,
    active_iteration_path: str,
    execution_keep_latest: int,
) -> list[dict[str, Any]]:
    experiments_dir = repo_root / "experiments"
    if not experiments_dir.is_dir():
        return []

    bundles: dict[str, dict[str, Any]] = {}
    for filename in ("plan_execution_state.json", "plan_execution_summary.json"):
        for path in experiments_dir.rglob(filename):
            bundle_path = _repo_relative(repo_root, path.parent)
            bundle = bundles.setdefault(
                bundle_path,
                {
                    "bundle_path": bundle_path,
                    "iteration_id": path.parent.name,
                    "paths": [],
                    "sort_key": 0.0,
                },
            )
            bundle["paths"].append(path)
            bundle["sort_key"] = max(float(bundle["sort_key"]), _file_mtime(path))

    sortable = [
        bundle
        for bundle in bundles.values()
        if str(bundle.get("bundle_path", "")) != active_iteration_path
    ]
    sortable.sort(key=lambda item: float(item.get("sort_key", 0.0)), reverse=True)

    actions: list[dict[str, Any]] = []
    for bundle in sortable[execution_keep_latest:]:
        bundle_paths = [
            _repo_relative(repo_root, path)
            for path in bundle.get("paths", [])
            if isinstance(path, Path)
        ]
        actions.append(
            {
                "kind": "execution",
                "label": f"execution bundle {bundle.get('bundle_path', '')}",
                "iteration_id": bundle.get("iteration_id", ""),
                "paths": sorted(set(bundle_paths)),
                "bytes": sum(
                    _path_bytes(path)
                    for path in bundle.get("paths", [])
                    if isinstance(path, Path)
                ),
                "reason": (
                    "older non-active execution bundle beyond "
                    f"keep_latest={execution_keep_latest}"
                ),
            }
        )
    return actions


def _iter_traceability_actions(
    repo_root: Path,
    *,
    active_iteration_path: str,
    traceability_keep_latest: int,
) -> list[dict[str, Any]]:
    experiments_dir = repo_root / "experiments"
    if not experiments_dir.is_dir():
        return []

    items: list[dict[str, Any]] = []
    for path in experiments_dir.rglob("traceability_coverage.json"):
        bundle_path = _repo_relative(repo_root, path.parent)
        if bundle_path == active_iteration_path:
            continue
        items.append(
            {
                "iteration_id": path.parent.name,
                "bundle_path": bundle_path,
                "path": path,
                "sort_key": _file_mtime(path),
            }
        )
    items.sort(key=lambda item: float(item.get("sort_key", 0.0)), reverse=True)

    actions: list[dict[str, Any]] = []
    for item in items[traceability_keep_latest:]:
        path = item["path"]
        actions.append(
            {
                "kind": "traceability",
                "label": f"traceability {item.get('bundle_path', '')}",
                "iteration_id": item.get("iteration_id", ""),
                "paths": [_repo_relative(repo_root, path)],
                "bytes": _path_bytes(path),
                "reason": (
                    "older non-active traceability output beyond "
                    f"keep_latest={traceability_keep_latest}"
                ),
            }
        )
    return actions


def _iter_reset_archive_actions(
    repo_root: Path,
    *,
    reset_archive_max_age_days: int,
) -> list[dict[str, Any]]:
    archive_root = repo_root / ".autolab" / "reset_archive"
    if not archive_root.is_dir():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=reset_archive_max_age_days)
    actions: list[dict[str, Any]] = []
    for archive_dir in sorted(archive_root.iterdir()):
        if not archive_dir.is_dir():
            continue
        manifest = _load_json_if_exists(archive_dir / "manifest.json")
        archived_at = None
        if isinstance(manifest, dict):
            archived_at = _parse_timestamp(str(manifest.get("archived_at", "")))
        if archived_at is None:
            archived_at = datetime.fromtimestamp(_file_mtime(archive_dir), timezone.utc)
        if archived_at >= cutoff:
            continue
        actions.append(
            {
                "kind": "reset-archives",
                "label": f"reset archive {archive_dir.name}",
                "archive_id": archive_dir.name,
                "paths": [_repo_relative(repo_root, archive_dir)],
                "bytes": _path_bytes(archive_dir),
                "reason": (
                    f"archive older than max_age_days={reset_archive_max_age_days}"
                ),
            }
        )
    return actions


def update_managed_docs_manifest(
    repo_root: Path,
    output_dir: Path,
    *,
    written_paths: list[Path],
    iteration_id: str,
) -> Path:
    manifest_path = output_dir / AUTOLAB_DOCS_MANIFEST_FILENAME
    existing = _load_json_if_exists(manifest_path)
    owned_files: set[str] = set()
    if isinstance(existing, dict):
        for rel_name in existing.get("owned_files", []):
            rel_text = str(rel_name).strip()
            if rel_text in _DOC_VIEW_FILENAMES:
                owned_files.add(rel_text)

    for path in written_paths:
        if path.parent == output_dir and path.name in _DOC_VIEW_FILENAMES:
            owned_files.add(path.name)

    manifest = {
        "schema_version": "1.0",
        "managed_by": "autolab docs generate",
        "generated_at": _utc_now(),
        "iteration_id": iteration_id,
        "output_dir": _repo_relative(repo_root, output_dir),
        "owned_files": sorted(owned_files),
    }
    _write_json(manifest_path, manifest)
    return manifest_path


def _iter_docs_view_actions(
    repo_root: Path,
    *,
    views_keep_latest: int,
) -> list[dict[str, Any]]:
    manifests: list[dict[str, Any]] = []
    for manifest_path in repo_root.rglob(AUTOLAB_DOCS_MANIFEST_FILENAME):
        manifest = _load_json_if_exists(manifest_path)
        if not isinstance(manifest, dict):
            continue
        if str(manifest.get("managed_by", "")).strip() != "autolab docs generate":
            continue
        output_dir = manifest_path.parent
        owned_files = [
            str(name).strip()
            for name in manifest.get("owned_files", [])
            if str(name).strip() in _DOC_VIEW_FILENAMES
        ]
        generated_at = _parse_timestamp(str(manifest.get("generated_at", "")))
        manifests.append(
            {
                "manifest_path": manifest_path,
                "output_dir": output_dir,
                "output_dir_rel": _repo_relative(repo_root, output_dir),
                "owned_files": owned_files,
                "sort_key": (
                    generated_at.timestamp()
                    if generated_at is not None
                    else _file_mtime(manifest_path)
                ),
            }
        )

    manifests.sort(key=lambda item: float(item.get("sort_key", 0.0)), reverse=True)

    actions: list[dict[str, Any]] = []
    for item in manifests[views_keep_latest:]:
        output_dir = item["output_dir"]
        manifest_path = item["manifest_path"]
        paths: list[str] = []
        bytes_total = 0
        for name in item.get("owned_files", []):
            target = output_dir / str(name)
            paths.append(_repo_relative(repo_root, target))
            bytes_total += _path_bytes(target)
        paths.append(_repo_relative(repo_root, manifest_path))
        bytes_total += _path_bytes(manifest_path)
        actions.append(
            {
                "kind": "docs-views",
                "label": f"docs views {item.get('output_dir_rel', '')}",
                "output_dir": item.get("output_dir_rel", ""),
                "paths": paths,
                "bytes": bytes_total,
                "reason": (
                    "managed docs-view output directory beyond "
                    f"keep_latest={views_keep_latest}"
                ),
            }
        )
    return actions


def build_gc_plan(
    repo_root: Path,
    *,
    state_path: Path,
    categories: list[str],
    checkpoint_keep_latest: int = DEFAULT_CHECKPOINT_KEEP_LATEST,
    execution_keep_latest: int = DEFAULT_EXECUTION_KEEP_LATEST,
    traceability_keep_latest: int = DEFAULT_TRACEABILITY_KEEP_LATEST,
    reset_archive_max_age_days: int = DEFAULT_RESET_ARCHIVE_MAX_AGE_DAYS,
    views_keep_latest: int = DEFAULT_DOCS_VIEWS_KEEP_LATEST,
) -> dict[str, Any]:
    state = _load_state(state_path)
    active_iteration_id, active_iteration_path = _resolve_active_iteration_path(
        repo_root, state
    )

    actions: list[dict[str, Any]] = []
    selected = [item for item in categories if item in GC_ONLY_CHOICES]
    if "checkpoints" in selected:
        actions.extend(
            _iter_checkpoint_actions(
                repo_root,
                checkpoint_keep_latest=checkpoint_keep_latest,
            )
        )
    if "execution" in selected:
        actions.extend(
            _iter_execution_actions(
                repo_root,
                active_iteration_path=active_iteration_path,
                execution_keep_latest=execution_keep_latest,
            )
        )
    if "traceability" in selected:
        actions.extend(
            _iter_traceability_actions(
                repo_root,
                active_iteration_path=active_iteration_path,
                traceability_keep_latest=traceability_keep_latest,
            )
        )
    if "reset-archives" in selected:
        actions.extend(
            _iter_reset_archive_actions(
                repo_root,
                reset_archive_max_age_days=reset_archive_max_age_days,
            )
        )
    if "docs-views" in selected:
        actions.extend(
            _iter_docs_view_actions(
                repo_root,
                views_keep_latest=views_keep_latest,
            )
        )

    return {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "mode": "dry-run",
        "active_iteration_id": active_iteration_id,
        "active_iteration_path": active_iteration_path,
        "categories": selected,
        "policy": {
            "checkpoint_keep_latest": checkpoint_keep_latest,
            "execution_keep_latest": execution_keep_latest,
            "traceability_keep_latest": traceability_keep_latest,
            "reset_archive_max_age_days": reset_archive_max_age_days,
            "views_keep_latest": views_keep_latest,
        },
        "summary": _summarize_actions(actions),
        "actions": actions,
    }


def _delete_relative_path(repo_root: Path, relative_path: str) -> tuple[bool, str]:
    target = _safe_repo_path(repo_root, relative_path)
    if target is None:
        return False, f"unsafe relative path: {relative_path}"
    if not target.exists():
        return True, ""
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    except OSError as exc:
        return False, str(exc)
    return True, ""


def _repair_traceability_latest(repo_root: Path) -> None:
    latest_path = repo_root / ".autolab" / "traceability_latest.json"
    experiments_dir = repo_root / "experiments"
    if not experiments_dir.is_dir():
        if latest_path.exists():
            latest_path.unlink()
        return

    candidates = sorted(
        experiments_dir.rglob("traceability_coverage.json"),
        key=_file_mtime,
        reverse=True,
    )
    for coverage_path in candidates:
        coverage_payload = _load_json_if_exists(coverage_path)
        if not isinstance(coverage_payload, dict):
            continue
        latest_payload = {
            "schema_version": coverage_payload.get("schema_version", "1.0"),
            "generated_at": _utc_now(),
            "iteration_id": coverage_payload.get("iteration_id", ""),
            "experiment_id": coverage_payload.get("experiment_id", ""),
            "run_id": coverage_payload.get("run_id", ""),
            "traceability_path": _repo_relative(repo_root, coverage_path),
            "decision": coverage_payload.get("decision", {}),
            "summary": coverage_payload.get("summary", {}),
        }
        _write_json(latest_path, latest_payload)
        return

    if latest_path.exists():
        latest_path.unlink()


def apply_gc_plan(repo_root: Path, plan: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(plan)
    actions = result.get("actions", [])
    if not isinstance(actions, list):
        result["actions"] = []
        result["summary"] = {
            **_summarize_actions([]),
            "applied_units": 0,
            "deleted_paths": 0,
            "failures": 0,
        }
        result["mode"] = "apply"
        return result

    autolab_dir = repo_root / ".autolab"
    removed_checkpoint_ids: set[str] = set()
    traceability_touched = False
    deleted_paths_total = 0
    applied_units = 0
    failures = 0

    for action in actions:
        if not isinstance(action, dict):
            continue
        action["applied"] = False
        action["errors"] = []
        deleted_paths: list[str] = []

        checkpoint_id = str(action.get("checkpoint_id", "")).strip()
        for relative_path in action.get("paths", []):
            ok, error = _delete_relative_path(repo_root, str(relative_path))
            if ok:
                deleted_paths.append(str(relative_path))
            elif error:
                action["errors"].append(error)

        if (
            action.get("kind") == "checkpoints"
            and checkpoint_id
            and not action["errors"]
        ):
            removed_checkpoint_ids.add(checkpoint_id)
        if action.get("kind") == "traceability" and deleted_paths:
            traceability_touched = True

        if action.get("kind") == "docs-views":
            output_dir_rel = str(action.get("output_dir", "")).strip()
            output_dir = _safe_repo_path(repo_root, output_dir_rel)
            if output_dir is not None and output_dir.is_dir():
                try:
                    next(output_dir.iterdir())
                except StopIteration:
                    try:
                        output_dir.rmdir()
                    except OSError:
                        pass

        action["deleted_paths"] = deleted_paths
        action["applied"] = not action["errors"]
        if action["applied"]:
            applied_units += 1
            deleted_paths_total += len(deleted_paths)
        else:
            failures += 1

    if removed_checkpoint_ids:
        try:
            _remove_checkpoint_index_entries(autolab_dir, removed_checkpoint_ids)
        except Exception as exc:
            failures += 1
            actions.append(
                {
                    "kind": "checkpoints",
                    "label": "checkpoint index update",
                    "paths": [
                        _repo_relative(
                            repo_root, autolab_dir / "checkpoints" / "index.json"
                        )
                    ],
                    "bytes": 0,
                    "reason": "remove pruned checkpoint entries from index",
                    "applied": False,
                    "deleted_paths": [],
                    "errors": [str(exc)],
                }
            )

    if traceability_touched:
        try:
            _repair_traceability_latest(repo_root)
        except Exception as exc:
            failures += 1
            actions.append(
                {
                    "kind": "traceability",
                    "label": "traceability_latest repair",
                    "paths": [
                        _repo_relative(
                            repo_root,
                            repo_root / ".autolab" / "traceability_latest.json",
                        )
                    ],
                    "bytes": 0,
                    "reason": "repair traceability latest pointer after pruning",
                    "applied": False,
                    "deleted_paths": [],
                    "errors": [str(exc)],
                }
            )

    result["mode"] = "apply"
    result["summary"] = {
        **_summarize_actions(actions),
        "applied_units": applied_units,
        "deleted_paths": deleted_paths_total,
        "failures": failures,
    }
    return result


__all__ = [
    "AUTOLAB_DOCS_MANIFEST_FILENAME",
    "DEFAULT_CHECKPOINT_KEEP_LATEST",
    "DEFAULT_DOCS_VIEWS_KEEP_LATEST",
    "DEFAULT_EXECUTION_KEEP_LATEST",
    "DEFAULT_RESET_ARCHIVE_MAX_AGE_DAYS",
    "DEFAULT_TRACEABILITY_KEEP_LATEST",
    "GC_ONLY_CHOICES",
    "apply_gc_plan",
    "build_gc_plan",
    "update_managed_docs_manifest",
]
