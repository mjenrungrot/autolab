from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("autolab.sidecar_context")

from autolab.sidecar_context import resolve_context_sidecars
from autolab.sidecar_tools import resolve_context_ref
from autolab.utils import _path_fingerprint


def _sidecar_item(item_id: str, text: str) -> dict[str, str]:
    return {
        "id": item_id,
        "summary": text,
        "detail": text,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_scope_policy(repo: Path, project_wide_root: str) -> None:
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        f"scope_roots:\n  project_wide_root: {project_wide_root}\n",
        encoding="utf-8",
    )


def _write_sidecar_payload(
    path: Path,
    *,
    sidecar_kind: str,
    scope_kind: str,
    scope_root: str,
    collection_name: str,
    items: list[dict[str, str]],
    derived_from: list[dict[str, str]] | None = None,
    stale_if: list[dict[str, str]] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "sidecar_kind": sidecar_kind,
        "scope_kind": scope_kind,
        "scope_root": scope_root,
        "generated_at": "2026-03-05T00:00:00Z",
        "locked_decisions": [],
        "preferences": [],
        "constraints": [],
        "open_questions": [],
        "promotion_candidates": [],
        "questions": [],
        "findings": [],
        "recommendations": [],
        "sources": [],
    }
    if scope_kind == "experiment":
        payload["iteration_id"] = "iter1"
        payload["experiment_id"] = "e1"
    payload[collection_name] = items
    if derived_from is not None:
        payload["derived_from"] = derived_from
    if stale_if is not None:
        payload["stale_if"] = stale_if
    _write_json(path, payload)


def _write_context_fixture(repo: Path) -> None:
    context_dir = repo / ".autolab" / "context"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    project_sidecar_dir = context_dir / "sidecars" / "project_wide"
    experiment_sidecar_dir = iteration_dir / "context" / "sidecars"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _write_scope_policy(repo, "src")

    _write_json(
        context_dir / "project_map.json",
        {
            "schema_version": "1.0",
            "generated_at": "2026-03-05T00:00:00Z",
            "scan_mode": "fast_heuristic",
            "repo_root": str(repo),
        },
    )
    _write_json(
        iteration_dir / "context_delta.json",
        {
            "schema_version": "1.0",
            "generated_at": "2026-03-05T00:00:00Z",
            "iteration_id": "iter1",
            "experiment_id": "e1",
            "changed_paths": ["src/model.py"],
        },
    )
    _write_json(
        context_dir / "bundle.json",
        {
            "schema_version": "1.0",
            "generated_at": "2026-03-05T00:00:00Z",
            "focus_iteration_id": "iter1",
            "focus_experiment_id": "e1",
            "project_map_path": ".autolab/context/project_map.json",
            "selected_experiment_delta_path": "experiments/plan/iter1/context_delta.json",
            "experiment_delta_maps": [
                {
                    "iteration_id": "iter1",
                    "experiment_id": "e1",
                    "path": "experiments/plan/iter1/context_delta.json",
                }
            ],
        },
    )
    _write_sidecar_payload(
        project_sidecar_dir / "discuss.json",
        sidecar_kind="discuss",
        scope_kind="project_wide",
        scope_root=str((repo / "src").resolve()),
        collection_name="preferences",
        items=[
            _sidecar_item("shared-discuss", "project-wide discuss shared baseline"),
            _sidecar_item("pw-discuss", "project-wide discuss only"),
        ],
    )
    _write_sidecar_payload(
        project_sidecar_dir / "research.json",
        sidecar_kind="research",
        scope_kind="project_wide",
        scope_root=str((repo / "src").resolve()),
        collection_name="findings",
        items=[
            _sidecar_item("shared-research", "project-wide research shared baseline"),
            _sidecar_item("pw-research", "project-wide research only"),
        ],
    )
    _write_sidecar_payload(
        experiment_sidecar_dir / "discuss.json",
        sidecar_kind="discuss",
        scope_kind="experiment",
        scope_root=str(iteration_dir.resolve()),
        collection_name="preferences",
        items=[
            _sidecar_item("shared-discuss", "experiment discuss override"),
            _sidecar_item("exp-discuss", "experiment discuss only"),
        ],
    )
    _write_sidecar_payload(
        experiment_sidecar_dir / "research.json",
        sidecar_kind="research",
        scope_kind="experiment",
        scope_root=str(iteration_dir.resolve()),
        collection_name="findings",
        items=[
            _sidecar_item("shared-research", "experiment research override"),
            _sidecar_item("exp-research", "experiment research only"),
        ],
    )


def _resolve_context(
    repo: Path,
    *,
    scope_kind: str,
) -> dict[str, Any]:
    return resolve_context_sidecars(
        repo,
        iteration_id="iter1",
        experiment_id="e1",
        scope_kind=scope_kind,
    )


def _items_by_id(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for value in payload.values():
        if not isinstance(value, list):
            continue
        for item in value:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id", "")).strip()
            if item_id:
                by_id[item_id] = item
    return by_id


def _assert_component_order_tokens(
    component_order: object, expected_tokens: list[tuple[str, ...]]
) -> None:
    assert isinstance(component_order, list)
    assert len(component_order) == len(expected_tokens)
    for actual, tokens in zip(component_order, expected_tokens, strict=True):
        actual_text = str(actual).lower()
        for token in tokens:
            assert token in actual_text


def test_project_wide_resolution_excludes_experiment_sidecars(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_context_fixture(repo)

    resolution = _resolve_context(repo, scope_kind="project_wide")

    assert resolution["scope_kind"] == "project_wide"
    assert resolution["scope_root"] == str((repo / "src").resolve())
    assert resolution["diagnostics"] == []
    assert [
        (row["component_id"], row["artifact_kind"], row["scope_kind"])
        for row in resolution["components"]
    ] == [
        ("project_map", "project_map", "project_wide"),
        ("project_wide_discuss", "discuss", "project_wide"),
        ("project_wide_research", "research", "project_wide"),
    ]
    _assert_component_order_tokens(
        resolution["component_order"],
        [("project_map",), ("project", "discuss"), ("project", "research")],
    )

    discuss_items = _items_by_id(resolution["effective_discuss"])
    research_items = _items_by_id(resolution["effective_research"])
    assert set(discuss_items) == {"shared-discuss", "pw-discuss"}
    assert set(research_items) == {"shared-research", "pw-research"}
    assert discuss_items["shared-discuss"]["source_scope_kind"] == "project_wide"
    assert research_items["shared-research"]["source_scope_kind"] == "project_wide"
    assert "experiment_discuss" not in resolution["compact_render"]
    assert "experiment_research" not in resolution["compact_render"]


def test_experiment_resolution_loads_project_and_experiment_layers(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_context_fixture(repo)

    resolution = _resolve_context(repo, scope_kind="experiment")

    assert resolution["scope_kind"] == "experiment"
    assert resolution["scope_root"] == str(
        (repo / "experiments" / "plan" / "iter1").resolve()
    )
    assert resolution["diagnostics"] == []
    assert [
        (row["component_id"], row["artifact_kind"], row["scope_kind"])
        for row in resolution["components"]
    ] == [
        ("project_map", "project_map", "project_wide"),
        ("project_wide_discuss", "discuss", "project_wide"),
        ("project_wide_research", "research", "project_wide"),
        ("context_delta", "context_delta", "experiment"),
        ("experiment_discuss", "discuss", "experiment"),
        ("experiment_research", "research", "experiment"),
    ]
    _assert_component_order_tokens(
        resolution["component_order"],
        [
            ("project_map",),
            ("project", "discuss"),
            ("project", "research"),
            ("context_delta",),
            ("experiment", "discuss"),
            ("experiment", "research"),
        ],
    )


def test_experiment_resolution_overlays_later_items_by_id(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_context_fixture(repo)

    resolution = _resolve_context(repo, scope_kind="experiment")

    discuss_items = _items_by_id(resolution["effective_discuss"])
    research_items = _items_by_id(resolution["effective_research"])
    assert discuss_items["shared-discuss"]["source_scope_kind"] == "experiment"
    assert research_items["shared-research"]["source_scope_kind"] == "experiment"
    assert str(discuss_items["shared-discuss"]["source_component_path"]).endswith(
        "experiments/plan/iter1/context/sidecars/discuss.json"
    )
    assert str(research_items["shared-research"]["source_component_path"]).endswith(
        "experiments/plan/iter1/context/sidecars/research.json"
    )
    assert discuss_items["shared-discuss"]["overridden_component_paths"] == [
        ".autolab/context/sidecars/project_wide/discuss.json"
    ]
    assert research_items["shared-research"]["overridden_component_paths"] == [
        ".autolab/context/sidecars/project_wide/research.json"
    ]


def test_scope_qualified_sidecar_refs_resolve_against_raw_addressed_scope(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_context_fixture(repo)

    resolution = _resolve_context(repo, scope_kind="experiment")

    project_ref = resolve_context_ref(
        repo,
        iteration_id="iter1",
        experiment_id="e1",
        raw_ref="project_wide:discuss:preferences:shared-discuss",
        scope_kind="experiment",
        context_resolution=resolution,
    )
    experiment_ref = resolve_context_ref(
        repo,
        iteration_id="iter1",
        experiment_id="e1",
        raw_ref="experiment:discuss:preferences:shared-discuss",
        scope_kind="experiment",
        context_resolution=resolution,
    )

    assert project_ref is not None
    assert experiment_ref is not None
    assert project_ref["scope_kind"] == "project_wide"
    assert experiment_ref["scope_kind"] == "experiment"
    assert project_ref["summary"] == "project-wide discuss shared baseline"
    assert experiment_ref["summary"] == "experiment discuss override"


def test_resolution_marks_stale_sidecars_when_dependency_fingerprints_change(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_context_fixture(repo)
    expected_fingerprint = _path_fingerprint(repo, ".autolab/context/project_map.json")
    _write_sidecar_payload(
        repo / ".autolab" / "context" / "sidecars" / "project_wide" / "research.json",
        sidecar_kind="research",
        scope_kind="project_wide",
        scope_root=str((repo / "src").resolve()),
        collection_name="findings",
        items=[
            _sidecar_item("shared-research", "project-wide research shared baseline"),
            _sidecar_item("pw-research", "project-wide research only"),
        ],
        derived_from=[
            {
                "path": ".autolab/context/project_map.json",
                "fingerprint": expected_fingerprint,
                "reason": "project_map",
            }
        ],
        stale_if=[
            {
                "path": ".autolab/context/project_map.json",
                "fingerprint": expected_fingerprint,
                "reason": "project_map",
            }
        ],
    )
    _write_json(
        repo / ".autolab" / "context" / "project_map.json",
        {
            "schema_version": "1.0",
            "generated_at": "2026-03-06T00:00:00Z",
            "scan_mode": "full_refresh",
            "repo_root": str(repo),
        },
    )

    resolution = _resolve_context(repo, scope_kind="project_wide")

    research_row = next(
        row
        for row in resolution["components"]
        if row["component_id"] == "project_wide_research"
    )
    assert research_row["derived_from"] == [
        {
            "path": ".autolab/context/project_map.json",
            "fingerprint": expected_fingerprint,
            "reason": "project_map",
        }
    ]
    assert research_row["stale"] is True
    assert research_row["status"] == "stale"
    assert research_row["selected"] is False
    assert research_row["stale_reasons"]
    assert any(
        ".autolab/context/project_map.json" in str(reason)
        for reason in research_row["stale_reasons"]
    )
    assert any("fingerprint changed" in str(item) for item in resolution["diagnostics"])


def test_resolution_ignores_bundle_focus_for_other_iteration(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_context_fixture(repo)
    _write_json(
        repo / "experiments" / "plan" / "iter2" / "context_delta.json",
        {
            "schema_version": "1.0",
            "generated_at": "2026-03-05T00:00:00Z",
            "iteration_id": "iter2",
            "experiment_id": "e2",
            "changed_paths": ["src/other_model.py"],
        },
    )
    _write_json(
        repo / ".autolab" / "context" / "bundle.json",
        {
            "schema_version": "1.0",
            "generated_at": "2026-03-05T00:00:00Z",
            "focus_iteration_id": "iter2",
            "focus_experiment_id": "e2",
            "project_map_path": ".autolab/context/project_map.json",
            "selected_experiment_delta_path": "experiments/plan/iter2/context_delta.json",
            "experiment_delta_maps": [
                {
                    "iteration_id": "iter2",
                    "experiment_id": "e2",
                    "path": "experiments/plan/iter2/context_delta.json",
                }
            ],
        },
    )

    resolution = _resolve_context(repo, scope_kind="experiment")

    context_delta_row = next(
        row
        for row in resolution["components"]
        if row["component_id"] == "context_delta"
    )
    assert context_delta_row["path"] == "experiments/plan/iter1/context_delta.json"
    assert context_delta_row["selected"] is True
    assert any(
        "does not match requested experiment" in str(item)
        for item in resolution["diagnostics"]
    )


def test_resolution_ignores_wrong_experiment_bundle_entry_for_same_iteration(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_context_fixture(repo)
    _write_json(
        repo / ".autolab" / "context" / "bundle.json",
        {
            "schema_version": "1.0",
            "generated_at": "2026-03-05T00:00:00Z",
            "focus_iteration_id": "iter1",
            "focus_experiment_id": "e2",
            "project_map_path": ".autolab/context/project_map.json",
            "selected_experiment_delta_path": "experiments/plan/iter1/context_delta.json",
            "experiment_delta_maps": [
                {
                    "iteration_id": "iter1",
                    "experiment_id": "e2",
                    "path": "experiments/plan/iter1/context_delta.json",
                }
            ],
        },
    )

    resolution = _resolve_context(repo, scope_kind="experiment")

    context_delta_row = next(
        row
        for row in resolution["components"]
        if row["component_id"] == "context_delta"
    )
    assert context_delta_row["path"] == "experiments/plan/iter1/context_delta.json"
    assert context_delta_row["selected"] is True
    assert any(
        "did not contain a matching experiment entry" in str(item)
        for item in resolution["diagnostics"]
    )


def test_resolution_rejects_fixed_sidecar_path_symlink_outside_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_context_fixture(repo)
    external_path = tmp_path / "external_research.json"
    _write_json(
        external_path,
        {
            "schema_version": "1.0",
            "sidecar_kind": "research",
            "scope_kind": "project_wide",
            "scope_root": str((repo / "src").resolve()),
            "generated_at": "2026-03-05T00:00:00Z",
            "questions": [],
            "findings": [{"id": "external", "summary": "outside repo"}],
            "recommendations": [],
            "sources": [],
        },
    )
    sidecar_path = (
        repo / ".autolab" / "context" / "sidecars" / "project_wide" / "research.json"
    )
    sidecar_path.unlink()
    try:
        sidecar_path.symlink_to(external_path)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are unavailable on this platform")

    resolution = _resolve_context(repo, scope_kind="project_wide")

    research_row = next(
        row
        for row in resolution["components"]
        if row["component_id"] == "project_wide_research"
    )
    assert research_row["selected"] is False
    assert research_row["status"] == "invalid"
    assert "shared research base is invalid" in research_row["selection_reason"]
    assert "path escapes repository root" in research_row["selection_reason"]
    assert research_row["stale_reasons"] == []


def test_resolution_rejects_project_wide_sidecar_with_experiment_identity(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_context_fixture(repo)
    sidecar_path = (
        repo / ".autolab" / "context" / "sidecars" / "project_wide" / "discuss.json"
    )
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload["iteration_id"] = "iter1"
    payload["experiment_id"] = "e1"
    sidecar_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    resolution = _resolve_context(repo, scope_kind="project_wide")

    discuss_row = next(
        row
        for row in resolution["components"]
        if row["component_id"] == "project_wide_discuss"
    )
    assert discuss_row["selected"] is False
    assert discuss_row["status"] == "invalid"
    assert "shared discuss base is invalid" in discuss_row["selection_reason"]
    assert (
        "iteration_id must be omitted for project-wide sidecars"
        in discuss_row["stale_reasons"]
    )
    assert (
        "experiment_id must be omitted for project-wide sidecars"
        in discuss_row["stale_reasons"]
    )


def test_resolution_rejects_experiment_sidecar_missing_identity(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_context_fixture(repo)
    sidecar_path = (
        repo
        / "experiments"
        / "plan"
        / "iter1"
        / "context"
        / "sidecars"
        / "research.json"
    )
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload.pop("iteration_id", None)
    payload.pop("experiment_id", None)
    sidecar_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    resolution = _resolve_context(repo, scope_kind="experiment")

    research_row = next(
        row
        for row in resolution["components"]
        if row["component_id"] == "experiment_research"
    )
    assert research_row["selected"] is False
    assert research_row["status"] == "invalid"
    assert (
        "experiment-local research overlay is invalid"
        in research_row["selection_reason"]
    )
    assert (
        "iteration_id must be non-empty for experiment sidecars"
        in research_row["stale_reasons"]
    )
    assert (
        "experiment_id must be non-empty for experiment sidecars"
        in research_row["stale_reasons"]
    )
