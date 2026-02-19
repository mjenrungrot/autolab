from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
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

    policy_path = target / "verifier_policy.yaml"
    policy_lines = policy_path.read_text(encoding="utf-8").splitlines()
    for idx, line in enumerate(policy_lines):
        if line.strip().startswith("python_bin:"):
            policy_lines[idx] = f'python_bin: "{sys.executable}"'
            break
    policy_path.write_text("\n".join(policy_lines) + "\n", encoding="utf-8")


def _write_state(
    repo: Path,
    *,
    iteration_id: str = "iter1",
    experiment_id: str = "e1",
    stage: str = "design",
    stage_attempt: int = 2,
) -> Path:
    payload = {
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "stage": stage,
        "stage_attempt": stage_attempt,
        "last_run_id": "run_old",
        "pending_run_id": "run_pending_old",
        "sync_status": "failed",
        "max_stage_attempts": 5,
        "max_total_iterations": 20,
        "assistant_mode": "on",
        "current_task_id": "task_old",
        "task_cycle_stage": "review",
        "repeat_guard": {
            "last_decision": "design",
            "same_decision_streak": 2,
            "last_open_task_count": 3,
            "no_progress_decisions": 2,
            "update_docs_cycle_count": 1,
            "last_verification_passed": True,
        },
        "task_change_baseline": {"foo.py": "abc"},
        "run_group": ["run_a", "run_b"],
        "history": [],
    }
    path = repo / ".autolab" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


def _read_state(state_path: Path) -> dict:
    return json.loads(state_path.read_text(encoding="utf-8"))


def _write_backlog(repo: Path, *, experiments: list[dict]) -> None:
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
        "experiments": experiments,
    }
    path = repo / ".autolab" / "backlog.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(backlog, sort_keys=False), encoding="utf-8")


def _read_backlog(repo: Path) -> dict:
    return yaml.safe_load(
        (repo / ".autolab" / "backlog.yaml").read_text(encoding="utf-8")
    )


def _mk_iteration_dir(repo: Path, experiment_type: str, iteration_id: str) -> Path:
    path = repo / "experiments" / experiment_type / iteration_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_lock(repo: Path) -> None:
    now = (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )
    payload = {
        "pid": 99999,
        "host": "test-host",
        "owner_uuid": "abc123",
        "started_at": now,
        "last_heartbeat_at": now,
        "command": "autolab loop --auto",
        "state_file": str(repo / ".autolab" / "state.json"),
    }
    lock_path = repo / ".autolab" / "lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _open_tasks(repo: Path) -> list[dict]:
    payload = json.loads(
        (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
    )
    tasks = payload.get("tasks", {})
    return [
        task
        for task in tasks.values()
        if isinstance(task, dict) and str(task.get("status", "")).strip() == "open"
    ]


def test_focus_by_experiment_id_resets_state_cleanly(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, iteration_id="iter_old", experiment_id="e_old")
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "in_progress",
                "iteration_id": "iter_focus",
            }
        ],
    )
    _mk_iteration_dir(repo, "in_progress", "iter_focus")

    exit_code = commands_module.main(
        [
            "focus",
            "--state-file",
            str(state_path),
            "--experiment-id",
            "e1",
        ]
    )

    assert exit_code == 0
    state = _read_state(state_path)
    assert state["iteration_id"] == "iter_focus"
    assert state["experiment_id"] == "e1"
    assert state["stage"] == "hypothesis"
    assert state["stage_attempt"] == 0
    assert state["last_run_id"] == ""
    assert state["pending_run_id"] == ""
    assert state["sync_status"] == "na"
    assert state["assistant_mode"] == "off"
    assert state["current_task_id"] == ""
    assert state["task_cycle_stage"] == "select"
    assert state["task_change_baseline"] == {}
    assert state["repeat_guard"]["same_decision_streak"] == 0
    assert state["repeat_guard"]["update_docs_cycle_count"] == 0


def test_focus_by_iteration_id_fails_when_backlog_is_ambiguous(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter_shared",
            },
            {
                "id": "e2",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter_shared",
            },
        ],
    )
    _mk_iteration_dir(repo, "plan", "iter_shared")

    exit_code = commands_module.main(
        [
            "focus",
            "--state-file",
            str(state_path),
            "--iteration-id",
            "iter_shared",
        ]
    )

    assert exit_code == 1
    state = _read_state(state_path)
    assert state["iteration_id"] == "iter1"


def test_focus_fails_when_lock_is_active(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter1",
            }
        ],
    )
    _mk_iteration_dir(repo, "plan", "iter1")
    _write_lock(repo)

    exit_code = commands_module.main(
        ["focus", "--state-file", str(state_path), "--experiment-id", "e1"]
    )

    assert exit_code == 1


def test_focus_sets_stop_for_done_experiment(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e_done",
                "hypothesis_id": "h1",
                "status": "completed",
                "type": "done",
                "iteration_id": "iter_done",
            }
        ],
    )
    _mk_iteration_dir(repo, "done", "iter_done")

    exit_code = commands_module.main(
        ["focus", "--state-file", str(state_path), "--experiment-id", "e_done"]
    )

    assert exit_code == 0
    state = _read_state(state_path)
    assert state["stage"] == "stop"


def test_todo_add_and_sync_updates_todo_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter1",
            }
        ],
    )
    _mk_iteration_dir(repo, "plan", "iter1")

    exit_code = commands_module.main(
        [
            "todo",
            "add",
            "--state-file",
            str(state_path),
            "--stage",
            "implementation",
            "--priority",
            "high",
            "--owner",
            "alice",
            "--label",
            "backend",
            "--label",
            "urgent",
            "Investigate parser behavior",
        ]
    )

    assert exit_code == 0
    todo_md = (repo / "docs" / "todo.md").read_text(encoding="utf-8")
    assert "[stage:implementation] Investigate parser behavior" in todo_md

    tasks = _open_tasks(repo)
    matching = [
        task
        for task in tasks
        if task.get("source") == "manual"
        and task.get("stage") == "implementation"
        and task.get("text") == "Investigate parser behavior"
    ]
    assert len(matching) == 1
    assert matching[0].get("priority") == "high"
    assert matching[0].get("owner") == "alice"
    assert sorted(matching[0].get("labels", [])) == ["backend", "urgent"]


def test_todo_list_is_index_stable_and_json_friendly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter1",
            }
        ],
    )
    _mk_iteration_dir(repo, "plan", "iter1")

    assert (
        commands_module.main(
            [
                "todo",
                "add",
                "--state-file",
                str(state_path),
                "--stage",
                "implementation",
                "Task one",
            ]
        )
        == 0
    )
    assert (
        commands_module.main(
            [
                "todo",
                "add",
                "--state-file",
                str(state_path),
                "--stage",
                "implementation",
                "Task two",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        commands_module.main(
            ["todo", "list", "--state-file", str(state_path), "--json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["open_count"] >= 2
    texts = [task["text"] for task in payload["tasks"]]
    assert "Task one" in texts
    assert "Task two" in texts
    assert texts.index("Task one") < texts.index("Task two")


def test_todo_done_by_index_and_task_id(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter1",
            }
        ],
    )
    _mk_iteration_dir(repo, "plan", "iter1")

    assert (
        commands_module.main(
            [
                "todo",
                "add",
                "--state-file",
                str(state_path),
                "--stage",
                "implementation",
                "Task one",
            ]
        )
        == 0
    )
    assert (
        commands_module.main(
            [
                "todo",
                "add",
                "--state-file",
                str(state_path),
                "--stage",
                "implementation",
                "Task two",
            ]
        )
        == 0
    )

    assert (
        commands_module.main(["todo", "done", "--state-file", str(state_path), "1"])
        == 0
    )
    capsys.readouterr()
    assert (
        commands_module.main(
            ["todo", "list", "--state-file", str(state_path), "--json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    remaining_manual = [
        task
        for task in payload["tasks"]
        if task.get("source") == "manual" and task.get("text") == "Task two"
    ]
    assert len(remaining_manual) == 1
    remaining_id = remaining_manual[0]["task_id"]

    assert (
        commands_module.main(
            ["todo", "done", "--state-file", str(state_path), remaining_id]
        )
        == 0
    )
    capsys.readouterr()
    assert (
        commands_module.main(
            ["todo", "list", "--state-file", str(state_path), "--json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert not any(
        task.get("source") == "manual" and task.get("text") in {"Task one", "Task two"}
        for task in payload["tasks"]
    )


def test_todo_remove_clears_open_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter1",
            }
        ],
    )
    _mk_iteration_dir(repo, "plan", "iter1")

    assert (
        commands_module.main(
            [
                "todo",
                "add",
                "--state-file",
                str(state_path),
                "--stage",
                "implementation",
                "Task one",
            ]
        )
        == 0
    )
    assert (
        commands_module.main(["todo", "remove", "--state-file", str(state_path), "1"])
        == 0
    )
    capsys.readouterr()
    assert (
        commands_module.main(
            ["todo", "list", "--state-file", str(state_path), "--json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert not any(
        task.get("source") == "manual" and task.get("text") == "Task one"
        for task in payload["tasks"]
    )


def test_todo_sync_reconciles_manual_markdown(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation")
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter1",
            }
        ],
    )
    _mk_iteration_dir(repo, "plan", "iter1")
    (repo / "docs").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "todo.md").write_text(
        (
            "# TODO\n\n"
            "## Tasks\n"
            "- [ ] [stage:implementation] Manual follow-up task\n\n"
            "## Notes\n"
            "notes\n"
        ),
        encoding="utf-8",
    )

    exit_code = commands_module.main(["todo", "sync", "--state-file", str(state_path)])
    assert exit_code == 0
    tasks = _open_tasks(repo)
    assert any(task.get("text") == "Manual follow-up task" for task in tasks)


def test_experiment_move_plan_to_in_progress_moves_and_rewrites_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation", stage_attempt=3)
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter1",
            }
        ],
    )
    source_dir = _mk_iteration_dir(repo, "plan", "iter1")
    (source_dir / "docs_update.md").write_text(
        "metrics artifact: experiments/plan/iter1/runs/run_001/metrics.json\n",
        encoding="utf-8",
    )
    (repo / ".autolab" / "run_context.json").write_text(
        json.dumps(
            {"iteration_path": "experiments/plan/iter1", "run_id": "run_001"},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = commands_module.main(
        [
            "experiment",
            "move",
            "--state-file",
            str(state_path),
            "--experiment-id",
            "e1",
            "--to",
            "in_progress",
        ]
    )

    assert exit_code == 0
    destination_dir = repo / "experiments" / "in_progress" / "iter1"
    assert not source_dir.exists()
    assert destination_dir.exists()
    updated_text = (destination_dir / "docs_update.md").read_text(encoding="utf-8")
    assert "experiments/in_progress/iter1/runs/run_001/metrics.json" in updated_text

    run_context = json.loads(
        (repo / ".autolab" / "run_context.json").read_text(encoding="utf-8")
    )
    assert run_context["iteration_path"] == "experiments/in_progress/iter1"

    backlog = _read_backlog(repo)
    exp = backlog["experiments"][0]
    assert exp["type"] == "in_progress"
    assert exp["status"] == "in_progress"

    state = _read_state(state_path)
    assert state["stage"] == "hypothesis"
    assert state["stage_attempt"] == 0
    assert state["sync_status"] == "na"


def test_experiment_move_in_progress_to_done_updates_state_to_stop(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation", stage_attempt=4)
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "in_progress",
                "type": "in_progress",
                "iteration_id": "iter1",
            }
        ],
    )
    _mk_iteration_dir(repo, "in_progress", "iter1")

    exit_code = commands_module.main(
        [
            "experiment",
            "move",
            "--state-file",
            str(state_path),
            "--experiment-id",
            "e1",
            "--to",
            "done",
        ]
    )

    assert exit_code == 0
    backlog = _read_backlog(repo)
    exp = backlog["experiments"][0]
    assert exp["type"] == "done"
    assert exp["status"] == "completed"

    state = _read_state(state_path)
    assert state["stage"] == "stop"
    assert state["stage_attempt"] == 0


def test_experiment_move_backlog_write_failure_rolls_back_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation", stage_attempt=3)
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter1",
            }
        ],
    )
    source_dir = _mk_iteration_dir(repo, "plan", "iter1")
    destination_dir = repo / "experiments" / "in_progress" / "iter1"

    call_counter = {"count": 0}

    def _fake_write_backlog(path: Path, payload: dict) -> tuple[bool, str]:
        call_counter["count"] += 1
        if call_counter["count"] == 1:
            return (False, "simulated write failure")
        return (True, "")

    monkeypatch.setattr(commands_module, "_write_backlog_yaml", _fake_write_backlog)

    exit_code = commands_module.main(
        [
            "experiment",
            "move",
            "--state-file",
            str(state_path),
            "--experiment-id",
            "e1",
            "--to",
            "in_progress",
        ]
    )

    assert exit_code == 1
    assert call_counter["count"] >= 2
    assert source_dir.exists()
    assert not destination_dir.exists()


def test_experiment_move_rewrite_failure_rolls_back_move_and_backlog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, stage="implementation", stage_attempt=3)
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter1",
            }
        ],
    )
    source_dir = _mk_iteration_dir(repo, "plan", "iter1")
    destination_dir = repo / "experiments" / "in_progress" / "iter1"
    (source_dir / "docs_update.md").write_text(
        "metrics artifact: experiments/plan/iter1/runs/run_001/metrics.json\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        commands_module,
        "_rewrite_iteration_prefix_scoped",
        lambda *args, **kwargs: ([], "simulated rewrite failure"),
    )

    exit_code = commands_module.main(
        [
            "experiment",
            "move",
            "--state-file",
            str(state_path),
            "--experiment-id",
            "e1",
            "--to",
            "in_progress",
        ]
    )

    assert exit_code == 1
    assert source_dir.exists()
    assert not destination_dir.exists()
    backlog = _read_backlog(repo)
    exp = backlog["experiments"][0]
    assert exp["type"] == "plan"
    assert exp["status"] == "open"


def test_experiment_move_does_not_reset_state_when_state_experiment_id_empty(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(
        repo,
        iteration_id="iter1",
        experiment_id="",
        stage="implementation",
        stage_attempt=3,
    )
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter1",
            }
        ],
    )
    _mk_iteration_dir(repo, "plan", "iter1")

    exit_code = commands_module.main(
        [
            "experiment",
            "move",
            "--state-file",
            str(state_path),
            "--experiment-id",
            "e1",
            "--to",
            "in_progress",
        ]
    )

    assert exit_code == 0
    state = _read_state(state_path)
    assert state["stage"] == "implementation"
    assert state["stage_attempt"] == 3


def test_experiment_move_fails_if_destination_exists(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter1",
            }
        ],
    )
    _mk_iteration_dir(repo, "plan", "iter1")
    _mk_iteration_dir(repo, "in_progress", "iter1")

    exit_code = commands_module.main(
        [
            "experiment",
            "move",
            "--state-file",
            str(state_path),
            "--experiment-id",
            "e1",
            "--to",
            "in_progress",
        ]
    )

    assert exit_code == 1


def test_experiment_move_fails_when_lock_is_active(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(
        repo,
        experiments=[
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter1",
            }
        ],
    )
    _mk_iteration_dir(repo, "plan", "iter1")
    _write_lock(repo)

    exit_code = commands_module.main(
        [
            "experiment",
            "move",
            "--state-file",
            str(state_path),
            "--experiment-id",
            "e1",
            "--to",
            "in_progress",
        ]
    )

    assert exit_code == 1
