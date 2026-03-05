from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None  # type: ignore[assignment]

from autolab.models import StageCheckError
from autolab.utils import _utc_now

PARSER_CAPABILITIES_SCHEMA_VERSION = "1.0"
PARSER_CAPABILITIES_FILENAME = "parser_capabilities.json"
PARSER_CAPABILITIES_INDEX_PATH = Path(".autolab") / "parser_capabilities.json"


def _load_json_mapping(path: Path, *, label: str) -> dict[str, Any]:
    if not path.exists():
        raise StageCheckError(f"{label} is missing at {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageCheckError(f"{label} is not valid JSON at {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise StageCheckError(f"{label} must contain a JSON object")
    return payload


def _normalize_metric_names(raw_value: Any) -> list[str]:
    if not isinstance(raw_value, list):
        return []
    normalized: list[str] = []
    for item in raw_value:
        metric_name = str(item).strip()
        if metric_name and metric_name not in normalized:
            normalized.append(metric_name)
    return normalized


def _load_design_payload(iteration_dir: Path) -> dict[str, Any]:
    design_path = iteration_dir / "design.yaml"
    if yaml is None:
        raise StageCheckError("parser capability validation requires PyYAML")
    if not design_path.exists():
        raise StageCheckError(f"design.yaml is missing at {design_path}")
    try:
        payload = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageCheckError(
            f"design.yaml is not valid YAML at {design_path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise StageCheckError("design.yaml must contain a mapping")
    return payload


def _design_parser_kind(design_payload: dict[str, Any]) -> str:
    parser_block = design_payload.get("extract_parser")
    if not isinstance(parser_block, dict):
        return ""
    return str(parser_block.get("kind", "")).strip().lower()


def _design_primary_metric(design_payload: dict[str, Any]) -> str:
    metrics = design_payload.get("metrics")
    if not isinstance(metrics, dict):
        return ""
    primary = metrics.get("primary")
    if not isinstance(primary, dict):
        return ""
    return str(primary.get("name", "")).strip()


def parser_capabilities_path(iteration_dir: Path) -> Path:
    return iteration_dir / PARSER_CAPABILITIES_FILENAME


def parser_capabilities_index_path(repo_root: Path) -> Path:
    return repo_root / PARSER_CAPABILITIES_INDEX_PATH


def build_iteration_parser_capabilities_manifest(
    *,
    iteration_id: str,
    parser_kind: str,
    parser_locator: str,
    supported_metrics: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": PARSER_CAPABILITIES_SCHEMA_VERSION,
        "iteration_id": str(iteration_id).strip(),
        "parser": {
            "kind": str(parser_kind).strip().lower(),
            "locator": str(parser_locator).strip(),
        },
        "supported_metrics": list(supported_metrics),
        "output_contract": {
            "writes_metrics_json": True,
            "writes_summary_markdown": True,
        },
        "generated_at": _utc_now(),
    }


def write_iteration_parser_capabilities_manifest(
    *, iteration_dir: Path, payload: dict[str, Any]
) -> Path:
    manifest_path = parser_capabilities_path(iteration_dir)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def upsert_parser_capabilities_index(
    *,
    repo_root: Path,
    iteration_id: str,
    manifest_path: Path,
    parser_kind: str,
    supported_metrics: list[str],
) -> Path:
    index_path = parser_capabilities_index_path(repo_root)
    index_payload: dict[str, Any]
    if index_path.exists():
        try:
            loaded = json.loads(index_path.read_text(encoding="utf-8"))
            index_payload = loaded if isinstance(loaded, dict) else {}
        except Exception:
            index_payload = {}
    else:
        index_payload = {}

    iterations = index_payload.get("iterations")
    if not isinstance(iterations, dict):
        iterations = {}

    try:
        manifest_relative = manifest_path.relative_to(repo_root).as_posix()
    except ValueError:
        manifest_relative = str(manifest_path)

    now = _utc_now()
    iterations[str(iteration_id).strip()] = {
        "manifest_path": manifest_relative,
        "parser_kind": str(parser_kind).strip().lower(),
        "supported_metrics": list(supported_metrics),
        "updated_at": now,
    }

    index_payload = {
        "schema_version": PARSER_CAPABILITIES_SCHEMA_VERSION,
        "generated_at": now,
        "iterations": iterations,
    }
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index_payload, indent=2) + "\n", encoding="utf-8")
    return index_path


def validate_parser_capability_alignment(
    *,
    repo_root: Path,
    iteration_dir: Path,
    iteration_id: str,
    require_manifest: bool,
    require_index: bool,
) -> list[str]:
    issues: list[str] = []

    try:
        design_payload = _load_design_payload(iteration_dir)
    except StageCheckError as exc:
        return [str(exc)]

    manifest_path = parser_capabilities_path(iteration_dir)
    index_path = parser_capabilities_index_path(repo_root)

    if (
        not require_manifest
        and not require_index
        and not manifest_path.exists()
        and not index_path.exists()
    ):
        return []

    if not manifest_path.exists():
        issues.append(f"{manifest_path} is required for parser capability validation")
        manifest_payload: dict[str, Any] | None = None
    else:
        try:
            manifest_payload = _load_json_mapping(
                manifest_path,
                label="parser capabilities manifest",
            )
        except StageCheckError as exc:
            issues.append(str(exc))
            manifest_payload = None

    manifest_kind = ""
    manifest_supported_metrics: list[str] = []
    if isinstance(manifest_payload, dict):
        parser_block = manifest_payload.get("parser")
        if isinstance(parser_block, dict):
            manifest_kind = str(parser_block.get("kind", "")).strip().lower()
        manifest_supported_metrics = _normalize_metric_names(
            manifest_payload.get("supported_metrics")
        )

    design_kind = _design_parser_kind(design_payload)
    if design_kind and manifest_kind and design_kind != manifest_kind:
        issues.append(
            "parser capability mismatch: "
            f"design.extract_parser.kind='{design_kind}' "
            f"!= parser_capabilities.parser.kind='{manifest_kind}'"
        )

    primary_metric = _design_primary_metric(design_payload)
    if primary_metric and not manifest_supported_metrics:
        issues.append(
            "parser capability mismatch: parser_capabilities.supported_metrics must be "
            f"non-empty and include design primary metric '{primary_metric}'"
        )
    elif primary_metric and primary_metric not in manifest_supported_metrics:
        issues.append(
            "parser capability mismatch: "
            f"design.metrics.primary.name='{primary_metric}' is not declared in "
            "parser_capabilities.supported_metrics"
        )

    if require_index and not index_path.exists():
        issues.append(f"{index_path} is required for parser capability validation")
        return issues

    if not index_path.exists():
        return issues

    try:
        index_payload = _load_json_mapping(
            index_path, label="parser capabilities index"
        )
    except StageCheckError as exc:
        issues.append(str(exc))
        return issues

    iterations_block = index_payload.get("iterations")
    if not isinstance(iterations_block, dict):
        issues.append(
            f"{index_path} must define an 'iterations' mapping for capability entries"
        )
        return issues

    iteration_entry = iterations_block.get(str(iteration_id).strip())
    if not isinstance(iteration_entry, dict):
        issues.append(
            f"{index_path} is missing iterations['{iteration_id}'] capability entry"
        )
        return issues

    entry_manifest_path = str(iteration_entry.get("manifest_path", "")).strip()
    if entry_manifest_path:
        try:
            expected_manifest = manifest_path.relative_to(repo_root).as_posix()
        except ValueError:
            expected_manifest = str(manifest_path)
        if entry_manifest_path != expected_manifest:
            issues.append(
                "parser capability mismatch: "
                f"index manifest_path '{entry_manifest_path}' != '{expected_manifest}'"
            )

    entry_kind = str(iteration_entry.get("parser_kind", "")).strip().lower()
    if manifest_kind and entry_kind and entry_kind != manifest_kind:
        issues.append(
            "parser capability mismatch: "
            f"index parser_kind '{entry_kind}' != manifest parser kind '{manifest_kind}'"
        )

    entry_supported = _normalize_metric_names(iteration_entry.get("supported_metrics"))
    if primary_metric and entry_supported and primary_metric not in entry_supported:
        issues.append(
            "parser capability mismatch: "
            f"index supported_metrics does not include design primary metric '{primary_metric}'"
        )

    return issues
