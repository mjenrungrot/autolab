#!/usr/bin/env python3
"""Verifier for template-completion and line-budget artifacts in the active iteration workspace."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

try:
    import yaml
except Exception:
    yaml = None


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / ".autolab" / "state.json"
TEMPLATE_ROOT = REPO_ROOT / "experiments" / "<ITERATION_ID>"
LINE_LIMITS_POLICY_FILE = REPO_ROOT / ".autolab" / "experiment_file_line_limits.yaml"
RUN_METRICS_POLICY_KEY = "runs/<RUN_ID>/metrics.json"
RUN_MANIFEST_POLICY_KEY = "runs/<RUN_ID>/run_manifest.json"
PLACEHOLDER_TOKENS = {
    "<iteration_id>",
    "<run_id>",
    "placeholder",
    "template placeholder",
    "placeholder run script",
    "todo:",
}

COMMENTED_SCRIPT_LINES = ("#!", "#", "set -e", "set -u", "set -uo", "set -o")


@dataclass
class Failure:
    path: str
    reason: str


@dataclass(frozen=True)
class RunManifestDynamicLimit:
    min_lines: int
    max_lines: int
    base_lines: int
    per_k_result_lines: int


@dataclass(frozen=True)
class LineLimitPolicy:
    fixed_max_lines: dict[str, int]
    run_manifest_dynamic: RunManifestDynamicLimit


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _coerce_positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{field_name} must be an integer greater than zero")
    try:
        parsed = int(str(value).strip())
    except Exception as exc:
        raise RuntimeError(f"{field_name} must be an integer greater than zero") from exc
    if parsed <= 0:
        raise RuntimeError(f"{field_name} must be an integer greater than zero")
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
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return data


def _load_line_limits_policy(path: Path = LINE_LIMITS_POLICY_FILE) -> LineLimitPolicy:
    if yaml is None:
        raise RuntimeError("PyYAML is not available; cannot parse line-limit policy")
    if not path.exists():
        raise RuntimeError(f"line-limit policy missing at {path}")

    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"line-limit policy at {path} must contain a mapping")

    line_limits = loaded.get("line_limits")
    if not isinstance(line_limits, dict):
        raise RuntimeError(f"{path} must contain 'line_limits' mapping")

    fixed_raw = line_limits.get("fixed_max_lines")
    if not isinstance(fixed_raw, dict):
        raise RuntimeError(f"{path} must contain 'line_limits.fixed_max_lines' mapping")
    fixed_max_lines: dict[str, int] = {}
    for raw_key, raw_value in fixed_raw.items():
        key = str(raw_key).strip()
        if not key:
            raise RuntimeError(f"{path} contains an empty fixed_max_lines key")
        fixed_max_lines[key] = _coerce_positive_int(
            raw_value,
            field_name=f"line_limits.fixed_max_lines[{key}]",
        )

    dynamic_raw = line_limits.get("run_manifest_dynamic")
    if not isinstance(dynamic_raw, dict):
        raise RuntimeError(f"{path} must contain 'line_limits.run_manifest_dynamic' mapping")

    target = str(dynamic_raw.get("target", RUN_MANIFEST_POLICY_KEY)).strip()
    if target and target != RUN_MANIFEST_POLICY_KEY:
        raise RuntimeError(
            f"line_limits.run_manifest_dynamic.target must be '{RUN_MANIFEST_POLICY_KEY}', got '{target}'"
        )

    dynamic = RunManifestDynamicLimit(
        min_lines=_coerce_positive_int(
            dynamic_raw.get("min_lines"),
            field_name="line_limits.run_manifest_dynamic.min_lines",
        ),
        max_lines=_coerce_positive_int(
            dynamic_raw.get("max_lines"),
            field_name="line_limits.run_manifest_dynamic.max_lines",
        ),
        base_lines=_coerce_positive_int(
            dynamic_raw.get("base_lines"),
            field_name="line_limits.run_manifest_dynamic.base_lines",
        ),
        per_k_result_lines=_coerce_positive_int(
            dynamic_raw.get("per_k_result_lines"),
            field_name="line_limits.run_manifest_dynamic.per_k_result_lines",
        ),
    )
    if dynamic.min_lines > dynamic.max_lines:
        raise RuntimeError(
            "line_limits.run_manifest_dynamic.min_lines must be <= "
            "line_limits.run_manifest_dynamic.max_lines"
        )

    return LineLimitPolicy(
        fixed_max_lines=fixed_max_lines,
        run_manifest_dynamic=dynamic,
    )


def _contains_placeholder(text: str, template_text: str = "") -> Optional[str]:
    normalized = normalize_text(text)
    if not normalized:
        return "content is empty"
    for token in PLACEHOLDER_TOKENS:
        token_lc = token.lower()
        if token_lc.startswith("<") or " " in token_lc:
            if token_lc in normalized:
                return f"contains placeholder token {token}"
            continue
        pattern = rf"(?<![a-z0-9_]){re.escape(token_lc)}(?![a-z0-9_])"
        if re.search(pattern, normalized):
            return f"contains placeholder token {token}"

    if template_text:
        template_norm = normalize_text(template_text)
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
    if path.suffix in {".sh", ".bash", ".zsh"}:
        if not _has_meaningful_script_line(text):
            return "script has no meaningful commands"
        return None
    meaningful_lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if not meaningful_lines:
        return "content has only comments/boilerplate"
    return None


def _check_max_lines(path: Path, *, max_lines: int) -> Optional[Failure]:
    if not path.exists():
        return None
    line_count = _count_lines(_read_text(path))
    if line_count <= max_lines:
        return None
    over_by = line_count - max_lines
    return Failure(str(path), f"line count {line_count} exceeds max {max_lines} (over by {over_by})")


def _check_fixed_line_limit(
    path: Path,
    policy: LineLimitPolicy,
    policy_key: str,
) -> Optional[Failure]:
    max_lines = policy.fixed_max_lines.get(policy_key)
    if max_lines is None:
        return None
    return _check_max_lines(path, max_lines=max_lines)


def _count_manifest_k_results(payload: dict) -> int:
    videos = payload.get("videos")
    if not isinstance(videos, list):
        return 0
    total = 0
    for entry in videos:
        if not isinstance(entry, dict):
            continue
        k_results = entry.get("k_results")
        if isinstance(k_results, list):
            total += len(k_results)
    return total


def _run_manifest_dynamic_limit(policy: LineLimitPolicy, k_results_count: int) -> int:
    count = max(0, int(k_results_count))
    dynamic = policy.run_manifest_dynamic
    candidate = dynamic.base_lines + dynamic.per_k_result_lines * count
    return max(dynamic.min_lines, min(dynamic.max_lines, candidate))


def _check_run_manifest_line_limit(path: Path, policy: LineLimitPolicy) -> Optional[Failure]:
    if not path.exists():
        return None
    try:
        payload = _load_json(path)
    except Exception as exc:
        return Failure(str(path), f"could not evaluate dynamic line limit: {exc}")

    k_results_count = _count_manifest_k_results(payload)
    max_lines = _run_manifest_dynamic_limit(policy, k_results_count)
    line_count = _count_lines(_read_text(path))
    if line_count <= max_lines:
        return None

    over_by = line_count - max_lines
    return Failure(
        str(path),
        (
            f"line count {line_count} exceeds dynamic max {max_lines} "
            f"for k_results_count={k_results_count} (over by {over_by})"
        ),
    )


def _check_file_text(path: Path, *, relative: Path) -> Optional[Failure]:
    if not path.exists():
        return Failure(str(path), "required file is missing")
    text = _read_text(path)
    template_text = _template_text(relative)

    reason = _contains_placeholder(text, template_text)
    if reason:
        return Failure(str(path), reason)

    reason = _is_trivial_text_file(text, path)
    if reason:
        return Failure(str(path), reason)

    return None


def _check_review_result(path: Path) -> Optional[Failure]:
    if not path.exists():
        return Failure(str(path), "required file is missing")
    text = _read_text(path)
    template_text = _template_text(Path("review_result.json"))
    reason = _contains_placeholder(text, template_text)
    if reason:
        return Failure(str(path), reason)

    data = _load_json(path)
    required = {"status", "blocking_findings", "required_checks", "reviewed_at"}
    missing = required - data.keys()
    if missing:
        return Failure(str(path), f"missing required keys: {sorted(missing)}")

    status = data.get("status")
    if status not in {"pass", "needs_retry", "failed"}:
        return Failure(str(path), f"invalid status '{status}'")

    blocking_findings = data.get("blocking_findings")
    if not isinstance(blocking_findings, list):
        return Failure(str(path), "'blocking_findings' must be a list")

    required_checks = data.get("required_checks")
    if not isinstance(required_checks, dict):
        return Failure(str(path), "'required_checks' must be a mapping")

    return None


def _check_design(path: Path, iteration_id: str) -> Optional[Failure]:
    if not path.exists():
        return Failure(str(path), "required file is missing")
    text = _read_text(path)
    template_text = _template_text(Path("design.yaml"))
    reason = _contains_placeholder(text, template_text)
    if reason:
        return Failure(str(path), reason)

    data = _load_yaml(path)
    required = {"id", "iteration_id", "hypothesis_id", "entrypoint", "compute", "metrics", "baselines"}
    missing = required - data.keys()
    if missing:
        return Failure(str(path), f"missing required keys: {sorted(missing)}")

    if data["iteration_id"] != iteration_id:
        return Failure(str(path), "iteration_id does not match .autolab/state.json")

    entrypoint = data["entrypoint"]
    if not isinstance(entrypoint, dict) or "module" not in entrypoint:
        return Failure(str(path), "entrypoint.module is required")

    compute = data["compute"]
    if not isinstance(compute, dict) or "location" not in compute:
        return Failure(str(path), "compute.location is required")

    baselines = data.get("baselines")
    if not isinstance(baselines, list) or not baselines:
        return Failure(str(path), "baselines must be a non-empty list")

    return None


def _check_run_manifest(path: Path, iteration_id: str, run_id: str) -> Optional[Failure]:
    if not path.exists():
        return Failure(str(path), "required file is missing")
    text = _read_text(path)
    template_text = _template_text(Path("runs") / "<RUN_ID>" / "run_manifest.json")
    reason = _contains_placeholder(text, template_text)
    if reason:
        return Failure(str(path), reason)

    data = _load_json(path)
    if data.get("iteration_id") and data.get("iteration_id") != iteration_id:
        return Failure(str(path), "iteration_id does not match .autolab/state.json")
    if data.get("run_id") and data.get("run_id") != run_id:
        return Failure(str(path), "run_id does not match .autolab/state.json.last_run_id")

    return None


def _check_metrics(path: Path) -> Optional[Failure]:
    if not path.exists():
        return Failure(str(path), "required file is missing")
    text = _read_text(path)
    reason = _contains_placeholder(text, _template_text(Path("runs") / "<RUN_ID>" / "metrics.json"))
    if reason:
        return Failure(str(path), reason)

    data = _load_json(path)
    if not data:
        return Failure(str(path), "metrics content is empty")
    if data == {"primary_metric": None, "status": "pending"}:
        return Failure(str(path), "metrics is placeholder-like")
    return None


def _stage_checks(
    stage: str,
    iteration_dir: Path,
    state: dict,
    line_limit_policy: LineLimitPolicy,
) -> List[Failure]:
    checks: List[Failure] = []
    run_id = str(state.get("last_run_id", "")).strip()

    if stage == "hypothesis":
        relative = Path("hypothesis.md")
        path = iteration_dir / relative
        failure = _check_file_text(path, relative=relative)
        if failure:
            checks.append(failure)
        failure = _check_fixed_line_limit(path, line_limit_policy, relative.as_posix())
        if failure:
            checks.append(failure)
        return checks

    if stage == "design":
        path = iteration_dir / "design.yaml"
        failure = _check_design(path, str(state.get("iteration_id", "")))
        if failure:
            checks.append(failure)
        failure = _check_fixed_line_limit(path, line_limit_policy, "design.yaml")
        if failure:
            checks.append(failure)
        return checks

    if stage == "implementation":
        relative = Path("implementation_plan.md")
        path = iteration_dir / relative
        failure = _check_file_text(path, relative=relative)
        if failure:
            checks.append(failure)
        failure = _check_fixed_line_limit(path, line_limit_policy, relative.as_posix())
        if failure:
            checks.append(failure)
        return checks

    if stage == "implementation_review":
        for relative in [Path("implementation_review.md"), Path("review_result.json")]:
            path = iteration_dir / relative
            if relative.suffix == ".json":
                failure = _check_review_result(path)
            else:
                failure = _check_file_text(path, relative=relative)
            if failure:
                checks.append(failure)
            line_failure = _check_fixed_line_limit(path, line_limit_policy, relative.as_posix())
            if line_failure:
                checks.append(line_failure)
        return checks

    if stage == "launch":
        for relative in [Path("launch/run_local.sh"), Path("launch/run_slurm.sbatch")]:
            path = iteration_dir / relative
            failure = _check_file_text(path, relative=relative)
            if failure:
                checks.append(failure)
            line_failure = _check_fixed_line_limit(path, line_limit_policy, relative.as_posix())
            if line_failure:
                checks.append(line_failure)
        return checks

    if stage == "extract_results":
        if not run_id or run_id.startswith("<"):
            checks.append(Failure(f"{iteration_dir}/runs/<RUN_ID>", "state.json has missing/placeholder last_run_id"))
            return checks

        run_dir = iteration_dir / "runs" / run_id
        manifest_path = run_dir / "run_manifest.json"
        metrics_path = run_dir / "metrics.json"

        failure = _check_run_manifest(manifest_path, str(state.get("iteration_id", "")), run_id)
        if failure:
            checks.append(failure)
        failure = _check_metrics(metrics_path)
        if failure:
            checks.append(failure)

        failure = _check_run_manifest_line_limit(manifest_path, line_limit_policy)
        if failure:
            checks.append(failure)
        failure = _check_fixed_line_limit(metrics_path, line_limit_policy, RUN_METRICS_POLICY_KEY)
        if failure:
            checks.append(failure)
        return checks

    if stage == "update_docs":
        for relative in [Path("docs_update.md"), Path("analysis/summary.md")]:
            path = iteration_dir / relative
            failure = _check_file_text(path, relative=relative)
            if failure:
                checks.append(failure)
            line_failure = _check_fixed_line_limit(path, line_limit_policy, relative.as_posix())
            if line_failure:
                checks.append(line_failure)
        return checks

    return checks


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=None, help="Override current stage from .autolab/state.json")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        state = _load_state()
    except Exception as exc:  # pragma: no cover
        print(f"template_fill: ERROR {exc}")
        return 1

    try:
        line_limit_policy = _load_line_limits_policy()
    except Exception as exc:  # pragma: no cover
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
