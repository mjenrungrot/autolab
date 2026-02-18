from __future__ import annotations

from pathlib import Path

import pytest

import autolab.commands as commands_module
from autolab.__main__ import _build_parser, main


def test_install_skill_codex_creates_project_local_file(tmp_path: Path) -> None:
    exit_code = main(["install-skill", "codex", "--project-root", str(tmp_path)])
    assert exit_code == 0

    destination = tmp_path / ".codex" / "skills" / "autolab" / "SKILL.md"
    assert destination.exists()
    content = destination.read_text(encoding="utf-8")
    assert "name: autolab" in content
    assert "# /autolab - Autolab Workflow Operator" in content


def test_install_skill_codex_overwrites_existing_file(tmp_path: Path) -> None:
    destination = tmp_path / ".codex" / "skills" / "autolab" / "SKILL.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("SENTINEL", encoding="utf-8")

    exit_code = main(["install-skill", "codex", "--project-root", str(tmp_path)])
    assert exit_code == 0

    content = destination.read_text(encoding="utf-8")
    assert "SENTINEL" not in content
    assert "name: autolab" in content


def test_install_skill_is_listed_in_help() -> None:
    help_text = _build_parser().format_help()
    assert "install-skill" in help_text


def test_install_skill_rejects_unknown_provider() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["install-skill", "claude"])
    assert int(exc_info.value.code) == 2


def test_install_skill_reports_missing_packaged_asset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise_missing(_provider: str) -> str:
        raise RuntimeError("bundled skill template is unavailable")

    monkeypatch.setattr(commands_module, "_load_packaged_skill_template_text", _raise_missing)
    exit_code = main(["install-skill", "codex", "--project-root", str(tmp_path)])
    assert exit_code == 1
