from __future__ import annotations

import sys
from pathlib import Path

import autolab.commands as commands_module


def _load_toml(path: Path) -> dict:
    payload: dict
    if sys.version_info >= (3, 11):
        import tomllib

        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    else:  # pragma: no cover
        import tomli  # type: ignore

        payload = tomli.loads(path.read_text(encoding="utf-8"))
    return payload


def test_status_docs_generate_and_policy_doctor_smoke(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    assert commands_module.main(["status", "--state-file", str(state_path)]) == 0
    assert (
        commands_module.main(["docs", "generate", "--state-file", str(state_path)])
        == 0
    )
    assert (
        commands_module.main(
            ["policy", "doctor", "--state-file", str(state_path)]
        )
        == 0
    )


def test_package_data_contract_includes_registry_and_golden_fixtures() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = _load_toml(pyproject_path)

    package_data = (
        pyproject.get("tool", {})
        .get("setuptools", {})
        .get("package-data", {})
        .get("autolab", [])
    )
    assert isinstance(package_data, list)

    assert "scaffold/.autolab/workflow.yaml" in package_data
    assert "golden_iteration/README.md" in package_data
    assert "golden_iteration/experiments/plan/iter_golden/runs/*/*.json" in package_data


def test_packaged_golden_iteration_is_in_sync_with_examples() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    examples_root = repo_root / "examples" / "golden_iteration"
    packaged_root = repo_root / "src" / "autolab" / "golden_iteration"

    assert examples_root.is_dir()
    assert packaged_root.is_dir()

    example_files = sorted(
        path.relative_to(examples_root)
        for path in examples_root.rglob("*")
        if path.is_file()
    )
    packaged_files = sorted(
        path.relative_to(packaged_root)
        for path in packaged_root.rglob("*")
        if path.is_file()
    )

    assert packaged_files == example_files
    for relative in example_files:
        assert (packaged_root / relative).read_bytes() == (
            examples_root / relative
        ).read_bytes()
