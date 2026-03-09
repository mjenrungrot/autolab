from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import autolab.cli.handlers_admin as handlers_admin
import autolab.commands as commands_module
import pytest
from autolab.campaign import _refresh_campaign_results
from autolab.cli.handlers_admin import (
    _build_oracle_browser_argv,
    _run_oracle_roundtrip_auto,
    _run_oracle_roundtrip_dry_run_full,
)
from autolab.cli.handlers_run import _auto_oracle_trigger_reason
from autolab.models import OracleApplyPolicyConfig, OraclePolicyConfig, RunOutcome
from autolab.oracle_runtime import oracle_packet_fingerprint, parse_oracle_reply
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


def _write_oracle_reply(
    path: Path,
    *,
    verdict: str,
    rationale: list[str] | tuple[str, ...] = (),
    actions: list[str] | tuple[str, ...] = (),
    risks: list[str] | tuple[str, ...] = (),
) -> None:
    lines = [
        "# Expert Review Response",
        "",
        f"ReviewerVerdict: {verdict}",
        "",
        "## Rationale",
    ]
    if rationale:
        lines.extend(f"- {item}" for item in rationale)
    else:
        lines.append("- no rationale provided")
    lines.extend(["", "## Recommended Actions"])
    if actions:
        lines.extend(f"1. {item}" for item in actions)
    else:
        lines.append("1. no action provided")
    lines.extend(["", "## Risks"])
    if risks:
        lines.extend(f"- {item}" for item in risks)
    else:
        lines.append("- no risks recorded")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _oracle_policy(
    *,
    auto_allowed: bool = True,
    apply_on_success: bool = True,
    preview_before_send: bool = True,
) -> OraclePolicyConfig:
    return OraclePolicyConfig(
        auto_allowed=auto_allowed,
        mode="browser_only",
        max_auto_attempts_per_epoch=1,
        timeout_minutes=60,
        browser_model_strategy="current",
        browser_manual_login_profile_required=True,
        browser_auto_reattach_delay="30s",
        browser_auto_reattach_interval="2m",
        browser_auto_reattach_timeout="2m",
        preview_before_send=preview_before_send,
        apply_on_success=apply_on_success,
        graceful_failure=True,
    )


def _oracle_apply_policy(
    *,
    ingestion_mode: str = "hybrid",
    llm_command: str = "",
    llm_timeout_seconds: float = 300.0,
    allow_continue_search: bool = True,
    allow_switch_family: bool = True,
    allow_rewind_design: bool = False,
    allow_request_human_review: bool = True,
    allow_stop_campaign: bool = True,
) -> OracleApplyPolicyConfig:
    return OracleApplyPolicyConfig(
        ingestion_mode=ingestion_mode,
        llm_command=llm_command,
        llm_timeout_seconds=llm_timeout_seconds,
        allow_continue_search=allow_continue_search,
        allow_switch_family=allow_switch_family,
        allow_rewind_design=allow_rewind_design,
        allow_request_human_review=allow_request_human_review,
        allow_stop_campaign=allow_stop_campaign,
    )


def test_oracle_packet_fingerprint_ignores_generated_at_and_oracle_fields() -> None:
    base_packet = {
        "schema_version": "1.0",
        "generated_at": "2026-03-08T00:00:00Z",
        "active_stage": {
            "stage": "design",
            "scope_kind": "experiment",
            "scope_root": "/tmp/repo/experiments/plan/bootstrap_iteration",
        },
        "next_action": {
            "recommended_command": "autolab run",
            "safe_status": "ready",
        },
        "diagnostics": ["plan_graph.json unavailable"],
        "oracle_epoch": "epoch-a",
        "oracle_auto_status": "succeeded",
        "oracle_verdict": "rethink_design",
        "oracle_suggested_next_action": "return to design",
        "oracle_attempt_window": "1/1 this epoch",
    }
    variant_packet = {
        **base_packet,
        "generated_at": "2026-03-08T23:38:12Z",
        "oracle_epoch": "epoch-b",
        "oracle_auto_status": "not_attempted",
        "oracle_verdict": "",
        "oracle_suggested_next_action": "",
        "oracle_attempt_window": "0/1 this epoch",
    }

    assert oracle_packet_fingerprint(base_packet) == oracle_packet_fingerprint(
        variant_packet
    )


def test_oracle_writes_scope_root_document_with_inlined_artifacts(
    tmp_path: Path,
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

    before_files = _repo_files(repo)
    exit_code = commands_module.main(["oracle", "--state-file", str(state_path)])
    after_files = _repo_files(repo)

    assert exit_code == 0
    created_files = sorted(after_files - before_files)
    assert created_files == ["experiments/plan/bootstrap_iteration/oracle.md"]
    oracle_path = repo / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
    oracle_text = oracle_path.read_text(encoding="utf-8")
    assert oracle_text.splitlines()[0] == "# plan_graph.json unavailable"
    assert "## Relevant Files and Excerpts" in oracle_text
    assert "## Instructions for Reviewer" in oracle_text
    assert "ReviewerVerdict:" in oracle_text
    assert "# Expert Review Handoff" not in oracle_text
    assert str(repo.resolve()) not in oracle_text
    assert ".autolab/handoff.json" not in oracle_text


def test_oracle_fails_when_source_collection_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
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
    monkeypatch.setattr(
        commands_module,
        "_oracle_collect_sources",
        lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("inline sources unavailable")
        ),
    )

    exit_code = commands_module.main(["oracle", "--state-file", str(state_path)])

    assert exit_code == 1
    assert "inline sources unavailable" in capsys.readouterr().err
    assert not (
        repo / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
    ).exists()


def test_oracle_updates_campaign_last_oracle_at(tmp_path: Path) -> None:
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

    exit_code = commands_module.main(["oracle", "--state-file", str(state_path)])
    campaign_payload = json.loads(
        (repo / ".autolab" / "campaign.json").read_text(encoding="utf-8")
    )

    assert exit_code == 0
    assert campaign_payload["last_oracle_at"]


def test_oracle_includes_campaign_results_markdown_but_not_tsv(tmp_path: Path) -> None:
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

    exit_code = commands_module.main(["oracle", "--state-file", str(state_path)])
    oracle_path = repo / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
    oracle_text = oracle_path.read_text(encoding="utf-8")

    assert exit_code == 0
    assert "### `experiments/plan/bootstrap_iteration/results.md`" in oracle_text
    assert "### `experiments/plan/bootstrap_iteration/results.tsv`" not in oracle_text


def test_oracle_apply_updates_scope_artifacts(tmp_path: Path) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    reply_path = iteration_dir / "oracle_reply.md"
    _write_oracle_reply(
        reply_path,
        verdict="rethink_design",
        rationale=[
            "Keep experiment edits narrow and iteration-local.",
            "Prefer design changes before another implementation wave.",
        ],
        actions=[
            "Compare warmup variants on the active benchmark.",
            "Add a narrower design note before the next run.",
        ],
        risks=[
            "Could the warmup schedule still be masking regressions?",
            "Remote harness edits would make comparisons invalid.",
        ],
    )

    result = handlers_admin._apply_oracle_reply_text(
        state_path=state_path,
        repo_root=repo,
        state=handlers_admin._normalize_state(handlers_admin._load_state(state_path)),
        source_label="oracle_reply.md",
        raw_notes=reply_path.read_text(encoding="utf-8"),
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
    oracle_state = json.loads(
        (repo / ".autolab" / "oracle_state.json").read_text(encoding="utf-8")
    )
    approval_payload = load_plan_approval(iteration_dir)
    open_tasks = list_open_tasks(repo)

    assert result["ingestion_path"] == "strict"
    assert any(
        entry.get("summary") == "Keep experiment edits narrow and iteration-local."
        for entry in discuss_payload["preferences"]
    )
    assert any(
        entry.get("summary")
        == "Prefer design changes before another implementation wave."
        for entry in discuss_payload["preferences"]
    )
    assert any(
        entry.get("summary")
        == "Could the warmup schedule still be masking regressions?"
        for entry in discuss_payload["open_questions"]
    )
    assert any(
        entry.get("summary") == "Remote harness edits would make comparisons invalid."
        for entry in discuss_payload["constraints"]
    )
    assert any(
        entry.get("summary")
        == "Could the warmup schedule still be masking regressions?"
        for entry in research_payload["questions"]
    )
    assert any(
        task.get("text") == "Compare warmup variants on the active benchmark."
        for task in open_tasks
    )
    assert campaign_payload["status"] == "needs_rethink"
    assert len(campaign_payload["oracle_feedback"]) == 1
    assert "Oracle verdict: rethink_design." in approval_payload["notes"]
    assert oracle_state["verdict"] == "rethink_design"
    assert (
        oracle_state["suggested_next_action"]
        == "Compare warmup variants on the active benchmark."
    )
    assert (repo / ".autolab" / "handoff.json").exists()


def test_oracle_apply_uses_llm_ingestion_for_free_form_reply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    reply_path = iteration_dir / "oracle_reply.md"
    reply_path.write_text(
        "\n".join(
            [
                "# Expert Review Response",
                "",
                "The implementation path is still too broad and the current benchmark evidence is not enough to justify another run.",
                "Return to design, compare the warmup variants directly, and have a human sanity-check the next experiment setup.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "autolab.config._load_oracle_apply_policy",
        lambda *_args, **_kwargs: _oracle_apply_policy(
            ingestion_mode="hybrid",
            llm_command="mock-ingest",
        ),
    )
    captured_commands: list[str] = []

    def _fake_run_local_agent(_repo_root: Path, **_kwargs):
        captured_commands.append(str(_kwargs.get("command_override", "")))
        payload = {
            "verdict": "rethink_design",
            "suggested_next_action": "Compare warmup variants on the active benchmark.",
            "recommended_human_review": True,
            "summary": "LLM extracted a rethink-design recommendation.",
            "discuss_updates": {
                "preferences": [
                    {
                        "summary": "Keep experiment edits narrow and iteration-local.",
                        "detail": "Do not broaden the patch until the warmup schedule is understood.",
                    }
                ],
                "constraints": [
                    {
                        "summary": "Remote harness edits would make comparisons invalid.",
                        "detail": "Preserve the benchmark harness while isolating the warmup comparison.",
                    }
                ],
            },
            "research_questions": [
                {
                    "summary": "Could the warmup schedule still be masking regressions?",
                    "detail": "Check whether the plateau disappears when the warmup is narrowed.",
                }
            ],
            "todo_hints": [
                {
                    "stage": "implementation",
                    "text": "Compare warmup variants on the active benchmark.",
                    "rationale": "The reviewer wants one isolated design follow-up.",
                }
            ],
            "campaign_feedback": [
                {
                    "summary": "Current family needs a design rethink.",
                    "detail": "The reviewer recommends stepping back before another autonomous run.",
                    "signal": "rethink",
                }
            ],
            "plan_approval_note": "Oracle verdict: rethink_design. Re-scope the next run around the warmup comparison.",
        }
        return (0, json.dumps(payload), "", "mock-ingest")

    monkeypatch.setattr(handlers_admin, "_run_local_agent", _fake_run_local_agent)

    result = handlers_admin._apply_oracle_reply_text(
        state_path=state_path,
        repo_root=repo,
        state=handlers_admin._normalize_state(handlers_admin._load_state(state_path)),
        source_label="oracle_reply.md",
        raw_notes=reply_path.read_text(encoding="utf-8"),
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
    oracle_state = json.loads(
        (repo / ".autolab" / "oracle_state.json").read_text(encoding="utf-8")
    )
    approval_payload = load_plan_approval(iteration_dir)
    assert captured_commands == ["mock-ingest"]
    assert result["ingestion_path"] == "llm"
    assert any(
        entry.get("summary") == "Keep experiment edits narrow and iteration-local."
        for entry in discuss_payload["preferences"]
    )
    assert any(
        entry.get("summary") == "Remote harness edits would make comparisons invalid."
        for entry in discuss_payload["constraints"]
    )
    assert any(
        entry.get("summary")
        == "Could the warmup schedule still be masking regressions?"
        for entry in research_payload["questions"]
    )
    assert campaign_payload["status"] == "needs_rethink"
    assert len(campaign_payload["oracle_feedback"]) == 1
    assert "Oracle verdict: rethink_design." in approval_payload["notes"]
    assert oracle_state["verdict"] == "rethink_design"
    assert (
        oracle_state["suggested_next_action"]
        == "Compare warmup variants on the active benchmark."
    )
    assert oracle_state["recommended_human_review"] is True


def test_oracle_apply_request_human_review_is_advisory_only(tmp_path: Path) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    reply_path = iteration_dir / "oracle_reply.md"
    _write_oracle_reply(
        reply_path,
        verdict="request_human_review",
        rationale=["The benchmark evidence is internally contradictory."],
        actions=["Collect one manual sanity check before the next run."],
        risks=["Could the current evaluator be masking a regression?"],
    )

    exit_code = commands_module.main(
        ["oracle", "apply", "--state-file", str(state_path), str(reply_path)]
    )

    campaign_payload = json.loads(
        (repo / ".autolab" / "campaign.json").read_text(encoding="utf-8")
    )
    oracle_state = json.loads(
        (repo / ".autolab" / "oracle_state.json").read_text(encoding="utf-8")
    )

    assert exit_code == 0
    assert campaign_payload["status"] == "running"
    assert len(campaign_payload["oracle_feedback"]) == 1
    assert oracle_state["verdict"] == "request_human_review"
    assert oracle_state["recommended_human_review"] is True
    assert oracle_state["current_epoch"]


def test_oracle_apply_disallowed_stop_campaign_keeps_campaign_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    reply_path = iteration_dir / "oracle_reply.md"
    _write_oracle_reply(
        reply_path,
        verdict="stop_campaign",
        rationale=["The current family has exhausted its useful search space."],
        actions=["Stop the current campaign and revisit the search framing."],
        risks=["Could the baseline comparison still be noisy?"],
    )
    monkeypatch.setattr(
        "autolab.config._load_oracle_apply_policy",
        lambda *_args, **_kwargs: _oracle_apply_policy(
            allow_stop_campaign=False,
        ),
    )

    exit_code = commands_module.main(
        ["oracle", "apply", "--state-file", str(state_path), str(reply_path)]
    )

    campaign_payload = json.loads(
        (repo / ".autolab" / "campaign.json").read_text(encoding="utf-8")
    )
    oracle_state = json.loads(
        (repo / ".autolab" / "oracle_state.json").read_text(encoding="utf-8")
    )

    assert exit_code == 0
    assert campaign_payload["status"] == "running"
    assert len(campaign_payload["oracle_feedback"]) == 1
    assert oracle_state["verdict"] == "stop_campaign"


def test_oracle_apply_is_idempotent_for_duplicate_feedback(tmp_path: Path) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    notes_path = iteration_dir / "oracle_reply.md"
    _write_oracle_reply(
        notes_path,
        verdict="rethink_design",
        rationale=["Keep experiment edits narrow and iteration-local."],
        actions=["Compare warmup variants on the active benchmark."],
        risks=["Which training window causes the plateau?"],
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
                if entry.get("summary")
                == "Keep experiment edits narrow and iteration-local."
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
                if task.get("text")
                == "Compare warmup variants on the active benchmark."
            ]
        )
        == 1
    )
    assert len(campaign_payload["oracle_feedback"]) == 1
    assert approval_payload["notes"].count("Oracle verdict: rethink_design.") == 1


def test_oracle_apply_rejects_missing_verdict_without_writes(tmp_path: Path) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    notes_path = iteration_dir / "oracle_reply.md"
    notes_path.write_text(
        "# Expert Review Response\n\n## Rationale\n- Missing required verdict.\n",
        encoding="utf-8",
    )
    campaign_before = (repo / ".autolab" / "campaign.json").read_text(encoding="utf-8")
    approval_before = load_plan_approval(iteration_dir)
    open_tasks_before = list_open_tasks(repo)

    exit_code = commands_module.main(
        ["oracle", "apply", "--state-file", str(state_path), str(notes_path)]
    )

    assert exit_code == 1
    assert not (iteration_dir / "context" / "sidecars" / "discuss.json").exists()
    assert not (iteration_dir / "context" / "sidecars" / "research.json").exists()
    assert (repo / ".autolab" / "campaign.json").read_text(
        encoding="utf-8"
    ) == campaign_before
    assert load_plan_approval(iteration_dir) == approval_before
    assert list_open_tasks(repo) == open_tasks_before
    assert not (repo / ".autolab" / "oracle_state.json").exists()


def test_oracle_apply_strict_only_rejects_free_form_without_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    notes_path = iteration_dir / "oracle_reply.md"
    notes_path.write_text(
        "# Expert Review Response\n\nThe reviewer wants another design pass before the next run.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "autolab.config._load_oracle_apply_policy",
        lambda *_args, **_kwargs: _oracle_apply_policy(ingestion_mode="strict_only"),
    )
    campaign_before = (repo / ".autolab" / "campaign.json").read_text(encoding="utf-8")
    approval_before = load_plan_approval(iteration_dir)
    open_tasks_before = list_open_tasks(repo)

    exit_code = commands_module.main(
        ["oracle", "apply", "--state-file", str(state_path), str(notes_path)]
    )

    assert exit_code == 1
    assert not (iteration_dir / "context" / "sidecars" / "discuss.json").exists()
    assert not (iteration_dir / "context" / "sidecars" / "research.json").exists()
    assert (repo / ".autolab" / "campaign.json").read_text(
        encoding="utf-8"
    ) == campaign_before
    assert load_plan_approval(iteration_dir) == approval_before
    assert list_open_tasks(repo) == open_tasks_before
    assert not (repo / ".autolab" / "oracle_state.json").exists()


def test_oracle_apply_extracts_review_sections_from_export_markdown(
    tmp_path: Path,
) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    notes_path = iteration_dir / "oracle.md"
    notes_path.write_text(
        "\n".join(
            [
                "# plan_graph.json unavailable",
                "",
                "## Executive Summary",
                "Dense export.",
                "",
                "ReviewerVerdict: continue_search",
                "",
                "## Rationale",
                "- Need a narrower patch before retrying the campaign.",
                "",
                "## Recommended Actions",
                "1. Run one more comparison from the current champion.",
                "",
                "## Risks",
                "- Could the current baseline still be noisy?",
                "",
                "## Instructions for Reviewer",
                "Answer directly.",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
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


def test_parse_oracle_reply_keeps_nested_recommended_actions_grouped() -> None:
    reply = parse_oracle_reply(
        "\n".join(
            [
                "ReviewerVerdict: rethink_design",
                "",
                "## Rationale",
                "- The packet is incomplete.",
                "",
                "## Recommended Actions",
                "1. Replace hypothesis.md with an actual falsifiable hypothesis:",
                "   * intervention/change being tested",
                "   * baseline",
                "2. Reconstruct the missing planning artifacts before any further run:",
                "   * plan_graph.json",
                "   * plan_execution_summary.json",
                "",
                "## Risks",
                "- False progress.",
            ]
        )
    )

    assert reply.recommended_actions == (
        "Replace hypothesis.md with an actual falsifiable hypothesis: Substeps: intervention/change being tested Substeps: baseline",
        "Reconstruct the missing planning artifacts before any further run: Substeps: plan_graph.json Substeps: plan_execution_summary.json",
    )


def test_oracle_apply_groups_nested_recommendations_into_single_todos(
    tmp_path: Path,
) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    notes_path = iteration_dir / "oracle_nested_reply.md"
    notes_path.write_text(
        "\n".join(
            [
                "ReviewerVerdict: rethink_design",
                "",
                "## Rationale",
                "- The packet is incomplete.",
                "",
                "## Recommended Actions",
                "1. Replace hypothesis.md with an actual falsifiable hypothesis:",
                "   * intervention/change being tested",
                "   * baseline",
                "2. Reconstruct the missing planning artifacts before any further run:",
                "   * plan_graph.json",
                "   * plan_execution_summary.json",
                "",
                "## Risks",
                "- False progress.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        commands_module.main(
            ["oracle", "apply", "--state-file", str(state_path), str(notes_path)]
        )
        == 0
    )

    todo_text = (repo / "docs" / "todo.md").read_text(encoding="utf-8")
    assert (
        "- [stage:implementation] Replace hypothesis.md with an actual falsifiable hypothesis: Substeps: intervention/change being tested Substeps: baseline"
        in todo_text
    )
    assert (
        "- [stage:implementation] Reconstruct the missing planning artifacts before any further run: Substeps: plan_graph.json Substeps: plan_execution_summary.json"
        in todo_text
    )
    assert "- [stage:implementation] intervention/change being tested" not in todo_text
    assert "- [stage:implementation] plan_graph.json" not in todo_text


def test_oracle_export_validation_rejects_template_heading(tmp_path: Path) -> None:
    validation_error = handlers_admin._validate_oracle_output(
        "\n".join(
            [
                "# Expert Review Handoff",
                "",
                "## Executive Summary",
                "summary",
                "",
                "## Project Context",
                "- context",
                "",
                "## Current Problem",
                "- issue",
                "",
                "## Evidence and Constraints",
                "- evidence",
                "",
                "## Relevant Files and Excerpts",
                "none",
                "",
                "## Requested Response Format",
                "free-form is acceptable",
                "",
                "## Instructions for Reviewer",
                "be candid",
            ]
        ),
        repo_root=tmp_path,
        sources=[],
    )

    assert (
        validation_error
        == "review handoff title must use the actual request, not a template heading"
    )


def test_oracle_apply_can_read_notes_from_stdin(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo, state_path, _iteration_dir = _init_oracle_apply_repo(tmp_path)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            "\n".join(
                [
                    "ReviewerVerdict: continue_search",
                    "",
                    "## Rationale",
                    "- stdin rationale",
                    "",
                    "## Recommended Actions",
                    "1. Keep iterating from the current family.",
                    "",
                    "## Risks",
                    "- Could the verifier still be too loose?",
                ]
            )
            + "\n"
        ),
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


def test_oracle_apply_llm_only_bypasses_strict_parser(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    notes_path = iteration_dir / "oracle_reply.md"
    notes_path.write_text(
        "# Expert Review Response\n\nKeep iterating from the current family, but narrow the benchmark diff.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "autolab.config._load_oracle_apply_policy",
        lambda *_args, **_kwargs: _oracle_apply_policy(
            ingestion_mode="llm_only",
            llm_command="mock-ingest",
        ),
    )
    monkeypatch.setattr(
        handlers_admin,
        "parse_oracle_reply",
        lambda *_args, **_kwargs: pytest.fail(
            "strict parser should not run in llm_only mode"
        ),
    )
    llm_calls: list[str] = []

    def _fake_run_local_agent(_repo_root: Path, **_kwargs):
        llm_calls.append(str(_kwargs.get("command_override", "")))
        payload = {
            "verdict": "continue_search",
            "suggested_next_action": "Run one more benchmark comparison.",
            "recommended_human_review": False,
            "summary": "LLM-only ingestion kept the campaign moving.",
            "discuss_updates": {},
            "research_questions": [],
            "todo_hints": [],
            "campaign_feedback": [],
            "plan_approval_note": "",
        }
        return (0, json.dumps(payload), "", "mock-ingest")

    monkeypatch.setattr(handlers_admin, "_run_local_agent", _fake_run_local_agent)

    result = handlers_admin._apply_oracle_reply_text(
        state_path=state_path,
        repo_root=repo,
        state=handlers_admin._normalize_state(handlers_admin._load_state(state_path)),
        source_label="oracle_reply.md",
        raw_notes=notes_path.read_text(encoding="utf-8"),
    )
    oracle_state = json.loads(
        (repo / ".autolab" / "oracle_state.json").read_text(encoding="utf-8")
    )

    assert llm_calls == ["mock-ingest"]
    assert result["ingestion_path"] == "llm"
    assert oracle_state["verdict"] == "continue_search"


def test_oracle_apply_free_form_ingestion_failure_keeps_repo_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, state_path, iteration_dir = _init_oracle_apply_repo(tmp_path)
    notes_path = iteration_dir / "oracle_reply.md"
    notes_path.write_text(
        "# Expert Review Response\n\nThe current plan looks contradictory and needs follow-up.\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "autolab.config._load_oracle_apply_policy",
        lambda *_args, **_kwargs: _oracle_apply_policy(
            ingestion_mode="hybrid",
            llm_command="mock-ingest",
        ),
    )
    monkeypatch.setattr(
        handlers_admin,
        "_run_local_agent",
        lambda _repo_root, **_kwargs: (0, "{not-json", "", "mock-ingest"),
    )
    campaign_before = (repo / ".autolab" / "campaign.json").read_text(encoding="utf-8")
    approval_before = load_plan_approval(iteration_dir)
    open_tasks_before = list_open_tasks(repo)

    with pytest.raises(ValueError, match="valid JSON"):
        handlers_admin._apply_oracle_reply_text(
            state_path=state_path,
            repo_root=repo,
            state=handlers_admin._normalize_state(
                handlers_admin._load_state(state_path)
            ),
            source_label="oracle_reply.md",
            raw_notes=notes_path.read_text(encoding="utf-8"),
        )

    assert not (iteration_dir / "context" / "sidecars" / "discuss.json").exists()
    assert not (iteration_dir / "context" / "sidecars" / "research.json").exists()
    assert (repo / ".autolab" / "campaign.json").read_text(
        encoding="utf-8"
    ) == campaign_before
    assert load_plan_approval(iteration_dir) == approval_before
    assert list_open_tasks(repo) == open_tasks_before
    assert not (repo / ".autolab" / "oracle_state.json").exists()


def test_oracle_roundtrip_requires_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )

    assert (
        commands_module.main(["oracle", "roundtrip", "--state-file", str(state_path)])
        == 1
    )


def test_oracle_roundtrip_runs_dry_run_full_path_when_requested(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
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

    captured: dict[str, str] = {}

    def _fake_run_oracle_roundtrip_dry_run_full(
        *,
        state_path: Path,
        repo_root: Path,
        output_path: Path | None = None,
    ) -> dict[str, object]:
        captured["state_path"] = str(state_path)
        captured["repo_root"] = str(repo_root)
        captured["output_path"] = str(output_path) if output_path is not None else ""
        return {
            "exit_code": 0,
            "attempted": False,
            "status": "preview_succeeded",
            "failure_reason": "",
            "output_path": str(
                repo_root / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
            ),
            "reply_path": "",
            "export_command": "autolab oracle",
            "browser_command": "",
            "preview_command": "oracle --engine browser --dry-run full --prompt 'test' --file oracle.md",
            "source_count": 3,
            "apply_status": "",
            "preview_output": "# preview bundle",
        }

    monkeypatch.setattr(
        commands_module,
        "_run_oracle_roundtrip_dry_run_full",
        _fake_run_oracle_roundtrip_dry_run_full,
    )

    output_path = repo / "custom_oracle.md"
    assert (
        commands_module.main(
            [
                "oracle",
                "roundtrip",
                "--state-file",
                str(state_path),
                "--dry-run-full",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert captured["state_path"] == str(state_path.resolve())
    assert captured["repo_root"] == str(repo.resolve())
    assert captured["output_path"] == str(output_path)
    assert "oracle_preview_command: oracle --engine browser" in printed
    assert "--dry-run full" in printed
    assert "preview_output:" in printed


def test_oracle_roundtrip_runs_auto_path_when_enabled(
    tmp_path: Path,
    monkeypatch,
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

    captured: dict[str, str] = {}

    def _fake_run_oracle_roundtrip_auto(
        *,
        state_path: Path,
        repo_root: Path,
        trigger_reason: str,
        output_path: Path | None = None,
    ) -> dict[str, object]:
        captured["state_path"] = str(state_path)
        captured["repo_root"] = str(repo_root)
        captured["trigger_reason"] = trigger_reason
        captured["output_path"] = str(output_path) if output_path is not None else ""
        return {
            "exit_code": 0,
            "attempted": True,
            "status": "succeeded",
            "failure_reason": "",
            "output_path": str(
                repo_root / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
            ),
            "reply_path": str(repo_root / ".autolab" / "oracle_last_response.md"),
            "export_command": "autolab oracle",
            "browser_command": "oracle --engine browser",
            "preview_command": "oracle --engine browser --dry-run full --prompt 'test' --file oracle.md",
            "source_count": 3,
            "apply_status": "applied",
        }

    monkeypatch.setattr(
        commands_module,
        "_run_oracle_roundtrip_auto",
        _fake_run_oracle_roundtrip_auto,
    )

    output_path = repo / "custom_oracle.md"
    assert (
        commands_module.main(
            [
                "oracle",
                "roundtrip",
                "--state-file",
                str(state_path),
                "--auto",
                "--output",
                str(output_path),
            ]
        )
        == 0
    )
    assert captured["state_path"] == str(state_path.resolve())
    assert captured["repo_root"] == str(repo.resolve())
    assert captured["trigger_reason"] == "manual automation request"
    assert captured["output_path"] == str(output_path)


def test_oracle_roundtrip_recommends_dry_run_full_when_auto_fails(
    tmp_path: Path,
    monkeypatch,
    capsys: pytest.CaptureFixture[str],
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

    monkeypatch.setattr(
        commands_module,
        "_run_oracle_roundtrip_auto",
        lambda **_kwargs: {
            "exit_code": 1,
            "attempted": True,
            "status": "launch_failed",
            "failure_reason": "browser launch failed",
            "output_path": str(
                repo / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
            ),
            "reply_path": "",
            "export_command": "autolab oracle",
            "browser_command": "oracle --engine browser",
            "preview_command": "oracle --engine browser --dry-run full --prompt 'test' --file oracle.md",
            "source_count": 3,
            "apply_status": "",
        },
    )

    assert (
        commands_module.main(
            ["oracle", "roundtrip", "--state-file", str(state_path), "--auto"]
        )
        == 1
    )
    printed = capsys.readouterr().out
    assert "recommended_debug_command: oracle --engine browser" in printed
    assert "--dry-run full" in printed


def test_oracle_roundtrip_dry_run_full_runs_preview_without_reply_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    oracle_output_path = (
        repo / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
    )
    oracle_output_path.parent.mkdir(parents=True, exist_ok=True)
    oracle_output_path.write_text("# Oracle\n", encoding="utf-8")
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._export_oracle_document",
        lambda **_kwargs: (oracle_output_path, 1, "autolab oracle"),
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin.shutil.which",
        lambda _name: "/usr/local/bin/oracle",
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._oracle_cli_supports_flag",
        lambda flag: flag in {"--write-output", "--browser-manual-login"},
    )

    captured: dict[str, list[str]] = {}

    def _fake_run_oracle_browser_cli(*, argv: list[str], **_kwargs):
        captured["argv"] = list(argv)
        return (0, "# full preview bundle", "", "oracle --dry-run full")

    monkeypatch.setattr(
        "autolab.cli.handlers_admin._run_oracle_browser_cli",
        _fake_run_oracle_browser_cli,
    )

    result = _run_oracle_roundtrip_dry_run_full(
        state_path=state_path,
        repo_root=repo,
    )

    assert result["status"] == "preview_succeeded"
    assert result["attempted"] is False
    assert result["reply_path"] == ""
    assert result["preview_output"] == "# full preview bundle"
    assert captured["argv"][:3] == ["oracle", "--engine", "browser"]
    assert "--dry-run" in captured["argv"]
    assert "full" in captured["argv"]
    assert "--write-output" in captured["argv"]
    assert "--files-report" not in captured["argv"]


def test_build_oracle_browser_argv_uses_prompt_and_output_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "oracle.md"
    bundle_path.write_text("# Oracle\n", encoding="utf-8")
    reply_path = tmp_path / "reply.md"
    supported_flags = {
        "--browser-model-strategy",
        "--browser-manual-login",
        "--browser-auto-reattach-delay",
        "--browser-auto-reattach-interval",
        "--browser-auto-reattach-timeout",
        "--timeout",
        "--wait",
        "--write-output",
    }
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._oracle_cli_supports_flag",
        lambda flag: flag in supported_flags,
    )

    argv, _display = _build_oracle_browser_argv(
        prompt_text="Review the attached oracle packet.",
        oracle_bundle_path=bundle_path,
        preview_mode="",
        reply_output_path=reply_path,
        timeout_seconds=3600.0,
        browser_model_strategy="current",
        browser_auto_reattach_delay="30s",
        browser_auto_reattach_interval="2m",
        browser_auto_reattach_timeout="2m",
    )

    assert "--prompt" in argv
    assert "--prompt-file" not in argv
    assert "--browser-model-strategy" not in argv
    assert "--browser-manual-login" in argv
    assert "--browser-auto-reattach-delay" in argv
    assert "--browser-auto-reattach-interval" in argv
    assert "--browser-auto-reattach-timeout" in argv
    assert "--write-output" in argv
    assert "--wait" in argv


def test_build_oracle_browser_argv_preserves_non_current_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "oracle.md"
    bundle_path.write_text("# Oracle\n", encoding="utf-8")
    supported_flags = {
        "--browser-model-strategy",
        "--browser-manual-login",
    }
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._oracle_cli_supports_flag",
        lambda flag: flag in supported_flags,
    )

    argv, _display = _build_oracle_browser_argv(
        prompt_text="Review the attached oracle packet.",
        oracle_bundle_path=bundle_path,
        preview_mode="",
        reply_output_path=None,
        timeout_seconds=3600.0,
        browser_model_strategy="ignore",
        browser_auto_reattach_delay="30s",
        browser_auto_reattach_interval="2m",
        browser_auto_reattach_timeout="2m",
    )

    assert "--browser-model-strategy" in argv
    strategy_index = argv.index("--browser-model-strategy") + 1
    assert argv[strategy_index] == "ignore"


def test_build_oracle_browser_argv_supports_dry_run_full_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle_path = tmp_path / "oracle.md"
    bundle_path.write_text("# Oracle\n", encoding="utf-8")
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._oracle_cli_supports_flag",
        lambda _flag: False,
    )

    argv, _display = _build_oracle_browser_argv(
        prompt_text="Review the attached oracle packet.",
        oracle_bundle_path=bundle_path,
        preview_mode="full",
        reply_output_path=None,
        timeout_seconds=60.0,
        browser_model_strategy="current",
        browser_auto_reattach_delay="30s",
        browser_auto_reattach_interval="2m",
        browser_auto_reattach_timeout="2m",
    )

    assert argv[:3] == ["oracle", "--engine", "browser"]
    assert "--dry-run" in argv
    assert "full" in argv
    assert "--files-report" not in argv
    assert "--engine" in argv


def test_pytest_guard_blocks_unmocked_oracle_cli_execution() -> None:
    with pytest.raises(
        AssertionError, match="pytest blocked an unmocked Oracle CLI execution"
    ):
        subprocess.run(["oracle", "--help"], check=False)


def test_oracle_cli_supports_hidden_browser_flags_from_debug_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handlers_admin._oracle_cli_help_text.cache_clear()
    monkeypatch.setattr(
        "autolab.cli.handlers_admin.shutil.which",
        lambda _name: "/usr/local/bin/oracle",
    )

    calls: list[tuple[str, ...]] = []

    def _fake_run(
        argv: list[str],
        *,
        text: bool,
        capture_output: bool,
        check: bool,
        timeout: int,
    ) -> SimpleNamespace:
        calls.append(tuple(argv))
        if argv == ["oracle", "--help"]:
            return SimpleNamespace(
                stdout="--timeout\n--write-output\n",
                stderr="",
                returncode=0,
            )
        if argv == ["oracle", "--debug-help"]:
            return SimpleNamespace(
                stdout="--browser-manual-login\n--browser-auto-reattach-delay\n",
                stderr="",
                returncode=0,
            )
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr("autolab.cli.handlers_admin.subprocess.run", _fake_run)
    try:
        assert (
            handlers_admin._oracle_cli_supports_flag("--browser-manual-login") is True
        )
        assert (
            handlers_admin._oracle_cli_supports_flag("--browser-auto-reattach-delay")
            is True
        )
        assert handlers_admin._oracle_cli_supports_flag("--timeout") is True
        assert calls == [("oracle", "--help"), ("oracle", "--debug-help")]
    finally:
        handlers_admin._oracle_cli_help_text.cache_clear()


def test_oracle_roundtrip_persists_epoch_on_export_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr(
        "autolab.cli.handlers_admin.oracle_stage_auto_allowed",
        lambda *_args, **_kwargs: (True, _oracle_policy()),
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._export_oracle_document",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("oracle export failed")),
    )

    result = _run_oracle_roundtrip_auto(
        state_path=state_path,
        repo_root=repo,
        trigger_reason="manual automation request",
    )
    oracle_state = json.loads(
        (repo / ".autolab" / "oracle_state.json").read_text(encoding="utf-8")
    )

    assert result["status"] == "preview_failed"
    assert result["attempted"] is True
    assert oracle_state["current_epoch"]
    assert oracle_state["auto"]["attempted"] is True
    assert oracle_state["auto"]["status"] == "preview_failed"


def test_oracle_roundtrip_respects_apply_on_success_false(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    oracle_output_path = (
        repo / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
    )
    oracle_output_path.parent.mkdir(parents=True, exist_ok=True)
    oracle_output_path.write_text("# Oracle\n", encoding="utf-8")
    reply_text = "\n".join(
        [
            "# Expert Review Response",
            "",
            "Keep iterating from the current family, but narrow the next benchmark comparison before trusting the plateau.",
        ]
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin.oracle_stage_auto_allowed",
        lambda *_args, **_kwargs: (
            True,
            _oracle_policy(apply_on_success=False),
        ),
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._export_oracle_document",
        lambda **_kwargs: (oracle_output_path, 1, "autolab oracle"),
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin.shutil.which",
        lambda _name: "/usr/local/bin/oracle",
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin.oracle_profile_ready",
        lambda: True,
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._oracle_cli_supports_flag",
        lambda flag: (
            flag
            in {
                "--browser-manual-login",
                "--timeout",
                "--wait",
                "--write-output",
            }
        ),
    )
    resets: list[str] = []

    monkeypatch.setattr(
        "autolab.cli.handlers_admin._reset_oracle_browser_session",
        lambda: resets.append("reset"),
    )

    def _fake_run_oracle_browser_cli(*, argv: list[str], **_kwargs):
        if "--dry-run" in argv:
            return (0, "preview ok", "", "oracle --dry-run")
        output_index = argv.index("--write-output") + 1
        Path(argv[output_index]).write_text(reply_text + "\n", encoding="utf-8")
        return (0, "", "", "oracle --engine browser")

    monkeypatch.setattr(
        "autolab.cli.handlers_admin._run_oracle_browser_cli",
        _fake_run_oracle_browser_cli,
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._apply_oracle_reply_text",
        lambda **_kwargs: pytest.fail("roundtrip should not auto-apply when disabled"),
    )
    monkeypatch.setattr(
        "autolab.config._load_oracle_apply_policy",
        lambda *_args, **_kwargs: _oracle_apply_policy(
            ingestion_mode="hybrid",
            llm_command="mock-ingest",
        ),
    )

    def _fake_run_oracle_apply_agent(_repo_root: Path, **_kwargs):
        payload = {
            "verdict": "continue_search",
            "suggested_next_action": "Run one more benchmark comparison.",
            "recommended_human_review": False,
            "summary": "LLM extracted a continue-search recommendation.",
            "discuss_updates": {},
            "research_questions": [],
            "todo_hints": [],
            "campaign_feedback": [],
            "plan_approval_note": "",
        }
        return (0, json.dumps(payload), "", "mock-ingest")

    monkeypatch.setattr(
        handlers_admin,
        "_run_oracle_apply_agent",
        _fake_run_oracle_apply_agent,
    )

    result = _run_oracle_roundtrip_auto(
        state_path=state_path,
        repo_root=repo,
        trigger_reason="manual automation request",
    )
    oracle_state = json.loads(
        (repo / ".autolab" / "oracle_state.json").read_text(encoding="utf-8")
    )

    assert result["status"] == "succeeded"
    assert result["apply_status"] == "not_applied"
    assert oracle_state["verdict"] == "continue_search"
    assert oracle_state["auto"]["apply_status"] == "not_applied"
    assert resets == ["reset", "reset"]
    assert not (
        repo / "experiments" / "plan" / "bootstrap_iteration" / "context"
    ).exists()


def test_oracle_roundtrip_refreshes_handoff_with_stable_epoch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    oracle_output_path = (
        repo / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
    )
    oracle_output_path.parent.mkdir(parents=True, exist_ok=True)
    oracle_output_path.write_text("# Oracle\n", encoding="utf-8")
    reply_text = "\n".join(
        [
            "ReviewerVerdict: continue_search",
            "",
            "## Rationale",
            "- Keep the search moving.",
            "",
            "## Recommended Actions",
            "1. Run the next iteration.",
            "",
            "## Risks",
            "- Could the current evidence still be incomplete?",
        ]
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin.oracle_stage_auto_allowed",
        lambda *_args, **_kwargs: (
            True,
            _oracle_policy(apply_on_success=False),
        ),
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._export_oracle_document",
        lambda **_kwargs: (oracle_output_path, 1, "internal-render"),
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin.shutil.which",
        lambda _name: "/usr/local/bin/oracle",
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin.oracle_profile_ready",
        lambda: True,
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._oracle_cli_supports_flag",
        lambda flag: (
            flag
            in {
                "--browser-manual-login",
                "--timeout",
                "--wait",
                "--write-output",
            }
        ),
    )
    resets: list[str] = []

    monkeypatch.setattr(
        "autolab.cli.handlers_admin._reset_oracle_browser_session",
        lambda: resets.append("reset"),
    )

    def _fake_run_oracle_browser_cli(*, argv: list[str], **_kwargs):
        if "--dry-run" in argv:
            return (0, "preview ok", "", "oracle --dry-run")
        output_index = argv.index("--write-output") + 1
        Path(argv[output_index]).write_text(reply_text + "\n", encoding="utf-8")
        return (0, "", "", "oracle --engine browser")

    monkeypatch.setattr(
        "autolab.cli.handlers_admin._run_oracle_browser_cli",
        _fake_run_oracle_browser_cli,
    )

    result = _run_oracle_roundtrip_auto(
        state_path=state_path,
        repo_root=repo,
        trigger_reason="manual automation request",
    )
    handoff_payload = json.loads(
        (repo / ".autolab" / "handoff.json").read_text(encoding="utf-8")
    )
    continuation = handoff_payload["continuation_packet"]
    oracle_state = json.loads(
        (repo / ".autolab" / "oracle_state.json").read_text(encoding="utf-8")
    )

    assert result["status"] == "succeeded"
    assert continuation["oracle_auto_status"] == "succeeded"
    assert continuation["oracle_verdict"] == "continue_search"
    assert continuation["oracle_epoch"] == oracle_state["current_epoch"]
    assert resets == ["reset", "reset"]


def test_oracle_roundtrip_requires_manual_login_flag_support(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    oracle_output_path = (
        repo / "experiments" / "plan" / "bootstrap_iteration" / "oracle.md"
    )
    oracle_output_path.parent.mkdir(parents=True, exist_ok=True)
    oracle_output_path.write_text("# Oracle\n", encoding="utf-8")
    monkeypatch.setattr(
        "autolab.cli.handlers_admin.oracle_stage_auto_allowed",
        lambda *_args, **_kwargs: (True, _oracle_policy()),
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._export_oracle_document",
        lambda **_kwargs: (oracle_output_path, 1, "internal-render"),
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin.shutil.which",
        lambda _name: "/usr/local/bin/oracle",
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin.oracle_profile_ready",
        lambda: True,
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._oracle_cli_supports_flag",
        lambda flag: flag in {"--timeout", "--wait", "--write-output"},
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_admin._run_oracle_browser_cli",
        lambda **_kwargs: pytest.fail(
            "browser CLI should not run without manual-login support"
        ),
    )

    result = _run_oracle_roundtrip_auto(
        state_path=state_path,
        repo_root=repo,
        trigger_reason="manual automation request",
    )

    assert result["status"] == "unavailable"
    assert "manual-login" in result["failure_reason"]


def test_auto_oracle_trigger_ignores_stale_guardrail_breach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    (repo / ".autolab").mkdir(parents=True)
    (repo / ".autolab" / "guardrail_breach.json").write_text(
        json.dumps(
            {
                "breached_at": "2026-03-08T00:00:00Z",
                "rule": "no_progress",
                "counters": {"no_progress_decisions": 2},
                "stage": "decide_repeat",
                "remediation": "Escalated to 'human_review'.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "autolab.cli.handlers_run._load_guardrail_config",
        lambda _repo_root: SimpleNamespace(on_breach="human_review"),
    )

    trigger_reason = _auto_oracle_trigger_reason(
        repo_root=repo,
        outcome=RunOutcome(
            exit_code=1,
            transitioned=True,
            stage_before="decide_repeat",
            stage_after="human_review",
            message="decision applied: decide_repeat -> human_review",
        ),
        assistant_mode=False,
        iteration_started_at="2026-03-08T00:10:00Z",
    )

    assert trigger_reason == ""
