from __future__ import annotations

import json
from pathlib import Path

import yaml

import os
import time

from autolab.todo_sync import _map_blocking_finding_stage, sync_todo_pre_run


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_review_result(
    repo: Path,
    *,
    iteration_id: str = "iter1",
    status: str = "needs_retry",
    blocking_findings: list[object] | None = None,
) -> None:
    payload = {
        "status": status,
        "blocking_findings": blocking_findings or [],
        "required_checks": {
            "tests": "pass",
            "dry_run": "skip",
            "schema": "pass",
            "env_smoke": "skip",
            "docs_target_update": "skip",
        },
        "reviewed_at": "2026-02-19T00:00:00Z",
    }
    path = repo / "experiments" / "plan" / iteration_id / "review_result.json"
    _write(path, json.dumps(payload, indent=2) + "\n")


def test_todo_sync_parses_nested_and_wrapped_markdown_tasks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        (
            "# TODO\n\n"
            "## Tasks\n"
            "- [ ] [stage:implementation] Improve parser handling\n"
            "  - [ ] keep nested bullets as context\n"
            "  - [x] retain checkbox details\n"
            "  wrapped line for extra context\n"
            "- [ ] [stage:design] Plan schema migration\n"
            "  wrapped line continues here\n\n"
            "## Notes\n"
            "notes\n"
        ),
    )

    state = {
        "iteration_id": "iter1",
        "stage": "implementation",
        "assistant_mode": "off",
    }
    result = sync_todo_pre_run(repo, state, host_mode="local")
    assert result.open_count == 2

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    tasks = [
        task for task in todo_state["tasks"].values() if task.get("status") == "open"
    ]
    assert len(tasks) == 2
    implementation_task = next(
        task for task in tasks if task["stage"] == "implementation"
    )
    design_task = next(task for task in tasks if task["stage"] == "design")
    assert "nested bullets as context" in implementation_task["text"]
    assert "wrapped line for extra context" in implementation_task["text"]
    assert "wrapped line continues here" in design_task["text"]


def test_todo_sync_uses_policy_fallback_task_configuration(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        ("# TODO\n\n## Tasks\n<!-- empty -->\n\n## Notes\nnotes\n"),
    )
    policy = {
        "autorun": {
            "todo_fallback": {
                "local": {
                    "stage": "update_docs",
                    "scope": "policy:custom:local",
                    "text": "Custom local fallback task inside experiments/ scope.",
                }
            }
        }
    }
    _write(
        repo / ".autolab" / "verifier_policy.yaml",
        yaml.safe_dump(policy, sort_keys=False),
    )

    state = {
        "iteration_id": "iter1",
        "stage": "decide_repeat",
        "assistant_mode": "off",
    }
    sync_todo_pre_run(repo, state, host_mode="local")

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    tasks = [
        task for task in todo_state["tasks"].values() if task.get("status") == "open"
    ]
    assert any(
        task.get("scope") == "policy:custom:local"
        and task.get("stage") == "update_docs"
        and "Custom local fallback task" in task.get("text", "")
        for task in tasks
    )


def test_todo_sync_extracts_generated_tasks_from_review_blockers(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        ("# TODO\n\n## Tasks\n<!-- empty -->\n\n## Notes\nnotes\n"),
    )
    _seed_review_result(
        repo,
        status="needs_retry",
        blocking_findings=[
            "Fix parser bug in scripts/method_b_av10_experiment.py",
            "SLURM launch submit fails due to missing job_id",
            {"stage": "extract_results", "summary": "metrics aggregation missing"},
        ],
    )

    state = {
        "iteration_id": "iter1",
        "stage": "implementation_review",
        "assistant_mode": "off",
    }
    sync_todo_pre_run(repo, state, host_mode="local")

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    open_tasks = [
        task for task in todo_state["tasks"].values() if task.get("status") == "open"
    ]
    blocker_tasks = [
        task
        for task in open_tasks
        if str(task.get("scope", "")).startswith("review:blocker:")
    ]
    assert blocker_tasks
    stages = {str(task.get("stage", "")) for task in blocker_tasks}
    assert "implementation" in stages
    assert "launch" in stages
    assert "extract_results" in stages


def test_todo_sync_prefers_implementation_for_file_backed_blockers(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        ("# TODO\n\n## Tasks\n<!-- empty -->\n\n## Notes\nnotes\n"),
    )
    _seed_review_result(
        repo,
        status="needs_retry",
        blocking_findings=[
            "run_manifest mismatch file=experiments/plan/iter1/scripts/method.py",
        ],
    )

    state = {
        "iteration_id": "iter1",
        "stage": "implementation_review",
        "assistant_mode": "off",
    }
    sync_todo_pre_run(repo, state, host_mode="local")

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    blocker_tasks = [
        task
        for task in todo_state["tasks"].values()
        if task.get("status") == "open"
        and str(task.get("scope", "")).startswith("review:blocker:")
    ]
    assert blocker_tasks
    assert any(str(task.get("stage", "")) == "implementation" for task in blocker_tasks)


def test_todo_sync_parses_stage_hint_from_blocker_string(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        ("# TODO\n\n## Tasks\n<!-- empty -->\n\n## Notes\nnotes\n"),
    )
    _seed_review_result(
        repo,
        status="needs_retry",
        blocking_findings=[
            "stage=implementation; run_manifest requires local script fix",
        ],
    )

    state = {
        "iteration_id": "iter1",
        "stage": "implementation_review",
        "assistant_mode": "off",
    }
    sync_todo_pre_run(repo, state, host_mode="local")

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    blocker_tasks = [
        task
        for task in todo_state["tasks"].values()
        if task.get("status") == "open"
        and str(task.get("scope", "")).startswith("review:blocker:")
    ]
    assert blocker_tasks
    assert all(str(task.get("stage", "")) == "implementation" for task in blocker_tasks)


def test_todo_sync_maps_launch_input_not_runnable_to_implementation_with_data_hints(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        ("# TODO\n\n## Tasks\n<!-- empty -->\n\n## Notes\nnotes\n"),
    )
    data_file = repo / "data" / "curated_yt_drummers" / "sample.mp4"
    data_file.parent.mkdir(parents=True, exist_ok=True)
    data_file.write_bytes(b"video")
    _seed_review_result(
        repo,
        status="needs_retry",
        blocking_findings=[
            "manual_check=launch_input_not_runnable | run_manifest pending",
        ],
    )

    state = {
        "iteration_id": "iter1",
        "stage": "implementation_review",
        "assistant_mode": "off",
    }
    sync_todo_pre_run(repo, state, host_mode="local")

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    blocker_tasks = [
        task
        for task in todo_state["tasks"].values()
        if task.get("status") == "open"
        and str(task.get("scope", "")).startswith("review:blocker:")
    ]
    assert blocker_tasks
    assert all(str(task.get("stage", "")) == "implementation" for task in blocker_tasks)
    assert any(
        str((repo / "data").resolve()) in str(task.get("text", ""))
        for task in blocker_tasks
    )


def test_todo_sync_filters_non_actionable_blocker_meta_lines(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        ("# TODO\n\n## Tasks\n<!-- empty -->\n\n## Notes\nnotes\n"),
    )
    _seed_review_result(
        repo,
        status="needs_retry",
        blocking_findings=[
            "verifier_failing_checks=none",
            "Fix parser bug in scripts/method_b_av10_experiment.py",
        ],
    )

    state = {
        "iteration_id": "iter1",
        "stage": "implementation_review",
        "assistant_mode": "off",
    }
    sync_todo_pre_run(repo, state, host_mode="local")

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    blocker_texts = [
        str(task.get("text", ""))
        for task in todo_state["tasks"].values()
        if task.get("status") == "open"
        and str(task.get("scope", "")).startswith("review:blocker:")
    ]
    assert blocker_texts
    assert not any("verifier_failing_checks=none" in text for text in blocker_texts)


def test_todo_sync_skips_fallback_when_unresolved_review_blockers_exist(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        ("# TODO\n\n## Tasks\n<!-- empty -->\n\n## Notes\nnotes\n"),
    )
    _seed_review_result(
        repo,
        status="needs_retry",
        blocking_findings=["Fix blocker in scripts/train.py before launch"],
    )

    state = {
        "iteration_id": "iter1",
        "stage": "decide_repeat",
        "assistant_mode": "on",
    }
    sync_todo_pre_run(repo, state, host_mode="local")

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    open_tasks = [
        task for task in todo_state["tasks"].values() if task.get("status") == "open"
    ]
    assert any(
        str(task.get("scope", "")).startswith("review:blocker:") for task in open_tasks
    )
    assert not any(
        str(task.get("scope", "")).startswith("policy:no_task_fallback:")
        for task in open_tasks
    )


def test_todo_sync_keeps_manual_blocker_tasks_sticky_with_unresolved_blockers(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        (
            "# TODO\n\n## Tasks\n"
            "- [ ] [stage:implementation] Fix blocker in scripts/method.py\n\n"
            "## Notes\nnotes\n"
        ),
    )
    _seed_review_result(
        repo,
        status="needs_retry",
        blocking_findings=["Fix blocker in scripts/method.py"],
    )
    state = {
        "iteration_id": "iter1",
        "stage": "implementation_review",
        "assistant_mode": "off",
    }
    sync_todo_pre_run(repo, state, host_mode="local")

    _write(
        repo / "docs" / "todo.md",
        "# TODO\n\n## Tasks\n<!-- empty -->\n\n## Notes\nnotes\n",
    )
    sync_todo_pre_run(repo, state, host_mode="local")

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    assert any(
        task.get("source") == "manual"
        and task.get("status") == "open"
        and "Fix blocker in scripts/method.py" in str(task.get("text", ""))
        for task in todo_state["tasks"].values()
    )


# ===========================================================================
# Fix 2: Stale review freshness gating
# ===========================================================================


def test_stale_review_emits_rerun_task_instead_of_individual_blockers(
    tmp_path: Path,
) -> None:
    """When implementation artifacts are newer than review_result.json, emit a
    single 'rerun review' task rather than individual blocker tasks."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        "# TODO\n\n## Tasks\n<!-- empty -->\n\n## Notes\nnotes\n",
    )

    # Write review_result.json first
    _seed_review_result(
        repo,
        status="needs_retry",
        blocking_findings=["Fix parser bug in scripts/method.py"],
    )

    # Create a script file that is newer than the review result
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    script_file = iteration_dir / "scripts" / "method.py"
    script_file.parent.mkdir(parents=True, exist_ok=True)

    # Ensure the review_result has an older mtime
    review_result_path = iteration_dir / "review_result.json"
    old_time = time.time() - 100
    os.utime(review_result_path, (old_time, old_time))

    # Now create the script (will have a newer mtime)
    script_file.write_text("# fixed\n", encoding="utf-8")

    state = {
        "iteration_id": "iter1",
        "stage": "implementation_review",
        "assistant_mode": "off",
    }
    sync_todo_pre_run(repo, state, host_mode="local")

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    open_tasks = [
        task for task in todo_state["tasks"].values() if task.get("status") == "open"
    ]
    blocker_tasks = [
        task
        for task in open_tasks
        if str(task.get("scope", "")).startswith("review:blocker:")
    ]
    assert len(blocker_tasks) == 1
    assert blocker_tasks[0]["scope"] == "review:blocker:stale_review"
    assert "stale" in blocker_tasks[0]["text"].lower()


def test_fresh_review_emits_normal_blockers(tmp_path: Path) -> None:
    """When review_result.json is newer than implementation artifacts, emit
    individual blocker tasks as usual."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        "# TODO\n\n## Tasks\n<!-- empty -->\n\n## Notes\nnotes\n",
    )

    iteration_dir = repo / "experiments" / "plan" / "iter1"
    # Create a script file first (older)
    script_file = iteration_dir / "scripts" / "method.py"
    script_file.parent.mkdir(parents=True, exist_ok=True)
    script_file.write_text("# code\n", encoding="utf-8")
    old_time = time.time() - 100
    os.utime(script_file, (old_time, old_time))

    # Write review_result.json after (newer)
    _seed_review_result(
        repo,
        status="needs_retry",
        blocking_findings=["Fix parser bug in scripts/method.py"],
    )

    state = {
        "iteration_id": "iter1",
        "stage": "implementation_review",
        "assistant_mode": "off",
    }
    sync_todo_pre_run(repo, state, host_mode="local")

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    blocker_tasks = [
        task
        for task in todo_state["tasks"].values()
        if task.get("status") == "open"
        and str(task.get("scope", "")).startswith("review:blocker:")
    ]
    assert blocker_tasks
    # Should NOT be the stale_review task
    assert all(task["scope"] != "review:blocker:stale_review" for task in blocker_tasks)


def test_stale_review_skips_epoch_placeholder(tmp_path: Path) -> None:
    """When reviewed_at is the scaffold placeholder '1970-01-01T00:00:00Z',
    freshness check is skipped, so normal blockers are emitted even if
    scripts/ files are newer."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        "# TODO\n\n## Tasks\n<!-- empty -->\n\n## Notes\nnotes\n",
    )

    iteration_dir = repo / "experiments" / "plan" / "iter1"
    review_result_path = iteration_dir / "review_result.json"
    review_result_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": "needs_retry",
        "blocking_findings": ["Fix parser bug in scripts/method.py"],
        "required_checks": {},
        "reviewed_at": "1970-01-01T00:00:00Z",
    }
    _write(review_result_path, json.dumps(payload, indent=2) + "\n")

    # Make review old and scripts newer
    old_time = time.time() - 100
    os.utime(review_result_path, (old_time, old_time))

    script_file = iteration_dir / "scripts" / "method.py"
    script_file.parent.mkdir(parents=True, exist_ok=True)
    script_file.write_text("# newer\n", encoding="utf-8")

    state = {
        "iteration_id": "iter1",
        "stage": "implementation_review",
        "assistant_mode": "off",
    }
    sync_todo_pre_run(repo, state, host_mode="local")

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    blocker_tasks = [
        task
        for task in todo_state["tasks"].values()
        if task.get("status") == "open"
        and str(task.get("scope", "")).startswith("review:blocker:")
    ]
    assert blocker_tasks
    # Should emit normal blockers, not the stale_review sentinel
    assert all(task["scope"] != "review:blocker:stale_review" for task in blocker_tasks)


def test_stale_review_handles_missing_dirs_gracefully(tmp_path: Path) -> None:
    """When scripts/ and data/ directories don't exist, the freshness check
    falls through to normal blocker emission."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _write(
        repo / "docs" / "todo.md",
        "# TODO\n\n## Tasks\n<!-- empty -->\n\n## Notes\nnotes\n",
    )

    _seed_review_result(
        repo,
        status="needs_retry",
        blocking_findings=["Fix issue in train loop"],
    )
    # No scripts/ or data/ directories exist

    state = {
        "iteration_id": "iter1",
        "stage": "implementation_review",
        "assistant_mode": "off",
    }
    sync_todo_pre_run(repo, state, host_mode="local")

    todo_state = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    blocker_tasks = [
        task
        for task in todo_state["tasks"].values()
        if task.get("status") == "open"
        and str(task.get("scope", "")).startswith("review:blocker:")
    ]
    assert blocker_tasks
    # Normal blockers, not stale
    assert all(task["scope"] != "review:blocker:stale_review" for task in blocker_tasks)


# ===========================================================================
# Fix 4: _map_blocking_finding_stage preserves implementation_review
# ===========================================================================


def test_map_blocking_finding_stage_preserves_implementation_review() -> None:
    result = _map_blocking_finding_stage(
        text="some finding", stage_hint="implementation_review"
    )
    assert result == "implementation_review"


def test_map_blocking_finding_stage_preserves_implementation() -> None:
    result = _map_blocking_finding_stage(
        text="some finding", stage_hint="implementation"
    )
    assert result == "implementation"


def test_map_blocking_finding_stage_falls_through_for_other_hints() -> None:
    result = _map_blocking_finding_stage(text="some finding", stage_hint="launch")
    assert result == "launch"
