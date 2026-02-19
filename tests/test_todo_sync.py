from __future__ import annotations

import json
from pathlib import Path

import yaml

from autolab.todo_sync import sync_todo_pre_run


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
