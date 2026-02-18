#!/usr/bin/env python3
"""Bump the patch version in pyproject.toml on each commit."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

VERSION_LINE_RE = re.compile(
    r'^(?P<prefix>\s*version\s*=\s*")(?P<version>[^"]+)(?P<suffix>"\s*(?:#.*)?)$'
)
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _bump_patch(version: str) -> str:
    match = SEMVER_RE.fullmatch(version.strip())
    if match is None:
        raise ValueError(
            f"unsupported version '{version}'; expected MAJOR.MINOR.PATCH numeric format"
        )
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def _bump_project_version_line(lines: list[str]) -> tuple[list[str], str, str]:
    in_project_section = False

    for idx, line in enumerate(lines):
        stripped = line.strip()

        if stripped.startswith("[") and stripped.endswith("]"):
            in_project_section = stripped == "[project]"
            continue
        if not in_project_section:
            continue

        match = VERSION_LINE_RE.match(line)
        if match is None:
            continue

        old_version = match.group("version").strip()
        new_version = _bump_patch(old_version)
        lines[idx] = f'{match.group("prefix")}{new_version}{match.group("suffix")}'
        return lines, old_version, new_version

    raise ValueError("could not find [project].version in pyproject.toml")


def bump_version(pyproject_path: Path, *, dry_run: bool = False) -> tuple[str, str]:
    text = pyproject_path.read_text(encoding="utf-8")
    has_trailing_newline = text.endswith("\n")
    lines = text.splitlines()
    updated_lines, old_version, new_version = _bump_project_version_line(lines)
    updated_text = "\n".join(updated_lines)
    if has_trailing_newline:
        updated_text += "\n"

    if not dry_run:
        pyproject_path.write_text(updated_text, encoding="utf-8")

    return old_version, new_version


def _default_pyproject_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / "pyproject.toml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Increment pyproject.toml package patch version."
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=_default_pyproject_path(),
        help="Path to pyproject.toml (default: repository root)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute and print next version without writing changes",
    )
    args = parser.parse_args(argv)

    try:
        old_version, new_version = bump_version(args.pyproject, dry_run=args.dry_run)
    except Exception as exc:
        print(f"bump-version: ERROR {exc}", file=sys.stderr)
        return 1

    action = "would bump" if args.dry_run else "bumped"
    print(f"bump-version: {action} {old_version} -> {new_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
