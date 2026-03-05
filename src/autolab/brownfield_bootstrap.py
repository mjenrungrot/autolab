from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - optional dependency during import
    yaml = None  # type: ignore[assignment]

from autolab.constants import (
    BACKLOG_COMPLETED_STATUSES,
    DEFAULT_EXPERIMENT_TYPE,
    EXPERIMENT_TYPES,
)
from autolab.dataset_discovery import discover_media_inputs, summarize_root_counts
from autolab.utils import _manifest_timestamp

_SCAN_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".autolab",
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        ".idea",
        ".vscode",
    }
)

_DEFAULT_PYTEST_COMMAND = "{{python_bin}} -m pytest"


@dataclass(frozen=True)
class DiscoveredExperiment:
    iteration_id: str
    experiment_id: str
    hypothesis_id: str
    experiment_type: str
    iteration_path: str
    success_metric: str
    target_delta: float | str
    run_manifest_count: int
    latest_run_id: str
    latest_run_timestamp: str
    latest_run_status: str
    artifact_count: int
    available_artifacts: tuple[str, ...]


@dataclass(frozen=True)
class BrownfieldBootstrapResult:
    changed_files: tuple[Path, ...]
    focus_iteration_id: str
    focus_experiment_id: str
    backlog_action: str
    policy_seeded: bool
    project_map_path: Path
    experiment_delta_map_path: Path
    context_bundle_path: Path
    warnings: tuple[str, ...]


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_status(value: str) -> str:
    return _normalize_space(value).lower()


def _is_completed_status(value: str) -> bool:
    return _normalize_status(value) in BACKLOG_COMPLETED_STATUSES


def _read_json_object(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{path} must contain a JSON object")
    return loaded


def _load_yaml_mapping(path: Path) -> dict[str, Any]:
    if yaml is None:
        return {}
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise RuntimeError(f"{path} must contain a YAML mapping")
    return loaded


def _write_json_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    rendered = json.dumps(payload, indent=2, sort_keys=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == rendered:
                return False
        except Exception:
            pass
    path.write_text(rendered, encoding="utf-8")
    return True


def _write_text_if_changed(path: Path, text: str) -> bool:
    rendered = text.rstrip() + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == rendered:
                return False
        except Exception:
            pass
    path.write_text(rendered, encoding="utf-8")
    return True


def _write_yaml_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    if yaml is None:
        return False
    rendered = yaml.safe_dump(payload, sort_keys=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == rendered:
                return False
        except Exception:
            pass
    path.write_text(rendered, encoding="utf-8")
    return True


def _scan_repo(repo_root: Path, *, max_files: int = 12000) -> dict[str, Any]:
    files_scanned = 0
    extension_counts: dict[str, int] = {}
    key_files: list[str] = []
    manifests: list[str] = []

    manifest_names = {
        "pyproject.toml",
        "requirements.txt",
        "setup.py",
        "package.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "package-lock.json",
        "go.mod",
        "Cargo.toml",
        "pom.xml",
        "build.gradle",
        "Makefile",
        "Dockerfile",
        "docker-compose.yml",
        "docker-compose.yaml",
    }
    key_names = {
        "pytest.ini",
        ".pre-commit-config.yaml",
        ".prettierrc",
        ".eslintrc",
        ".eslintrc.json",
        ".eslintrc.js",
        ".ruff.toml",
        "mypy.ini",
        "tox.ini",
    }

    for raw_dir, dir_names, file_names in os.walk(repo_root):
        dir_names[:] = sorted(
            name for name in dir_names if name and name not in _SCAN_SKIP_DIR_NAMES
        )
        for file_name in sorted(file_names):
            file_path = Path(raw_dir) / file_name
            try:
                rel_path = file_path.relative_to(repo_root).as_posix()
            except Exception:
                continue
            files_scanned += 1
            suffix = file_path.suffix.lower()
            if suffix:
                extension_counts[suffix] = int(extension_counts.get(suffix, 0)) + 1
            else:
                extension_counts["<no_ext>"] = (
                    int(extension_counts.get("<no_ext>", 0)) + 1
                )
            if file_name in manifest_names:
                manifests.append(rel_path)
            if file_name in key_names:
                key_files.append(rel_path)
            if files_scanned >= max_files:
                break
        if files_scanned >= max_files:
            break

    top_level_dirs = sorted(
        entry.name
        for entry in repo_root.iterdir()
        if entry.is_dir()
        and entry.name not in _SCAN_SKIP_DIR_NAMES
        and not entry.name.startswith(".")
    )

    ci_workflows = sorted(
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / ".github" / "workflows").glob("*")
        if path.is_file()
    )

    languages: list[str] = []
    if any(name in manifests for name in ("pyproject.toml", "requirements.txt")) or (
        extension_counts.get(".py", 0) > 0
    ):
        languages.append("python")
    if "package.json" in manifests or extension_counts.get(".ts", 0) > 0:
        languages.append("typescript")
    if extension_counts.get(".js", 0) > 0 and "typescript" not in languages:
        languages.append("javascript")
    if "go.mod" in manifests or extension_counts.get(".go", 0) > 0:
        languages.append("go")
    if "Cargo.toml" in manifests or extension_counts.get(".rs", 0) > 0:
        languages.append("rust")
    if extension_counts.get(".java", 0) > 0 or "pom.xml" in manifests:
        languages.append("java")
    if extension_counts.get(".sh", 0) > 0:
        languages.append("shell")
    if extension_counts.get(".md", 0) > 0:
        languages.append("markdown")

    toolchains: list[str] = []
    if "pyproject.toml" in manifests:
        toolchains.append("python:pyproject")
    if "package.json" in manifests:
        toolchains.append("node:package-json")
    if "go.mod" in manifests:
        toolchains.append("go:modules")
    if "Cargo.toml" in manifests:
        toolchains.append("rust:cargo")
    if ci_workflows:
        toolchains.append("ci:github-actions")

    testing_frameworks: list[str] = []
    if (repo_root / "pytest.ini").exists() or extension_counts.get(".py", 0) > 0:
        tests_root = repo_root / "tests"
        if tests_root.exists():
            testing_frameworks.append("pytest")
    package_json_path = repo_root / "package.json"
    package_json_payload: dict[str, Any] = {}
    if package_json_path.exists():
        try:
            loaded = json.loads(package_json_path.read_text(encoding="utf-8"))
        except Exception:
            loaded = {}
        if isinstance(loaded, dict):
            package_json_payload = loaded
            scripts = loaded.get("scripts")
            if isinstance(scripts, dict):
                script_text = " ".join(str(value) for value in scripts.values()).lower()
                if "jest" in script_text:
                    testing_frameworks.append("jest")
                if "vitest" in script_text:
                    testing_frameworks.append("vitest")

    linters: list[str] = []
    if (repo_root / ".ruff.toml").exists() or "pyproject.toml" in manifests:
        linters.append("ruff")
    if (repo_root / ".eslintrc").exists() or (repo_root / ".eslintrc.json").exists():
        linters.append("eslint")
    if (repo_root / "mypy.ini").exists():
        linters.append("mypy")

    formatters: list[str] = []
    if (repo_root / ".prettierrc").exists():
        formatters.append("prettier")
    if "pyproject.toml" in manifests:
        pyproject_text = ""
        try:
            pyproject_text = (repo_root / "pyproject.toml").read_text(encoding="utf-8")
        except Exception:
            pyproject_text = ""
        lowered_pyproject = pyproject_text.lower()
        if "black" in lowered_pyproject:
            formatters.append("black")
        if "ruff" in lowered_pyproject and "format" in lowered_pyproject:
            formatters.append("ruff-format")

    package_managers: list[str] = []
    if (repo_root / "uv.lock").exists():
        package_managers.append("uv")
    if (repo_root / "poetry.lock").exists():
        package_managers.append("poetry")
    if (repo_root / "package-lock.json").exists():
        package_managers.append("npm")
    if (repo_root / "pnpm-lock.yaml").exists():
        package_managers.append("pnpm")
    if (repo_root / "yarn.lock").exists():
        package_managers.append("yarn")

    concerns: list[dict[str, str]] = []
    if not (repo_root / "tests").exists():
        concerns.append(
            {
                "id": "tests_missing",
                "severity": "medium",
                "summary": "No top-level tests/ directory detected.",
                "evidence": "tests/",
            }
        )
    if not ci_workflows:
        concerns.append(
            {
                "id": "ci_missing",
                "severity": "low",
                "summary": "No GitHub Actions workflow detected.",
                "evidence": ".github/workflows/",
            }
        )
    if not (repo_root / "docs").exists():
        concerns.append(
            {
                "id": "docs_sparse",
                "severity": "low",
                "summary": "No docs/ directory detected.",
                "evidence": "docs/",
            }
        )
    if extension_counts.get(".ipynb", 0) > 0 and not (repo_root / "notebooks").exists():
        concerns.append(
            {
                "id": "notebook_layout",
                "severity": "low",
                "summary": "Notebook files exist but no dedicated notebooks/ root was detected.",
                "evidence": ".ipynb files",
            }
        )

    return {
        "files_scanned": files_scanned,
        "extension_counts": dict(
            sorted(extension_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        "manifests": sorted(set(manifests)),
        "key_files": sorted(set(key_files)),
        "top_level_dirs": top_level_dirs,
        "ci_workflows": ci_workflows,
        "languages": sorted(set(languages)),
        "toolchains": sorted(set(toolchains)),
        "testing_frameworks": sorted(set(testing_frameworks)),
        "linters": sorted(set(linters)),
        "formatters": sorted(set(formatters)),
        "package_managers": sorted(set(package_managers)),
        "concerns": concerns,
        "package_json_payload": package_json_payload,
    }


def _extract_design_metric(design_payload: dict[str, Any]) -> tuple[str, float | str]:
    metrics = design_payload.get("metrics")
    if not isinstance(metrics, dict):
        return ("primary_metric", 0.0)
    primary = metrics.get("primary")
    name = "primary_metric"
    if isinstance(primary, dict):
        maybe_name = _normalize_space(str(primary.get("name", "")))
        if maybe_name:
            name = maybe_name

    raw_target = metrics.get("success_delta")
    if isinstance(raw_target, (int, float)):
        return (name, float(raw_target))
    target_text = _normalize_space(str(raw_target))
    if not target_text:
        return (name, 0.0)
    numeric_match = re.search(r"[-+]?\d+(?:\.\d+)?", target_text)
    if numeric_match:
        try:
            return (name, float(numeric_match.group(0)))
        except Exception:
            pass
    return (name, target_text)


def _discover_experiments(
    repo_root: Path,
    *,
    backlog_payload: dict[str, Any],
) -> list[DiscoveredExperiment]:
    experiments_root = repo_root / "experiments"
    if not experiments_root.exists():
        return []

    backlog_by_iteration: dict[str, dict[str, Any]] = {}
    raw_backlog_experiments = backlog_payload.get("experiments")
    if isinstance(raw_backlog_experiments, list):
        for entry in raw_backlog_experiments:
            if not isinstance(entry, dict):
                continue
            iteration_id = _normalize_space(str(entry.get("iteration_id", "")))
            if iteration_id and iteration_id not in backlog_by_iteration:
                backlog_by_iteration[iteration_id] = entry

    discovered: list[DiscoveredExperiment] = []
    for type_dir in sorted(experiments_root.iterdir()):
        if not type_dir.is_dir():
            continue
        experiment_type = _normalize_space(type_dir.name)
        if experiment_type not in EXPERIMENT_TYPES:
            continue
        for iteration_dir in sorted(type_dir.iterdir()):
            if not iteration_dir.is_dir():
                continue
            iteration_id = _normalize_space(iteration_dir.name)
            if not iteration_id:
                continue
            iteration_path = iteration_dir.relative_to(repo_root).as_posix()
            backlog_entry = backlog_by_iteration.get(iteration_id, {})
            if not isinstance(backlog_entry, dict):
                backlog_entry = {}

            design_payload = _load_yaml_mapping(iteration_dir / "design.yaml")
            design_experiment_id = _normalize_space(str(design_payload.get("id", "")))
            design_hypothesis_id = _normalize_space(
                str(design_payload.get("hypothesis_id", ""))
            )
            success_metric, target_delta = _extract_design_metric(design_payload)

            experiment_id = (
                design_experiment_id
                or _normalize_space(str(backlog_entry.get("id", "")))
                or f"e_{iteration_id}"
            )
            hypothesis_id = (
                design_hypothesis_id
                or _normalize_space(str(backlog_entry.get("hypothesis_id", "")))
                or f"h_{iteration_id}"
            )

            run_manifest_paths = sorted(iteration_dir.glob("runs/*/run_manifest.json"))
            latest_run_id = ""
            latest_run_timestamp = ""
            latest_run_status = ""
            latest_timestamp_value = datetime.min.replace(tzinfo=timezone.utc)
            for manifest_path in run_manifest_paths:
                payload = {}
                try:
                    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception:
                    loaded = {}
                if isinstance(loaded, dict):
                    payload = loaded
                run_id = _normalize_space(
                    str(payload.get("run_id", "")) or manifest_path.parent.name
                )
                timestamp = _manifest_timestamp(payload, run_id)
                timestamp_value = (
                    timestamp
                    if isinstance(timestamp, datetime)
                    else datetime.min.replace(tzinfo=timezone.utc)
                )
                if timestamp_value >= latest_timestamp_value:
                    latest_timestamp_value = timestamp_value
                    latest_run_id = run_id
                    latest_run_status = _normalize_space(str(payload.get("status", "")))
                    latest_run_timestamp = (
                        timestamp_value.isoformat(timespec="seconds").replace(
                            "+00:00", "Z"
                        )
                        if timestamp
                        else ""
                    )

            artifact_count = sum(
                1 for path in iteration_dir.rglob("*") if path.is_file()
            )
            available_artifacts: list[str] = []
            for candidate in (
                "hypothesis.md",
                "design.yaml",
                "implementation_plan.md",
                "implementation_review.md",
                "docs_update.md",
                "analysis/summary.md",
                "launch/run_local.sh",
                "launch/run_slurm.sbatch",
            ):
                if (iteration_dir / candidate).exists():
                    available_artifacts.append(candidate)
            if latest_run_id:
                metrics_rel = f"runs/{latest_run_id}/metrics.json"
                manifest_rel = f"runs/{latest_run_id}/run_manifest.json"
                if (iteration_dir / metrics_rel).exists():
                    available_artifacts.append(metrics_rel)
                if (iteration_dir / manifest_rel).exists():
                    available_artifacts.append(manifest_rel)

            discovered.append(
                DiscoveredExperiment(
                    iteration_id=iteration_id,
                    experiment_id=experiment_id,
                    hypothesis_id=hypothesis_id,
                    experiment_type=experiment_type,
                    iteration_path=iteration_path,
                    success_metric=success_metric,
                    target_delta=target_delta,
                    run_manifest_count=len(run_manifest_paths),
                    latest_run_id=latest_run_id,
                    latest_run_timestamp=latest_run_timestamp,
                    latest_run_status=latest_run_status,
                    artifact_count=artifact_count,
                    available_artifacts=tuple(sorted(set(available_artifacts))),
                )
            )
    return discovered


def _select_focus(
    state: dict[str, Any],
    *,
    backlog_payload: dict[str, Any],
    discovered_experiments: list[DiscoveredExperiment],
) -> tuple[str, str]:
    if not discovered_experiments:
        return (
            _normalize_space(str(state.get("iteration_id", ""))),
            _normalize_space(str(state.get("experiment_id", ""))),
        )

    discovered_by_iteration: dict[str, DiscoveredExperiment] = {
        item.iteration_id: item for item in discovered_experiments
    }

    state_iteration = _normalize_space(str(state.get("iteration_id", "")))
    state_experiment = _normalize_space(str(state.get("experiment_id", "")))
    state_is_bootstrap_placeholder = (
        state_iteration == "bootstrap_iteration" and len(discovered_experiments) > 1
    )
    if (
        state_iteration
        and state_iteration in discovered_by_iteration
        and not state_is_bootstrap_placeholder
    ):
        discovered = discovered_by_iteration[state_iteration]
        resolved_experiment_id = state_experiment
        if not resolved_experiment_id:
            raw_backlog_experiments = backlog_payload.get("experiments")
            if isinstance(raw_backlog_experiments, list):
                for entry in raw_backlog_experiments:
                    if not isinstance(entry, dict):
                        continue
                    entry_iteration = _normalize_space(
                        str(entry.get("iteration_id", ""))
                    )
                    if entry_iteration != state_iteration:
                        continue
                    resolved = _normalize_space(str(entry.get("id", "")))
                    if resolved:
                        resolved_experiment_id = resolved
                        break
        return (state_iteration, resolved_experiment_id or discovered.experiment_id)

    raw_backlog_experiments = backlog_payload.get("experiments")
    if isinstance(
        raw_backlog_experiments, list
    ) and not _is_bootstrap_placeholder_backlog(backlog_payload):
        for entry in raw_backlog_experiments:
            if not isinstance(entry, dict):
                continue
            status = _normalize_space(str(entry.get("status", "")))
            if _is_completed_status(status):
                continue
            iteration_id = _normalize_space(str(entry.get("iteration_id", "")))
            experiment_id = _normalize_space(str(entry.get("id", "")))
            if not iteration_id:
                continue
            discovered = discovered_by_iteration.get(iteration_id)
            if discovered is None:
                continue
            return (iteration_id, experiment_id or discovered.experiment_id)

    ranked = sorted(
        discovered_experiments,
        key=lambda item: (
            item.latest_run_timestamp,
            item.run_manifest_count,
            item.artifact_count,
            item.iteration_id,
        ),
        reverse=True,
    )
    selected = ranked[0]
    return (selected.iteration_id, selected.experiment_id)


def _infer_backlog_entries(
    discovered_experiments: list[DiscoveredExperiment],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    hypotheses: list[dict[str, Any]] = []
    experiments: list[dict[str, Any]] = []

    used_hypothesis_ids: set[str] = set()
    used_experiment_ids: set[str] = set()

    for index, item in enumerate(discovered_experiments, start=1):
        hypothesis_id = _normalize_space(item.hypothesis_id) or f"h{index}"
        while hypothesis_id in used_hypothesis_ids:
            hypothesis_id = f"{hypothesis_id}_{index}"
        used_hypothesis_ids.add(hypothesis_id)

        experiments_hypothesis_id = hypothesis_id
        if all(
            _normalize_space(str(entry.get("id", ""))) != hypothesis_id
            for entry in hypotheses
        ):
            hypotheses.append(
                {
                    "id": hypothesis_id,
                    "status": "open",
                    "title": f"Brownfield hypothesis for {item.iteration_id}",
                    "success_metric": item.success_metric or "primary_metric",
                    "target_delta": item.target_delta,
                }
            )

        experiment_id = _normalize_space(item.experiment_id) or f"e{index}"
        while experiment_id in used_experiment_ids:
            experiment_id = f"{experiment_id}_{index}"
        used_experiment_ids.add(experiment_id)

        experiments.append(
            {
                "id": experiment_id,
                "hypothesis_id": experiments_hypothesis_id,
                "status": "open",
                "type": item.experiment_type
                if item.experiment_type in EXPERIMENT_TYPES
                else DEFAULT_EXPERIMENT_TYPE,
                "iteration_id": item.iteration_id,
            }
        )

    return (hypotheses, experiments)


def _is_bootstrap_placeholder_backlog(backlog_payload: dict[str, Any]) -> bool:
    hypotheses = backlog_payload.get("hypotheses")
    experiments = backlog_payload.get("experiments")
    if not isinstance(hypotheses, list) or not isinstance(experiments, list):
        return False
    if len(hypotheses) != 1 or len(experiments) != 1:
        return False

    hypothesis = hypotheses[0]
    experiment = experiments[0]
    if not isinstance(hypothesis, dict) or not isinstance(experiment, dict):
        return False

    target_delta_raw = hypothesis.get("target_delta")
    target_delta_text = _normalize_space(str(target_delta_raw))
    if target_delta_text in {"0", "0.0", "+0", "+0.0"}:
        target_ok = True
    elif isinstance(target_delta_raw, (int, float)) and float(target_delta_raw) == 0.0:
        target_ok = True
    else:
        target_ok = False

    return (
        _normalize_space(str(hypothesis.get("id", ""))) == "h1"
        and _normalize_space(str(hypothesis.get("status", ""))).lower() == "open"
        and _normalize_space(str(hypothesis.get("title", ""))).lower()
        == "bootstrap hypothesis"
        and _normalize_space(str(hypothesis.get("success_metric", "")))
        == "primary_metric"
        and target_ok
        and _normalize_space(str(experiment.get("id", ""))) == "e1"
        and _normalize_space(str(experiment.get("hypothesis_id", ""))) == "h1"
        and _normalize_space(str(experiment.get("status", ""))).lower() == "open"
        and _normalize_space(str(experiment.get("type", ""))).lower() in {"", "plan"}
    )


def _apply_backlog_inference(
    backlog_payload: dict[str, Any],
    *,
    inferred_hypotheses: list[dict[str, Any]],
    inferred_experiments: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    if not inferred_hypotheses or not inferred_experiments:
        return (backlog_payload, "none")

    payload = dict(backlog_payload)
    payload_hypotheses = payload.get("hypotheses")
    payload_experiments = payload.get("experiments")
    if not isinstance(payload_hypotheses, list):
        payload_hypotheses = []
    if not isinstance(payload_experiments, list):
        payload_experiments = []

    if _is_bootstrap_placeholder_backlog(payload):
        payload["hypotheses"] = inferred_hypotheses
        payload["experiments"] = inferred_experiments
        return (payload, "replaced_bootstrap_placeholders")

    existing_hypothesis_ids = {
        _normalize_space(str(entry.get("id", "")))
        for entry in payload_hypotheses
        if isinstance(entry, dict)
    }
    existing_hypothesis_titles = {
        _normalize_space(str(entry.get("title", ""))).lower()
        for entry in payload_hypotheses
        if isinstance(entry, dict)
    }
    existing_experiment_ids = {
        _normalize_space(str(entry.get("id", "")))
        for entry in payload_experiments
        if isinstance(entry, dict)
    }
    existing_experiment_iterations = {
        _normalize_space(str(entry.get("iteration_id", "")))
        for entry in payload_experiments
        if isinstance(entry, dict)
    }

    appended_hypotheses = 0
    for entry in inferred_hypotheses:
        hypothesis_id = _normalize_space(str(entry.get("id", "")))
        title = _normalize_space(str(entry.get("title", ""))).lower()
        if not hypothesis_id or hypothesis_id in existing_hypothesis_ids:
            continue
        if title and title in existing_hypothesis_titles:
            continue
        payload_hypotheses.append(entry)
        existing_hypothesis_ids.add(hypothesis_id)
        if title:
            existing_hypothesis_titles.add(title)
        appended_hypotheses += 1

    appended_experiments = 0
    for entry in inferred_experiments:
        experiment_id = _normalize_space(str(entry.get("id", "")))
        iteration_id = _normalize_space(str(entry.get("iteration_id", "")))
        if not experiment_id or experiment_id in existing_experiment_ids:
            continue
        if iteration_id and iteration_id in existing_experiment_iterations:
            continue
        payload_experiments.append(entry)
        existing_experiment_ids.add(experiment_id)
        if iteration_id:
            existing_experiment_iterations.add(iteration_id)
        appended_experiments += 1

    payload["hypotheses"] = payload_hypotheses
    payload["experiments"] = payload_experiments
    if appended_hypotheses or appended_experiments:
        return (payload, "appended_inferred_entries")
    return (payload, "none")


def _infer_test_command(scan_payload: dict[str, Any]) -> str:
    languages = scan_payload.get("languages")
    if isinstance(languages, list) and "python" in languages:
        return _DEFAULT_PYTEST_COMMAND

    package_json_payload = scan_payload.get("package_json_payload")
    if isinstance(package_json_payload, dict):
        scripts = package_json_payload.get("scripts")
        if isinstance(scripts, dict):
            if "test" in scripts:
                return "npm test"
    return ""


def _seed_policy_from_existing(
    policy_path: Path,
    *,
    scan_payload: dict[str, Any],
    focus_iteration_id: str,
    focus_experiment_id: str,
) -> bool:
    policy = _load_yaml_mapping(policy_path)
    if not isinstance(policy, dict):
        return False

    bootstrap = policy.get("bootstrap")
    if not isinstance(bootstrap, dict):
        bootstrap = {}
    from_existing = bootstrap.get("from_existing")
    if not isinstance(from_existing, dict):
        from_existing = {}
    from_existing.update(
        {
            "generated_at": _utc_now(),
            "scan_mode": "fast_heuristic",
            "focus_iteration_id": focus_iteration_id,
            "focus_experiment_id": focus_experiment_id,
            "detected_languages": scan_payload.get("languages", []),
            "ci_workflows": scan_payload.get("ci_workflows", []),
        }
    )
    bootstrap["from_existing"] = from_existing
    policy["bootstrap"] = bootstrap

    inferred_test_command = _infer_test_command(scan_payload)
    current_test_command = _normalize_space(str(policy.get("test_command", "")))
    if (
        inferred_test_command
        and current_test_command in {"", _DEFAULT_PYTEST_COMMAND}
        and current_test_command != inferred_test_command
    ):
        policy["test_command"] = inferred_test_command

    return _write_yaml_if_changed(policy_path, policy)


def _project_summary(
    *,
    scan_payload: dict[str, Any],
    discovered_experiments: list[DiscoveredExperiment],
) -> str:
    languages = scan_payload.get("languages")
    if not isinstance(languages, list):
        languages = []
    concerns = scan_payload.get("concerns")
    concern_count = len(concerns) if isinstance(concerns, list) else 0
    language_text = (
        ", ".join(str(item) for item in languages) if languages else "unknown"
    )
    return (
        f"languages={language_text}; "
        f"experiments={len(discovered_experiments)}; "
        f"concerns={concern_count}"
    )


def _delta_summary(delta_payload: dict[str, Any]) -> str:
    iteration_id = _normalize_space(str(delta_payload.get("iteration_id", "")))
    experiment_type = _normalize_space(str(delta_payload.get("experiment_type", "")))
    adds = delta_payload.get("adds")
    latest = {}
    if isinstance(adds, dict):
        latest_raw = adds.get("latest_run")
        if isinstance(latest_raw, dict):
            latest = latest_raw
    latest_run_id = _normalize_space(str(latest.get("run_id", "")))
    latest_status = _normalize_space(str(latest.get("status", "")))
    if latest_run_id:
        run_text = f"{latest_run_id} ({latest_status or 'unknown'})"
    else:
        run_text = "none"
    return (
        f"iteration={iteration_id or 'unknown'}; "
        f"type={experiment_type or DEFAULT_EXPERIMENT_TYPE}; "
        f"latest_run={run_text}"
    )


def _render_project_map_markdown(payload: dict[str, Any]) -> str:
    stack = payload.get("stack")
    architecture = payload.get("architecture")
    conventions = payload.get("conventions")
    concerns = payload.get("concerns")
    evidence = payload.get("evidence")

    stack_lines = []
    if isinstance(stack, dict):
        stack_lines.append(
            f"- languages: {', '.join(stack.get('languages', [])) or 'unknown'}"
        )
        stack_lines.append(
            f"- toolchains: {', '.join(stack.get('toolchains', [])) or 'unknown'}"
        )
        stack_lines.append(
            f"- manifests: {', '.join(stack.get('manifests', [])) or 'none'}"
        )

    architecture_lines = []
    discovered_lines: list[str] = []
    if isinstance(architecture, dict):
        architecture_lines.append(
            f"- top_level_dirs: {', '.join(architecture.get('top_level_dirs', [])) or 'none'}"
        )
        architecture_lines.append(
            f"- ci_workflows: {', '.join(architecture.get('ci_workflows', [])) or 'none'}"
        )
        discovered = architecture.get("discovered_experiments")
        if isinstance(discovered, list):
            for entry in discovered[:20]:
                if not isinstance(entry, dict):
                    continue
                discovered_lines.append(
                    "- {path} (id={eid}, type={etype}, runs={runs}, latest={latest})".format(
                        path=_normalize_space(str(entry.get("iteration_path", ""))),
                        eid=_normalize_space(str(entry.get("experiment_id", ""))),
                        etype=_normalize_space(str(entry.get("experiment_type", ""))),
                        runs=int(entry.get("run_manifest_count", 0) or 0),
                        latest=_normalize_space(str(entry.get("latest_run_id", "")))
                        or "none",
                    )
                )

    conventions_lines = []
    if isinstance(conventions, dict):
        conventions_lines.append(
            "- testing_frameworks: "
            + (", ".join(conventions.get("testing_frameworks", [])) or "none")
        )
        conventions_lines.append(
            "- linters: " + (", ".join(conventions.get("linters", [])) or "none")
        )
        conventions_lines.append(
            "- formatters: " + (", ".join(conventions.get("formatters", [])) or "none")
        )
        conventions_lines.append(
            "- package_managers: "
            + (", ".join(conventions.get("package_managers", [])) or "none")
        )

    concern_lines = []
    if isinstance(concerns, list):
        for entry in concerns[:20]:
            if not isinstance(entry, dict):
                continue
            concern_lines.append(
                f"- [{entry.get('severity', 'unknown')}] {entry.get('id', 'unknown')}: {entry.get('summary', '')}"
            )

    evidence_lines = []
    if isinstance(evidence, dict):
        files_scanned = int(evidence.get("files_scanned", 0) or 0)
        evidence_lines.append(f"- files_scanned: {files_scanned}")
        media_counts = evidence.get("project_data_media_count_summary")
        if media_counts is not None:
            evidence_lines.append(f"- project_data_media_counts: {media_counts}")

    lines = [
        "# Project Codebase Map",
        "",
        f"- generated_at: {payload.get('generated_at', '')}",
        f"- scan_mode: {payload.get('scan_mode', '')}",
        f"- repo_root: {payload.get('repo_root', '')}",
        "",
        "## Stack",
        *stack_lines,
        "",
        "## Architecture",
        *architecture_lines,
        "",
        "## Discovered Experiments",
        *(discovered_lines or ["- none"]),
        "",
        "## Conventions",
        *conventions_lines,
        "",
        "## Concerns",
        *(concern_lines or ["- none"]),
        "",
        "## Evidence",
        *evidence_lines,
    ]
    return "\n".join(lines).rstrip() + "\n"


def _render_delta_markdown(payload: dict[str, Any]) -> str:
    adds = payload.get("adds")
    if not isinstance(adds, dict):
        adds = {}
    available_artifacts = adds.get("available_artifacts")
    if not isinstance(available_artifacts, list):
        available_artifacts = []
    assumptions = adds.get("assumptions")
    if not isinstance(assumptions, list):
        assumptions = []
    concerns = adds.get("concerns")
    if not isinstance(concerns, list):
        concerns = []
    latest_run = adds.get("latest_run")
    if not isinstance(latest_run, dict):
        latest_run = {}

    lines = [
        "# Experiment Delta Map",
        "",
        f"- generated_at: {payload.get('generated_at', '')}",
        f"- iteration_id: {payload.get('iteration_id', '')}",
        f"- experiment_id: {payload.get('experiment_id', '')}",
        f"- experiment_type: {payload.get('experiment_type', '')}",
        f"- iteration_path: {payload.get('iteration_path', '')}",
        f"- inherits_project_map: {payload.get('inherits_project_map', '')}",
        "",
        "## Latest Run",
        f"- run_id: {latest_run.get('run_id', '') or 'none'}",
        f"- status: {latest_run.get('status', '') or 'unknown'}",
        f"- timestamp: {latest_run.get('timestamp', '') or 'unknown'}",
        "",
        "## Available Artifacts",
        *([f"- {item}" for item in available_artifacts] or ["- none"]),
        "",
        "## Assumptions",
        *([f"- {item}" for item in assumptions] or ["- none"]),
        "",
        "## Concerns",
        *([f"- {item}" for item in concerns] or ["- none"]),
    ]
    return "\n".join(lines).rstrip() + "\n"


def _default_delta_payload(
    *,
    focus_iteration_id: str,
    focus_experiment_id: str,
) -> dict[str, Any]:
    iteration_path = f"experiments/{DEFAULT_EXPERIMENT_TYPE}/{focus_iteration_id}"
    return {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "iteration_id": focus_iteration_id,
        "experiment_id": focus_experiment_id,
        "inherits_project_map": ".autolab/context/project_map.json",
        "iteration_path": iteration_path,
        "experiment_type": DEFAULT_EXPERIMENT_TYPE,
        "adds": {
            "available_artifacts": [],
            "assumptions": [
                "Focus iteration was selected from limited brownfield evidence."
            ],
            "concerns": [
                "No discovered experiment directory matched the selected iteration."
            ],
            "latest_run": {"run_id": "", "status": "", "timestamp": ""},
        },
    }


def _selected_delta_payload(
    *,
    selected: DiscoveredExperiment | None,
    focus_iteration_id: str,
    focus_experiment_id: str,
) -> dict[str, Any]:
    if selected is None:
        return _default_delta_payload(
            focus_iteration_id=focus_iteration_id,
            focus_experiment_id=focus_experiment_id,
        )
    assumptions = [
        "Reuse project-wide map as canonical context; this delta records only experiment-specific additions.",
        "Keep experiment-specific implementation under the iteration workspace unless abstraction to src/ is clearly beneficial.",
    ]
    concerns: list[str] = []
    if not selected.latest_run_id:
        concerns.append("No run_manifest.json was detected for this iteration.")
    if selected.run_manifest_count > 0 and selected.latest_run_status not in {
        "completed",
        "synced",
        "running",
        "submitted",
        "pending",
    }:
        concerns.append(
            f"Latest run status is '{selected.latest_run_status or 'unknown'}'; verify launch/extract readiness."
        )
    if not concerns:
        concerns.append("No additional experiment-specific blocker was inferred.")

    return {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "iteration_id": selected.iteration_id,
        "experiment_id": selected.experiment_id,
        "inherits_project_map": ".autolab/context/project_map.json",
        "iteration_path": selected.iteration_path,
        "experiment_type": selected.experiment_type,
        "adds": {
            "available_artifacts": list(selected.available_artifacts),
            "assumptions": assumptions,
            "concerns": concerns,
            "latest_run": {
                "run_id": selected.latest_run_id,
                "status": selected.latest_run_status,
                "timestamp": selected.latest_run_timestamp,
            },
        },
    }


def run_brownfield_bootstrap(
    repo_root: Path,
    *,
    state_path: Path,
    backlog_path: Path,
    policy_path: Path,
) -> BrownfieldBootstrapResult:
    if not state_path.exists():
        raise RuntimeError(f"state file is missing at {state_path}")
    state = _read_json_object(state_path)
    backlog_payload = _load_yaml_mapping(backlog_path)

    changed_files: list[Path] = []
    warnings: list[str] = []

    scan_payload = _scan_repo(repo_root)
    discovered_experiments = _discover_experiments(
        repo_root,
        backlog_payload=backlog_payload,
    )
    if len(discovered_experiments) > 1 and any(
        item.iteration_id != "bootstrap_iteration" for item in discovered_experiments
    ):
        discovered_experiments = [
            item
            for item in discovered_experiments
            if item.iteration_id != "bootstrap_iteration"
        ]
    if not discovered_experiments:
        warnings.append(
            "brownfield scan did not find existing experiment directories under experiments/<type>/<iteration_id>"
        )

    focus_iteration_id, focus_experiment_id = _select_focus(
        state,
        backlog_payload=backlog_payload,
        discovered_experiments=discovered_experiments,
    )
    if not focus_iteration_id:
        focus_iteration_id = _normalize_space(str(state.get("iteration_id", "")))
    if not focus_iteration_id:
        focus_iteration_id = "bootstrap_iteration"
    if not focus_experiment_id:
        focus_experiment_id = _normalize_space(str(state.get("experiment_id", "")))
    if not focus_experiment_id:
        focus_experiment_id = "e1"

    state_changed = False
    if _normalize_space(str(state.get("iteration_id", ""))) != focus_iteration_id:
        state["iteration_id"] = focus_iteration_id
        state_changed = True
    if _normalize_space(str(state.get("experiment_id", ""))) != focus_experiment_id:
        state["experiment_id"] = focus_experiment_id
        state_changed = True
    if state_changed and _write_json_if_changed(state_path, state):
        changed_files.append(state_path)

    inferred_hypotheses, inferred_experiments = _infer_backlog_entries(
        discovered_experiments
    )
    next_backlog_payload, backlog_action = _apply_backlog_inference(
        backlog_payload,
        inferred_hypotheses=inferred_hypotheses,
        inferred_experiments=inferred_experiments,
    )
    if _write_yaml_if_changed(backlog_path, next_backlog_payload):
        changed_files.append(backlog_path)

    policy_seeded = _seed_policy_from_existing(
        policy_path,
        scan_payload=scan_payload,
        focus_iteration_id=focus_iteration_id,
        focus_experiment_id=focus_experiment_id,
    )
    if policy_seeded:
        changed_files.append(policy_path)

    selected_experiment = next(
        (
            item
            for item in discovered_experiments
            if item.iteration_id == focus_iteration_id
            and item.experiment_id == focus_experiment_id
        ),
        None,
    )
    if selected_experiment is None:
        selected_experiment = next(
            (
                item
                for item in discovered_experiments
                if item.iteration_id == focus_iteration_id
            ),
            None,
        )

    media_discovery = discover_media_inputs(repo_root)
    project_map_payload = {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "scan_mode": "fast_heuristic",
        "repo_root": str(repo_root),
        "stack": {
            "languages": scan_payload.get("languages", []),
            "manifests": scan_payload.get("manifests", []),
            "toolchains": scan_payload.get("toolchains", []),
        },
        "architecture": {
            "top_level_dirs": scan_payload.get("top_level_dirs", []),
            "ci_workflows": scan_payload.get("ci_workflows", []),
            "discovered_experiments": [
                {
                    "iteration_id": item.iteration_id,
                    "experiment_id": item.experiment_id,
                    "experiment_type": item.experiment_type,
                    "iteration_path": item.iteration_path,
                    "run_manifest_count": item.run_manifest_count,
                    "latest_run_id": item.latest_run_id,
                    "latest_run_timestamp": item.latest_run_timestamp,
                    "latest_run_status": item.latest_run_status,
                }
                for item in discovered_experiments
            ],
        },
        "conventions": {
            "testing_frameworks": scan_payload.get("testing_frameworks", []),
            "linters": scan_payload.get("linters", []),
            "formatters": scan_payload.get("formatters", []),
            "package_managers": scan_payload.get("package_managers", []),
        },
        "concerns": scan_payload.get("concerns", []),
        "evidence": {
            "files_scanned": int(scan_payload.get("files_scanned", 0) or 0),
            "extension_counts": scan_payload.get("extension_counts", {}),
            "key_files": scan_payload.get("key_files", []),
            "project_data_roots": [str(path) for path in media_discovery.project_roots],
            "project_data_media_counts": {
                str(path): int(count)
                for path, count in media_discovery.project_root_counts.items()
            },
            "project_data_media_count_summary": summarize_root_counts(
                media_discovery.project_root_counts
            ),
        },
    }
    project_map_summary = _project_summary(
        scan_payload=scan_payload,
        discovered_experiments=discovered_experiments,
    )
    context_root = repo_root / ".autolab" / "context"
    project_map_path = context_root / "project_map.json"
    project_map_md_path = context_root / "project_map.md"
    if _write_json_if_changed(project_map_path, project_map_payload):
        changed_files.append(project_map_path)
    if _write_text_if_changed(
        project_map_md_path, _render_project_map_markdown(project_map_payload)
    ):
        changed_files.append(project_map_md_path)

    delta_payload = _selected_delta_payload(
        selected=selected_experiment,
        focus_iteration_id=focus_iteration_id,
        focus_experiment_id=focus_experiment_id,
    )
    delta_summary = _delta_summary(delta_payload)
    delta_type = _normalize_space(str(delta_payload.get("experiment_type", "")))
    if delta_type not in EXPERIMENT_TYPES:
        delta_type = DEFAULT_EXPERIMENT_TYPE
    delta_iteration_id = _normalize_space(str(delta_payload.get("iteration_id", "")))
    if not delta_iteration_id:
        delta_iteration_id = focus_iteration_id
    delta_dir = repo_root / "experiments" / delta_type / delta_iteration_id
    delta_json_path = delta_dir / "context_delta.json"
    delta_md_path = delta_dir / "context_delta.md"
    if _write_json_if_changed(delta_json_path, delta_payload):
        changed_files.append(delta_json_path)
    if _write_text_if_changed(delta_md_path, _render_delta_markdown(delta_payload)):
        changed_files.append(delta_md_path)

    try:
        delta_json_rel = delta_json_path.relative_to(repo_root).as_posix()
    except ValueError:
        delta_json_rel = str(delta_json_path)
    bundle_payload = {
        "schema_version": "1.0",
        "generated_at": _utc_now(),
        "scan_mode": "fast_heuristic",
        "project_map_path": ".autolab/context/project_map.json",
        "project_map_summary": project_map_summary,
        "focus_iteration_id": focus_iteration_id,
        "focus_experiment_id": focus_experiment_id,
        "selected_experiment_delta_path": delta_json_rel,
        "selected_experiment_delta_summary": delta_summary,
        "experiment_delta_maps": [
            {
                "iteration_id": delta_iteration_id,
                "experiment_id": _normalize_space(
                    str(delta_payload.get("experiment_id", ""))
                ),
                "path": delta_json_rel,
                "summary": delta_summary,
            }
        ],
    }
    bundle_path = context_root / "bundle.json"
    if _write_json_if_changed(bundle_path, bundle_payload):
        changed_files.append(bundle_path)

    return BrownfieldBootstrapResult(
        changed_files=tuple(changed_files),
        focus_iteration_id=focus_iteration_id,
        focus_experiment_id=focus_experiment_id,
        backlog_action=backlog_action,
        policy_seeded=policy_seeded,
        project_map_path=project_map_path,
        experiment_delta_map_path=delta_json_path,
        context_bundle_path=bundle_path,
        warnings=tuple(warnings),
    )
