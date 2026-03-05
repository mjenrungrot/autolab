from __future__ import annotations

from pathlib import Path

import autolab.commands as commands_module
import autolab.state as state_module
from autolab.__main__ import _build_parser


def _canonical_scaffold_root() -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "src"
        / "autolab"
        / "scaffold"
        / ".autolab"
    )


def test_sync_scaffold_copies_from_canonical_scaffold(tmp_path: Path) -> None:
    destination = tmp_path / ".autolab"
    exit_code = commands_module.main(["sync-scaffold", "--dest", str(destination)])
    assert exit_code == 0

    source_root = _canonical_scaffold_root()
    for relative in (
        "prompts/stage_hypothesis.md",
        "schemas/state.schema.json",
        "schemas/parser_capabilities.schema.json",
        "verifiers/template_fill.py",
        "policy/local_dev.yaml",
        "parser_fixtures/command_basic/fixture.yaml",
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
        "prompts/stage_implementation_runner.md",
        "schemas/backlog.schema.json",
        "schemas/parser_capabilities_index.schema.json",
        "verifiers/schema_checks.py",
        "verifiers/registry_consistency.py",
        "verifiers/consistency_checks.py",
        "parser_fixtures/python_basic/fixture.yaml",
    ):
        source = source_root / relative
        target = repo / ".autolab" / relative
        assert target.exists(), f"missing initialized scaffold file {relative}"
        assert target.read_text(encoding="utf-8") == source.read_text(encoding="utf-8")

    assert not (repo / "dotautolab").exists()


def test_lint_scaffold_sync_command_is_removed() -> None:
    help_text = _build_parser().format_help()
    assert "lint-scaffold-sync" not in help_text


def test_sync_scaffold_skips_python_cache_artifacts(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "destination"

    expected_file = source_root / "parser_fixtures" / "example" / "fixture.yaml"
    expected_file.parent.mkdir(parents=True, exist_ok=True)
    expected_file.write_text("name: fixture\n", encoding="utf-8")

    parser_file = source_root / "parser_fixtures" / "example" / "parsers" / "parser.py"
    parser_file.parent.mkdir(parents=True, exist_ok=True)
    parser_file.write_text("def extract():\n    return {}\n", encoding="utf-8")

    cache_dir = parser_file.parent / "__pycache__"
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / "parser.cpython-312.pyc").write_bytes(b"bytecode")
    (cache_dir / "notes.txt").write_text("not bytecode, still ignored in cache dir")
    (parser_file.parent / "module.pyc").write_bytes(b"bytecode")

    copied, skipped = state_module._sync_scaffold_bundle(
        source_root,
        destination_root,
        overwrite=False,
    )

    assert copied == 2
    assert skipped == 0
    assert (destination_root / "parser_fixtures" / "example" / "fixture.yaml").exists()
    assert (
        destination_root / "parser_fixtures" / "example" / "parsers" / "parser.py"
    ).exists()
    assert not (
        destination_root / "parser_fixtures" / "example" / "parsers" / "module.pyc"
    ).exists()
    assert not (
        destination_root / "parser_fixtures" / "example" / "parsers" / "__pycache__"
    ).exists()
