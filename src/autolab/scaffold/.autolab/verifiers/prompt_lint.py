#!/usr/bin/env python3
"""Lint stage prompts for token and structure contract issues."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from verifier_lib import REPO_ROOT, make_result, print_result

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
    "run_group",
    "replicate_count",
}

# Shared tokens always allowed (not stage-specific).
_SHARED_TOKENS = {"python_bin", "stage", "stage_context"}
OPTIONAL_TOKENS_BY_STAGE: dict[str, set[str]] = {
    "design": {"available_memory_gb", "experiment_id", "recommended_memory_estimate"},
    "implementation": {"review_feedback", "verifier_errors"},
    "implementation_review": {"diff_summary", "dry_run_output", "verifier_outputs"},
    "launch": {
        "launch_mode",
        "recommended_memory_estimate",
        "run_group",
        "replicate_count",
    },
    "extract_results": {"run_group", "replicate_count"},
    "update_docs": {"paper_targets", "metrics_summary", "target_comparison"},
    "decide_repeat": {
        "metrics_summary",
        "target_comparison",
        "decision_suggestion",
        "auto_metrics_evidence",
    },
}


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
            tokens.update(
                {
                    "review_feedback",
                    "verifier_errors",
                    "verifier_outputs",
                    "dry_run_output",
                    "metrics_summary",
                    "target_comparison",
                    "decision_suggestion",
                    "auto_metrics_evidence",
                    "diff_summary",
                    "experiment_id",
                    "paper_targets",
                    "launch_mode",
                    "recommended_memory_estimate",
                    "available_memory_gb",
                    "hypothesis_id",
                    "run_group",
                    "replicate_count",
                }
            )
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
    "slurm_monitor": {"iteration_id", "iteration_path", "run_id"},
    "extract_results": {"iteration_id", "iteration_path", "run_id"},
    "update_docs": {"iteration_id", "iteration_path", "run_id"},
    "decide_repeat": {"iteration_id", "iteration_path", "run_id"},
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
        stage = prompt_path.stem[len("stage_") :].strip()
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
                tokens = {
                    str(token).strip() for token in raw_tokens if str(token).strip()
                }
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

    # Map sections to shared includes that satisfy the requirement
    _section_shared_equivalents: dict[str, str] = {
        "## FAILURE / RETRY BEHAVIOR": "{{shared:failure_retry.md}}",
    }
    for section in REQUIRED_SECTIONS:
        if section.lower() not in lowered:
            # Check if an equivalent shared include is present
            equivalent = _section_shared_equivalents.get(section, "")
            if equivalent and equivalent in text:
                continue
            failures.append(
                f"{prompt_path} missing required section heading: {section}"
            )

    for literal in LEGACY_LITERAL_TOKENS:
        if literal in text:
            failures.append(
                f"{prompt_path} contains unresolved legacy literal token: {literal}"
            )

    if "## file checklist" in lowered and "{{shared:checklist.md}}" not in text:
        failures.append(
            f"{prompt_path} checklist section must include {{shared:checklist.md}}"
        )

    required_tokens = required_tokens_by_stage.get(stage, set())
    if "run_id" in required_tokens and "{{run_id}}" not in text:
        failures.append(
            f"{prompt_path} must reference {{run_id}} for run-scoped stages"
        )

    tokens_in_prompt = {
        match.group(1).strip() for match in TOKEN_PATTERN.finditer(text)
    }
    unsupported_tokens = sorted(
        token for token in tokens_in_prompt if token not in ALLOWED_TOKENS
    )
    if unsupported_tokens:
        failures.append(
            f"{prompt_path} has unsupported token(s): {', '.join(unsupported_tokens)}"
        )

    missing_required_tokens = sorted(
        token for token in required_tokens if token not in tokens_in_prompt
    )
    if missing_required_tokens:
        failures.append(
            f"{prompt_path} missing required token(s) for stage '{stage}': {', '.join(missing_required_tokens)}"
        )

    stage_optional_tokens = OPTIONAL_TOKENS_BY_STAGE.get(stage, set())
    optional_tokens_used = sorted(
        token
        for token in tokens_in_prompt
        if token not in required_tokens and token not in _SHARED_TOKENS
    )
    undocumented_optional_tokens = [
        token for token in optional_tokens_used if token not in stage_optional_tokens
    ]
    if undocumented_optional_tokens:
        failures.append(
            f"{prompt_path} uses token(s) not declared as required or optional for stage '{stage}': {', '.join(undocumented_optional_tokens)}"
        )
    if optional_tokens_used and "## missing-input fallbacks" not in lowered:
        failures.append(
            f"{prompt_path} uses optional token(s) but is missing '## MISSING-INPUT FALLBACKS' safe-fallback section"
        )
    return failures


ASSISTANT_PROMPTS_DIR = PROMPTS_DIR
ASSISTANT_REQUIRED_SECTIONS = (
    "## ROLE",
    "## PRIMARY OBJECTIVE",
)
ASSISTANT_REQUIRED_SHARED_INCLUDES = (
    "{{shared:guardrails.md}}",
    "{{shared:repo_scope.md}}",
    "{{shared:runtime_context.md}}",
)


def _lint_assistant_prompt(prompt_path: Path) -> list[str]:
    """Lint an assistant-mode prompt for required sections and shared includes."""
    if not prompt_path.exists():
        return [f"{prompt_path} is missing"]
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except Exception as exc:
        return [f"{prompt_path} could not be read: {exc}"]

    failures: list[str] = []
    lowered = text.lower()

    for section in ASSISTANT_REQUIRED_SECTIONS:
        if section.lower() not in lowered:
            failures.append(
                f"{prompt_path} missing required section heading: {section}"
            )

    for include in ASSISTANT_REQUIRED_SHARED_INCLUDES:
        if include not in text:
            failures.append(f"{prompt_path} missing required shared include: {include}")

    tokens_in_prompt = {
        match.group(1).strip() for match in TOKEN_PATTERN.finditer(text)
    }
    unsupported_tokens = sorted(
        token for token in tokens_in_prompt if token not in ALLOWED_TOKENS
    )
    if unsupported_tokens:
        failures.append(
            f"{prompt_path} has unsupported token(s): {', '.join(unsupported_tokens)}"
        )

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage", default=None, help="Stage to lint (default: all stage prompts)"
    )
    parser.add_argument(
        "--assistant",
        action="store_true",
        default=False,
        help="Lint assistant prompts instead of stage prompts",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output machine-readable JSON envelope",
    )
    args = parser.parse_args()

    stage_label = args.stage or ""
    failures: list[str] = []

    if args.assistant:
        # Lint assistant-mode prompts
        assistant_files = sorted(ASSISTANT_PROMPTS_DIR.glob("assistant_*.md"))
        if not assistant_files:
            result = make_result(
                "prompt_lint", "assistant", [], ["no assistant prompts found"]
            )
            print_result(result, as_json=args.json)
            return 1
        for prompt_path in assistant_files:
            failures.extend(_lint_assistant_prompt(prompt_path))
    else:
        stage_prompt_files = _resolve_stage_prompt_files()
        required_tokens_by_stage = _resolve_required_tokens_by_stage()

        stages: list[str]
        if args.stage:
            requested = str(args.stage).strip()
            if requested not in stage_prompt_files:
                result = make_result(
                    "prompt_lint", requested, [], [f"unsupported stage '{requested}'"]
                )
                print_result(result, as_json=args.json)
                return 1
            stages = [requested]
        else:
            stages = list(stage_prompt_files.keys())

        for stage in stages:
            prompt_path = PROMPTS_DIR / stage_prompt_files[stage]
            failures.extend(
                _lint_stage_prompt(
                    stage,
                    prompt_path,
                    required_tokens_by_stage=required_tokens_by_stage,
                )
            )

    checks = [{"name": f, "status": "fail", "detail": f} for f in failures]
    if not failures:
        checks = [
            {
                "name": "prompt_lint",
                "status": "pass",
                "detail": "all prompt checks passed",
            }
        ]
    result = make_result(
        "prompt_lint",
        stage_label or ("assistant" if args.assistant else ""),
        checks,
        failures,
    )
    print_result(result, as_json=args.json)

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
