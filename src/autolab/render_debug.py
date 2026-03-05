from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from autolab.constants import PROMPT_SHARED_INCLUDE_PATTERN
from autolab.models import RenderedPromptBundle

ALL_RENDER_VIEWS: tuple[str, ...] = ("runner", "audit", "brief", "human", "context")
_WARNING_ORDER: tuple[str, ...] = (
    "duplicate_headers",
    "unknown_unavailable_leaks",
    "raw_json_log_injection",
    "stage_irrelevant_includes",
)

RUNNER_BANNED_SECTION_PREFIXES: tuple[str, ...] = (
    "## STATUS VOCABULARY",
    "## FILE LENGTH BUDGET",
    "## VERIFICATION RITUAL",
    "## EVIDENCE RECORD FORMAT",
    "## EVIDENCE POINTERS",
    "## RUN ARTIFACTS",
    "## FILE CHECKLIST",
    "## CHECKLIST",
)
RUNNER_BANNED_SHARED_INCLUDES: tuple[str, ...] = (
    "shared:verification_ritual.md",
    "shared:verifier_common.md",
    "shared:runtime_context.md",
)
RUNNER_STATUS_VOCAB_SECTION_PREFIX = "## STATUS VOCABULARY"
RUNNER_STATUS_VOCAB_INCLUDE = "shared:status_vocabulary.md"
RUNNER_STATUS_VOCAB_ALLOWED_STAGES = {"launch", "slurm_monitor", "extract_results"}

_HEADING_LINE_PATTERN = re.compile(r"^\s*##\s+(.+?)\s*$")
_FENCE_LINE_PATTERN = re.compile(r"^\s*```")
_FENCED_BLOCK_PATTERN = re.compile(r"```([A-Za-z0-9_-]*)\n(.*?)\n```", flags=re.DOTALL)


@dataclass(frozen=True)
class RenderViewStats:
    view: str
    line_count: int
    token_estimate: int
    largest_sections: tuple[tuple[str, int], ...]
    dropped_sections: tuple[str, ...]
    warnings: dict[str, tuple[str, ...]]


def _line_count(text: str) -> int:
    if not text:
        return 0
    return len(text.splitlines())


def _estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return int(math.ceil(len(text) / 4.0))


def _normalize_heading(text: str) -> str:
    return " ".join(text.strip().lower().split())


def _safe_repr(value: Any) -> str:
    try:
        return repr(value)
    except Exception:
        return f"<unrepresentable {type(value).__name__}>"


def _to_jsonable(value: Any, *, seen: set[int] | None = None) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    seen_ids = seen if seen is not None else set()
    value_id = id(value)
    if value_id in seen_ids:
        return "<recursive reference>"

    if isinstance(value, dict):
        seen_ids.add(value_id)
        normalized: dict[str, Any] = {}
        for key, nested in value.items():
            normalized[str(key)] = _to_jsonable(nested, seen=seen_ids)
        seen_ids.remove(value_id)
        return normalized

    if isinstance(value, (list, tuple, set, frozenset)):
        seen_ids.add(value_id)
        normalized_items = [_to_jsonable(item, seen=seen_ids) for item in value]
        seen_ids.remove(value_id)
        return normalized_items

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")

    return _safe_repr(value)


def _safe_json_dumps(value: Any, *, sort_keys: bool) -> str:
    try:
        return json.dumps(
            value,
            indent=2,
            sort_keys=sort_keys,
            ensure_ascii=True,
        )
    except Exception:
        pass

    try:
        return json.dumps(
            _to_jsonable(value),
            indent=2,
            sort_keys=sort_keys,
            ensure_ascii=True,
        )
    except Exception:
        return _safe_repr(value)


def _iter_headings_outside_fences(text: str) -> Iterable[str]:
    in_fence = False
    for line in text.splitlines():
        if _FENCE_LINE_PATTERN.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        heading_match = _HEADING_LINE_PATTERN.match(line)
        if heading_match is None:
            continue
        heading = heading_match.group(1).strip()
        if heading:
            yield f"## {heading}"


def _extract_markdown_sections(text: str) -> list[tuple[str, int]]:
    if not text:
        return []

    lines = text.splitlines()
    sections: list[tuple[str, int]] = []
    current_name = "(preamble)"
    current_lines: list[str] = []
    in_fence = False

    for line in lines:
        if _FENCE_LINE_PATTERN.match(line):
            in_fence = not in_fence
            current_lines.append(line)
            continue

        heading_match = None if in_fence else _HEADING_LINE_PATTERN.match(line)
        if heading_match is not None:
            if current_lines:
                sections.append((current_name, len(current_lines)))
            current_name = f"## {heading_match.group(1).strip()}"
            current_lines = [line]
            continue
        current_lines.append(line)

    if current_lines:
        sections.append((current_name, len(current_lines)))
    return sections


def _extract_context_sections(context_payload: dict[Any, Any]) -> list[tuple[str, int]]:
    if not isinstance(context_payload, dict) or not context_payload:
        return []
    sections: list[tuple[str, int]] = []
    for key in sorted(context_payload.keys(), key=lambda item: str(item)):
        rendered_value = _safe_json_dumps(context_payload.get(key), sort_keys=True)
        sections.append((str(key), _line_count(rendered_value)))
    return sections


def _largest_sections(
    view: str,
    *,
    text: str,
    context_payload: dict[Any, Any],
    limit: int = 5,
) -> tuple[tuple[str, int], ...]:
    if view == "context":
        sections = _extract_context_sections(context_payload)
    else:
        sections = _extract_markdown_sections(text)
    sections.sort(key=lambda item: (-item[1], item[0]))
    return tuple(sections[:limit])


def _find_duplicate_headers(text: str) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for display_heading in _iter_headings_outside_fences(text):
        normalized = _normalize_heading(display_heading)
        if not normalized:
            continue
        if normalized in seen:
            duplicates.append(display_heading)
            continue
        seen.add(normalized)
    return tuple(sorted(set(duplicates)))


def _detect_unknown_unavailable_leaks(text: str) -> tuple[str, ...]:
    findings: list[str] = []
    if re.search(r"(?i)\bunavailable:\s*", text):
        findings.append("contains 'unavailable:' marker")
    if re.search(r"(?im)(?:[:=]\s*)(unknown|none)\s*$", text):
        findings.append("contains key-value unknown/none sentinel")
    if re.search(r"(?im)^\s*[-*]\s*(unknown|none)\s*$", text):
        findings.append("contains bullet unknown/none sentinel")
    return tuple(findings)


def _detect_raw_json_log_injection(text: str) -> tuple[str, ...]:
    findings: list[str] = []

    diff_marker_count = len(re.findall(r"(?m)^(diff --git|@@\s|\+\+\+\s|---\s)", text))
    if diff_marker_count >= 3:
        findings.append("diff/log marker density suggests raw patch or log injection")

    for match in _FENCED_BLOCK_PATTERN.finditer(text):
        language = match.group(1).strip().lower()
        body = match.group(2)
        body_line_count = _line_count(body)
        if body_line_count < 20:
            continue
        if language in {"json", "log"}:
            findings.append(
                f"large fenced {language or 'text'} block ({body_line_count} lines)"
            )
            continue
        compact = body.lstrip()
        if compact.startswith("{") or compact.startswith("["):
            findings.append(f"large fenced JSON-like block ({body_line_count} lines)")
            continue
        if "Traceback (most recent call last)" in body:
            findings.append(
                f"large fenced traceback/log block ({body_line_count} lines)"
            )

    json_key_lines = len(re.findall(r'(?m)^\s*"[A-Za-z0-9_.-]+"\s*:\s*', text))
    if json_key_lines >= 10:
        findings.append("raw JSON key/value blob appears inline")

    return tuple(sorted(set(findings)))


def _extract_shared_includes(text: str) -> set[str]:
    return {
        f"shared:{match.group(1).strip()}"
        for match in PROMPT_SHARED_INCLUDE_PATTERN.finditer(text)
    }


def _resolve_shared_include_closure(
    template_path: Path,
    *,
    include_root: Path,
    visited: set[str] | None = None,
) -> set[str]:
    seen = visited if visited is not None else set()
    try:
        text = template_path.read_text(encoding="utf-8")
    except Exception:
        return set()

    resolved: set[str] = set()
    for include_ref in _extract_shared_includes(text):
        normalized = include_ref.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        resolved.add(normalized)

        include_target = normalized.split(":", 1)[1].strip()
        if not include_target:
            continue

        candidate_path = (include_root / include_target).resolve()
        try:
            candidate_path.relative_to(include_root)
        except Exception:
            continue
        if not candidate_path.exists() or not candidate_path.is_file():
            continue
        resolved.update(
            _resolve_shared_include_closure(
                candidate_path,
                include_root=include_root,
                visited=seen,
            )
        )
    return resolved


def _resolve_shared_include_root(template_path: Path) -> Path:
    try:
        resolved = template_path.resolve()
    except Exception:
        resolved = template_path

    for parent in resolved.parents:
        if parent.name == "prompts" and parent.parent.name == ".autolab":
            return parent / "shared"
    return template_path.parent / "shared"


def _detect_stage_irrelevant_includes(
    stage: str, template_path: Path
) -> tuple[str, ...]:
    include_root = _resolve_shared_include_root(template_path)
    if not include_root.exists():
        return ()

    closure = _resolve_shared_include_closure(
        template_path,
        include_root=include_root,
    )
    findings: list[str] = []

    for include_ref in RUNNER_BANNED_SHARED_INCLUDES:
        if include_ref in closure:
            findings.append(f"{include_ref} included in runner template")

    if (
        RUNNER_STATUS_VOCAB_INCLUDE in closure
        and stage not in RUNNER_STATUS_VOCAB_ALLOWED_STAGES
    ):
        findings.append(
            f"{RUNNER_STATUS_VOCAB_INCLUDE} not allowed for stage '{stage}'"
        )

    return tuple(sorted(findings))


def _runner_banned_section_prefixes_for_stage(stage: str) -> tuple[str, ...]:
    if stage not in RUNNER_STATUS_VOCAB_ALLOWED_STAGES:
        return RUNNER_BANNED_SECTION_PREFIXES
    status_vocab_prefix = _normalize_heading(RUNNER_STATUS_VOCAB_SECTION_PREFIX)
    return tuple(
        prefix
        for prefix in RUNNER_BANNED_SECTION_PREFIXES
        if _normalize_heading(prefix) != status_vocab_prefix
    )


def _detect_dropped_sections(text: str, *, view: str, stage: str) -> tuple[str, ...]:
    if view != "runner":
        return ()

    dropped: list[str] = []
    seen: set[str] = set()
    banned_prefixes = tuple(
        _normalize_heading(prefix)
        for prefix in _runner_banned_section_prefixes_for_stage(stage)
    )

    for display_heading in _iter_headings_outside_fences(text):
        normalized_with_prefix = _normalize_heading(display_heading)

        if any(normalized_with_prefix.startswith(prefix) for prefix in banned_prefixes):
            dropped.append(f"banned section: {display_heading}")

        if normalized_with_prefix in seen:
            dropped.append(f"duplicate heading: {display_heading}")
            continue
        seen.add(normalized_with_prefix)

    return tuple(dropped)


def _view_text(bundle: RenderedPromptBundle, *, view: str) -> str:
    if view == "runner":
        return bundle.prompt_text
    if view == "audit":
        return bundle.audit_text
    if view == "brief":
        return bundle.brief_text
    if view == "human":
        return bundle.human_text
    if view == "context":
        return _safe_json_dumps(bundle.context_payload, sort_keys=True)
    raise ValueError(f"unsupported view '{view}'")


def _view_template_path(bundle: RenderedPromptBundle, *, view: str) -> Path | None:
    if view == "runner":
        return bundle.template_path
    if view == "audit":
        return bundle.audit_template_path
    if view == "brief":
        return bundle.brief_template_path
    if view == "human":
        return bundle.human_template_path
    return None


def build_render_view_stats(
    *,
    stage: str,
    bundle: RenderedPromptBundle,
    view: str,
) -> RenderViewStats:
    text = _view_text(bundle, view=view)
    template_path = _view_template_path(bundle, view=view)

    duplicate_headers = ()
    unknown_unavailable = ()
    raw_json_log = ()
    stage_irrelevant = ()
    if view != "context":
        duplicate_headers = _find_duplicate_headers(text)
        unknown_unavailable = _detect_unknown_unavailable_leaks(text)
        raw_json_log = _detect_raw_json_log_injection(text)
    if view == "runner" and template_path is not None:
        stage_irrelevant = _detect_stage_irrelevant_includes(stage, template_path)

    warnings = {
        "duplicate_headers": duplicate_headers,
        "unknown_unavailable_leaks": unknown_unavailable,
        "raw_json_log_injection": raw_json_log,
        "stage_irrelevant_includes": stage_irrelevant,
    }

    return RenderViewStats(
        view=view,
        line_count=_line_count(text),
        token_estimate=_estimate_tokens(text),
        largest_sections=_largest_sections(
            view,
            text=text,
            context_payload=bundle.context_payload,
        ),
        dropped_sections=_detect_dropped_sections(text, view=view, stage=stage),
        warnings=warnings,
    )


def build_render_stats(
    *,
    stage: str,
    bundle: RenderedPromptBundle,
    views: Iterable[str] | None = None,
) -> tuple[RenderViewStats, ...]:
    requested_views = list(views) if views is not None else list(ALL_RENDER_VIEWS)
    normalized_views: list[str] = []
    for view in requested_views:
        normalized = str(view).strip().lower()
        if not normalized:
            continue
        if normalized not in ALL_RENDER_VIEWS:
            raise ValueError(f"unsupported view '{normalized}'")
        if normalized not in normalized_views:
            normalized_views.append(normalized)

    return tuple(
        build_render_view_stats(stage=stage, bundle=bundle, view=view)
        for view in normalized_views
    )


def format_render_stats_report(
    *,
    stage: str,
    stats: Iterable[RenderViewStats],
) -> str:
    stats_list = list(stats)
    lines: list[str] = [
        "autolab render stats",
        f"stage: {stage}",
        "views: " + ", ".join(item.view for item in stats_list),
    ]

    for item in stats_list:
        lines.extend(
            [
                "",
                f"[{item.view}]",
                f"line_count: {item.line_count}",
                f"token_estimate: {item.token_estimate}",
                "largest_sections:",
            ]
        )
        if item.largest_sections:
            for section_name, count in item.largest_sections:
                lines.append(f"- {section_name}: {count} line(s)")
        else:
            lines.append("- (none)")

        lines.append("dropped_sections:")
        if item.dropped_sections:
            for entry in item.dropped_sections:
                lines.append(f"- {entry}")
        else:
            lines.append("- (none)")

        lines.append("warnings:")
        for warning_key in _WARNING_ORDER:
            findings = item.warnings.get(warning_key, ())
            if not findings:
                lines.append(f"- {warning_key}: (none)")
                continue
            for finding in findings:
                lines.append(f"- {warning_key}: {finding}")

    return "\n".join(lines)


def build_render_stats_report(
    *,
    stage: str,
    bundle: RenderedPromptBundle,
    views: Iterable[str] | None = None,
) -> str:
    stats = build_render_stats(stage=stage, bundle=bundle, views=views)
    return format_render_stats_report(stage=stage, stats=stats)
