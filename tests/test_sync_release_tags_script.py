from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_sync_module():
    repo_root = Path(__file__).resolve().parents[1]
    module_path = repo_root / "scripts" / "sync_release_tags.py"
    spec = importlib.util.spec_from_file_location(
        "sync_release_tags_script", module_path
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sync_release_tags_uses_tomli_fallback_when_tomllib_missing(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_sync_module()
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(
        """[project]
name = "autolab"
version = "4.5.6"
""",
        encoding="utf-8",
    )

    class _TomliFallback:
        @staticmethod
        def loads(text: str) -> dict:
            assert "4.5.6" in text
            return {"project": {"version": "4.5.6"}}

    monkeypatch.setattr(module, "tomllib", None)
    monkeypatch.setattr(module, "tomli", _TomliFallback())

    assert module._current_project_version(pyproject_path) == "4.5.6"


def test_sync_release_tags_errors_when_no_toml_parser_available(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_sync_module()
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
        module._current_project_version(pyproject_path)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "tomllib/tomli is unavailable" in str(exc)
