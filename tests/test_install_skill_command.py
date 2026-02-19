from __future__ import annotations

from pathlib import Path

import pytest

import autolab.commands as commands_module
from autolab.__main__ import _build_parser, _list_bundled_skills, main


def _assert_has_yaml_frontmatter(content: str, *, expected_name: str) -> None:
    lines = content.splitlines()
    assert lines, "skill file is empty"
    assert lines[0] == "---"

    end_index = next(
        (i for i, line in enumerate(lines[1:], start=1) if line == "---"), None
    )
    assert end_index is not None, "missing closing YAML frontmatter delimiter"

    frontmatter = "\n".join(lines[1:end_index])
    assert f"name: {expected_name}" in frontmatter


def test_install_skill_codex_creates_project_local_file(tmp_path: Path) -> None:
    exit_code = main(["install-skill", "codex", "--project-root", str(tmp_path)])
    assert exit_code == 0

    destination = tmp_path / ".codex" / "skills" / "autolab" / "SKILL.md"
    assert destination.exists()
    content = destination.read_text(encoding="utf-8")
    _assert_has_yaml_frontmatter(content, expected_name="autolab")
    assert "# /autolab - Autolab Workflow Operator" in content


def test_install_skill_codex_overwrites_existing_file(tmp_path: Path) -> None:
    destination = tmp_path / ".codex" / "skills" / "autolab" / "SKILL.md"
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text("SENTINEL", encoding="utf-8")

    exit_code = main(["install-skill", "codex", "--project-root", str(tmp_path)])
    assert exit_code == 0

    content = destination.read_text(encoding="utf-8")
    assert "SENTINEL" not in content
    _assert_has_yaml_frontmatter(content, expected_name="autolab")


def test_install_skill_is_listed_in_help() -> None:
    help_text = _build_parser().format_help()
    assert "install-skill" in help_text


def test_install_skill_rejects_unknown_provider() -> None:
    parser = _build_parser()
    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["install-skill", "unknown-provider"])
    assert int(exc_info.value.code) == 2


def test_install_skill_accepts_claude_provider() -> None:
    parser = _build_parser()
    args = parser.parse_args(["install-skill", "claude"])
    assert args.provider == "claude"


def test_install_skill_reports_missing_packaged_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise_missing(_provider: str, _skill: str) -> str:
        raise RuntimeError("bundled skill template is unavailable")

    monkeypatch.setattr(
        commands_module, "_load_packaged_skill_template_text", _raise_missing
    )
    exit_code = main(["install-skill", "codex", "--project-root", str(tmp_path)])
    assert exit_code == 1


def test_install_skill_codex_installs_all_skills(tmp_path: Path) -> None:
    exit_code = main(["install-skill", "codex", "--project-root", str(tmp_path)])
    assert exit_code == 0

    expected_skills = _list_bundled_skills("codex")
    assert len(expected_skills) == 4
    for skill_name in expected_skills:
        dest = tmp_path / ".codex" / "skills" / skill_name / "SKILL.md"
        assert dest.exists(), f"missing {skill_name}/SKILL.md"
        content = dest.read_text(encoding="utf-8")
        _assert_has_yaml_frontmatter(content, expected_name=skill_name)


def test_install_skill_claude_lists_bundled_skills() -> None:
    expected_skills = _list_bundled_skills("claude")
    assert expected_skills == ["autolab"]


def test_install_skill_codex_selective_install(tmp_path: Path) -> None:
    exit_code = main(
        [
            "install-skill",
            "codex",
            "--skill",
            "swarm-planner",
            "--project-root",
            str(tmp_path),
        ]
    )
    assert exit_code == 0

    dest = tmp_path / ".codex" / "skills" / "swarm-planner" / "SKILL.md"
    assert dest.exists()
    content = dest.read_text(encoding="utf-8")
    _assert_has_yaml_frontmatter(content, expected_name="swarm-planner")

    # Only the requested skill should be installed
    autolab_dest = tmp_path / ".codex" / "skills" / "autolab" / "SKILL.md"
    assert not autolab_dest.exists()


def test_install_skill_codex_selective_unknown_fails(tmp_path: Path) -> None:
    exit_code = main(
        [
            "install-skill",
            "codex",
            "--skill",
            "nonexistent-skill",
            "--project-root",
            str(tmp_path),
        ]
    )
    assert exit_code == 1


def test_install_skill_claude_creates_project_local_file(tmp_path: Path) -> None:
    exit_code = main(["install-skill", "claude", "--project-root", str(tmp_path)])
    assert exit_code == 0

    destination = tmp_path / ".claude" / "skills" / "autolab" / "SKILL.md"
    assert destination.exists()
    content = destination.read_text(encoding="utf-8")
    _assert_has_yaml_frontmatter(content, expected_name="autolab")
    assert "Workflow Operator (Claude)" in content


def test_install_skill_claude_selective_unknown_fails(tmp_path: Path) -> None:
    exit_code = main(
        [
            "install-skill",
            "claude",
            "--skill",
            "nonexistent-skill",
            "--project-root",
            str(tmp_path),
        ]
    )
    assert exit_code == 1
