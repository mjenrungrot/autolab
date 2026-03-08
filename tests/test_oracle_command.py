from __future__ import annotations

import io
import json
from pathlib import Path
import sys

import autolab.commands as commands_module
from autolab.campaign import _refresh_campaign_results
from autolab.plan_approval import load_plan_approval
from autolab.todo_sync import list_open_tasks


def _repo_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }


def _extract_appendix_blocks(prompt_text: str) -> str:
    marker = "Required appendix blocks (paste exactly):\n"
    remainder = prompt_text.split(marker, 1)[1]
    return remainder.rsplit("\n\nNow produce the oracle document.", 1)[0].strip()


def _set_state_fields(state_path: Path, **updates: object) -> dict[str, object]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(updates)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def _write_plan_approval_fixture(repo: Path, state_path: Path) -> Path:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    iteration_id = str(state.get("iteration_id", "")).strip()
    iteration_dir = repo / "experiments" / "plan" / iteration_id
    iteration_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "generated_at": "2026-03-08T00:00:00Z",
        "iteration_id": iteration_id,
        "status": "pending",
        "requires_approval": True,
        "plan_hash": "plan-hash",
        "risk_fingerprint": "risk-fingerprint",
        "trigger_reasons": ["project_wide_tasks_present"],
        "counts": {"tasks_total": 1, "waves_total": 1},
        "reviewed_by": "",
        "reviewed_at": "",
        "notes": "",
        "source_paths": {
            "plan_contract": ".autolab/plan_contract.json",
            "plan_graph": ".autolab/plan_graph.json",
            "plan_check_result": ".autolab/plan_check_result.json",
        },
        "uat": {
            "policy_required": False,
            "effective_required": False,
            "required_by": "none",
        },
    }
    (iteration_dir / "plan_approval.json").write_text(
        json.dumps(payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (iteration_dir / "plan_approval.md").write_text(
        "# Plan Approval\n\n- notes: \n",
        encoding="utf-8",
    )
    return iteration_dir


def _write_campaign_fixture(repo: Path, *, iteration_id: str) -> None:
    (repo / ".autolab" / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": "campaign_test",
                "label": "nightly",
                "scope_kind": "experiment",
                "iteration_id": iteration_id,
                "objective_metric": "primary_metric",
                "objective_mode": "maximize",
                "status": "running",
                "design_locked": False,
                "champion_run_id": "run_baseline",
                "champion_revision_label": "unversioned-worktree",
                "no_improvement_streak": 0,
                "crash_streak": 0,
                "started_at": "2026-03-08T00:00:00Z",
                "last_oracle_at": "",
                "oracle_feedback": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _init_oracle_apply_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    state = _set_state_fields(
        state_path,
        experiment_id="exp_oracle_apply",
        stage="implementation",
    )
    iteration_id = str(state.get("iteration_id", "")).strip()
    iteration_dir = repo / "experiments" / "plan" / iteration_id
    _write_plan_approval_fixture(repo, state_path)
    _write_campaign_fixture(repo, iteration_id=iteration_id)
    return (repo, state_path, iteration_dir)


def test_oracle_writes_scope_root_document_with_inlined_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    assert commands_module.main(["progress", "--state-file", str(state_path)]) == 0

    def _fake_run_oracle_agent(_repo_root: Path, *, prompt_text: str, **_kwargs):
        appendices = _extract_appendix_blocks(prompt_text)
        return (
            0,
            "\n".join(
                [
                    "# Autolab Oracle",
                    "",
                    "## Summary",
                    "Oracle review generated from the continuation packet.",
                    "",
                    "## Continuation Packet",
                    "```json",
                    "{}",
                    "```",
                    "",
                    "## Expert Review",
                    "The handoff packet is coherent and ready for expert review.",
                    "",
                    "## Recommended Next Steps",
                    "- Run the recommended next command after reviewing blockers.",
                    "",
                    "## Artifact Guide",
                    "| Path | Role | Status | Why it matters |",
                    "| --- | --- | --- | --- |",
                    "| .autolab/handoff.json | machine_packet | present | Compact continuation source. |",
                    "",
                    "## Appendices",
                    "",
                    appendices,
                ]
            ),
            "",
            "fake-oracle",
        )

    monkeypatch.setattr(commands_module, "_run_oracle_agent", _fake_run_oracle_agent)

    before_files = _repo_files(repo)
    exit_code = commands_module.main(["oracle", "--state-file", str(state_path)])
    after_files = _repo_files(repo)

    assert exit_code == 0
    created_files = sorted(after_files - before_files)
    assert created_files == ["experiments/plan/bootstrap_iteration/oracle.md"]
    oracle_path = repo / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
    oracle_text = oracle_path.read_text(encoding="utf-8")
    assert "# Autolab Oracle" in oracle_text
    assert "### Artifact: .autolab/handoff.json" in oracle_text
    assert (
        "### Artifact: experiments/plan/bootstrap_iteration/handoff.md" in oracle_text
    )


def test_oracle_fails_when_agent_omits_required_appendix(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    assert commands_module.main(["progress", "--state-file", str(state_path)]) == 0

    def _fake_run_oracle_agent(_repo_root: Path, *, prompt_text: str, **_kwargs):
        _ = prompt_text
        return (
            0,
            "\n".join(
                [
                    "# Autolab Oracle",
                    "",
                    "## Summary",
                    "Incomplete oracle output.",
                    "",
                    "## Continuation Packet",
                    "```json",
                    "{}",
                    "```",
                    "",
                    "## Expert Review",
                    "Missing appendices on purpose.",
                    "",
                    "## Recommended Next Steps",
                    "- Retry oracle generation.",
                    "",
                    "## Artifact Guide",
                    "| Path | Role | Status | Why it matters |",
                    "| --- | --- | --- | --- |",
                    "| .autolab/handoff.json | machine_packet | present | Compact continuation source. |",
                    "",
                    "## Appendices",
                    "",
                    "### Artifact: .autolab/handoff.json",
                ]
            ),
            "",
            "fake-oracle",
        )

    monkeypatch.setattr(commands_module, "_run_oracle_agent", _fake_run_oracle_agent)

    exit_code = commands_module.main(["oracle", "--state-file", str(state_path)])

    assert exit_code == 1
    assert not (
        repo / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
    ).exists()


def test_oracle_updates_campaign_last_oracle_at(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    assert commands_module.main(["progress", "--state-file", str(state_path)]) == 0

    (repo / ".autolab" / "campaign.json").write_text(
        json.dumps(
            {
                "campaign_id": "campaign_test",
                "label": "nightly",
                "scope_kind": "experiment",
                "iteration_id": "bootstrap_iteration",
                "objective_metric": "primary_metric",
                "objective_mode": "maximize",
                "status": "running",
                "design_locked": False,
                "champion_run_id": "run_baseline",
                "champion_revision_label": "unversioned-worktree",
                "no_improvement_streak": 0,
                "crash_streak": 0,
                "started_at": "2026-03-08T00:00:00Z",
                "last_oracle_at": "",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    def _fake_run_oracle_agent(_repo_root: Path, *, prompt_text: str, **_kwargs):
        appendices = _extract_appendix_blocks(prompt_text)
        return (
            0,
            "\n".join(
                [
                    "# Autolab Oracle",
                    "",
                    "## Summary",
                    "Oracle review generated from the continuation packet.",
                    "",
                    "## Continuation Packet",
                    "```json",
                    "{}",
                    "```",
                    "",
                    "## Expert Review",
                    "The handoff packet is coherent and ready for expert review.",
                    "",
                    "## Recommended Next Steps",
                    "- Continue the campaign when ready.",
                    "",
                    "## Artifact Guide",
                    "| Path | Role | Status | Why it matters |",
                    "| --- | --- | --- | --- |",
                    "| .autolab/handoff.json | machine_packet | present | Compact continuation source. |",
                    "",
                    "## Appendices",
                    "",
                    appendices,
                ]
            ),
            "",
            "fake-oracle",
        )

    monkeypatch.setattr(commands_module, "_run_oracle_agent", _fake_run_oracle_agent)

    exit_code = commands_module.main(["oracle", "--state-file", str(state_path)])
    campaign_payload = json.loads(
        (repo / ".autolab" / "campaign.json").read_text(encoding="utf-8")
    )

    assert exit_code == 0
    assert campaign_payload["last_oracle_at"]


def test_oracle_includes_campaign_results_markdown_but_not_tsv(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    iteration_dir = repo / "experiments" / "plan" / "bootstrap_iteration"
    run_dir = iteration_dir / "runs" / "run_baseline"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "bootstrap_iteration",
                "run_id": "run_baseline",
                "status": "completed",
                "primary_metric": {
                    "name": "primary_metric",
                    "value": 1.0,
                    "delta_vs_baseline": 0.0,
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "run_id": "run_baseline",
                "iteration_id": "bootstrap_iteration",
                "status": "completed",
                "host_mode": "local",
                "command": "python run.py",
                "resource_request": {"memory": "8GB"},
                "artifact_sync_to_local": {"status": "completed"},
                "timestamps": {
                    "started_at": "2026-03-07T23:50:00Z",
                    "completed_at": "2026-03-07T23:55:00Z",
                },
                "started_at": "2026-03-07T23:50:00Z",
                "completed_at": "2026-03-07T23:55:00Z",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    campaign_payload = {
        "campaign_id": "campaign_test",
        "label": "nightly",
        "scope_kind": "experiment",
        "iteration_id": "bootstrap_iteration",
        "objective_metric": "primary_metric",
        "objective_mode": "maximize",
        "status": "running",
        "design_locked": False,
        "champion_run_id": "run_baseline",
        "champion_revision_label": "unversioned-worktree",
        "no_improvement_streak": 0,
        "crash_streak": 0,
        "started_at": "2026-03-08T00:00:00Z",
        "last_oracle_at": "",
    }
    (repo / ".autolab" / "campaign.json").write_text(
        json.dumps(campaign_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    _refresh_campaign_results(repo, campaign_payload)

    def _fake_run_oracle_agent(_repo_root: Path, *, prompt_text: str, **_kwargs):
        appendices = _extract_appendix_blocks(prompt_text)
        return (
            0,
            "\n".join(
                [
                    "# Autolab Oracle",
                    "",
                    "## Summary",
                    "Oracle review generated from the continuation packet.",
                    "",
                    "## Continuation Packet",
                    "```json",
                    "{}",
                    "```",
                    "",
                    "## Expert Review",
                    "Campaign results markdown was included.",
                    "",
                    "## Recommended Next Steps",
                    "- Review the results appendix.",
                    "",
                    "## Artifact Guide",
                    "| Path | Role | Status | Why it matters |",
                    "| --- | --- | --- | --- |",
                    "| .autolab/handoff.json | machine_packet | present | Compact continuation source. |",
                    "",
                    "## Appendices",
                    "",
                    appendices,
                ]
            ),
            "",
            "fake-oracle",
        )

    monkeypatch.setattr(commands_module, "_run_oracle_agent", _fake_run_oracle_agent)

    exit_code = commands_module.main(["oracle", "--state-file", str(state_path)])
    oracle_path = repo / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
    oracle_text = oracle_path.read_text(encoding="utf-8")

    assert exit_code == 0
    assert (
        "### Artifact: experiments/plan/bootstrap_iteration/results.md" in oracle_text
    )
    assert (
        "### Artifact: experiments/plan/bootstrap_iteration/results.tsv"
        not in oracle_text
    )


def test_oracle_apply_updates_scope_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    notes_path = iteration_dir / "expert_notes.md"
    notes_path.write_text(
        "\n".join(
            [
                "# Autolab Oracle",
                "",
                "## Summary",
                "Need tighter steering.",
                "",
                "## Expert Review",
                "Keep the patch narrow and avoid remote harness edits.",
                "",
                "## Recommended Next Steps",
                "- Compare warmup variants on the active benchmark.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def _fake_run_oracle_apply_agent(_repo_root: Path, *, prompt_text: str, **_kwargs):
        assert "Keep the patch narrow" in prompt_text
        return (
            0,
            json.dumps(
                {
                    "summary": "Applied oracle steering.",
                    "discuss_updates": {
                        "preferences": [
                            {
                                "summary": "Keep experiment edits narrow",
                                "detail": "Prefer iteration-local changes only.",
                            }
                        ],
                        "constraints": [
                            {
                                "summary": "Do not edit the remote harness",
                                "detail": "Hold the SLURM profile and evaluator contract fixed.",
                            }
                        ],
                        "open_questions": [
                            {
                                "summary": "Should the warmup schedule change?",
                                "detail": "Verify whether warmup is causing the plateau.",
                            }
                        ],
                    },
                    "research_questions": [
                        {
                            "summary": "Which training window causes the plateau?",
                            "detail": "Analyze the metric drop after epoch three.",
                        }
                    ],
                    "todo_hints": [
                        {
                            "summary": "Compare warmup variants on the active benchmark",
                            "stage": "implementation",
                            "labels": ["nightly"],
                        }
                    ],
                    "campaign_feedback": [
                        {
                            "summary": "Rethink if the next batch still stalls",
                            "detail": "Stop implementation-level search if the metric remains flat.",
                            "signal": "rethink",
                        }
                    ],
                    "plan_approval_note": "Oracle recommends a narrow patch before broader rollout.",
                }
            ),
            "",
            "fake-oracle-apply",
        )

    monkeypatch.setattr(
        commands_module,
        "_run_oracle_apply_agent",
        _fake_run_oracle_apply_agent,
    )

    exit_code = commands_module.main(
        [
            "oracle",
            "apply",
            "--state-file",
            str(state_path),
            "--notes",
            str(notes_path),
        ]
    )

    discuss_payload = json.loads(
        (iteration_dir / "context" / "sidecars" / "discuss.json").read_text(
            encoding="utf-8"
        )
    )
    research_payload = json.loads(
        (iteration_dir / "context" / "sidecars" / "research.json").read_text(
            encoding="utf-8"
        )
    )
    campaign_payload = json.loads(
        (repo / ".autolab" / "campaign.json").read_text(encoding="utf-8")
    )
    approval_payload = load_plan_approval(iteration_dir)
    open_tasks = list_open_tasks(repo)

    assert exit_code == 0
    assert any(
        entry.get("summary") == "Keep experiment edits narrow"
        for entry in discuss_payload["preferences"]
    )
    assert any(
        entry.get("summary") == "Do not edit the remote harness"
        for entry in discuss_payload["constraints"]
    )
    assert any(
        entry.get("summary") == "Should the warmup schedule change?"
        for entry in discuss_payload["open_questions"]
    )
    assert any(
        entry.get("summary") == "Which training window causes the plateau?"
        for entry in discuss_payload["open_questions"]
    )
    assert any(
        entry.get("summary") == "Which training window causes the plateau?"
        for entry in research_payload["questions"]
    )
    assert any(
        task.get("text") == "Compare warmup variants on the active benchmark"
        and "oracle" in task.get("labels", [])
        for task in open_tasks
    )
    assert campaign_payload["status"] == "needs_rethink"
    assert len(campaign_payload["oracle_feedback"]) == 1
    assert "Oracle recommends a narrow patch" in approval_payload["notes"]
    assert (repo / ".autolab" / "handoff.json").exists()


def test_oracle_apply_is_idempotent_for_duplicate_feedback(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    notes_path = iteration_dir / "expert_notes.md"
    notes_path.write_text("Keep the patch narrow.\n", encoding="utf-8")

    payload = {
        "summary": "Applied oracle steering.",
        "discuss_updates": {
            "preferences": [
                {
                    "summary": "Keep experiment edits narrow",
                    "detail": "Prefer iteration-local changes only.",
                }
            ]
        },
        "research_questions": [
            {
                "summary": "Which training window causes the plateau?",
                "detail": "Analyze the metric drop after epoch three.",
            }
        ],
        "todo_hints": [
            {
                "summary": "Compare warmup variants on the active benchmark",
                "stage": "implementation",
            }
        ],
        "campaign_feedback": [
            {
                "summary": "Rethink if the next batch still stalls",
                "detail": "Stop implementation-level search if the metric remains flat.",
                "signal": "rethink",
            }
        ],
        "plan_approval_note": "Oracle recommends a narrow patch before broader rollout.",
    }

    monkeypatch.setattr(
        commands_module,
        "_run_oracle_apply_agent",
        lambda _repo_root, **_kwargs: (0, json.dumps(payload), "", "fake-oracle-apply"),
    )

    for _ in range(2):
        assert (
            commands_module.main(
                [
                    "oracle",
                    "apply",
                    "--state-file",
                    str(state_path),
                    "--notes",
                    str(notes_path),
                ]
            )
            == 0
        )

    discuss_payload = json.loads(
        (iteration_dir / "context" / "sidecars" / "discuss.json").read_text(
            encoding="utf-8"
        )
    )
    research_payload = json.loads(
        (iteration_dir / "context" / "sidecars" / "research.json").read_text(
            encoding="utf-8"
        )
    )
    campaign_payload = json.loads(
        (repo / ".autolab" / "campaign.json").read_text(encoding="utf-8")
    )
    approval_payload = load_plan_approval(iteration_dir)
    open_tasks = list_open_tasks(repo)

    assert (
        len(
            [
                entry
                for entry in discuss_payload["preferences"]
                if entry.get("summary") == "Keep experiment edits narrow"
            ]
        )
        == 1
    )
    assert (
        len(
            [
                entry
                for entry in research_payload["questions"]
                if entry.get("summary") == "Which training window causes the plateau?"
            ]
        )
        == 1
    )
    assert (
        len(
            [
                task
                for task in open_tasks
                if task.get("text") == "Compare warmup variants on the active benchmark"
            ]
        )
        == 1
    )
    assert len(campaign_payload["oracle_feedback"]) == 1
    assert (
        approval_payload["notes"].count(
            "Oracle recommends a narrow patch before broader rollout."
        )
        == 1
    )


def test_oracle_apply_rejects_invalid_classifier_output_without_writes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    notes_path = iteration_dir / "expert_notes.md"
    notes_path.write_text("Keep the patch narrow.\n", encoding="utf-8")
    campaign_before = (repo / ".autolab" / "campaign.json").read_text(encoding="utf-8")
    approval_before = load_plan_approval(iteration_dir)
    open_tasks_before = list_open_tasks(repo)

    monkeypatch.setattr(
        commands_module,
        "_run_oracle_apply_agent",
        lambda _repo_root, **_kwargs: (
            0,
            json.dumps(
                {"campaign_feedback": [{"summary": "stop now", "signal": "halt"}]}
            ),
            "",
            "fake-oracle-apply",
        ),
    )

    exit_code = commands_module.main(
        [
            "oracle",
            "apply",
            "--state-file",
            str(state_path),
            "--notes",
            str(notes_path),
        ]
    )

    assert exit_code == 1
    assert not (iteration_dir / "context" / "sidecars" / "discuss.json").exists()
    assert not (iteration_dir / "context" / "sidecars" / "research.json").exists()
    assert (repo / ".autolab" / "campaign.json").read_text(
        encoding="utf-8"
    ) == campaign_before
    assert load_plan_approval(iteration_dir) == approval_before
    assert list_open_tasks(repo) == open_tasks_before


def test_oracle_apply_extracts_review_sections_from_export_markdown(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    notes_path = iteration_dir / "oracle.md"
    notes_path.write_text(
        "\n".join(
            [
                "# Autolab Oracle",
                "",
                "## Summary",
                "Dense export.",
                "",
                "## Expert Review",
                "Need a narrower patch before retrying the campaign.",
                "",
                "## Recommended Next Steps",
                "- Run one more comparison from the current champion.",
                "",
                "## Appendices",
                "",
                "### Artifact: .autolab/handoff.json",
                "",
                "```json",
                "{}",
                "```",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def _fake_run_oracle_apply_agent(_repo_root: Path, *, prompt_text: str, **_kwargs):
        assert "Need a narrower patch before retrying the campaign." in prompt_text
        assert "Run one more comparison from the current champion." in prompt_text
        assert "### Artifact: .autolab/handoff.json" not in prompt_text
        return (
            0,
            json.dumps(
                {
                    "summary": "No changes applied.",
                    "discuss_updates": {},
                    "research_questions": [],
                    "todo_hints": [],
                    "campaign_feedback": [],
                    "plan_approval_note": "",
                }
            ),
            "",
            "fake-oracle-apply",
        )

    monkeypatch.setattr(
        commands_module,
        "_run_oracle_apply_agent",
        _fake_run_oracle_apply_agent,
    )

    assert (
        commands_module.main(
            [
                "oracle",
                "apply",
                "--state-file",
                str(state_path),
                "--notes",
                str(notes_path),
            ]
        )
        == 0
    )


def test_oracle_apply_can_read_notes_from_stdin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, state_path, _iteration_dir = _init_oracle_apply_repo(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("stdin oracle notes\n"))

    def _fake_run_oracle_apply_agent(_repo_root: Path, *, prompt_text: str, **_kwargs):
        assert "stdin oracle notes" in prompt_text
        return (
            0,
            json.dumps(
                {
                    "summary": "No changes applied.",
                    "discuss_updates": {},
                    "research_questions": [],
                    "todo_hints": [],
                    "campaign_feedback": [],
                    "plan_approval_note": "",
                }
            ),
            "",
            "fake-oracle-apply",
        )

    monkeypatch.setattr(
        commands_module,
        "_run_oracle_apply_agent",
        _fake_run_oracle_apply_agent,
    )

    assert (
        commands_module.main(
            [
                "oracle",
                "apply",
                "--state-file",
                str(state_path),
                "--stdin",
            ]
        )
        == 0
    )
