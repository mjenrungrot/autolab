from __future__ import annotations

from pathlib import Path

import autolab.commands as commands_module


def _repo_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }


def test_report_generates_one_issue_document_only(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    log_path = repo / ".autolab" / "logs" / "orchestrator.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "2026-02-23T00:00:00Z run transition implementation -> review\n",
        encoding="utf-8",
    )

    def _fake_run_issue_report_agent(*_args, **_kwargs):
        return (
            0,
            "\n".join(
                [
                    "## Summary",
                    "The run failed during implementation review.",
                    "",
                    "## User Comment",
                    "Captured from command input.",
                    "",
                    "## Evidence",
                    "- transition log indicates review handoff failure.",
                    "",
                    "## Likely Root Cause",
                    "- review contract mismatch.",
                    "",
                    "## Recommendations",
                    "- harden review result validation and improve error messaging.",
                ]
            ),
            "",
            "fake-agent",
        )

    monkeypatch.setattr(
        commands_module, "_run_issue_report_agent", _fake_run_issue_report_agent
    )

    before_files = _repo_files(repo)
    exit_code = commands_module.main(
        [
            "report",
            "--state-file",
            str(state_path),
            "--comment",
            "Review keeps failing after run.",
        ]
    )
    after_files = _repo_files(repo)

    assert exit_code == 0
    created_files = sorted(after_files - before_files)
    assert len(created_files) == 1
    created = created_files[0]
    assert created.startswith(".autolab/logs/issue_report_")
    report_text = (repo / created).read_text(encoding="utf-8")
    assert "# Autolab Issue Report" in report_text
    assert "Review keeps failing after run." in report_text
    assert "## Summary" in report_text


def test_report_failure_still_writes_one_document(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    log_path = repo / ".autolab" / "logs" / "orchestrator.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "2026-02-23T00:05:00Z agent runner timeout stage=implementation\n",
        encoding="utf-8",
    )

    def _fake_run_issue_report_agent(*_args, **_kwargs):
        return (1, "", "simulated agent failure", "fake-agent")

    monkeypatch.setattr(
        commands_module, "_run_issue_report_agent", _fake_run_issue_report_agent
    )

    before_files = _repo_files(repo)
    exit_code = commands_module.main(
        [
            "report",
            "--state-file",
            str(state_path),
        ]
    )
    after_files = _repo_files(repo)

    assert exit_code == 1
    created_files = sorted(after_files - before_files)
    assert len(created_files) == 1
    created = created_files[0]
    assert created.startswith(".autolab/logs/issue_report_")
    report_text = (repo / created).read_text(encoding="utf-8")
    assert "Automated issue analysis could not complete." in report_text
    assert "simulated agent failure" in report_text


def test_report_supports_output_override(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    output_path = repo / "issue_submission.md"

    def _fake_run_issue_report_agent(*_args, **_kwargs):
        return (
            0,
            "\n".join(
                [
                    "## Summary",
                    "No obvious failure in logs.",
                    "",
                    "## User Comment",
                    "None.",
                    "",
                    "## Evidence",
                    "- insufficient runtime events.",
                    "",
                    "## Likely Root Cause",
                    "- missing log evidence.",
                    "",
                    "## Recommendations",
                    "- rerun with fuller logs and regenerate report.",
                ]
            ),
            "",
            "fake-agent",
        )

    monkeypatch.setattr(
        commands_module, "_run_issue_report_agent", _fake_run_issue_report_agent
    )

    before_files = _repo_files(repo)
    exit_code = commands_module.main(
        [
            "report",
            "--state-file",
            str(state_path),
            "--output",
            str(output_path),
        ]
    )
    after_files = _repo_files(repo)

    assert exit_code == 0
    assert output_path.exists()
    created_files = sorted(after_files - before_files)
    assert created_files == ["issue_submission.md"]


def test_report_campaign_writes_scope_root_morning_report(
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

    output_path = repo / "experiments" / "plan" / "iter_demo" / "morning_report.md"

    monkeypatch.setattr(
        commands_module,
        "_load_campaign",
        lambda _repo_root: {"campaign_id": "campaign_demo"},
    )
    monkeypatch.setattr(
        commands_module,
        "_refresh_campaign_results",
        lambda _repo_root, _campaign: {
            "results_tsv_path": repo
            / "experiments"
            / "plan"
            / "iter_demo"
            / "results.tsv",
            "results_md_path": repo
            / "experiments"
            / "plan"
            / "iter_demo"
            / "results.md",
            "rows": [],
            "baseline_run_id": "run_baseline",
        },
    )
    monkeypatch.setattr(
        commands_module,
        "_safe_refresh_handoff",
        lambda _state_path: (
            {
                "continuation_packet": {
                    "next_action": {
                        "recommended_command": "autolab campaign continue",
                        "reason": "campaign is resumable",
                        "safe_status": "ready",
                    }
                },
                "safe_resume_point": {
                    "command": "autolab campaign continue",
                    "status": "ready",
                    "preconditions": [],
                },
            },
            "",
        ),
    )
    monkeypatch.setattr(
        commands_module,
        "_campaign_build_morning_report_payload",
        lambda *_args, **_kwargs: {"recommended_command": "autolab campaign continue"},
    )
    monkeypatch.setattr(
        commands_module,
        "_campaign_render_morning_report",
        lambda *_args, **_kwargs: "# Campaign Morning Report\n",
    )
    monkeypatch.setattr(
        commands_module,
        "_campaign_morning_report_path",
        lambda _repo_root, _campaign: output_path,
    )

    before_files = _repo_files(repo)
    exit_code = commands_module.main(
        [
            "report",
            "--campaign",
            "--state-file",
            str(state_path),
        ]
    )
    after_files = _repo_files(repo)

    assert exit_code == 0
    assert output_path.exists()
    created_files = sorted(after_files - before_files)
    assert created_files == ["experiments/plan/iter_demo/morning_report.md"]
    assert output_path.read_text(encoding="utf-8") == "# Campaign Morning Report\n"


def test_report_campaign_requires_active_campaign(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )

    monkeypatch.setattr(commands_module, "_load_campaign", lambda _repo_root: None)

    exit_code = commands_module.main(
        [
            "report",
            "--campaign",
            "--state-file",
            str(state_path),
        ]
    )

    assert exit_code == 1


def test_report_campaign_rejects_issue_only_options(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )

    exit_code = commands_module.main(
        [
            "report",
            "--campaign",
            "--state-file",
            str(state_path),
            "--comment",
            "not supported here",
        ]
    )

    assert exit_code == 1
