#!/usr/bin/env python3
"""Lint stage prompts for token and structure contract issues."""

from __future__ import annotations

import pathlib
import sys

_VERIFIER_DIR = pathlib.Path(__file__).resolve().parent
if str(_VERIFIER_DIR) not in sys.path:
    sys.path.insert(0, str(_VERIFIER_DIR))

import argparse
import re
from pathlib import Path

from verifier_lib import REPO_ROOT, make_result, print_result

PROMPTS_DIR = REPO_ROOT / ".autolab" / "prompts"
SHARED_PROMPTS_DIR = PROMPTS_DIR / "shared"
TOKEN_PATTERN = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")
SHARED_INCLUDE_PATTERN = re.compile(
    r"\{\{\s*shared\s*:\s*([A-Za-z0-9][A-Za-z0-9_.\-/]*)\s*\}\}"
)
LEGACY_LITERAL_TOKENS = ("<ITERATION_ID>", "<ITERATION_PATH>", "<RUN_ID>")
RUNNER_REQUIRED_SECTIONS = (
    "## ROLE",
    "## PRIMARY OBJECTIVE",
    "## OUTPUTS (STRICT)",
    "## REQUIRED INPUTS",
    "## STOP CONDITIONS",
    "## FAILURE / RETRY BEHAVIOR",
)
AUDIT_REQUIRED_SECTIONS = (
    "## ROLE",
    "## PRIMARY OBJECTIVE",
)
BRIEF_REQUIRED_SECTIONS = ("## SUMMARY",)
HUMAN_REQUIRED_SECTIONS = ("## ROLE", "## SUMMARY")
AUDIT_REQUIRED_SHARED_INCLUDES = (
    "shared:guardrails.md",
    "shared:repo_scope.md",
)
RUNNER_BANNED_SHARED_INCLUDES = (
    "shared:verification_ritual.md",
    "shared:verifier_common.md",
    "shared:runtime_context.md",
)
RUNNER_STATUS_VOCAB_INCLUDE = "shared:status_vocabulary.md"
RUNNER_STATUS_VOCAB_ALLOWED_STAGES = {"launch", "slurm_monitor", "extract_results"}
RUNNER_NON_NEGOTIABLES_INCLUDE = "shared:runner_non_negotiables.md"
RUNNER_NON_NEGOTIABLES_SECTION = "## NON-NEGOTIABLES"
RUNNER_BANNED_SECTION_PREFIXES = (
    "## STATUS VOCABULARY",
    "## FILE LENGTH BUDGET",
    "## VERIFICATION RITUAL",
    "## EVIDENCE RECORD FORMAT",
    "## EVIDENCE POINTERS",
    "## RUN ARTIFACTS",
    "## FILE CHECKLIST",
    "## CHECKLIST",
)
RUNNER_BANNED_TOKENS = {
    "diff_summary",
    "verifier_outputs",
    "verifier_errors",
    "dry_run_output",
}
_FALLBACK_TERMINAL_STAGES = {"human_review", "stop"}
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
    "launch_execute",
    "metrics_summary",
    "target_comparison",
    "decision_suggestion",
    "auto_metrics_evidence",
    "diff_summary",
    "run_group",
    "replicate_count",
    "task_context",
    "brief_summary",
}

# Shared tokens always allowed (not stage-specific).
_SHARED_TOKENS = {"python_bin", "stage", "stage_context"}
DEFAULT_OPTIONAL_TOKENS_BY_STAGE: dict[str, set[str]] = {
    "design": {"available_memory_gb", "experiment_id", "recommended_memory_estimate"},
    "implementation": {"review_feedback", "verifier_errors"},
    "implementation_review": {"diff_summary", "dry_run_output", "verifier_outputs"},
    "launch": {
        "launch_mode",
        "launch_execute",
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


def _lint_shared_prompt_contracts() -> list[str]:
    failures: list[str] = []

    guardrails_path = SHARED_PROMPTS_DIR / "guardrails.md"
    verification_ritual_path = SHARED_PROMPTS_DIR / "verification_ritual.md"
    runtime_context_path = SHARED_PROMPTS_DIR / "runtime_context.md"

    try:
        guardrails_text = guardrails_path.read_text(encoding="utf-8")
    except Exception as exc:
        failures.append(f"{guardrails_path} could not be read: {exc}")
        guardrails_text = ""
    if "docs/todo.md" in guardrails_text or "documentation.md" in guardrails_text:
        failures.append(
            f"{guardrails_path} must not include universal dual-memory policy guidance; use shared:memory_brief.md in opted-in stages"
        )

    try:
        verification_text = verification_ritual_path.read_text(encoding="utf-8")
    except Exception as exc:
        failures.append(f"{verification_ritual_path} could not be read: {exc}")
        verification_text = ""
    if (
        "Dual-memory audit" in verification_text
        or "docs/todo.md" in verification_text
        or "documentation.md" in verification_text
    ):
        failures.append(
            f"{verification_ritual_path} must remain memory-policy neutral; stage-specific memory checks belong in shared:memory_brief.md"
        )

    try:
        runtime_context_text = runtime_context_path.read_text(encoding="utf-8")
    except Exception as exc:
        failures.append(f"{runtime_context_path} could not be read: {exc}")
        runtime_context_text = ""
    if "deterministic runtime stages" not in runtime_context_text or (
        "bypasses the runner" not in runtime_context_text
    ):
        failures.append(
            f"{runtime_context_path} must explicitly describe deterministic-stage runner bypass semantics"
        )

    return failures


def _format_shared_include(include_ref: str) -> str:
    normalized = str(include_ref).strip()
    if normalized.startswith("{{") and normalized.endswith("}}"):
        return normalized
    return "{{" + normalized + "}}"


def _extract_shared_includes(text: str) -> set[str]:
    return {
        f"shared:{match.group(1).strip()}"
        for match in SHARED_INCLUDE_PATTERN.finditer(text)
    }


def _resolve_shared_include_closure(
    text: str,
    *,
    _visited: set[str] | None = None,
) -> set[str]:
    visited = _visited if _visited is not None else set()
    include_root = (PROMPTS_DIR / "shared").resolve()
    resolved: set[str] = set()

    for include_ref in _extract_shared_includes(text):
        normalized_include = include_ref.strip()
        if normalized_include in visited:
            continue
        visited.add(normalized_include)
        resolved.add(normalized_include)

        if not normalized_include.startswith("shared:"):
            continue

        include_target = normalized_include.split(":", 1)[1].strip()
        if not include_target:
            continue

        candidate_path = (include_root / include_target).resolve()
        try:
            candidate_path.relative_to(include_root)
        except Exception:
            continue
        if not candidate_path.exists() or not candidate_path.is_file():
            continue
        try:
            nested_text = candidate_path.read_text(encoding="utf-8")
        except Exception:
            continue
        resolved.update(_resolve_shared_include_closure(nested_text, _visited=visited))

    return resolved


def _resolve_terminal_stages() -> set[str]:
    try:
        from autolab.registry import load_registry, registry_terminal_stages

        registry = load_registry(REPO_ROOT)
        if registry:
            terminal_stages = {
                str(stage).strip()
                for stage in registry_terminal_stages(registry)
                if str(stage).strip()
            }
            if terminal_stages:
                return terminal_stages
    except Exception:
        pass
    return set(_FALLBACK_TERMINAL_STAGES)


def _resolve_allowed_tokens() -> set[str]:
    """Build ALLOWED_TOKENS dynamically from workflow.yaml required_tokens."""
    try:
        from autolab.registry import load_registry

        registry = load_registry(REPO_ROOT)
        if registry:
            tokens: set[str] = set(_SHARED_TOKENS)
            for spec in registry.values():
                tokens.update(spec.required_tokens)
                tokens.update(spec.optional_tokens)
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
                    "launch_execute",
                    "recommended_memory_estimate",
                    "available_memory_gb",
                    "hypothesis_id",
                    "run_group",
                    "replicate_count",
                    "task_context",
                    "brief_summary",
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
    "human_review": {"iteration_id", "iteration_path"},
    "stop": {"iteration_id", "iteration_path"},
}


def _resolve_stage_prompt_files() -> dict[str, dict[str, str]]:
    try:
        from autolab.registry import (
            load_registry,
            registry_brief_prompt_files,
            registry_human_prompt_files,
            registry_prompt_files,
            registry_runner_prompt_files,
        )

        registry = load_registry(REPO_ROOT)
        mapping: dict[str, dict[str, str]] = {}
        if registry:
            audit = registry_prompt_files(registry)
            runner = registry_runner_prompt_files(registry)
            brief = registry_brief_prompt_files(registry)
            human = registry_human_prompt_files(registry)
            for stage_name in registry.keys():
                normalized_stage = str(stage_name).strip()
                if not normalized_stage:
                    continue
                mapping[normalized_stage] = {
                    "runner": str(runner.get(stage_name, "")).strip(),
                    "audit": str(audit.get(stage_name, "")).strip(),
                    "brief": str(brief.get(stage_name, "")).strip(),
                    "human": str(human.get(stage_name, "")).strip(),
                }
        if mapping:
            return mapping
    except Exception:
        pass

    discovered: dict[str, dict[str, str]] = {}
    for prompt_path in sorted(PROMPTS_DIR.glob("stage_*.runner.md")):
        stem = prompt_path.name
        stage = stem[len("stage_") : -len(".runner.md")].strip()
        if not stage:
            continue
        discovered[stage] = {
            "runner": f"stage_{stage}.runner.md",
            "audit": f"stage_{stage}.audit.md",
            "brief": f"stage_{stage}.brief.md",
            "human": f"stage_{stage}.human.md",
        }
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
                if tokens:
                    resolved[stage_name] = tokens
                else:
                    fallback_tokens = DEFAULT_REQUIRED_TOKENS_BY_STAGE.get(
                        stage_name, set()
                    )
                    if fallback_tokens:
                        resolved[stage_name] = set(fallback_tokens)
        for fallback_stage, fallback_tokens in DEFAULT_REQUIRED_TOKENS_BY_STAGE.items():
            if fallback_stage not in resolved and fallback_tokens:
                resolved[fallback_stage] = set(fallback_tokens)
        if resolved:
            return resolved
    except Exception:
        pass
    return dict(DEFAULT_REQUIRED_TOKENS_BY_STAGE)


def _resolve_optional_tokens_by_stage() -> dict[str, set[str]]:
    try:
        from autolab.registry import load_registry

        registry = load_registry(REPO_ROOT)
        resolved: dict[str, set[str]] = {}
        missing_optional_key_stages: set[str] = set()

        workflow_path = REPO_ROOT / ".autolab" / "workflow.yaml"
        if workflow_path.exists():
            try:
                import yaml as _yaml

                workflow_payload = _yaml.safe_load(
                    workflow_path.read_text(encoding="utf-8")
                )
                stages_payload = (
                    workflow_payload.get("stages", {})
                    if isinstance(workflow_payload, dict)
                    else {}
                )
                if isinstance(stages_payload, dict):
                    for stage_name, stage_payload in stages_payload.items():
                        normalized_stage = str(stage_name).strip()
                        if not normalized_stage or not isinstance(stage_payload, dict):
                            continue
                        if "optional_tokens" not in stage_payload:
                            missing_optional_key_stages.add(normalized_stage)
            except Exception:
                missing_optional_key_stages = set()

        for stage_name, spec in registry.items():
            normalized_stage = str(stage_name).strip()
            if not normalized_stage:
                continue
            tokens = {
                str(token).strip()
                for token in spec.optional_tokens
                if str(token).strip()
            }
            if tokens:
                resolved[normalized_stage] = tokens
                continue
            if normalized_stage in missing_optional_key_stages:
                fallback_tokens = DEFAULT_OPTIONAL_TOKENS_BY_STAGE.get(
                    normalized_stage, set()
                )
                if fallback_tokens:
                    resolved[normalized_stage] = set(fallback_tokens)
        if resolved:
            return resolved
    except Exception:
        pass
    return dict(DEFAULT_OPTIONAL_TOKENS_BY_STAGE)


def _lint_stage_prompt(
    stage: str,
    audience: str,
    prompt_path: Path,
    *,
    terminal_stages: set[str],
    required_tokens_by_stage: dict[str, set[str]],
    optional_tokens_by_stage: dict[str, set[str]],
) -> list[str]:
    if not prompt_path.exists():
        return [f"{prompt_path} is missing"]
    try:
        text = prompt_path.read_text(encoding="utf-8")
    except Exception as exc:
        return [f"{prompt_path} could not be read: {exc}"]

    failures: list[str] = []
    lowered = text.lower()
    shared_includes = _resolve_shared_include_closure(text)

    def _runner_duplicate_headings() -> list[str]:
        heading_pattern = re.compile(r"^\s*##\s+(.+?)\s*$", flags=re.MULTILINE)
        seen: dict[str, str] = {}
        duplicates: list[str] = []
        for match in heading_pattern.finditer(text):
            original = match.group(1).strip()
            normalized = " ".join(original.lower().split())
            if not normalized:
                continue
            if normalized in seen:
                duplicates.append(original)
            else:
                seen[normalized] = original
        return duplicates

    required_sections = AUDIT_REQUIRED_SECTIONS
    if audience == "runner":
        required_sections = RUNNER_REQUIRED_SECTIONS
    elif audience == "brief":
        required_sections = BRIEF_REQUIRED_SECTIONS
    elif audience == "human":
        required_sections = HUMAN_REQUIRED_SECTIONS
    elif audience == "audit":
        required_sections = AUDIT_REQUIRED_SECTIONS

    if audience == "audit" and stage not in terminal_stages:
        for include in AUDIT_REQUIRED_SHARED_INCLUDES:
            if include not in shared_includes:
                failures.append(
                    f"{prompt_path} missing required shared include: {_format_shared_include(include)}"
                )
    if audience == "audit" and stage == "design":
        extract_parser_mentions = lowered.count("extract_parser")
        if extract_parser_mentions < 2:
            failures.append(
                f"{prompt_path} design audit prompt must mention extract_parser in both contract guidance and output template"
            )
        if not re.search(r"^\s*extract_parser:\s*$", text, flags=re.MULTILINE):
            failures.append(
                f"{prompt_path} design output template must include an extract_parser mapping block"
            )
        if "scope_kind" not in lowered:
            failures.append(
                f"{prompt_path} design contract must explicitly require implementation_requirements.scope_kind"
            )

    if audience == "runner":
        if (
            RUNNER_STATUS_VOCAB_INCLUDE in shared_includes
            and stage not in RUNNER_STATUS_VOCAB_ALLOWED_STAGES
        ):
            failures.append(
                (
                    f"{prompt_path} includes status vocabulary in runner template "
                    f"for non-mutator stage '{stage}': {_format_shared_include(RUNNER_STATUS_VOCAB_INCLUDE)}"
                )
            )

        for include in RUNNER_BANNED_SHARED_INCLUDES:
            if include in shared_includes:
                failures.append(
                    f"{prompt_path} includes audit-only shared block in runner template: {_format_shared_include(include)}"
                )

        has_non_negotiables_section = bool(
            re.search(
                r"^\s*##\s*NON-NEGOTIABLES\b", text, flags=re.IGNORECASE | re.MULTILINE
            )
        )
        has_non_negotiables_include = RUNNER_NON_NEGOTIABLES_INCLUDE in shared_includes
        if not has_non_negotiables_section and not has_non_negotiables_include:
            failures.append(
                (
                    f"{prompt_path} runner template must include "
                    f"{_format_shared_include(RUNNER_NON_NEGOTIABLES_INCLUDE)} "
                    f"or section heading: {RUNNER_NON_NEGOTIABLES_SECTION}"
                )
            )

        for heading_prefix in RUNNER_BANNED_SECTION_PREFIXES:
            if re.search(
                rf"^\s*{re.escape(heading_prefix)}\b",
                text,
                flags=re.IGNORECASE | re.MULTILINE,
            ):
                failures.append(
                    f"{prompt_path} includes banned runner section: {heading_prefix}"
                )

        duplicate_headings = _runner_duplicate_headings()
        if duplicate_headings:
            failures.append(
                (
                    f"{prompt_path} contains duplicate runner heading(s): "
                    f"{', '.join(sorted(set(duplicate_headings)))}"
                )
            )

    # Map sections to shared includes that satisfy the requirement
    _section_shared_equivalents: dict[str, str] = {
        "## FAILURE / RETRY BEHAVIOR": "{{shared:failure_retry.md}}",
    }
    for section in required_sections:
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

    # Flag literal double-slash pattern paths in prompt content.
    for ds_match in re.finditer(r"(?:runs|experiments|paper)//", text):
        failures.append(
            f"{prompt_path} contains literal double-slash path "
            f"'{ds_match.group()}' at an unresolved path token"
        )

    if (
        audience == "audit"
        and "## file checklist" in lowered
        and "{{shared:checklist.md}}" not in text
    ):
        failures.append(
            f"{prompt_path} checklist section must include {{shared:checklist.md}}"
        )

    required_tokens = required_tokens_by_stage.get(stage, set())

    tokens_in_prompt = {
        match.group(1).strip() for match in TOKEN_PATTERN.finditer(text)
    }
    if audience == "runner" and required_tokens:
        missing_required_tokens = sorted(
            token for token in required_tokens if token not in tokens_in_prompt
        )
        if missing_required_tokens:
            failures.append(
                f"{prompt_path} missing required token(s) for stage '{stage}': {', '.join(missing_required_tokens)}"
            )
    unsupported_tokens = sorted(
        token for token in tokens_in_prompt if token not in ALLOWED_TOKENS
    )
    if unsupported_tokens:
        failures.append(
            f"{prompt_path} has unsupported token(s): {', '.join(unsupported_tokens)}"
        )

    if audience == "runner":
        banned_tokens_used = sorted(
            token for token in tokens_in_prompt if token in RUNNER_BANNED_TOKENS
        )
        if banned_tokens_used:
            failures.append(
                (
                    f"{prompt_path} uses banned raw-blob token(s) in runner template: "
                    f"{', '.join(banned_tokens_used)}"
                )
            )

    stage_optional_tokens = optional_tokens_by_stage.get(stage, set())
    optional_tokens_used = sorted(
        token
        for token in tokens_in_prompt
        if token not in required_tokens and token not in _SHARED_TOKENS
    )
    undocumented_optional_tokens = [
        token for token in optional_tokens_used if token not in stage_optional_tokens
    ]
    if audience == "runner" and undocumented_optional_tokens:
        failures.append(
            f"{prompt_path} uses token(s) not declared as required or optional for stage '{stage}': {', '.join(undocumented_optional_tokens)}"
        )
    if (
        audience == "runner"
        and optional_tokens_used
        and "## missing-input fallbacks" not in lowered
    ):
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
    "{{shared:assistant_guardrails.md}}",
    "{{shared:assistant_output_contract.md}}",
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

    if "## response format" not in lowered:
        failures.append(
            f"{prompt_path} missing required section heading: ## RESPONSE FORMAT"
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

    # Flag literal double-slash pattern paths in prompt content.
    for ds_match in re.finditer(r"(?:runs|experiments|paper)//", text):
        failures.append(
            f"{prompt_path} contains literal double-slash path "
            f"'{ds_match.group()}' at an unresolved path token"
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
        failures.extend(_lint_shared_prompt_contracts())
        stage_prompt_files = _resolve_stage_prompt_files()
        terminal_stages = _resolve_terminal_stages()
        required_tokens_by_stage = _resolve_required_tokens_by_stage()
        optional_tokens_by_stage = _resolve_optional_tokens_by_stage()

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
            stage_files = stage_prompt_files[stage]
            if not isinstance(stage_files, dict):
                failures.append(
                    f"stage '{stage}' prompt mapping must include runner/audit/brief/human files"
                )
                continue
            for audience in ("runner", "audit", "brief", "human"):
                filename = str(stage_files.get(audience, "")).strip()
                if not filename:
                    failures.append(
                        f"stage '{stage}' is missing prompt file mapping for audience '{audience}'"
                    )
                    continue
                prompt_path = PROMPTS_DIR / filename
                failures.extend(
                    _lint_stage_prompt(
                        stage,
                        audience,
                        prompt_path,
                        terminal_stages=terminal_stages,
                        required_tokens_by_stage=required_tokens_by_stage,
                        optional_tokens_by_stage=optional_tokens_by_stage,
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
