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
