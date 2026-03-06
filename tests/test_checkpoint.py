"""Tests for checkpoint management: create, list, restore, verify, and context-rot detection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autolab.utils import _write_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _setup_repo(tmp_path: Path, *, stage: str = "design") -> tuple[Path, Path]:
    """Create minimal repo + .autolab + state + iteration directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    autolab = repo / ".autolab"
    autolab.mkdir()
    state_path = autolab / "state.json"
    _write_json(
        state_path,
        {
            "stage": stage,
            "iteration_id": "iter-01",
            "experiment_id": "exp-01",
            "stage_attempt": 0,
            "max_stage_attempts": 3,
            "max_total_iterations": 5,
            "last_run_id": "",
        },
    )
    # Create iteration directory with some artifacts
    iteration_dir = repo / "experiments" / "plan" / "iter-01"
    iteration_dir.mkdir(parents=True)
    return repo, state_path


def _write_artifact(iteration_dir: Path, rel: str, content: str = "test") -> Path:
    p = iteration_dir / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# TestBuildCheckpointId
# ---------------------------------------------------------------------------


class TestBuildCheckpointId:
    def test_id_format(self):
        from autolab.checkpoint import _build_checkpoint_id

        cp_id = _build_checkpoint_id("design", "")
        assert cp_id.startswith("cp_")
        assert "_design_auto" in cp_id

    def test_label_sanitization(self):
        from autolab.checkpoint import _build_checkpoint_id

        cp_id = _build_checkpoint_id("hypothesis", "my label/with:special chars!")
        assert "my_label_with_special_chars_" in cp_id
        assert "/" not in cp_id
        assert ":" not in cp_id

    def test_label_truncation(self):
        from autolab.checkpoint import _build_checkpoint_id

        long_label = "a" * 100
        cp_id = _build_checkpoint_id("design", long_label)
        # label portion should be at most 40 chars + _<6hex> suffix
        parts = cp_id.split("_design_", 1)
        assert len(parts) == 2
        # 40 chars label + _ + 6 hex = 47 max
        assert len(parts[1]) <= 47


# ---------------------------------------------------------------------------
# TestResolveRevisionLabel
# ---------------------------------------------------------------------------


class TestResolveRevisionLabel:
    def test_unversioned_fallback(self, tmp_path):
        from autolab.checkpoint import _resolve_revision_label

        result = _resolve_revision_label(tmp_path)
        assert result == "unversioned-worktree"


# ---------------------------------------------------------------------------
# TestCollectCanonicalArtifacts
# ---------------------------------------------------------------------------


class TestCollectCanonicalArtifacts:
    def test_hypothesis_stage(self, tmp_path):
        from autolab.checkpoint import _collect_canonical_artifacts

        repo, _ = _setup_repo(tmp_path)
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md", "# Hypothesis")

        result = _collect_canonical_artifacts(
            repo, iteration_dir, "hypothesis", "experiment"
        )
        paths = [r[1] for r in result]
        assert any("hypothesis.md" in p for p in paths)

    def test_design_includes_hypothesis(self, tmp_path):
        from autolab.checkpoint import _collect_canonical_artifacts

        repo, _ = _setup_repo(tmp_path)
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md")
        _write_artifact(iteration_dir, "design.yaml")

        result = _collect_canonical_artifacts(
            repo, iteration_dir, "design", "experiment"
        )
        paths = [r[1] for r in result]
        assert any("hypothesis.md" in p for p in paths)
        assert any("design.yaml" in p for p in paths)

    def test_optional_artifacts(self, tmp_path):
        from autolab.checkpoint import _collect_canonical_artifacts

        repo, _ = _setup_repo(tmp_path)
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md")
        _write_artifact(iteration_dir, "uat.md", "user acceptance test")

        result = _collect_canonical_artifacts(
            repo, iteration_dir, "hypothesis", "experiment"
        )
        paths = [r[1] for r in result]
        assert any("uat.md" in p for p in paths)

    def test_missing_iteration_dir(self):
        from autolab.checkpoint import _collect_canonical_artifacts

        result = _collect_canonical_artifacts(
            Path("/tmp"), None, "design", "experiment"
        )
        assert result == []


# ---------------------------------------------------------------------------
# TestCreateCheckpoint
# ---------------------------------------------------------------------------


class TestCreateCheckpoint:
    def test_creates_directory_and_manifest(self, tmp_path):
        from autolab.checkpoint import create_checkpoint

        repo, state_path = _setup_repo(tmp_path, stage="design")
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md")
        _write_artifact(iteration_dir, "design.yaml")

        cp_id, cp_dir = create_checkpoint(
            repo, state_path=state_path, stage="design", trigger="manual", label="test"
        )

        assert cp_dir.exists()
        manifest_path = cp_dir / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["checkpoint_id"] == cp_id
        assert manifest["stage"] == "design"
        assert manifest["trigger"] == "manual"
        assert manifest["label"] == "test"
        assert isinstance(manifest["artifacts"], list)
        assert isinstance(manifest["state_snapshot"], dict)

    def test_index_updated(self, tmp_path):
        from autolab.checkpoint import create_checkpoint

        repo, state_path = _setup_repo(tmp_path)
        _write_artifact(repo / "experiments" / "plan" / "iter-01", "hypothesis.md")

        cp_id, _ = create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )

        index_path = repo / ".autolab" / "checkpoints" / "index.json"
        assert index_path.exists()
        index = json.loads(index_path.read_text())
        assert len(index["checkpoints"]) == 1
        assert index["checkpoints"][0]["checkpoint_id"] == cp_id

    def test_fingerprints_stored(self, tmp_path):
        from autolab.checkpoint import create_checkpoint

        repo, state_path = _setup_repo(tmp_path)
        _write_artifact(
            repo / "experiments" / "plan" / "iter-01", "hypothesis.md", "test content"
        )

        _, cp_dir = create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )

        manifest = json.loads((cp_dir / "manifest.json").read_text())
        for art in manifest["artifacts"]:
            assert "fingerprint" in art
            assert art["fingerprint"] not in ("", "<missing>")


# ---------------------------------------------------------------------------
# TestPruneAutoCheckpoints
# ---------------------------------------------------------------------------


class TestPruneAutoCheckpoints:
    def test_retention_limit(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, _prune_auto_checkpoints

        repo, state_path = _setup_repo(tmp_path)
        _write_artifact(repo / "experiments" / "plan" / "iter-01", "hypothesis.md")

        for i in range(5):
            create_checkpoint(
                repo,
                state_path=state_path,
                stage="hypothesis",
                trigger="auto",
                label=f"auto-{i}",
            )

        _prune_auto_checkpoints(repo / ".autolab", max_auto=3)

        index = json.loads(
            (repo / ".autolab" / "checkpoints" / "index.json").read_text()
        )
        auto_entries = [c for c in index["checkpoints"] if c.get("trigger") == "auto"]
        assert len(auto_entries) == 3

    def test_manual_preserved(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, _prune_auto_checkpoints

        repo, state_path = _setup_repo(tmp_path)
        _write_artifact(repo / "experiments" / "plan" / "iter-01", "hypothesis.md")

        create_checkpoint(
            repo,
            state_path=state_path,
            stage="hypothesis",
            trigger="manual",
            label="keep",
        )
        for i in range(5):
            create_checkpoint(
                repo,
                state_path=state_path,
                stage="hypothesis",
                trigger="auto",
                label=f"auto-{i}",
            )

        _prune_auto_checkpoints(repo / ".autolab", max_auto=2)

        index = json.loads(
            (repo / ".autolab" / "checkpoints" / "index.json").read_text()
        )
        manual = [c for c in index["checkpoints"] if c.get("trigger") == "manual"]
        assert len(manual) == 1


# ---------------------------------------------------------------------------
# TestListCheckpoints
# ---------------------------------------------------------------------------


class TestListCheckpoints:
    def test_empty(self, tmp_path):
        from autolab.checkpoint import list_checkpoints

        repo, _ = _setup_repo(tmp_path)
        assert list_checkpoints(repo) == []

    def test_sorting_desc(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, list_checkpoints

        repo, state_path = _setup_repo(tmp_path)
        _write_artifact(repo / "experiments" / "plan" / "iter-01", "hypothesis.md")

        create_checkpoint(
            repo,
            state_path=state_path,
            stage="hypothesis",
            trigger="manual",
            label="first",
        )
        create_checkpoint(
            repo,
            state_path=state_path,
            stage="hypothesis",
            trigger="manual",
            label="second",
        )

        result = list_checkpoints(repo)
        assert len(result) == 2
        # Most recent first
        assert (
            result[0].get("label") == "second"
            or result[0]["created_at"] >= result[1]["created_at"]
        )

    def test_filter_by_iteration(self, tmp_path):
        from autolab.checkpoint import list_checkpoints

        repo, _ = _setup_repo(tmp_path)
        index_path = repo / ".autolab" / "checkpoints" / "index.json"
        _write_json(
            index_path,
            {
                "schema_version": "1.0",
                "checkpoints": [
                    {
                        "checkpoint_id": "cp_1",
                        "created_at": "2024-01-01T00:00:00Z",
                        "stage": "hypothesis",
                        "trigger": "manual",
                        "iteration_id": "iter-01",
                        "artifact_count": 1,
                        "revision_label": "test",
                    },
                    {
                        "checkpoint_id": "cp_2",
                        "created_at": "2024-01-02T00:00:00Z",
                        "stage": "design",
                        "trigger": "manual",
                        "iteration_id": "iter-02",
                        "artifact_count": 2,
                        "revision_label": "test",
                    },
                ],
            },
        )

        result = list_checkpoints(repo, iteration_id="iter-01")
        assert len(result) == 1
        assert result[0]["checkpoint_id"] == "cp_1"


# ---------------------------------------------------------------------------
# TestVerifyCheckpoint
# ---------------------------------------------------------------------------


class TestVerifyCheckpoint:
    def test_valid_checkpoint(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, verify_checkpoint

        repo, state_path = _setup_repo(tmp_path)
        _write_artifact(
            repo / "experiments" / "plan" / "iter-01", "hypothesis.md", "test"
        )

        cp_id, _ = create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )

        valid, issues = verify_checkpoint(repo, cp_id)
        assert valid is True
        assert issues == []

    def test_tampered_file(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, verify_checkpoint

        repo, state_path = _setup_repo(tmp_path)
        _write_artifact(
            repo / "experiments" / "plan" / "iter-01", "hypothesis.md", "original"
        )

        cp_id, cp_dir = create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )

        # Tamper with the stored file
        stored = cp_dir / "files"
        for f in stored.rglob("hypothesis.md"):
            f.write_text("tampered!")

        valid, issues = verify_checkpoint(repo, cp_id)
        assert valid is False
        assert any("fingerprint mismatch" in i for i in issues)

    def test_missing_file(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, verify_checkpoint

        repo, state_path = _setup_repo(tmp_path)
        _write_artifact(repo / "experiments" / "plan" / "iter-01", "hypothesis.md")

        cp_id, cp_dir = create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )

        # Remove stored file
        for f in (cp_dir / "files").rglob("hypothesis.md"):
            f.unlink()

        valid, issues = verify_checkpoint(repo, cp_id)
        assert valid is False
        assert any("missing" in i for i in issues)


# ---------------------------------------------------------------------------
# TestArchiveArtifacts
# ---------------------------------------------------------------------------


class TestArchiveArtifacts:
    def test_archive_creation(self, tmp_path):
        from autolab.checkpoint import _archive_artifacts

        repo, _ = _setup_repo(tmp_path)
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        art = _write_artifact(iteration_dir, "hypothesis.md", "content")

        archive_dir = tmp_path / "archive"
        result = _archive_artifacts(
            repo,
            archive_dir,
            [(art, "experiments/plan/iter-01/hypothesis.md")],
        )

        assert result == archive_dir
        assert (archive_dir / "manifest.json").exists()
        assert (
            archive_dir / "files" / "experiments" / "plan" / "iter-01" / "hypothesis.md"
        ).exists()


# ---------------------------------------------------------------------------
# TestRestoreCheckpoint
# ---------------------------------------------------------------------------


class TestRestoreCheckpoint:
    def test_restore_artifacts(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, restore_checkpoint

        repo, state_path = _setup_repo(tmp_path, stage="hypothesis")
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md", "original content")

        cp_id, _ = create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )

        # Modify the artifact
        (iteration_dir / "hypothesis.md").write_text("modified content")

        success, message, changed = restore_checkpoint(
            repo, state_path, cp_id, archive_current=True
        )

        assert success is True
        assert "restored" in message
        assert (iteration_dir / "hypothesis.md").read_text() == "original content"

    def test_archive_before_restore(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, restore_checkpoint

        repo, state_path = _setup_repo(tmp_path, stage="hypothesis")
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md", "original")

        cp_id, _ = create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )
        _write_artifact(iteration_dir, "hypothesis.md", "modified")

        restore_checkpoint(repo, state_path, cp_id, archive_current=True)

        archive_dir = repo / ".autolab" / "reset_archive"
        assert archive_dir.exists()
        archives = list(archive_dir.iterdir())
        assert len(archives) == 1

    def test_state_reset(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, restore_checkpoint

        repo, state_path = _setup_repo(tmp_path, stage="hypothesis")
        _write_artifact(repo / "experiments" / "plan" / "iter-01", "hypothesis.md")

        cp_id, _ = create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )

        # Modify state
        state = json.loads(state_path.read_text())
        state["stage"] = "design"
        _write_json(state_path, state)

        restore_checkpoint(repo, state_path, cp_id)

        restored_state = json.loads(state_path.read_text())
        assert restored_state["stage"] == "hypothesis"


# ---------------------------------------------------------------------------
# TestRewindToStage
# ---------------------------------------------------------------------------


class TestRewindToStage:
    def test_rewind_delegates_to_restore(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, rewind_to_stage

        repo, state_path = _setup_repo(tmp_path, stage="design")
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md", "original")
        _write_artifact(iteration_dir, "design.yaml", "design content")

        create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )

        success, message, changed = rewind_to_stage(repo, state_path, "hypothesis")
        assert success is True

    def test_no_checkpoint_error(self, tmp_path):
        from autolab.checkpoint import rewind_to_stage

        repo, state_path = _setup_repo(tmp_path)

        success, message, changed = rewind_to_stage(repo, state_path, "hypothesis")
        assert success is False
        assert "no checkpoint found" in message


# ---------------------------------------------------------------------------
# TestTryAutoCheckpoint
# ---------------------------------------------------------------------------


class TestTryAutoCheckpoint:
    def test_successful_trigger(self, tmp_path):
        from autolab.checkpoint import try_auto_checkpoint

        repo, state_path = _setup_repo(tmp_path, stage="design")
        _write_artifact(repo / "experiments" / "plan" / "iter-01", "hypothesis.md")

        result = try_auto_checkpoint(
            repo,
            state_path=state_path,
            stage_before="hypothesis",
            stage_after="design",
            transitioned=True,
            agent_status="complete",
        )

        assert result is not None
        assert result.startswith("cp_")

    def test_skip_when_not_transitioned(self, tmp_path):
        from autolab.checkpoint import try_auto_checkpoint

        repo, state_path = _setup_repo(tmp_path)

        result = try_auto_checkpoint(
            repo,
            state_path=state_path,
            stage_before="hypothesis",
            stage_after="hypothesis",
            transitioned=False,
            agent_status="complete",
        )
        assert result is None

    def test_skip_on_failure(self, tmp_path):
        from autolab.checkpoint import try_auto_checkpoint

        repo, state_path = _setup_repo(tmp_path)

        result = try_auto_checkpoint(
            repo,
            state_path=state_path,
            stage_before="hypothesis",
            stage_after="design",
            transitioned=True,
            agent_status="failed",
        )
        assert result is None

    def test_skip_for_excluded_stages(self, tmp_path):
        from autolab.checkpoint import try_auto_checkpoint

        repo, state_path = _setup_repo(tmp_path)

        result = try_auto_checkpoint(
            repo,
            state_path=state_path,
            stage_before="launch",
            stage_after="slurm_monitor",
            transitioned=True,
            agent_status="complete",
        )
        assert result is None

    def test_non_propagation_of_exceptions(self, tmp_path):
        from autolab.checkpoint import try_auto_checkpoint

        # Non-existent repo should not raise
        result = try_auto_checkpoint(
            Path("/nonexistent"),
            state_path=Path("/nonexistent/state.json"),
            stage_before="hypothesis",
            stage_after="design",
            transitioned=True,
            agent_status="complete",
        )
        assert result is None


# ---------------------------------------------------------------------------
# TestDetectContextRot
# ---------------------------------------------------------------------------


class TestDetectContextRot:
    def test_no_rot_baseline(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, detect_context_rot

        repo, state_path = _setup_repo(tmp_path, stage="hypothesis")
        _write_artifact(
            repo / "experiments" / "plan" / "iter-01", "hypothesis.md", "content"
        )

        create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )

        result = detect_context_rot(repo, state_path=state_path)
        assert result["has_rot"] is False
        assert result["context_rot_flags"] == []

    def test_modified_artifact_detection(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, detect_context_rot

        repo, state_path = _setup_repo(tmp_path, stage="hypothesis")
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md", "original")

        create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )

        # Modify the artifact
        (iteration_dir / "hypothesis.md").write_text("modified")

        result = detect_context_rot(repo, state_path=state_path)
        assert result["has_rot"] is True
        assert any("modified" in f for f in result["context_rot_flags"])

    def test_stale_sidecar_detection(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, detect_context_rot

        repo, state_path = _setup_repo(tmp_path, stage="hypothesis")
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md")
        _write_artifact(
            iteration_dir, "context/sidecars/discuss.json", '{"test": true}'
        )

        create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )

        # Modify the sidecar
        (iteration_dir / "context" / "sidecars" / "discuss.json").write_text(
            '{"test": false}'
        )

        result = detect_context_rot(repo, state_path=state_path)
        assert result["has_rot"] is True
        assert any("stale" in f for f in result["context_rot_flags"])

    def test_no_checkpoints(self, tmp_path):
        from autolab.checkpoint import detect_context_rot

        repo, state_path = _setup_repo(tmp_path)
        result = detect_context_rot(repo, state_path=state_path)
        assert result["has_rot"] is False


# ---------------------------------------------------------------------------
# TestResetCLIIntegration
# ---------------------------------------------------------------------------


class TestResetCLIIntegration:
    def test_no_to_preserves_existing_behavior(self, tmp_path):
        """Without --to, the existing hard-reset behavior should work."""
        # This test validates that the original code path is preserved.
        # Full CLI test requires scaffold source, so we just check the argparse.
        from autolab.cli.parser import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["reset"])
        assert getattr(args, "to", "") == ""
        assert getattr(args, "archive_only", False) is False

    def test_to_checkpoint_arg(self):
        from autolab.cli.parser import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["reset", "--to", "checkpoint:cp_test_123"])
        assert args.to == "checkpoint:cp_test_123"

    def test_to_stage_arg(self):
        from autolab.cli.parser import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["reset", "--to", "stage:design"])
        assert args.to == "stage:design"

    def test_archive_only_arg(self):
        from autolab.cli.parser import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["reset", "--to", "stage:design", "--archive-only"])
        assert args.archive_only is True


# ---------------------------------------------------------------------------
# TestCheckpointCLISmoke
# ---------------------------------------------------------------------------


class TestCheckpointCLISmoke:
    def test_checkpoint_create_parser(self):
        from autolab.cli.parser import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["checkpoint", "create", "--label", "test"])
        assert args.label == "test"
        assert hasattr(args, "handler")

    def test_checkpoint_list_parser(self):
        from autolab.cli.parser import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["checkpoint", "list", "--json"])
        assert args.json is True

    def test_checkpoint_list_json_output(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, list_checkpoints

        repo, state_path = _setup_repo(tmp_path)
        _write_artifact(repo / "experiments" / "plan" / "iter-01", "hypothesis.md")

        create_checkpoint(
            repo,
            state_path=state_path,
            stage="hypothesis",
            trigger="manual",
            label="test",
        )

        result = list_checkpoints(repo)
        assert len(result) == 1
        # Verify it's JSON serializable
        json_str = json.dumps(result)
        parsed = json.loads(json_str)
        assert len(parsed) == 1


# ---------------------------------------------------------------------------
# TestHooksInstall
# ---------------------------------------------------------------------------


class TestHooksInstall:
    def test_hooks_install_parser(self):
        from autolab.cli.parser import _build_parser

        parser = _build_parser()
        args = parser.parse_args(["hooks", "install"])
        assert hasattr(args, "handler")

    def test_generate_hook_content(self):
        from autolab.cli.handlers_checkpoint import _generate_post_commit_hook

        content = _generate_post_commit_hook()
        assert "#!/usr/bin/env bash" in content
        assert "autolab.checkpoint_hook" in content
        assert "VIRTUAL_ENV" in content
        assert "set -e" not in content


# ---------------------------------------------------------------------------
# TestPathSafety — security: path traversal and symlink protection
# ---------------------------------------------------------------------------


class TestPathSafety:
    def test_safe_rel_path_rejects_traversal(self):
        from autolab.checkpoint import _safe_rel_path

        base = Path("/repo")
        assert _safe_rel_path(base, "../../etc/passwd") is None
        assert _safe_rel_path(base, "/absolute/path") is None
        assert _safe_rel_path(base, "valid/relative/path.txt") is not None

    def test_safe_rel_path_rejects_backslash(self):
        from autolab.checkpoint import _safe_rel_path

        assert _safe_rel_path(Path("/repo"), "foo\\bar") is None

    def test_validate_checkpoint_id(self):
        from autolab.checkpoint import _validate_checkpoint_id

        assert _validate_checkpoint_id("cp_20240101_design_auto_abc123") is True
        assert _validate_checkpoint_id("") is False
        assert _validate_checkpoint_id("../../etc") is False
        assert _validate_checkpoint_id("cp_normal") is True
        assert _validate_checkpoint_id("not_cp_prefixed") is False

    def test_restore_rejects_invalid_checkpoint_id(self, tmp_path):
        from autolab.checkpoint import restore_checkpoint

        repo, state_path = _setup_repo(tmp_path)
        success, msg, _ = restore_checkpoint(repo, state_path, "../../escape")
        assert success is False
        assert "invalid" in msg

    def test_prune_skips_empty_checkpoint_id(self, tmp_path):
        from autolab.checkpoint import _prune_auto_checkpoints

        repo, _ = _setup_repo(tmp_path)
        autolab_dir = repo / ".autolab"
        index_path = autolab_dir / "checkpoints" / "index.json"
        # Create index with an empty checkpoint_id entry
        _write_json(
            index_path,
            {
                "schema_version": "1.0",
                "checkpoints": [
                    {
                        "checkpoint_id": "",
                        "trigger": "auto",
                        "created_at": f"2024-01-{i:02d}T00:00:00Z",
                    }
                    for i in range(25)
                ],
            },
        )
        # Should not crash or delete the checkpoints directory
        _prune_auto_checkpoints(autolab_dir, max_auto=3)
        assert (autolab_dir / "checkpoints").is_dir()

    def test_collect_skips_symlinks(self, tmp_path):
        from autolab.checkpoint import _collect_canonical_artifacts

        repo, _ = _setup_repo(tmp_path)
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        # Create a real file and a symlink
        _write_artifact(iteration_dir, "design.yaml", "real content")
        symlink_path = iteration_dir / "hypothesis.md"
        symlink_path.symlink_to("/etc/passwd")

        result = _collect_canonical_artifacts(
            repo, iteration_dir, "design", "experiment"
        )
        paths = [r[1] for r in result]
        assert any("design.yaml" in p for p in paths)
        assert not any("hypothesis.md" in p for p in paths)


# ---------------------------------------------------------------------------
# TestRestoreCheckpointEdgeCases
# ---------------------------------------------------------------------------


class TestRestoreCheckpointEdgeCases:
    def test_restore_nonexistent_checkpoint(self, tmp_path):
        from autolab.checkpoint import restore_checkpoint

        repo, state_path = _setup_repo(tmp_path)
        success, msg, changed = restore_checkpoint(
            repo, state_path, "cp_does_not_exist"
        )
        assert success is False
        assert "not found" in msg

    def test_restore_refuses_tampered_checkpoint(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, restore_checkpoint

        repo, state_path = _setup_repo(tmp_path, stage="hypothesis")
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md", "original")

        cp_id, cp_dir = create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )
        for f in (cp_dir / "files").rglob("hypothesis.md"):
            f.write_text("tampered!")

        success, msg, _ = restore_checkpoint(repo, state_path, cp_id)
        assert success is False
        assert "verification failed" in msg

    def test_restore_without_archive(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, restore_checkpoint

        repo, state_path = _setup_repo(tmp_path, stage="hypothesis")
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md", "original")

        cp_id, _ = create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )
        (iteration_dir / "hypothesis.md").write_text("modified")

        success, _, _ = restore_checkpoint(
            repo, state_path, cp_id, archive_current=False
        )
        assert success is True
        assert not (repo / ".autolab" / "reset_archive").exists()

    def test_restore_with_empty_state_snapshot(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, restore_checkpoint

        repo, state_path = _setup_repo(tmp_path, stage="hypothesis")
        _write_artifact(repo / "experiments" / "plan" / "iter-01", "hypothesis.md")

        cp_id, cp_dir = create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )
        # Empty the state_snapshot in the manifest
        manifest = json.loads((cp_dir / "manifest.json").read_text())
        manifest["state_snapshot"] = {}
        _write_json(cp_dir / "manifest.json", manifest)

        state_before = json.loads(state_path.read_text())
        _write_json(state_path, {**state_before, "stage": "design"})

        restore_checkpoint(repo, state_path, cp_id)
        # State should remain as "design" since snapshot was empty
        assert json.loads(state_path.read_text())["stage"] == "design"


# ---------------------------------------------------------------------------
# TestVerifyCheckpointEdgeCases
# ---------------------------------------------------------------------------


class TestVerifyCheckpointEdgeCases:
    def test_verify_missing_manifest(self, tmp_path):
        from autolab.checkpoint import verify_checkpoint

        repo, _ = _setup_repo(tmp_path)
        (repo / ".autolab" / "checkpoints" / "cp_empty").mkdir(parents=True)
        valid, issues = verify_checkpoint(repo, "cp_empty")
        assert valid is False
        assert any("missing" in i for i in issues)

    def test_verify_invalid_checkpoint_id(self, tmp_path):
        from autolab.checkpoint import verify_checkpoint

        repo, _ = _setup_repo(tmp_path)
        valid, issues = verify_checkpoint(repo, "../../escape")
        assert valid is False
        assert any("invalid" in i for i in issues)


# ---------------------------------------------------------------------------
# TestCheckpointHook
# ---------------------------------------------------------------------------


class TestCheckpointHook:
    def test_read_version_from_pyproject(self, tmp_path):
        from autolab.checkpoint_hook import _read_version

        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "1.2.3"\n')
        assert _read_version(tmp_path) == "1.2.3"

    def test_read_version_from_setup_cfg(self, tmp_path):
        from autolab.checkpoint_hook import _read_version

        (tmp_path / "setup.cfg").write_text("[metadata]\nversion = 4.5.6\n")
        assert _read_version(tmp_path) == "4.5.6"

    def test_read_version_from_autolab_version(self, tmp_path):
        from autolab.checkpoint_hook import _read_version

        (tmp_path / ".autolab").mkdir()
        (tmp_path / ".autolab" / "version").write_text("7.8.9\n")
        assert _read_version(tmp_path) == "7.8.9"

    def test_read_version_none_found(self, tmp_path):
        from autolab.checkpoint_hook import _read_version

        assert _read_version(tmp_path) == ""

    def test_create_version_tag_empty_version(self, tmp_path):
        from autolab.checkpoint_hook import _create_version_tag

        # Should not raise with empty version
        _create_version_tag(tmp_path, "")

    def test_main_no_state_file(self, tmp_path, monkeypatch):
        from autolab.checkpoint_hook import main, _detect_repo_root

        monkeypatch.setattr(
            "autolab.checkpoint_hook._detect_repo_root", lambda: tmp_path
        )
        main()  # Should return without error


# ---------------------------------------------------------------------------
# TestDetectContextRotEdgeCases
# ---------------------------------------------------------------------------


class TestDetectContextRotEdgeCases:
    def test_rot_on_deleted_artifact(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, detect_context_rot

        repo, state_path = _setup_repo(tmp_path, stage="hypothesis")
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md", "content")
        create_checkpoint(
            repo, state_path=state_path, stage="hypothesis", trigger="manual"
        )
        (iteration_dir / "hypothesis.md").unlink()
        result = detect_context_rot(repo, state_path=state_path)
        assert result["has_rot"] is True
        assert any("missing" in f for f in result["context_rot_flags"])

    def test_rot_produces_rewind_targets(self, tmp_path):
        from autolab.checkpoint import create_checkpoint, detect_context_rot

        repo, state_path = _setup_repo(tmp_path, stage="design")
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md", "orig")
        _write_artifact(iteration_dir, "design.yaml", "orig")
        create_checkpoint(repo, state_path=state_path, stage="design", trigger="manual")
        (iteration_dir / "design.yaml").write_text("changed")
        result = detect_context_rot(repo, state_path=state_path)
        assert result["has_rot"] is True
        assert "recommended_rewind_targets" in result
        assert "hypothesis" in result["recommended_rewind_targets"]
        assert "design" in result["recommended_rewind_targets"]

    def test_consistent_return_shape(self, tmp_path):
        from autolab.checkpoint import detect_context_rot

        repo, state_path = _setup_repo(tmp_path)
        result = detect_context_rot(repo, state_path=state_path)
        # All branches must return the same keys
        assert "has_rot" in result
        assert "context_rot_flags" in result
        assert "artifact_drift_summary" in result
        assert "recommended_rewind_targets" in result


# ---------------------------------------------------------------------------
# TestFullLifecycle — integration test
# ---------------------------------------------------------------------------


class TestFullLifecycle:
    def test_create_list_modify_restore_verify(self, tmp_path):
        from autolab.checkpoint import (
            create_checkpoint,
            list_checkpoints,
            restore_checkpoint,
            verify_checkpoint,
        )

        repo, state_path = _setup_repo(tmp_path, stage="hypothesis")
        iteration_dir = repo / "experiments" / "plan" / "iter-01"
        _write_artifact(iteration_dir, "hypothesis.md", "original")

        # Create
        cp_id, _ = create_checkpoint(
            repo,
            state_path=state_path,
            stage="hypothesis",
            trigger="manual",
            label="v1",
        )

        # List
        cps = list_checkpoints(repo)
        assert len(cps) == 1
        assert cps[0]["checkpoint_id"] == cp_id

        # Verify
        valid, issues = verify_checkpoint(repo, cp_id)
        assert valid is True

        # Modify
        (iteration_dir / "hypothesis.md").write_text("modified")
        state = json.loads(state_path.read_text())
        state["stage"] = "design"
        _write_json(state_path, state)

        # Restore
        success, msg, changed = restore_checkpoint(repo, state_path, cp_id)
        assert success is True
        assert (iteration_dir / "hypothesis.md").read_text() == "original"
        assert json.loads(state_path.read_text())["stage"] == "hypothesis"

        # Checkpoint still listed
        cps_after = list_checkpoints(repo)
        assert len(cps_after) == 1
