"""CLI handlers for artifact retention and pruning."""

from __future__ import annotations

from autolab.cli.support import *

from autolab.gc import (
    DEFAULT_CHECKPOINT_KEEP_LATEST,
    DEFAULT_DOCS_VIEWS_KEEP_LATEST,
    DEFAULT_EXECUTION_KEEP_LATEST,
    DEFAULT_RESET_ARCHIVE_MAX_AGE_DAYS,
    DEFAULT_TRACEABILITY_KEEP_LATEST,
    GC_ONLY_CHOICES,
    apply_gc_plan,
    build_gc_plan,
)


def _format_bytes(value: int) -> str:
    size = float(max(value, 0))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{int(value)} B"


def _render_gc_text(result: dict[str, Any]) -> str:
    mode = "apply" if result.get("mode") == "apply" else "preview"
    lines = [f"autolab gc ({mode})"]
    policy = result.get("policy", {})
    categories = result.get("categories", [])
    lines.append(
        "categories: "
        + (", ".join(str(item) for item in categories) if categories else "(none)")
    )
    lines.append(f"checkpoint_keep_latest: {policy.get('checkpoint_keep_latest', 0)}")
    lines.append(f"execution_keep_latest: {policy.get('execution_keep_latest', 0)}")
    lines.append(
        f"traceability_keep_latest: {policy.get('traceability_keep_latest', 0)}"
    )
    lines.append(
        f"reset_archive_max_age_days: {policy.get('reset_archive_max_age_days', 0)}"
    )
    lines.append(f"views_keep_latest: {policy.get('views_keep_latest', 0)}")

    summary = result.get("summary", {})
    lines.append(f"candidate_units: {summary.get('candidate_units', 0)}")
    lines.append(f"candidate_paths: {summary.get('candidate_paths', 0)}")
    lines.append(
        "bytes_reclaimable: "
        f"{_format_bytes(int(summary.get('bytes_reclaimable', 0) or 0))}"
    )

    if result.get("mode") == "apply":
        lines.append(f"applied_units: {summary.get('applied_units', 0)}")
        lines.append(f"deleted_paths: {summary.get('deleted_paths', 0)}")
        lines.append(f"failures: {summary.get('failures', 0)}")

    actions = result.get("actions", [])
    if not actions:
        lines.append("(nothing to prune)")
        return "\n".join(lines)

    for action in actions:
        prefix = "applied" if action.get("applied") else "candidate"
        label = str(action.get("label", "") or action.get("kind", "artifact")).strip()
        reason = str(action.get("reason", "")).strip()
        lines.append(
            f"- {prefix}: {label} [{action.get('kind', '')}] "
            f"bytes={_format_bytes(int(action.get('bytes', 0) or 0))}"
        )
        if reason:
            lines.append(f"  reason: {reason}")
        for path in action.get("paths", []):
            lines.append(f"  path: {path}")
        for error in action.get("errors", []):
            lines.append(f"  error: {error}")

    return "\n".join(lines)


def _cmd_gc(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)

    only_values = [str(value).strip() for value in getattr(args, "only", []) or []]
    categories: list[str] = []
    seen: set[str] = set()
    source_values = only_values or list(GC_ONLY_CHOICES)
    for value in source_values:
        if not value or value in seen:
            continue
        seen.add(value)
        categories.append(value)

    try:
        result = build_gc_plan(
            repo_root,
            state_path=state_path,
            categories=categories,
            checkpoint_keep_latest=max(
                int(getattr(args, "checkpoint_keep_latest", 0) or 0), 0
            ),
            execution_keep_latest=max(
                int(getattr(args, "execution_keep_latest", 0) or 0), 0
            ),
            traceability_keep_latest=max(
                int(getattr(args, "traceability_keep_latest", 0) or 0), 0
            ),
            reset_archive_max_age_days=max(
                int(getattr(args, "reset_archive_max_age_days", 0) or 0), 0
            ),
            views_keep_latest=max(int(getattr(args, "views_keep_latest", 0) or 0), 0),
        )
        if bool(getattr(args, "apply", False)):
            result = apply_gc_plan(repo_root, result)
    except Exception as exc:
        print(f"autolab gc: ERROR {exc}", file=sys.stderr)
        return 1

    if bool(getattr(args, "json", False)):
        print(json.dumps(result, indent=2))
    else:
        print(_render_gc_text(result))

    if result.get("mode") == "apply" and int(
        result.get("summary", {}).get("failures", 0) or 0
    ):
        return 1
    return 0


__all__ = [name for name in globals() if not name.startswith("__")]
