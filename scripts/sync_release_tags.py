#!/usr/bin/env python3
"""Sync release tags to remote and keep only the latest N semantic versions."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

VERSION_LINE_RE = re.compile(
    r'^(?P<prefix>\s*version\s*=\s*")(?P<version>[^"]+)(?P<suffix>"\s*(?:#.*)?)$'
)
TAG_RE = re.compile(r"^v(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")


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


def _current_project_version(pyproject_path: Path) -> str:
    lines = pyproject_path.read_text(encoding="utf-8").splitlines()
    in_project_section = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_project_section = stripped == "[project]"
            continue
        if not in_project_section:
            continue
        match = VERSION_LINE_RE.match(line)
        if match is not None:
            return match.group("version").strip()
    raise RuntimeError("missing [project].version in pyproject.toml")


def _semver_key(tag: str) -> tuple[int, int, int]:
    match = TAG_RE.fullmatch(tag)
    if match is None:
        raise ValueError(f"not semver tag: {tag}")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def _list_remote_semver_tags(remote: str) -> list[str]:
    result = _run_git(["ls-remote", "--tags", "--refs", remote], check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "ls-remote failed")

    tags: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        ref = parts[1].strip()
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref.removeprefix("refs/tags/")
        if TAG_RE.fullmatch(tag):
            tags.append(tag)
    tags.sort(key=_semver_key)
    return tags


def _default_pyproject_path() -> Path:
    return Path(__file__).resolve().parents[1] / "pyproject.toml"


def _tag_exists_local(tag: str) -> bool:
    return _run_git(["rev-parse", "-q", "--verify", f"refs/tags/{tag}"], check=False).returncode == 0


def _remote_exists(remote: str) -> bool:
    return _run_git(["remote", "get-url", remote], check=False).returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create/push current release tag and prune remote tags beyond retention."
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=_default_pyproject_path(),
        help="Path to pyproject.toml (default: repository root)",
    )
    parser.add_argument(
        "--remote",
        default="origin",
        help="Remote name to sync tags to (default: origin)",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=10,
        help="Number of newest semantic version tags to keep on remote (default: 10)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report planned pushes/deletions without mutating tags",
    )
    args = parser.parse_args(argv)

    if args.keep < 1:
        print("sync-release-tags: ERROR --keep must be >= 1", file=sys.stderr)
        return 1

    try:
        version = _current_project_version(args.pyproject)
    except Exception as exc:
        print(f"sync-release-tags: ERROR {exc}", file=sys.stderr)
        return 1

    current_tag = f"v{version}"

    try:
        if _tag_exists_local(current_tag):
            print(f"sync-release-tags: local tag {current_tag} already exists")
        else:
            if args.dry_run:
                print(f"sync-release-tags: would create local tag {current_tag}")
            else:
                _run_git(["tag", current_tag])
                print(f"sync-release-tags: created local tag {current_tag}")
    except Exception as exc:
        print(f"sync-release-tags: ERROR {exc}", file=sys.stderr)
        return 1

    if not _remote_exists(args.remote):
        print(f"sync-release-tags: remote '{args.remote}' not found; skipped remote sync")
        return 0

    if args.dry_run:
        print(f"sync-release-tags: would push {current_tag} to {args.remote}")
    else:
        push_result = _run_git(
            ["push", args.remote, f"refs/tags/{current_tag}"],
            check=False,
        )
        if push_result.returncode != 0:
            print(
                "sync-release-tags: WARN failed to push current tag: "
                f"{(push_result.stderr or push_result.stdout).strip()}",
                file=sys.stderr,
            )
        else:
            print(f"sync-release-tags: pushed {current_tag} to {args.remote}")

    try:
        remote_tags = _list_remote_semver_tags(args.remote)
    except Exception as exc:
        print(
            f"sync-release-tags: WARN could not list remote tags: {exc}",
            file=sys.stderr,
        )
        return 0

    prune_tags = remote_tags[:-args.keep] if len(remote_tags) > args.keep else []
    if not prune_tags:
        print(
            f"sync-release-tags: remote has {len(remote_tags)} semantic tags; "
            f"nothing to prune (keep={args.keep})"
        )
        return 0

    print(
        "sync-release-tags: pruning oldest tags on remote: "
        + ", ".join(prune_tags)
    )
    for tag in prune_tags:
        if args.dry_run:
            print(f"sync-release-tags: would delete remote tag {tag}")
        else:
            delete_result = _run_git(
                ["push", args.remote, "--delete", tag],
                check=False,
            )
            if delete_result.returncode != 0:
                print(
                    f"sync-release-tags: WARN failed deleting remote tag {tag}: "
                    f"{(delete_result.stderr or delete_result.stdout).strip()}",
                    file=sys.stderr,
                )
            else:
                print(f"sync-release-tags: deleted remote tag {tag}")

        if _tag_exists_local(tag):
            if args.dry_run:
                print(f"sync-release-tags: would delete local tag {tag}")
            else:
                _run_git(["tag", "-d", tag], check=False)
                print(f"sync-release-tags: deleted local tag {tag}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
