#!/usr/bin/env python3
"""Verifier for documentation target updates."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / ".autolab" / "state.json"
PAPER_TARGETS = [REPO_ROOT / "paper" / "paperbanana.md", REPO_ROOT / "paper" / "ralph.md"]


def _load_state() -> dict:
    if not STATE_FILE.exists():
        raise RuntimeError("Missing .autolab/state.json")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise RuntimeError("state.json must contain an object")
    return state


def _placeholder_text(text: str) -> bool:
    lowered = text.lower()
    return "placeholder" in lowered


def _load_text(path: Path) -> str:
    if not path.exists():
        raise RuntimeError(f"{path} is missing")
    return path.read_text(encoding="utf-8")


def _iter_reference(iteration_id: str, run_id: str) -> bool:
    for paper in PAPER_TARGETS:
        text = _load_text(paper).lower()
        if not text:
            continue
        if iteration_id.lower() in text:
            return True
        if run_id and run_id.lower() in text:
            return True
    return False


def _validate_docs_update(docs_update_path: Path, iteration_id: str, run_id: str) -> list[str]:
    failures: list[str] = []
    text = _load_text(docs_update_path)
    lowered = text.lower()

    if _placeholder_text(text):
        failures.append(f"{docs_update_path} still contains placeholder text")
    if not lowered.strip():
        failures.append(f"{docs_update_path} is empty")

    no_change = "no changes needed" in lowered
    if no_change:
        return failures

    if not _iter_reference(iteration_id, run_id):
        failures.append("paper targets do not contain this iteration or run reference and no no-change rationale is provided")
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

    if stage != "update_docs":
        print("docs_targets: PASS")
        return 0

    iteration_dir = REPO_ROOT / "experiments" / iteration_id
    docs_update_path = iteration_dir / "docs_update.md"

    for paper in PAPER_TARGETS:
        if not paper.exists():
            print("docs_targets: FAIL")
            print(f"{paper} is missing")
            return 1

    failures: list[str] = _validate_docs_update(docs_update_path, iteration_id, run_id)
    if failures:
        print("docs_targets: FAIL")
        for reason in failures:
            print(reason)
        return 1

    print("docs_targets: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
