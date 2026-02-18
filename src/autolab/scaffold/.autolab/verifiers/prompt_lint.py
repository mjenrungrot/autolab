#!/usr/bin/env python3
"""Lint stage prompts for token and structure contract issues."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / ".autolab" / "prompts"
STAGE_PROMPT_FILES = {
    "hypothesis": "stage_hypothesis.md",
    "design": "stage_design.md",
    "implementation": "stage_implementation.md",
    "implementation_review": "stage_implementation_review.md",
    "launch": "stage_launch.md",
    "extract_results": "stage_extract_results.md",
    "update_docs": "stage_update_docs.md",
    "decide_repeat": "stage_decide_repeat.md",
}
TOKEN_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
LEGACY_LITERAL_TOKENS = ("<ITERATION_ID>", "<ITERATION_PATH>", "<RUN_ID>")
REQUIRED_SECTIONS = (
    "## ROLE",
    "## PRIMARY OBJECTIVE",
    "## OUTPUTS (STRICT)",
    "## REQUIRED INPUTS",
    "## FILE CHECKLIST",
    "## FAILURE / RETRY BEHAVIOR",
)
REQUIRED_SHARED_INCLUDES = (
    "{{shared:guardrails.md}}",
    "{{shared:repo_scope.md}}",
    "{{shared:runtime_context.md}}",
)
ALLOWED_TOKENS = {
    "iteration_id",
    "iteration_path",
    "experiment_id",
    "paper_targets",
    "python_bin",
    "recommended_memory_estimate",
    "available_memory_gb",
    "stage",
    "stage_context",
    "run_id",
    "hypothesis_id",
    "review_feedback",
    "verifier_errors",
    "verifier_outputs",
    "dry_run_output",
    "launch_mode",
    "metrics_summary",
    "target_comparison",
    "decision_suggestion",
    "diff_summary",
}


def _lint_stage_prompt(stage: str, prompt_path: Path) -> list[str]:
    if not prompt_path.exists():
        return [f"{prompt_path} is missing"]
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except Exception as exc:
        return [f"{prompt_path} could not be read: {exc}"]

    failures: list[str] = []
    lowered = text.lower()

    for include in REQUIRED_SHARED_INCLUDES:
        if include not in text:
            failures.append(f"{prompt_path} missing required shared include: {include}")

    if "{{stage_context}}" not in text and "{{shared:runtime_context.md}}" not in text:
        failures.append(
            f"{prompt_path} must include runtime stage context via {{stage_context}} or shared runtime_context include"
        )

    for section in REQUIRED_SECTIONS:
        if section.lower() not in lowered:
            failures.append(f"{prompt_path} missing required section heading: {section}")

    for literal in LEGACY_LITERAL_TOKENS:
        if literal in text:
            failures.append(f"{prompt_path} contains unresolved legacy literal token: {literal}")

    if "## file checklist" in lowered and "{{shared:checklist.md}}" not in text:
        failures.append(f"{prompt_path} checklist section must include {{shared:checklist.md}}")

    if stage in {"launch", "extract_results", "update_docs", "decide_repeat"} and "{{run_id}}" not in text:
        failures.append(f"{prompt_path} must reference {{run_id}} for run-scoped stages")

    unsupported_tokens = sorted(
        token for token in {match.group(1).strip() for match in TOKEN_PATTERN.finditer(text)} if token not in ALLOWED_TOKENS
    )
    if unsupported_tokens:
        failures.append(f"{prompt_path} has unsupported token(s): {', '.join(unsupported_tokens)}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=None, help="Stage to lint (default: all stage prompts)")
    args = parser.parse_args()

    stages: list[str]
    if args.stage:
        requested = str(args.stage).strip()
        if requested not in STAGE_PROMPT_FILES:
            print(f"prompt_lint: ERROR unsupported stage '{requested}'")
            return 1
        stages = [requested]
    else:
        stages = list(STAGE_PROMPT_FILES.keys())

    failures: list[str] = []
    for stage in stages:
        prompt_path = PROMPTS_DIR / STAGE_PROMPT_FILES[stage]
        failures.extend(_lint_stage_prompt(stage, prompt_path))

    if failures:
        print("prompt_lint: FAIL")
        for failure in failures:
            print(failure)
        return 1

    print("prompt_lint: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
