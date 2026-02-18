#!/usr/bin/env python3
"""Verifier for documentation target updates."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / ".autolab" / "state.json"


def _load_state() -> dict:
    if not STATE_FILE.exists():
        raise RuntimeError("Missing .autolab/state.json")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise RuntimeError("state.json must contain an object")
    return state


def _iter_targets(paper_targets: object) -> list[Path]:
    if isinstance(paper_targets, str):
        value = paper_targets.strip()
        return [REPO_ROOT / value] if value else []
    if isinstance(paper_targets, list):
        paths: list[Path] = []
        for item in paper_targets:
            if isinstance(item, str):
                value = item.strip()
                if value:
                    paths.append(REPO_ROOT / value)
        return paths
    return []


def _load_text(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"{path} is missing")
    return path.read_text(encoding="utf-8")


def _contains_placeholder_text(text: str) -> bool:
    return "placeholder" in text.lower()


def _iter_reference(iteration_id: str, run_id: str, targets: list[Path]) -> bool:
    for path in targets:
        try:
            text = _load_text(path).lower()
        except Exception:
            continue
        if iteration_id.lower() in text:
            return True
        if run_id and run_id.lower() in text:
            return True
    return False


def _validate_docs_update(
    docs_update_path: Path,
    *,
    iteration_id: str,
    run_id: str,
    targets: list[Path],
) -> list[str]:
    failures: list[str] = []
    text = _load_text(docs_update_path)
    lowered = text.lower()

    if _contains_placeholder_text(text):
        failures.append(f"{docs_update_path} contains placeholder text")
    if not lowered.strip():
        failures.append(f"{docs_update_path} is empty")

    if "no changes needed" in lowered:
        return failures

    if not targets:
        failures.append(
            "state.paper_targets is not configured; if no doc updates are needed, set docs_update.md"
            " to include 'No changes needed'."
        )
        return failures

    if not _iter_reference(iteration_id, run_id, targets):
        failures.append(
            "paper targets do not contain this iteration or run reference and no no-change rationale is provided"
        )
    return failures


def main() -> int:
    try:
        state = _load_state()
    except Exception as exc:
        print(f"docs_targets: ERROR {exc}")
        return 1

    stage = str(state.get("stage", "")).strip()
    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    targets = _iter_targets(state.get("paper_targets"))

    if stage != "update_docs":
        print("docs_targets: PASS")
        return 0

    iteration_dir = REPO_ROOT / "experiments" / iteration_id
    docs_update_path = iteration_dir / "docs_update.md"

    if not targets:
        targets = [
            REPO_ROOT / "paper" / "paperbanana.md",
            REPO_ROOT / "paper" / "ralph.md",
        ]

    for path in targets:
        if not path.exists():
            print("docs_targets: FAIL")
            print(f"{path} is missing")
            return 1

    failures = _validate_docs_update(
        docs_update_path,
        iteration_id=iteration_id,
        run_id=run_id,
        targets=targets,
    )
    if failures:
        print("docs_targets: FAIL")
        for reason in failures:
            print(reason)
        return 1

    print("docs_targets: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
