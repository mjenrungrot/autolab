"""Git hook entry point for auto-checkpoint and version tagging."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def _detect_repo_root() -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except Exception:
        pass
    return None


def _read_version(repo_root: Path) -> str:
    """Read version from pyproject.toml, setup.cfg, or .autolab/version."""
    pyproject = repo_root / "pyproject.toml"
    if pyproject.exists():
        try:
            text = pyproject.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("version") and "=" in stripped:
                    val = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
        except Exception:
            pass

    setup_cfg = repo_root / "setup.cfg"
    if setup_cfg.exists():
        try:
            text = setup_cfg.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("version") and "=" in stripped:
                    val = stripped.split("=", 1)[1].strip()
                    if val:
                        return val
        except Exception:
            pass

    version_file = repo_root / ".autolab" / "version"
    if version_file.exists():
        try:
            return version_file.read_text(encoding="utf-8").strip()
        except Exception:
            pass

    return ""


def _create_version_tag(repo_root: Path, version: str) -> None:
    if not version:
        return
    tag = f"v{version}" if not version.startswith("v") else version
    try:
        result = subprocess.run(
            ["git", "tag", "-l", tag],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and tag in result.stdout.strip().splitlines():
            return
        subprocess.run(
            ["git", "tag", tag],
            cwd=str(repo_root),
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _get_commit_subject(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%s"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return ""


def main() -> None:
    repo_root = _detect_repo_root()
    if repo_root is None:
        return

    state_path = repo_root / ".autolab" / "state.json"
    if not state_path.exists():
        return

    version = _read_version(repo_root)
    _create_version_tag(repo_root, version)

    commit_subject = _get_commit_subject(repo_root)
    label = commit_subject[:40] if commit_subject else "commit"

    try:
        import json

        current_stage = ""
        try:
            state_data = json.loads(state_path.read_text(encoding="utf-8"))
            current_stage = str(state_data.get("stage", "")).strip()
        except Exception:
            pass

        from autolab.checkpoint import create_checkpoint

        create_checkpoint(
            repo_root,
            state_path=state_path,
            stage=current_stage,
            trigger="commit",
            label=label,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
