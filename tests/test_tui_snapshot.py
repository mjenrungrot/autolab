from __future__ import annotations

import json
from pathlib import Path

from autolab.tui.snapshot import (
    load_artifact_text,
    load_cockpit_snapshot,
    resolve_stage_prompt_path,
)


def _write_state(
    path: Path,
    *,
    stage: str,
    last_run_id: str = "",
    stage_attempt: object = 1,
    max_stage_attempts: object = 3,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": stage,
        "stage_attempt": stage_attempt,
        "last_run_id": last_run_id,
        "sync_status": "completed",
        "max_stage_attempts": max_stage_attempts,
        "max_total_iterations": 20,
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_snapshot_handles_missing_optional_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    state_path = repo / ".autolab" / "state.json"
    _write_state(state_path, stage="design")

    snapshot = load_cockpit_snapshot(state_path)

    assert snapshot.current_stage == "design"
    assert snapshot.verification is None
    assert snapshot.render_preview.status == "error"
    assert snapshot.runs == ()
    assert snapshot.todos == ()
    assert snapshot.top_blockers == ()
    assert snapshot.primary_blocker == "none"
    assert snapshot.secondary_blockers == ()
    assert snapshot.recommended_actions
    assert snapshot.recommended_actions[0].action_id == "open_stage_prompt"
    assert "design" in {item.name for item in snapshot.stage_items}


def test_snapshot_render_preview_ok_without_rendered_output_writes(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    state_path = repo / ".autolab" / "state.json"
    _write_state(state_path, stage="design")
    prompt_path = repo / ".autolab" / "prompts" / "stage_design.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(
        "# Stage design\n\nstage: {{stage}}\niteration_id: {{iteration_id}}\n",
        encoding="utf-8",
    )

    snapshot = load_cockpit_snapshot(state_path)

    assert snapshot.render_preview.status == "ok"
    assert "stage: design" in snapshot.render_preview.prompt_text.lower()
    assert snapshot.recommended_actions
    assert snapshot.recommended_actions[0].action_id == "open_rendered_prompt"
    rendered_dir = repo / ".autolab" / "prompts" / "rendered"
    assert not (rendered_dir / "design.md").exists()
    assert not (rendered_dir / "design.context.json").exists()


def test_snapshot_render_preview_failure_is_nonfatal(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    state_path = repo / ".autolab" / "state.json"
    _write_state(state_path, stage="design")
    prompt_path = repo / ".autolab" / "prompts" / "stage_design.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text("# Stage design\n", encoding="utf-8")

    def _raise_render(*_args, **_kwargs):
        raise RuntimeError("render exploded")

    monkeypatch.setattr("autolab.tui.snapshot._render_stage_prompt", _raise_render)

    snapshot = load_cockpit_snapshot(state_path)

    assert snapshot.current_stage == "design"
    assert snapshot.render_preview.status == "error"
    assert "render exploded" in snapshot.render_preview.error_message
    assert snapshot.recommended_actions
    assert snapshot.recommended_actions[0].action_id == "open_stage_prompt"


def test_snapshot_merges_verification_and_review_blockers(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    state_path = repo / ".autolab" / "state.json"
    _write_state(state_path, stage="implementation_review")
    autolab_dir = repo / ".autolab"
    autolab_dir.mkdir(parents=True, exist_ok=True)
    verification_payload = {
        "generated_at": "2026-02-01T00:00:00Z",
        "stage_effective": "implementation_review",
        "passed": False,
        "message": "verification failed: schema checks",
        "details": {
            "commands": [
                {
                    "name": "schema_checks",
                    "status": "fail",
                    "detail": "review_result.json missing required keys",
                }
            ]
        },
    }
    (autolab_dir / "verification_result.json").write_text(
        json.dumps(verification_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    review_payload = {
        "status": "needs_retry",
        "blocking_findings": ["Resolve stale metrics evidence."],
        "required_checks": {
            "tests": "pass",
            "dry_run": "pass",
            "schema": "fail",
            "env_smoke": "pass",
            "docs_target_update": "pass",
        },
        "reviewed_at": "2026-02-01T01:00:00Z",
    }
    (iteration_dir / "review_result.json").write_text(
        json.dumps(review_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    snapshot = load_cockpit_snapshot(state_path)

    assert snapshot.verification is not None
    assert snapshot.verification.passed is False
    blocker_blob = "\n".join(snapshot.top_blockers)
    assert "verification failed: schema checks" in blocker_blob
    assert "schema_checks: review_result.json missing required keys" in blocker_blob
    assert "Resolve stale metrics evidence." in blocker_blob
    assert snapshot.primary_blocker == snapshot.top_blockers[0]
    assert snapshot.secondary_blockers == snapshot.top_blockers[1:4]
    assert any(
        item.action_id == "open_state_history" for item in snapshot.recommended_actions
    )


def test_snapshot_run_order_is_deterministic(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    state_path = repo / ".autolab" / "state.json"
    _write_state(state_path, stage="extract_results")
    runs_root = repo / "experiments" / "plan" / "iter1" / "runs"
    run_a = runs_root / "run_a"
    run_b = runs_root / "run_b"
    run_a.mkdir(parents=True, exist_ok=True)
    run_b.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamps": {"started_at": "2026-02-01T01:00:00Z"},
        "status": "running",
    }
    (run_a / "run_manifest.json").write_text(
        json.dumps({**payload, "run_id": "run_a"}, indent=2) + "\n",
        encoding="utf-8",
    )
    (run_b / "run_manifest.json").write_text(
        json.dumps({**payload, "run_id": "run_b"}, indent=2) + "\n",
        encoding="utf-8",
    )
    first = load_cockpit_snapshot(state_path)
    second = load_cockpit_snapshot(state_path)
    assert [item.run_id for item in first.runs] == [item.run_id for item in second.runs]
    assert [item.run_id for item in first.runs] == ["run_b", "run_a"]


def test_snapshot_invalid_attempt_values_fallback_safely(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    state_path = repo / ".autolab" / "state.json"
    _write_state(
        state_path,
        stage="design",
        stage_attempt="invalid",
        max_stage_attempts="bad",
    )
    snapshot = load_cockpit_snapshot(state_path)
    assert snapshot.stage_attempt == 0
    assert snapshot.max_stage_attempts == 1
    selected = next(item for item in snapshot.stage_items if item.name == "design")
    assert selected.attempts == "0/1"


def test_snapshot_skips_run_id_artifacts_when_last_run_id_missing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    state_path = repo / ".autolab" / "state.json"
    _write_state(state_path, stage="launch", last_run_id="")
    snapshot = load_cockpit_snapshot(state_path)
    launch_artifacts = snapshot.artifacts_by_stage["launch"]
    launch_paths = [item.path.as_posix() for item in launch_artifacts]
    assert all("runs/" not in path for path in launch_paths)


def test_resolve_stage_prompt_path_handles_known_and_unknown_stage(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    state_path = repo / ".autolab" / "state.json"
    _write_state(state_path, stage="design")
    snapshot = load_cockpit_snapshot(state_path)
    prompt_path = resolve_stage_prompt_path(snapshot, "design")
    assert prompt_path == snapshot.autolab_dir / "prompts" / "stage_design.md"
    assert resolve_stage_prompt_path(snapshot, "unknown_stage") is None


def test_load_artifact_text_handles_read_error(tmp_path: Path, monkeypatch) -> None:
    artifact_path = tmp_path / "notes.md"
    artifact_path.write_text("hello", encoding="utf-8")

    def _raise_open(*args, **kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "open", _raise_open)
    text, truncated = load_artifact_text(artifact_path)
    assert "Unable to read file" in text
    assert truncated is False


def test_load_artifact_text_binary_artifact_stat_failure(
    tmp_path: Path, monkeypatch
) -> None:
    artifact_path = tmp_path / "artifact.bin"

    monkeypatch.setattr(Path, "exists", lambda _self: True)
    monkeypatch.setattr("autolab.tui.snapshot.is_text_artifact", lambda _path: False)

    def _raise_stat(_self):
        raise OSError("stat denied")

    monkeypatch.setattr(Path, "stat", _raise_stat)
    text, truncated = load_artifact_text(artifact_path)
    assert "Binary/unsupported artifact (size unavailable: stat denied)." in text
    assert truncated is False


def test_load_artifact_text_truncates_and_handles_malformed_json(
    tmp_path: Path,
) -> None:
    text_path = tmp_path / "big.txt"
    text_path.write_text("abcdefghij", encoding="utf-8")
    rendered_text, truncated = load_artifact_text(text_path, max_chars=5)
    assert truncated is True
    assert rendered_text.startswith("abcde")
    assert "... [truncated]" in rendered_text

    json_path = tmp_path / "bad.json"
    json_path.write_text("{not:json}", encoding="utf-8")
    json_text, json_truncated = load_artifact_text(json_path, max_chars=50)
    assert "{not:json}" in json_text
    assert json_truncated is False


def test_load_artifact_text_non_positive_max_chars_defaults_to_one(
    tmp_path: Path,
) -> None:
    text_path = tmp_path / "small.txt"
    text_path.write_text("abcdef", encoding="utf-8")
    rendered_text, truncated = load_artifact_text(text_path, max_chars=0)
    assert truncated is True
    assert rendered_text.startswith("a")


def test_load_artifact_text_without_max_chars_reads_full_content(
    tmp_path: Path,
) -> None:
    text_path = tmp_path / "full.txt"
    text_path.write_text("abcdef", encoding="utf-8")
    rendered_text, truncated = load_artifact_text(text_path)
    assert rendered_text == "abcdef"
    assert truncated is False
