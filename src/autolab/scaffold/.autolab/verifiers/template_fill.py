#!/usr/bin/env python3
"""Verifier for template-completion and artifact budgets."""

from __future__ import annotations

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


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / ".autolab" / "state.json"
TEMPLATE_ROOT = REPO_ROOT / "experiments" / "<ITERATION_ID>"
LINE_LIMITS_POLICY_FILE = REPO_ROOT / ".autolab" / "experiment_file_line_limits.yaml"
RUN_METRICS_POLICY_KEY = "runs/<RUN_ID>/metrics.json"
RUN_MANIFEST_POLICY_KEY = "runs/<RUN_ID>/run_manifest.json"
REVIEW_RESULT_REQUIRED_CHECKS = (
    "tests",
    "dry_run",
    "schema",
    "env_smoke",
    "docs_target_update",
)
REVIEW_RESULT_CHECK_STATUSES = {"pass", "skip", "fail"}
PRIMARY_METRIC_LINE_PATTERN = re.compile(
    r"^PrimaryMetric:\s*[^;]+;\s*Unit:\s*[^;]+;\s*Success:\s*.+$"
)

PLACEHOLDER_PATTERNS = (
    re.compile(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}"),
    re.compile(r"<[A-Za-z0-9_]+>"),
    re.compile(r"\bTODO:\b", re.IGNORECASE),
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bTBD\b", re.IGNORECASE),
    re.compile(r"\bFIXME\b", re.IGNORECASE),
)

COMMENTED_SCRIPT_LINES = ("#!", "#", "set -e", "set -u", "set -uo", "set -o")


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


def _coerce_positive_int(value: object, *, field_name: str, allow_zero: bool = False) -> int:
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


def _load_state() -> dict:
    if not STATE_FILE.exists():
        raise RuntimeError(f"state.json missing at {STATE_FILE}")
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("state.json must contain a JSON object")
    return data


def _load_yaml(path: Path) -> dict:
    if yaml is None:
        raise RuntimeError("PyYAML is not available; cannot parse YAML artifacts")
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{path} must contain a mapping")
    return loaded


def _load_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return payload


def _load_limit_mapping(line_limits: dict, key: str, *, positive_only: bool) -> dict[str, int]:
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

    fixed_max_lines = _load_limit_mapping(line_limits, "fixed_max_lines", positive_only=True)
    fixed_max_chars = _load_limit_mapping(line_limits, "fixed_max_chars", positive_only=False)
    fixed_max_bytes = _load_limit_mapping(line_limits, "fixed_max_bytes", positive_only=False)

    dynamic_raw = line_limits.get("run_manifest_dynamic")
    if not isinstance(dynamic_raw, dict):
        raise RuntimeError(f"{path} must contain 'line_limits.run_manifest_dynamic' mapping")

    target = str(dynamic_raw.get("target", RUN_MANIFEST_POLICY_KEY)).strip()
    if target and target != RUN_MANIFEST_POLICY_KEY:
        raise RuntimeError(
            f"line_limits.run_manifest_dynamic.target must be '{RUN_MANIFEST_POLICY_KEY}', got '{target}'"
        )

    count_paths = dynamic_raw.get("count_paths", [])
    if not isinstance(count_paths, list) or not count_paths:
        raise RuntimeError("line_limits.run_manifest_dynamic.count_paths must be a non-empty list")
    normalized_count_paths: list[str] = []
    for raw_count_path in count_paths:
        count_path = str(raw_count_path).strip()
        if not count_path:
            raise RuntimeError("line_limits.run_manifest_dynamic.count_paths contains an empty entry")
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
                dynamic_raw.get("per_item_lines", dynamic_raw.get("per_k_result_lines")),
                field_name="line_limits.run_manifest_dynamic.per_item_lines",
                allow_zero=True,
            ),
            max_chars=_coerce_positive_int(
                dynamic_raw.get("max_chars"),
                field_name="line_limits.run_manifest_dynamic.max_chars",
                allow_zero=True,
            ) if dynamic_raw.get("max_chars") is not None else None,
            max_bytes=_coerce_positive_int(
                dynamic_raw.get("max_bytes"),
                field_name="line_limits.run_manifest_dynamic.max_bytes",
                allow_zero=True,
            ) if dynamic_raw.get("max_bytes") is not None else None,
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
    meaningful_lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not meaningful_lines:
        return "content has only comments/boilerplate"
    return None


def _count_chars(text: str) -> int:
    return len(text)


def _count_bytes(text: str) -> int:
    return len(text.encode("utf-8"))


def _check_size_limits(path: Path, policy: LineLimitPolicy, policy_key: str) -> list[Failure]:
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
    segments = [segment.strip() for segment in path_expression.split(".") if segment.strip()]
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


def _run_manifest_dynamic_limit(policy: RunManifestDynamicLimit, payload: dict) -> tuple[int, int]:
    item_count = max(0, int(_count_manifest_items(payload, policy.count_paths)))
    candidate = policy.base_lines + policy.per_item_lines * item_count
    dynamic_lines = max(policy.min_cap_lines, min(policy.max_cap_lines, candidate))
    return item_count, dynamic_lines


def _check_run_manifest_limits(path: Path, policy: LineLimitPolicy) -> list[Failure]:
    if not path.exists():
        return [Failure(str(path), "required file is missing")]

    try:
        payload = _load_json(path)
    except Exception as exc:
        return [Failure(str(path), f"could not evaluate dynamic cap: {exc}")]

    text = _read_text(path)
    failures: list[Failure] = []
    item_count, dynamic_lines = _run_manifest_dynamic_limit(policy.run_manifest_dynamic, payload)
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


def _check_hypothesis(path: Path) -> list[Failure]:
    failures: list[Failure] = []
    if not path.exists():
        return [Failure(str(path), "required file is missing")]
    text = _read_text(path)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    metric_lines = [line for line in lines if line.startswith("PrimaryMetric:")]
    if len(metric_lines) != 1:
        failures.append(Failure(str(path), "must contain exactly one 'PrimaryMetric:' definition line"))
    elif not PRIMARY_METRIC_LINE_PATTERN.match(metric_lines[0]):
        failures.append(
            Failure(
                str(path),
                "PrimaryMetric line must match 'PrimaryMetric: <name>; Unit: <unit>; Success: ...' format",
            )
        )
    return failures


def _check_review_result(path: Path) -> list[Failure]:
    if not path.exists():
        return [Failure(str(path), "required file is missing")]
    text = _read_text(path)
    template_text = _template_text(Path("review_result.json"))
    reason = _contains_placeholder(text, template_text)
    if reason:
        return [Failure(str(path), reason)]

    data = _load_json(path)
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
            failures.append(Failure(str(path), f"required_checks missing '{check_name}'"))
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
    if not path.exists():
        return [Failure(str(path), "required file is missing")]
    text = _read_text(path)
    template_text = _template_text(Path("design.yaml"))
    reason = _contains_placeholder(text, template_text)
    if reason:
        return [Failure(str(path), reason)]

    data = _load_yaml(path)
    required = {"id", "iteration_id", "hypothesis_id", "entrypoint", "compute", "metrics", "baselines"}
    missing = required - data.keys()
    if missing:
        return [Failure(str(path), f"missing required keys: {sorted(missing)}")]

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


def _check_run_manifest(path: Path, iteration_id: str, run_id: str) -> list[Failure]:
    if not path.exists():
        return [Failure(str(path), "required file is missing")]
    text = _read_text(path)
    template_text = _template_text(Path("runs") / "<RUN_ID>" / "run_manifest.json")
    reason = _contains_placeholder(text, template_text)
    if reason:
        return [Failure(str(path), reason)]

    data = _load_json(path)
    if data.get("iteration_id") and data.get("iteration_id") != iteration_id:
        return [Failure(str(path), "iteration_id does not match .autolab/state.json")]
    if data.get("run_id") and data.get("run_id") != run_id:
        return [Failure(str(path), "run_id does not match .autolab/state.json.last_run_id")]

    return []


def _check_metrics(path: Path) -> list[Failure]:
    if not path.exists():
        return [Failure(str(path), "required file is missing")]
    text = _read_text(path)
    reason = _contains_placeholder(text, _template_text(Path("runs") / "<RUN_ID>" / "metrics.json"))
    if reason:
        return [Failure(str(path), reason)]

    data = _load_json(path)
    if not data:
        return [Failure(str(path), "metrics content is empty")]
    if data == {"primary_metric": None, "status": "pending"}:
        return [Failure(str(path), "metrics is placeholder-like")]
    return []


def _check_launch_scripts(iteration_dir: Path, policy: LineLimitPolicy) -> list[Failure]:
    failures: list[Failure] = []
    candidates = [Path("launch/run_local.sh"), Path("launch/run_slurm.sbatch")]
    existing = [relative for relative in candidates if (iteration_dir / relative).exists()]
    if not existing:
        return [Failure(str(iteration_dir / "launch"), "launch stage requires run_local.sh or run_slurm.sbatch")]

    for relative in existing:
        path = iteration_dir / relative
        failures.extend(_check_file_text(path, relative=relative))
        failures.extend(_check_size_limits(path, policy, relative.as_posix()))
    return failures


def _stage_checks(
    stage: str,
    iteration_dir: Path,
    state: dict,
    line_limit_policy: LineLimitPolicy,
) -> list[Failure]:
    checks: list[Failure] = []
    run_id = str(state.get("last_run_id", "")).strip()
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
        checks.extend(_check_size_limits(path, line_limit_policy, relative.as_posix()))
        return checks

    if stage == "implementation_review":
        for relative in [Path("implementation_review.md"), Path("review_result.json")]:
            path = iteration_dir / relative
            if path.suffix == ".json":
                checks.extend(_check_review_result(path))
            else:
                checks.extend(_check_file_text(path, relative=relative))
            checks.extend(_check_size_limits(path, line_limit_policy, relative.as_posix()))
        return checks

    if stage == "launch":
        checks.extend(_check_launch_scripts(iteration_dir, line_limit_policy))
        if not run_id or run_id.startswith("<"):
            checks.append(Failure(f"{iteration_dir}/runs/<RUN_ID>", "state.json has missing/placeholder last_run_id"))
            return checks
        manifest_path = iteration_dir / "runs" / run_id / "run_manifest.json"
        checks.extend(_check_run_manifest(manifest_path, iteration_id, run_id))
        checks.extend(_check_run_manifest_limits(manifest_path, line_limit_policy))
        return checks

    if stage == "extract_results":
        if not run_id or run_id.startswith("<"):
            checks.append(Failure(f"{iteration_dir}/runs/<RUN_ID>", "state.json has missing/placeholder last_run_id"))
            return checks

        run_dir = iteration_dir / "runs" / run_id
        manifest_path = run_dir / "run_manifest.json"
        metrics_path = run_dir / "metrics.json"

        checks.extend(_check_run_manifest(manifest_path, iteration_id, run_id))
        checks.extend(_check_metrics(metrics_path))
        checks.extend(_check_size_limits(metrics_path, line_limit_policy, RUN_METRICS_POLICY_KEY))
        checks.extend(_check_run_manifest_limits(manifest_path, line_limit_policy))
        return checks

    if stage == "update_docs":
        for relative in [Path("docs_update.md"), Path("analysis/summary.md")]:
            path = iteration_dir / relative
            checks.extend(_check_file_text(path, relative=relative))
            checks.extend(_check_size_limits(path, line_limit_policy, relative.as_posix()))
        return checks

    return checks


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=None, help="Override current stage from .autolab/state.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        state = _load_state()
    except Exception as exc:
        print(f"template_fill: ERROR {exc}")
        return 1

    try:
        line_limit_policy = _load_line_limits_policy()
    except Exception as exc:
        print(f"template_fill: ERROR {exc}")
        return 1

    stage = args.stage or str(state.get("stage", "")).strip()
    iteration_id = str(state.get("iteration_id", "")).strip()
    iteration_dir = REPO_ROOT / "experiments" / iteration_id

    if not iteration_id or iteration_id.startswith("<"):
        print("template_fill: ERROR iteration_id in state is missing or placeholder")
        return 1
    if not iteration_dir.exists():
        print(f"template_fill: ERROR iteration workspace missing at {iteration_dir}")
        return 1
    if not stage:
        print("template_fill: ERROR state stage is missing")
        return 1

    failures = _stage_checks(stage, iteration_dir, state, line_limit_policy)

    if failures:
        print(f"template_fill: FAIL stage={stage} issues={len(failures)}")
        for failure in failures:
            print(f"{failure.path}\t{failure.reason}")
        return 1

    print(f"template_fill: PASS stage={stage} iteration={iteration_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
