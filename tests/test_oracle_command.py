from __future__ import annotations

import json
from pathlib import Path

import autolab.commands as commands_module
from autolab.campaign import _refresh_campaign_results


def _repo_files(root: Path) -> set[str]:
    return {
        path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()
    }


def _extract_appendix_blocks(prompt_text: str) -> str:
    marker = "Required appendix blocks (paste exactly):\n"
    remainder = prompt_text.split(marker, 1)[1]
    return remainder.rsplit("\n\nNow produce the oracle document.", 1)[0].strip()


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
