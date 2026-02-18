from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from autolab.run_standard import _run_once_standard
from autolab.utils import _generate_run_id


def _copy_scaffold(repo: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "autolab" / "scaffold" / ".autolab"
    target = repo / ".autolab"
    shutil.copytree(source, target, dirs_exist_ok=True)


def test_generate_run_id_is_unique_and_utc_formatted() -> None:
    run_id_a = _generate_run_id()
    run_id_b = _generate_run_id()
    assert run_id_a != run_id_b
    pattern = re.compile(r"^20\d{6}T\d{6}Z_[a-f0-9]{6}$")
    assert pattern.match(run_id_a)
    assert pattern.match(run_id_b)


def test_decide_repeat_writes_auto_decision_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = repo / ".autolab" / "state.json"
    state_payload = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "decide_repeat",
        "stage_attempt": 0,
        "last_run_id": "",
        "pending_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
        "assistant_mode": "off",
        "current_task_id": "",
        "task_cycle_stage": "select",
        "repeat_guard": {
            "last_decision": "",
            "same_decision_streak": 0,
            "last_open_task_count": -1,
            "no_progress_decisions": 0,
            "update_docs_cycle_count": 0,
            "last_verification_passed": False,
        },
        "task_change_baseline": {},
        "history": [],
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")
    (repo / "experiments" / "plan" / "iter1").mkdir(parents=True, exist_ok=True)
    (repo / ".autolab" / "backlog.yaml").write_text(
        "experiments:\n  - id: e1\n    iteration_id: iter1\n    hypothesis_id: h1\n    status: open\n",
        encoding="utf-8",
    )
    (repo / ".autolab" / "agent_result.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "summary": "seed",
                "changed_files": [],
                "completion_token_seen": True,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    outcome = _run_once_standard(
        state_path,
        decision=None,
        auto_decision=True,
        auto_mode=True,
        run_agent_mode="force_off",
    )

    assert outcome.exit_code == 0
    auto_decision_path = repo / ".autolab" / "auto_decision.json"
    assert auto_decision_path.exists()
    payload = json.loads(auto_decision_path.read_text(encoding="utf-8"))
    assert payload.get("stage") == "decide_repeat"
    outputs = payload.get("outputs", {})
    assert isinstance(outputs, dict)
    assert outputs.get("selected_decision") in {"hypothesis", "design", "stop", "human_review"}
