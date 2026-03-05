"""Parser SDK command handlers (`autolab parser ...`)."""

from __future__ import annotations

from autolab.cli.support import *
from autolab.extract_runtime import _execute_extract_runtime
from autolab.parser_capabilities import (
    build_iteration_parser_capabilities_manifest,
    upsert_parser_capabilities_index,
    validate_parser_capability_alignment,
    write_iteration_parser_capabilities_manifest,
)


def _sanitize_module_stem(raw_value: str) -> str:
    candidate = str(raw_value).strip().replace("-", "_")
    if not candidate:
        return ""
    normalized_chars: list[str] = []
    for char in candidate:
        if char.isalnum() or char == "_":
            normalized_chars.append(char)
        else:
            normalized_chars.append("_")
    normalized = "".join(normalized_chars)
    if normalized and normalized[0].isdigit():
        normalized = f"p_{normalized}"
    return normalized.strip("_")


def _load_design_yaml(path: Path) -> dict[str, Any]:
    if _yaml_mod is None:
        raise StageCheckError("parser SDK requires PyYAML")
    if not path.exists():
        raise StageCheckError(f"design.yaml is missing at {path}")
    try:
        payload = _yaml_mod.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageCheckError(
            f"design.yaml is not valid YAML at {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise StageCheckError("design.yaml must contain a mapping")
    return payload


def _dump_yaml(path: Path, payload: dict[str, Any]) -> None:
    if _yaml_mod is None:
        raise StageCheckError("parser SDK requires PyYAML")
    path.write_text(
        _yaml_mod.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _extract_primary_metric_name(design_payload: dict[str, Any]) -> str:
    metrics = design_payload.get("metrics")
    if not isinstance(metrics, dict):
        return "primary_metric"
    primary = metrics.get("primary")
    if not isinstance(primary, dict):
        return "primary_metric"
    name = str(primary.get("name", "")).strip()
    return name or "primary_metric"


def _render_parser_module_template(*, primary_metric_name: str) -> str:
    return (
        "from __future__ import annotations\n"
        "\n"
        "import argparse\n"
        "import json\n"
        "from pathlib import Path\n"
        "from typing import Any\n"
        "\n"
        "try:\n"
        "    import yaml\n"
        "except Exception:  # pragma: no cover\n"
        "    yaml = None\n"
        "\n"
        "\n"
        "def parse_results(\n"
        "    *,\n"
        "    repo_root: str,\n"
        "    iteration_dir: str,\n"
        "    run_id: str,\n"
        "    state: dict[str, Any],\n"
        "    design: dict[str, Any],\n"
        ") -> dict[str, Any]:\n"
        "    metric_name = str(\n"
        "        ((design.get('metrics') or {}).get('primary') or {}).get('name', '')\n"
        "    ).strip() or '" + primary_metric_name + "'\n"
        "    iteration_id = str(design.get('iteration_id', '')).strip() or Path(iteration_dir).name\n"
        "\n"
        "    # TODO: Replace placeholder metric extraction with real parsing logic.\n"
        "    metric_value = 0.0\n"
        "    delta_vs_baseline = 0.0\n"
        "\n"
        "    return {\n"
        "        'metrics': {\n"
        "            'schema_version': '1.0',\n"
        "            'iteration_id': iteration_id,\n"
        "            'run_id': run_id,\n"
        "            'status': 'completed',\n"
        "            'primary_metric': {\n"
        "                'name': metric_name,\n"
        "                'value': metric_value,\n"
        "                'delta_vs_baseline': delta_vs_baseline,\n"
        "            },\n"
        "        },\n"
        "        'summary_markdown': (\n"
        "            '# Analysis Summary\\n\\n'\n"
        "            f'- parser: template\\n'\n"
        "            f'- run_id: {run_id}\\n'\n"
        "            f'- primary_metric: {metric_name}={metric_value}\\n'\n"
        "        ),\n"
        "    }\n"
        "\n"
        "\n"
        "def _load_primary_metric_from_design(iteration_path: Path) -> str:\n"
        "    if yaml is None:\n"
        "        return '" + primary_metric_name + "'\n"
        "    design_path = iteration_path / 'design.yaml'\n"
        "    if not design_path.exists():\n"
        "        return '" + primary_metric_name + "'\n"
        "    try:\n"
        "        payload = yaml.safe_load(design_path.read_text(encoding='utf-8'))\n"
        "    except Exception:\n"
        "        return '" + primary_metric_name + "'\n"
        "    if not isinstance(payload, dict):\n"
        "        return '" + primary_metric_name + "'\n"
        "    return str((((payload.get('metrics') or {}).get('primary') or {}).get('name', ''))).strip() or '"
        + primary_metric_name
        + "'\n"
        "\n"
        "\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    parser = argparse.ArgumentParser(description='Parser template command hook')\n"
        "    parser.add_argument('--run-id', required=True)\n"
        "    parser.add_argument('--iteration-path', required=True)\n"
        "    args = parser.parse_args(argv)\n"
        "\n"
        "    iteration_path = Path(args.iteration_path)\n"
        "    metric_name = _load_primary_metric_from_design(iteration_path)\n"
        "    payload = {\n"
        "        'metrics': {\n"
        "            'schema_version': '1.0',\n"
        "            'iteration_id': iteration_path.name,\n"
        "            'run_id': str(args.run_id).strip(),\n"
        "            'status': 'completed',\n"
        "            'primary_metric': {\n"
        "                'name': metric_name,\n"
        "                'value': 0.0,\n"
        "                'delta_vs_baseline': 0.0,\n"
        "            },\n"
        "        },\n"
        "        'summary_markdown': (\n"
        "            '# Analysis Summary\\n\\n'\n"
        "            f'- parser: template-command\\n'\n"
        "            f'- run_id: {args.run_id}\\n'\n"
        "            f'- primary_metric: {metric_name}=0.0\\n'\n"
        "        ),\n"
        "    }\n"
        "    print(json.dumps(payload))\n"
        "    return 0\n"
        "\n"
        "\n"
        "if __name__ == '__main__':\n"
        "    raise SystemExit(main())\n"
    )


def _copy_repo_for_isolated_parser_test(source_root: Path, target_root: Path) -> None:
    ignore = shutil.ignore_patterns(
        ".git",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
    )
    shutil.copytree(source_root, target_root, dirs_exist_ok=True, ignore=ignore)


def _build_parser_test_state(
    *,
    state: dict[str, Any],
    iteration_id: str,
    run_id: str,
) -> dict[str, Any]:
    run_group_raw = state.get("run_group", [])
    run_group: list[str] = []
    if isinstance(run_group_raw, list):
        for item in run_group_raw:
            candidate = str(item).strip()
            if candidate and candidate not in run_group:
                run_group.append(candidate)
    return {
        "iteration_id": iteration_id,
        "experiment_id": str(state.get("experiment_id", "")).strip(),
        "last_run_id": run_id,
        "run_group": run_group,
    }


def _validate_generated_outputs(
    *,
    repo_root: Path,
    iteration_dir: Path,
    run_id: str,
    expected_metrics_path: Path | None,
    expected_summary_path: Path | None,
) -> list[str]:
    issues: list[str] = []

    design_payload = _load_design_yaml(iteration_dir / "design.yaml")
    primary_metric = _extract_primary_metric_name(design_payload)

    metrics_path = iteration_dir / "runs" / run_id / "metrics.json"
    if not metrics_path.exists():
        issues.append(f"metrics artifact is missing at {metrics_path}")
        return issues
    try:
        metrics_payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    except Exception as exc:
        issues.append(f"metrics artifact is not valid JSON at {metrics_path}: {exc}")
        metrics_payload = {}

    if isinstance(metrics_payload, dict):
        primary = metrics_payload.get("primary_metric")
        metrics_name = ""
        if isinstance(primary, dict):
            metrics_name = str(primary.get("name", "")).strip()
        if primary_metric and metrics_name and metrics_name != primary_metric:
            issues.append(
                "metric-name mismatch: "
                f"metrics.primary_metric.name='{metrics_name}' "
                f"!= design.metrics.primary.name='{primary_metric}'"
            )

    summary_path = iteration_dir / "analysis" / "summary.md"
    if not summary_path.exists():
        issues.append(f"summary artifact is missing at {summary_path}")

    capability_issues = validate_parser_capability_alignment(
        repo_root=repo_root,
        iteration_dir=iteration_dir,
        iteration_id=iteration_dir.name,
        require_manifest=True,
        require_index=True,
    )
    issues.extend(capability_issues)

    if expected_metrics_path is not None:
        try:
            expected_metrics = json.loads(
                expected_metrics_path.read_text(encoding="utf-8")
            )
            actual_metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            if expected_metrics != actual_metrics:
                issues.append(
                    f"metrics mismatch with golden fixture {expected_metrics_path}"
                )
        except Exception as exc:
            issues.append(
                f"could not compare golden metrics fixture {expected_metrics_path}: {exc}"
            )

    if expected_summary_path is not None and summary_path.exists():
        try:
            expected_summary = expected_summary_path.read_text(encoding="utf-8").strip()
            actual_summary = summary_path.read_text(encoding="utf-8").strip()
            if expected_summary != actual_summary:
                issues.append(
                    f"summary mismatch with golden fixture {expected_summary_path}"
                )
        except Exception as exc:
            issues.append(
                f"could not compare golden summary fixture {expected_summary_path}: {exc}"
            )

    return issues


def _load_fixture_pack(
    *,
    repo_root: Path,
    pack_name: str,
    target_repo: Path,
) -> tuple[dict[str, Any], Path | None, Path | None]:
    fixtures_root = repo_root / ".autolab" / "parser_fixtures"
    pack_root = fixtures_root / str(pack_name).strip()
    if not pack_root.is_dir():
        raise StageCheckError(
            f"parser fixture pack '{pack_name}' is missing at {pack_root}"
        )

    fixture_manifest_path = pack_root / "fixture.yaml"
    if _yaml_mod is None:
        raise StageCheckError("parser fixture loading requires PyYAML")
    if not fixture_manifest_path.exists():
        raise StageCheckError(
            f"parser fixture manifest is missing at {fixture_manifest_path}"
        )
    try:
        fixture_manifest = _yaml_mod.safe_load(
            fixture_manifest_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise StageCheckError(
            f"parser fixture manifest is not valid YAML at {fixture_manifest_path}: {exc}"
        ) from exc
    if not isinstance(fixture_manifest, dict):
        raise StageCheckError("parser fixture manifest must contain a mapping")

    repo_snapshot = pack_root / "repo"
    if not repo_snapshot.is_dir():
        raise StageCheckError(
            f"parser fixture pack is missing repo snapshot at {repo_snapshot}"
        )

    shutil.copytree(repo_snapshot, target_repo, dirs_exist_ok=True)

    expected_metrics_path = pack_root / "expected" / "metrics.json"
    expected_summary_path = pack_root / "expected" / "summary.md"
    if not expected_metrics_path.exists():
        expected_metrics_path = None
    if not expected_summary_path.exists():
        expected_summary_path = None

    return fixture_manifest, expected_metrics_path, expected_summary_path


def _cmd_parser_init(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    force = bool(args.force)

    try:
        state = _normalize_state(_load_state(state_path))
    except Exception as exc:
        print(f"autolab parser init: ERROR {exc}", file=sys.stderr)
        return 1

    iteration_id = (
        str(getattr(args, "iteration_id", "") or "").strip()
        or str(state.get("iteration_id", "")).strip()
    )
    if not iteration_id:
        print(
            "autolab parser init: ERROR iteration_id is required (state.iteration_id is empty)",
            file=sys.stderr,
        )
        return 1

    try:
        iteration_dir, _iteration_type = _resolve_iteration_directory(
            repo_root,
            iteration_id=iteration_id,
            experiment_id=str(state.get("experiment_id", "")).strip(),
            require_exists=False,
        )
    except StageCheckError as exc:
        print(f"autolab parser init: ERROR {exc}", file=sys.stderr)
        return 1

    design_path = iteration_dir / "design.yaml"
    try:
        design_payload = _load_design_yaml(design_path)
    except StageCheckError as exc:
        print(f"autolab parser init: ERROR {exc}", file=sys.stderr)
        return 1

    default_module_stem = _sanitize_module_stem(f"{iteration_id}_extract_parser")
    module_stem = _sanitize_module_stem(
        str(getattr(args, "module", "") or "").strip() or default_module_stem
    )
    if not module_stem:
        print(
            "autolab parser init: ERROR parser module name resolved to an empty identifier",
            file=sys.stderr,
        )
        return 1

    parser_dir = repo_root / "parsers"
    parser_module_path = parser_dir / f"{module_stem}.py"
    parser_package_init = parser_dir / "__init__.py"

    if parser_module_path.exists() and not force:
        print(
            "autolab parser init: ERROR parser module already exists; "
            "rerun with --force to overwrite",
            file=sys.stderr,
        )
        return 1

    primary_metric = _extract_primary_metric_name(design_payload)
    parser_import_path = f"parsers.{module_stem}"
    command_template = f"python -m {parser_import_path} --run-id {{run_id}} --iteration-path {{iteration_path}}"

    try:
        parser_dir.mkdir(parents=True, exist_ok=True)
        if not parser_package_init.exists():
            parser_package_init.write_text("", encoding="utf-8")
        parser_module_path.write_text(
            _render_parser_module_template(primary_metric_name=primary_metric),
            encoding="utf-8",
        )

        design_payload["extract_parser"] = {
            "kind": "command",
            "command": command_template,
        }
        _dump_yaml(design_path, design_payload)

        manifest_payload = build_iteration_parser_capabilities_manifest(
            iteration_id=iteration_id,
            parser_kind="command",
            parser_locator=command_template,
            supported_metrics=[primary_metric],
        )
        manifest_path = write_iteration_parser_capabilities_manifest(
            iteration_dir=iteration_dir,
            payload=manifest_payload,
        )

        index_path = upsert_parser_capabilities_index(
            repo_root=repo_root,
            iteration_id=iteration_id,
            manifest_path=manifest_path,
            parser_kind="command",
            supported_metrics=[primary_metric],
        )
    except StageCheckError as exc:
        print(f"autolab parser init: ERROR {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"autolab parser init: ERROR {exc}", file=sys.stderr)
        return 1

    print("autolab parser init")
    print(f"iteration_id: {iteration_id}")
    print(f"parser_module: {parser_module_path}")
    print(f"design_path: {design_path}")
    print(f"capability_manifest: {manifest_path}")
    print(f"capability_index: {index_path}")
    return 0


def _cmd_parser_test(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    fixture_pack = str(getattr(args, "fixture_pack", "") or "").strip()
    in_place = bool(getattr(args, "in_place", False))
    output_json = bool(getattr(args, "json", False))

    result_payload: dict[str, Any] = {
        "command": "autolab parser test",
        "mode": "fixture" if fixture_pack else "iteration",
        "workspace_mode": "in_place" if in_place else "isolated",
        "passed": False,
        "issues": [],
    }

    expected_metrics_path: Path | None = None
    expected_summary_path: Path | None = None

    if fixture_pack:
        with tempfile.TemporaryDirectory(prefix="autolab_parser_fixture_test_") as tmp:
            target_repo = Path(tmp) / "repo"
            try:
                fixture_manifest, expected_metrics_path, expected_summary_path = (
                    _load_fixture_pack(
                        repo_root=repo_root,
                        pack_name=fixture_pack,
                        target_repo=target_repo,
                    )
                )
                fixture_state_path = target_repo / ".autolab" / "state.json"
                fixture_state = (
                    _normalize_state(_load_state(fixture_state_path))
                    if fixture_state_path.exists()
                    else {}
                )
                iteration_id = (
                    str(getattr(args, "iteration_id", "") or "").strip()
                    or str(fixture_manifest.get("iteration_id", "")).strip()
                    or str(fixture_state.get("iteration_id", "")).strip()
                )
                if not iteration_id:
                    raise StageCheckError(
                        "parser fixture pack did not provide iteration_id and --iteration-id is missing"
                    )
                run_id = (
                    str(getattr(args, "run_id", "") or "").strip()
                    or str(fixture_manifest.get("run_id", "")).strip()
                    or str(fixture_state.get("last_run_id", "")).strip()
                    or "parser_fixture_run"
                )
                state_payload = _build_parser_test_state(
                    state=fixture_state,
                    iteration_id=iteration_id,
                    run_id=run_id,
                )
                _execute_extract_runtime(target_repo, state=state_payload)
                iteration_dir, _ = _resolve_iteration_directory(
                    target_repo,
                    iteration_id=iteration_id,
                    experiment_id=str(state_payload.get("experiment_id", "")).strip(),
                    require_exists=False,
                )
                issues = _validate_generated_outputs(
                    repo_root=target_repo,
                    iteration_dir=iteration_dir,
                    run_id=run_id,
                    expected_metrics_path=expected_metrics_path,
                    expected_summary_path=expected_summary_path,
                )
            except Exception as exc:
                issues = [str(exc)]
                iteration_id = ""
                run_id = ""

            result_payload.update(
                {
                    "iteration_id": iteration_id,
                    "run_id": run_id,
                    "fixture_pack": fixture_pack,
                    "passed": not issues,
                    "issues": issues,
                }
            )
    else:
        try:
            state = _normalize_state(_load_state(state_path))
        except Exception as exc:
            if output_json:
                result_payload["issues"] = [str(exc)]
                print(json.dumps(result_payload, indent=2))
            else:
                print(f"autolab parser test: ERROR {exc}", file=sys.stderr)
            return 1

        iteration_id = (
            str(getattr(args, "iteration_id", "") or "").strip()
            or str(state.get("iteration_id", "")).strip()
        )
        if not iteration_id:
            issue = "iteration_id is required (state.iteration_id is empty)"
            if output_json:
                result_payload["issues"] = [issue]
                print(json.dumps(result_payload, indent=2))
            else:
                print(f"autolab parser test: ERROR {issue}", file=sys.stderr)
            return 1

        run_id = (
            str(getattr(args, "run_id", "") or "").strip()
            or str(state.get("last_run_id", "")).strip()
            or "parser_test_run"
        )

        state_payload = _build_parser_test_state(
            state=state,
            iteration_id=iteration_id,
            run_id=run_id,
        )

        if in_place:
            target_repo = repo_root
            try:
                _execute_extract_runtime(target_repo, state=state_payload)
                iteration_dir, _ = _resolve_iteration_directory(
                    target_repo,
                    iteration_id=iteration_id,
                    experiment_id=str(state_payload.get("experiment_id", "")).strip(),
                    require_exists=False,
                )
                issues = _validate_generated_outputs(
                    repo_root=target_repo,
                    iteration_dir=iteration_dir,
                    run_id=run_id,
                    expected_metrics_path=None,
                    expected_summary_path=None,
                )
            except Exception as exc:
                issues = [str(exc)]
        else:
            with tempfile.TemporaryDirectory(prefix="autolab_parser_test_") as tmp:
                target_repo = Path(tmp) / "repo"
                try:
                    _copy_repo_for_isolated_parser_test(repo_root, target_repo)
                    _execute_extract_runtime(target_repo, state=state_payload)
                    iteration_dir, _ = _resolve_iteration_directory(
                        target_repo,
                        iteration_id=iteration_id,
                        experiment_id=str(
                            state_payload.get("experiment_id", "")
                        ).strip(),
                        require_exists=False,
                    )
                    issues = _validate_generated_outputs(
                        repo_root=target_repo,
                        iteration_dir=iteration_dir,
                        run_id=run_id,
                        expected_metrics_path=None,
                        expected_summary_path=None,
                    )
                except Exception as exc:
                    issues = [str(exc)]

        result_payload.update(
            {
                "iteration_id": iteration_id,
                "run_id": run_id,
                "passed": not issues,
                "issues": issues,
            }
        )

    if output_json:
        print(json.dumps(result_payload, indent=2))
    else:
        status = "PASS" if result_payload.get("passed") else "FAIL"
        print("autolab parser test")
        print(f"mode: {result_payload.get('mode')}")
        print(f"workspace_mode: {result_payload.get('workspace_mode')}")
        if result_payload.get("fixture_pack"):
            print(f"fixture_pack: {result_payload['fixture_pack']}")
        print(f"iteration_id: {result_payload.get('iteration_id', '')}")
        print(f"run_id: {result_payload.get('run_id', '')}")
        print(f"status: {status}")
        for issue in result_payload.get("issues", []):
            print(f"issue: {issue}")

    return 0 if result_payload.get("passed") else 1


__all__ = [name for name in globals() if not name.startswith("__")]
