from __future__ import annotations

from pathlib import Path

import autolab.commands as commands_module
from autolab.__main__ import _build_parser


def _canonical_scaffold_root() -> Path:
    return Path(__file__).resolve().parents[1] / "src" / "autolab" / "scaffold" / ".autolab"


def test_sync_scaffold_copies_from_canonical_scaffold(tmp_path: Path) -> None:
    destination = tmp_path / ".autolab"
    exit_code = commands_module.main(["sync-scaffold", "--dest", str(destination)])
    assert exit_code == 0

    source_root = _canonical_scaffold_root()
    for relative in (
        "prompts/stage_hypothesis.md",
        "schemas/state.schema.json",
        "verifiers/template_fill.py",
        "policy/local_dev.yaml",
    ):
        source = source_root / relative
        target = destination / relative
        assert target.exists(), f"missing scaffold file {relative}"
        assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_init_bootstraps_from_canonical_scaffold(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    state_path = repo / ".autolab" / "state.json"

    exit_code = commands_module.main(["init", "--state-file", str(state_path)])
    assert exit_code == 0

    source_root = _canonical_scaffold_root()
    for relative in (
        "prompts/stage_implementation.md",
        "schemas/backlog.schema.json",
        "verifiers/schema_checks.py",
        "verifiers/registry_consistency.py",
        "verifiers/consistency_checks.py",
    ):
        source = source_root / relative
        target = repo / ".autolab" / relative
        assert target.exists(), f"missing initialized scaffold file {relative}"
        assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")

    assert not (repo / "dotautolab").exists()


def test_lint_scaffold_sync_command_is_removed() -> None:
    help_text = _build_parser().format_help()
    assert "lint-scaffold-sync" not in help_text
