from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import autolab.commands as commands_module


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


def _write_state(repo: Path, *, stage: str = "design") -> Path:
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
    path = repo / ".autolab" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


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


def test_render_uses_current_stage_and_prints_prompt(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "stage: design" in captured.out.lower()


def test_render_stage_override_does_not_mutate_state(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)
    before = json.loads(state_path.read_text(encoding="utf-8"))

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--stage", "implementation"]
    )

    captured = capsys.readouterr()
    after = json.loads(state_path.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert captured.err == ""
    assert "stage: implementation" in captured.out.lower()
    assert after == before


def test_render_context_appends_separator_and_json(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--view", "context"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    context = json.loads(captured.out)
    assert context["stage"] == "design"
    assert context["iteration_id"] == "iter1"
    assert context["runner_scope"]["mode"] == "scope_root_plus_core"
    assert context["runner_scope"]["scope_kind"] == "experiment"


def test_render_context_uses_project_wide_scope_root(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(repo)
    (repo / "src").mkdir(exist_ok=True)
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8")
        + "\nscope_roots:\n  project_wide_root: src\n",
        encoding="utf-8",
    )
    (repo / ".autolab" / "plan_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "tasks": [{"task_id": "T1", "scope_kind": "project_wide"}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--view", "context"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    context = json.loads(captured.out)
    assert context["runner_scope"]["scope_kind"] == "project_wide"
    assert context["runner_scope"]["scope_root"] == str((repo / "src").resolve())
    assert context["runner_scope"]["workspace_dir"] == str((repo / "src").resolve())


def test_render_fails_with_invalid_project_wide_root(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(repo)
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8")
        + "\nscope_roots:\n  project_wide_root: missing_dir\n",
        encoding="utf-8",
    )
    (repo / ".autolab" / "plan_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "tasks": [{"task_id": "T1", "scope_kind": "project_wide"}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "scope_roots.project_wide_root" in captured.err


def test_render_human_view_prints_human_packet(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--view", "human"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "stage: design (human packet)" in captured.out.lower()


def test_render_does_not_write_rendered_artifacts(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    rendered_dir = repo / ".autolab" / "prompts" / "rendered"
    if rendered_dir.exists():
        shutil.rmtree(rendered_dir)

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    _captured = capsys.readouterr()
    assert exit_code == 0
    assert not (rendered_dir / "design.md").exists()
    assert not (rendered_dir / "design.context.json").exists()


def test_render_stats_does_not_write_rendered_artifacts(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    rendered_dir = repo / ".autolab" / "prompts" / "rendered"
    if rendered_dir.exists():
        shutil.rmtree(rendered_dir)

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--stats"]
    )

    _captured = capsys.readouterr()
    assert exit_code == 0
    assert not rendered_dir.exists()


def test_render_fails_when_prompt_template_missing(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)
    (repo / ".autolab" / "prompts" / "stage_design.runner.md").unlink()

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "autolab render: ERROR" in captured.err
    assert "stage prompt is missing" in captured.err


def test_render_fails_when_stage_requires_missing_run_id(
    tmp_path: Path, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="launch")
    _write_backlog(repo)

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "autolab render: ERROR" in captured.err
    assert "requires a resolved run_id" in captured.err


def test_render_fails_for_invalid_stage_override(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--stage", "not_a_real_stage"]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "autolab render: ERROR" in captured.err
    assert "no stage prompt mapping is defined" in captured.err


def test_render_fails_when_state_stage_is_invalid_without_override(
    tmp_path: Path, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_payload["stage"] = "not_a_real_stage"
    state_path.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "autolab render: ERROR" in captured.err
    assert "state.stage must be one of" in captured.err


def test_render_fails_when_state_file_is_missing(capsys, tmp_path: Path) -> None:
    missing_state_path = tmp_path / "missing" / "state.json"

    exit_code = commands_module.main(
        ["render", "--state-file", str(missing_state_path)]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "autolab render: ERROR" in captured.err
    assert "state file not found" in captured.err


def test_render_fails_when_state_file_is_malformed(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{bad json", encoding="utf-8")

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "autolab render: ERROR" in captured.err
    assert "state file is not valid JSON" in captured.err


def test_render_entrypoint_via_python_module(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    repo_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    existing_pythonpath = env.get("PYTHONPATH", "")
    src_path = str(repo_root / "src")
    env["PYTHONPATH"] = (
        f"{src_path}{os.pathsep}{existing_pythonpath}"
        if existing_pythonpath
        else src_path
    )

    result = subprocess.run(
        [sys.executable, "-m", "autolab", "render", "--state-file", str(state_path)],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    assert "stage: design" in result.stdout.lower()
    assert result.stderr == ""


def test_render_implementation_audit_and_retry_brief_modes(
    tmp_path: Path, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    review_result_path = repo / "experiments" / "plan" / "iter1" / "review_result.json"
    review_result_path.parent.mkdir(parents=True, exist_ok=True)
    review_result_path.write_text(
        json.dumps(
            {
                "status": "needs_retry",
                "blocking_findings": ["Fix dry_run failure in trainer integration."],
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
    verification_result_path = repo / ".autolab" / "verification_result.json"
    verification_result_path.write_text(
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

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--view", "audit"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Implementation Auditor" in captured.out

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--view", "brief"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Stage: implementation (brief)" in captured.out
    assert "Fix dry_run failure in trainer integration." in captured.out


def test_render_brief_mode_is_available_for_non_implementation_stage(
    tmp_path: Path, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--view", "brief"]
    )
    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Stage: design (brief)" in captured.out


def test_render_fails_fast_when_implementation_runner_template_missing(
    tmp_path: Path, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(repo)
    (repo / ".autolab" / "prompts" / "stage_implementation.runner.md").unlink()

    exit_code = commands_module.main(["render", "--state-file", str(state_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "stage prompt is missing" in captured.err


def test_render_stats_defaults_to_all_views(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--stats"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "autolab render stats" in captured.out
    assert "views: runner, audit, brief, human, context" in captured.out
    assert "[runner]" in captured.out
    assert "[context]" in captured.out
    assert "line_count:" in captured.out
    assert "token_estimate:" in captured.out


def test_render_stats_with_explicit_view(tmp_path: Path, capsys) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="design")
    _write_backlog(repo)

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--stats", "--view", "audit"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "views: audit" in captured.out
    assert "[audit]" in captured.out
    assert "[runner]" not in captured.out


def test_render_rejects_legacy_audience_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = commands_module._build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["render", "--audience", "runner"])

    assert int(exc_info.value.code) == 2
    captured = capsys.readouterr()
    assert "--audience" in captured.err
    assert any(marker in captured.err.lower() for marker in ("unrecognized", "unknown"))


def test_render_context_project_wide_sidecar_resolution_excludes_experiment_layers(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_sidecar_context_support()
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _install_codex_skill(repo, "planner")
    _install_codex_skill(repo, "plan-checker")
    (repo / "src").mkdir(exist_ok=True)
    _write_context_resolution_fixture(
        repo,
        project_scope_root=str((repo / "src").resolve()),
    )
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.write_text(
        policy_path.read_text(encoding="utf-8")
        + "\nscope_roots:\n  project_wide_root: src\n",
        encoding="utf-8",
    )
    (repo / ".autolab" / "plan_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "tasks": [{"task_id": "T1", "scope_kind": "project_wide"}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--view", "context"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    context = json.loads(captured.out)
    resolution = context["context_resolution"]
    assert resolution["scope_kind"] == "project_wide"
    assert resolution["scope_root"] == str((repo / "src").resolve())
    assert resolution["diagnostics"] == []
    assert context["agent_surface"]["provider"] == "codex"
    assert context["agent_surface"]["primary_role"]["id"] == "planner"
    assert context["agent_surface"]["primary_role"]["installed"] is True
    assert [role["id"] for role in context["agent_surface"]["secondary_roles"]] == [
        "plan_checker"
    ]
    assert context["agent_surface"]["invocation_hints"] == [
        "$planner",
        "$plan-checker",
    ]
    assert all(
        "skill_path" not in role
        for role in context["agent_surface"]["roles"]
        if isinstance(role, dict)
    )
    _assert_component_order_tokens(
        resolution["component_order"],
        [("project_map",), ("project", "discuss"), ("project", "research")],
    )

    components = resolution["components"]
    assert [
        (row["component_id"], row["artifact_kind"], row["scope_kind"])
        for row in components
    ] == [
        ("project_map", "project_map", "project_wide"),
        ("project_wide_discuss", "discuss", "project_wide"),
        ("project_wide_research", "research", "project_wide"),
    ]
    assert [row["precedence_index"] for row in components] == [0, 1, 2]
    assert not any(
        str(row["path"]).endswith("context_delta.json") for row in components
    )
    assert not any(
        str(row["path"]).endswith("/discuss.json") and row["scope_kind"] == "experiment"
        for row in components
    )

    discuss_items = _items_by_id(resolution["effective_discuss"])
    research_items = _items_by_id(resolution["effective_research"])
    assert set(discuss_items) == {"shared-discuss", "pw-discuss"}
    assert set(research_items) == {"shared-research", "pw-research"}
    assert discuss_items["shared-discuss"]["source_scope_kind"] == "project_wide"
    assert research_items["shared-research"]["source_scope_kind"] == "project_wide"
    assert "experiment_discuss" not in resolution["compact_render"]
    assert "experiment_research" not in resolution["compact_render"]
    assert "effective_discuss=shared-discuss,pw-discuss" in resolution["compact_render"]
    assert (
        "effective_research=shared-research,pw-research" in resolution["compact_render"]
    )


def test_render_context_experiment_sidecar_resolution_loads_both_layers_and_overlays_ids(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_sidecar_context_support()
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_context_resolution_fixture(repo)
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(repo)

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--view", "context"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    resolution = json.loads(captured.out)["context_resolution"]
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

    discuss_items = _items_by_id(resolution["effective_discuss"])
    research_items = _items_by_id(resolution["effective_research"])
    assert set(discuss_items) == {"shared-discuss", "pw-discuss", "exp-discuss"}
    assert set(research_items) == {"shared-research", "pw-research", "exp-research"}
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


def test_render_context_marks_invalid_project_wide_sidecar_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_sidecar_context_support()
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_context_resolution_fixture(repo)
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(repo)
    sidecar_path = (
        repo / ".autolab" / "context" / "sidecars" / "project_wide" / "discuss.json"
    )
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload["iteration_id"] = "iter1"
    payload["experiment_id"] = "e1"
    sidecar_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    exit_code = commands_module.main(
        ["render", "--state-file", str(state_path), "--view", "context"]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    resolution = json.loads(captured.out)["context_resolution"]
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
