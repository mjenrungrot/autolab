#!/usr/bin/env python3
"""Verifier for implementation plan structure and task-block integrity."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Iterable, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / ".autolab" / "state.json"
EXPERIMENT_TYPES = ("plan", "in_progress", "done")
DEFAULT_EXPERIMENT_TYPE = "plan"

TASK_HEADING_PATTERN = re.compile(r"^###\s+(T\d+):\s*(.*)$", re.MULTILINE)
DEPENDS_ON_PATTERN = re.compile(
    r"^\s*-\s*\*\*depends_on\*\*:\s*\[([^\]]*)\]",
    re.MULTILINE,
)
STATUS_PATTERN = re.compile(
    r"^\s*-\s*\*\*status\*\*:\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)
LOCATION_PATTERN = re.compile(
    r"^\s*-\s*\*\*location\*\*:\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)
DESCRIPTION_PATTERN = re.compile(
    r"^\s*-\s*\*\*description\*\*:\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)
VALIDATION_PATTERN = re.compile(
    r"^\s*-\s*\*\*validation\*\*:\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)
TOUCHES_PATTERN = re.compile(
    r"^\s*-\s*\*\*touches\*\*:\s*\[([^\]]*)\]",
    re.MULTILINE,
)
CONFLICT_GROUP_PATTERN = re.compile(
    r"^\s*-\s*\*\*conflict_group\*\*:\s*(.+)$",
    re.MULTILINE | re.IGNORECASE,
)
WAVE_TABLE_PATTERN = re.compile(
    r"^\|\s*(\d+)\s*\|([^|]+)\|",
    re.MULTILINE,
)
CHANGE_SUMMARY_PATTERN = re.compile(
    r"^##\s+Change\s+Summary",
    re.MULTILINE | re.IGNORECASE,
)

PLACEHOLDER_PATTERNS = (
    re.compile(r"\{\{\s*[A-Za-z0-9_]+\s*\}\}"),
    re.compile(r"<[A-Za-z0-9_]+>"),
    re.compile(r"\bTODO:\b", re.IGNORECASE),
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bTBD\b", re.IGNORECASE),
    re.compile(r"\bFIXME\b", re.IGNORECASE),
)

VALID_STATUSES = {"not completed", "completed", "in progress", "blocked"}


def _load_state() -> dict:
    if not STATE_FILE.exists():
        raise RuntimeError(f"state.json missing at {STATE_FILE}")
    data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("state.json must contain a JSON object")
    return data


def _resolve_iteration_dir(iteration_id: str) -> Path:
    normalized = iteration_id.strip()
    experiments_root = REPO_ROOT / "experiments"
    for experiment_type in EXPERIMENT_TYPES:
        candidate = experiments_root / experiment_type / normalized
        if candidate.exists():
            return candidate
    return experiments_root / DEFAULT_EXPERIMENT_TYPE / normalized


def _split_task_sections(text: str) -> dict[str, str]:
    """Split markdown into per-task sections keyed by task ID."""
    headings = list(TASK_HEADING_PATTERN.finditer(text))
    sections: dict[str, str] = {}
    for i, match in enumerate(headings):
        task_id = match.group(1)
        start = match.start()
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        sections[task_id] = text[start:end]
    return sections


def _extract_field(section: str, pattern: re.Pattern[str]) -> str | None:
    match = pattern.search(section)
    if not match:
        return None
    return match.group(1).strip()


def _parse_list_field(raw: str) -> list[str]:
    """Parse comma-separated values from a bracketed field."""
    items = [item.strip().strip("'\"") for item in raw.split(",") if item.strip()]
    return items


def _detect_placeholders(text: str) -> list[str]:
    found: list[str] = []
    for pattern in PLACEHOLDER_PATTERNS:
        for match in pattern.finditer(text):
            found.append(match.group(0))
    return found


def _topological_check(deps: dict[str, list[str]]) -> list[str]:
    """Check for circular dependencies via topological sort. Return cycle description if found."""
    visited: set[str] = set()
    in_stack: set[str] = set()
    issues: list[str] = []

    def visit(node: str) -> bool:
        if node in in_stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        for dep in deps.get(node, []):
            if dep in deps and visit(dep):
                issues.append(f"circular dependency involving {node} -> {dep}")
                return True
        in_stack.discard(node)
        return False

    for task_id in deps:
        if task_id not in visited:
            visit(task_id)
    return issues


def _parse_wave_table(text: str) -> dict[int, list[str]]:
    """Parse wave table into {wave_number: [task_ids]}."""
    waves: dict[int, list[str]] = {}
    for match in WAVE_TABLE_PATTERN.finditer(text):
        wave_num = int(match.group(1))
        raw_tasks = match.group(2).strip()
        task_ids = [t.strip() for t in re.split(r"[,\s]+", raw_tasks) if re.match(r"T\d+", t.strip())]
        if task_ids:
            waves[wave_num] = task_ids
    return waves


def _parse_touches_from_section(section: str) -> list[str]:
    raw = _extract_field(section, TOUCHES_PATTERN)
    if raw is None:
        return []
    return _parse_list_field(raw)


def _paths_overlap(paths_a: list[str], paths_b: list[str]) -> bool:
    """Check if any path in a is a prefix of any path in b or vice versa."""
    for a in paths_a:
        for b in paths_b:
            if a == b or a.startswith(b + "/") or b.startswith(a + "/"):
                return True
    return False


def lint(plan_text: str) -> list[str]:
    """Lint an implementation plan. Returns list of issues."""
    issues: list[str] = []

    # Check Change Summary
    if not CHANGE_SUMMARY_PATTERN.search(plan_text):
        issues.append("missing required '## Change Summary' section")

    # Check for placeholder tokens in the overall plan
    placeholders = _detect_placeholders(plan_text)
    if placeholders:
        unique = sorted(set(placeholders))
        issues.append(f"unresolved placeholders found: {', '.join(unique[:5])}")

    # Parse task blocks
    task_sections = _split_task_sections(plan_text)

    if not task_sections:
        # Plans with no task blocks are valid (simple changes)
        return issues

    # Validate each task block
    all_task_ids = set(task_sections.keys())
    deps_map: dict[str, list[str]] = {}
    task_touches: dict[str, list[str]] = {}
    task_conflict_groups: dict[str, str] = {}

    for task_id, section in task_sections.items():
        # depends_on
        depends_raw = _extract_field(section, DEPENDS_ON_PATTERN)
        if depends_raw is None:
            issues.append(f"{task_id}: missing 'depends_on' field")
            deps_map[task_id] = []
        else:
            dep_list = _parse_list_field(depends_raw)
            deps_map[task_id] = dep_list
            for dep in dep_list:
                if dep not in all_task_ids:
                    issues.append(f"{task_id}: depends_on references unknown task '{dep}'")

        # status
        status_raw = _extract_field(section, STATUS_PATTERN)
        if status_raw is None:
            issues.append(f"{task_id}: missing 'status' field")
        elif status_raw.lower() not in VALID_STATUSES:
            issues.append(
                f"{task_id}: invalid status '{status_raw}' "
                f"(must be one of: {', '.join(sorted(VALID_STATUSES))})"
            )

        # location
        if _extract_field(section, LOCATION_PATTERN) is None:
            issues.append(f"{task_id}: missing 'location' field")

        # description
        if _extract_field(section, DESCRIPTION_PATTERN) is None:
            issues.append(f"{task_id}: missing 'description' field")

        # validation
        if _extract_field(section, VALIDATION_PATTERN) is None:
            issues.append(f"{task_id}: missing 'validation' field")

        # touches (collect for wave validation)
        task_touches[task_id] = _parse_touches_from_section(section)

        # conflict_group (optional, collect for wave validation)
        cg = _extract_field(section, CONFLICT_GROUP_PATTERN)
        if cg and cg.lower() not in ("<optional>", "none", ""):
            task_conflict_groups[task_id] = cg

    # Circular dependency check
    cycle_issues = _topological_check(deps_map)
    issues.extend(cycle_issues)

    # Wave table validation
    waves = _parse_wave_table(plan_text)
    if waves:
        defined_in_waves: set[str] = set()
        for wave_num, wave_tasks in waves.items():
            for tid in wave_tasks:
                defined_in_waves.add(tid)
                if tid not in all_task_ids:
                    issues.append(f"wave {wave_num}: references unknown task '{tid}'")

            # Check touches overlap within wave
            for i, tid_a in enumerate(wave_tasks):
                for tid_b in wave_tasks[i + 1 :]:
                    touches_a = task_touches.get(tid_a, [])
                    touches_b = task_touches.get(tid_b, [])
                    if touches_a and touches_b and _paths_overlap(touches_a, touches_b):
                        issues.append(
                            f"wave {wave_num}: tasks {tid_a} and {tid_b} have overlapping touches"
                        )

                    # Check conflict_group overlap within wave
                    cg_a = task_conflict_groups.get(tid_a)
                    cg_b = task_conflict_groups.get(tid_b)
                    if cg_a and cg_b and cg_a == cg_b:
                        issues.append(
                            f"wave {wave_num}: tasks {tid_a} and {tid_b} share conflict_group '{cg_a}'"
                        )

    return issues


def _load_allowed_dirs() -> list[str] | None:
    """Load allowed_edit_dirs from rendered context if available. Returns None if not found."""
    context_path = REPO_ROOT / ".autolab" / "prompts" / "rendered" / "implementation.context.json"
    if not context_path.exists():
        return None
    try:
        data = json.loads(context_path.read_text(encoding="utf-8"))
        runner_scope = data.get("runner_scope", {})
        allowed = runner_scope.get("allowed_dirs") or runner_scope.get("allowed_edit_dirs")
        if isinstance(allowed, list):
            return [str(d).strip() for d in allowed if str(d).strip()]
    except Exception:
        pass
    return None


def _check_scope(task_touches: dict[str, list[str]], allowed_dirs: list[str]) -> list[str]:
    """Warn (not fail) if task touches are outside allowed scope."""
    warnings: list[str] = []
    for task_id, touches in task_touches.items():
        for touch in touches:
            in_scope = any(
                touch == d or touch.startswith(d + "/") or touch.startswith(d + "\\")
                for d in allowed_dirs
            )
            if not in_scope:
                warnings.append(f"{task_id}: touches '{touch}' is outside allowed scope")
    return warnings


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=None, help="Override current stage")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        state = _load_state()
    except Exception as exc:
        print(f"implementation_plan_lint: ERROR {exc}")
        return 1

    stage = args.stage or str(state.get("stage", "")).strip()

    # Only run for implementation stage
    if stage != "implementation":
        print(f"implementation_plan_lint: SKIP stage={stage}")
        return 0

    iteration_id = str(state.get("iteration_id", "")).strip()
    if not iteration_id or iteration_id.startswith("<"):
        print("implementation_plan_lint: ERROR iteration_id is missing or placeholder")
        return 1

    iteration_dir = _resolve_iteration_dir(iteration_id)
    plan_path = iteration_dir / "implementation_plan.md"

    if not plan_path.exists():
        print(f"implementation_plan_lint: SKIP plan file not found at {plan_path}")
        return 0

    plan_text = plan_path.read_text(encoding="utf-8")
    if not plan_text.strip():
        print("implementation_plan_lint: FAIL issues=1")
        print(f"{plan_path}\tcontent is empty")
        return 1

    issues = lint(plan_text)

    if issues:
        print(f"implementation_plan_lint: FAIL issues={len(issues)}")
        for issue in issues:
            print(f"  {issue}")
        return 1

    # Optional scope validation (warnings only)
    allowed_dirs = _load_allowed_dirs()
    if allowed_dirs:
        task_sections = _split_task_sections(plan_text)
        task_touches: dict[str, list[str]] = {}
        for task_id, section in task_sections.items():
            task_touches[task_id] = _parse_touches_from_section(section)
        scope_warnings = _check_scope(task_touches, allowed_dirs)
        for warning in scope_warnings:
            print(f"  WARN: {warning}")

    print(f"implementation_plan_lint: PASS stage={stage} iteration={iteration_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
