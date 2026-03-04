from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace


def _load_changelog_module():
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "scripts" / "changelog.py"
    spec = importlib.util.spec_from_file_location("changelog_script", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_validate_current_section_passes_for_exact_one_hop_range() -> None:
    module = _load_changelog_module()
    text = """# Changelog

## [1.2.4] - 2026-03-03

### Summary
- Tightened release checks for changelog range enforcement.

<!-- autolab:range v1.2.3..v1.2.4 -->

## [1.2.3] - 2026-03-02
"""

    errors = module._validate_current_section(
        text=text,
        previous_version="1.2.3",
        current_version="1.2.4",
    )

    assert errors == []


def test_validate_current_section_requires_current_section_first() -> None:
    module = _load_changelog_module()
    text = """# Changelog

## [1.2.3] - 2026-03-02

### Summary
- Older release.

<!-- autolab:range v1.2.2..v1.2.3 -->

## [1.2.4] - 2026-03-03

### Summary
- New release.

<!-- autolab:range v1.2.3..v1.2.4 -->
"""

    errors = module._validate_current_section(
        text=text,
        previous_version="1.2.3",
        current_version="1.2.4",
    )

    assert any("must be the first release section" in item for item in errors)


def test_validate_current_section_rejects_mismatched_range_marker() -> None:
    module = _load_changelog_module()
    text = """# Changelog

## [1.2.4] - 2026-03-03

### Summary
- New release notes.

<!-- autolab:range v1.2.2..v1.2.4 -->
"""

    errors = module._validate_current_section(
        text=text,
        previous_version="1.2.3",
        current_version="1.2.4",
    )

    assert any("range marker must be v1.2.3..v1.2.4" in item for item in errors)


def test_validate_current_section_rejects_placeholder_summary() -> None:
    module = _load_changelog_module()
    text = """# Changelog

## [1.2.4] - 2026-03-03

### Summary
- TODO: write this later.

<!-- autolab:range v1.2.3..v1.2.4 -->
"""

    errors = module._validate_current_section(
        text=text,
        previous_version="1.2.3",
        current_version="1.2.4",
    )

    assert any("summary bullets are placeholders" in item for item in errors)


def test_insert_scaffold_section_prepends_new_release() -> None:
    module = _load_changelog_module()
    existing = """# Changelog

## [1.2.0] - 2026-03-01

### Summary
- Existing release notes.

<!-- autolab:range v1.1.9..v1.2.0 -->
"""

    updated = module._insert_scaffold_section(
        existing_text=existing,
        previous_version="1.2.0",
        current_version="1.2.1",
        date_value="2026-03-03",
    )

    sections = module._parse_sections(updated)
    assert sections[0].version == "1.2.1"
    assert "## [1.2.1] - 2026-03-03" in updated
    assert "<!-- autolab:range v1.2.0..v1.2.1 -->" in updated


def test_render_release_notes_contains_summary_and_commit_list(monkeypatch) -> None:
    module = _load_changelog_module()
    text = """# Changelog

## [1.2.4] - 2026-03-03

### Summary
- Added strict changelog gating.

<!-- autolab:range v1.2.3..v1.2.4 -->
"""
    section = module._extract_section_for_version(text, "1.2.4")

    monkeypatch.setattr(
        module,
        "_run_git",
        lambda args, check=True: SimpleNamespace(  # noqa: ARG005
            stdout="- a1b2c3 Add changelog validator (Bot)\n- d4e5f6 Wire release notes (Bot)\n"
        ),
    )

    notes = module._render_release_notes(
        section=section,
        previous_tag="v1.2.3",
        current_tag="v1.2.4",
    )

    assert "# Release v1.2.4" in notes
    assert "### Summary" in notes
    assert "- Added strict changelog gating." in notes
    assert "### Commits (v1.2.3..v1.2.4)" in notes
    assert "- a1b2c3 Add changelog validator (Bot)" in notes


def test_main_render_release_notes_writes_output_file(
    monkeypatch, tmp_path: Path
) -> None:
    module = _load_changelog_module()

    changelog_path = tmp_path / "CHANGELOG.md"
    output_path = tmp_path / "release_notes.md"
    changelog_path.write_text(
        """# Changelog

## [1.2.4] - 2026-03-03

### Summary
- Added strict changelog gating.

<!-- autolab:range v1.2.3..v1.2.4 -->
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        module,
        "_run_git",
        lambda args, check=True: SimpleNamespace(
            stdout="- 123abc commit title (Bot)\n"
        ),  # noqa: ARG005
    )

    exit_code = module.main(
        [
            "render-release-notes",
            "--version",
            "1.2.4",
            "--current-tag",
            "v1.2.4",
            "--previous-tag",
            "v1.2.3",
            "--changelog",
            str(changelog_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == 0
    assert output_path.exists()
    content = output_path.read_text(encoding="utf-8")
    assert "### Summary" in content
    assert "### Commits (v1.2.3..v1.2.4)" in content
