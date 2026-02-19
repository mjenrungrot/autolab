from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _exec_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


def test_scaffold_verifiers_are_importable_via_spec() -> None:
    verifiers_dir = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "autolab"
        / "scaffold"
        / ".autolab"
        / "verifiers"
    )
    scripts = sorted(
        path
        for path in verifiers_dir.glob("*.py")
        if path.stem != "verifier_lib"
    )
    assert scripts, "expected scaffold verifier scripts"

    for idx, script in enumerate(scripts):
        module_name = f"autolab_scaffold_verifier_{idx}_{script.stem}"
        module = _exec_module(script, module_name)
        assert module is not None
