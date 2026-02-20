#!/usr/bin/env python3
"""Verifier for template-completion and artifact budgets.

Responsibility boundary
-----------------------
**Owns (template_fill)**:
  - Placeholder detection: {{...}}, <...>, TODO, TBD, FIXME, ellipsis
  - Template-identity detection (content == bootstrap template verbatim)
  - Artifact existence checks (required files present for each stage)
  - Line / character / byte budget enforcement (fixed and dynamic caps)
  - Triviality detection (scripts with no meaningful commands, comment-only files)
  - Hypothesis PrimaryMetric format validation

**Does NOT own** (these belong to schema_checks):
  - JSON Schema validation (Draft 2020-12 via jsonschema library)
  - Cross-artifact consistency (e.g. metrics.primary_metric.name matches
    design.metrics.primary.name, review_result status gating before launch)
  - Stage-gating policy invariants (policy-required checks must be 'pass')
  - Formal required-field / type validation against .schema.json files

**Known boundary overlap** (intentional defence-in-depth):
  Functions _check_review_result, _check_design, _check_decision_result,
  _check_run_manifest, and _check_metrics perform lightweight structural
  checks (required keys, enum values, schema_version == "1.0") as a fast
  pre-flight.  These duplicate a subset of what schema_checks validates
  with full JSON Schema.  The duplication is deliberate so template_fill
  can give an early, specific error message without depending on the
  jsonschema library.
"""

from __future__ import annotations

import pathlib
import sys

_VERIFIER_DIR = pathlib.Path(__file__).resolve().parent
if str(_VERIFIER_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFIER_DIR))

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

try:
    from autolab.constants import (
        REVIEW_RESULT_REQUIRED_CHECKS,
        REVIEW_RESULT_CHECK_STATUSES,
    )
except Exception:  # pragma: no cover
    REVIEW_RESULT_REQUIRED_CHECKS = (  # type: ignore[misc]
        "tests",
        "dry_run",
        "schema",
        "env_smoke",
        "docs_target_update",
    )
    REVIEW_RESULT_CHECK_STATUSES = {"pass", "skip", "fail"}  # type: ignore[misc]

from verifier_lib import (
    REPO_ROOT,
    STATE_FILE,
    EXPERIMENT_TYPES,
    DEFAULT_EXPERIMENT_TYPE,
    load_json,
    load_yaml,
    load_state,
    resolve_iteration_dir,
    suggest_fix_hints,
)

TEMPLATE_ROOT = REPO_ROOT / ".autolab" / "templates"
WORKFLOW_FILE = REPO_ROOT / ".autolab" / "workflow.yaml"
LINE_LIMITS_POLICY_FILE = REPO_ROOT / ".autolab" / "experiment_file_line_limits.yaml"
RUN_METRICS_POLICY_KEY = "runs/<RUN_ID>/metrics.json"
RUN_MANIFEST_POLICY_KEY = "runs/<RUN_ID>/run_manifest.json"
DECISION_RESULT_ALLOWED = {"hypothesis", "design", "stop", "human_review"}
PRIMARY_METRIC_LINE_PATTERN = re.compile(
    r"^PrimaryMetric:\s*[^;]+;\s*Unit:\s*[^;]+;\s*Success:\s*.+$"
)
HYPOTHESIS_KEY_PATTERN = re.compile(
    r"^\s*(?:[-*]\s*)?([A-Za-z][A-Za-z0-9 _-]{0,48})\s*:\s*(.+)$"
)
HYPOTHESIS_NUMBER_PATTERN = re.compile(r"[-+]?\s*\d+(?:\.\d+)?")

PLACEHOLDER_PATTERNS = (
    re.compile(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}"),
    re.compile(r"<[A-Za-z0-9_]+>"),
    re.compile(r"\bTODO:\b", re.IGNORECASE),
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bTBD\b", re.IGNORECASE),
    re.compile(r"\bFIXME\b", re.IGNORECASE),
    re.compile(r"(?<!\.)\.\.\.(?!\.)"),  # ASCII ellipsis (not part of longer run)
    re.compile(r"\u2026"),  # Unicode ellipsis …
)

COMMENTED_SCRIPT_LINES = ("#!", "#", "set -e", "set -u", "set -uo", "set -o")
EVIDENCE_ARTIFACT_PATTERN = re.compile(r"^\s*-\s*artifact_path\s*:\s*(.+)\s*$")
EVIDENCE_FIELD_PATTERN = re.compile(r"^\s{2,}([A-Za-z_][A-Za-z0-9_]*)\s*:\s*(.*)$")
NON_TEXT_EVIDENCE_SUFFIXES = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".npz",
    ".pt",
    ".pth",
    ".onnx",
)

BOOTSTRAP_TEMPLATE_TEXT_BY_PATH: dict[str, str] = {
    "hypothesis.md": "# Hypothesis\n\n- metric: primary_metric\n- target_delta: 0.0\n",
    "design.yaml": (
        'schema_version: "1.0"\n'
        'id: "e1"\n'
        'iteration_id: "<ITERATION_ID>"\n'
        'hypothesis_id: "h1"\n'
        "entrypoint:\n"
        '  module: "tinydesk_v4.train"\n'
        "  args:\n"
        '    config: "TODO: set config path"\n'
        "compute:\n"
        '  location: "local"\n'
        '  walltime_estimate: "00:30:00"\n'
        '  memory_estimate: "64GB"\n'
        "  gpu_count: 0\n"
        "metrics:\n"
        "  primary:\n"
        '    name: "primary_metric"\n'
        '    unit: "unit"\n'
        '    mode: "maximize"\n'
        "  secondary: []\n"
        '  success_delta: "TODO: define target delta"\n'
        '  aggregation: "mean"\n'
        '  baseline_comparison: "TODO: define baseline comparison"\n'
        "baselines:\n"
        '  - name: "baseline_current"\n'
        '    description: "TODO: describe current baseline"\n'
    ),
    "implementation_plan.md": "# Implementation Plan\n\n- Implement the design requirements.\n",
    "implementation_review.md": "# Implementation Review\n\nReview notes.\n",
    "review_result.json": (
        "{\n"
        '  "status": "pass",\n'
        '  "blocking_findings": [],\n'
        '  "required_checks": {\n'
        '    "tests": "skip",\n'
        '    "dry_run": "skip",\n'
        '    "schema": "pass",\n'
        '    "env_smoke": "skip",\n'
        '    "docs_target_update": "skip"\n'
        "  },\n"
        '  "reviewed_at": "1970-01-01T00:00:00Z"\n'
        "}\n"
    ),
    "launch/run_local.sh": "#!/usr/bin/env bash\nset -euo pipefail\n# local launch placeholder\n",
    "launch/run_slurm.sbatch": "#!/usr/bin/env bash\nset -euo pipefail\n# slurm launch placeholder\n",
    "analysis/summary.md": "# Analysis Summary\n\nInitial summary.\n",
    "docs_update.md": "# Documentation Update\n\nNo changes needed.\n",
}


@dataclass
class Failure:
    path: str
    reason: str


@dataclass(frozen=True)
class RunManifestDynamicLimit:
    min_cap_lines: int
    max_cap_lines: int
    base_lines: int
    per_item_lines: int
    max_chars: int | None = None
    max_bytes: int | None = None
    count_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class LineLimitPolicy:
    fixed_max_lines: dict[str, int]
    fixed_max_chars: dict[str, int]
    fixed_max_bytes: dict[str, int]
    run_manifest_dynamic: RunManifestDynamicLimit


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _extract_markdown_key_values(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = HYPOTHESIS_KEY_PATTERN.match(line)
        if not match:
            continue
        raw_key = match.group(1).strip().lower().replace(" ", "_").replace("-", "_")
        raw_value = match.group(2).strip()
        if raw_key and raw_value and raw_key not in values:
            values[raw_key] = raw_value
    return values


def _parse_numeric_delta(value: str) -> float | None:
    match = HYPOTHESIS_NUMBER_PATTERN.search(str(value))
    if not match:
        return None
    try:
        return float(match.group(0).replace(" ", ""))
    except Exception:
        return None


def _coerce_positive_int(
    value: object, *, field_name: str, allow_zero: bool = False
) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{field_name} must be an integer")
    try:
        parsed = int(str(value).strip())
    except Exception as exc:
        raise RuntimeError(f"{field_name} must be an integer") from exc
    if allow_zero:
        if parsed < 0:
            raise RuntimeError(f"{field_name} must be >= 0")
    elif parsed <= 0:
        raise RuntimeError(f"{field_name} must be > 0")
    return parsed


def _load_limit_mapping(
    line_limits: dict, key: str, *, positive_only: bool
) -> dict[str, int]:
    raw_mapping = line_limits.get(key, {})
    if not isinstance(raw_mapping, dict):
        raise RuntimeError(f"{key} must be a mapping in {LINE_LIMITS_POLICY_FILE}")
    output: dict[str, int] = {}
    for raw_path, raw_limit in raw_mapping.items():
        path = str(raw_path).strip()
        if not path:
            raise RuntimeError(f"{key} contains an empty path")
        output[path] = _coerce_positive_int(
            raw_limit,
            field_name=f"line_limits.{key}['{path}']",
            allow_zero=not positive_only,
        )
    return output


def _dynamic_field(dynamic_raw: dict, new_name: str, legacy_name: str) -> object:
    if new_name in dynamic_raw:
        return dynamic_raw.get(new_name)
    return dynamic_raw.get(legacy_name)


def _load_line_limits_policy(path: Path = LINE_LIMITS_POLICY_FILE) -> LineLimitPolicy:
    if yaml is None:
        raise RuntimeError("PyYAML is not available; cannot parse line-limit policy")
    if not path.exists():
        raise RuntimeError(f"line-limit policy missing at {path}")

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{path} must contain a mapping")

    line_limits = loaded.get("line_limits")
    if not isinstance(line_limits, dict):
        raise RuntimeError(f"{path} must contain 'line_limits' mapping")

    fixed_max_lines = _load_limit_mapping(
        line_limits, "fixed_max_lines", positive_only=True
    )
    fixed_max_chars = _load_limit_mapping(
        line_limits, "fixed_max_chars", positive_only=False
    )
    fixed_max_bytes = _load_limit_mapping(
        line_limits, "fixed_max_bytes", positive_only=False
    )

    dynamic_raw = line_limits.get("run_manifest_dynamic")
    if not isinstance(dynamic_raw, dict):
        raise RuntimeError(
            f"{path} must contain 'line_limits.run_manifest_dynamic' mapping"
        )

    target = str(dynamic_raw.get("target", RUN_MANIFEST_POLICY_KEY)).strip()
    if target and target != RUN_MANIFEST_POLICY_KEY:
        raise RuntimeError(
            f"line_limits.run_manifest_dynamic.target must be '{RUN_MANIFEST_POLICY_KEY}', got '{target}'"
        )

    count_paths = (
        dynamic_raw.get("count_paths") or dynamic_raw.get("item_count_paths") or []
    )
    if not isinstance(count_paths, list) or not count_paths:
        raise RuntimeError(
            "line_limits.run_manifest_dynamic.count_paths (or item_count_paths) must be a non-empty list"
        )
    normalized_count_paths: list[str] = []
    for raw_count_path in count_paths:
        count_path = str(raw_count_path).strip()
        if not count_path:
            raise RuntimeError(
                "line_limits.run_manifest_dynamic.count_paths contains an empty entry"
            )
        normalized_count_paths.append(count_path)

    min_cap_value = _dynamic_field(dynamic_raw, "min_cap_lines", "min_lines")
    max_cap_value = _dynamic_field(dynamic_raw, "max_cap_lines", "max_lines")

    return LineLimitPolicy(
        fixed_max_lines=fixed_max_lines,
        fixed_max_chars=fixed_max_chars,
        fixed_max_bytes=fixed_max_bytes,
        run_manifest_dynamic=RunManifestDynamicLimit(
            min_cap_lines=_coerce_positive_int(
                min_cap_value,
                field_name="line_limits.run_manifest_dynamic.min_cap_lines",
            ),
            max_cap_lines=_coerce_positive_int(
                max_cap_value,
                field_name="line_limits.run_manifest_dynamic.max_cap_lines",
            ),
            base_lines=_coerce_positive_int(
                dynamic_raw.get("base_lines"),
                field_name="line_limits.run_manifest_dynamic.base_lines",
            ),
            per_item_lines=_coerce_positive_int(
                dynamic_raw.get(
                    "per_item_lines", dynamic_raw.get("per_k_result_lines")
                ),
                field_name="line_limits.run_manifest_dynamic.per_item_lines",
                allow_zero=True,
            ),
            max_chars=_coerce_positive_int(
                dynamic_raw.get("max_chars"),
                field_name="line_limits.run_manifest_dynamic.max_chars",
                allow_zero=True,
            )
            if dynamic_raw.get("max_chars") is not None
            else None,
            max_bytes=_coerce_positive_int(
                dynamic_raw.get("max_bytes"),
                field_name="line_limits.run_manifest_dynamic.max_bytes",
                allow_zero=True,
            )
            if dynamic_raw.get("max_bytes") is not None
            else None,
            count_paths=tuple(normalized_count_paths),
        ),
    )


def _normalize_text(text: str) -> str:
    return text.replace("\r\n", "\n").strip().lower()


def _contains_placeholder(text: str, template_text: str = "") -> Optional[str]:
    normalized = _normalize_text(text)
    if not normalized:
        return "content is empty"
    for pattern in PLACEHOLDER_PATTERNS:
        if pattern.search(normalized):
            return f"contains placeholder pattern: {pattern.pattern}"
    if template_text:
        template_norm = _normalize_text(template_text)
        if template_norm and normalized == template_norm:
            return "exactly matches template placeholder content"
    return None


def _template_text(relative_path: Path) -> str:
    key = relative_path.as_posix()
    if key in BOOTSTRAP_TEMPLATE_TEXT_BY_PATH:
        return BOOTSTRAP_TEMPLATE_TEXT_BY_PATH[key]
    candidate = TEMPLATE_ROOT / relative_path
    if not candidate.exists():
        return ""
    return candidate.read_text(encoding="utf-8")


def _has_meaningful_script_line(text: str) -> bool:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if any(lowered.startswith(prefix) for prefix in COMMENTED_SCRIPT_LINES):
            continue
        if lowered.startswith("#!"):
            continue
        return True
    return False


def _is_trivial_text_file(text: str, path: Path) -> Optional[str]:
    if path.suffix in {".sh", ".bash", ".zsh", ".sbatch"}:
        if not _has_meaningful_script_line(text):
            return "script has no meaningful commands"
        return None
    meaningful_lines = [
        ln.strip()
        for ln in text.splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")
    ]
    if not meaningful_lines:
        return "content has only comments/boilerplate"
    return None


def _count_chars(text: str) -> int:
    return len(text)


def _count_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _check_size_limits(
    path: Path, policy: LineLimitPolicy, policy_key: str
) -> list[Failure]:
    if not path.exists():
        return []
    text = _read_text(path)
    failures: list[Failure] = []

    if policy_key in policy.fixed_max_lines:
        max_lines = policy.fixed_max_lines[policy_key]
        line_count = _count_lines(text)
        if line_count > max_lines:
            failures.append(
                Failure(
                    str(path),
                    f"line count {line_count} exceeds max {max_lines} (over by {line_count - max_lines})",
                )
            )

    if policy_key in policy.fixed_max_chars:
        max_chars = policy.fixed_max_chars[policy_key]
        char_count = _count_chars(text)
        if char_count > max_chars:
            failures.append(
                Failure(
                    str(path),
                    f"char count {char_count} exceeds max {max_chars} (over by {char_count - max_chars})",
                )
            )

    if policy_key in policy.fixed_max_bytes:
        max_bytes = policy.fixed_max_bytes[policy_key]
        byte_count = _count_bytes(text)
        if byte_count > max_bytes:
            failures.append(
                Failure(
                    str(path),
                    f"byte count {byte_count} exceeds max {max_bytes} (over by {byte_count - max_bytes})",
                )
            )

    return failures


def _collect_terminal_values(payload: object, path_expression: str) -> list[object]:
    segments = [
        segment.strip() for segment in path_expression.split(".") if segment.strip()
    ]
    current: list[object] = [payload]

    for raw_segment in segments:
        if not current:
            return []

        wildcard = raw_segment.endswith("[]")
        key = raw_segment[:-2] if wildcard else raw_segment
        next_values: list[object] = []

        if wildcard:
            for item in current:
                if not isinstance(item, dict):
                    continue
                raw_values = item.get(key)
                if isinstance(raw_values, list):
                    next_values.extend(raw_values)
            current = next_values
            continue

        for item in current:
            if not isinstance(item, dict):
                continue
            if key in item:
                next_values.append(item[key])
        current = next_values

    return current


def _count_manifest_items(payload: dict, count_paths: tuple[str, ...]) -> int:
    total = 0
    for path_expr in count_paths:
        for terminal in _collect_terminal_values(payload, path_expr):
            if isinstance(terminal, list):
                total += len(terminal)
            elif terminal is not None:
                total += 1
    return total


def _run_manifest_dynamic_limit(
    policy: RunManifestDynamicLimit, payload: dict
) -> tuple[int, int]:
    item_count = max(0, int(_count_manifest_items(payload, policy.count_paths)))
    candidate = policy.base_lines + policy.per_item_lines * item_count
    dynamic_lines = max(policy.min_cap_lines, min(policy.max_cap_lines, candidate))
    return item_count, dynamic_lines


def _check_run_manifest_limits(path: Path, policy: LineLimitPolicy) -> list[Failure]:
    if not path.exists():
        return [Failure(str(path), "required file is missing")]

    try:
        payload = load_json(path)
    except Exception as exc:
        return [Failure(str(path), f"could not evaluate dynamic cap: {exc}")]

    text = _read_text(path)
    failures: list[Failure] = []
    item_count, dynamic_lines = _run_manifest_dynamic_limit(
        policy.run_manifest_dynamic, payload
    )
    line_count = _count_lines(text)
    if line_count > dynamic_lines:
        failures.append(
            Failure(
                str(path),
                (
                    f"line count {line_count} exceeds dynamic cap {dynamic_lines} "
                    f"for manifest-count {item_count} (over by {line_count - dynamic_lines})"
                ),
            )
        )

    if policy.run_manifest_dynamic.max_chars is not None:
        max_chars = policy.run_manifest_dynamic.max_chars
        char_count = _count_chars(text)
        if char_count > max_chars:
            failures.append(
                Failure(
                    str(path),
                    f"char count {char_count} exceeds dynamic max {max_chars} (over by {char_count - max_chars})",
                )
            )
    if policy.run_manifest_dynamic.max_bytes is not None:
        max_bytes = policy.run_manifest_dynamic.max_bytes
        byte_count = _count_bytes(text)
        if byte_count > max_bytes:
            failures.append(
                Failure(
                    str(path),
                    f"byte count {byte_count} exceeds dynamic max {max_bytes} (over by {byte_count - max_bytes})",
                )
            )
    return failures


def _check_file_text(path: Path, *, relative: Path) -> list[Failure]:
    if not path.exists():
        return [Failure(str(path), "required file is missing")]
    text = _read_text(path)
    template_text = _template_text(relative)

    reason = _contains_placeholder(text, template_text)
    if reason:
        return [Failure(str(path), reason)]

    reason = _is_trivial_text_file(text, path)
    if reason:
        return [Failure(str(path), reason)]

    return []


def _normalize_evidence_value(value: str) -> str:
    normalized = str(value).strip()
    if normalized.startswith("`") and normalized.endswith("`") and len(normalized) >= 2:
        normalized = normalized[1:-1].strip()
    return normalized


def _artifact_is_textual(path_text: str) -> bool:
    normalized = _normalize_evidence_value(path_text).lower()
    if not normalized:
        return False
    if " or " in normalized:
        return True
    return not normalized.endswith(NON_TEXT_EVIDENCE_SUFFIXES)


def _check_evidence_records_markdown(path: Path) -> list[Failure]:
    if not path.exists():
        return []
    text = _read_text(path)
    lines = text.splitlines()
    failures: list[Failure] = []
    index = 0
    found_evidence_block = False

    while index < len(lines):
        match = EVIDENCE_ARTIFACT_PATTERN.match(lines[index])
        if not match:
            index += 1
            continue

        found_evidence_block = True
        fields: dict[str, str] = {"artifact_path": match.group(1).strip()}
        cursor = index + 1
        while cursor < len(lines):
            if EVIDENCE_ARTIFACT_PATTERN.match(lines[cursor]):
                break
            field_match = EVIDENCE_FIELD_PATTERN.match(lines[cursor])
            if field_match:
                key = str(field_match.group(1)).strip().lower()
                value = str(field_match.group(2)).strip()
                if key and value and key not in fields:
                    fields[key] = value
            cursor += 1

        for required in ("what_it_proves", "verifier_output_pointer"):
            if not str(fields.get(required, "")).strip():
                failures.append(
                    Failure(
                        str(path),
                        f"evidence block for artifact_path '{fields['artifact_path']}' is missing required field '{required}'",
                    )
                )

        artifact_path = fields.get("artifact_path", "")
        if _artifact_is_textual(artifact_path):
            for required in ("excerpt", "command"):
                if not str(fields.get(required, "")).strip():
                    failures.append(
                        Failure(
                            str(path),
                            (
                                "evidence block for textual artifact_path "
                                f"'{artifact_path}' is missing required field '{required}'"
                            ),
                        )
                    )
        index = cursor

    if not found_evidence_block:
        return []
    return failures


def _check_hypothesis(path: Path) -> list[Failure]:
    failures: list[Failure] = []
    if not path.exists():
        return [Failure(str(path), "required file is missing")]
    text = _read_text(path)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    metric_lines = [line for line in lines if line.startswith("PrimaryMetric:")]
    if len(metric_lines) != 1:
        failures.append(
            Failure(
                str(path), "must contain exactly one 'PrimaryMetric:' definition line"
            )
        )
    elif not PRIMARY_METRIC_LINE_PATTERN.match(metric_lines[0]):
        failures.append(
            Failure(
                str(path),
                "PrimaryMetric line must match 'PrimaryMetric: metric_name; Unit: unit_name; Success: ...' format",
            )
        )

    key_values = _extract_markdown_key_values(text)
    metric_mode = str(key_values.get("metric_mode", "")).strip().lower()
    if metric_mode not in {"maximize", "minimize"}:
        failures.append(
            Failure(
                str(path),
                "metric_mode must be present in hypothesis metadata and equal to maximize|minimize",
            )
        )
        return failures

    target_delta_raw = (
        key_values.get("target_delta")
        or key_values.get("expected_delta")
        or key_values.get("success_delta")
        or ""
    )
    target_delta = _parse_numeric_delta(target_delta_raw)
    if target_delta is None:
        failures.append(
            Failure(
                str(path),
                "target_delta must include a numeric value in hypothesis metadata",
            )
        )
        return failures
    if metric_mode == "maximize" and target_delta <= 0:
        failures.append(
            Failure(
                str(path),
                "target_delta must be positive when metric_mode=maximize",
            )
        )
    if metric_mode == "minimize" and target_delta >= 0:
        failures.append(
            Failure(
                str(path),
                "target_delta must be negative when metric_mode=minimize",
            )
        )
    return failures


def _check_review_result(path: Path) -> list[Failure]:
    # Boundary note: the structural checks below (required keys, status enum,
    # required_checks presence) overlap with schema_checks._validate_review_result.
    # Kept here intentionally for fast pre-flight error messages without jsonschema.
    if not path.exists():
        return [Failure(str(path), "required file is missing")]
    text = _read_text(path)
    template_text = _template_text(Path("review_result.json"))
    reason = _contains_placeholder(text, template_text)
    if reason:
        return [Failure(str(path), reason)]

    data = load_json(path)
    required = {"status", "blocking_findings", "required_checks", "reviewed_at"}
    missing = required - data.keys()
    if missing:
        return [Failure(str(path), f"missing required keys: {sorted(missing)}")]

    status = data.get("status")
    if status not in {"pass", "needs_retry", "failed"}:
        return [Failure(str(path), f"invalid status '{status}'")]

    blocking_findings = data.get("blocking_findings")
    if not isinstance(blocking_findings, list):
        return [Failure(str(path), "'blocking_findings' must be a list")]

    required_checks = data.get("required_checks")
    if not isinstance(required_checks, dict):
        return [Failure(str(path), "'required_checks' must be a mapping")]

    failures: list[Failure] = []
    for check_name in REVIEW_RESULT_REQUIRED_CHECKS:
        if check_name not in required_checks:
            failures.append(
                Failure(str(path), f"required_checks missing '{check_name}'")
            )
            continue
        check_status = str(required_checks.get(check_name, "")).strip().lower()
        if check_status not in REVIEW_RESULT_CHECK_STATUSES:
            failures.append(
                Failure(
                    str(path),
                    f"required_checks['{check_name}'] must be one of {sorted(REVIEW_RESULT_CHECK_STATUSES)}",
                )
            )
    return failures


def _check_design(path: Path, iteration_id: str) -> list[Failure]:
    # Boundary note: the structural checks below (required keys, schema_version,
    # iteration_id match, entrypoint/compute/baselines shape) overlap with
    # schema_checks._validate_design.  Kept here for fast pre-flight without jsonschema.
    if not path.exists():
        return [Failure(str(path), "required file is missing")]
    text = _read_text(path)
    template_text = _template_text(Path("design.yaml"))
    reason = _contains_placeholder(text, template_text)
    if reason:
        return [Failure(str(path), reason)]

    data = load_yaml(path)
    required = {
        "schema_version",
        "id",
        "iteration_id",
        "hypothesis_id",
        "entrypoint",
        "compute",
        "metrics",
        "baselines",
    }
    missing = required - data.keys()
    if missing:
        return [Failure(str(path), f"missing required keys: {sorted(missing)}")]

    if str(data.get("schema_version", "")).strip() != "1.0":
        return [Failure(str(path), "schema_version must be '1.0'")]

    if str(data.get("iteration_id", "")).strip() != iteration_id:
        return [Failure(str(path), "iteration_id does not match .autolab/state.json")]

    entrypoint = data.get("entrypoint")
    if not isinstance(entrypoint, dict) or "module" not in entrypoint:
        return [Failure(str(path), "entrypoint.module is required")]

    compute = data.get("compute")
    if not isinstance(compute, dict) or "location" not in compute:
        return [Failure(str(path), "compute.location is required")]

    baselines = data.get("baselines")
    if not isinstance(baselines, list) or not baselines:
        return [Failure(str(path), "baselines must be a non-empty list")]

    return []


def _check_decision_result(path: Path) -> list[Failure]:
    # Boundary note: the structural checks below (schema_version, decision enum,
    # rationale/evidence/risks shape) overlap with schema_checks._validate_decision_result.
    # Kept here for fast pre-flight without jsonschema.
    if not path.exists():
        return [Failure(str(path), "required file is missing")]
    text = _read_text(path)
    reason = _contains_placeholder(text)
    if reason:
        return [Failure(str(path), reason)]

    data = load_json(path)
    if str(data.get("schema_version", "")).strip() != "1.0":
        return [Failure(str(path), "schema_version must be '1.0'")]

    decision = str(data.get("decision", "")).strip()
    if decision not in DECISION_RESULT_ALLOWED:
        return [
            Failure(
                str(path),
                f"decision must be one of {sorted(DECISION_RESULT_ALLOWED)}",
            )
        ]

    rationale = str(data.get("rationale", "")).strip()
    if not rationale:
        return [Failure(str(path), "rationale must be non-empty")]

    evidence = data.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return [Failure(str(path), "evidence must be a non-empty list")]

    risks = data.get("risks")
    if not isinstance(risks, list):
        return [Failure(str(path), "risks must be a list")]

    return []


def _check_run_manifest(path: Path, iteration_id: str, run_id: str) -> list[Failure]:
    # Boundary note: schema_version and iteration_id/run_id match checks overlap
    # with schema_checks._validate_run_manifest.  Kept here for fast pre-flight.
    if not path.exists():
        return [Failure(str(path), "required file is missing")]
    text = _read_text(path)
    template_text = _template_text(Path("runs") / "<RUN_ID>" / "run_manifest.json")
    reason = _contains_placeholder(text, template_text)
    if reason:
        return [Failure(str(path), reason)]

    data = load_json(path)
    if str(data.get("schema_version", "")).strip() != "1.0":
        return [Failure(str(path), "schema_version must be '1.0'")]
    if data.get("iteration_id") and data.get("iteration_id") != iteration_id:
        return [Failure(str(path), "iteration_id does not match .autolab/state.json")]
    if data.get("run_id") and data.get("run_id") != run_id:
        return [
            Failure(str(path), "run_id does not match .autolab/state.json.last_run_id")
        ]

    return []


def _check_metrics(path: Path) -> list[Failure]:
    # Boundary note: schema_version check overlaps with schema_checks._validate_metrics.
    # Kept here for fast pre-flight.
    if not path.exists():
        return [Failure(str(path), "required file is missing")]
    text = _read_text(path)
    reason = _contains_placeholder(
        text, _template_text(Path("runs") / "<RUN_ID>" / "metrics.json")
    )
    if reason:
        return [Failure(str(path), reason)]

    data = load_json(path)
    if str(data.get("schema_version", "")).strip() != "1.0":
        return [Failure(str(path), "schema_version must be '1.0'")]
    if not data:
        return [Failure(str(path), "metrics content is empty")]
    if data == {"primary_metric": None, "status": "pending"}:
        return [Failure(str(path), "metrics is placeholder-like")]
    return []


def _load_stage_output_contract(
    stage: str,
) -> tuple[list[str], list[list[str]], list[tuple[dict[str, str], list[str]]]]:
    try:
        workflow = load_yaml(WORKFLOW_FILE)
    except Exception:
        return ([], [], [])
    stages = workflow.get("stages")
    if not isinstance(stages, dict):
        return ([], [], [])
    stage_spec = stages.get(stage)
    if not isinstance(stage_spec, dict):
        return ([], [], [])

    required_outputs_raw = stage_spec.get("required_outputs", [])
    required_outputs = (
        [str(item).strip() for item in required_outputs_raw if str(item).strip()]
        if isinstance(required_outputs_raw, list)
        else []
    )

    any_of_outputs_raw = stage_spec.get("required_outputs_any_of", [])
    any_of_outputs: list[list[str]] = []
    if isinstance(any_of_outputs_raw, list):
        for group in any_of_outputs_raw:
            if not isinstance(group, list):
                continue
            normalized_group = [
                str(item).strip() for item in group if str(item).strip()
            ]
            if normalized_group:
                any_of_outputs.append(normalized_group)

    conditional_raw = stage_spec.get("required_outputs_if", [])
    if isinstance(conditional_raw, dict):
        conditional_raw = [conditional_raw]
    conditional_outputs: list[tuple[dict[str, str], list[str]]] = []
    if isinstance(conditional_raw, list):
        for raw_rule in conditional_raw:
            if not isinstance(raw_rule, dict):
                continue
            outputs_raw = raw_rule.get("outputs", [])
            if not isinstance(outputs_raw, list):
                continue
            outputs = [str(item).strip() for item in outputs_raw if str(item).strip()]
            if not outputs:
                continue
            conditions: dict[str, str] = {}
            for raw_key, raw_value in raw_rule.items():
                key = str(raw_key).strip()
                if not key or key == "outputs":
                    continue
                conditions[key] = str(raw_value).strip().lower()
            if conditions:
                conditional_outputs.append((conditions, outputs))

    return (required_outputs, any_of_outputs, conditional_outputs)


def _replace_pattern_tokens(path_template: str, context: dict[str, str]) -> str:
    resolved = str(path_template)
    replacements = {
        "<RUN_ID>": context.get("run_id", ""),
        "<ITERATION_ID>": context.get("iteration_id", ""),
    }
    for token, value in replacements.items():
        if value:
            resolved = resolved.replace(token, value)
    return resolved


def _resolve_required_output_path(
    path_template: str,
    *,
    iteration_dir: Path,
    context: dict[str, str],
) -> tuple[Path | None, str | None]:
    if "{{" in str(path_template) or "}}" in str(path_template):
        return (
            None,
            (
                f"required output path '{path_template}' uses prompt-style mustache token(s); "
                "workflow output contracts must use pattern tokens like <RUN_ID>"
            ),
        )

    resolved = _replace_pattern_tokens(path_template, context)
    if "<" in resolved and ">" in resolved:
        return (
            None,
            f"required output path '{path_template}' has unresolved pattern token(s)",
        )

    candidate = Path(resolved)
    if candidate.is_absolute():
        return (candidate, None)

    root_scoped_prefixes = (
        ".autolab/",
        "docs/",
        "paper/",
        "src/",
        "scripts/",
        "tests/",
        "examples/",
        "experiments/",
    )
    normalized = resolved.replace("\\", "/")
    if normalized.startswith(root_scoped_prefixes):
        return (REPO_ROOT / candidate, None)
    return (iteration_dir / candidate, None)


def _resolve_host_mode_for_contract(iteration_dir: Path, run_id: str) -> str:
    run_id_value = str(run_id).strip()
    if run_id_value and not run_id_value.startswith("<"):
        manifest_path = iteration_dir / "runs" / run_id_value / "run_manifest.json"
        if manifest_path.exists():
            try:
                manifest_payload = load_json(manifest_path)
            except Exception:
                manifest_payload = {}
            if isinstance(manifest_payload, dict):
                manifest_host_mode = (
                    str(
                        manifest_payload.get("host_mode")
                        or manifest_payload.get("launch_mode")
                        or manifest_payload.get("location")
                        or ""
                    )
                    .strip()
                    .lower()
                )
                if manifest_host_mode in {"local", "slurm"}:
                    return manifest_host_mode

    design_path = iteration_dir / "design.yaml"
    if design_path.exists():
        try:
            design_payload = load_yaml(design_path)
        except Exception:
            design_payload = {}
        if isinstance(design_payload, dict):
            compute = design_payload.get("compute")
            if isinstance(compute, dict):
                design_host_mode = str(compute.get("location", "")).strip().lower()
                if design_host_mode in {"local", "slurm"}:
                    return design_host_mode
    return ""


def _check_registry_required_outputs(
    *,
    stage: str,
    iteration_dir: Path,
    state: dict,
) -> list[Failure]:
    required_outputs, any_of_outputs, conditional_outputs = _load_stage_output_contract(
        stage
    )
    if not required_outputs and not any_of_outputs and not conditional_outputs:
        return []

    run_id = _resolve_run_id_for_stage(state, stage)
    iteration_id = str(state.get("iteration_id", "")).strip()
    context = {
        "iteration_id": iteration_id,
        "run_id": run_id,
        "host_mode": _resolve_host_mode_for_contract(iteration_dir, run_id),
    }

    failures: list[Failure] = []

    for output in required_outputs:
        resolved_path, error = _resolve_required_output_path(
            output,
            iteration_dir=iteration_dir,
            context=context,
        )
        if error:
            failures.append(Failure(output, error))
            continue
        assert resolved_path is not None
        if not resolved_path.exists():
            failures.append(
                Failure(
                    str(resolved_path),
                    f"required output from workflow.yaml is missing for stage '{stage}'",
                )
            )

    for group in any_of_outputs:
        resolved_candidates: list[Path] = []
        group_errors: list[str] = []
        for output in group:
            resolved_path, error = _resolve_required_output_path(
                output,
                iteration_dir=iteration_dir,
                context=context,
            )
            if error:
                group_errors.append(error)
                continue
            assert resolved_path is not None
            resolved_candidates.append(resolved_path)
        if any(path.exists() for path in resolved_candidates):
            continue
        if group_errors:
            failures.append(Failure(" | ".join(group), "; ".join(group_errors)))
            continue
        candidate_text = ", ".join(str(path) for path in resolved_candidates)
        failures.append(
            Failure(
                candidate_text,
                (
                    "required one-of outputs from workflow.yaml are all missing "
                    f"for stage '{stage}'"
                ),
            )
        )

    for conditions, outputs in conditional_outputs:
        should_enforce = True
        for key, expected in conditions.items():
            actual = str(context.get(key, "")).strip().lower()
            if actual != expected:
                should_enforce = False
                break
        if not should_enforce:
            continue

        for output in outputs:
            resolved_path, error = _resolve_required_output_path(
                output,
                iteration_dir=iteration_dir,
                context=context,
            )
            if error:
                failures.append(Failure(output, error))
                continue
            assert resolved_path is not None
            if not resolved_path.exists():
                condition_text = ", ".join(
                    f"{key}={value}" for key, value in conditions.items()
                )
                failures.append(
                    Failure(
                        str(resolved_path),
                        (
                            "conditional required output from workflow.yaml is missing "
                            f"(condition: {condition_text})"
                        ),
                    )
                )
    return failures


def _check_launch_scripts(
    iteration_dir: Path, policy: LineLimitPolicy
) -> list[Failure]:
    failures: list[Failure] = []
    candidates = [Path("launch/run_local.sh"), Path("launch/run_slurm.sbatch")]
    existing = [
        relative for relative in candidates if (iteration_dir / relative).exists()
    ]
    if not existing:
        return []

    for relative in existing:
        path = iteration_dir / relative
        failures.extend(_check_file_text(path, relative=relative))
        failures.extend(_check_size_limits(path, policy, relative.as_posix()))
    return failures


def _resolve_run_id_for_stage(state: dict, stage: str) -> str:
    """Resolve the effective run_id, mirroring prompts.py fallback logic.

    Order: pending_run_id (launch only) -> .autolab/run_context.json
    (launch only, matching iteration) -> last_run_id.
    """
    if stage == "launch":
        pending = str(state.get("pending_run_id", "")).strip()
        if pending and not pending.startswith("<"):
            return pending
        run_context_path = REPO_ROOT / ".autolab" / "run_context.json"
        if run_context_path.exists():
            try:
                payload = json.loads(run_context_path.read_text(encoding="utf-8"))
            except Exception:
                payload = None
            if isinstance(payload, dict):
                ctx_stage = str(payload.get("stage", "")).strip()
                ctx_iter = str(payload.get("iteration_id", "")).strip()
                state_iter = str(state.get("iteration_id", "")).strip()
                ctx_run = str(payload.get("run_id", "")).strip()
                if (
                    ctx_stage == "launch"
                    and ctx_iter
                    and ctx_iter == state_iter
                    and ctx_run
                    and not ctx_run.startswith("<")
                ):
                    return ctx_run
    return str(state.get("last_run_id", "")).strip()


def _stage_checks(
    stage: str,
    iteration_dir: Path,
    state: dict,
    line_limit_policy: LineLimitPolicy,
) -> list[Failure]:
    checks: list[Failure] = _check_registry_required_outputs(
        stage=stage,
        iteration_dir=iteration_dir,
        state=state,
    )
    run_id = _resolve_run_id_for_stage(state, stage)
    iteration_id = str(state.get("iteration_id", "")).strip()

    if stage == "hypothesis":
        relative = Path("hypothesis.md")
        path = iteration_dir / relative
        checks.extend(_check_file_text(path, relative=relative))
        checks.extend(_check_hypothesis(path))
        checks.extend(_check_size_limits(path, line_limit_policy, relative.as_posix()))
        return checks

    if stage == "design":
        path = iteration_dir / "design.yaml"
        checks.extend(_check_design(path, iteration_id))
        checks.extend(_check_size_limits(path, line_limit_policy, "design.yaml"))
        return checks

    if stage == "implementation":
        relative = Path("implementation_plan.md")
        path = iteration_dir / relative
        checks.extend(_check_file_text(path, relative=relative))
        checks.extend(_check_evidence_records_markdown(path))
        checks.extend(_check_size_limits(path, line_limit_policy, relative.as_posix()))
        return checks

    if stage == "implementation_review":
        for relative in [Path("implementation_review.md"), Path("review_result.json")]:
            path = iteration_dir / relative
            if path.suffix == ".json":
                checks.extend(_check_review_result(path))
            else:
                checks.extend(_check_file_text(path, relative=relative))
                checks.extend(_check_evidence_records_markdown(path))
            checks.extend(
                _check_size_limits(path, line_limit_policy, relative.as_posix())
            )
        return checks

    if stage == "launch":
        checks.extend(_check_launch_scripts(iteration_dir, line_limit_policy))
        if not run_id or run_id.startswith("<"):
            checks.append(
                Failure(
                    f"{iteration_dir}/runs/<RUN_ID>",
                    "state.json has missing/placeholder last_run_id",
                )
            )
            return checks
        manifest_path = iteration_dir / "runs" / run_id / "run_manifest.json"
        checks.extend(_check_run_manifest(manifest_path, iteration_id, run_id))
        checks.extend(_check_run_manifest_limits(manifest_path, line_limit_policy))
        return checks

    if stage == "extract_results":
        if not run_id or run_id.startswith("<"):
            checks.append(
                Failure(
                    f"{iteration_dir}/runs/<RUN_ID>",
                    "state.json has missing/placeholder last_run_id",
                )
            )
            return checks

        run_dir = iteration_dir / "runs" / run_id
        manifest_path = run_dir / "run_manifest.json"
        metrics_path = run_dir / "metrics.json"

        checks.extend(_check_run_manifest(manifest_path, iteration_id, run_id))
        checks.extend(_check_metrics(metrics_path))
        checks.extend(
            _check_size_limits(metrics_path, line_limit_policy, RUN_METRICS_POLICY_KEY)
        )
        checks.extend(_check_run_manifest_limits(manifest_path, line_limit_policy))
        summary_relative = Path("analysis/summary.md")
        summary_path = iteration_dir / summary_relative
        checks.extend(_check_file_text(summary_path, relative=summary_relative))
        checks.extend(_check_evidence_records_markdown(summary_path))
        checks.extend(
            _check_size_limits(
                summary_path, line_limit_policy, summary_relative.as_posix()
            )
        )
        return checks

    if stage == "update_docs":
        for relative in [Path("docs_update.md"), Path("analysis/summary.md")]:
            path = iteration_dir / relative
            checks.extend(_check_file_text(path, relative=relative))
            checks.extend(_check_evidence_records_markdown(path))
            checks.extend(
                _check_size_limits(path, line_limit_policy, relative.as_posix())
            )
        return checks

    if stage == "decide_repeat":
        path = iteration_dir / "decision_result.json"
        checks.extend(_check_decision_result(path))
        return checks

    return checks


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", default=None, help="Override current stage from .autolab/state.json"
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON envelope",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        state = load_state()
    except Exception as exc:
        if args.json:
            envelope = {
                "status": "fail",
                "verifier": "template_fill",
                "stage": "",
                "checks": [],
                "errors": [str(exc)],
            }
            print(json.dumps(envelope))
        else:
            print(f"template_fill: ERROR {exc}")
        return 1

    try:
        line_limit_policy = _load_line_limits_policy()
    except Exception as exc:
        if args.json:
            envelope = {
                "status": "fail",
                "verifier": "template_fill",
                "stage": "",
                "checks": [],
                "errors": [str(exc)],
            }
            print(json.dumps(envelope))
        else:
            print(f"template_fill: ERROR {exc}")
        return 1

    stage = args.stage or str(state.get("stage", "")).strip()
    iteration_id = str(state.get("iteration_id", "")).strip()
    iteration_dir = resolve_iteration_dir(iteration_id)

    if not iteration_id or iteration_id.startswith("<"):
        if args.json:
            envelope = {
                "status": "fail",
                "verifier": "template_fill",
                "stage": "",
                "checks": [],
                "errors": ["iteration_id in state is missing or placeholder"],
            }
            print(json.dumps(envelope))
        else:
            print(
                "template_fill: ERROR iteration_id in state is missing or placeholder"
            )
        return 1
    if not iteration_dir.exists():
        if args.json:
            envelope = {
                "status": "fail",
                "verifier": "template_fill",
                "stage": stage,
                "checks": [],
                "errors": [f"iteration workspace missing at {iteration_dir}"],
            }
            print(json.dumps(envelope))
        else:
            print(
                f"template_fill: ERROR iteration workspace missing at {iteration_dir}"
            )
        return 1
    if not stage:
        if args.json:
            envelope = {
                "status": "fail",
                "verifier": "template_fill",
                "stage": "",
                "checks": [],
                "errors": ["state stage is missing"],
            }
            print(json.dumps(envelope))
        else:
            print("template_fill: ERROR state stage is missing")
        return 1

    failures = _stage_checks(stage, iteration_dir, state, line_limit_policy)

    passed = not failures

    if args.json:
        checks = [
            {"name": f.path, "status": "fail", "detail": f.reason} for f in failures
        ]
        if passed:
            checks = [
                {
                    "name": "template_fill",
                    "status": "pass",
                    "detail": f"stage={stage} iteration={iteration_id}",
                }
            ]
        envelope = {
            "status": "pass" if passed else "fail",
            "verifier": "template_fill",
            "stage": stage,
            "checks": checks,
            "errors": [f"{f.path}\t{f.reason}" for f in failures],
        }
        print(json.dumps(envelope))
    else:
        if failures:
            print(f"template_fill: FAIL stage={stage} issues={len(failures)}")
            for failure in failures:
                print(f"{failure.path}\t{failure.reason}")
            hint_texts = suggest_fix_hints(
                [f"{failure.path}\t{failure.reason}" for failure in failures],
                stage=stage,
                verifier="template_fill",
            )
            if hint_texts:
                print("\nMost likely fixes:")
                for hint in hint_texts:
                    print(f"- {hint}")
        else:
            print(f"template_fill: PASS stage={stage} iteration={iteration_id}")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
