from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from autolab.extract_runtime import _execute_extract_runtime
from autolab.models import StageCheckError


def _write_policy(repo: Path, payload: dict) -> None:
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _base_state(
    *, iteration_id: str = "iter1", run_id: str = "run_001"
) -> dict[str, object]:
    return {
        "iteration_id": iteration_id,
        "experiment_id": "e1",
        "last_run_id": run_id,
        "run_group": [],
    }


def _write_design(iteration_dir: Path, *, extract_parser: dict[str, object]) -> None:
    design_payload = {
        "schema_version": "1.0",
        "id": "e1",
        "iteration_id": iteration_dir.name,
        "hypothesis_id": "h1",
        "entrypoint": {"module": "pkg.train", "args": {}},
        "compute": {"location": "local"},
        "metrics": {
            "primary": {"name": "acc", "unit": "%", "mode": "maximize"},
            "success_delta": "+0.1",
            "aggregation": "mean",
            "baseline_comparison": "vs baseline",
        },
        "baselines": [{"name": "baseline", "description": "baseline"}],
        "implementation_requirements": [
            {
                "requirement_id": "R1",
                "description": "req",
                "scope_kind": "experiment",
            }
        ],
        "extract_parser": extract_parser,
    }
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "design.yaml").write_text(
        yaml.safe_dump(design_payload, sort_keys=False),
        encoding="utf-8",
    )


def _write_python_hook(repo: Path, *, module_name: str, body: str) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    module_path = repo / f"{module_name}.py"
    module_path.write_text(body, encoding="utf-8")


def _write_executable_script(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def test_extract_runtime_skips_when_design_missing_and_parser_optional(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    (iteration_dir / "runs" / "run_001").mkdir(parents=True, exist_ok=True)
    (iteration_dir / "analysis").mkdir(parents=True, exist_ok=True)
    (iteration_dir / "runs" / "run_001" / "metrics.json").write_text(
        '{"schema_version":"1.0"}\n',
        encoding="utf-8",
    )
    (iteration_dir / "analysis" / "summary.md").write_text(
        "# Summary\nok\n",
        encoding="utf-8",
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {"require_hook": False},
                "summary": {"mode": "none"},
            }
        },
    )

    result = _execute_extract_runtime(repo, state=_base_state())
    assert result.run_id == "run_001"
    assert result.changed_files == ()


def test_extract_runtime_requires_design_when_parser_hook_is_required(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    (iteration_dir / "runs" / "run_001").mkdir(parents=True, exist_ok=True)
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {"require_hook": True},
                "summary": {"mode": "none"},
            }
        },
    )

    with pytest.raises(StageCheckError, match="design.yaml"):
        _execute_extract_runtime(repo, state=_base_state())


def test_extract_runtime_python_hook_writes_metrics_and_summary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    module_name = "extract_hook_runtime_test"
    _write_python_hook(
        repo,
        module_name=module_name,
        body=(
            "def parse_results(*, run_id, **kwargs):\n"
            "    return {\n"
            "        'metrics': {\n"
            "            'schema_version': '1.0',\n"
            "            'iteration_id': 'iter1',\n"
            "            'run_id': run_id,\n"
            "            'status': 'completed',\n"
            "            'primary_metric': {\n"
            "                'name': 'acc',\n"
            "                'value': 1.0,\n"
            "                'delta_vs_baseline': 0.1,\n"
            "            },\n"
            "        },\n"
            "        'summary_markdown': '# Summary\\nfrom-hook\\n',\n"
            "    }\n"
        ),
    )
    _write_design(
        iteration_dir,
        extract_parser={
            "kind": "python",
            "module": module_name,
            "callable": "parse_results",
        },
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {"require_hook": True},
                "summary": {"mode": "none"},
            }
        },
    )

    sys.path.insert(0, str(repo))
    try:
        result = _execute_extract_runtime(repo, state=_base_state())
    finally:
        sys.path.remove(str(repo))

    metrics_path = iteration_dir / "runs" / "run_001" / "metrics.json"
    summary_path = iteration_dir / "analysis" / "summary.md"
    assert metrics_path.exists()
    assert summary_path.exists()
    metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics["run_id"] == "run_001"
    assert "from-hook" in summary_path.read_text(encoding="utf-8")
    assert set(result.changed_files) == {metrics_path, summary_path}


def test_extract_runtime_multi_run_processes_run_group_replicates(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    module_name = "extract_hook_runtime_multi"
    _write_python_hook(
        repo,
        module_name=module_name,
        body=(
            "from pathlib import Path\n"
            "\n"
            "def parse_results(*, iteration_dir, run_id, **kwargs):\n"
            "    calls_path = Path(iteration_dir) / 'analysis' / 'calls.log'\n"
            "    calls_path.parent.mkdir(parents=True, exist_ok=True)\n"
            "    with calls_path.open('a', encoding='utf-8') as handle:\n"
            "        handle.write(run_id + '\\n')\n"
            "    return {\n"
            "        'metrics': {\n"
            "            'schema_version': '1.0',\n"
            "            'iteration_id': 'iter1',\n"
            "            'run_id': run_id,\n"
            "            'status': 'completed',\n"
            "            'primary_metric': {\n"
            "                'name': 'acc',\n"
            "                'value': 1.0,\n"
            "                'delta_vs_baseline': 0.1,\n"
            "            },\n"
            "        },\n"
            "        'summary_markdown': f'# Summary\\\\nrun={run_id}\\\\n',\n"
            "    }\n"
        ),
    )
    _write_design(
        iteration_dir,
        extract_parser={
            "kind": "python",
            "module": module_name,
            "callable": "parse_results",
        },
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {"require_hook": True},
                "summary": {"mode": "none"},
            }
        },
    )

    state = _base_state(run_id="run_base")
    state["run_group"] = ["run_base_r1", "run_base_r2", "run_base_r1", "run_base"]
    sys.path.insert(0, str(repo))
    try:
        result = _execute_extract_runtime(repo, state=state)
    finally:
        sys.path.remove(str(repo))

    assert result.run_id == "run_base"
    expected_targets = ("run_base", "run_base_r1", "run_base_r2")
    for target in expected_targets:
        metrics_path = iteration_dir / "runs" / target / "metrics.json"
        assert metrics_path.exists()
        payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        assert payload["run_id"] == target
    calls_log = (iteration_dir / "analysis" / "calls.log").read_text(encoding="utf-8")
    assert calls_log.splitlines() == list(expected_targets)
    summary_path = iteration_dir / "analysis" / "summary.md"
    assert summary_path.exists()
    assert "run=run_base_r2" in summary_path.read_text(encoding="utf-8")
    expected_changed = {
        iteration_dir / "runs" / "run_base" / "metrics.json",
        iteration_dir / "runs" / "run_base_r1" / "metrics.json",
        iteration_dir / "runs" / "run_base_r2" / "metrics.json",
        summary_path,
    }
    assert set(result.changed_files) == expected_changed


def test_extract_runtime_fails_when_summary_missing_in_mode_none(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    module_name = "extract_hook_runtime_metrics_only"
    _write_python_hook(
        repo,
        module_name=module_name,
        body=(
            "def parse_results(*, run_id, **kwargs):\n"
            "    return {\n"
            "        'metrics': {\n"
            "            'schema_version': '1.0',\n"
            "            'iteration_id': 'iter1',\n"
            "            'run_id': run_id,\n"
            "            'status': 'completed',\n"
            "            'primary_metric': {\n"
            "                'name': 'acc',\n"
            "                'value': 1.0,\n"
            "                'delta_vs_baseline': 0.1,\n"
            "            },\n"
            "        }\n"
            "    }\n"
        ),
    )
    _write_design(
        iteration_dir,
        extract_parser={
            "kind": "python",
            "module": module_name,
            "callable": "parse_results",
        },
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {"require_hook": True},
                "summary": {"mode": "none"},
            }
        },
    )

    sys.path.insert(0, str(repo))
    try:
        with pytest.raises(
            StageCheckError, match="did not produce analysis/summary.md"
        ):
            _execute_extract_runtime(repo, state=_base_state())
    finally:
        sys.path.remove(str(repo))


def test_extract_runtime_python_hook_blocks_external_import_side_effects(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    module_name = "extract_runtime_external_side_effect"
    marker_path = tmp_path / "side_effect_marker.txt"
    external_dir = tmp_path / "external_modules"
    external_dir.mkdir(parents=True, exist_ok=True)
    (external_dir / f"{module_name}.py").write_text(
        (
            "from pathlib import Path\n"
            f"Path({str(marker_path)!r}).write_text('imported', encoding='utf-8')\n"
            "def parse_results(*, run_id, **kwargs):\n"
            "    return {\n"
            "        'metrics': {'schema_version': '1.0', 'iteration_id': 'iter1', 'run_id': run_id},\n"
            "        'summary_markdown': '# Summary\\nexternal\\n',\n"
            "    }\n"
        ),
        encoding="utf-8",
    )
    _write_design(
        iteration_dir,
        extract_parser={
            "kind": "python",
            "module": module_name,
            "callable": "parse_results",
        },
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {
                    "require_hook": True,
                    "allow_external_python_modules": False,
                },
                "summary": {"mode": "none"},
            }
        },
    )

    assert not marker_path.exists()
    sys.path.insert(0, str(external_dir))
    try:
        with pytest.raises(StageCheckError, match="repository root"):
            _execute_extract_runtime(repo, state=_base_state())
    finally:
        sys.path.remove(str(external_dir))
        sys.modules.pop(module_name, None)
    assert not marker_path.exists()


def test_extract_runtime_blocks_command_allowlist_traversal_bypass(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    marker_path = iteration_dir / "analysis" / "blocked_parser_ran.txt"
    blocked_script = repo / "tools" / "blocked" / "parser_hook.py"
    _write_executable_script(
        blocked_script,
        (
            f"#!{sys.executable}\n"
            "import json\n"
            "from pathlib import Path\n"
            f"Path({str(marker_path)!r}).parent.mkdir(parents=True, exist_ok=True)\n"
            f"Path({str(marker_path)!r}).write_text('ran\\n', encoding='utf-8')\n"
            "print(json.dumps({\n"
            "    'metrics': {\n"
            "        'schema_version': '1.0',\n"
            "        'iteration_id': 'iter1',\n"
            "        'run_id': 'run_001',\n"
            "        'status': 'completed',\n"
            "        'primary_metric': {'name': 'acc', 'value': 1.0, 'delta_vs_baseline': 0.1},\n"
            "    },\n"
            "    'summary_markdown': '# Summary\\\\nblocked\\\\n',\n"
            "}))\n"
        ),
    )
    allowed_dir = repo / "tools" / "allowed"
    allowed_dir.mkdir(parents=True, exist_ok=True)
    traversal_command = str(allowed_dir / ".." / "blocked" / "parser_hook.py")
    _write_design(
        iteration_dir,
        extract_parser={
            "kind": "command",
            "command": traversal_command,
        },
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {
                    "require_hook": True,
                    "allow_command_hook": True,
                    "command_allowlist": [str(allowed_dir)],
                },
                "summary": {"mode": "none"},
            }
        },
    )

    with pytest.raises(StageCheckError, match="not allowed"):
        _execute_extract_runtime(repo, state=_base_state())
    assert not marker_path.exists()


def test_extract_runtime_command_hook_runs_with_realpath_allowlist(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    command_script = repo / "tools" / "parser_hook.py"
    _write_executable_script(
        command_script,
        (
            f"#!{sys.executable}\n"
            "import argparse\n"
            "import json\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('--run-id', required=True)\n"
            "args = parser.parse_args()\n"
            "print(json.dumps({\n"
            "    'metrics': {\n"
            "        'schema_version': '1.0',\n"
            "        'iteration_id': 'iter1',\n"
            "        'run_id': args.run_id,\n"
            "        'status': 'completed',\n"
            "        'primary_metric': {'name': 'acc', 'value': 1.0, 'delta_vs_baseline': 0.1},\n"
            "    },\n"
            "    'summary_markdown': '# Summary\\\\nfrom-command\\\\n',\n"
            "}))\n"
        ),
    )
    _write_design(
        iteration_dir,
        extract_parser={
            "kind": "command",
            "command": f"{command_script} --run-id {{run_id}}",
        },
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {
                    "require_hook": True,
                    "allow_command_hook": True,
                    "command_allowlist": [str(command_script)],
                },
                "summary": {"mode": "none"},
            }
        },
    )

    result = _execute_extract_runtime(repo, state=_base_state())

    metrics_path = iteration_dir / "runs" / "run_001" / "metrics.json"
    summary_path = iteration_dir / "analysis" / "summary.md"
    stdout_path = (
        iteration_dir / "runs" / "run_001" / "logs" / "extract.parser.stdout.log"
    )
    stderr_path = (
        iteration_dir / "runs" / "run_001" / "logs" / "extract.parser.stderr.log"
    )
    assert json.loads(metrics_path.read_text(encoding="utf-8"))["run_id"] == "run_001"
    assert "from-command" in summary_path.read_text(encoding="utf-8")
    assert set(result.changed_files) == {
        metrics_path,
        summary_path,
        stdout_path,
        stderr_path,
    }


def test_extract_runtime_command_hook_allows_basename_allowlist_entry(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    command_script = repo / "tools" / "basename_parser_hook.py"
    _write_executable_script(
        command_script,
        (
            f"#!{sys.executable}\n"
            "import json\n"
            "print(json.dumps({\n"
            "    'metrics': {\n"
            "        'schema_version': '1.0',\n"
            "        'iteration_id': 'iter1',\n"
            "        'run_id': 'run_001',\n"
            "        'status': 'completed',\n"
            "        'primary_metric': {'name': 'acc', 'value': 1.0, 'delta_vs_baseline': 0.1},\n"
            "    },\n"
            "    'summary_markdown': '# Summary\\\\nbasename-allowlist\\\\n',\n"
            "}))\n"
        ),
    )
    _write_design(
        iteration_dir,
        extract_parser={
            "kind": "command",
            "command": str(command_script),
        },
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {
                    "require_hook": True,
                    "allow_command_hook": True,
                    "command_allowlist": [command_script.name],
                },
                "summary": {"mode": "none"},
            }
        },
    )

    result = _execute_extract_runtime(repo, state=_base_state())
    metrics_path = iteration_dir / "runs" / "run_001" / "metrics.json"
    summary_path = iteration_dir / "analysis" / "summary.md"
    assert json.loads(metrics_path.read_text(encoding="utf-8"))["run_id"] == "run_001"
    assert "basename-allowlist" in summary_path.read_text(encoding="utf-8")
    assert metrics_path in result.changed_files
    assert summary_path in result.changed_files


def test_extract_runtime_command_hook_policy_gate_blocks_execution(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    marker_path = iteration_dir / "analysis" / "should_not_run.txt"
    command_script = repo / "tools" / "parser_hook.py"
    _write_executable_script(
        command_script,
        (
            f"#!{sys.executable}\n"
            "import json\n"
            "from pathlib import Path\n"
            f"Path({str(marker_path)!r}).parent.mkdir(parents=True, exist_ok=True)\n"
            f"Path({str(marker_path)!r}).write_text('ran\\n', encoding='utf-8')\n"
            "print(json.dumps({'metrics': {'schema_version': '1.0'}, 'summary_markdown': '# Summary\\nran\\n'}))\n"
        ),
    )
    _write_design(
        iteration_dir,
        extract_parser={
            "kind": "command",
            "command": str(command_script),
        },
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {
                    "require_hook": True,
                    "allow_command_hook": False,
                    "command_allowlist": [str(command_script)],
                },
                "summary": {"mode": "none"},
            }
        },
    )

    with pytest.raises(StageCheckError, match="disabled by policy"):
        _execute_extract_runtime(repo, state=_base_state())
    assert not marker_path.exists()


def test_extract_runtime_llm_on_demand_generates_missing_summary(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    module_name = "extract_hook_runtime_llm_success"
    _write_python_hook(
        repo,
        module_name=module_name,
        body=(
            "def parse_results(*, run_id, **kwargs):\n"
            "    return {\n"
            "        'metrics': {\n"
            "            'schema_version': '1.0',\n"
            "            'iteration_id': 'iter1',\n"
            "            'run_id': run_id,\n"
            "            'status': 'completed',\n"
            "            'primary_metric': {\n"
            "                'name': 'acc',\n"
            "                'value': 1.0,\n"
            "                'delta_vs_baseline': 0.1,\n"
            "            },\n"
            "        }\n"
            "    }\n"
        ),
    )
    _write_design(
        iteration_dir,
        extract_parser={
            "kind": "python",
            "module": module_name,
            "callable": "parse_results",
        },
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {"require_hook": True},
                "summary": {
                    "mode": "llm_on_demand",
                    "llm_command": "fake-summary-command",
                    "llm_timeout_seconds": 12,
                },
            }
        },
    )

    def _fake_run(*args, **kwargs):
        summary_path = iteration_dir / "analysis" / "summary.md"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text("# Summary\nllm-generated\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            args=["fake-summary-command"],
            returncode=0,
            stdout="ok\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    sys.path.insert(0, str(repo))
    try:
        result = _execute_extract_runtime(repo, state=_base_state())
    finally:
        sys.path.remove(str(repo))

    summary_path = iteration_dir / "analysis" / "summary.md"
    metrics_path = iteration_dir / "runs" / "run_001" / "metrics.json"
    stdout_path = (
        iteration_dir / "runs" / "run_001" / "logs" / "extract.summary_llm.stdout.log"
    )
    stderr_path = (
        iteration_dir / "runs" / "run_001" / "logs" / "extract.summary_llm.stderr.log"
    )
    assert summary_path.exists()
    assert "llm-generated" in summary_path.read_text(encoding="utf-8")
    assert set(result.changed_files) == {
        metrics_path,
        summary_path,
        stdout_path,
        stderr_path,
    }


def test_extract_runtime_llm_on_demand_fails_on_non_zero_exit(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    module_name = "extract_hook_runtime_llm_fail"
    _write_python_hook(
        repo,
        module_name=module_name,
        body=(
            "def parse_results(*, run_id, **kwargs):\n"
            "    return {\n"
            "        'metrics': {\n"
            "            'schema_version': '1.0',\n"
            "            'iteration_id': 'iter1',\n"
            "            'run_id': run_id,\n"
            "            'status': 'completed',\n"
            "            'primary_metric': {\n"
            "                'name': 'acc',\n"
            "                'value': 1.0,\n"
            "                'delta_vs_baseline': 0.1,\n"
            "            },\n"
            "        }\n"
            "    }\n"
        ),
    )
    _write_design(
        iteration_dir,
        extract_parser={
            "kind": "python",
            "module": module_name,
            "callable": "parse_results",
        },
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {"require_hook": True},
                "summary": {
                    "mode": "llm_on_demand",
                    "llm_command": "fake-summary-command",
                    "llm_timeout_seconds": 12,
                },
            }
        },
    )

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["fake-summary-command"],
            returncode=7,
            stdout="",
            stderr="boom\n",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    sys.path.insert(0, str(repo))
    try:
        with pytest.raises(StageCheckError, match="exit_code=7"):
            _execute_extract_runtime(repo, state=_base_state())
    finally:
        sys.path.remove(str(repo))


def test_extract_runtime_llm_on_demand_fails_on_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    module_name = "extract_hook_runtime_llm_timeout"
    _write_python_hook(
        repo,
        module_name=module_name,
        body=(
            "def parse_results(*, run_id, **kwargs):\n"
            "    return {\n"
            "        'metrics': {\n"
            "            'schema_version': '1.0',\n"
            "            'iteration_id': 'iter1',\n"
            "            'run_id': run_id,\n"
            "            'status': 'completed',\n"
            "            'primary_metric': {\n"
            "                'name': 'acc',\n"
            "                'value': 1.0,\n"
            "                'delta_vs_baseline': 0.1,\n"
            "            },\n"
            "        }\n"
            "    }\n"
        ),
    )
    _write_design(
        iteration_dir,
        extract_parser={
            "kind": "python",
            "module": module_name,
            "callable": "parse_results",
        },
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {"require_hook": True},
                "summary": {
                    "mode": "llm_on_demand",
                    "llm_command": "fake-summary-command",
                    "llm_timeout_seconds": 12,
                },
            }
        },
    )

    def _fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="fake-summary-command", timeout=12)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    sys.path.insert(0, str(repo))
    try:
        with pytest.raises(StageCheckError, match="timed out"):
            _execute_extract_runtime(repo, state=_base_state())
    finally:
        sys.path.remove(str(repo))


def test_extract_runtime_llm_on_demand_fails_when_summary_still_missing(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    module_name = "extract_hook_runtime_llm_empty"
    _write_python_hook(
        repo,
        module_name=module_name,
        body=(
            "def parse_results(*, run_id, **kwargs):\n"
            "    return {\n"
            "        'metrics': {\n"
            "            'schema_version': '1.0',\n"
            "            'iteration_id': 'iter1',\n"
            "            'run_id': run_id,\n"
            "            'status': 'completed',\n"
            "            'primary_metric': {\n"
            "                'name': 'acc',\n"
            "                'value': 1.0,\n"
            "                'delta_vs_baseline': 0.1,\n"
            "            },\n"
            "        }\n"
            "    }\n"
        ),
    )
    _write_design(
        iteration_dir,
        extract_parser={
            "kind": "python",
            "module": module_name,
            "callable": "parse_results",
        },
    )
    _write_policy(
        repo,
        {
            "extract_results": {
                "parser": {"require_hook": True},
                "summary": {
                    "mode": "llm_on_demand",
                    "llm_command": "fake-summary-command",
                    "llm_timeout_seconds": 12,
                },
            }
        },
    )

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["fake-summary-command"],
            returncode=0,
            stdout="generated\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    sys.path.insert(0, str(repo))
    try:
        with pytest.raises(
            StageCheckError,
            match="analysis/summary.md is still missing or empty",
        ):
            _execute_extract_runtime(repo, state=_base_state())
    finally:
        sys.path.remove(str(repo))
