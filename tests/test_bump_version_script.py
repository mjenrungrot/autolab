from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_bump_module():
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "scripts" / "bump_version.py"
    spec = importlib.util.spec_from_file_location("bump_version_script", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bump_version_updates_pyproject_and_readme(tmp_path: Path) -> None:
    module = _load_bump_module()

    pyproject_path = tmp_path / "pyproject.toml"
    readme_path = tmp_path / "README.md"

    pyproject_path.write_text(
        """[project]
name = "autolab"
version = "1.2.3"  # managed
""",
        encoding="utf-8",
    )
    readme_path.write_text(
        """# Example

python -m pip install git+https://github.com/mjenrungrot/autolab.git@v1.2.3
""",
        encoding="utf-8",
    )

    old_version, new_version, old_tag_version = module.bump_version(
        pyproject_path,
        readme_path,
        dry_run=False,
    )

    assert old_version == "1.2.3"
    assert new_version == "1.2.4"
    assert old_tag_version == "1.2.3"
    assert 'version = "1.2.4"' in pyproject_path.read_text(encoding="utf-8")
    assert "@v1.2.4" in readme_path.read_text(encoding="utf-8")


def test_bump_version_dry_run_does_not_modify_files(tmp_path: Path) -> None:
    module = _load_bump_module()

    pyproject_path = tmp_path / "pyproject.toml"
    readme_path = tmp_path / "README.md"

    original_pyproject = """[project]
name = "autolab"
version = "0.0.9"
"""
    original_readme = """python -m pip install git+https://github.com/mjenrungrot/autolab.git@v0.0.9
"""
    pyproject_path.write_text(original_pyproject, encoding="utf-8")
    readme_path.write_text(original_readme, encoding="utf-8")

    old_version, new_version, old_tag_version = module.bump_version(
        pyproject_path,
        readme_path,
        dry_run=True,
    )

    assert old_version == "0.0.9"
    assert new_version == "0.0.10"
    assert old_tag_version == "0.0.9"
    assert pyproject_path.read_text(encoding="utf-8") == original_pyproject
    assert readme_path.read_text(encoding="utf-8") == original_readme


def test_bump_version_uses_tomli_fallback_when_tomllib_missing(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_bump_module()
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        """[project]
name = "autolab"
version = "3.2.1"
""",
        encoding="utf-8",
    )

    class _TomliFallback:
        @staticmethod
        def loads(text: str) -> dict:
            assert "3.2.1" in text
            return {"project": {"version": "3.2.1"}}

    monkeypatch.setattr(module, "tomllib", None)
    monkeypatch.setattr(module, "tomli", _TomliFallback())

    assert module._load_project_version(pyproject_path) == "3.2.1"


def test_bump_version_errors_when_no_toml_parser_available(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_bump_module()
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        """[project]
name = "autolab"
version = "1.0.0"
""",
        encoding="utf-8",
    )

    monkeypatch.setattr(module, "tomllib", None)
    monkeypatch.setattr(module, "tomli", None)

    try:
        module._load_project_version(pyproject_path)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "tomllib/tomli is unavailable" in str(exc)
