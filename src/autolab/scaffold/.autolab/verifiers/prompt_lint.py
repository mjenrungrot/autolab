#!/usr/bin/env python3
"""Lint stage prompts for token and structure contract issues."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from verifier_lib import REPO_ROOT
PROMPTS_DIR = REPO_ROOT / ".autolab" / "prompts"
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
TERMINAL_STAGES = {"human_review", "stop"}
_FALLBACK_ALLOWED_TOKENS = {
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
    "auto_metrics_evidence",
    "diff_summary",
}

# Shared tokens always allowed (not stage-specific).
_SHARED_TOKENS = {"python_bin", "stage", "stage_context"}


def _resolve_allowed_tokens() -> set[str]:
    """Build ALLOWED_TOKENS dynamically from workflow.yaml required_tokens."""
    try:
        from autolab.registry import load_registry
        registry = load_registry(REPO_ROOT)
        if registry:
            tokens: set[str] = set(_SHARED_TOKENS)
            for spec in registry.values():
                tokens.update(spec.required_tokens)
            # Also include known runtime-injected tokens not in registry.
            tokens.update({
                "review_feedback", "verifier_errors", "verifier_outputs",
                "dry_run_output", "metrics_summary", "target_comparison",
                "decision_suggestion", "auto_metrics_evidence", "diff_summary",
                "experiment_id", "paper_targets", "launch_mode",
                "recommended_memory_estimate", "available_memory_gb",
                "hypothesis_id",
            })
            return tokens
    except Exception:
        pass
    return set(_FALLBACK_ALLOWED_TOKENS)


ALLOWED_TOKENS = _resolve_allowed_tokens()
DEFAULT_REQUIRED_TOKENS_BY_STAGE: dict[str, set[str]] = {
    "hypothesis": {"iteration_id", "iteration_path", "hypothesis_id"},
    "design": {"iteration_id", "iteration_path", "hypothesis_id"},
    "implementation": {"iteration_id", "iteration_path"},
    "implementation_review": {"iteration_id", "iteration_path"},
    "launch": {"iteration_id", "iteration_path", "run_id"},
    "extract_results": {"iteration_id", "iteration_path", "run_id"},
    "update_docs": {"iteration_id", "iteration_path", "run_id"},
    "decide_repeat": {"iteration_id", "iteration_path"},
}


def _resolve_stage_prompt_files() -> dict[str, str]:
    try:
        from autolab.registry import load_registry, registry_prompt_files

        registry = load_registry(REPO_ROOT)
        mapping = registry_prompt_files(registry) if registry else {}
        mapping = {
            str(stage).strip(): str(filename).strip()
            for stage, filename in dict(mapping).items()
            if str(stage).strip() and str(filename).strip()
        }
        if mapping:
            return mapping
    except Exception:
        pass

    discovered: dict[str, str] = {}
    for prompt_path in sorted(PROMPTS_DIR.glob("stage_*.md")):
        stage = prompt_path.stem[len("stage_"):].strip()
        if stage:
            discovered[stage] = prompt_path.name
    return discovered


def _resolve_required_tokens_by_stage() -> dict[str, set[str]]:
    try:
        from autolab.registry import load_registry, registry_required_tokens

        registry = load_registry(REPO_ROOT)
        required_tokens = registry_required_tokens(registry) if registry else {}
        resolved = {}
        for stage, raw_tokens in dict(required_tokens).items():
            stage_name = str(stage).strip()
            if not stage_name:
                continue
            if isinstance(raw_tokens, (set, list, tuple)):
                tokens = {str(token).strip() for token in raw_tokens if str(token).strip()}
                resolved[stage_name] = tokens
        if resolved:
            return resolved
    except Exception:
        pass
    return dict(DEFAULT_REQUIRED_TOKENS_BY_STAGE)


def _lint_stage_prompt(
    stage: str,
    prompt_path: Path,
    *,
    required_tokens_by_stage: dict[str, set[str]],
) -> list[str]:
    if not prompt_path.exists():
        return [f"{prompt_path} is missing"]
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except Exception as exc:
        return [f"{prompt_path} could not be read: {exc}"]

    failures: list[str] = []
    lowered = text.lower()

    if stage in TERMINAL_STAGES:
        return failures

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

    required_tokens = required_tokens_by_stage.get(stage, set())
    if "run_id" in required_tokens and "{{run_id}}" not in text:
        failures.append(f"{prompt_path} must reference {{run_id}} for run-scoped stages")

    tokens_in_prompt = {match.group(1).strip() for match in TOKEN_PATTERN.finditer(text)}
    unsupported_tokens = sorted(token for token in tokens_in_prompt if token not in ALLOWED_TOKENS)
    if unsupported_tokens:
        failures.append(f"{prompt_path} has unsupported token(s): {', '.join(unsupported_tokens)}")

    missing_required_tokens = sorted(token for token in required_tokens if token not in tokens_in_prompt)
    if missing_required_tokens:
        failures.append(
            f"{prompt_path} missing required token(s) for stage '{stage}': {', '.join(missing_required_tokens)}"
        )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=None, help="Stage to lint (default: all stage prompts)")
    parser.add_argument("--json", action="store_true", default=False, help="Output machine-readable JSON envelope")
    args = parser.parse_args()
    stage_prompt_files = _resolve_stage_prompt_files()
    required_tokens_by_stage = _resolve_required_tokens_by_stage()

    stage_label = args.stage or ""

    stages: list[str]
    if args.stage:
        requested = str(args.stage).strip()
        if requested not in stage_prompt_files:
            if args.json:
                envelope = {"status": "fail", "verifier": "prompt_lint", "stage": requested, "checks": [], "errors": [f"unsupported stage '{requested}'"]}
                print(json.dumps(envelope))
            else:
                print(f"prompt_lint: ERROR unsupported stage '{requested}'")
            return 1
        stages = [requested]
    else:
        stages = list(stage_prompt_files.keys())

    failures: list[str] = []
    for stage in stages:
        prompt_path = PROMPTS_DIR / stage_prompt_files[stage]
        failures.extend(
            _lint_stage_prompt(
                stage,
                prompt_path,
                required_tokens_by_stage=required_tokens_by_stage,
            )
        )

    passed = not failures

    if args.json:
        checks = [{"name": f, "status": "fail", "detail": f} for f in failures]
        if passed:
            checks = [{"name": "prompt_lint", "status": "pass", "detail": "all prompt checks passed"}]
        envelope = {
            "status": "pass" if passed else "fail",
            "verifier": "prompt_lint",
            "stage": stage_label,
            "checks": checks,
            "errors": failures,
        }
        print(json.dumps(envelope))
    else:
        if failures:
            print("prompt_lint: FAIL")
            for failure in failures:
                print(failure)
        else:
            print("prompt_lint: PASS")

    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
