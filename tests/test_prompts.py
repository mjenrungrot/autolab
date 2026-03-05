from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import re

import pytest
import yaml

from autolab.constants import PROMPT_TOKEN_PATTERN
from autolab.models import StageCheckError
from autolab.prompts import (
    _parse_signed_delta,
    _suggest_decision_from_metrics,
    _target_comparison_text,
    _render_stage_prompt,
    _resolve_stage_prompt_path,
)
from autolab.utils import _path_fingerprint

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAFFOLD_PROMPTS_DIR = (
    _REPO_ROOT / "src" / "autolab" / "scaffold" / ".autolab" / "prompts"
)
_SKILLS_DIR = _REPO_ROOT / "src" / "autolab" / "skills"
_DOCS_DIR = _REPO_ROOT / "docs"
_ALL_PROMPT_MDS = sorted(_SCAFFOLD_PROMPTS_DIR.rglob("*.md"))

_ASCII_ENFORCED_MDS: list[Path] = []
_ASCII_ENFORCED_MDS.extend(_ALL_PROMPT_MDS)
_ASCII_ENFORCED_MDS.extend(sorted(_SKILLS_DIR.rglob("*.md")))
_readme = _REPO_ROOT / "README.md"
if _readme.exists():
    _ASCII_ENFORCED_MDS.append(_readme)

_TABLE_ENFORCED_MDS: list[Path] = []
if _readme.exists():
    _TABLE_ENFORCED_MDS.append(_readme)
_TABLE_ENFORCED_MDS.extend(_ALL_PROMPT_MDS)
if _DOCS_DIR.exists():
    _TABLE_ENFORCED_MDS.extend(sorted(_DOCS_DIR.rglob("*.md")))
if _SKILLS_DIR.exists():
    _TABLE_ENFORCED_MDS.extend(sorted(_SKILLS_DIR.rglob("*.md")))
_TABLE_ENFORCED_MDS = sorted(dict.fromkeys(_TABLE_ENFORCED_MDS))


def _copy_scaffold(repo: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "autolab"
        / "scaffold"
        / ".autolab"
    )
    target = repo / ".autolab"
    shutil.copytree(source, target, dirs_exist_ok=True)


def _install_codex_skill(repo: Path, skill_name: str) -> None:
    destination = repo / ".codex" / "skills" / skill_name / "SKILL.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        f"---\nname: {skill_name}\n---\n\n# {skill_name}\n",
        encoding="utf-8",
    )


def _write_state(repo: Path, *, stage: str) -> dict[str, object]:
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": stage,
        "stage_attempt": 0,
        "last_run_id": "",
        "pending_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state


def _write_backlog(repo: Path) -> None:
    backlog = {
        "hypotheses": [
            {
                "id": "h1",
                "status": "open",
                "title": "hypothesis",
                "success_metric": "accuracy",
                "target_delta": 0.1,
            }
        ],
        "experiments": [
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "iteration_id": "iter1",
            }
        ],
    }
    path = repo / ".autolab" / "backlog.yaml"
    path.write_text(yaml.safe_dump(backlog, sort_keys=False), encoding="utf-8")


def _set_stage_prompt_mapping(
    repo: Path, *, stage: str, mapping_key: str, mapping_value: str
) -> None:
    workflow_path = repo / ".autolab" / "workflow.yaml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)
    stages = workflow.get("stages")
    assert isinstance(stages, dict)
    stage_spec = stages.get(stage)
    assert isinstance(stage_spec, dict)
    stage_spec[mapping_key] = mapping_value
    workflow_path.write_text(
        yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8"
    )


def _require_sidecar_context_support() -> None:
    if importlib.util.find_spec("autolab.sidecar_context") is None:
        pytest.skip("sidecar context rollout not landed")


def _sidecar_item(item_id: str, text: str) -> dict[str, str]:
    return {
        "id": item_id,
        "summary": text,
        "detail": text,
    }


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
    payload: dict[str, object] = {
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_context_resolution_fixture(
    repo: Path,
    *,
    project_research_dependency_fingerprint: str | None = None,
    project_scope_root: str | None = None,
) -> None:
    context_dir = repo / ".autolab" / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    project_sidecar_dir = context_dir / "sidecars" / "project_wide"
    experiment_sidecar_dir = iteration_dir / "context" / "sidecars"

    (context_dir / "project_map.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "scan_mode": "fast_heuristic",
                "repo_root": str(repo),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (iteration_dir / "context_delta.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "iteration_id": "iter1",
                "experiment_id": "e1",
                "changed_paths": ["src/model.py"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (context_dir / "bundle.json").write_text(
        json.dumps(
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
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    _write_sidecar_payload(
        project_sidecar_dir / "discuss.json",
        sidecar_kind="discuss",
        scope_kind="project_wide",
        scope_root=project_scope_root or str(repo.resolve()),
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
        scope_root=project_scope_root or str(repo.resolve()),
        collection_name="findings",
        items=[
            _sidecar_item("shared-research", "project-wide research shared baseline"),
            _sidecar_item("pw-research", "project-wide research only"),
        ],
        derived_from=(
            [
                {
                    "path": ".autolab/context/project_map.json",
                    "fingerprint": project_research_dependency_fingerprint,
                    "reason": "project_map",
                }
            ]
            if project_research_dependency_fingerprint is not None
            else None
        ),
        stale_if=(
            [
                {
                    "path": ".autolab/context/project_map.json",
                    "fingerprint": project_research_dependency_fingerprint,
                    "reason": "project_map",
                }
            ]
            if project_research_dependency_fingerprint is not None
            else None
        ),
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


def _write_design_with_context_refs(repo: Path) -> None:
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    design = {
        "schema_version": "1.0",
        "id": "e1",
        "iteration_id": "iter1",
        "hypothesis_id": "h1",
        "entrypoint": {"module": "pkg.train", "args": {}},
        "compute": {"location": "local", "gpu_count": 0},
        "metrics": {
            "primary": {"name": "accuracy", "unit": "%", "mode": "maximize"},
            "secondary": [],
            "success_delta": "+0.1",
            "aggregation": "mean",
            "baseline_comparison": "vs baseline",
        },
        "baselines": [{"name": "baseline", "description": "existing"}],
        "implementation_requirements": [
            {
                "requirement_id": "R1",
                "description": "Keep experiment-local training path aligned with research.",
                "scope_kind": "experiment",
                "context_refs": [
                    "project_wide:research:findings:pw-research",
                    "experiment:research:findings:exp-research",
                ],
                "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
            },
            {
                "requirement_id": "R2",
                "description": "Promote the experiment preference into the shared parser contract.",
                "scope_kind": "project_wide",
                "promoted_constraints": [
                    {
                        "id": "pc1",
                        "source_ref": "experiment:discuss:preferences:exp-discuss",
                        "summary": "Respect the experiment-local discuss preference.",
                        "rationale": "Shared parser changes should preserve the chosen experiment workflow.",
                    }
                ],
                "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
            },
        ],
        "extract_parser": {
            "kind": "command",
            "command": "python -m tools.extract_results --run-id {run_id}",
        },
    }
    (iteration_dir / "design.yaml").write_text(
        yaml.safe_dump(design, sort_keys=False),
        encoding="utf-8",
    )


def _items_by_id(payload: object) -> dict[str, dict[str, object]]:
    if isinstance(payload, dict):
        raw_items: list[object] = []
        for value in payload.values():
            if isinstance(value, list):
                raw_items.extend(value)
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []

    by_id: dict[str, dict[str, object]] = {}
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or item.get("item_id") or "").strip()
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


def test_render_design_prompt_accepts_required_hypothesis_id(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="design")
    _write_backlog(repo)

    template_path = _resolve_stage_prompt_path(repo, "design", prompt_role="runner")
    bundle = _render_stage_prompt(
        repo, stage="design", state=state, template_path=template_path, runner_scope={}
    )

    assert "{{hypothesis_id}}" not in bundle.prompt_text
    assert "iteration_id=iter1" in bundle.prompt_text
    assert "iteration_path=experiments/plan/iter1" in bundle.prompt_text
    assert bundle.context_payload.get("hypothesis_id") == "h1"


def test_render_runner_prompt_replaces_empty_token_values_with_blank(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="design")
    _write_backlog(repo)

    template_path = repo / ".autolab" / "prompts" / "custom_runner_blank.md"
    template_path.write_text(
        (
            "# Stage: design (runner)\n\n"
            "## ROLE\nx\n\n"
            "## PRIMARY OBJECTIVE\nx\n\n"
            "task_context={{task_context}}\n"
        ),
        encoding="utf-8",
    )

    bundle = _render_stage_prompt(
        repo, stage="design", state=state, template_path=template_path, runner_scope={}
    )

    assert "task_context=" in bundle.prompt_text
    assert "unavailable:" not in bundle.prompt_text


def test_render_runner_prompt_rejects_unavailable_marker(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="design")
    _write_backlog(repo)

    template_path = repo / ".autolab" / "prompts" / "custom_runner_unavailable.md"
    template_path.write_text(
        (
            "# Stage: design (runner)\n\n"
            "## ROLE\nx\n\n"
            "## PRIMARY OBJECTIVE\nx\n\n"
            "paper_targets={{paper_targets}}\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(StageCheckError, match="disallowed sentinel marker"):
        _render_stage_prompt(
            repo,
            stage="design",
            state=state,
            template_path=template_path,
            runner_scope={},
        )


def test_render_runner_prompt_rejects_unknown_none_markers(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="design")
    _write_backlog(repo)

    template_path = repo / ".autolab" / "prompts" / "custom_runner_unknown.md"
    template_path.write_text(
        (
            "# Stage: design (runner)\n\n"
            "## ROLE\nx\n\n"
            "## PRIMARY OBJECTIVE\nx\n\n"
            "sync_status: unknown\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(StageCheckError, match="disallowed sentinel marker"):
        _render_stage_prompt(
            repo,
            stage="design",
            state=state,
            template_path=template_path,
            runner_scope={},
        )


def test_render_runner_prompt_does_not_auto_inject_audit_boilerplate_when_missing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="design")
    _write_backlog(repo)

    template_path = repo / ".autolab" / "prompts" / "custom_design_minimal.md"
    template_path.write_text(
        (
            "# Custom Design Prompt\n\n"
            "## ROLE\n"
            "Design owner.\n\n"
            "## PRIMARY OBJECTIVE\n"
            "Create design artifacts.\n\n"
            "## STEPS\n"
            "1. Produce `design.yaml`.\n"
        ),
        encoding="utf-8",
    )

    bundle = _render_stage_prompt(
        repo, stage="design", state=state, template_path=template_path, runner_scope={}
    )

    assert "## OUTPUTS (STRICT)" not in bundle.prompt_text
    assert "## FILE CHECKLIST" not in bundle.prompt_text
    assert "## VERIFIER MAPPING" not in bundle.prompt_text
    assert "## STEPS" in bundle.prompt_text


def test_render_prompt_manual_boilerplate_sections_override_auto_injection(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="design")
    _write_backlog(repo)

    template_path = repo / ".autolab" / "prompts" / "custom_design_manual.md"
    template_path.write_text(
        (
            "# Custom Design Prompt\n\n"
            "## ROLE\n"
            "Design owner.\n\n"
            "## PRIMARY OBJECTIVE\n"
            "Create design artifacts.\n\n"
            "## OUTPUTS (MANUAL)\n"
            "- manual outputs sentinel\n\n"
            "## REQUIRED INPUTS\n"
            "- manual inputs sentinel\n\n"
            "## FILE CHECKLIST (MACHINE-AUDITABLE)\n"
            "- manual checklist sentinel\n\n"
            "## VERIFIER MAPPING\n"
            "- manual verifier sentinel\n\n"
            "## STEPS\n"
            "1. Produce `design.yaml`.\n"
        ),
        encoding="utf-8",
    )

    bundle = _render_stage_prompt(
        repo, stage="design", state=state, template_path=template_path, runner_scope={}
    )

    assert "manual outputs sentinel" in bundle.prompt_text
    assert "manual inputs sentinel" in bundle.prompt_text
    assert "manual checklist sentinel" in bundle.prompt_text
    assert "manual verifier sentinel" in bundle.prompt_text
    assert "{{iteration_path}}/design.yaml" not in bundle.prompt_text

    assert len(re.findall(r"(?mi)^##\s*outputs\b", bundle.prompt_text)) == 1
    assert len(re.findall(r"(?mi)^##\s*required inputs\b", bundle.prompt_text)) == 1
    assert len(re.findall(r"(?mi)^##\s*file checklist\b", bundle.prompt_text)) == 1
    assert len(re.findall(r"(?mi)^##\s*verifier mapping\b", bundle.prompt_text)) == 1


def test_render_prompt_rejects_legacy_literal_placeholder_tokens(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="hypothesis")
    _write_backlog(repo)

    template_path = repo / ".autolab" / "prompts" / "custom_legacy.md"
    template_path.write_text("# bad\nlegacy token: <ITERATION_ID>\n", encoding="utf-8")

    with pytest.raises(StageCheckError, match="unresolved placeholders"):
        _render_stage_prompt(
            repo,
            stage="hypothesis",
            state=state,
            template_path=template_path,
            runner_scope={},
        )


@pytest.mark.parametrize(
    "stage",
    [
        "hypothesis",
        "design",
        "implementation",
        "implementation_review",
        "launch",
        "slurm_monitor",
        "extract_results",
        "update_docs",
        "decide_repeat",
        "human_review",
        "stop",
    ],
)
def test_render_scaffold_prompts_have_no_unresolved_tokens(
    tmp_path: Path, stage: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage=stage)
    if stage == "launch":
        state["pending_run_id"] = "20260101T000000Z_ab12cd"
    if stage in {"slurm_monitor", "extract_results", "update_docs", "decide_repeat"}:
        state["last_run_id"] = "run_001"
    _write_backlog(repo)

    template_path = _resolve_stage_prompt_path(repo, stage, prompt_role="runner")
    bundle = _render_stage_prompt(
        repo, stage=stage, state=state, template_path=template_path, runner_scope={}
    )

    unresolved_tokens = {
        match.group(1).strip()
        for match in PROMPT_TOKEN_PATTERN.finditer(bundle.prompt_text)
    }
    assert not unresolved_tokens
    assert "<ITERATION_ID>" not in bundle.prompt_text
    assert "## Runtime Stage Context" not in bundle.prompt_text
    assert bundle.context_payload.get("stage") == stage
    assert "rendered_packets" in bundle.context_payload


@pytest.mark.parametrize(
    "prompt_role,mapping_key",
    [
        ("runner", "runner_prompt_file"),
        ("audit", "prompt_file"),
        ("brief", "brief_prompt_file"),
        ("human", "human_prompt_file"),
    ],
)
def test_resolve_stage_prompt_requires_explicit_role_mapping(
    tmp_path: Path,
    prompt_role: str,
    mapping_key: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _set_stage_prompt_mapping(
        repo,
        stage="design",
        mapping_key=mapping_key,
        mapping_value="",
    )

    with pytest.raises(
        StageCheckError,
        match=rf"no stage prompt mapping is defined.*role '{prompt_role}'",
    ):
        _resolve_stage_prompt_path(repo, "design", prompt_role=prompt_role)


def test_resolve_runner_prompt_no_legacy_implementation_runner_fallback(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    (repo / ".autolab" / "workflow.yaml").unlink()
    (repo / ".autolab" / "prompts" / "stage_implementation.runner.md").unlink()
    assert (repo / ".autolab" / "prompts" / "stage_implementation_runner.md").exists()

    with pytest.raises(
        StageCheckError,
        match="stage prompt is missing for 'implementation' role 'runner'",
    ):
        _resolve_stage_prompt_path(repo, "implementation", prompt_role="runner")


def test_resolve_audit_prompt_no_legacy_single_file_fallback_without_registry(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    (repo / ".autolab" / "workflow.yaml").unlink()
    (repo / ".autolab" / "prompts" / "stage_design.audit.md").unlink()
    assert (repo / ".autolab" / "prompts" / "stage_design.md").exists()

    with pytest.raises(
        StageCheckError, match="stage prompt is missing for 'design' role 'audit'"
    ):
        _resolve_stage_prompt_path(repo, "design", prompt_role="audit")


@pytest.mark.parametrize("stage", ["slurm_monitor", "human_review", "stop"])
@pytest.mark.parametrize(
    "prompt_role,suffix",
    [("runner", "runner"), ("audit", "audit"), ("brief", "brief"), ("human", "human")],
)
def test_resolve_stage_prompt_paths_cover_slurm_and_terminal_stages(
    tmp_path: Path,
    stage: str,
    prompt_role: str,
    suffix: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)

    resolved = _resolve_stage_prompt_path(repo, stage, prompt_role=prompt_role)
    assert resolved.name == f"stage_{stage}.{suffix}.md"
    assert resolved.exists()


def test_render_implementation_prompt_includes_project_data_root_hints(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    media_path = repo / "data" / "curated_yt_drummers" / "clip.mp4"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_bytes(b"video")

    template_path = _resolve_stage_prompt_path(
        repo, "implementation", prompt_role="runner"
    )
    bundle = _render_stage_prompt(
        repo,
        stage="implementation",
        state=state,
        template_path=template_path,
        runner_scope={},
    )

    data_root = str((repo / "data").resolve())
    curated_root = str((repo / "data" / "curated_yt_drummers").resolve())
    project_data_roots = bundle.context_payload.get("project_data_roots", [])
    project_data_media_counts = bundle.context_payload.get("project_data_media_counts")
    assert data_root in project_data_roots
    assert curated_root in project_data_roots
    assert isinstance(project_data_media_counts, dict)
    assert curated_root in project_data_media_counts


def test_render_prompt_includes_codebase_context_bundle_paths_and_summaries(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    (repo / ".autolab" / "context").mkdir(parents=True, exist_ok=True)
    (repo / ".autolab" / "context" / "project_map.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "scan_mode": "fast_heuristic",
                "repo_root": str(repo),
                "stack": {"languages": ["python"], "manifests": [], "toolchains": []},
                "architecture": {
                    "top_level_dirs": ["src", "tests"],
                    "ci_workflows": [],
                    "discovered_experiments": [],
                },
                "conventions": {
                    "testing_frameworks": ["pytest"],
                    "linters": ["ruff"],
                    "formatters": [],
                    "package_managers": ["uv"],
                },
                "concerns": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    delta_path = "experiments/plan/iter1/context_delta.json"
    (repo / "experiments" / "plan" / "iter1").mkdir(parents=True, exist_ok=True)
    (repo / delta_path).write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "iteration_id": "iter1",
                "experiment_id": "e1",
                "inherits_project_map": ".autolab/context/project_map.json",
                "iteration_path": "experiments/plan/iter1",
                "experiment_type": "plan",
                "adds": {
                    "available_artifacts": [],
                    "assumptions": ["assume existing repo semantics"],
                    "concerns": [],
                    "latest_run": {"run_id": "", "status": "", "timestamp": ""},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / ".autolab" / "context" / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "scan_mode": "fast_heuristic",
                "project_map_path": ".autolab/context/project_map.json",
                "project_map_summary": "languages=python; experiments=1; concerns=0",
                "focus_iteration_id": "iter1",
                "focus_experiment_id": "e1",
                "selected_experiment_delta_path": delta_path,
                "selected_experiment_delta_summary": "iteration=iter1; type=plan; latest_run=none",
                "experiment_delta_maps": [
                    {
                        "iteration_id": "iter1",
                        "experiment_id": "e1",
                        "path": delta_path,
                        "summary": "iteration=iter1; type=plan; latest_run=none",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    template_path = _resolve_stage_prompt_path(
        repo, "implementation", prompt_role="runner"
    )
    bundle = _render_stage_prompt(
        repo,
        stage="implementation",
        state=state,
        template_path=template_path,
        runner_scope={},
    )

    assert (
        bundle.context_payload.get("codebase_project_map_path")
        == ".autolab/context/project_map.json"
    )
    assert (
        bundle.context_payload.get("codebase_experiment_delta_map_path") == delta_path
    )
    assert (
        bundle.context_payload.get("codebase_project_map_summary")
        == "languages=python; experiments=1; concerns=0"
    )
    assert (
        bundle.context_payload.get("codebase_experiment_delta_summary")
        == "iteration=iter1; type=plan; latest_run=none"
    )


def test_render_implementation_prompt_pack_metadata_and_texts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    template_path = _resolve_stage_prompt_path(
        repo, "implementation", prompt_role="runner"
    )
    bundle = _render_stage_prompt(
        repo,
        stage="implementation",
        state=state,
        template_path=template_path,
        runner_scope={},
        write_outputs=False,
    )

    assert bundle.rendered_path.name == "implementation.runner.md"
    assert bundle.context_path.name == "implementation.context.json"
    assert bundle.audit_path is not None
    assert bundle.audit_path.name == "implementation.audit.md"
    assert bundle.brief_path is not None
    assert bundle.brief_path.name == "implementation.brief.md"
    assert bundle.human_path is not None
    assert bundle.human_path.name == "implementation.human.md"
    assert "Implementation Auditor" in bundle.audit_text
    assert "Stage: implementation (brief)" in bundle.brief_text
    assert "Stage: implementation (human packet)" in bundle.human_text
    assert "rendered_audit_path" in bundle.context_payload
    assert "rendered_brief_path" in bundle.context_payload
    assert "rendered_human_path" in bundle.context_payload


@pytest.mark.parametrize(
    "prompt_role,suffix",
    [("audit", "audit"), ("brief", "brief"), ("human", "human")],
)
def test_render_stage_raises_when_sidecar_template_missing(
    tmp_path: Path,
    prompt_role: str,
    suffix: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="design")
    _write_backlog(repo)
    (repo / ".autolab" / "prompts" / f"stage_design.{suffix}.md").unlink()

    template_path = _resolve_stage_prompt_path(repo, "design", prompt_role="runner")
    with pytest.raises(StageCheckError, match=rf"role '{prompt_role}'"):
        _render_stage_prompt(
            repo,
            stage="design",
            state=state,
            template_path=template_path,
            runner_scope={},
            write_outputs=False,
        )


@pytest.mark.parametrize(
    "prompt_role,suffix",
    [("audit", "audit"), ("brief", "brief"), ("human", "human")],
)
def test_render_stage_raises_when_sidecar_template_has_invalid_include(
    tmp_path: Path,
    prompt_role: str,
    suffix: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="design")
    _write_backlog(repo)
    invalid_template_path = repo / ".autolab" / "prompts" / f"stage_design.{suffix}.md"
    invalid_template_path.write_text(
        "# broken\n{{shared:missing_sidecar_include.md}}\n",
        encoding="utf-8",
    )

    template_path = _resolve_stage_prompt_path(repo, "design", prompt_role="runner")
    with pytest.raises(
        StageCheckError, match="prompt shared include 'missing_sidecar_include.md'"
    ):
        _render_stage_prompt(
            repo,
            stage="design",
            state=state,
            template_path=template_path,
            runner_scope={},
            write_outputs=False,
        )


def test_render_implementation_brief_distills_blockers(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "review_result.json").write_text(
        json.dumps(
            {
                "status": "needs_retry",
                "blocking_findings": [
                    "Fix failing dry run in training loop.",
                    "Add missing validation evidence for task T2.",
                ],
                "required_checks": {
                    "tests": "pass",
                    "dry_run": "fail",
                    "schema": "pass",
                    "env_smoke": "pass",
                    "docs_target_update": "pass",
                },
                "reviewed_at": "2026-03-04T00:00:00Z",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (repo / ".autolab" / "verification_result.json").write_text(
        json.dumps(
            {
                "passed": False,
                "message": "verification failed: dry_run command returned non-zero",
                "details": {
                    "commands": [
                        {
                            "name": "dry_run",
                            "status": "fail",
                            "detail": "python -m pkg.train exited with code 1",
                        }
                    ]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    template_path = _resolve_stage_prompt_path(
        repo, "implementation", prompt_role="runner"
    )
    bundle = _render_stage_prompt(
        repo,
        stage="implementation",
        state=state,
        template_path=template_path,
        runner_scope={},
        write_outputs=False,
    )

    bullet_lines = [
        line for line in bundle.brief_text.splitlines() if line.startswith("- ")
    ]
    assert 3 <= len(bullet_lines) <= 7
    assert "Fix failing dry run in training loop." in bundle.brief_text
    assert "python -m pkg.train exited with code 1" in bundle.brief_text


_NON_ASCII_RE = re.compile(r"[^\x00-\x7F]")
_PIPE_TABLE_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)


def _find_pipe_table_headers(content: str) -> list[tuple[int, str]]:
    lines = content.splitlines()
    in_code_fence = False
    violations: list[tuple[int, str]] = []
    for idx, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if not _PIPE_TABLE_SEPARATOR_RE.match(line):
            continue
        header_idx = idx - 1
        while header_idx >= 0 and not lines[header_idx].strip():
            header_idx -= 1
        if header_idx < 0:
            continue
        header = lines[header_idx]
        if "|" not in header:
            continue
        violations.append((header_idx + 1, header.strip()))
    return violations


@pytest.mark.parametrize(
    "md_path",
    _ASCII_ENFORCED_MDS,
    ids=[str(p.relative_to(_REPO_ROOT)) for p in _ASCII_ENFORCED_MDS],
)
def test_prompt_markdown_contains_only_ascii(md_path: Path) -> None:
    """All prompt, skill, and README markdown files must use standard ASCII only (no Unicode)."""
    content = md_path.read_text(encoding="utf-8")
    violations: list[str] = []
    for lineno, line in enumerate(content.splitlines(), start=1):
        for match in _NON_ASCII_RE.finditer(line):
            char = match.group()
            violations.append(
                f"  {md_path.name}:{lineno} col {match.start() + 1}: "
                f"U+{ord(char):04X} {repr(char)}"
            )
    assert not violations, (
        f"Non-ASCII characters found in {md_path.name}:\n" + "\n".join(violations)
    )


@pytest.mark.parametrize(
    "md_path",
    _TABLE_ENFORCED_MDS,
    ids=[str(p.relative_to(_REPO_ROOT)) for p in _TABLE_ENFORCED_MDS],
)
def test_markdown_disallows_pipe_tables(md_path: Path) -> None:
    content = md_path.read_text(encoding="utf-8")
    violations = _find_pipe_table_headers(content)
    assert not violations, (
        f"Pipe-style Markdown tables are not allowed in {md_path.name}; "
        "use bullet records instead:\n"
        + "\n".join(
            f"  {md_path.name}:{lineno} -> {line_preview}"
            for lineno, line_preview in violations
        )
    )


# ---------------------------------------------------------------------------
# _parse_signed_delta tests (Item 3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        ("80 +5.0%", 5.0),
        ("2.0 -0.3", -0.3),
        ("5.0", 5.0),
        ("+10", 10.0),
        ("-2.5%", -2.5),
        ("", None),
    ],
)
def test_parse_signed_delta(text: str, expected: float | None) -> None:
    result = _parse_signed_delta(text)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert abs(result - expected) < 1e-6


# ---------------------------------------------------------------------------
# _suggest_decision_from_metrics tests (Item 1)
# ---------------------------------------------------------------------------


def _setup_metrics_repo(
    tmp_path: Path,
    *,
    hypothesis_target_delta: str = "5.0",
    metrics_delta: float = 6.0,
    metric_mode: str = "maximize",
) -> tuple[Path, dict[str, object]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "decide_repeat",
        "stage_attempt": 0,
        "last_run_id": "run_001",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    backlog = {
        "experiments": [
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "iteration_id": "iter1",
            }
        ],
    }
    (repo / ".autolab" / "backlog.yaml").write_text(
        yaml.safe_dump(backlog), encoding="utf-8"
    )

    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "hypothesis.md").write_text(
        f"# Hypothesis\n- target_delta: {hypothesis_target_delta}\n",
        encoding="utf-8",
    )
    design = {
        "schema_version": "1.0",
        "id": "e1",
        "iteration_id": "iter1",
        "hypothesis_id": "h1",
        "entrypoint": {"module": "x"},
        "compute": {"location": "local"},
        "metrics": {
            "primary": {"name": "accuracy", "unit": "%", "mode": metric_mode},
            "secondary": [],
            "success_delta": "+5.0",
        },
        "baselines": [{"name": "b", "description": "b"}],
    }
    (iteration_dir / "design.yaml").write_text(yaml.safe_dump(design), encoding="utf-8")
    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = {
        "status": "complete",
        "primary_metric": {
            "name": "accuracy",
            "value": 90.0,
            "delta_vs_baseline": metrics_delta,
        },
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
    return repo, state


def test_suggest_decision_from_metrics_returns_stop_when_target_met(
    tmp_path: Path,
) -> None:
    repo, state = _setup_metrics_repo(
        tmp_path, hypothesis_target_delta="5.0", metrics_delta=6.0
    )
    result, evidence = _suggest_decision_from_metrics(repo, state)
    assert result == "stop"
    assert isinstance(evidence, dict)
    assert "comparison" in evidence


def test_suggest_decision_from_metrics_returns_design_when_target_not_met(
    tmp_path: Path,
) -> None:
    repo, state = _setup_metrics_repo(
        tmp_path, hypothesis_target_delta="10.0", metrics_delta=6.0
    )
    result, evidence = _suggest_decision_from_metrics(repo, state)
    assert result == "design"
    assert isinstance(evidence, dict)
    assert "comparison" in evidence


# ---------------------------------------------------------------------------
# _target_comparison_text with metric_mode tests (Item 4)
# ---------------------------------------------------------------------------


def test_target_comparison_maximize_met() -> None:
    payload = {"primary_metric": {"name": "accuracy", "delta_vs_baseline": 6.0}}
    _comp, suggestion = _target_comparison_text(
        metrics_payload=payload,
        hypothesis_target_delta=5.0,
        design_target_delta="",
        run_id="run_001",
        metric_mode="maximize",
    )
    assert "stop" in suggestion


def test_target_comparison_maximize_not_met() -> None:
    payload = {"primary_metric": {"name": "accuracy", "delta_vs_baseline": 3.0}}
    _comp, suggestion = _target_comparison_text(
        metrics_payload=payload,
        hypothesis_target_delta=5.0,
        design_target_delta="",
        run_id="run_001",
        metric_mode="maximize",
    )
    assert "design" in suggestion


def test_target_comparison_minimize_met() -> None:
    payload = {"primary_metric": {"name": "loss", "delta_vs_baseline": -3.0}}
    _comp, suggestion = _target_comparison_text(
        metrics_payload=payload,
        hypothesis_target_delta=-2.0,
        design_target_delta="",
        run_id="run_001",
        metric_mode="minimize",
    )
    assert "stop" in suggestion


def test_target_comparison_minimize_not_met() -> None:
    payload = {"primary_metric": {"name": "loss", "delta_vs_baseline": -1.0}}
    _comp, suggestion = _target_comparison_text(
        metrics_payload=payload,
        hypothesis_target_delta=-2.0,
        design_target_delta="",
        run_id="run_001",
        metric_mode="minimize",
    )
    assert "design" in suggestion


def test_target_comparison_invalid_maximize_sign_returns_unavailable() -> None:
    payload = {"primary_metric": {"name": "accuracy", "delta_vs_baseline": 3.0}}
    comparison, suggestion = _target_comparison_text(
        metrics_payload=payload,
        hypothesis_target_delta=-1.0,
        design_target_delta="",
        run_id="run_001",
        metric_mode="maximize",
    )
    assert "invalid target_delta semantics" in comparison
    assert "human_review" in suggestion


def test_target_comparison_invalid_minimize_sign_returns_unavailable() -> None:
    payload = {"primary_metric": {"name": "loss", "delta_vs_baseline": -0.5}}
    comparison, suggestion = _target_comparison_text(
        metrics_payload=payload,
        hypothesis_target_delta=1.0,
        design_target_delta="",
        run_id="run_001",
        metric_mode="minimize",
    )
    assert "invalid target_delta semantics" in comparison
    assert "human_review" in suggestion


def test_suggest_decision_from_metrics_minimize_mode(tmp_path: Path) -> None:
    repo, state = _setup_metrics_repo(
        tmp_path,
        hypothesis_target_delta="-2.0",
        metrics_delta=-3.0,
        metric_mode="minimize",
    )
    result, evidence = _suggest_decision_from_metrics(repo, state)
    assert result == "stop"
    assert isinstance(evidence, dict)
    assert evidence.get("metric_mode") == "minimize"


def test_render_stage_prompt_includes_context_resolution_component_metadata(
    tmp_path: Path,
) -> None:
    _require_sidecar_context_support()
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_context_resolution_fixture(repo)
    state = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    template_path = _resolve_stage_prompt_path(
        repo, "implementation", prompt_role="runner"
    )
    bundle = _render_stage_prompt(
        repo,
        stage="implementation",
        state=state,
        template_path=template_path,
        runner_scope={
            "mode": "scope_root_plus_core",
            "scope_kind": "experiment",
            "scope_root": str((repo / "experiments" / "plan" / "iter1").resolve()),
            "project_wide_root": str(repo.resolve()),
            "workspace_dir": str((repo / "experiments" / "plan" / "iter1").resolve()),
            "allowed_edit_dirs": [],
        },
        write_outputs=False,
    )

    resolution = bundle.context_payload["context_resolution"]
    assert resolution["scope_kind"] == "experiment"
    assert resolution["scope_root"] == str(
        (repo / "experiments" / "plan" / "iter1").resolve()
    )
    assert resolution["diagnostics"] == []
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

    components = resolution["components"]
    assert [
        (row["component_id"], row["artifact_kind"], row["scope_kind"])
        for row in components
    ] == [
        ("project_map", "project_map", "project_wide"),
        ("project_wide_discuss", "discuss", "project_wide"),
        ("project_wide_research", "research", "project_wide"),
        ("context_delta", "context_delta", "experiment"),
        ("experiment_discuss", "discuss", "experiment"),
        ("experiment_research", "research", "experiment"),
    ]
    assert [row["precedence_index"] for row in components] == [0, 1, 2, 3, 4, 5]
    for row in components:
        assert set(row) >= {
            "component_id",
            "artifact_kind",
            "scope_kind",
            "path",
            "status",
            "selected",
            "selection_reason",
            "precedence_index",
            "fingerprint",
            "derived_from",
            "stale",
            "stale_reasons",
        }

    discuss_items = _items_by_id(resolution["effective_discuss"])
    research_items = _items_by_id(resolution["effective_research"])
    assert discuss_items["shared-discuss"]["source_scope_kind"] == "experiment"
    assert research_items["shared-research"]["source_scope_kind"] == "experiment"
    assert (
        discuss_items["shared-discuss"]["source_component_id"] == "experiment_discuss"
    )
    assert (
        research_items["shared-research"]["source_component_id"]
        == "experiment_research"
    )
    assert str(discuss_items["shared-discuss"]["source_component_path"]).endswith(
        "experiments/plan/iter1/context/sidecars/discuss.json"
    )
    assert str(research_items["shared-research"]["source_component_path"]).endswith(
        "experiments/plan/iter1/context/sidecars/research.json"
    )
    assert (
        "project_wide_discuss: artifact_kind=discuss status=loaded selected=yes"
        in resolution["compact_render"]
    )
    assert (
        "project_wide_research: artifact_kind=research status=loaded selected=yes"
        in resolution["compact_render"]
    )
    assert (
        "experiment_discuss: artifact_kind=discuss status=loaded selected=yes"
        in resolution["compact_render"]
    )
    assert (
        "experiment_research: artifact_kind=research status=loaded selected=yes"
        in resolution["compact_render"]
    )
    assert (
        "effective_discuss=pw-discuss,shared-discuss,exp-discuss"
        in resolution["compact_render"]
    )
    assert (
        "effective_research=pw-research,shared-research,exp-research"
        in resolution["compact_render"]
    )


def test_render_stage_prompt_context_resolution_surfaces_stale_dependency_hashes(
    tmp_path: Path,
) -> None:
    _require_sidecar_context_support()
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    (repo / "src").mkdir(exist_ok=True)
    _write_context_resolution_fixture(repo)
    expected_fingerprint = _path_fingerprint(repo, ".autolab/context/project_map.json")
    _write_sidecar_payload(
        repo / ".autolab" / "context" / "sidecars" / "project_wide" / "research.json",
        sidecar_kind="research",
        scope_kind="project_wide",
        scope_root=str(repo.resolve()),
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
    state = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    (repo / ".autolab" / "context" / "project_map.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-06T00:00:00Z",
                "scan_mode": "full_refresh",
                "repo_root": str(repo),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    template_path = _resolve_stage_prompt_path(
        repo, "implementation", prompt_role="runner"
    )
    bundle = _render_stage_prompt(
        repo,
        stage="implementation",
        state=state,
        template_path=template_path,
        runner_scope={
            "mode": "scope_root_plus_core",
            "scope_kind": "project_wide",
            "scope_root": str((repo / "src").resolve()),
            "project_wide_root": str((repo / "src").resolve()),
            "workspace_dir": str((repo / "src").resolve()),
            "allowed_edit_dirs": [],
        },
        write_outputs=False,
    )

    resolution = bundle.context_payload["context_resolution"]
    research_row = next(
        row
        for row in resolution["components"]
        if row["component_id"] == "project_wide_research"
        and row["artifact_kind"] == "research"
        and row["scope_kind"] == "project_wide"
    )
    assert research_row["stale"] is True
    assert research_row["derived_from"] == [
        {
            "path": ".autolab/context/project_map.json",
            "fingerprint": expected_fingerprint,
            "reason": "project_map",
        }
    ]
    assert research_row["stale_reasons"]
    assert any(
        ".autolab/context/project_map.json" in str(reason)
        for reason in research_row["stale_reasons"]
    )
    assert any(
        ".autolab/context/project_map.json" in str(item)
        for item in resolution["diagnostics"]
    )


def test_render_implementation_prompt_includes_compact_sidecar_guidance_only(
    tmp_path: Path,
) -> None:
    _require_sidecar_context_support()
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_context_resolution_fixture(repo)
    _write_design_with_context_refs(repo)
    state = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    template_path = _resolve_stage_prompt_path(
        repo, "implementation", prompt_role="runner"
    )
    bundle = _render_stage_prompt(
        repo,
        stage="implementation",
        state=state,
        template_path=template_path,
        runner_scope={
            "mode": "scope_root_plus_core",
            "scope_kind": "experiment",
            "scope_root": str((repo / "experiments" / "plan" / "iter1").resolve()),
            "project_wide_root": str(repo.resolve()),
            "workspace_dir": str((repo / "experiments" / "plan" / "iter1").resolve()),
            "allowed_edit_dirs": [],
        },
        write_outputs=False,
    )

    sidecar_guidance = bundle.context_payload["sidecar_guidance"]
    assert any(
        "research_findings: experiment research override; experiment research only"
        in line
        for line in sidecar_guidance["stage_context_lines"]
    )
    assert (
        "promoted constraints: R2: Respect the experiment-local discuss preference."
        in sidecar_guidance["brief_items"]
    )
    assert "research_findings:" in bundle.context_payload["stage_context"]
    assert "promoted_constraints:" in bundle.context_payload["stage_context"]
    assert (
        "findings: experiment research override; experiment research only"
        in bundle.context_payload["brief_summary"]
    )
    assert "effective_discuss" not in bundle.prompt_text
    assert "effective_research" not in bundle.prompt_text
    assert "source_component_id" not in bundle.prompt_text


def test_render_implementation_prompt_includes_semantic_agent_surface_when_installed(
    tmp_path: Path,
) -> None:
    _require_sidecar_context_support()
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_context_resolution_fixture(repo)
    _write_design_with_context_refs(repo)
    _install_codex_skill(repo, "planner")
    _install_codex_skill(repo, "plan-checker")
    state = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    template_path = _resolve_stage_prompt_path(
        repo, "implementation", prompt_role="runner"
    )
    bundle = _render_stage_prompt(
        repo,
        stage="implementation",
        state=state,
        template_path=template_path,
        runner_scope={
            "mode": "scope_root_plus_core",
            "scope_kind": "experiment",
            "scope_root": str((repo / "experiments" / "plan" / "iter1").resolve()),
            "project_wide_root": str(repo.resolve()),
            "workspace_dir": str((repo / "experiments" / "plan" / "iter1").resolve()),
            "allowed_edit_dirs": [],
        },
        write_outputs=False,
    )

    surface = bundle.context_payload["agent_surface"]
    assert surface["provider"] == "codex"
    assert surface["primary_role"]["id"] == "planner"
    assert surface["primary_role"]["installed"] is True
    assert surface["secondary_roles"][0]["id"] == "plan_checker"
    assert surface["secondary_roles"][0]["installed"] is True
    assert surface["invocation_hints"] == ["$planner", "$plan-checker"]
    assert (
        "semantic_agent_primary: planner ($planner)"
        in bundle.context_payload["stage_context"]
    )
    assert (
        "semantic_agent_secondary: plan_checker ($plan-checker)"
        in bundle.context_payload["stage_context"]
    )
    assert (
        "semantic role: planner via $planner" in bundle.context_payload["brief_summary"]
    )
    assert (
        "secondary role: plan_checker via $plan-checker"
        in bundle.context_payload["brief_summary"]
    )


def test_render_implementation_prompt_falls_back_when_semantic_skill_missing(
    tmp_path: Path,
) -> None:
    _require_sidecar_context_support()
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_context_resolution_fixture(repo)
    state = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    template_path = _resolve_stage_prompt_path(
        repo, "implementation", prompt_role="runner"
    )
    bundle = _render_stage_prompt(
        repo,
        stage="implementation",
        state=state,
        template_path=template_path,
        runner_scope={
            "mode": "scope_root_plus_core",
            "scope_kind": "experiment",
            "scope_root": str((repo / "experiments" / "plan" / "iter1").resolve()),
            "project_wide_root": str(repo.resolve()),
            "workspace_dir": str((repo / "experiments" / "plan" / "iter1").resolve()),
            "allowed_edit_dirs": [],
        },
        write_outputs=False,
    )

    surface = bundle.context_payload["agent_surface"]
    assert surface["primary_role"]["id"] == "planner"
    assert surface["primary_role"]["installed"] is False
    assert surface["invocation_hints"] == []
    assert (
        "semantic_agent_primary: planner -" in bundle.context_payload["stage_context"]
    )
    assert "$planner" not in bundle.context_payload["stage_context"]
    assert (
        "semantic role: planner with inline guidance only"
        in bundle.context_payload["brief_summary"]
    )


def test_render_implementation_review_prompt_uses_reviewer_semantic_role(
    tmp_path: Path,
) -> None:
    _require_sidecar_context_support()
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_context_resolution_fixture(repo)
    _install_codex_skill(repo, "reviewer")
    state = _write_state(repo, stage="implementation_review")
    _write_backlog(repo)

    template_path = _resolve_stage_prompt_path(
        repo, "implementation_review", prompt_role="runner"
    )
    bundle = _render_stage_prompt(
        repo,
        stage="implementation_review",
        state=state,
        template_path=template_path,
        runner_scope={
            "mode": "scope_root_plus_core",
            "scope_kind": "experiment",
            "scope_root": str((repo / "experiments" / "plan" / "iter1").resolve()),
            "project_wide_root": str(repo.resolve()),
            "workspace_dir": str((repo / "experiments" / "plan" / "iter1").resolve()),
            "allowed_edit_dirs": [],
        },
        write_outputs=False,
    )

    surface = bundle.context_payload["agent_surface"]
    assert surface["primary_role"]["id"] == "reviewer"
    assert surface["invocation_hints"] == ["$reviewer"]
    assert (
        "semantic_agent_primary: reviewer ($reviewer)"
        in bundle.context_payload["stage_context"]
    )
    assert (
        "semantic role: reviewer via $reviewer"
        in bundle.context_payload["brief_summary"]
    )


def test_render_stage_prompt_written_context_omits_raw_effective_sidecars(
    tmp_path: Path,
) -> None:
    _require_sidecar_context_support()
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_context_resolution_fixture(repo)
    _write_design_with_context_refs(repo)
    state = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    template_path = _resolve_stage_prompt_path(
        repo, "implementation", prompt_role="runner"
    )
    bundle = _render_stage_prompt(
        repo,
        stage="implementation",
        state=state,
        template_path=template_path,
        runner_scope={
            "mode": "scope_root_plus_core",
            "scope_kind": "experiment",
            "scope_root": str((repo / "experiments" / "plan" / "iter1").resolve()),
            "project_wide_root": str(repo.resolve()),
            "workspace_dir": str((repo / "experiments" / "plan" / "iter1").resolve()),
            "allowed_edit_dirs": [],
        },
        write_outputs=True,
    )

    in_memory_resolution = bundle.context_payload["context_resolution"]
    assert "effective_discuss" in in_memory_resolution
    assert "effective_research" in in_memory_resolution

    runtime_context = json.loads(bundle.context_path.read_text(encoding="utf-8"))
    written_resolution = runtime_context["context_resolution"]
    assert "effective_discuss" not in written_resolution
    assert "effective_research" not in written_resolution
    assert "effective_discuss" not in runtime_context["artifacts"]["context_resolution"]
    assert (
        "effective_research" not in runtime_context["artifacts"]["context_resolution"]
    )
    assert any(
        "research_findings: experiment research override; experiment research only"
        in line
        for line in runtime_context["sidecar_guidance"]["stage_context_lines"]
    )
