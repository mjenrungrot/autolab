#!/usr/bin/env python3
"""Bump package patch version and sync README pinned install tag."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

try:
    import tomllib
except Exception:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
README_TAG_RE = re.compile(
    r"^(?P<prefix>\s*python -m pip install "
    r"git\+https://github\.com/mjenrungrot/autolab\.git@v)"
    r"(?P<version>(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*))"
    r"(?P<suffix>\s*)$"
)


def _bump_patch(version: str) -> str:
    match = SEMVER_RE.fullmatch(version.strip())
    if match is None:
        raise ValueError(
            f"unsupported version '{version}'; expected MAJOR.MINOR.PATCH numeric format"
        )
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def _load_project_version(pyproject_path: Path) -> str:
    if tomllib is None:
        raise RuntimeError("python tomllib is unavailable; use Python 3.11+")
    payload = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("pyproject.toml must contain a top-level mapping")
    project = payload.get("project")
    if not isinstance(project, dict):
        raise ValueError("pyproject.toml missing [project] table")
    version = str(project.get("version", "")).strip()
    if not version:
        raise ValueError("pyproject.toml missing [project].version")
    return version


def _replace_project_version_line(lines: list[str], *, new_version: str) -> list[str]:
    in_project_section = False

    for idx, line in enumerate(lines):
        stripped = line.strip()

        if stripped.startswith("[") and stripped.endswith("]"):
            in_project_section = stripped == "[project]"
            continue
        if not in_project_section:
            continue

        if not stripped or stripped.startswith("#"):
            continue

        key, sep, rhs = line.partition("=")
        if not sep or key.strip() != "version":
            continue

        comment = ""
        hash_idx = rhs.find("#")
        if hash_idx >= 0:
            comment = rhs[hash_idx:].strip()

        indent = line[: len(line) - len(line.lstrip(" \t"))]
        lines[idx] = f'{indent}version = "{new_version}"' + (
            f" {comment}" if comment else ""
        )
        return lines

    raise ValueError("could not find [project].version in pyproject.toml")


def _sync_readme_tag_line(lines: list[str], new_version: str) -> tuple[list[str], str]:
    for idx, line in enumerate(lines):
        match = README_TAG_RE.match(line)
        if match is None:
            continue
        old_version = match.group("version")
        lines[idx] = f'{match.group("prefix")}{new_version}{match.group("suffix")}'
        return lines, old_version
    raise ValueError("could not find pinned release install command in README.md")


def _update_readme_tag(
    readme_path: Path, new_version: str, *, dry_run: bool = False
) -> str:
    text = readme_path.read_text(encoding="utf-8")
    has_trailing_newline = text.endswith("\n")
    lines = text.splitlines()
    updated_lines, old_tag_version = _sync_readme_tag_line(lines, new_version)
    updated_text = "\n".join(updated_lines)
    if has_trailing_newline:
        updated_text += "\n"

    if not dry_run:
        readme_path.write_text(updated_text, encoding="utf-8")
    return old_tag_version


def bump_version(
    pyproject_path: Path, readme_path: Path, *, dry_run: bool = False
) -> tuple[str, str, str]:
    old_version = _load_project_version(pyproject_path)
    new_version = _bump_patch(old_version)
    text = pyproject_path.read_text(encoding="utf-8")
    has_trailing_newline = text.endswith("\n")
    lines = text.splitlines()
    updated_lines = _replace_project_version_line(lines, new_version=new_version)
    updated_text = "\n".join(updated_lines)
    if has_trailing_newline:
        updated_text += "\n"

    if not dry_run:
        pyproject_path.write_text(updated_text, encoding="utf-8")

    old_tag_version = _update_readme_tag(readme_path, new_version, dry_run=dry_run)
    return old_version, new_version, old_tag_version


def _default_pyproject_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / "pyproject.toml"


def _default_readme_path() -> Path:
    repo_root = Path(__file__).resolve().parents[1]
    return repo_root / "README.md"


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
    parser.add_argument(
        "--readme",
        type=Path,
        default=_default_readme_path(),
        help="Path to README.md with pinned install tag (default: repository root)",
    )
    args = parser.parse_args(argv)

    try:
        old_version, new_version, old_tag_version = bump_version(
            args.pyproject, args.readme, dry_run=args.dry_run
        )
    except Exception as exc:
        print(f"bump-version: ERROR {exc}", file=sys.stderr)
        return 1

    action = "would bump" if args.dry_run else "bumped"
    print(f"bump-version: {action} pyproject {old_version} -> {new_version}")
    print(f"bump-version: synced README tag v{old_tag_version} -> v{new_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
