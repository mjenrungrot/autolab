from __future__ import annotations

from pathlib import Path

import autolab.commands as commands_module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_lint_scaffold_sync_passes_when_trees_match(tmp_path: Path) -> None:
    scaffold_dir = tmp_path / "src" / "autolab" / "scaffold" / ".autolab"
    dotautolab_dir = tmp_path / "dotautolab" / ".autolab"

    _write(scaffold_dir / "prompts" / "stage_hypothesis.md", "# hypothesis\n")
    _write(scaffold_dir / "verifiers" / "template_fill.py", "print('ok')\n")
    _write(dotautolab_dir / "prompts" / "stage_hypothesis.md", "# hypothesis\n")
    _write(dotautolab_dir / "verifiers" / "template_fill.py", "print('ok')\n")

    exit_code = commands_module.main(
        [
            "lint-scaffold-sync",
            "--scaffold-dir",
            str(scaffold_dir),
            "--dotautolab-dir",
            str(dotautolab_dir),
        ]
    )

    assert exit_code == 0


def test_lint_scaffold_sync_fails_on_drift(tmp_path: Path) -> None:
    scaffold_dir = tmp_path / "src" / "autolab" / "scaffold" / ".autolab"
    dotautolab_dir = tmp_path / "dotautolab" / ".autolab"

    _write(scaffold_dir / "prompts" / "stage_hypothesis.md", "# hypothesis\n")
    _write(scaffold_dir / "schemas" / "design.schema.json", "{}\n")
    _write(dotautolab_dir / "prompts" / "stage_hypothesis.md", "# stale\n")
    _write(dotautolab_dir / "prompts" / "stage_legacy.md", "# legacy\n")

    exit_code = commands_module.main(
        [
            "lint-scaffold-sync",
            "--scaffold-dir",
            str(scaffold_dir),
            "--dotautolab-dir",
            str(dotautolab_dir),
        ]
    )

    assert exit_code == 1
