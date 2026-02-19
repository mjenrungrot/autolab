"""Autolab stage registry -- loads workflow.yaml into typed StageSpec objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml as _yaml
except ImportError:  # pragma: no cover
    _yaml = None  # type: ignore[assignment]


@dataclass(frozen=True)
class StageSpec:
    """Typed descriptor for a single stage in the autolab workflow."""

    name: str
    prompt_file: str
    required_tokens: frozenset[str]
    required_outputs: tuple[str, ...]
    next_stage: str
    verifier_categories: dict[str, bool]
    optional_tokens: frozenset[str] = field(default_factory=frozenset)
    required_outputs_any_of: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    required_outputs_if: tuple[
        tuple[tuple[tuple[str, str], ...], tuple[str, ...]], ...
    ] = field(default_factory=tuple)
    decision_map: dict[str, str] = field(default_factory=dict)
    is_active: bool = False
    is_terminal: bool = False
    is_decision: bool = False
    is_runner_eligible: bool = False


def _parse_stage_spec(name: str, raw: dict[str, Any]) -> StageSpec:
    """Convert a raw YAML mapping into a ``StageSpec``."""
    classifications = raw.get("classifications") or {}
    if not isinstance(classifications, dict):
        classifications = {}

    verifier_cats = raw.get("verifier_categories") or {}
    if not isinstance(verifier_cats, dict):
        verifier_cats = {}

    required_tokens_raw = raw.get("required_tokens") or []
    if not isinstance(required_tokens_raw, list):
        required_tokens_raw = []
    optional_tokens_raw = raw.get("optional_tokens") or []
    if not isinstance(optional_tokens_raw, list):
        optional_tokens_raw = []

    required_outputs_raw = raw.get("required_outputs") or []
    if not isinstance(required_outputs_raw, list):
        required_outputs_raw = []
    required_outputs = [
        str(output).strip() for output in required_outputs_raw if str(output).strip()
    ]

    required_outputs_any_of_raw = raw.get("required_outputs_any_of") or []
    required_outputs_any_of: list[tuple[str, ...]] = []
    if isinstance(required_outputs_any_of_raw, list):
        for raw_group in required_outputs_any_of_raw:
            if not isinstance(raw_group, list):
                continue
            normalized_group = tuple(
                str(output).strip() for output in raw_group if str(output).strip()
            )
            if normalized_group:
                required_outputs_any_of.append(normalized_group)

    required_outputs_if_raw = raw.get("required_outputs_if") or []
    if isinstance(required_outputs_if_raw, dict):
        required_outputs_if_raw = [required_outputs_if_raw]
    required_outputs_if: list[tuple[tuple[tuple[str, str], ...], tuple[str, ...]]] = []
    if isinstance(required_outputs_if_raw, list):
        for raw_rule in required_outputs_if_raw:
            if not isinstance(raw_rule, dict):
                continue
            outputs_raw = raw_rule.get("outputs") or []
            if not isinstance(outputs_raw, list):
                continue
            outputs = tuple(
                str(output).strip() for output in outputs_raw if str(output).strip()
            )
            if not outputs:
                continue
            conditions = tuple(
                (str(key).strip(), str(value).strip().lower())
                for key, value in raw_rule.items()
                if str(key).strip() and str(key).strip() != "outputs"
            )
            if not conditions:
                continue
            required_outputs_if.append((conditions, outputs))

    decision_map_raw = raw.get("decision_map") or {}
    if not isinstance(decision_map_raw, dict):
        decision_map_raw = {}

    return StageSpec(
        name=name,
        prompt_file=str(raw.get("prompt_file", f"stage_{name}.md")),
        required_tokens=frozenset(str(t) for t in required_tokens_raw),
        optional_tokens=frozenset(str(t) for t in optional_tokens_raw),
        required_outputs=tuple(required_outputs),
        required_outputs_any_of=tuple(required_outputs_any_of),
        required_outputs_if=tuple(required_outputs_if),
        next_stage=str(raw.get("next_stage", "")),
        verifier_categories={str(k): bool(v) for k, v in verifier_cats.items()},
        decision_map={str(k): str(v) for k, v in decision_map_raw.items()},
        is_active=bool(classifications.get("active", False)),
        is_terminal=bool(classifications.get("terminal", False)),
        is_decision=bool(classifications.get("decision", False)),
        is_runner_eligible=bool(classifications.get("runner_eligible", False)),
    )


def load_registry(repo_root: Path) -> dict[str, StageSpec]:
    """Load the stage registry from ``workflow.yaml``.

    Returns a mapping of stage name to ``StageSpec``.  Falls back to an
    empty dict when PyYAML is unavailable or the file is missing.
    """
    if _yaml is None:
        return {}

    workflow_path = repo_root / ".autolab" / "workflow.yaml"
    if not workflow_path.exists():
        return {}

    try:
        data = _yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    if not isinstance(data, dict):
        return {}

    stages_raw = data.get("stages")
    if not isinstance(stages_raw, dict):
        return {}

    registry: dict[str, StageSpec] = {}
    for stage_name, stage_data in stages_raw.items():
        if not isinstance(stage_data, dict):
            continue
        registry[str(stage_name)] = _parse_stage_spec(str(stage_name), stage_data)

    return registry


# ---------------------------------------------------------------------------
# Convenience accessors (derive constants-compatible data from registry)
# ---------------------------------------------------------------------------


def registry_prompt_files(registry: dict[str, StageSpec]) -> dict[str, str]:
    """Return a ``STAGE_PROMPT_FILES``-compatible mapping from a registry."""
    return {name: spec.prompt_file for name, spec in registry.items()}


def registry_required_tokens(registry: dict[str, StageSpec]) -> dict[str, set[str]]:
    """Return a ``PROMPT_REQUIRED_TOKENS_BY_STAGE``-compatible mapping."""
    return {
        name: set(spec.required_tokens)
        for name, spec in registry.items()
        if spec.required_tokens
    }


def registry_optional_tokens(registry: dict[str, StageSpec]) -> dict[str, set[str]]:
    """Return per-stage optional prompt tokens from the workflow registry."""
    return {
        name: set(spec.optional_tokens)
        for name, spec in registry.items()
        if spec.optional_tokens
    }


def registry_active_stages(registry: dict[str, StageSpec]) -> tuple[str, ...]:
    """Return an ``ACTIVE_STAGES``-compatible tuple (preserving insertion order)."""
    return tuple(name for name, spec in registry.items() if spec.is_active)


def registry_terminal_stages(registry: dict[str, StageSpec]) -> tuple[str, ...]:
    """Return a ``TERMINAL_STAGES``-compatible tuple."""
    return tuple(name for name, spec in registry.items() if spec.is_terminal)


def registry_decision_stages(registry: dict[str, StageSpec]) -> tuple[str, ...]:
    """Return a ``DECISION_STAGES``-compatible tuple."""
    return tuple(name for name, spec in registry.items() if spec.is_decision)


def registry_runner_eligible(registry: dict[str, StageSpec]) -> tuple[str, ...]:
    """Return a ``RUNNER_ELIGIBLE_STAGES``-compatible tuple."""
    return tuple(name for name, spec in registry.items() if spec.is_runner_eligible)


def registry_decision_map(registry: dict[str, StageSpec]) -> dict[str, dict[str, str]]:
    """Return a mapping of stage name to its decision_map (only non-empty entries)."""
    return {
        name: dict(spec.decision_map)
        for name, spec in registry.items()
        if spec.decision_map
    }


def registry_all_stages(registry: dict[str, StageSpec]) -> set[str]:
    """Return an ``ALL_STAGES``-compatible set."""
    return set(registry.keys())
