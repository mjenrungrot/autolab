from __future__ import annotations

from pathlib import Path

import yaml


def test_release_workflow_validates_and_publishes_changelog_notes() -> None:
    workflow_path = (
        Path(__file__).resolve().parents[1] / ".github/workflows/release.yml"
    )
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert isinstance(workflow, dict)

    jobs = workflow.get("jobs")
    assert isinstance(jobs, dict)
    release_job = jobs.get("release")
    assert isinstance(release_job, dict)

    steps = release_job.get("steps")
    assert isinstance(steps, list)

    names = [step.get("name") for step in steps if isinstance(step, dict)]
    assert "Validate changelog range" in names
    assert "Build changelog release notes" in names

    release_step = next(
        (
            step
            for step in steps
            if isinstance(step, dict) and step.get("name") == "Create GitHub release"
        ),
        None,
    )
    assert isinstance(release_step, dict)
    with_payload = release_step.get("with")
    assert isinstance(with_payload, dict)
    assert with_payload.get("body_path") == "release_notes.md"
    assert "generate_release_notes" not in with_payload
