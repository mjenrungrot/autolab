#!/usr/bin/env python3
"""Fail when closed backlog iterations are edited in the current git diff."""

from __future__ import annotations

import subprocess
from pathlib import Path

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKLOG_FILE = REPO_ROOT / ".autolab" / "backlog.yaml"
CLOSED_STATUSES = {"done", "completed", "closed", "resolved"}
EXPERIMENT_TYPES = ("plan", "in_progress", "done")


def _load_closed_iteration_ids() -> set[str]:
    if yaml is None or not BACKLOG_FILE.exists():
        return set()
    try:
        payload = yaml.safe_load(BACKLOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return set()
    if not isinstance(payload, dict):
        return set()

    experiments = payload.get("experiments")
    if not isinstance(experiments, list):
        return set()

    closed: set[str] = set()
    for entry in experiments:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status", "")).strip().lower()
        if status not in CLOSED_STATUSES:
            continue
        iteration_id = str(entry.get("iteration_id", "")).strip()
        if iteration_id:
            closed.add(iteration_id)
    return closed


def _git_changed_paths() -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "status", "--porcelain", "--untracked-files=all"],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return []

    output: list[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path_part = line[3:].strip()
        if " -> " in path_part:
            path_part = path_part.split(" -> ", 1)[1].strip()
        if path_part:
            output.append(path_part)
    return output


def _matches_closed_iteration(path: str, iteration_id: str) -> bool:
    normalized = path.replace("\\", "/")
    for experiment_type in EXPERIMENT_TYPES:
        if normalized.startswith(f"experiments/{experiment_type}/{iteration_id}/"):
            return True
    return False


def main() -> int:
    closed_iterations = _load_closed_iteration_ids()
    if not closed_iterations:
        print("closed_experiment_guard: PASS")
        return 0

    changed_paths = _git_changed_paths()
    if not changed_paths:
        print("closed_experiment_guard: PASS")
        return 0

    violations: list[tuple[str, str]] = []
    for changed in changed_paths:
        for iteration_id in sorted(closed_iterations):
            if _matches_closed_iteration(changed, iteration_id):
                violations.append((iteration_id, changed))
                break

    if violations:
        print("closed_experiment_guard: FAIL")
        for iteration_id, changed in violations:
            print(f"closed iteration '{iteration_id}' has modified path: {changed}")
        return 1

    print("closed_experiment_guard: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
