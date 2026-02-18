#!/usr/bin/env python3
"""Verifier for documentation target updates."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
STATE_FILE = REPO_ROOT / ".autolab" / "state.json"
EXPERIMENT_TYPES = ("plan", "in_progress", "done")
DEFAULT_EXPERIMENT_TYPE = "plan"
PLACEHOLDER_PATTERNS = (
    re.compile(r"\{\{[^{}\n]+\}\}"),
    re.compile(r"<[^>\n]+>"),
    re.compile(r"\bTODO\b", re.IGNORECASE),
    re.compile(r"\bTBD\b", re.IGNORECASE),
    re.compile(r"\bFIXME\b", re.IGNORECASE),
    re.compile(r"\bplaceholder\b", re.IGNORECASE),
)


def _load_state() -> dict:
    if not STATE_FILE.exists():
        raise RuntimeError("Missing .autolab/state.json")
    state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise RuntimeError("state.json must contain an object")
    return state


def _resolve_iteration_dir(iteration_id: str) -> Path:
    normalized_iteration = iteration_id.strip()
    experiments_root = REPO_ROOT / "experiments"
    candidates = [experiments_root / experiment_type / normalized_iteration for experiment_type in EXPERIMENT_TYPES]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return experiments_root / DEFAULT_EXPERIMENT_TYPE / normalized_iteration


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
    return any(pattern.search(text) for pattern in PLACEHOLDER_PATTERNS)


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

    if not targets:
        if "no target configured" in lowered or "no targets configured" in lowered:
            return failures
        failures.append(
            "state.paper_targets is not configured; docs_update.md must include explicit"
            " 'No target configured' rationale."
        )
        return failures

    if "no changes needed" in lowered:
        return failures

    if not _iter_reference(iteration_id, run_id, targets):
        failures.append(
            "paper targets do not contain this iteration or run reference and no no-change rationale is provided"
        )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", default=False, help="Output machine-readable JSON envelope")
    args = parser.parse_args()

    try:
        state = _load_state()
    except Exception as exc:
        if args.json:
            import json as _json
            envelope = {"status": "fail", "verifier": "docs_targets", "stage": "", "checks": [], "errors": [str(exc)]}
            print(_json.dumps(envelope))
        else:
            print(f"docs_targets: ERROR {exc}")
        return 1

    stage = str(state.get("stage", "")).strip()
    iteration_id = str(state.get("iteration_id", "")).strip()
    run_id = str(state.get("last_run_id", "")).strip()
    targets = _iter_targets(state.get("paper_targets"))

    if stage != "update_docs":
        if args.json:
            import json as _json
            envelope = {"status": "pass", "verifier": "docs_targets", "stage": stage, "checks": [{"name": "docs_targets", "status": "pass", "detail": f"skipped for stage={stage}"}], "errors": []}
            print(_json.dumps(envelope))
        else:
            print("docs_targets: PASS")
        return 0

    iteration_dir = _resolve_iteration_dir(iteration_id)
    docs_update_path = iteration_dir / "docs_update.md"

    failures: list[str] = []
    for path in targets:
        if not path.exists():
            failures.append(f"{path} is missing")

    if not failures:
        failures = _validate_docs_update(
            docs_update_path,
            iteration_id=iteration_id,
            run_id=run_id,
            targets=targets,
        )

    passed = not failures

    if args.json:
        import json as _json
        checks = [{"name": f, "status": "fail", "detail": f} for f in failures]
        if passed:
            checks = [{"name": "docs_targets", "status": "pass", "detail": "all docs target checks passed"}]
        envelope = {
            "status": "pass" if passed else "fail",
            "verifier": "docs_targets",
            "stage": stage,
            "checks": checks,
            "errors": failures,
        }
        print(_json.dumps(envelope))
    else:
        if failures:
            print("docs_targets: FAIL")
            for reason in failures:
                print(reason)
        else:
            print("docs_targets: PASS")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
