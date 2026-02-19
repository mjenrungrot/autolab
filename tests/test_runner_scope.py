"""Tests for runner scope checking — filesystem snapshot fallback.

Ensures that when the repository is not a git worktree, the filesystem
snapshot mechanism detects out-of-scope edits and the on_non_git_behavior
policy is respected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from autolab.runners import (
    _collect_filesystem_snapshot,
    _filesystem_snapshot_delta_paths,
    _is_within_scope,
    _looks_like_codex_sandbox_permission_failure,
)


def test_filesystem_snapshot_detects_new_file(tmp_path: Path) -> None:
    """A file created after baseline should appear in delta."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("pass\n")
    before = _collect_filesystem_snapshot(tmp_path)

    (tmp_path / "src" / "b.py").write_text("pass\n")
    after = _collect_filesystem_snapshot(tmp_path)

    delta = _filesystem_snapshot_delta_paths(before, after)
    assert "src/b.py" in delta


def test_filesystem_snapshot_detects_modified_file(tmp_path: Path) -> None:
    """A file whose mtime/size changes should appear in delta."""
    (tmp_path / "file.txt").write_text("hello\n")
    before = _collect_filesystem_snapshot(tmp_path)

    (tmp_path / "file.txt").write_text("hello world\n")
    after = _collect_filesystem_snapshot(tmp_path)

    delta = _filesystem_snapshot_delta_paths(before, after)
    assert "file.txt" in delta


def test_filesystem_snapshot_detects_deleted_file(tmp_path: Path) -> None:
    """A file removed after baseline should appear in delta."""
    (tmp_path / "gone.txt").write_text("bye\n")
    before = _collect_filesystem_snapshot(tmp_path)

    (tmp_path / "gone.txt").unlink()
    after = _collect_filesystem_snapshot(tmp_path)

    delta = _filesystem_snapshot_delta_paths(before, after)
    assert "gone.txt" in delta


def test_filesystem_snapshot_no_changes(tmp_path: Path) -> None:
    """No delta when nothing changes."""
    (tmp_path / "stable.txt").write_text("ok\n")
    before = _collect_filesystem_snapshot(tmp_path)
    after = _collect_filesystem_snapshot(tmp_path)

    delta = _filesystem_snapshot_delta_paths(before, after)
    assert delta == []


def test_is_within_scope_basic() -> None:
    assert _is_within_scope("src/foo.py", ("src", "docs"))
    assert _is_within_scope("docs/readme.md", ("src", "docs"))
    assert not _is_within_scope("other/bar.py", ("src", "docs"))


def test_scope_violation_detected_via_snapshot(tmp_path: Path) -> None:
    """End-to-end: snapshot detects out-of-scope edits."""
    allowed_roots = ("experiments/plan/iter1", "src")
    (tmp_path / "src").mkdir()
    (tmp_path / "experiments" / "plan" / "iter1").mkdir(parents=True)

    before = _collect_filesystem_snapshot(tmp_path)

    # Write an in-scope file and an out-of-scope file
    (tmp_path / "src" / "new.py").write_text("pass\n")
    (tmp_path / "secrets.env").write_text("SECRET=bad\n")

    after = _collect_filesystem_snapshot(tmp_path)
    delta = _filesystem_snapshot_delta_paths(before, after)
    out_of_scope = [p for p in delta if not _is_within_scope(p, allowed_roots)]

    assert "secrets.env" in out_of_scope
    assert all(not _is_within_scope(p, allowed_roots) for p in out_of_scope)


def test_detects_codex_sandbox_permission_failure_signature() -> None:
    stdout = "failed to queue rollout items: channel closed"
    stderr = "sandbox-exec: sandbox_apply: Operation not permitted"

    assert _looks_like_codex_sandbox_permission_failure(stdout, stderr) is True


def test_does_not_flag_unrelated_runner_failure_as_sandbox_issue() -> None:
    stdout = "runner exited with code 1"
    stderr = "network timeout while waiting for response"

    assert _looks_like_codex_sandbox_permission_failure(stdout, stderr) is False
