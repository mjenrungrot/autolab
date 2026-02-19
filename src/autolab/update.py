from __future__ import annotations

import importlib.metadata as importlib_metadata
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from autolab.state import _resolve_autolab_dir, _resolve_repo_root

DEFAULT_RELEASE_REPO_URL = "https://github.com/mjenrungrot/autolab.git"
STABLE_TAG_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")
SEMVER_PATTERN = re.compile(r"^v?(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)$")
PYPROJECT_VERSION_PATTERN = re.compile(r'^version\s*=\s*"(?P<version>[^"]+)"\s*$')


@dataclass(frozen=True)
class UpdateResult:
    current_version: str
    latest_tag: str
    upgraded: bool
    synced_scaffold: bool
    sync_skipped_reason: str | None = None


def parse_semver(text: str) -> tuple[int, int, int]:
    match = SEMVER_PATTERN.fullmatch(str(text).strip())
    if match is None:
        raise ValueError(f"invalid semver '{text}'")
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
    )


def _format_semver(version: tuple[int, int, int]) -> str:
    return f"{version[0]}.{version[1]}.{version[2]}"


def _read_local_pyproject_version() -> str:
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject_path.exists():
        raise RuntimeError("autolab package metadata is unavailable")
    for raw_line in pyproject_path.read_text(encoding="utf-8").splitlines():
        match = PYPROJECT_VERSION_PATTERN.match(raw_line.strip())
        if match is not None:
            return match.group("version")
    raise RuntimeError("pyproject.toml does not define project version")


def get_installed_version() -> tuple[int, int, int]:
    try:
        raw_version = importlib_metadata.version("autolab")
    except importlib_metadata.PackageNotFoundError:
        raw_version = _read_local_pyproject_version()

    try:
        return parse_semver(raw_version)
    except ValueError as exc:
        raise RuntimeError(
            f"installed autolab version is not stable semver: {raw_version}"
        ) from exc


def fetch_latest_stable_tag(repo_url: str) -> str:
    result = subprocess.run(
        ["git", "ls-remote", "--tags", "--refs", repo_url],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        if not detail:
            detail = "git ls-remote failed"
        raise RuntimeError(f"unable to query release tags: {detail}")

    stable_tags: list[str] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        fields = line.split()
        if len(fields) < 2:
            continue
        ref = fields[-1].strip()
        if not ref.startswith("refs/tags/"):
            continue
        tag = ref.removeprefix("refs/tags/")
        if STABLE_TAG_PATTERN.fullmatch(tag):
            stable_tags.append(tag)

    if not stable_tags:
        raise RuntimeError(
            f"no stable release tags found for {repo_url} (expected vX.Y.Z)"
        )

    return max(stable_tags, key=parse_semver)


def build_git_install_spec(repo_url: str, tag: str) -> str:
    normalized_repo_url = str(repo_url).strip().removeprefix("git+")
    normalized_tag = str(tag).strip()
    if not STABLE_TAG_PATTERN.fullmatch(normalized_tag):
        raise ValueError(f"invalid stable tag '{tag}'")
    return f"git+{normalized_repo_url}@{normalized_tag}"


def run_pip_upgrade(spec: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pip", "install", spec],
        check=False,
        capture_output=True,
        text=True,
    )


def run_scaffold_sync(*, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "autolab", "sync-scaffold", "--force"],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
    )


def _is_autolab_repo(candidate_repo_root: Path) -> bool:
    state_path = candidate_repo_root / ".autolab" / "state.json"
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    return autolab_dir.exists() and autolab_dir.is_dir()


def _discover_autolab_repo_root(cwd: Path) -> Path | None:
    resolved_cwd = cwd.expanduser().resolve()
    for candidate in (resolved_cwd, *resolved_cwd.parents):
        if _is_autolab_repo(candidate):
            return candidate
    return None


def _summarize_process_error(result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout or "").strip()
    if not detail:
        detail = "no subprocess output"
    if len(detail) > 500:
        detail = f"{detail[:497]}..."
    return detail


def run_update(cwd: Path) -> UpdateResult:
    current_version_tuple = get_installed_version()
    current_version = _format_semver(current_version_tuple)
    latest_tag = fetch_latest_stable_tag(DEFAULT_RELEASE_REPO_URL)
    latest_version_tuple = parse_semver(latest_tag)

    if latest_version_tuple <= current_version_tuple:
        return UpdateResult(
            current_version=current_version,
            latest_tag=latest_tag,
            upgraded=False,
            synced_scaffold=False,
        )

    install_spec = build_git_install_spec(DEFAULT_RELEASE_REPO_URL, latest_tag)
    upgrade_result = run_pip_upgrade(install_spec)
    if upgrade_result.returncode != 0:
        detail = _summarize_process_error(upgrade_result)
        raise RuntimeError(
            f"pip install failed with exit code {upgrade_result.returncode}: {detail}"
        )

    repo_root = _discover_autolab_repo_root(cwd)
    if repo_root is None:
        return UpdateResult(
            current_version=current_version,
            latest_tag=latest_tag,
            upgraded=True,
            synced_scaffold=False,
            sync_skipped_reason="outside repo",
        )

    sync_result = run_scaffold_sync(cwd=repo_root)
    if sync_result.returncode != 0:
        detail = _summarize_process_error(sync_result)
        raise RuntimeError(
            f"sync-scaffold failed with exit code {sync_result.returncode}: {detail}"
        )

    return UpdateResult(
        current_version=current_version,
        latest_tag=latest_tag,
        upgraded=True,
        synced_scaffold=True,
    )
