"""Policy explain/docs/report command handlers."""

from __future__ import annotations

import math

from autolab.cli.support import *
from autolab.scope import _resolve_project_wide_root, _resolve_scope_context
from autolab.wave_observability import build_wave_observability


def _cmd_explain(args: argparse.Namespace) -> int:
    stage_name = str(args.stage).strip()
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    output_json = getattr(args, "json", False)

    registry = load_registry(repo_root)
    if not registry:
        print(
            "autolab explain: ERROR could not load workflow.yaml registry",
            file=sys.stderr,
        )
        return 1

    spec = registry.get(stage_name)
    if spec is None:
        print(f"autolab explain: ERROR unknown stage '{stage_name}'", file=sys.stderr)
        print(f"available stages: {', '.join(sorted(registry.keys()))}")
        return 1

    policy = _load_verifier_policy(repo_root)

    from autolab.config import (
        _resolve_policy_python_bin,
        _resolve_stage_requirements,
        _resolve_stage_max_retries,
    )

    effective = _resolve_stage_requirements(
        policy,
        stage_name,
        registry_verifier_categories=spec.verifier_categories,
    )
    max_retries = _resolve_stage_max_retries(policy, stage_name)
    python_bin = _resolve_policy_python_bin(policy)

    # Resolve prompt file paths (runner/audit/brief/human)
    prompt_path = repo_root / ".autolab" / "prompts" / spec.prompt_file
    runner_prompt_path = prompt_path
    brief_prompt_path = prompt_path
    human_prompt_path = prompt_path
    try:
        runner_prompt_path = _resolve_stage_prompt_path(
            repo_root, stage_name, prompt_role="runner"
        )
    except StageCheckError:
        pass
    try:
        prompt_path = _resolve_stage_prompt_path(
            repo_root, stage_name, prompt_role="audit"
        )
    except StageCheckError:
        pass
    try:
        brief_prompt_path = _resolve_stage_prompt_path(
            repo_root, stage_name, prompt_role="brief"
        )
    except StageCheckError:
        pass
    try:
        human_prompt_path = _resolve_stage_prompt_path(
            repo_root, stage_name, prompt_role="human"
        )
    except StageCheckError:
        pass

    try:
        resolved_prompt_path = prompt_path.relative_to(repo_root).as_posix()
    except ValueError:
        resolved_prompt_path = str(prompt_path)
    try:
        resolved_runner_prompt_path = runner_prompt_path.relative_to(
            repo_root
        ).as_posix()
    except ValueError:
        resolved_runner_prompt_path = str(runner_prompt_path)
    try:
        resolved_brief_prompt_path = brief_prompt_path.relative_to(repo_root).as_posix()
    except ValueError:
        resolved_brief_prompt_path = str(brief_prompt_path)
    try:
        resolved_human_prompt_path = human_prompt_path.relative_to(repo_root).as_posix()
    except ValueError:
        resolved_human_prompt_path = str(human_prompt_path)

    # Determine which verifier scripts would run
    verifier_scripts: list[str] = []
    verifiers_dir = repo_root / ".autolab" / "verifiers"
    if effective.get("schema"):
        verifier_scripts.append(
            f"{python_bin} .autolab/verifiers/schema_checks.py --stage {stage_name} --json"
        )
    if effective.get("consistency"):
        if (verifiers_dir / "consistency_checks.py").exists():
            verifier_scripts.append(
                f"{python_bin} .autolab/verifiers/consistency_checks.py --stage {stage_name} --json"
            )
    if effective.get("env_smoke"):
        verifier_scripts.append(f"{python_bin} .autolab/verifiers/run_health.py --json")
        verifier_scripts.append(
            f"{python_bin} .autolab/verifiers/result_sanity.py --json"
        )
    if effective.get("docs_target_update") and stage_name in {
        "update_docs",
        "implementation_review",
    }:
        verifier_scripts.append(
            f"{python_bin} .autolab/verifiers/docs_targets.py --json"
        )
    if effective.get("prompt_lint"):
        verifier_scripts.append(
            f"{python_bin} .autolab/verifiers/prompt_lint.py --stage {stage_name} --json"
        )
    if stage_name == "implementation":
        implementation_contract_path = verifiers_dir / "implementation_plan_contract.py"
        if implementation_contract_path.exists():
            verifier_scripts.append(
                f"{python_bin} .autolab/verifiers/implementation_plan_contract.py --stage {stage_name} --json"
            )

    # Pattern-path notes on required_outputs
    output_notes: list[dict[str, Any]] = []
    for output in spec.required_outputs:
        note: dict[str, Any] = {"pattern": output}
        if "<RUN_ID>" in output:
            note["note"] = "<RUN_ID> is replaced at runtime with state.last_run_id"
        output_notes.append(note)
    for group in spec.required_outputs_any_of:
        output_notes.append(
            {
                "any_of": list(group),
                "note": "at least one of these outputs must exist",
            }
        )
    for conditions, outputs in spec.required_outputs_if:
        output_notes.append(
            {
                "if": {key: value for key, value in conditions},
                "outputs": list(outputs),
            }
        )

    if output_json:
        payload: dict[str, Any] = {
            "stage": stage_name,
            "audit_prompt_file": spec.prompt_file,
            "resolved_audit_prompt_path": resolved_prompt_path,
            "runner_prompt_file": spec.runner_prompt_file or None,
            "resolved_runner_prompt_path": resolved_runner_prompt_path,
            "brief_prompt_file": spec.brief_prompt_file or None,
            "resolved_brief_prompt_path": resolved_brief_prompt_path,
            "human_prompt_file": spec.human_prompt_file or None,
            "resolved_human_prompt_path": resolved_human_prompt_path,
            "required_tokens": sorted(spec.required_tokens),
            "optional_tokens": sorted(spec.optional_tokens),
            "required_outputs": output_notes,
            "next_stage": spec.next_stage or None,
            "decision_map": spec.decision_map or None,
            "effective_requirements": effective,
            "verifier_scripts": verifier_scripts,
            "retry_policy": {"max_retries": max_retries},
            "classifications": {
                "active": spec.is_active,
                "terminal": spec.is_terminal,
                "decision": spec.is_decision,
                "runner_eligible": spec.is_runner_eligible,
            },
        }
        print(json.dumps(payload, indent=2))
    else:
        print(f"autolab explain stage {stage_name}")
        print("")
        print(f"audit_prompt_file: {spec.prompt_file}")
        print(f"resolved_audit_prompt_path: {resolved_prompt_path}")
        print(f"runner_prompt_file: {spec.runner_prompt_file}")
        print(f"resolved_runner_prompt_path: {resolved_runner_prompt_path}")
        print(f"brief_prompt_file: {spec.brief_prompt_file}")
        print(f"resolved_brief_prompt_path: {resolved_brief_prompt_path}")
        print(f"human_prompt_file: {spec.human_prompt_file}")
        print(f"resolved_human_prompt_path: {resolved_human_prompt_path}")
        print(f"required_tokens: {', '.join(sorted(spec.required_tokens)) or '(none)'}")
        print(f"optional_tokens: {', '.join(sorted(spec.optional_tokens)) or '(none)'}")
        required_outputs_text = (
            ", ".join(spec.required_outputs) if spec.required_outputs else "(none)"
        )
        print(f"required_outputs: {required_outputs_text}")
        if spec.required_outputs_any_of:
            for index, group in enumerate(spec.required_outputs_any_of, start=1):
                print(
                    f"required_outputs_any_of[{index}]: {' | '.join(group)} (at least one)"
                )
        if spec.required_outputs_if:
            for index, (conditions, outputs) in enumerate(
                spec.required_outputs_if, start=1
            ):
                condition_text = ", ".join(
                    f"{key}={value}" for key, value in conditions
                )
                print(
                    f"required_outputs_if[{index}] when {condition_text}: {', '.join(outputs)}"
                )
        for note in output_notes:
            if "pattern" in note and "note" in note:
                print(f"  {note['pattern']}: {note['note']}")
        print(f"next_stage: {spec.next_stage or '(branching)'}")
        if spec.decision_map:
            print(f"decision_map: {spec.decision_map}")
        print("")

        print("effective verifier requirements:")
        for key in sorted(effective.keys()):
            eff_val = effective[key]
            reg_val = spec.verifier_categories.get(key, False)
            if eff_val and not reg_val:
                note_str = "(policy override)"
            elif reg_val and not eff_val:
                note_str = f"(registry: {reg_val}, policy: {eff_val}) # capable but not required"
            else:
                note_str = ""
            print(f"  {key}: {eff_val}{' ' + note_str if note_str else ''}")

        if verifier_scripts:
            print("")
            print("verifier scripts that would run:")
            for script in verifier_scripts:
                print(f"  {script}")

        print("")
        print(f"retry_policy: max_retries={max_retries}")
        print(
            f"classifications: active={spec.is_active}, terminal={spec.is_terminal}, decision={spec.is_decision}, runner_eligible={spec.is_runner_eligible}"
        )

    return 0


# ---------------------------------------------------------------------------
# Policy list/show commands
# ---------------------------------------------------------------------------


def _cmd_policy_list(args: argparse.Namespace) -> int:
    try:
        scaffold_source = _resolve_scaffold_source()
    except RuntimeError as exc:
        print(f"autolab policy list: ERROR {exc}", file=sys.stderr)
        return 1

    policy_dir = scaffold_source / "policy"
    if not policy_dir.exists():
        print("autolab policy list: no presets found")
        return 0

    print("autolab policy list")
    print("available presets:")
    for path in sorted(policy_dir.glob("*.yaml")):
        print(f"  {path.stem}")
    return 0


def _cmd_policy_show(args: argparse.Namespace) -> int:
    preset_name = str(args.preset).strip()
    try:
        scaffold_source = _resolve_scaffold_source()
    except RuntimeError as exc:
        print(f"autolab policy show: ERROR {exc}", file=sys.stderr)
        return 1

    preset_path = scaffold_source / "policy" / f"{preset_name}.yaml"
    if not preset_path.exists():
        print(
            f"autolab policy show: ERROR preset '{preset_name}' not found",
            file=sys.stderr,
        )
        return 1

    print(f"autolab policy show {preset_name}")
    print(f"file: {preset_path}")
    print("---")
    print(preset_path.read_text(encoding="utf-8").rstrip())
    return 0


# ---------------------------------------------------------------------------
# Policy doctor command
# ---------------------------------------------------------------------------


def _cmd_policy_doctor(args: argparse.Namespace) -> int:
    """Diagnose common policy misconfigurations."""
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)

    policy = _load_verifier_policy(repo_root)
    registry = load_registry(repo_root)
    if not registry:
        print(
            "autolab policy doctor: ERROR could not load workflow.yaml registry",
            file=sys.stderr,
        )
        return 1

    issues: list[str] = []
    warnings: list[str] = []

    # Check 1: dry_run_command is not the stub if any stage requires dry_run
    dry_run_command = str(policy.get("dry_run_command", "")).strip()
    requirements = policy.get("requirements_by_stage", {})
    if isinstance(requirements, dict):
        for stage_name, reqs in requirements.items():
            if isinstance(reqs, dict) and reqs.get("dry_run"):
                if (
                    "AUTOLAB DRY-RUN STUB" in dry_run_command
                    or "sys.exit(1)" in dry_run_command
                ):
                    issues.append(
                        f"stage '{stage_name}' requires dry_run but dry_run_command is the default stub. "
                        "Configure a project-specific dry_run_command or set dry_run: false."
                    )
                    break

    # Check 2: test_command is configured if any stage requires tests
    test_command = str(policy.get("test_command", "")).strip()
    if isinstance(requirements, dict):
        for stage_name, reqs in requirements.items():
            if isinstance(reqs, dict) and reqs.get("tests"):
                if not test_command:
                    issues.append(
                        f"stage '{stage_name}' requires tests but test_command is empty."
                    )
                    break

    # Check 3: All requirements_by_stage keys match stages in workflow.yaml
    if isinstance(requirements, dict):
        registry_stages = set(registry.keys())
        for stage_name in requirements:
            if stage_name not in registry_stages:
                issues.append(
                    f"requirements_by_stage references unknown stage '{stage_name}' "
                    f"(workflow.yaml stages: {', '.join(sorted(registry_stages))})"
                )

    # Check 4: retry_policy_by_stage covers all active stages
    retry_policy = policy.get("retry_policy_by_stage", {})
    if isinstance(retry_policy, dict):
        for stage_name, spec in registry.items():
            if spec.is_active and stage_name not in retry_policy:
                warnings.append(
                    f"retry_policy_by_stage missing active stage '{stage_name}'; "
                    "will fall back to state.max_stage_attempts"
                )

    # Check 5: agent_runner.stages are runner-eligible per registry
    agent_runner = policy.get("agent_runner", {})
    if isinstance(agent_runner, dict):
        runner_stages = agent_runner.get("stages", [])
        if isinstance(runner_stages, list):
            for stage_name in runner_stages:
                stage_name = str(stage_name).strip()
                if (
                    stage_name in registry
                    and not registry[stage_name].is_runner_eligible
                ):
                    issues.append(
                        f"agent_runner.stages includes '{stage_name}' which is not runner-eligible in workflow.yaml"
                    )

    print("autolab policy doctor")
    print("")
    if issues:
        print(f"issues found: {len(issues)}")
        for issue in issues:
            print(f"  ERROR: {issue}")
    if warnings:
        print(f"warnings: {len(warnings)}")
        for warning in warnings:
            print(f"  WARN: {warning}")
    if not issues and not warnings:
        print("no issues found")
    print("")
    return 1 if issues else 0


# ---------------------------------------------------------------------------
# Docs generate command
# ---------------------------------------------------------------------------


_DOCS_GENERATE_DEFAULT_VIEWS: tuple[str, ...] = (
    "project",
    "roadmap",
    "state",
    "requirements",
    "sidecar",
)
_DOCS_VIEW_MAX_READ_BYTES = 2 * 1024 * 1024


def _docs_relpath(repo_root: Path, path: Path | None) -> str:
    if path is None:
        return ""
    try:
        relative = path.relative_to(repo_root).as_posix()
        return relative or "."
    except ValueError:
        return str(path)


def _docs_markdown_escape(value: str) -> str:
    return str(value or "").replace("|", "\\|")


def _docs_append_error(existing: str, extra: str) -> str:
    existing_text = str(existing or "").strip()
    extra_text = str(extra or "").strip()
    if existing_text and extra_text:
        return f"{existing_text}; {extra_text}"
    return existing_text or extra_text


def _docs_safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _docs_safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _docs_non_empty_strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            output.append(text)
    return output


def _docs_format_seconds(value: Any, *, blank: str = "n/a") -> str:
    if value in ("", None):
        return blank
    numeric = _docs_safe_float(value, float("nan"))
    if math.isnan(numeric):
        return blank
    return f"{numeric:.3f}".rstrip("0").rstrip(".") + "s"


def _docs_merge_diagnostics(*groups: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            text = str(item).strip()
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
    return merged


def _docs_collect_execution_task_ids(
    *,
    plan_execution_state_payload: dict[str, Any] | None,
    plan_execution_summary_payload: dict[str, Any] | None,
) -> set[str]:
    task_ids: set[str] = set()
    if isinstance(plan_execution_summary_payload, dict):
        for row in plan_execution_summary_payload.get("task_details", []):
            if not isinstance(row, dict):
                continue
            task_id = str(row.get("task_id", "")).strip()
            if task_id:
                task_ids.add(task_id)
        for row in plan_execution_summary_payload.get("wave_details", []):
            if not isinstance(row, dict):
                continue
            task_ids.update(_docs_non_empty_strings(row.get("tasks")))
        critical_path = plan_execution_summary_payload.get("critical_path")
        if isinstance(critical_path, dict):
            task_ids.update(_docs_non_empty_strings(critical_path.get("task_ids")))
    if isinstance(plan_execution_state_payload, dict):
        task_status = plan_execution_state_payload.get("task_status")
        if isinstance(task_status, dict):
            for raw_task_id in task_status.keys():
                task_id = str(raw_task_id).strip()
                if task_id:
                    task_ids.add(task_id)
    return task_ids


def _docs_collect_plan_graph_task_ids(
    plan_graph_payload: dict[str, Any] | None,
) -> set[str]:
    task_ids: set[str] = set()
    if not isinstance(plan_graph_payload, dict):
        return task_ids
    for row in plan_graph_payload.get("nodes", []):
        if not isinstance(row, dict):
            continue
        task_id = str(row.get("task_id", "")).strip()
        if task_id:
            task_ids.add(task_id)
    for row in plan_graph_payload.get("waves", []):
        if not isinstance(row, dict):
            continue
        task_ids.update(_docs_non_empty_strings(row.get("tasks")))
    return task_ids


def _docs_validate_iteration_scoped_observability_payload(
    *,
    artifact_name: str,
    payload: dict[str, Any] | None,
    iteration_id: str,
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(payload, dict):
        return (payload, "")
    artifact_iteration_id = str(payload.get("iteration_id", "")).strip()
    if artifact_iteration_id and iteration_id and artifact_iteration_id != iteration_id:
        return (
            None,
            (
                f"stale {artifact_name}: iteration_id differs from requested "
                f"iteration_id ({artifact_iteration_id} != {iteration_id}); ignoring artifact"
            ),
        )
    return (payload, "")


def _docs_compare_observability_execution_payloads(
    *,
    plan_execution_state_payload: dict[str, Any] | None,
    plan_execution_summary_payload: dict[str, Any] | None,
) -> list[str]:
    if not isinstance(plan_execution_state_payload, dict) or not isinstance(
        plan_execution_summary_payload, dict
    ):
        return []

    diagnostics: list[str] = []
    for field in ("contract_hash", "run_unit"):
        state_value = str(plan_execution_state_payload.get(field, "")).strip()
        summary_value = str(plan_execution_summary_payload.get(field, "")).strip()
        if state_value and summary_value and state_value != summary_value:
            diagnostics.append(
                "plan_execution_state.json and plan_execution_summary.json "
                f"{field} differ ({state_value} != {summary_value})"
            )

    state_plan_file = str(plan_execution_state_payload.get("plan_file", "")).strip()
    summary_plan_file = str(plan_execution_summary_payload.get("plan_file", "")).strip()
    if state_plan_file and summary_plan_file and state_plan_file != summary_plan_file:
        diagnostics.append(
            "plan_execution_state.json and plan_execution_summary.json plan_file differ "
            f"({state_plan_file} != {summary_plan_file})"
        )
    return diagnostics


def _docs_sanitize_plan_graph_payload(
    *,
    plan_graph_payload: dict[str, Any] | None,
    execution_task_ids: set[str],
) -> tuple[dict[str, Any] | None, bool, str]:
    if not isinstance(plan_graph_payload, dict):
        return (plan_graph_payload, False, "")
    graph_task_ids = _docs_collect_plan_graph_task_ids(plan_graph_payload)
    if not graph_task_ids or not execution_task_ids:
        return (plan_graph_payload, False, "")

    overlap = graph_task_ids & execution_task_ids
    if not overlap:
        return (
            None,
            True,
            (
                "stale plan_graph.json: graph tasks do not overlap selected iteration "
                "execution tasks; ignoring artifact"
            ),
        )

    extras = sorted(graph_task_ids - execution_task_ids)
    missing = sorted(execution_task_ids - graph_task_ids)
    notes: list[str] = []
    if extras:
        suffix = "..." if len(extras) > 5 else ""
        notes.append(f"extra={', '.join(extras[:5])}{suffix}")
    if missing:
        suffix = "..." if len(missing) > 5 else ""
        notes.append(f"missing={', '.join(missing[:5])}{suffix}")
    if notes:
        return (
            plan_graph_payload,
            False,
            "plan_graph.json task set differs from selected iteration execution tasks "
            f"({'; '.join(notes)})",
        )
    return (plan_graph_payload, False, "")


def _docs_sanitize_plan_check_result_payload(
    *,
    plan_check_result_payload: dict[str, Any] | None,
    graph_ignored_as_stale: bool,
) -> tuple[dict[str, Any] | None, bool, str]:
    if not isinstance(plan_check_result_payload, dict):
        return (plan_check_result_payload, False, "")
    if not graph_ignored_as_stale:
        return (plan_check_result_payload, False, "")
    return (
        None,
        True,
        (
            "stale plan_check_result.json: ignoring artifact because "
            "plan_graph.json was ignored as stale for the selected iteration"
        ),
    )


def _docs_apply_critical_path_projection(
    observability: dict[str, Any],
    *,
    critical_path: dict[str, Any],
) -> dict[str, Any]:
    projected = dict(observability)
    critical_task_ids = {
        str(item).strip()
        for item in _docs_non_empty_strings(critical_path.get("task_ids"))
    }
    critical_wave_ids = {
        _docs_safe_int(item, 0)
        for item in critical_path.get("wave_ids", [])
        if _docs_safe_int(item, 0) > 0
    }

    waves: list[dict[str, Any]] = []
    for row in projected.get("waves", []):
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["critical_path"] = _docs_safe_int(item.get("wave"), 0) in critical_wave_ids
        waves.append(item)

    tasks: list[dict[str, Any]] = []
    for row in projected.get("tasks", []):
        if not isinstance(row, dict):
            continue
        item = dict(row)
        item["critical_path"] = (
            str(item.get("task_id", "")).strip() in critical_task_ids
        )
        tasks.append(item)

    projected["critical_path"] = critical_path
    projected["waves"] = waves
    projected["tasks"] = tasks
    return projected


def _docs_path_within_repo_root(repo_root: Path, path: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(repo_root.resolve(strict=False))
        return True
    except Exception:
        return False


def _docs_read_text_limited(
    repo_root: Path,
    path: Path,
) -> tuple[str | None, str]:
    if not path.exists():
        return (None, f"missing {_docs_relpath(repo_root, path)}")
    if not path.is_file():
        return (None, f"expected regular file at {_docs_relpath(repo_root, path)}")
    try:
        file_size = int(path.stat().st_size)
    except Exception as exc:
        return (None, f"could not stat {_docs_relpath(repo_root, path)}: {exc}")
    if file_size > _DOCS_VIEW_MAX_READ_BYTES:
        return (
            None,
            (
                f"refusing to read {_docs_relpath(repo_root, path)}: "
                f"{file_size} bytes exceeds {_DOCS_VIEW_MAX_READ_BYTES} byte limit"
            ),
        )
    try:
        return (path.read_text(encoding="utf-8"), "")
    except Exception as exc:
        return (None, f"unable to read {_docs_relpath(repo_root, path)}: {exc}")


def _docs_load_json_mapping(
    repo_root: Path,
    path: Path,
) -> tuple[dict[str, Any] | None, str]:
    payload_text, read_error = _docs_read_text_limited(repo_root, path)
    if payload_text is None:
        return (None, read_error)
    try:
        payload = json.loads(payload_text)
    except Exception as exc:
        return (None, f"invalid JSON at {_docs_relpath(repo_root, path)}: {exc}")
    if not isinstance(payload, dict):
        return (None, f"invalid JSON object at {_docs_relpath(repo_root, path)}")
    return (payload, "")


def _docs_load_yaml_mapping(
    repo_root: Path,
    path: Path,
) -> tuple[dict[str, Any] | None, str]:
    if _yaml_mod is None:
        return (None, "PyYAML is unavailable")
    payload_text, read_error = _docs_read_text_limited(repo_root, path)
    if payload_text is None:
        return (None, read_error)
    try:
        payload = _yaml_mod.safe_load(payload_text)
    except Exception as exc:
        return (None, f"invalid YAML at {_docs_relpath(repo_root, path)}: {exc}")
    if not isinstance(payload, dict):
        return (None, f"expected YAML mapping at {_docs_relpath(repo_root, path)}")
    return (payload, "")


def _docs_resolve_pointer_path(
    repo_root: Path, raw_pointer: Any
) -> tuple[Path | None, str]:
    pointer_text = str(raw_pointer or "").strip()
    if not pointer_text:
        return (None, "")
    try:
        pointer_path = Path(pointer_text).expanduser()
    except Exception as exc:
        return (None, f"invalid pointer '{pointer_text}': {exc}")
    candidate = pointer_path if pointer_path.is_absolute() else repo_root / pointer_path
    try:
        resolved = candidate.resolve(strict=False)
    except Exception as exc:
        return (None, f"invalid pointer '{pointer_text}': {exc}")
    if not _docs_path_within_repo_root(repo_root, resolved):
        return (
            None,
            f"pointer '{pointer_text}' resolves outside repository root",
        )
    if resolved.exists() and not resolved.is_file():
        return (
            None,
            (
                f"pointer '{pointer_text}' resolves to non-regular file "
                f"{_docs_relpath(repo_root, resolved)}"
            ),
        )
    return (resolved, "")


def _docs_summarize_status_counts(rows: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        status = str(row.get("status", "")).strip().lower() or "unknown"
        counts[status] = int(counts.get(status, 0)) + 1
    if not counts:
        return "none"
    ordered_keys = sorted(counts.keys())
    return ", ".join(f"{key}={counts[key]}" for key in ordered_keys)


def _docs_select_views(raw_view: str) -> list[str]:
    normalized = str(raw_view or "").strip().lower() or "registry"
    if normalized == "all":
        return list(_DOCS_GENERATE_DEFAULT_VIEWS)
    return [normalized]


def _docs_collect_context(
    *,
    state_path: Path,
    iteration_override: str,
) -> tuple[dict[str, Any] | None, str]:
    repo_root = _resolve_repo_root(state_path)
    try:
        state = _normalize_state(_load_state(state_path))
    except RuntimeError as exc:
        return (None, str(exc))
    if iteration_override:
        state = dict(state)
        state["iteration_id"] = iteration_override

    policy = _load_verifier_policy(repo_root)
    scope_roots = policy.get("scope_roots")
    if not isinstance(scope_roots, dict):
        scope_roots = {}
    configured_project_wide_root = (
        str(scope_roots.get("project_wide_root", ".")).strip() or "."
    )
    try:
        resolved_project_wide_root = _resolve_project_wide_root(
            repo_root,
            scope_roots=scope_roots,
        )
    except StageCheckError as exc:
        return (None, str(exc))

    iteration_id = str(state.get("iteration_id", "")).strip()
    experiment_id = str(state.get("experiment_id", "")).strip()

    try:
        detected_scope_kind, effective_scope_root, scope_iteration_dir = (
            _resolve_scope_context(
                repo_root,
                iteration_id=iteration_id,
                experiment_id=experiment_id,
            )
        )
    except Exception:
        detected_scope_kind = "unknown"
        effective_scope_root = resolved_project_wide_root
        scope_iteration_dir = None

    iteration_dir: Path | None = scope_iteration_dir
    iteration_type = ""
    if iteration_id:
        try:
            resolved_iteration_dir, iteration_type = _resolve_iteration_directory(
                repo_root,
                iteration_id=iteration_id,
                experiment_id=experiment_id,
                require_exists=False,
            )
            iteration_dir = resolved_iteration_dir
        except StageCheckError:
            iteration_dir = None

    try:
        resolved_project_wide_root_text = (
            resolved_project_wide_root.relative_to(repo_root).as_posix() or "."
        )
    except ValueError:
        resolved_project_wide_root_text = str(resolved_project_wide_root)
    try:
        effective_scope_root_text = effective_scope_root.relative_to(
            repo_root
        ).as_posix()
        if not effective_scope_root_text:
            effective_scope_root_text = "."
    except ValueError:
        effective_scope_root_text = str(effective_scope_root)

    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    backlog_payload, backlog_error = _load_backlog_yaml(backlog_path)
    backlog_hypotheses: list[dict[str, Any]] = []
    backlog_experiments: list[dict[str, Any]] = []
    active_backlog_entry: dict[str, Any] | None = None
    active_backlog_error = ""
    if backlog_payload is not None:
        hypotheses = backlog_payload.get("hypotheses")
        if isinstance(hypotheses, list):
            backlog_hypotheses = [row for row in hypotheses if isinstance(row, dict)]
        experiments = backlog_payload.get("experiments")
        if isinstance(experiments, list):
            backlog_experiments = [row for row in experiments if isinstance(row, dict)]
        active_backlog_entry, active_backlog_error = _find_backlog_experiment_entry(
            backlog_payload,
            experiment_id=experiment_id,
            iteration_id=iteration_id,
        )

    design_path = (
        iteration_dir / "design.yaml"
        if iteration_dir is not None
        else repo_root / "experiments" / "plan" / iteration_id / "design.yaml"
    )
    design_payload, design_error = _docs_load_yaml_mapping(repo_root, design_path)

    plan_contract_path = (
        iteration_dir / "plan_contract.json"
        if iteration_dir is not None
        else repo_root / "experiments" / "plan" / iteration_id / "plan_contract.json"
    )
    plan_contract_payload, plan_contract_error = _docs_load_json_mapping(
        repo_root,
        plan_contract_path,
    )
    if plan_contract_payload is None:
        fallback_path = repo_root / ".autolab" / "plan_contract.json"
        fallback_payload, fallback_error = _docs_load_json_mapping(
            repo_root, fallback_path
        )
        if isinstance(fallback_payload, dict):
            fallback_iteration_id = str(
                fallback_payload.get("iteration_id", "")
            ).strip()
            if not fallback_iteration_id or fallback_iteration_id == iteration_id:
                plan_contract_payload = fallback_payload
                plan_contract_path = fallback_path
                plan_contract_error = ""
            else:
                plan_contract_error = (
                    "iteration-specific plan_contract.json missing and "
                    f".autolab/plan_contract.json targets iteration '{fallback_iteration_id}'"
                )
        elif fallback_error:
            plan_contract_error = plan_contract_error or fallback_error

    handoff_path = repo_root / ".autolab" / "handoff.json"
    handoff_payload, handoff_error = _docs_load_json_mapping(repo_root, handoff_path)
    handoff_markdown_path, handoff_pointer_error = _docs_resolve_pointer_path(
        repo_root,
        handoff_payload.get("handoff_markdown_path", "") if handoff_payload else "",
    )
    handoff_error = _docs_append_error(handoff_error, handoff_pointer_error)

    handoff_context_errors: list[str] = []
    if isinstance(handoff_payload, dict):
        handoff_iteration_id = str(handoff_payload.get("iteration_id", "")).strip()
        handoff_experiment_id = str(handoff_payload.get("experiment_id", "")).strip()
        if (
            handoff_iteration_id
            and iteration_id
            and handoff_iteration_id != iteration_id
        ):
            handoff_context_errors.append(
                "handoff iteration_id differs from requested iteration_id "
                f"({handoff_iteration_id} != {iteration_id})"
            )
        if (
            handoff_experiment_id
            and experiment_id
            and handoff_experiment_id != experiment_id
        ):
            handoff_context_errors.append(
                "handoff experiment_id differs from requested experiment_id "
                f"({handoff_experiment_id} != {experiment_id})"
            )

    trace_latest_path = repo_root / ".autolab" / "traceability_latest.json"
    trace_latest_payload, trace_latest_error = _docs_load_json_mapping(
        repo_root,
        trace_latest_path,
    )
    traceability_latest_pointer_path = None
    traceability_latest_iteration_id = ""
    traceability_latest_pointer_error = ""
    if isinstance(trace_latest_payload, dict):
        traceability_latest_iteration_id = str(
            trace_latest_payload.get("iteration_id", "")
        ).strip()
        (
            traceability_latest_pointer_path,
            traceability_latest_pointer_error,
        ) = _docs_resolve_pointer_path(
            repo_root,
            trace_latest_payload.get("traceability_path", ""),
        )
    trace_latest_error = _docs_append_error(
        trace_latest_error,
        traceability_latest_pointer_error,
    )

    traceability_path = (
        iteration_dir / "traceability_coverage.json"
        if iteration_dir is not None
        else None
    )
    traceability_payload = None
    traceability_error = "traceability coverage path is unavailable"
    if traceability_path is not None:
        traceability_payload, traceability_error = _docs_load_json_mapping(
            repo_root,
            traceability_path,
        )

    traceability_selection_diagnostics: list[str] = []
    pointer_iteration_mismatch = bool(
        traceability_latest_iteration_id
        and iteration_id
        and traceability_latest_iteration_id != iteration_id
    )
    if pointer_iteration_mismatch and traceability_payload is not None:
        traceability_selection_diagnostics.append(
            "traceability_latest iteration_id differs from requested iteration_id "
            f"({traceability_latest_iteration_id} != {iteration_id}); using iteration-scoped coverage"
        )

    if traceability_payload is None and traceability_latest_pointer_path is not None:
        pointer_traceability_payload, pointer_traceability_error = (
            _docs_load_json_mapping(
                repo_root,
                traceability_latest_pointer_path,
            )
        )
        if isinstance(pointer_traceability_payload, dict):
            pointer_payload_iteration_id = str(
                pointer_traceability_payload.get("iteration_id", "")
            ).strip()
            selected_pointer_iteration_id = (
                traceability_latest_iteration_id or pointer_payload_iteration_id
            )
            if (
                selected_pointer_iteration_id
                and iteration_id
                and selected_pointer_iteration_id != iteration_id
            ):
                traceability_selection_diagnostics.append(
                    "traceability_latest fallback iteration_id differs from requested "
                    f"iteration_id ({selected_pointer_iteration_id} != {iteration_id}); "
                    f"using fallback because iteration-scoped coverage is unavailable ({traceability_error})"
                )
            traceability_path = traceability_latest_pointer_path
            traceability_payload = pointer_traceability_payload
            traceability_error = ""
        else:
            traceability_error = _docs_append_error(
                traceability_error,
                f"traceability_latest fallback failed: {pointer_traceability_error}",
            )

    if pointer_iteration_mismatch and traceability_payload is None:
        traceability_selection_diagnostics.append(
            "traceability_latest iteration_id differs from requested iteration_id "
            f"({traceability_latest_iteration_id} != {iteration_id})"
        )

    if isinstance(traceability_latest_pointer_path, Path) and isinstance(
        traceability_path, Path
    ):
        try:
            latest_pointer_resolved = traceability_latest_pointer_path.resolve(
                strict=False
            )
            selected_traceability_resolved = traceability_path.resolve(strict=False)
        except Exception:
            latest_pointer_resolved = traceability_latest_pointer_path
            selected_traceability_resolved = traceability_path
        if latest_pointer_resolved != selected_traceability_resolved:
            traceability_selection_diagnostics.append(
                "traceability_latest.traceability_path differs from selected coverage path"
            )

    traceability_selection_error = "; ".join(
        item for item in traceability_selection_diagnostics if item
    )

    context_bundle_path = repo_root / ".autolab" / "context" / "bundle.json"
    context_bundle_payload, context_bundle_error = _docs_load_json_mapping(
        repo_root,
        context_bundle_path,
    )

    project_map_path, project_map_pointer_error = _docs_resolve_pointer_path(
        repo_root,
        context_bundle_payload.get("project_map_path", "")
        if context_bundle_payload
        else ".autolab/context/project_map.json",
    )
    project_map_payload = None
    project_map_error = "project map path is unavailable"
    if project_map_path is not None:
        project_map_payload, project_map_error = _docs_load_json_mapping(
            repo_root,
            project_map_path,
        )
    project_map_error = _docs_append_error(project_map_error, project_map_pointer_error)

    context_delta_path, context_delta_pointer_error = _docs_resolve_pointer_path(
        repo_root,
        context_bundle_payload.get("selected_experiment_delta_path", "")
        if context_bundle_payload
        else "",
    )
    if context_delta_path is None and iteration_dir is not None:
        context_delta_path = iteration_dir / "context_delta.json"
    context_delta_payload = None
    context_delta_error = "context delta path is unavailable"
    if context_delta_path is not None:
        context_delta_payload, context_delta_error = _docs_load_json_mapping(
            repo_root,
            context_delta_path,
        )
    context_delta_error = _docs_append_error(
        context_delta_error,
        context_delta_pointer_error,
    )

    plan_execution_state_path = (
        iteration_dir / "plan_execution_state.json"
        if iteration_dir is not None
        else None
    )
    plan_execution_state_payload = None
    plan_execution_state_error = "plan execution state path is unavailable"
    if plan_execution_state_path is not None:
        (
            plan_execution_state_payload,
            plan_execution_state_error,
        ) = _docs_load_json_mapping(repo_root, plan_execution_state_path)

    plan_execution_summary_path = (
        iteration_dir / "plan_execution_summary.json"
        if iteration_dir is not None
        else None
    )
    plan_execution_summary_payload = None
    plan_execution_summary_error = "plan execution summary path is unavailable"
    if plan_execution_summary_path is not None:
        (
            plan_execution_summary_payload,
            plan_execution_summary_error,
        ) = _docs_load_json_mapping(repo_root, plan_execution_summary_path)

    plan_graph_path = repo_root / ".autolab" / "plan_graph.json"
    plan_graph_payload, plan_graph_error = _docs_load_json_mapping(
        repo_root,
        plan_graph_path,
    )

    plan_check_result_path = repo_root / ".autolab" / "plan_check_result.json"
    plan_check_result_payload, plan_check_result_error = _docs_load_json_mapping(
        repo_root,
        plan_check_result_path,
    )

    observability_context_diagnostics: list[str] = []
    (
        plan_execution_state_payload,
        stale_plan_execution_state_error,
    ) = _docs_validate_iteration_scoped_observability_payload(
        artifact_name="plan_execution_state.json",
        payload=plan_execution_state_payload,
        iteration_id=iteration_id,
    )
    if stale_plan_execution_state_error:
        plan_execution_state_error = stale_plan_execution_state_error
        observability_context_diagnostics.append(stale_plan_execution_state_error)

    (
        plan_execution_summary_payload,
        stale_plan_execution_summary_error,
    ) = _docs_validate_iteration_scoped_observability_payload(
        artifact_name="plan_execution_summary.json",
        payload=plan_execution_summary_payload,
        iteration_id=iteration_id,
    )
    if stale_plan_execution_summary_error:
        plan_execution_summary_error = stale_plan_execution_summary_error
        observability_context_diagnostics.append(stale_plan_execution_summary_error)

    observability_context_diagnostics.extend(
        _docs_compare_observability_execution_payloads(
            plan_execution_state_payload=plan_execution_state_payload,
            plan_execution_summary_payload=plan_execution_summary_payload,
        )
    )

    execution_task_ids = _docs_collect_execution_task_ids(
        plan_execution_state_payload=plan_execution_state_payload,
        plan_execution_summary_payload=plan_execution_summary_payload,
    )
    (
        plan_graph_payload,
        graph_ignored_as_stale,
        plan_graph_diagnostic,
    ) = _docs_sanitize_plan_graph_payload(
        plan_graph_payload=plan_graph_payload,
        execution_task_ids=execution_task_ids,
    )
    if plan_graph_diagnostic:
        observability_context_diagnostics.append(plan_graph_diagnostic)
        if graph_ignored_as_stale:
            plan_graph_error = plan_graph_diagnostic

    (
        plan_check_result_payload,
        plan_check_result_ignored_as_stale,
        plan_check_result_diagnostic,
    ) = _docs_sanitize_plan_check_result_payload(
        plan_check_result_payload=plan_check_result_payload,
        graph_ignored_as_stale=graph_ignored_as_stale,
    )
    if plan_check_result_diagnostic:
        observability_context_diagnostics.append(plan_check_result_diagnostic)
        if plan_check_result_ignored_as_stale:
            plan_check_result_error = plan_check_result_diagnostic

    wave_observability = build_wave_observability(
        repo_root,
        iteration_dir=iteration_dir,
        graph_payload=plan_graph_payload,
        plan_check_payload=plan_check_result_payload,
        execution_state_payload=plan_execution_state_payload,
        execution_summary_payload=plan_execution_summary_payload,
    )
    summary_critical_path = None
    if isinstance(plan_execution_summary_payload, dict):
        raw_summary_critical_path = plan_execution_summary_payload.get("critical_path")
        if isinstance(raw_summary_critical_path, dict):
            summary_critical_path = raw_summary_critical_path
    if (
        isinstance(summary_critical_path, dict)
        and (
            graph_ignored_as_stale
            or not isinstance(plan_graph_payload, dict)
            or not plan_graph_payload
            or str(
                wave_observability.get("critical_path", {}).get("status", "")
            ).strip()
            != "available"
        )
        and str(summary_critical_path.get("status", "")).strip() == "available"
    ):
        wave_observability = _docs_apply_critical_path_projection(
            wave_observability,
            critical_path=summary_critical_path,
        )
        observability_context_diagnostics.append(
            "using plan_execution_summary.json critical_path projection because "
            "plan_graph.json is unavailable or stale"
        )
    existing_wave_diagnostics = wave_observability.get("diagnostics")
    if not isinstance(existing_wave_diagnostics, list):
        existing_wave_diagnostics = []
    merged_observability_diagnostics = _docs_merge_diagnostics(
        [str(item).strip() for item in existing_wave_diagnostics if str(item).strip()],
        observability_context_diagnostics,
    )
    wave_observability = dict(wave_observability)
    wave_observability["diagnostics"] = merged_observability_diagnostics

    return (
        {
            "repo_root": repo_root,
            "state_path": state_path,
            "state": state,
            "policy": policy,
            "scope_roots": scope_roots,
            "configured_project_wide_root": configured_project_wide_root,
            "resolved_project_wide_root": resolved_project_wide_root,
            "resolved_project_wide_root_text": resolved_project_wide_root_text,
            "detected_scope_kind": detected_scope_kind,
            "effective_scope_root": effective_scope_root,
            "effective_scope_root_text": effective_scope_root_text,
            "iteration_id": iteration_id,
            "experiment_id": experiment_id,
            "iteration_dir": iteration_dir,
            "iteration_type": iteration_type,
            "backlog_path": backlog_path,
            "backlog_payload": backlog_payload,
            "backlog_error": backlog_error,
            "backlog_hypotheses": backlog_hypotheses,
            "backlog_experiments": backlog_experiments,
            "active_backlog_entry": active_backlog_entry,
            "active_backlog_error": active_backlog_error,
            "design_path": design_path,
            "design_payload": design_payload,
            "design_error": design_error,
            "plan_contract_path": plan_contract_path,
            "plan_contract_payload": plan_contract_payload,
            "plan_contract_error": plan_contract_error,
            "handoff_path": handoff_path,
            "handoff_payload": handoff_payload,
            "handoff_error": handoff_error,
            "handoff_markdown_path": handoff_markdown_path,
            "handoff_context_errors": handoff_context_errors,
            "trace_latest_path": trace_latest_path,
            "trace_latest_payload": trace_latest_payload,
            "trace_latest_error": trace_latest_error,
            "traceability_path": traceability_path,
            "traceability_payload": traceability_payload,
            "traceability_error": traceability_error,
            "traceability_selection_error": traceability_selection_error,
            "context_bundle_path": context_bundle_path,
            "context_bundle_payload": context_bundle_payload,
            "context_bundle_error": context_bundle_error,
            "project_map_path": project_map_path,
            "project_map_payload": project_map_payload,
            "project_map_error": project_map_error,
            "context_delta_path": context_delta_path,
            "context_delta_payload": context_delta_payload,
            "context_delta_error": context_delta_error,
            "plan_execution_state_path": plan_execution_state_path,
            "plan_execution_state_payload": plan_execution_state_payload,
            "plan_execution_state_error": plan_execution_state_error,
            "plan_execution_summary_path": plan_execution_summary_path,
            "plan_execution_summary_payload": plan_execution_summary_payload,
            "plan_execution_summary_error": plan_execution_summary_error,
            "plan_graph_path": plan_graph_path,
            "plan_graph_payload": plan_graph_payload,
            "plan_graph_error": plan_graph_error,
            "plan_check_result_path": plan_check_result_path,
            "plan_check_result_payload": plan_check_result_payload,
            "plan_check_result_error": plan_check_result_error,
            "observability_context_diagnostics": observability_context_diagnostics,
            "wave_observability": wave_observability,
        },
        "",
    )


def _docs_is_state_context_error(error_text: str) -> bool:
    lowered = str(error_text or "").strip().lower()
    if not lowered:
        return False
    if lowered.startswith("state file"):
        return True
    return "state." in lowered


def _docs_collect_registry_fallback_context(
    *,
    state_path: Path,
    iteration_override: str,
    state_error: str,
) -> dict[str, Any]:
    repo_root = _resolve_repo_root(state_path)
    policy = _load_verifier_policy(repo_root)
    scope_roots = policy.get("scope_roots")
    if not isinstance(scope_roots, dict):
        scope_roots = {}
    configured_project_wide_root = (
        str(scope_roots.get("project_wide_root", ".")).strip() or "."
    )

    fallback_scope_error = ""
    try:
        resolved_project_wide_root = _resolve_project_wide_root(
            repo_root,
            scope_roots=scope_roots,
        )
    except StageCheckError as exc:
        fallback_scope_error = str(exc)
        resolved_project_wide_root = repo_root

    try:
        resolved_project_wide_root_text = (
            resolved_project_wide_root.relative_to(repo_root).as_posix() or "."
        )
    except ValueError:
        resolved_project_wide_root_text = str(resolved_project_wide_root)

    fallback_diagnostics: list[str] = []
    if state_error:
        fallback_diagnostics.append(
            f"state unavailable for registry view: {state_error}"
        )
    if fallback_scope_error:
        fallback_diagnostics.append(fallback_scope_error)

    return {
        "repo_root": repo_root,
        "state_path": state_path,
        "state": {},
        "policy": policy,
        "scope_roots": scope_roots,
        "configured_project_wide_root": configured_project_wide_root,
        "resolved_project_wide_root": resolved_project_wide_root,
        "resolved_project_wide_root_text": resolved_project_wide_root_text,
        "detected_scope_kind": "unknown",
        "effective_scope_root": resolved_project_wide_root,
        "effective_scope_root_text": resolved_project_wide_root_text,
        "iteration_id": iteration_override,
        "experiment_id": "",
        "docs_generate_context_error": "; ".join(fallback_diagnostics),
    }


def _render_docs_registry_view(
    context: dict[str, Any],
    *,
    registry: dict[str, StageSpec],
) -> str:
    lines: list[str] = []
    lines.append("# Autolab Stage Flow")
    lines.append("")
    active = [
        name
        for name, spec in registry.items()
        if spec.is_active and not spec.is_terminal
    ]
    flow_parts: list[str] = []
    for name in active:
        spec = registry[name]
        if spec.decision_map:
            targets = ", ".join(sorted(spec.decision_map.values()))
            flow_parts.append(f"{name} -> {{{targets}}}")
        elif spec.next_stage:
            flow_parts.append(f"{name} -> {spec.next_stage}")
        else:
            flow_parts.append(name)
    lines.append(" | ".join(flow_parts))
    lines.append("")
    lines.append("## Scope Roots")
    lines.append("")
    lines.append(
        f"- configured_project_wide_root: `{context.get('configured_project_wide_root', '.')}`"
    )
    lines.append(
        f"- resolved_project_wide_root: `{context.get('resolved_project_wide_root_text', '.')}`"
    )
    lines.append(
        f"- detected_scope_kind: `{context.get('detected_scope_kind', 'unknown')}`"
    )
    lines.append(
        f"- effective_scope_root: `{context.get('effective_scope_root_text', '.')}`"
    )
    lines.append("")
    lines.append("## Artifact Map")
    lines.append("")
    lines.append("| Stage | Required Outputs |")
    lines.append("|-------|-----------------|")
    for name, spec in registry.items():
        outputs_parts: list[str] = []
        if spec.required_outputs:
            outputs_parts.append(", ".join(spec.required_outputs))
        for group in spec.required_outputs_any_of:
            outputs_parts.append(f"one-of({', '.join(group)})")
        for conditions, outputs in spec.required_outputs_if:
            condition_text = ", ".join(f"{key}={value}" for key, value in conditions)
            outputs_parts.append(f"when {condition_text}: {', '.join(outputs)}")
        outputs_text = "; ".join(outputs_parts) if outputs_parts else "(none)"
        lines.append(f"| {name} | {outputs_text} |")
    lines.append("")
    lines.append("## Token Reference")
    lines.append("")
    lines.append("| Stage | Required Tokens |")
    lines.append("|-------|----------------|")
    for name, spec in registry.items():
        tokens = (
            ", ".join(sorted(spec.required_tokens))
            if spec.required_tokens
            else "(none)"
        )
        lines.append(f"| {name} | {tokens} |")
    lines.append("")
    lines.append("## Classifications")
    lines.append("")
    lines.append("| Stage | Active | Terminal | Decision | Runner Eligible |")
    lines.append("|-------|--------|----------|----------|----------------|")
    for name, spec in registry.items():
        lines.append(
            f"| {name} | {spec.is_active} | {spec.is_terminal} | {spec.is_decision} | {spec.is_runner_eligible} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def _docs_wave_observability(context: dict[str, Any]) -> dict[str, Any]:
    payload = context.get("wave_observability")
    return payload if isinstance(payload, dict) else {}


def _docs_append_wave_observability_sections(
    lines: list[str],
    *,
    context: dict[str, Any],
    include_task_evidence: bool,
) -> None:
    observability = _docs_wave_observability(context)
    summary = observability.get("summary")
    if not isinstance(summary, dict):
        summary = {}
    critical_path = observability.get("critical_path")
    if not isinstance(critical_path, dict):
        critical_path = {}
    waves = observability.get("waves")
    if not isinstance(waves, list):
        waves = []
    conflicts = observability.get("file_conflicts")
    if not isinstance(conflicts, list):
        conflicts = []
    tasks = observability.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
    critical_wave_ids = _docs_non_empty_strings(critical_path.get("wave_ids"))
    critical_task_ids = _docs_non_empty_strings(critical_path.get("task_ids"))

    lines.extend(
        [
            "",
            "## Wave Observability",
            f"- status: `{observability.get('status', 'unavailable')}`",
            (
                "- waves: "
                f"total={_docs_safe_int(summary.get('waves_total', 0), 0)}, "
                f"executed={_docs_safe_int(summary.get('waves_executed', 0), 0)}, "
                f"retrying={_docs_safe_int(summary.get('retrying_waves', 0), 0)}"
            ),
            (
                "- tasks: "
                f"total={_docs_safe_int(summary.get('tasks_total', 0), 0)}, "
                f"completed={_docs_safe_int(summary.get('tasks_completed', 0), 0)}, "
                f"failed={_docs_safe_int(summary.get('tasks_failed', 0), 0)}, "
                f"blocked={_docs_safe_int(summary.get('tasks_blocked', 0), 0)}, "
                f"pending={_docs_safe_int(summary.get('tasks_pending', 0), 0)}, "
                f"skipped={_docs_safe_int(summary.get('tasks_skipped', 0), 0)}, "
                f"deferred={_docs_safe_int(summary.get('tasks_deferred', 0), 0)}"
            ),
            f"- conflicts: `{_docs_safe_int(summary.get('conflict_count', 0), 0)}`",
            f"- plan_execution_summary_path: `{_docs_relpath(context['repo_root'], context.get('plan_execution_summary_path'))}`",
            f"- plan_execution_state_path: `{_docs_relpath(context['repo_root'], context.get('plan_execution_state_path'))}`",
            f"- plan_graph_path: `{_docs_relpath(context['repo_root'], context.get('plan_graph_path'))}`",
            f"- plan_check_result_path: `{_docs_relpath(context['repo_root'], context.get('plan_check_result_path'))}`",
            "",
            "## Critical Path",
            f"- status: `{critical_path.get('status', 'unavailable')}`",
            f"- mode: `{critical_path.get('mode', 'unavailable')}`",
            f"- weight: `{critical_path.get('weight', 0)}`",
            f"- duration_seconds: `{critical_path.get('duration_seconds', 0)}`",
            f"- wave_count: `{len(critical_wave_ids)}`",
            f"- task_count: `{len(critical_task_ids)}`",
            f"- waves: `{', '.join(critical_wave_ids) or '-'}`",
            f"- tasks: `{', '.join(critical_task_ids) or '-'}`",
            f"- basis: {critical_path.get('basis_note', '')}",
            "",
            "## Wave Details",
            "",
            "| Wave | Status | Tasks | Attempts | Retries | Duration (s) | Last Attempt (s) | Retry Pending | Critical Path |",
            "|------|--------|-------|----------|---------|--------------|------------------|---------------|---------------|",
        ]
    )
    if waves:
        for entry in waves:
            if not isinstance(entry, dict):
                continue
            lines.append(
                "| {wave} | {status} | {tasks} | {attempts} | {retries} | {duration} | {last_attempt} | {retry_pending} | {critical} |".format(
                    wave=_docs_markdown_escape(str(entry.get("wave", ""))),
                    status=_docs_markdown_escape(str(entry.get("status", "unknown"))),
                    tasks=_docs_markdown_escape(
                        ", ".join(_docs_non_empty_strings(entry.get("tasks")))
                        or "(none)"
                    ),
                    attempts=_docs_markdown_escape(str(entry.get("attempts", 0))),
                    retries=_docs_markdown_escape(str(entry.get("retries_used", 0))),
                    duration=_docs_markdown_escape(
                        str(
                            round(
                                _docs_safe_float(entry.get("duration_seconds", 0), 0.0),
                                3,
                            )
                        )
                    ),
                    last_attempt=_docs_markdown_escape(
                        str(
                            round(
                                _docs_safe_float(
                                    entry.get("last_attempt_duration_seconds", 0),
                                    0.0,
                                ),
                                3,
                            )
                        )
                    ),
                    retry_pending="yes" if bool(entry.get("retry_pending")) else "no",
                    critical="yes" if bool(entry.get("critical_path")) else "no",
                )
            )
    else:
        lines.append("| (none) |  |  |  |  |  |  |  |  |")

    lines.extend(["", "## Wave Detail Notes", ""])
    if waves:
        for entry in waves:
            if not isinstance(entry, dict):
                continue
            lines.extend(
                [
                    (
                        f"- wave {entry.get('wave', '?')}: "
                        f"timing={_docs_format_seconds(entry.get('duration_seconds', 0), blank='n/a')} "
                        f"(last_attempt={_docs_format_seconds(entry.get('last_attempt_duration_seconds', 0), blank='n/a')}, "
                        f"window={entry.get('started_at', '') or '-'} -> {entry.get('completed_at', '') or '-'})"
                    ),
                    f"  retry_reasons: {', '.join(_docs_non_empty_strings(entry.get('retry_reasons'))) or 'none'}",
                    f"  blocked_tasks: {', '.join(_docs_non_empty_strings(entry.get('blocked_task_ids'))) or 'none'}",
                    f"  deferred_tasks: {', '.join(_docs_non_empty_strings(entry.get('deferred_task_ids'))) or 'none'}",
                    f"  skipped_tasks: {', '.join(_docs_non_empty_strings(entry.get('skipped_task_ids'))) or 'none'}",
                    f"  pending_tasks: {', '.join(_docs_non_empty_strings(entry.get('pending_task_ids'))) or 'none'}",
                    f"  failed_tasks: {', '.join(_docs_non_empty_strings(entry.get('failed_task_ids'))) or 'none'}",
                    f"  out_of_contract_paths: {', '.join(_docs_non_empty_strings(entry.get('out_of_contract_paths'))) or 'none'}",
                ]
            )
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "## File Conflicts",
            "",
            "| Wave | Kind | Tasks | Detail |",
            "|------|------|-------|--------|",
        ]
    )
    if conflicts:
        for entry in conflicts:
            if not isinstance(entry, dict):
                continue
            lines.append(
                "| {wave} | {kind} | {tasks} | {detail} |".format(
                    wave=_docs_markdown_escape(str(entry.get("wave", ""))),
                    kind=_docs_markdown_escape(str(entry.get("kind", ""))),
                    tasks=_docs_markdown_escape(
                        ", ".join(
                            str(item).strip()
                            for item in entry.get("tasks", [])
                            if str(item).strip()
                        )
                        or "(none)"
                    ),
                    detail=_docs_markdown_escape(str(entry.get("detail", ""))),
                )
            )
    else:
        lines.append("| (none) |  |  |  |")

    if include_task_evidence:
        lines.extend(
            [
                "",
                "## Task Evidence",
                "",
                "| Task | Wave | Status | Attempts | Retries | Duration (s) | Reason | Blocked By | Verification | Evidence | Critical Path |",
                "|------|------|--------|----------|---------|--------------|--------|------------|--------------|----------|---------------|",
            ]
        )
        if tasks:
            for entry in tasks:
                if not isinstance(entry, dict):
                    continue
                evidence = entry.get("evidence_summary")
                if not isinstance(evidence, dict):
                    evidence = {}
                reason_code = str(entry.get("reason_code", "")).strip()
                reason_detail = str(entry.get("reason_detail", "")).strip()
                reason_text = reason_code or "-"
                if reason_detail:
                    reason_text = (
                        f"{reason_code} ({reason_detail})"
                        if reason_code
                        else reason_detail
                    )
                lines.append(
                    "| {task} | {wave} | {status} | {attempts} | {retries} | {duration} | {reason} | {blocked_by} | {verification} | {evidence} | {critical} |".format(
                        task=_docs_markdown_escape(str(entry.get("task_id", ""))),
                        wave=_docs_markdown_escape(str(entry.get("wave", ""))),
                        status=_docs_markdown_escape(str(entry.get("status", ""))),
                        attempts=_docs_markdown_escape(str(entry.get("attempts", 0))),
                        retries=_docs_markdown_escape(
                            str(entry.get("retries_used", 0))
                        ),
                        duration=_docs_markdown_escape(
                            str(
                                round(
                                    _docs_safe_float(
                                        entry.get("duration_seconds", 0),
                                        0.0,
                                    ),
                                    3,
                                )
                            )
                        ),
                        reason=_docs_markdown_escape(reason_text),
                        blocked_by=_docs_markdown_escape(
                            ", ".join(_docs_non_empty_strings(entry.get("blocked_by")))
                            or "-"
                        ),
                        verification=_docs_markdown_escape(
                            str(entry.get("verification_status", "not_run"))
                            or "not_run"
                        ),
                        evidence=_docs_markdown_escape(
                            str(evidence.get("text", "")) or "n/a"
                        ),
                        critical="yes" if bool(entry.get("critical_path")) else "no",
                    )
                )
        else:
            lines.append("| (none) |  |  |  |  |  |  |  |  |  |  |")


def _render_docs_project_view(context: dict[str, Any]) -> str:
    repo_root = context["repo_root"]
    state = context["state"]
    backlog_hypotheses = context.get("backlog_hypotheses", [])
    backlog_experiments = context.get("backlog_experiments", [])
    active_backlog_entry = context.get("active_backlog_entry")
    trace_summary = {}
    traceability_payload = context.get("traceability_payload")
    if isinstance(traceability_payload, dict):
        raw_summary = traceability_payload.get("summary")
        if isinstance(raw_summary, dict):
            trace_summary = raw_summary
    if not trace_summary:
        trace_latest = context.get("trace_latest_payload")
        if isinstance(trace_latest, dict):
            raw_summary = trace_latest.get("summary")
            if isinstance(raw_summary, dict):
                trace_summary = raw_summary

    context_bundle = context.get("context_bundle_payload")
    if not isinstance(context_bundle, dict):
        context_bundle = {}

    lines: list[str] = [
        "# Project View",
        "",
        f"- repo_root: `{repo_root}`",
        f"- state_file: `{context.get('state_path')}`",
        f"- iteration_id: `{context.get('iteration_id', '')}`",
        f"- experiment_id: `{context.get('experiment_id', '')}`",
        f"- stage: `{state.get('stage', '')}`",
        f"- scope: `{context.get('detected_scope_kind', 'unknown')}`",
        f"- scope_root: `{context.get('effective_scope_root_text', '.')}`",
        f"- configured_project_wide_root: `{context.get('configured_project_wide_root', '.')}`",
        f"- resolved_project_wide_root: `{context.get('resolved_project_wide_root_text', '.')}`",
        "",
        "## Roadmap Summary",
        f"- hypotheses_total: {len(backlog_hypotheses)} ({_docs_summarize_status_counts(backlog_hypotheses)})",
        f"- experiments_total: {len(backlog_experiments)} ({_docs_summarize_status_counts(backlog_experiments)})",
    ]
    if isinstance(active_backlog_entry, dict):
        lines.append(
            "- active_backlog_experiment: "
            f"{active_backlog_entry.get('id', '')} "
            f"(status={active_backlog_entry.get('status', '')}, "
            f"type={active_backlog_entry.get('type', '') or 'plan'})"
        )
    else:
        lines.append("- active_backlog_experiment: unavailable")

    lines.extend(
        [
            "",
            "## Coverage Snapshot",
            "- traceability_latest_path: "
            f"`{_docs_relpath(repo_root, context.get('trace_latest_path'))}`",
            "- traceability_coverage_path: "
            f"`{_docs_relpath(repo_root, context.get('traceability_path'))}`",
        ]
    )
    if trace_summary:
        lines.append(
            "- rows: "
            f"total={_docs_safe_int(trace_summary.get('rows_total', 0), 0)}, "
            f"covered={_docs_safe_int(trace_summary.get('rows_covered', 0), 0)}, "
            f"untested={_docs_safe_int(trace_summary.get('rows_untested', 0), 0)}, "
            f"failed={_docs_safe_int(trace_summary.get('rows_failed', 0), 0)}"
        )
        lines.append(
            "- requirements: "
            f"total={_docs_safe_int(trace_summary.get('requirements_total', 0), 0)}, "
            f"covered={_docs_safe_int(trace_summary.get('requirements_covered', 0), 0)}, "
            f"untested={_docs_safe_int(trace_summary.get('requirements_untested', 0), 0)}, "
            f"failed={_docs_safe_int(trace_summary.get('requirements_failed', 0), 0)}"
        )
    else:
        lines.append("- summary: unavailable")

    lines.extend(
        [
            "",
            "## Context Bundle",
            f"- context_bundle_path: `{_docs_relpath(repo_root, context.get('context_bundle_path'))}`",
            f"- project_map_path: `{_docs_relpath(repo_root, context.get('project_map_path'))}`",
            f"- selected_experiment_delta_path: `{_docs_relpath(repo_root, context.get('context_delta_path'))}`",
            f"- focus_iteration_id: `{context_bundle.get('focus_iteration_id', '')}`",
            f"- focus_experiment_id: `{context_bundle.get('focus_experiment_id', '')}`",
        ]
    )
    _docs_append_wave_observability_sections(
        lines,
        context=context,
        include_task_evidence=False,
    )
    diagnostics = []
    for key in (
        "backlog_error",
        "active_backlog_error",
        "trace_latest_error",
        "traceability_error",
        "traceability_selection_error",
        "context_bundle_error",
        "project_map_error",
        "context_delta_error",
        "plan_execution_state_error",
        "plan_execution_summary_error",
        "plan_graph_error",
        "plan_check_result_error",
    ):
        value = str(context.get(key, "")).strip()
        if value:
            diagnostics.append(value)
    wave_observability = _docs_wave_observability(context)
    wave_diagnostics = wave_observability.get("diagnostics")
    if isinstance(wave_diagnostics, list):
        diagnostics.extend(
            str(item).strip() for item in wave_diagnostics if str(item).strip()
        )
    lines.extend(["", "## Diagnostics"])
    if diagnostics:
        lines.extend(f"- {item}" for item in diagnostics)
    else:
        lines.append("- none")
    return "\n".join(lines).rstrip() + "\n"


def _render_docs_roadmap_view(context: dict[str, Any]) -> str:
    repo_root = context["repo_root"]
    backlog_payload = context.get("backlog_payload")
    backlog_hypotheses = context.get("backlog_hypotheses", [])
    backlog_experiments = context.get("backlog_experiments", [])
    active_iteration = str(context.get("iteration_id", "")).strip()
    active_experiment = str(context.get("experiment_id", "")).strip()

    lines: list[str] = [
        "# Roadmap View",
        "",
        f"- backlog_path: `{_docs_relpath(repo_root, context.get('backlog_path'))}`",
    ]
    if not isinstance(backlog_payload, dict):
        lines.extend(
            [
                "- status: unavailable",
                "",
                "## Diagnostics",
                f"- {context.get('backlog_error', 'backlog is unavailable')}",
            ]
        )
        return "\n".join(lines).rstrip() + "\n"

    lines.extend(
        [
            "- status: available",
            f"- hypotheses_total: {len(backlog_hypotheses)} ({_docs_summarize_status_counts(backlog_hypotheses)})",
            f"- experiments_total: {len(backlog_experiments)} ({_docs_summarize_status_counts(backlog_experiments)})",
            "",
            "## Experiments",
            "",
            "| Experiment | Hypothesis | Status | Type | Iteration | Active |",
            "|------------|------------|--------|------|-----------|--------|",
        ]
    )
    if backlog_experiments:
        for entry in backlog_experiments:
            experiment_id = str(entry.get("id", "")).strip()
            hypothesis_id = str(entry.get("hypothesis_id", "")).strip()
            status = str(entry.get("status", "")).strip() or "unknown"
            experiment_type = str(entry.get("type", "")).strip() or "plan"
            iteration_id = str(entry.get("iteration_id", "")).strip()
            is_active = experiment_id == active_experiment or (
                iteration_id and iteration_id == active_iteration
            )
            lines.append(
                "| {experiment} | {hypothesis} | {status} | {etype} | {iteration} | {active} |".format(
                    experiment=_docs_markdown_escape(experiment_id),
                    hypothesis=_docs_markdown_escape(hypothesis_id),
                    status=_docs_markdown_escape(status),
                    etype=_docs_markdown_escape(experiment_type),
                    iteration=_docs_markdown_escape(iteration_id),
                    active="yes" if is_active else "no",
                )
            )
    else:
        lines.append("| (none) |  |  |  |  |  |")

    lines.extend(
        [
            "",
            "## Hypotheses",
            "",
            "| Hypothesis | Status | Success Metric | Target Delta |",
            "|------------|--------|----------------|--------------|",
        ]
    )
    if backlog_hypotheses:
        for entry in backlog_hypotheses:
            hypothesis_id = str(entry.get("id", "")).strip()
            status = str(entry.get("status", "")).strip() or "unknown"
            success_metric = str(entry.get("success_metric", "")).strip()
            target_delta = str(entry.get("target_delta", "")).strip()
            lines.append(
                "| {hypothesis} | {status} | {metric} | {delta} |".format(
                    hypothesis=_docs_markdown_escape(hypothesis_id),
                    status=_docs_markdown_escape(status),
                    metric=_docs_markdown_escape(success_metric),
                    delta=_docs_markdown_escape(target_delta),
                )
            )
    else:
        lines.append("| (none) |  |  |  |")

    return "\n".join(lines).rstrip() + "\n"


def _render_docs_state_view(context: dict[str, Any]) -> str:
    repo_root = context["repo_root"]
    state = context["state"]
    handoff_payload = context.get("handoff_payload")
    if not isinstance(handoff_payload, dict):
        handoff_payload = {}
    wave = handoff_payload.get("wave")
    if not isinstance(wave, dict):
        wave = {}
    task_status = handoff_payload.get("task_status")
    if not isinstance(task_status, dict):
        task_status = {}
    safe_resume = handoff_payload.get("safe_resume_point")
    if not isinstance(safe_resume, dict):
        safe_resume = {}
    recommended = handoff_payload.get("recommended_next_command")
    if not isinstance(recommended, dict):
        recommended = {}
    blocking_failures = handoff_payload.get("blocking_failures")
    if not isinstance(blocking_failures, list):
        blocking_failures = []
    pending_decisions = handoff_payload.get("pending_human_decisions")
    if not isinstance(pending_decisions, list):
        pending_decisions = []
    wave_observability = _docs_wave_observability(context)
    wave_summary = wave_observability.get("wave_summary")
    if not isinstance(wave_summary, dict):
        wave_summary = {}
    task_summary = wave_observability.get("task_summary")
    if not isinstance(task_summary, dict):
        task_summary = {}

    lines: list[str] = [
        "# State View",
        "",
        f"- state_file: `{context.get('state_path')}`",
        f"- iteration_id: `{state.get('iteration_id', '')}`",
        f"- experiment_id: `{state.get('experiment_id', '')}`",
        f"- stage: `{state.get('stage', '')}`",
        f"- stage_attempt: `{state.get('stage_attempt', 0)}` / `{state.get('max_stage_attempts', 0)}`",
        f"- last_run_id: `{state.get('last_run_id', '')}`",
        f"- sync_status: `{state.get('sync_status', '')}`",
        f"- assistant_mode: `{state.get('assistant_mode', '')}`",
        f"- current_scope: `{context.get('detected_scope_kind', 'unknown')}`",
        f"- effective_scope_root: `{context.get('effective_scope_root_text', '.')}`",
        "",
        "## Handoff Readiness",
        f"- handoff_json_path: `{_docs_relpath(repo_root, context.get('handoff_path'))}`",
        f"- handoff_markdown_path: `{_docs_relpath(repo_root, context.get('handoff_markdown_path'))}`",
        f"- safe_resume_status: `{safe_resume.get('status', 'blocked')}`",
        f"- safe_resume_command: `{safe_resume.get('command', '')}`",
        f"- recommended_next_command: `{recommended.get('command', '')}`",
        f"- blockers: {len(blocking_failures)}",
        f"- pending_human_decisions: {len(pending_decisions)}",
        "",
        "## Wave and Task Status",
        f"- wave: status={wave_summary.get('status', wave.get('status', 'unavailable'))}, current={wave_summary.get('current', wave.get('current', '-'))}, executed={wave_summary.get('executed', wave.get('executed', 0))}, total={wave_summary.get('total', wave.get('total', 0))}",
        f"- tasks: status={task_summary.get('status', task_status.get('status', 'unavailable'))}, total={task_summary.get('total', task_status.get('total', 0))}, completed={task_summary.get('completed', task_status.get('completed', 0))}, failed={task_summary.get('failed', task_status.get('failed', 0))}, blocked={task_summary.get('blocked', task_status.get('blocked', 0))}, pending={task_summary.get('pending', task_status.get('pending', 0))}, skipped={task_summary.get('skipped', 0)}, deferred={task_summary.get('deferred', 0)}",
    ]
    _docs_append_wave_observability_sections(
        lines,
        context=context,
        include_task_evidence=True,
    )
    lines.extend(["", "## Diagnostics"])
    diagnostics = []
    handoff_error = str(context.get("handoff_error", "")).strip()
    if handoff_error:
        diagnostics.append(handoff_error)
    handoff_context_errors = context.get("handoff_context_errors", [])
    if isinstance(handoff_context_errors, list):
        for message in handoff_context_errors:
            message_text = str(message).strip()
            if message_text:
                diagnostics.append(message_text)
    for key in (
        "plan_execution_state_error",
        "plan_execution_summary_error",
        "plan_graph_error",
        "plan_check_result_error",
    ):
        value = str(context.get(key, "")).strip()
        if value:
            diagnostics.append(value)
    wave_diagnostics = wave_observability.get("diagnostics")
    if isinstance(wave_diagnostics, list):
        diagnostics.extend(
            str(item).strip() for item in wave_diagnostics if str(item).strip()
        )
    if diagnostics:
        lines.extend(f"- {item}" for item in diagnostics)
    else:
        lines.append("- none")
    return "\n".join(lines).rstrip() + "\n"


def _render_docs_requirements_view(context: dict[str, Any]) -> str:
    repo_root = context["repo_root"]
    design_payload = context.get("design_payload")
    plan_contract_payload = context.get("plan_contract_payload")
    traceability_payload = context.get("traceability_payload")

    requirements: list[dict[str, Any]] = []
    if isinstance(design_payload, dict):
        raw_requirements = design_payload.get("implementation_requirements")
        if isinstance(raw_requirements, list):
            for row in raw_requirements:
                if isinstance(row, dict):
                    requirement_id = str(row.get("requirement_id", "")).strip()
                    if requirement_id:
                        requirements.append(row)

    requirement_to_tasks: dict[str, list[str]] = {}
    if isinstance(plan_contract_payload, dict):
        raw_tasks = plan_contract_payload.get("tasks")
        if isinstance(raw_tasks, list):
            for row in raw_tasks:
                if not isinstance(row, dict):
                    continue
                task_id = str(row.get("task_id", "")).strip()
                if not task_id:
                    continue
                covers = row.get("covers_requirements")
                if isinstance(covers, list):
                    for raw_requirement_id in covers:
                        requirement_id = str(raw_requirement_id).strip()
                        if not requirement_id:
                            continue
                        requirement_to_tasks.setdefault(requirement_id, [])
                        if task_id not in requirement_to_tasks[requirement_id]:
                            requirement_to_tasks[requirement_id].append(task_id)

    requirement_to_trace_statuses: dict[str, list[str]] = {}
    if isinstance(traceability_payload, dict):
        links = traceability_payload.get("links")
        if isinstance(links, list):
            for row in links:
                if not isinstance(row, dict):
                    continue
                requirement_id = str(row.get("requirement_id", "")).strip()
                status = str(row.get("coverage_status", "")).strip().lower()
                if not requirement_id or not status:
                    continue
                requirement_to_trace_statuses.setdefault(requirement_id, [])
                requirement_to_trace_statuses[requirement_id].append(status)

    def _aggregate_requirement_status(
        requirement_id: str,
        *,
        has_tasks: bool,
    ) -> str:
        statuses = requirement_to_trace_statuses.get(requirement_id, [])
        if statuses:
            if "failed" in statuses:
                return "failed"
            if "untested" in statuses:
                return "untested"
            if "covered" in statuses:
                return "covered"
            return "unknown"
        if not has_tasks:
            return "unmapped"
        return "unknown"

    lines: list[str] = [
        "# Requirements View",
        "",
        f"- iteration_id: `{context.get('iteration_id', '')}`",
        f"- design_path: `{_docs_relpath(repo_root, context.get('design_path'))}`",
        f"- plan_contract_path: `{_docs_relpath(repo_root, context.get('plan_contract_path'))}`",
        f"- traceability_coverage_path: `{_docs_relpath(repo_root, context.get('traceability_path'))}`",
        "",
        "| Requirement | Scope | Tasks | Coverage | Expected Artifacts | Description |",
        "|-------------|-------|-------|----------|--------------------|-------------|",
    ]
    if requirements:
        for row in requirements:
            requirement_id = str(row.get("requirement_id", "")).strip()
            scope_kind = str(row.get("scope_kind", "")).strip() or "unspecified"
            description = str(row.get("description", "")).strip()
            expected_artifacts_raw = row.get("expected_artifacts")
            expected_artifacts: list[str] = []
            if isinstance(expected_artifacts_raw, list):
                expected_artifacts = [
                    str(item).strip()
                    for item in expected_artifacts_raw
                    if str(item).strip()
                ]
            tasks = sorted(set(requirement_to_tasks.get(requirement_id, [])))
            coverage = _aggregate_requirement_status(
                requirement_id,
                has_tasks=bool(tasks),
            )
            lines.append(
                "| {requirement} | {scope} | {tasks} | {coverage} | {artifacts} | {description} |".format(
                    requirement=_docs_markdown_escape(requirement_id),
                    scope=_docs_markdown_escape(scope_kind),
                    tasks=_docs_markdown_escape(
                        ", ".join(tasks) if tasks else "(none)"
                    ),
                    coverage=_docs_markdown_escape(coverage),
                    artifacts=_docs_markdown_escape(
                        ", ".join(expected_artifacts)
                        if expected_artifacts
                        else "(none)"
                    ),
                    description=_docs_markdown_escape(description),
                )
            )
    else:
        lines.append("| (none) |  |  |  |  |  |")

    covered = 0
    untested = 0
    failed = 0
    unmapped = 0
    unknown = 0
    for row in requirements:
        requirement_id = str(row.get("requirement_id", "")).strip()
        tasks = requirement_to_tasks.get(requirement_id, [])
        status = _aggregate_requirement_status(requirement_id, has_tasks=bool(tasks))
        if status == "covered":
            covered += 1
        elif status == "untested":
            untested += 1
        elif status == "failed":
            failed += 1
        elif status == "unmapped":
            unmapped += 1
        else:
            unknown += 1

    lines.extend(
        [
            "",
            "## Summary",
            (
                f"- requirements_total={len(requirements)}, covered={covered}, "
                f"untested={untested}, failed={failed}, unmapped={unmapped}, unknown={unknown}"
            ),
            "",
            "## Diagnostics",
        ]
    )
    diagnostics = []
    for key in (
        "design_error",
        "plan_contract_error",
        "traceability_error",
        "traceability_selection_error",
    ):
        value = str(context.get(key, "")).strip()
        if value:
            diagnostics.append(value)
    if diagnostics:
        lines.extend(f"- {item}" for item in diagnostics)
    else:
        lines.append("- none")
    return "\n".join(lines).rstrip() + "\n"


def _render_docs_sidecar_view(context: dict[str, Any]) -> str:
    repo_root = context["repo_root"]
    trace_latest = context.get("trace_latest_payload")
    if not isinstance(trace_latest, dict):
        trace_latest = {}
    handoff = context.get("handoff_payload")
    if not isinstance(handoff, dict):
        handoff = {}
    bundle = context.get("context_bundle_payload")
    if not isinstance(bundle, dict):
        bundle = {}
    project_map = context.get("project_map_payload")
    if not isinstance(project_map, dict):
        project_map = {}
    context_delta = context.get("context_delta_payload")
    if not isinstance(context_delta, dict):
        context_delta = {}
    traceability_payload = context.get("traceability_payload")
    if not isinstance(traceability_payload, dict):
        traceability_payload = {}
    plan_execution_state_payload = context.get("plan_execution_state_payload")
    if not isinstance(plan_execution_state_payload, dict):
        plan_execution_state_payload = {}
    plan_execution_summary_payload = context.get("plan_execution_summary_payload")
    if not isinstance(plan_execution_summary_payload, dict):
        plan_execution_summary_payload = {}
    plan_graph_payload = context.get("plan_graph_payload")
    if not isinstance(plan_graph_payload, dict):
        plan_graph_payload = {}
    plan_check_result_payload = context.get("plan_check_result_payload")
    if not isinstance(plan_check_result_payload, dict):
        plan_check_result_payload = {}
    wave_observability = _docs_wave_observability(context)
    critical_path = wave_observability.get("critical_path")
    if not isinstance(critical_path, dict):
        critical_path = {}

    def _status_from_error(payload: dict[str, Any], error: str) -> str:
        error_text = str(error or "").strip()
        if error_text.startswith("stale "):
            return "stale"
        if payload:
            return "present"
        if error_text.startswith("missing "):
            return "missing"
        return "invalid"

    handoff_md_path = context.get("handoff_markdown_path")
    handoff_md_status = "missing"
    if isinstance(handoff_md_path, Path):
        handoff_md_status = "present" if handoff_md_path.exists() else "missing"

    trace_summary = traceability_payload.get("summary")
    if not isinstance(trace_summary, dict):
        trace_summary = {}

    lines: list[str] = [
        "# Sidecar View",
        "",
        f"- iteration_id: `{context.get('iteration_id', '')}`",
        f"- experiment_id: `{context.get('experiment_id', '')}`",
        "",
        "| Artifact | Path | Status | Note |",
        "|----------|------|--------|------|",
        "| handoff.json | `{path}` | {status} | safe_resume={safe_resume} |".format(
            path=_docs_relpath(repo_root, context.get("handoff_path")),
            status=_status_from_error(handoff, str(context.get("handoff_error", ""))),
            safe_resume=_docs_markdown_escape(
                str(handoff.get("safe_resume_point", {}).get("status", ""))
                if isinstance(handoff.get("safe_resume_point"), dict)
                else ""
            )
            or "n/a",
        ),
        "| handoff.md | `{path}` | {status} | human handoff snapshot |".format(
            path=_docs_relpath(repo_root, handoff_md_path),
            status=handoff_md_status,
        ),
        "| traceability_latest.json | `{path}` | {status} | iteration={iteration} |".format(
            path=_docs_relpath(repo_root, context.get("trace_latest_path")),
            status=_status_from_error(
                trace_latest, str(context.get("trace_latest_error", ""))
            ),
            iteration=_docs_markdown_escape(str(trace_latest.get("iteration_id", "")))
            or "n/a",
        ),
        "| traceability_coverage.json | `{path}` | {status} | rows_total={rows_total} |".format(
            path=_docs_relpath(repo_root, context.get("traceability_path")),
            status=_status_from_error(
                traceability_payload,
                str(context.get("traceability_error", "")),
            ),
            rows_total=_docs_safe_int(trace_summary.get("rows_total", 0), 0),
        ),
        "| context bundle | `{path}` | {status} | focus_iteration={iteration} |".format(
            path=_docs_relpath(repo_root, context.get("context_bundle_path")),
            status=_status_from_error(
                bundle,
                str(context.get("context_bundle_error", "")),
            ),
            iteration=_docs_markdown_escape(str(bundle.get("focus_iteration_id", "")))
            or "n/a",
        ),
        "| project_map.json | `{path}` | {status} | scan_mode={scan_mode} |".format(
            path=_docs_relpath(repo_root, context.get("project_map_path")),
            status=_status_from_error(
                project_map,
                str(context.get("project_map_error", "")),
            ),
            scan_mode=_docs_markdown_escape(str(project_map.get("scan_mode", "")))
            or "n/a",
        ),
        "| context_delta.json | `{path}` | {status} | iteration={iteration} |".format(
            path=_docs_relpath(repo_root, context.get("context_delta_path")),
            status=_status_from_error(
                context_delta,
                str(context.get("context_delta_error", "")),
            ),
            iteration=_docs_markdown_escape(str(context_delta.get("iteration_id", "")))
            or "n/a",
        ),
        "| plan_execution_state.json | `{path}` | {status} | current_wave={current_wave} |".format(
            path=_docs_relpath(repo_root, context.get("plan_execution_state_path")),
            status=_status_from_error(
                plan_execution_state_payload,
                str(context.get("plan_execution_state_error", "")),
            ),
            current_wave=_docs_markdown_escape(
                str(plan_execution_state_payload.get("current_wave", ""))
            )
            or "n/a",
        ),
        "| plan_execution_summary.json | `{path}` | {status} | critical_path={critical_path} |".format(
            path=_docs_relpath(repo_root, context.get("plan_execution_summary_path")),
            status=_status_from_error(
                plan_execution_summary_payload,
                str(context.get("plan_execution_summary_error", "")),
            ),
            critical_path=_docs_markdown_escape(
                str(critical_path.get("mode", "unavailable"))
            )
            or "n/a",
        ),
        "| plan_graph.json | `{path}` | {status} | waves={waves} |".format(
            path=_docs_relpath(repo_root, context.get("plan_graph_path")),
            status=_status_from_error(
                plan_graph_payload,
                str(context.get("plan_graph_error", "")),
            ),
            waves=_docs_markdown_escape(
                str(
                    len(plan_graph_payload.get("waves", []))
                    if isinstance(plan_graph_payload.get("waves"), list)
                    else 0
                )
            )
            or "n/a",
        ),
        "| plan_check_result.json | `{path}` | {status} | errors={errors} |".format(
            path=_docs_relpath(repo_root, context.get("plan_check_result_path")),
            status=_status_from_error(
                plan_check_result_payload,
                str(context.get("plan_check_result_error", "")),
            ),
            errors=_docs_markdown_escape(
                str(
                    len(plan_check_result_payload.get("errors", []))
                    if isinstance(plan_check_result_payload.get("errors"), list)
                    else 0
                )
            )
            or "n/a",
        ),
        "",
        "## Diagnostics",
    ]
    diagnostics: list[str] = []
    for key in (
        "handoff_error",
        "trace_latest_error",
        "traceability_error",
        "traceability_selection_error",
        "context_bundle_error",
        "project_map_error",
        "context_delta_error",
        "plan_execution_state_error",
        "plan_execution_summary_error",
        "plan_graph_error",
        "plan_check_result_error",
    ):
        value = str(context.get(key, "")).strip()
        if value:
            diagnostics.append(value)
    handoff_context_errors = context.get("handoff_context_errors", [])
    if isinstance(handoff_context_errors, list):
        for message in handoff_context_errors:
            message_text = str(message).strip()
            if message_text:
                diagnostics.append(message_text)

    focus_iteration_id = str(bundle.get("focus_iteration_id", "")).strip()
    target_iteration_id = str(context.get("iteration_id", "")).strip()
    if (
        focus_iteration_id
        and target_iteration_id
        and focus_iteration_id != target_iteration_id
    ):
        diagnostics.append(
            "context bundle focus_iteration_id differs from requested iteration_id "
            f"({focus_iteration_id} != {target_iteration_id})"
        )
    delta_iteration_id = str(context_delta.get("iteration_id", "")).strip()
    if (
        delta_iteration_id
        and target_iteration_id
        and delta_iteration_id != target_iteration_id
    ):
        diagnostics.append(
            "context delta iteration_id differs from requested iteration_id "
            f"({delta_iteration_id} != {target_iteration_id})"
        )

    latest_pointer, latest_pointer_error = _docs_resolve_pointer_path(
        repo_root,
        trace_latest.get("traceability_path", ""),
    )
    if latest_pointer_error and latest_pointer_error not in diagnostics:
        diagnostics.append(latest_pointer_error)
    coverage_path = context.get("traceability_path")
    if isinstance(latest_pointer, Path) and isinstance(coverage_path, Path):
        try:
            latest_pointer_resolved = latest_pointer.resolve(strict=False)
            coverage_path_resolved = coverage_path.resolve(strict=False)
        except Exception:
            latest_pointer_resolved = latest_pointer
            coverage_path_resolved = coverage_path
        if latest_pointer_resolved != coverage_path_resolved:
            mismatch_message = "traceability_latest.traceability_path differs from selected coverage path"
            if mismatch_message not in diagnostics:
                diagnostics.append(mismatch_message)

    if diagnostics:
        lines.extend(f"- {item}" for item in diagnostics)
    else:
        lines.append("- none")
    _docs_append_wave_observability_sections(
        lines,
        context=context,
        include_task_evidence=False,
    )
    return "\n".join(lines).rstrip() + "\n"


def _cmd_docs_generate(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    iteration_override = str(getattr(args, "iteration_id", "") or "").strip()
    selected_views = _docs_select_views(
        str(getattr(args, "view", "registry") or "registry")
    )
    context, context_error = _docs_collect_context(
        state_path=state_path,
        iteration_override=iteration_override,
    )
    if context is None:
        if selected_views == ["registry"] and _docs_is_state_context_error(
            context_error
        ):
            context = _docs_collect_registry_fallback_context(
                state_path=state_path,
                iteration_override=iteration_override,
                state_error=context_error,
            )
        else:
            print(f"autolab docs generate: ERROR {context_error}", file=sys.stderr)
            return 1

    rendered_by_view: dict[str, str] = {}
    for view in selected_views:
        if view == "registry":
            registry = load_registry(repo_root)
            if not registry:
                print(
                    "autolab docs generate: ERROR could not load workflow.yaml registry",
                    file=sys.stderr,
                )
                return 1
            rendered_by_view[view] = _render_docs_registry_view(
                context,
                registry=registry,
            )
            continue
        if view == "project":
            rendered_by_view[view] = _render_docs_project_view(context)
            continue
        if view == "roadmap":
            rendered_by_view[view] = _render_docs_roadmap_view(context)
            continue
        if view == "state":
            rendered_by_view[view] = _render_docs_state_view(context)
            continue
        if view == "requirements":
            rendered_by_view[view] = _render_docs_requirements_view(context)
            continue
        if view == "sidecar":
            rendered_by_view[view] = _render_docs_sidecar_view(context)
            continue
        print(
            f"autolab docs generate: ERROR unsupported view '{view}'", file=sys.stderr
        )
        return 1

    output_dir_text = str(getattr(args, "output_dir", "") or "").strip()
    if output_dir_text:
        try:
            requested_output_dir = Path(output_dir_text).expanduser()
            output_dir = (
                requested_output_dir.resolve(strict=False)
                if requested_output_dir.is_absolute()
                else (repo_root / requested_output_dir).resolve(strict=False)
            )
        except Exception as exc:
            print(
                f"autolab docs generate: ERROR invalid output-dir '{output_dir_text}': {exc}",
                file=sys.stderr,
            )
            return 1
        if not _docs_path_within_repo_root(repo_root, output_dir):
            print(
                "autolab docs generate: ERROR output-dir resolves outside repository "
                f"root: {output_dir}",
                file=sys.stderr,
            )
            return 1
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            if not output_dir.is_dir():
                raise RuntimeError("resolved output-dir is not a directory")
            written_paths: list[Path] = []
            for view in selected_views:
                output_path = (output_dir / f"{view}.md").resolve(strict=False)
                if not _docs_path_within_repo_root(repo_root, output_path):
                    raise RuntimeError(
                        f"resolved output path escapes repository root: {output_path}"
                    )
                output_path.write_text(rendered_by_view[view], encoding="utf-8")
                written_paths.append(output_path)
        except Exception as exc:
            print(
                "autolab docs generate: ERROR failed writing docs output to "
                f"{output_dir}: {exc}",
                file=sys.stderr,
            )
            return 1
        print("autolab docs generate")
        print(f"state_file: {state_path}")
        print(f"iteration_id: {context.get('iteration_id', '')}")
        print(f"views_written: {len(written_paths)}")
        for output_path in written_paths:
            print(f"- {output_path}")
        return 0

    for index, view in enumerate(selected_views):
        if index > 0:
            print("")
        sys.stdout.write(rendered_by_view[view].rstrip() + "\n")
    return 0


# ---------------------------------------------------------------------------
# Issue report command
# ---------------------------------------------------------------------------


def _truncate_issue_context(text: str, *, max_chars: int = 20000) -> str:
    normalized = str(text).strip()
    if len(normalized) <= max_chars:
        return normalized
    return f"...\n{normalized[-max_chars:]}"


def _tail_issue_log(path: Path, *, max_lines: int, max_chars: int = 20000) -> str:
    if max_lines <= 0 or not path.exists():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    if not lines:
        return ""
    tail = "\n".join(lines[-max_lines:])
    return _truncate_issue_context(tail, max_chars=max_chars)


def _autolab_version_text() -> str:
    try:
        return str(importlib_metadata.version("autolab")).strip()
    except Exception:
        return "unknown"


def _resolve_issue_report_agent_invocation(
    repo_root: Path,
) -> tuple[list[str], dict[str, str], str]:
    override = str(os.environ.get("AUTOLAB_REPORT_AGENT_COMMAND", "")).strip()
    if override:
        try:
            parsed = shlex.split(override)
        except ValueError as exc:
            raise RuntimeError(
                f"AUTOLAB_REPORT_AGENT_COMMAND could not be parsed: {exc}"
            ) from exc
        if not parsed:
            raise RuntimeError("AUTOLAB_REPORT_AGENT_COMMAND is empty")
        return (parsed, dict(os.environ), override)

    if shutil.which("claude"):
        env = dict(os.environ)
        env.pop("CLAUDECODE", None)
        argv = [
            "claude",
            "-p",
            "--permission-mode",
            "plan",
            "--output-format",
            "text",
            "-",
        ]
        display = " ".join(shlex.quote(token) for token in argv)
        return (argv, env, display)

    if shutil.which("codex"):
        argv = [
            "codex",
            "exec",
            "--sandbox",
            "read-only",
            "-C",
            str(repo_root),
            "-",
        ]
        display = " ".join(shlex.quote(token) for token in argv)
        return (argv, dict(os.environ), display)

    raise RuntimeError(
        "no supported local LLM CLI found; install 'claude' or 'codex', or set AUTOLAB_REPORT_AGENT_COMMAND"
    )


def _run_issue_report_agent(
    repo_root: Path,
    *,
    prompt_text: str,
    timeout_seconds: float,
) -> tuple[int, str, str, str]:
    command_argv, command_env, command_display = _resolve_issue_report_agent_invocation(
        repo_root
    )
    try:
        process = subprocess.run(
            command_argv,
            cwd=repo_root,
            input=prompt_text,
            text=True,
            capture_output=True,
            env=command_env,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        return (127, "", str(exc), command_display)
    except subprocess.TimeoutExpired as exc:
        return (
            124,
            str(getattr(exc, "stdout", "") or "").strip(),
            f"timed out after {timeout_seconds:.0f}s",
            command_display,
        )
    except Exception as exc:
        return (1, "", str(exc), command_display)

    return (
        int(process.returncode),
        str(process.stdout or "").strip(),
        str(process.stderr or "").strip(),
        command_display,
    )


def _build_issue_report_prompt(
    *,
    user_comment: str,
    state_json: str,
    verification_json: str,
    orchestrator_log_tail: str,
    log_tail_lines: int,
) -> str:
    comment_block = user_comment.strip() or "None provided."
    return "\n".join(
        [
            "You are an Autolab maintainer assistant.",
            "Analyze the provided runtime evidence and produce a concise, developer-facing issue report.",
            "Do not invent facts. Use only the provided evidence.",
            "",
            "Return Markdown with these sections in order:",
            "## Summary",
            "## User Comment",
            "## Evidence",
            "## Likely Root Cause",
            "## Recommendations",
            "",
            "Constraints:",
            "- Keep the report actionable and specific.",
            "- If evidence is insufficient, say exactly what is missing.",
            "- Do not include instructions that require modifying user files right now.",
            "",
            f"User comment:\n{comment_block}",
            "",
            "State snapshot (JSON):",
            "```json",
            state_json.strip() or "{}",
            "```",
            "",
            "Latest verification result (JSON, optional):",
            "```json",
            verification_json.strip() or "null",
            "```",
            "",
            f"orchestrator.log tail (last {log_tail_lines} lines):",
            "```text",
            orchestrator_log_tail.strip() or "<orchestrator.log missing or empty>",
            "```",
            "",
            "Now produce the issue report.",
        ]
    )


def _build_issue_report_document(
    *,
    generated_at_utc: str,
    user_comment: str,
    state_json: str,
    verification_json: str,
    orchestrator_log_tail: str,
    log_tail_lines: int,
    command_display: str,
    analysis_markdown: str,
    analysis_error: str,
) -> str:
    comment_block = user_comment.strip() or "_None provided._"
    analysis_block = analysis_markdown.strip()
    if not analysis_block:
        failure_detail = analysis_error.strip() or "agent returned no output"
        analysis_block = "\n".join(
            [
                "## Summary",
                "Automated issue analysis could not complete.",
                "",
                "## User Comment",
                comment_block,
                "",
                "## Evidence",
                "- LLM agent invocation failed.",
                "",
                "## Likely Root Cause",
                f"- {failure_detail}",
                "",
                "## Recommendations",
                "- Review the captured context snapshot below and retry the report command.",
                "- If the failure persists, set AUTOLAB_REPORT_AGENT_COMMAND to a known-good LLM CLI command.",
            ]
        )

    lines = [
        "# Autolab Issue Report",
        "",
        f"- generated_at_utc: `{generated_at_utc}`",
        f"- host: `{socket.gethostname()}`",
        f"- platform: `{platform.platform()}`",
        f"- autolab_version: `{_autolab_version_text()}`",
        f"- llm_command: `{command_display or '<unresolved>'}`",
        "",
        analysis_block,
        "",
        "## Context Snapshot",
        "",
        "### User Comment (raw)",
        comment_block,
        "",
        "### State (raw JSON)",
        "```json",
        state_json.strip() or "{}",
        "```",
        "",
        "### Verification Result (raw JSON)",
        "```json",
        verification_json.strip() or "null",
        "```",
        "",
        f"### orchestrator.log tail (last {log_tail_lines} lines)",
        "```text",
        orchestrator_log_tail.strip() or "<orchestrator.log missing or empty>",
        "```",
    ]
    if analysis_error.strip():
        lines.extend(
            [
                "",
                "### Agent Error",
                "```text",
                analysis_error.strip(),
                "```",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _cmd_report(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    if not autolab_dir.exists():
        print(
            f"autolab report: ERROR .autolab directory not found at {autolab_dir}",
            file=sys.stderr,
        )
        return 1

    try:
        log_tail_lines = int(args.log_tail)
    except Exception:
        print("autolab report: ERROR --log-tail must be an integer", file=sys.stderr)
        return 1
    if log_tail_lines <= 0:
        print("autolab report: ERROR --log-tail must be > 0", file=sys.stderr)
        return 1

    try:
        timeout_seconds = float(args.timeout_seconds)
    except Exception:
        print(
            "autolab report: ERROR --timeout-seconds must be a number",
            file=sys.stderr,
        )
        return 1
    if timeout_seconds <= 0:
        print("autolab report: ERROR --timeout-seconds must be > 0", file=sys.stderr)
        return 1

    user_comment = str(args.comment or "").strip()
    state_payload = _load_json_if_exists(state_path)
    verification_payload = _load_json_if_exists(
        autolab_dir / "verification_result.json"
    )
    state_json = _truncate_issue_context(
        json.dumps(state_payload, indent=2, sort_keys=True)
        if state_payload is not None
        else "{}"
    )
    verification_json = _truncate_issue_context(
        json.dumps(verification_payload, indent=2, sort_keys=True)
        if verification_payload is not None
        else "null"
    )
    orchestrator_log_path = autolab_dir / "logs" / "orchestrator.log"
    orchestrator_log_tail = _tail_issue_log(
        orchestrator_log_path,
        max_lines=log_tail_lines,
    )

    prompt_text = _build_issue_report_prompt(
        user_comment=user_comment,
        state_json=state_json,
        verification_json=verification_json,
        orchestrator_log_tail=orchestrator_log_tail,
        log_tail_lines=log_tail_lines,
    )
    (
        agent_returncode,
        agent_stdout,
        agent_stderr,
        command_display,
    ) = _run_issue_report_agent(
        repo_root,
        prompt_text=prompt_text,
        timeout_seconds=timeout_seconds,
    )

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if args.output:
        output_path = Path(args.output).expanduser().resolve()
    else:
        output_path = autolab_dir / "logs" / f"issue_report_{timestamp}.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    analysis_error = ""
    if agent_returncode != 0:
        if agent_stderr:
            analysis_error = (
                f"agent exited with code {agent_returncode}: {agent_stderr.strip()}"
            )
        else:
            analysis_error = f"agent exited with code {agent_returncode}"

    report_text = _build_issue_report_document(
        generated_at_utc=_utc_now(),
        user_comment=user_comment,
        state_json=state_json,
        verification_json=verification_json,
        orchestrator_log_tail=orchestrator_log_tail,
        log_tail_lines=log_tail_lines,
        command_display=command_display,
        analysis_markdown=agent_stdout,
        analysis_error=analysis_error,
    )
    output_path.write_text(report_text, encoding="utf-8")
    print(f"autolab report: wrote {output_path}")

    if agent_returncode != 0:
        print(
            "autolab report: WARN agent analysis failed; wrote fallback report with captured context",
            file=sys.stderr,
        )
        return 1
    return 0


__all__ = [name for name in globals() if not name.startswith("__")]
