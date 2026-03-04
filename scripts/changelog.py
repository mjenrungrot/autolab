#!/usr/bin/env python3
"""Manage and validate version-scoped CHANGELOG entries."""

from __future__ import annotations

import argparse
import datetime as dt
import re
import subprocess
import sys
from pathlib import Path

VERSION_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
SECTION_HEADER_RE = re.compile(
    r"^## \[(?P<version>(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*))\] - (?P<date>\d{4}-\d{2}-\d{2})$"
)
RANGE_MARKER_RE = re.compile(
    r"^<!-- autolab:range (?P<previous>v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*))\.\."
    r"(?P<current>v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)) -->$"
)
SUMMARY_HEADING = "### Summary"
TITLE_LINE = "# Changelog"
PLACEHOLDER_RE = re.compile(
    r"(todo|tbd|placeholder|fill in|write summary|replace this)", re.IGNORECASE
)


class ChangelogSection:
    def __init__(
        self,
        *,
        version: str,
        date: str,
        start_line: int,
        end_line: int,
        body_lines: tuple[str, ...],
    ) -> None:
        self.version = version
        self.date = date
        self.start_line = start_line
        self.end_line = end_line
        self.body_lines = body_lines


def _run_git(
    args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed: {(result.stderr or result.stdout).strip()}"
        )
    return result


def _validate_version(value: str, *, label: str) -> str:
    normalized = value.strip()
    if VERSION_RE.fullmatch(normalized) is None:
        raise ValueError(f"{label} must be MAJOR.MINOR.PATCH, got '{value}'")
    return normalized


def _validate_tag(value: str, *, label: str) -> str:
    normalized = value.strip()
    if TAG_RE.fullmatch(normalized) is None:
        raise ValueError(f"{label} must be vMAJOR.MINOR.PATCH, got '{value}'")
    return normalized


def _default_changelog_path() -> Path:
    return Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RuntimeError(f"missing changelog at {path}") from exc


def _read_text_from_index(path: Path) -> str:
    rel_path = str(path)
    if path.is_absolute():
        repo_root = Path(_run_git(["rev-parse", "--show-toplevel"]).stdout.strip())
        rel_path = str(path.relative_to(repo_root))

    result = _run_git(["show", f":{rel_path}"], check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip() or "path is not staged"
        raise RuntimeError(f"could not read {rel_path} from index: {detail}")
    return result.stdout


def _parse_sections(text: str) -> list[ChangelogSection]:
    lines = text.splitlines()
    header_points: list[tuple[int, re.Match[str]]] = []
    for idx, line in enumerate(lines):
        match = SECTION_HEADER_RE.fullmatch(line.strip())
        if match is None:
            continue
        header_points.append((idx, match))

    sections: list[ChangelogSection] = []
    for section_idx, (start, match) in enumerate(header_points):
        end = (
            header_points[section_idx + 1][0]
            if section_idx + 1 < len(header_points)
            else len(lines)
        )
        sections.append(
            ChangelogSection(
                version=match.group("version"),
                date=match.group("date"),
                start_line=start + 1,
                end_line=end,
                body_lines=tuple(lines[start + 1 : end]),
            )
        )
    return sections


def _extract_range_marker(section: ChangelogSection) -> tuple[str, str] | None:
    for line in section.body_lines:
        marker = RANGE_MARKER_RE.fullmatch(line.strip())
        if marker is None:
            continue
        return marker.group("previous"), marker.group("current")
    return None


def _is_placeholder_summary_bullet(value: str) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return True
    if normalized in {"todo", "tbd", "placeholder", "n/a"}:
        return True
    return PLACEHOLDER_RE.search(normalized) is not None


def _extract_summary_bullets(
    section: ChangelogSection,
) -> tuple[list[str], str | None]:
    body = list(section.body_lines)
    heading_index = -1
    for idx, line in enumerate(body):
        if line.strip() == SUMMARY_HEADING:
            heading_index = idx
            break
    if heading_index < 0:
        return [], f"version {section.version} is missing '{SUMMARY_HEADING}' heading"

    bullets: list[str] = []
    for line in body[heading_index + 1 :]:
        stripped = line.strip()
        if stripped.startswith("### "):
            break
        if not stripped or stripped.startswith("<!--"):
            continue
        if not stripped.startswith("- "):
            continue
        bullet = stripped[2:].strip()
        if bullet:
            bullets.append(bullet)

    if not bullets:
        return [], f"version {section.version} summary must include at least one bullet"

    meaningful = [
        bullet for bullet in bullets if not _is_placeholder_summary_bullet(bullet)
    ]
    if not meaningful:
        return (
            [],
            f"version {section.version} summary bullets are placeholders; write real release notes",
        )

    return meaningful, None


def _validate_current_section(
    *,
    text: str,
    previous_version: str,
    current_version: str,
) -> list[str]:
    errors: list[str] = []
    sections = _parse_sections(text)
    if not sections:
        return [
            "changelog has no version sections (expected headers like '## [1.2.3] - 2026-03-03')"
        ]

    target = next(
        (section for section in sections if section.version == current_version), None
    )
    if target is None:
        errors.append(
            f"missing section for current version {current_version} (expected header: "
            f"## [{current_version}] - YYYY-MM-DD)"
        )
        return errors

    if sections[0].version != current_version:
        errors.append(
            f"current version {current_version} must be the first release section "
            f"(found {sections[0].version} first)"
        )

    _, summary_error = _extract_summary_bullets(target)
    if summary_error is not None:
        errors.append(summary_error)

    marker = _extract_range_marker(target)
    expected_previous_tag = f"v{previous_version}"
    expected_current_tag = f"v{current_version}"
    if marker is None:
        errors.append(
            f"version {current_version} is missing range marker "
            f"'<!-- autolab:range {expected_previous_tag}..{expected_current_tag} -->'"
        )
    else:
        previous_tag, current_tag = marker
        if previous_tag != expected_previous_tag or current_tag != expected_current_tag:
            errors.append(
                f"version {current_version} range marker must be "
                f"{expected_previous_tag}..{expected_current_tag}, found {previous_tag}..{current_tag}"
            )

    return errors


def _insert_scaffold_section(
    *,
    existing_text: str,
    previous_version: str,
    current_version: str,
    date_value: str,
) -> str:
    sections = _parse_sections(existing_text)
    if any(section.version == current_version for section in sections):
        raise RuntimeError(
            f"section for version {current_version} already exists in changelog"
        )

    base_text = existing_text.strip("\n")
    if not base_text:
        base_text = TITLE_LINE
    if not base_text.splitlines()[0].strip().startswith("# "):
        base_text = f"{TITLE_LINE}\n\n{base_text}"
    elif base_text.splitlines()[0].strip() != TITLE_LINE:
        base_text = f"{TITLE_LINE}\n\n{base_text}"

    lines = base_text.splitlines()
    insert_idx = len(lines)
    for idx, line in enumerate(lines):
        if SECTION_HEADER_RE.fullmatch(line.strip()) is not None:
            insert_idx = idx
            break

    section_lines = [
        f"## [{current_version}] - {date_value}",
        "",
        SUMMARY_HEADING,
        "- TODO: summarize user-visible changes in this release.",
        "",
        f"<!-- autolab:range v{previous_version}..v{current_version} -->",
    ]

    prefix = lines[:insert_idx]
    suffix = lines[insert_idx:]
    while prefix and not prefix[-1].strip():
        prefix.pop()

    updated_lines: list[str] = [*prefix, "", *section_lines]
    if suffix:
        updated_lines.append("")
        updated_lines.extend(suffix)

    return "\n".join(updated_lines).rstrip() + "\n"


def _extract_section_for_version(text: str, version: str) -> ChangelogSection:
    for section in _parse_sections(text):
        if section.version == version:
            return section
    raise RuntimeError(
        f"missing section for version {version} (expected header '## [{version}] - YYYY-MM-DD')"
    )


def _render_release_notes(
    *,
    section: ChangelogSection,
    previous_tag: str,
    current_tag: str,
) -> str:
    summary_bullets, summary_error = _extract_summary_bullets(section)
    if summary_error is not None:
        raise RuntimeError(summary_error)

    log = _run_git(
        [
            "log",
            "--no-merges",
            "--pretty=format:- %h %s (%an)",
            f"{previous_tag}..{current_tag}",
        ]
    ).stdout.strip()
    commit_lines = [line for line in log.splitlines() if line.strip()]

    lines: list[str] = [
        f"# Release {current_tag}",
        "",
        SUMMARY_HEADING,
    ]
    lines.extend(f"- {bullet}" for bullet in summary_bullets)
    lines.extend(
        [
            "",
            f"### Commits ({previous_tag}..{current_tag})",
        ]
    )
    if commit_lines:
        lines.extend(commit_lines)
    else:
        lines.append("- No non-merge commits found in range.")
    lines.append("")
    return "\n".join(lines)


def _command_scaffold(args: argparse.Namespace) -> int:
    previous_version = _validate_version(
        args.previous_version, label="--previous-version"
    )
    current_version = _validate_version(args.current_version, label="--current-version")
    date_value = args.date.strip() if args.date else dt.date.today().isoformat()

    if dt.date.fromisoformat(date_value):  # validate date
        pass

    changelog_path: Path = args.changelog
    existing = (
        changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else ""
    )
    updated = _insert_scaffold_section(
        existing_text=existing,
        previous_version=previous_version,
        current_version=current_version,
        date_value=date_value,
    )
    changelog_path.write_text(updated, encoding="utf-8")
    print(
        f"changelog: scaffolded {changelog_path} for {previous_version}..{current_version}"
    )
    return 0


def _command_validate(args: argparse.Namespace) -> int:
    previous_version = _validate_version(
        args.previous_version, label="--previous-version"
    )
    current_version = _validate_version(args.current_version, label="--current-version")

    changelog_path: Path = args.changelog
    try:
        text = (
            _read_text_from_index(changelog_path)
            if args.from_index
            else _read_text(changelog_path)
        )
    except Exception as exc:
        print(f"changelog: ERROR {exc}", file=sys.stderr)
        return 1

    errors = _validate_current_section(
        text=text,
        previous_version=previous_version,
        current_version=current_version,
    )
    if errors:
        print("changelog: validation failed", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    source = "index" if args.from_index else "working tree"
    print(
        "changelog: validation passed "
        f"(version {current_version}, range v{previous_version}..v{current_version}, source={source})"
    )
    return 0


def _command_render_release_notes(args: argparse.Namespace) -> int:
    version = _validate_version(args.version, label="--version")
    current_tag = _validate_tag(args.current_tag, label="--current-tag")
    previous_tag = _validate_tag(args.previous_tag, label="--previous-tag")

    try:
        text = _read_text(args.changelog)
        section = _extract_section_for_version(text, version)
    except Exception as exc:
        print(f"changelog: ERROR {exc}", file=sys.stderr)
        return 1

    marker = _extract_range_marker(section)
    if marker is None:
        print(
            f"changelog: ERROR version {version} is missing range marker in CHANGELOG.md",
            file=sys.stderr,
        )
        return 1
    marker_previous, marker_current = marker
    if marker_previous != previous_tag or marker_current != current_tag:
        print(
            "changelog: ERROR section range marker does not match release tags "
            f"(expected {previous_tag}..{current_tag}, found {marker_previous}..{marker_current})",
            file=sys.stderr,
        )
        return 1

    try:
        notes = _render_release_notes(
            section=section,
            previous_tag=previous_tag,
            current_tag=current_tag,
        )
        args.output.write_text(notes, encoding="utf-8")
    except Exception as exc:
        print(f"changelog: ERROR {exc}", file=sys.stderr)
        return 1

    print(f"changelog: wrote release notes to {args.output}")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage version-scoped changelog entries."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    scaffold = subparsers.add_parser(
        "scaffold",
        help="Insert a new release section template for a specific previous/current version pair.",
    )
    scaffold.add_argument(
        "--previous-version",
        required=True,
        help="Previous released version in MAJOR.MINOR.PATCH format.",
    )
    scaffold.add_argument(
        "--current-version",
        required=True,
        help="Current version in MAJOR.MINOR.PATCH format.",
    )
    scaffold.add_argument(
        "--date",
        default="",
        help="Section date in YYYY-MM-DD (default: today).",
    )
    scaffold.add_argument(
        "--changelog",
        type=Path,
        default=_default_changelog_path(),
        help="Path to CHANGELOG.md (default: repository root).",
    )
    scaffold.set_defaults(func=_command_scaffold)

    validate = subparsers.add_parser(
        "validate",
        help="Validate current-version changelog section and exact previous..current marker.",
    )
    validate.add_argument(
        "--previous-version",
        required=True,
        help="Previous released version in MAJOR.MINOR.PATCH format.",
    )
    validate.add_argument(
        "--current-version",
        required=True,
        help="Current version in MAJOR.MINOR.PATCH format.",
    )
    validate.add_argument(
        "--changelog",
        type=Path,
        default=_default_changelog_path(),
        help="Path to CHANGELOG.md (default: repository root).",
    )
    validate.add_argument(
        "--from-index",
        action="store_true",
        help="Read changelog content from git index instead of working tree.",
    )
    validate.set_defaults(func=_command_validate)

    release_notes = subparsers.add_parser(
        "render-release-notes",
        help="Generate release note body from changelog summary plus commit log range.",
    )
    release_notes.add_argument(
        "--version",
        required=True,
        help="Version in MAJOR.MINOR.PATCH format (matches changelog section).",
    )
    release_notes.add_argument(
        "--current-tag",
        required=True,
        help="Current git release tag (vMAJOR.MINOR.PATCH).",
    )
    release_notes.add_argument(
        "--previous-tag",
        required=True,
        help="Previous git release tag (vMAJOR.MINOR.PATCH).",
    )
    release_notes.add_argument(
        "--changelog",
        type=Path,
        default=_default_changelog_path(),
        help="Path to CHANGELOG.md (default: repository root).",
    )
    release_notes.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output path for release notes markdown.",
    )
    release_notes.set_defaults(func=_command_render_release_notes)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        print(f"changelog: ERROR {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
