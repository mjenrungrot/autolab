from __future__ import annotations

import json
from pathlib import Path


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _setup_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()

    state_path = repo / ".autolab" / "state.json"
    _write_json(
        state_path,
        {
            "iteration_id": "iter-01",
            "experiment_id": "",
            "stage": "hypothesis",
        },
    )

    iteration_dir = repo / "experiments" / "plan" / "iter-01"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "hypothesis.md").write_text("hello\n", encoding="utf-8")
    return repo, state_path


def test_parser_import_supports_gc_command() -> None:
    from autolab.cli.parser import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["gc", "--json"])
    assert args.json is True
    assert hasattr(args, "handler")


def test_unpin_removes_gc_protection_for_labeled_checkpoint(tmp_path: Path) -> None:
    from autolab.checkpoint import create_checkpoint, set_checkpoint_pinned
    from autolab.gc import build_gc_plan

    repo, state_path = _setup_repo(tmp_path)

    checkpoint_id, _ = create_checkpoint(
        repo,
        state_path=state_path,
        stage="hypothesis",
        trigger="manual",
        label="keep-me",
        pinned=True,
        label_origin="user",
    )

    manifest = set_checkpoint_pinned(repo, checkpoint_id, pinned=False)
    assert manifest.get("pinned") is None
    assert manifest.get("gc_protected") is False

    plan = build_gc_plan(
        repo,
        state_path=state_path,
        categories=["checkpoints"],
        checkpoint_keep_latest=0,
    )

    candidate_ids = {
        str(action.get("checkpoint_id", ""))
        for action in plan.get("actions", [])
        if action.get("kind") == "checkpoints"
    }
    assert checkpoint_id in candidate_ids
