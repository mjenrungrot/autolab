from __future__ import annotations

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
