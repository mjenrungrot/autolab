"""Unit tests for policy_resolution module."""

from __future__ import annotations

import pytest

from autolab.policy_resolution import (
    _deep_merge_with_provenance,
    build_effective_artifact,
    derive_risk_flags,
    extract_overlay,
    resolve_effective_policy,
)


class TestDeepMergeWithProvenance:
    def test_scalar_last_wins(self):
        base = {"a": 1, "b": 2}
        overlay = {"b": 99}
        merged, keys = _deep_merge_with_provenance(base, overlay, "test")
        assert merged == {"a": 1, "b": 99}
        assert keys == ["b"]

    def test_dict_deep_merge(self):
        base = {"x": {"inner": 1, "keep": True}}
        overlay = {"x": {"inner": 2, "new": "val"}}
        merged, keys = _deep_merge_with_provenance(base, overlay, "test")
        assert merged["x"] == {"inner": 2, "keep": True, "new": "val"}
        assert "x" in keys

    def test_list_last_wins(self):
        base = {"items": [1, 2, 3]}
        overlay = {"items": [4, 5]}
        merged, keys = _deep_merge_with_provenance(base, overlay, "test")
        assert merged["items"] == [4, 5]
        assert "items" in keys

    def test_bool_last_wins(self):
        base = {"flag": True}
        overlay = {"flag": False}
        merged, keys = _deep_merge_with_provenance(base, overlay, "test")
        assert merged["flag"] is False
        assert "flag" in keys

    def test_no_change_no_keys(self):
        base = {"a": 1}
        overlay = {"a": 1}
        merged, keys = _deep_merge_with_provenance(base, overlay, "test")
        assert merged == {"a": 1}
        assert keys == []

    def test_new_key_added(self):
        base = {"a": 1}
        overlay = {"b": 2}
        merged, keys = _deep_merge_with_provenance(base, overlay, "test")
        assert merged == {"a": 1, "b": 2}
        assert keys == ["b"]


class TestResolveEffectivePolicy:
    def test_layer_ordering(self):
        scaffold = {"k": "scaffold"}
        preset = {"k": "preset"}
        host = {"k": "host"}
        scope = {"k": "scope"}
        stage = {"k": "stage"}
        risk = {"k": "risk"}
        repo = {"k": "repo"}
        merged, sources = resolve_effective_policy(
            scaffold, preset, host, scope, stage, risk, repo
        )
        # repo_local wins (last layer)
        assert merged["k"] == "repo"
        assert len(sources) == 7

    def test_empty_overlays_skip(self):
        scaffold = {"base": True}
        merged, sources = resolve_effective_policy(scaffold, {}, {}, {}, {}, {}, {})
        assert merged == {"base": True}
        assert len(sources) == 1  # only scaffold contributed

    def test_deep_merge_across_layers(self):
        scaffold = {"section": {"a": 1, "b": 2}}
        preset = {"section": {"b": 3, "c": 4}}
        merged, _ = resolve_effective_policy(scaffold, preset, {}, {}, {}, {}, {})
        assert merged["section"] == {"a": 1, "b": 3, "c": 4}

    def test_missing_overlays_graceful(self):
        merged, sources = resolve_effective_policy({}, {}, {}, {}, {}, {}, {})
        assert merged == {}
        assert sources == []


class TestExtractOverlay:
    def test_reads_nested_key(self):
        policy = {
            "policy_overlays": {
                "host": {
                    "slurm": {"timeout": 99},
                    "local": {},
                },
            },
        }
        assert extract_overlay(policy, "host", "slurm") == {"timeout": 99}
        assert extract_overlay(policy, "host", "local") == {}

    def test_missing_overlays_section(self):
        assert extract_overlay({}, "host", "slurm") == {}

    def test_missing_dimension(self):
        policy = {"policy_overlays": {"host": {}}}
        assert extract_overlay(policy, "scope", "project_wide") == {}

    def test_missing_key(self):
        policy = {"policy_overlays": {"host": {"local": {}}}}
        assert extract_overlay(policy, "host", "slurm") == {}

    def test_non_dict_entry(self):
        policy = {"policy_overlays": {"host": {"slurm": "not_a_dict"}}}
        assert extract_overlay(policy, "host", "slurm") == {}


class TestDeriveRiskFlags:
    def test_no_risk(self):
        flags = derive_risk_flags(
            host_mode="local",
            scope_kind="experiment",
            profile_mode="standalone",
            project_wide_unique_paths=[],
            uat_surface_patterns=["scripts/**"],
            plan_approval_required=False,
        )
        assert flags == {
            "plan_approval_required": False,
            "uat_required": False,
            "remote_profile_required": False,
        }

    def test_plan_approval_required(self):
        flags = derive_risk_flags(
            host_mode="local",
            scope_kind="experiment",
            profile_mode="standalone",
            project_wide_unique_paths=[],
            uat_surface_patterns=[],
            plan_approval_required=True,
        )
        assert flags["plan_approval_required"] is True

    def test_uat_required_project_wide_with_match(self):
        flags = derive_risk_flags(
            host_mode="local",
            scope_kind="project_wide",
            profile_mode="standalone",
            project_wide_unique_paths=["scripts/run.sh"],
            uat_surface_patterns=["scripts/**"],
            plan_approval_required=False,
        )
        assert flags["uat_required"] is True

    def test_uat_not_required_no_match(self):
        flags = derive_risk_flags(
            host_mode="local",
            scope_kind="project_wide",
            profile_mode="standalone",
            project_wide_unique_paths=["src/main.py"],
            uat_surface_patterns=["scripts/**", "docs/**"],
            plan_approval_required=False,
        )
        assert flags["uat_required"] is False

    def test_uat_not_required_experiment_scope(self):
        flags = derive_risk_flags(
            host_mode="local",
            scope_kind="experiment",
            profile_mode="standalone",
            project_wide_unique_paths=["scripts/run.sh"],
            uat_surface_patterns=["scripts/**"],
            plan_approval_required=False,
        )
        assert flags["uat_required"] is False

    def test_remote_profile_required(self):
        flags = derive_risk_flags(
            host_mode="slurm",
            scope_kind="experiment",
            profile_mode="standalone",
            project_wide_unique_paths=[],
            uat_surface_patterns=[],
            plan_approval_required=False,
        )
        assert flags["remote_profile_required"] is True

    def test_remote_profile_not_required_shared_fs(self):
        flags = derive_risk_flags(
            host_mode="slurm",
            scope_kind="experiment",
            profile_mode="shared_fs",
            project_wide_unique_paths=[],
            uat_surface_patterns=[],
            plan_approval_required=False,
        )
        assert flags["remote_profile_required"] is False

    def test_remote_profile_not_required_local(self):
        flags = derive_risk_flags(
            host_mode="local",
            scope_kind="experiment",
            profile_mode="standalone",
            project_wide_unique_paths=[],
            uat_surface_patterns=[],
            plan_approval_required=False,
        )
        assert flags["remote_profile_required"] is False

    def test_all_flags_active(self):
        flags = derive_risk_flags(
            host_mode="slurm",
            scope_kind="project_wide",
            profile_mode="standalone",
            project_wide_unique_paths=["docs/guide.md"],
            uat_surface_patterns=["docs/**"],
            plan_approval_required=True,
        )
        assert flags["plan_approval_required"] is True
        assert flags["uat_required"] is True
        assert flags["remote_profile_required"] is True


class TestBuildEffectiveArtifact:
    def test_basic_structure(self):
        artifact = build_effective_artifact(
            merged={"key": "val"},
            sources=[("preset", "local_dev", ["key"])],
            preset="local_dev",
            host_mode="local",
            scope_kind="experiment",
            stage="design",
            risk_flags={"plan_approval_required": False},
            generated_at="2026-01-01T00:00:00Z",
        )
        assert artifact["schema_version"] == "1.0"
        assert artifact["preset"] == "local_dev"
        assert artifact["host_mode"] == "local"
        assert artifact["scope_kind"] == "experiment"
        assert artifact["stage"] == "design"
        assert artifact["merged"] == {"key": "val"}
        assert len(artifact["sources"]) == 1
        assert artifact["sources"][0]["layer"] == "preset"
        assert artifact["risk_flags"]["plan_approval_required"] is False


class TestProvenance:
    def test_all_layers_recorded(self):
        merged, sources = resolve_effective_policy(
            {"a": 1},
            {"b": 2},
            {"c": 3},
            {"d": 4},
            {"e": 5},
            {"f": 6},
            {"g": 7},
        )
        layers = [s[0] for s in sources]
        assert "scaffold_default" in layers
        assert "preset" in layers
        assert "host" in layers
        assert "scope" in layers
        assert "stage" in layers
        assert "risk" in layers
        assert "repo_local" in layers

    def test_inactive_layers_not_recorded(self):
        merged, sources = resolve_effective_policy({"a": 1}, {}, {}, {}, {}, {}, {})
        layers = [s[0] for s in sources]
        assert layers == ["scaffold_default"]
