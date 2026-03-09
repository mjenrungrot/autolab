"""Policy explain/docs/report command handlers."""

from __future__ import annotations

from functools import lru_cache
import math
import re

from autolab.campaign import (
    _append_campaign_oracle_feedback,
    _campaign_build_morning_report_payload,
    _campaign_morning_report_path,
    _campaign_render_morning_report,
    _campaign_summary,
    _load_campaign,
    _mark_campaign_oracle_exported,
    _refresh_campaign_results,
    _write_campaign,
)
from autolab.cli.support import *
from autolab.cli.handlers_observe import _safe_refresh_handoff
from autolab.agent_surface import (
    build_agent_surface_guidance,
    infer_agent_surface_provider,
    resolve_agent_surface,
)
from autolab.oracle_runtime import (
    ORACLE_ALLOWED_VERDICTS,
    build_oracle_roundtrip_request,
    finish_oracle_attempt,
    load_oracle_state,
    oracle_default_suggested_next_action,
    oracle_last_response_path,
    oracle_profile_ready,
    oracle_stage_auto_allowed,
    parse_oracle_reply,
    start_oracle_attempt,
    write_oracle_last_response,
    write_oracle_state,
)
from autolab.plan_approval import (
    append_plan_approval_note,
    load_plan_approval,
    resolve_plan_approval_state,
)
from autolab.sidecar_context import resolve_context_sidecars
from autolab.sidecar_tools import (
    DISCUSS_COLLECTIONS,
    RESEARCH_COLLECTIONS,
    SIDECAR_COLLECTIONS_BY_KIND,
    build_sidecar_dependency_refs,
    build_sidecar_markdown,
    parse_context_ref,
    resolve_sidecar_output_paths,
)
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
    for preset_name in POLICY_PRESET_NAMES:
        path = policy_dir / f"{preset_name}.yaml"
        if not path.exists():
            continue
        details = policy_preset_details(preset_name)
        summary = str(details.get("summary", "")).strip()
        if summary:
            print(f"  {preset_name} - {summary}")
        else:
            print(f"  {preset_name}")
    return 0


def _cmd_policy_show(args: argparse.Namespace) -> int:
    effective_flag = getattr(args, "effective", False)
    preset_name = getattr(args, "preset", None)
    json_flag = getattr(args, "json", False)

    if effective_flag:
        # Show computed effective policy
        state_path = Path(".autolab/state.json").expanduser().resolve()
        try:
            repo_root = _resolve_repo_root(state_path)
        except Exception:
            repo_root = Path.cwd()
        from autolab.config import _load_effective_policy
        from autolab.policy_resolution import build_effective_artifact

        result = _load_effective_policy(
            repo_root,
            host_mode=getattr(args, "host", "") or "",
            scope_kind=getattr(args, "scope", "") or "",
            stage=getattr(args, "stage", "") or "",
        )
        if json_flag:
            artifact = build_effective_artifact(
                merged=result.merged,
                sources=[
                    (s.layer, s.name, list(s.keys_contributed)) for s in result.sources
                ],
                preset=result.preset,
                host_mode=result.host_mode,
                scope_kind=result.scope_kind,
                stage=result.stage,
                risk_flags=result.risk_flags,
                generated_at=_utc_now(),
            )
            _write_effective_policy_artifact(repo_root, artifact)
            print(json.dumps(artifact, indent=2))
        else:
            print("autolab policy show --effective")
            print(f"- Preset: {result.preset or '(none)'}")
            preset_details = policy_preset_details(result.preset)
            if preset_details:
                print(f"- Preset Summary: {preset_details.get('summary', '')}")
            print(
                f"- Host: {result.host_mode} | Scope: {result.scope_kind} | Profile: {result.profile_mode}"
            )
            risk_active = [k for k, v in result.risk_flags.items() if v]
            print(
                f"- Risk: {', '.join(risk_active) if risk_active else '(none active)'}"
            )
            print(f"- Sources: {len(result.sources)} layer(s) contributed")
            for source in result.sources:
                print(
                    f"    [{source.layer}] keys: {', '.join(source.keys_contributed)}"
                )
            print("---")
            if _yaml_mod is not None:
                print(_yaml_mod.dump(result.merged, default_flow_style=False).rstrip())
            else:
                print(json.dumps(result.merged, indent=2))
        return 0

    if preset_name:
        # Existing behavior: show raw preset YAML
        preset_name = str(preset_name).strip()
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
        preset_details = policy_preset_details(preset_name)
        if preset_details:
            print(f"summary: {preset_details.get('summary', '')}")
            print(f"recommended_mode: {preset_details.get('recommended_mode', '')}")
            print(
                "recommended_campaign_lock: "
                f"{preset_details.get('recommended_campaign_lock', 'none')}"
            )
        print("---")
        print(preset_path.read_text(encoding="utf-8").rstrip())
        return 0

    # No preset, no --effective: show current verifier_policy.yaml
    state_path = Path(".autolab/state.json").expanduser().resolve()
    try:
        repo_root = _resolve_repo_root(state_path)
    except Exception:
        repo_root = Path.cwd()
    policy_path = repo_root / ".autolab" / "verifier_policy.yaml"
    if not policy_path.exists():
        print("autolab policy show: no verifier_policy.yaml found", file=sys.stderr)
        return 1
    print("autolab policy show")
    print(f"file: {policy_path}")
    print("---")
    print(policy_path.read_text(encoding="utf-8").rstrip())
    return 0


def _write_effective_policy_artifact(repo_root: Path, artifact: dict[str, Any]) -> Path:
    """Write effective_policy.json artifact and return its path."""
    out_path = repo_root / ".autolab" / "effective_policy.json"
    _write_json(out_path, artifact)
    return out_path


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

    # --explain: show effective policy resolution chain
    if getattr(args, "explain", False):
        from autolab.config import _load_effective_policy

        try:
            result = _load_effective_policy(repo_root)
        except Exception as exc:
            print(
                f"autolab policy doctor: ERROR effective policy resolution failed: {exc}",
                file=sys.stderr,
            )
        else:
            print("effective policy resolution:")
            print(f"  preset: {result.preset or '(none)'}")
            print(f"  host_mode: {result.host_mode}")
            print(f"  scope_kind: {result.scope_kind}")
            print(f"  profile_mode: {result.profile_mode}")
            print(f"  stage: {result.stage or '(none)'}")
            print("")
            print("  resolution chain:")
            layer_names = [
                "scaffold_default",
                "preset",
                "host",
                "scope",
                "stage",
                "risk",
                "repo_local",
            ]
            source_by_layer = {s.layer: s for s in result.sources}
            for layer in layer_names:
                source = source_by_layer.get(layer)
                if source:
                    print(
                        f"    {layer}: contributed {', '.join(source.keys_contributed)}"
                    )
                else:
                    print(f"    {layer}: (no changes)")
            print("")
            print("  risk flags:")
            for flag, active in result.risk_flags.items():
                status = "ACTIVE" if active else "inactive"
                print(f"    {flag}: {status}")
            print("")

    return 1 if issues else 0


def _cmd_remote_show(args: argparse.Namespace) -> int:
    from autolab.remote_profiles import (
        ensure_remote_launch_revision,
        load_remote_profiles,
        normalize_host_mode,
        resolve_remote_profile,
        resolve_workspace_revision,
    )
    from autolab.utils import _detect_host_mode_with_probe

    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    try:
        host_mode, _probe = _detect_host_mode_with_probe()
        normalized_host_mode = normalize_host_mode(host_mode)
        config = load_remote_profiles(repo_root)
        profile = resolve_remote_profile(
            repo_root,
            host_mode=normalized_host_mode,
            profile_name=str(getattr(args, "profile", "")).strip(),
        )
    except Exception as exc:
        print(f"autolab remote show: ERROR {exc}", file=sys.stderr)
        return 1

    revision = resolve_workspace_revision(repo_root)
    print("autolab remote show")
    print(f"- config: {config.path}")
    print(f"- host_mode: {normalized_host_mode or '(unknown)'}")
    print(f"- profile: {profile.name}")
    print(f"- mode: {profile.mode}")
    print(
        f"- host_modes: {', '.join(profile.enabled_for_host_modes) if profile.enabled_for_host_modes else '(any)'}"
    )
    print(f"- login_host: {profile.login_host or '(none)'}")
    print(f"- remote_repo_root: {profile.remote_repo_root or '(none)'}")
    print(f"- python_path: {profile.python_path}")
    print(f"- bootstrap_command: {profile.bootstrap_command or '(none)'}")
    print(f"- submit_command: {profile.submit_command}")
    print(
        f"- revision: label={revision.label or '(missing)'} source={revision.source} dirty={str(revision.dirty).lower()}"
    )
    if profile.mode != "shared_fs":
        try:
            ensure_remote_launch_revision(repo_root, profile)
            print("- revision_ready: yes")
        except Exception as exc:
            print(f"- revision_ready: no ({exc})")
    else:
        print("- revision_ready: n/a (shared_fs)")
    print(
        f"- artifact_pull: enabled={str(profile.artifact_pull.enabled).lower()} max_file_size_mb={profile.artifact_pull.max_file_size_mb}"
    )
    for pattern in getattr(profile.artifact_pull, "allow_patterns", ()):
        print(f"    allow: {pattern}")
    for pattern in getattr(profile.data_policy, "deny_patterns", ()):
        print(f"    deny: {pattern}")
    if profile.smoke_command:
        print(f"- smoke_command: {profile.smoke_command}")
    return 0


def _cmd_remote_doctor(args: argparse.Namespace) -> int:
    from autolab.remote_profiles import (
        ensure_remote_launch_revision,
        lint_remote_profile,
        normalize_host_mode,
        resolve_remote_profile,
    )
    from autolab.utils import _detect_host_mode_with_probe, _is_command_available

    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    issues: list[str] = []
    try:
        host_mode, probe = _detect_host_mode_with_probe()
        profile = resolve_remote_profile(
            repo_root,
            host_mode=normalize_host_mode(host_mode),
            profile_name=str(getattr(args, "profile", "")).strip(),
        )
    except Exception as exc:
        print(f"autolab remote doctor: ERROR {exc}", file=sys.stderr)
        return 1

    for command in profile.host_detection.require_commands:
        if not _is_command_available(command):
            issues.append(f"required host command missing: {command}")
    if not _is_command_available("ssh"):
        issues.append("required command missing: ssh")
    if profile.mode != "shared_fs":
        try:
            ensure_remote_launch_revision(repo_root, profile)
        except Exception as exc:
            issues.append(str(exc))
    issues.extend(lint_remote_profile(profile))

    print("autolab remote doctor")
    print(f"- host_mode: {normalize_host_mode(host_mode)}")
    for key, value in sorted(probe.items()):
        print(f"  probe.{key}: {value}")
    print(f"- profile: {profile.name}")
    print(f"- mode: {profile.mode}")
    if issues:
        print("issues:")
        for issue in issues:
            print(f"  - {issue}")
        print("status: fail")
        return 1
    print("status: ok")
    return 0


def _cmd_remote_smoke(args: argparse.Namespace) -> int:
    from autolab.remote_profiles import (
        ensure_remote_python,
        normalize_host_mode,
        resolve_remote_profile,
        run_remote_command,
    )
    from autolab.utils import _detect_host_mode_with_probe

    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    try:
        host_mode, _probe = _detect_host_mode_with_probe()
        profile = resolve_remote_profile(
            repo_root,
            host_mode=normalize_host_mode(host_mode),
            profile_name=str(getattr(args, "profile", "")).strip(),
        )
    except Exception as exc:
        print(f"autolab remote smoke: ERROR {exc}", file=sys.stderr)
        return 1

    print("autolab remote smoke")
    print(f"- profile: {profile.name}")
    try:
        reachable = run_remote_command(
            profile, "printf remote-ok", timeout_seconds=15.0
        )
        if reachable.returncode != 0:
            raise RuntimeError(str(reachable.stderr or "").strip() or "ssh failed")
        print("- reachable: ok")

        repo_check = run_remote_command(
            profile,
            "git rev-parse --is-inside-work-tree",
            cwd=profile.remote_repo_root,
            timeout_seconds=15.0,
        )
        if repo_check.returncode != 0:
            raise RuntimeError("remote repo root is not a git checkout")
        print("- remote_repo: ok")

        for command in profile.host_detection.require_commands:
            command_check = run_remote_command(
                profile, f"command -v {command}", timeout_seconds=15.0
            )
            if command_check.returncode != 0:
                raise RuntimeError(f"remote command missing: {command}")
        if profile.host_detection.require_commands:
            print("- required_commands: ok")

        ensure_remote_python(profile, timeout_seconds=120.0)
        print("- python: ok")

        if profile.smoke_command:
            smoke = run_remote_command(
                profile,
                profile.smoke_command,
                cwd=profile.remote_repo_root,
                timeout_seconds=120.0,
            )
            if smoke.returncode != 0:
                raise RuntimeError(str(smoke.stderr or "").strip() or "smoke failed")
            print("- smoke_command: ok")
        else:
            print("- smoke_command: skipped (not configured)")
    except Exception as exc:
        print(f"status: fail ({exc})", file=sys.stderr)
        return 1

    print("status: ok")
    return 0


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
        handoff_stage = str(handoff_payload.get("current_stage", "")).strip()
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
        if handoff_stage and handoff_stage != str(state.get("stage", "")).strip():
            handoff_context_errors.append(
                "handoff current_stage differs from state stage "
                f"({handoff_stage} != {str(state.get('stage', '')).strip()})"
            )
    if handoff_context_errors:
        handoff_error = _docs_append_error(
            handoff_error,
            "; ".join(handoff_context_errors),
        )
        handoff_payload = {}

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

    plan_approval_path = (
        iteration_dir / "plan_approval.json" if iteration_dir is not None else None
    )
    plan_approval_payload = None
    plan_approval_error = "plan approval path is unavailable"
    if plan_approval_path is not None:
        plan_approval_payload, plan_approval_error = _docs_load_json_mapping(
            repo_root,
            plan_approval_path,
        )
        if str(state.get("stage", "")).strip() == "implementation":
            (
                effective_plan_approval,
                effective_plan_approval_error,
                _effective_plan_approval_action_mode,
            ) = resolve_plan_approval_state(repo_root, iteration_dir)
            if effective_plan_approval:
                plan_approval_payload = effective_plan_approval
            if effective_plan_approval_error:
                plan_approval_error = effective_plan_approval_error

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

    (
        plan_approval_payload,
        stale_plan_approval_error,
    ) = _docs_validate_iteration_scoped_observability_payload(
        artifact_name="plan_approval.json",
        payload=plan_approval_payload,
        iteration_id=iteration_id,
    )
    if stale_plan_approval_error:
        plan_approval_error = stale_plan_approval_error
        observability_context_diagnostics.append(stale_plan_approval_error)

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
            "plan_approval_path": plan_approval_path,
            "plan_approval_payload": plan_approval_payload,
            "plan_approval_error": plan_approval_error,
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
    plan_approval = context.get("plan_approval_payload")
    if not isinstance(plan_approval, dict):
        plan_approval = {}

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
    if plan_approval:
        counts = plan_approval.get("counts")
        if not isinstance(counts, dict):
            counts = {}
        trigger_reasons = [
            str(item).strip()
            for item in plan_approval.get("trigger_reasons", [])
            if str(item).strip()
        ]
        lines.extend(
            [
                "",
                "## Plan Approval",
                f"- status: `{plan_approval.get('status', '')}`",
                f"- requires_approval: `{bool(plan_approval.get('requires_approval', False))}`",
                (
                    "- counts: "
                    f"tasks={_docs_safe_int(counts.get('tasks_total', 0), 0)}, "
                    f"waves={_docs_safe_int(counts.get('waves_total', 0), 0)}, "
                    f"project_wide_tasks={_docs_safe_int(counts.get('project_wide_tasks', 0), 0)}, "
                    f"project_wide_paths={_docs_safe_int(counts.get('project_wide_unique_paths', 0), 0)}, "
                    f"retries={_docs_safe_int(counts.get('observed_retries', 0), 0)}"
                ),
                (
                    "- trigger_reasons: "
                    + (", ".join(trigger_reasons) if trigger_reasons else "none")
                ),
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
        "plan_approval_error",
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
    continuation_packet = handoff_payload.get("continuation_packet")
    if not isinstance(continuation_packet, dict):
        continuation_packet = {}
    next_action = continuation_packet.get("next_action")
    if not isinstance(next_action, dict):
        next_action = {}
    top_blockers = continuation_packet.get("top_blockers")
    if not isinstance(top_blockers, list):
        top_blockers = []
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
    plan_approval = context.get("plan_approval_payload")
    if not isinstance(plan_approval, dict):
        plan_approval = {}
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
        f"- safe_resume_status: `{next_action.get('safe_status', safe_resume.get('status', 'blocked'))}`",
        f"- safe_resume_command: `{next_action.get('safe_command', safe_resume.get('command', ''))}`",
        f"- recommended_next_command: `{next_action.get('recommended_command', recommended.get('command', ''))}`",
        f"- blockers: {len(top_blockers) if top_blockers else len(blocking_failures)}",
        f"- pending_human_decisions: {len(pending_decisions)}",
    ]
    if top_blockers:
        lines.append(
            "- top_blockers: "
            + ", ".join(str(item).strip() for item in top_blockers if str(item).strip())
        )
    if plan_approval:
        counts = plan_approval.get("counts")
        if not isinstance(counts, dict):
            counts = {}
        trigger_reasons = [
            str(item).strip()
            for item in plan_approval.get("trigger_reasons", [])
            if str(item).strip()
        ]
        lines.extend(
            [
                "",
                "## Plan Approval",
                f"- status: `{plan_approval.get('status', '')}`",
                f"- requires_approval: `{bool(plan_approval.get('requires_approval', False))}`",
                f"- plan_hash: `{plan_approval.get('plan_hash', '')}`",
                f"- risk_fingerprint: `{plan_approval.get('risk_fingerprint', '')}`",
                (
                    "- counts: "
                    f"tasks={_docs_safe_int(counts.get('tasks_total', 0), 0)}, "
                    f"waves={_docs_safe_int(counts.get('waves_total', 0), 0)}, "
                    f"project_wide_tasks={_docs_safe_int(counts.get('project_wide_tasks', 0), 0)}, "
                    f"project_wide_paths={_docs_safe_int(counts.get('project_wide_unique_paths', 0), 0)}, "
                    f"retries={_docs_safe_int(counts.get('observed_retries', 0), 0)}"
                ),
                (
                    "- trigger_reasons: "
                    + (", ".join(trigger_reasons) if trigger_reasons else "none")
                ),
            ]
        )
    lines.extend(
        [
            "",
            "## Wave and Task Status",
            f"- wave: status={wave_summary.get('status', wave.get('status', 'unavailable'))}, current={wave_summary.get('current', wave.get('current', '-'))}, executed={wave_summary.get('executed', wave.get('executed', 0))}, total={wave_summary.get('total', wave.get('total', 0))}",
            f"- tasks: status={task_summary.get('status', task_status.get('status', 'unavailable'))}, total={task_summary.get('total', task_status.get('total', 0))}, completed={task_summary.get('completed', task_status.get('completed', 0))}, failed={task_summary.get('failed', task_status.get('failed', 0))}, blocked={task_summary.get('blocked', task_status.get('blocked', 0))}, pending={task_summary.get('pending', task_status.get('pending', 0))}, skipped={task_summary.get('skipped', 0)}, deferred={task_summary.get('deferred', 0)}",
        ]
    )
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
        "plan_approval_error",
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
    continuation_packet = handoff.get("continuation_packet")
    if not isinstance(continuation_packet, dict):
        continuation_packet = {}
    next_action = continuation_packet.get("next_action")
    if not isinstance(next_action, dict):
        next_action = {}
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
                str(
                    next_action.get(
                        "safe_status",
                        handoff.get("safe_resume_point", {}).get("status", ""),
                    )
                )
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
        "| plan_approval.json | `{path}` | {status} | status={approval_status} |".format(
            path=_docs_relpath(repo_root, context.get("plan_approval_path")),
            status=_status_from_error(
                context.get("plan_approval_payload")
                if isinstance(context.get("plan_approval_payload"), dict)
                else {},
                str(context.get("plan_approval_error", "")),
            ),
            approval_status=_docs_markdown_escape(
                str(
                    context.get("plan_approval_payload", {}).get("status", "")
                    if isinstance(context.get("plan_approval_payload"), dict)
                    else ""
                )
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
            from autolab.gc import update_managed_docs_manifest

            update_managed_docs_manifest(
                repo_root,
                output_dir,
                written_paths=written_paths,
                iteration_id=str(context.get("iteration_id", "") or ""),
            )
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
# Discuss / research commands
# ---------------------------------------------------------------------------


def _truncate_issue_context(text: str, *, max_chars: int = 20000) -> str:
    normalized = str(text).strip()
    if len(normalized) <= max_chars:
        return normalized
    return f"...\n{normalized[-max_chars:]}"


def _resolve_sidecar_command_target(
    args: argparse.Namespace,
    *,
    command_name: str,
) -> tuple[Path, Path, dict[str, Any], dict[str, Any], dict[str, Any]]:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        raise StageCheckError(f"{command_name}: {exc}") from exc

    requested_scope_kind = str(getattr(args, "scope", "") or "").strip().lower()
    if requested_scope_kind not in {"project_wide", "experiment"}:
        raise StageCheckError(
            f"{command_name}: --scope must be one of ['experiment', 'project_wide']"
        )
    requested_iteration_id = str(getattr(args, "iteration_id", "") or "").strip()
    state_iteration_id = str(state.get("iteration_id", "")).strip()
    iteration_id = requested_iteration_id or state_iteration_id
    experiment_id = str(state.get("experiment_id", "")).strip()
    if requested_scope_kind == "experiment" and iteration_id:
        backlog_payload, _load_error = _load_backlog_yaml(
            repo_root / ".autolab" / "backlog.yaml"
        )
        if backlog_payload is not None:
            lookup_experiment_id = (
                experiment_id if iteration_id == state_iteration_id else ""
            )
            entry, resolve_error = _find_backlog_experiment_entry(
                backlog_payload,
                experiment_id=lookup_experiment_id,
                iteration_id=iteration_id,
            )
            if entry is not None:
                resolved_experiment_id = str(entry.get("id", "")).strip()
                if resolved_experiment_id:
                    experiment_id = resolved_experiment_id
            elif requested_iteration_id and resolve_error:
                raise StageCheckError(f"{command_name}: {resolve_error}")
    if requested_scope_kind == "experiment" and not iteration_id:
        raise StageCheckError(
            f"{command_name}: experiment scope requires --iteration-id or state.iteration_id"
        )
    if requested_scope_kind == "experiment" and not experiment_id:
        raise StageCheckError(
            f"{command_name}: experiment scope requires non-empty state.experiment_id"
        )
    context_resolution = resolve_context_sidecars(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        scope_kind=requested_scope_kind,
    )
    return (
        state_path,
        repo_root,
        state,
        {
            "scope_kind": requested_scope_kind,
            "iteration_id": iteration_id,
            "experiment_id": experiment_id,
            "context_resolution": context_resolution,
        },
        resolve_sidecar_output_paths(
            repo_root,
            scope_kind=requested_scope_kind,
            sidecar_kind="discuss",
            iteration_id=iteration_id,
            experiment_id=experiment_id,
        ),
    )


def _sidecar_relpath(repo_root: Path, path: Path) -> str:
    try:
        return (
            path.resolve(strict=False)
            .relative_to(repo_root.resolve(strict=False))
            .as_posix()
        )
    except Exception:
        return str(path)


def _slugify_sidecar_item_id(prefix: str, summary: str, index: int) -> str:
    raw = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(summary).strip())
    while "--" in raw:
        raw = raw.replace("--", "-")
    normalized = raw.strip("-")[:32] or f"{prefix}-{index + 1}"
    return f"{prefix}-{normalized}"


def _context_question_label(context_resolution: dict[str, Any]) -> str:
    components = context_resolution.get("components")
    if not isinstance(components, list):
        return "the active scope context"
    labels: list[str] = []
    for component in components:
        if not isinstance(component, dict) or not bool(component.get("selected")):
            continue
        artifact_kind = str(component.get("artifact_kind", "")).strip()
        path = str(component.get("path", "")).strip()
        if artifact_kind and path:
            labels.append(f"{artifact_kind} ({path})")
    if not labels:
        return "the active scope context"
    return ", ".join(labels[:4])


def _collection_status_for_discuss(collection_name: str) -> str:
    return {
        "locked_decisions": "locked",
        "preferences": "preferred",
        "constraints": "constraint",
        "open_questions": "unresolved",
        "promotion_candidates": "promote",
    }.get(collection_name, "")


def _normalize_discuss_entry(
    collection_name: str,
    raw_entry: Any,
    *,
    index: int,
    scope_kind: str,
) -> dict[str, Any] | None:
    expected_status = _collection_status_for_discuss(collection_name)
    if isinstance(raw_entry, str):
        parts = [part.strip() for part in raw_entry.split("|")]
        summary = parts[0] if parts else ""
        if not summary:
            return None
        entry: dict[str, Any] = {
            "id": _slugify_sidecar_item_id(collection_name[:3], summary, index),
            "summary": summary,
            "detail": parts[1] if len(parts) > 1 and parts[1] else summary,
            "status": expected_status,
        }
        if collection_name == "promotion_candidates":
            entry["requirement_hint"] = parts[1] if len(parts) > 1 else ""
            entry["rationale"] = parts[2] if len(parts) > 2 else summary
            target_scope_kind = (
                parts[3] if len(parts) > 3 and parts[3] else "project_wide"
            )
            if target_scope_kind not in {"experiment", "project_wide"}:
                target_scope_kind = "project_wide"
            entry["target_scope_kind"] = target_scope_kind
            entry["detail"] = parts[2] if len(parts) > 2 and parts[2] else summary
        return entry

    if not isinstance(raw_entry, dict):
        return None

    summary = str(raw_entry.get("summary", "")).strip()
    if not summary:
        return None
    entry = {
        "id": str(raw_entry.get("id", "")).strip()
        or _slugify_sidecar_item_id(collection_name[:3], summary, index),
        "summary": summary,
        "detail": str(raw_entry.get("detail", "")).strip() or summary,
        "status": expected_status,
    }
    if collection_name == "promotion_candidates":
        target_scope_kind = str(raw_entry.get("target_scope_kind", "")).strip()
        if target_scope_kind not in {"experiment", "project_wide"}:
            target_scope_kind = "project_wide"
        entry["target_scope_kind"] = target_scope_kind
        entry["requirement_hint"] = str(raw_entry.get("requirement_hint", "")).strip()
        entry["rationale"] = (
            str(raw_entry.get("rationale", "")).strip() or entry["detail"]
        )
    elif scope_kind == "project_wide":
        entry.pop("target_scope_kind", None)
    return entry


def _build_discuss_question_pack(
    *,
    scope_kind: str,
    scope_root: Path,
    iteration_id: str,
    experiment_id: str,
    context_resolution: dict[str, Any],
    existing_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    context_label = _context_question_label(context_resolution)
    questions: list[dict[str, Any]] = []
    existing = existing_payload if isinstance(existing_payload, dict) else {}
    prompts = {
        "locked_decisions": (
            f"Which decisions are already fixed for this {scope_kind} scope? Base them on direct user intent and {context_label}.",
            "summary | detail",
        ),
        "preferences": (
            "Which design or implementation preferences should guide later planning?",
            "summary | detail",
        ),
        "constraints": (
            "Which hard constraints or non-negotiables must design and implementation honor?",
            "summary | detail",
        ),
        "open_questions": (
            "Which unresolved questions should research or design answer next?",
            "summary | detail",
        ),
        "promotion_candidates": (
            "Which experiment-local items may need promotion into project-wide requirements?",
            "summary | requirement_hint | rationale | target_scope_kind",
        ),
    }
    collections = list(DISCUSS_COLLECTIONS)
    if scope_kind != "experiment":
        collections = [name for name in collections if name != "promotion_candidates"]
    for collection_name in collections:
        prompt_text, syntax = prompts[collection_name]
        questions.append(
            {
                "collection": collection_name,
                "prompt": prompt_text,
                "syntax": syntax,
                "existing": list(existing.get(collection_name, []))
                if isinstance(existing.get(collection_name), list)
                else [],
            }
        )
    return {
        "schema_version": "1.0",
        "command": "discuss",
        "scope_kind": scope_kind,
        "scope_root": str(scope_root),
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "context_resolution": {
            "compact_render": str(context_resolution.get("compact_render", "")).strip(),
            "diagnostics": list(context_resolution.get("diagnostics", []))
            if isinstance(context_resolution.get("diagnostics"), list)
            else [],
        },
        "questions": questions,
        "responses": {
            name: list(existing.get(name, []))
            if isinstance(existing.get(name), list)
            else []
            for name in DISCUSS_COLLECTIONS
        },
    }


def _coerce_discuss_answers(
    raw_payload: dict[str, Any],
    *,
    scope_kind: str,
) -> dict[str, list[dict[str, Any]]]:
    responses = raw_payload.get("responses")
    if not isinstance(responses, dict):
        responses = raw_payload
    output: dict[str, list[dict[str, Any]]] = {name: [] for name in DISCUSS_COLLECTIONS}
    for collection_name in DISCUSS_COLLECTIONS:
        if collection_name == "promotion_candidates" and scope_kind != "experiment":
            output[collection_name] = []
            continue
        raw_entries = responses.get(collection_name, [])
        if not isinstance(raw_entries, list):
            continue
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for index, raw_entry in enumerate(raw_entries):
            entry = _normalize_discuss_entry(
                collection_name,
                raw_entry,
                index=index,
                scope_kind=scope_kind,
            )
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id", "")).strip()
            if not item_id or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            normalized.append(entry)
        output[collection_name] = normalized
    return output


def _prompt_multiline_collection(
    question: dict[str, Any],
) -> list[str] | None:
    collection = str(question.get("collection", "")).strip()
    prompt_text = str(question.get("prompt", "")).strip()
    syntax = str(question.get("syntax", "")).strip()
    existing = question.get("existing")
    print("")
    print(f"[{collection}] {prompt_text}")
    if syntax:
        print(f"  syntax: {syntax}")
    if isinstance(existing, list) and existing:
        print("  existing:")
        for row in existing[:6]:
            if isinstance(row, dict):
                print(
                    "    - "
                    + (
                        str(row.get("summary", "")).strip()
                        or str(row.get("id", "")).strip()
                    )
                )
    print("  enter one item per line; blank line keeps existing; type !clear to clear.")
    lines: list[str] = []
    prompt = "> "
    while True:
        try:
            raw = input(prompt)
        except EOFError:
            break
        text = str(raw).strip()
        if not text:
            if not lines:
                return None
            break
        if text == "!clear" and not lines:
            return []
        lines.append(text)
        prompt = "... "
    return lines


def _load_answers_file(path: Path) -> dict[str, Any]:
    payload = _load_json_if_exists(path)
    if isinstance(payload, dict):
        return payload
    raise StageCheckError(f"answers file must contain a JSON object at {path}")


def _write_sidecar_outputs(
    *,
    repo_root: Path,
    sidecar_payload: dict[str, Any],
    output_paths: dict[str, Any],
) -> None:
    json_path = output_paths["json_path"]
    markdown_path = output_paths["markdown_path"]
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(json_path, sidecar_payload)
    markdown_path.write_text(
        build_sidecar_markdown(sidecar_payload),
        encoding="utf-8",
    )


def _resolve_local_agent_invocation(
    repo_root: Path,
    *,
    override_env_var: str,
    require_executable: bool = True,
) -> tuple[list[str], dict[str, str], str]:
    override = str(os.environ.get(override_env_var, "")).strip()
    if override:
        try:
            parsed = shlex.split(override)
        except ValueError as exc:
            raise RuntimeError(
                f"{override_env_var} could not be parsed: {exc}"
            ) from exc
        if not parsed:
            raise RuntimeError(f"{override_env_var} is empty")
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
        return (argv, env, " ".join(shlex.quote(token) for token in argv))

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
        return (argv, dict(os.environ), " ".join(shlex.quote(token) for token in argv))

    if require_executable:
        raise RuntimeError(
            f"no supported local LLM CLI found; install 'claude' or 'codex', or set {override_env_var}"
        )

    return (
        [""],
        dict(os.environ),
        "no supported local LLM CLI found",
    )


def _run_local_agent(
    repo_root: Path,
    *,
    prompt_text: str,
    timeout_seconds: float,
    override_env_var: str,
) -> tuple[int, str, str, str]:
    try:
        command_argv, command_env, command_display = _resolve_local_agent_invocation(
            repo_root,
            override_env_var=override_env_var,
        )
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
    except RuntimeError as exc:
        return (1, "", str(exc), "")
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


def _extract_json_object(text: str) -> dict[str, Any] | None:
    candidate = str(text or "").strip()
    if not candidate:
        return None
    if candidate.startswith("```"):
        parts = candidate.split("```")
        for part in parts:
            stripped = part.strip()
            if not stripped or stripped.lower().startswith("json"):
                stripped = (
                    stripped[4:].strip()
                    if stripped.lower().startswith("json")
                    else stripped
                )
            try:
                payload = json.loads(stripped)
            except Exception:
                continue
            if isinstance(payload, dict):
                return payload
    try:
        payload = json.loads(candidate)
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _build_research_source_catalog(
    *,
    repo_root: Path,
    context_resolution: dict[str, Any],
    exclude_path: str,
) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    components = context_resolution.get("components")
    if not isinstance(components, list):
        return sources
    for component in components:
        if not isinstance(component, dict) or not bool(component.get("selected")):
            continue
        path_text = str(component.get("path", "")).strip()
        if not path_text or path_text == exclude_path:
            continue
        source_id = str(component.get("component_id", "")).strip()
        if not source_id or source_id in seen_ids:
            continue
        seen_ids.add(source_id)
        artifact_kind = str(component.get("artifact_kind", "")).strip() or "artifact"
        sources.append(
            {
                "id": source_id,
                "summary": str(component.get("summary", "")).strip() or path_text,
                "detail": f"{artifact_kind} from {path_text}",
                "kind": artifact_kind,
                "path": path_text,
                "fingerprint": str(component.get("fingerprint", "")).strip()
                or _path_fingerprint(repo_root, path_text),
            }
        )
    return sources


def _build_research_questions(
    context_resolution: dict[str, Any],
    *,
    explicit_questions: list[str],
) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    effective_discuss = context_resolution.get("effective_discuss")
    if isinstance(effective_discuss, dict):
        for entry in effective_discuss.get("open_questions", []):
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id", "")).strip()
            summary = str(entry.get("summary", "")).strip()
            if not item_id or not summary or item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            questions.append(
                {
                    "id": item_id,
                    "summary": summary,
                    "detail": str(entry.get("detail", "")).strip() or summary,
                    "status": "unresolved",
                }
            )
    for index, question_text in enumerate(explicit_questions):
        normalized = str(question_text).strip()
        if not normalized:
            continue
        item_id = _slugify_sidecar_item_id("rq", normalized, index)
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        questions.append(
            {
                "id": item_id,
                "summary": normalized,
                "detail": normalized,
                "status": "unresolved",
            }
        )
    return questions


def _build_research_prompt(
    *,
    scope_kind: str,
    context_resolution: dict[str, Any],
    questions: list[dict[str, Any]],
    sources: list[dict[str, Any]],
    agent_surface: dict[str, Any],
) -> str:
    agent_guidance = build_agent_surface_guidance(agent_surface)
    guidance_lines = agent_guidance.get("stage_context_lines")
    if not isinstance(guidance_lines, list):
        guidance_lines = []
    return "\n".join(
        [
            "You are an Autolab evidence assistant.",
            "Use only the provided repository-local context. Do not browse the web or invent sources.",
            "Return a single JSON object with keys 'findings' and 'recommendations'.",
            "Every finding must include: id, summary, detail, question_ids, source_ids.",
            "Every recommendation must include: id, summary, detail, question_ids, finding_ids, applies_to_stages, source_ids.",
            "applies_to_stages must only contain 'design' or 'implementation'.",
            "Only use question_ids and source_ids that are provided below.",
            "",
            f"scope_kind: {scope_kind}",
            "",
            "Context resolution:",
            "```text",
            str(context_resolution.get("compact_render", "")).strip() or "(none)",
            "```",
            "",
            "Semantic agent surface:",
            "```text",
            "\n".join(str(item).strip() for item in guidance_lines if str(item).strip())
            or "(none)",
            "```",
            "",
            "Questions:",
            "```json",
            json.dumps(questions, indent=2),
            "```",
            "",
            "Available sources:",
            "```json",
            json.dumps(sources, indent=2),
            "```",
            "",
            "Now return only the JSON object.",
        ]
    )


def _normalize_research_entries(
    *,
    raw_payload: dict[str, Any],
    questions: list[dict[str, Any]],
    sources: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    question_ids = {str(entry.get("id", "")).strip() for entry in questions}
    source_ids = {str(entry.get("id", "")).strip() for entry in sources}
    findings: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_finding_ids: set[str] = set()
    raw_findings = raw_payload.get("findings")
    if isinstance(raw_findings, list):
        for index, raw_entry in enumerate(raw_findings):
            if not isinstance(raw_entry, dict):
                continue
            summary = str(raw_entry.get("summary", "")).strip()
            if not summary:
                continue
            item_id = str(raw_entry.get("id", "")).strip() or _slugify_sidecar_item_id(
                "rf", summary, index
            )
            if item_id in seen_finding_ids:
                continue
            seen_finding_ids.add(item_id)
            question_refs = (
                [
                    str(item).strip()
                    for item in raw_entry.get("question_ids", [])
                    if str(item).strip() in question_ids
                ]
                if isinstance(raw_entry.get("question_ids"), list)
                else []
            )
            source_refs = (
                [
                    str(item).strip()
                    for item in raw_entry.get("source_ids", [])
                    if str(item).strip() in source_ids
                ]
                if isinstance(raw_entry.get("source_ids"), list)
                else []
            )
            if not question_refs:
                errors.append(
                    f"finding '{item_id}' must reference at least one known question_id"
                )
                continue
            if not source_refs:
                errors.append(
                    f"finding '{item_id}' must reference at least one known source_id"
                )
                continue
            findings.append(
                {
                    "id": item_id,
                    "summary": summary,
                    "detail": str(raw_entry.get("detail", "")).strip() or summary,
                    "question_ids": question_refs,
                    "source_ids": source_refs,
                }
            )
    finding_ids = {str(entry.get("id", "")).strip() for entry in findings}
    raw_recommendations = raw_payload.get("recommendations")
    if isinstance(raw_recommendations, list):
        seen_recommendation_ids: set[str] = set()
        for index, raw_entry in enumerate(raw_recommendations):
            if not isinstance(raw_entry, dict):
                continue
            summary = str(raw_entry.get("summary", "")).strip()
            if not summary:
                continue
            item_id = str(raw_entry.get("id", "")).strip() or _slugify_sidecar_item_id(
                "rr", summary, index
            )
            if item_id in seen_recommendation_ids:
                continue
            seen_recommendation_ids.add(item_id)
            question_refs = (
                [
                    str(item).strip()
                    for item in raw_entry.get("question_ids", [])
                    if str(item).strip() in question_ids
                ]
                if isinstance(raw_entry.get("question_ids"), list)
                else []
            )
            finding_refs = (
                [
                    str(item).strip()
                    for item in raw_entry.get("finding_ids", [])
                    if str(item).strip() in finding_ids
                ]
                if isinstance(raw_entry.get("finding_ids"), list)
                else []
            )
            source_refs = (
                [
                    str(item).strip()
                    for item in raw_entry.get("source_ids", [])
                    if str(item).strip() in source_ids
                ]
                if isinstance(raw_entry.get("source_ids"), list)
                else []
            )
            stages = (
                [
                    str(item).strip()
                    for item in raw_entry.get("applies_to_stages", [])
                    if str(item).strip() in {"design", "implementation"}
                ]
                if isinstance(raw_entry.get("applies_to_stages"), list)
                else []
            )
            if not question_refs:
                errors.append(
                    f"recommendation '{item_id}' must reference at least one known question_id"
                )
                continue
            if not finding_refs:
                errors.append(
                    f"recommendation '{item_id}' must reference at least one known finding_id"
                )
                continue
            if not source_refs:
                errors.append(
                    f"recommendation '{item_id}' must reference at least one known source_id"
                )
                continue
            recommendations.append(
                {
                    "id": item_id,
                    "summary": summary,
                    "detail": str(raw_entry.get("detail", "")).strip() or summary,
                    "question_ids": question_refs,
                    "finding_ids": finding_refs,
                    "applies_to_stages": stages or ["design"],
                    "source_ids": source_refs,
                }
            )
    answered_question_ids = {
        str(item).strip()
        for row in [*findings, *recommendations]
        for item in row.get("question_ids", [])
        if str(item).strip()
    }
    for question in questions:
        question["status"] = (
            "answered"
            if str(question.get("id", "")).strip() in answered_question_ids
            else "unresolved"
        )
    if errors:
        raise StageCheckError("; ".join(errors))
    return (findings, recommendations)


def _cmd_discuss(args: argparse.Namespace) -> int:
    try:
        (
            _state_path,
            repo_root,
            state,
            target,
            discuss_paths,
        ) = _resolve_sidecar_command_target(args, command_name="autolab discuss")
    except StageCheckError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    existing_payload = _load_json_if_exists(discuss_paths["json_path"])
    question_pack = _build_discuss_question_pack(
        scope_kind=target["scope_kind"],
        scope_root=discuss_paths["scope_root"],
        iteration_id=target["iteration_id"],
        experiment_id=target["experiment_id"],
        context_resolution=target["context_resolution"],
        existing_payload=existing_payload
        if isinstance(existing_payload, dict)
        else None,
    )

    question_pack_path = str(getattr(args, "write_question_pack", "") or "").strip()
    if question_pack_path:
        try:
            output_path = Path(question_pack_path).expanduser().resolve()
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(
                json.dumps(question_pack, indent=2) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            print(
                f"autolab discuss: ERROR failed writing question pack: {exc}",
                file=sys.stderr,
            )
            return 1

    answers_file_text = str(getattr(args, "answers_file", "") or "").strip()
    responses: dict[str, Any] = {"responses": question_pack["responses"]}
    non_interactive = bool(getattr(args, "non_interactive", False))
    if answers_file_text:
        try:
            responses = _load_answers_file(
                Path(answers_file_text).expanduser().resolve()
            )
        except StageCheckError as exc:
            print(f"autolab discuss: ERROR {exc}", file=sys.stderr)
            return 1
    elif non_interactive:
        responses = {"responses": question_pack["responses"]}
    else:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print(
                "autolab discuss: ERROR interactive discuss requires a TTY, --answers-file, or --non-interactive",
                file=sys.stderr,
            )
            return 1
        interactive_responses: dict[str, list[Any]] = {}
        for question in question_pack["questions"]:
            entered = _prompt_multiline_collection(question)
            collection_name = str(question.get("collection", "")).strip()
            if entered is None:
                interactive_responses[collection_name] = list(
                    question.get("existing", [])
                    if isinstance(question.get("existing"), list)
                    else []
                )
            else:
                interactive_responses[collection_name] = entered
        responses = {"responses": interactive_responses}

    normalized = _coerce_discuss_answers(
        responses,
        scope_kind=target["scope_kind"],
    )
    exclude_relpaths = {_sidecar_relpath(repo_root, discuss_paths["json_path"])}
    dependency_refs = build_sidecar_dependency_refs(
        repo_root,
        target["context_resolution"],
        exclude_paths=exclude_relpaths,
    )
    discuss_payload: dict[str, Any] = {
        "schema_version": "1.0",
        "sidecar_kind": "discuss",
        "scope_kind": target["scope_kind"],
        "scope_root": str(discuss_paths["scope_root"]),
        "generated_at": _utc_now(),
        "derived_from": dependency_refs,
        "stale_if": dependency_refs,
    }
    if target["scope_kind"] == "experiment":
        discuss_payload["iteration_id"] = target["iteration_id"]
        discuss_payload["experiment_id"] = target["experiment_id"]
    for collection_name in DISCUSS_COLLECTIONS:
        discuss_payload[collection_name] = normalized.get(collection_name, [])

    try:
        _write_sidecar_outputs(
            repo_root=repo_root,
            sidecar_payload=discuss_payload,
            output_paths=discuss_paths,
        )
    except Exception as exc:
        print(f"autolab discuss: ERROR failed writing sidecar: {exc}", file=sys.stderr)
        return 1

    if bool(getattr(args, "json", False)):
        print(
            json.dumps(
                {
                    "status": "ok",
                    "scope_kind": target["scope_kind"],
                    "iteration_id": target["iteration_id"],
                    "experiment_id": target["experiment_id"],
                    "json_path": _sidecar_relpath(
                        repo_root, discuss_paths["json_path"]
                    ),
                    "markdown_path": _sidecar_relpath(
                        repo_root, discuss_paths["markdown_path"]
                    ),
                    "counts": {
                        name: len(discuss_payload.get(name, []))
                        for name in DISCUSS_COLLECTIONS
                    },
                    "question_pack_path": question_pack_path,
                },
                indent=2,
            )
        )
    else:
        print("autolab discuss")
        print(f"scope_kind: {target['scope_kind']}")
        if target["iteration_id"]:
            print(f"iteration_id: {target['iteration_id']}")
        if target["experiment_id"]:
            print(f"experiment_id: {target['experiment_id']}")
        print(f"json_path: {discuss_paths['json_path']}")
        print(f"markdown_path: {discuss_paths['markdown_path']}")
    return 0


def _cmd_research(args: argparse.Namespace) -> int:
    try:
        (
            _state_path,
            repo_root,
            state,
            target,
            discuss_paths,
        ) = _resolve_sidecar_command_target(args, command_name="autolab research")
        research_paths = resolve_sidecar_output_paths(
            repo_root,
            scope_kind=target["scope_kind"],
            sidecar_kind="research",
            iteration_id=target["iteration_id"],
            experiment_id=target["experiment_id"],
        )
    except StageCheckError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"autolab research: ERROR {exc}", file=sys.stderr)
        return 1

    explicit_questions = [
        str(item).strip()
        for item in (getattr(args, "question", []) or [])
        if str(item).strip()
    ]
    questions = _build_research_questions(
        target["context_resolution"],
        explicit_questions=explicit_questions,
    )
    if not questions:
        if bool(getattr(args, "json", False)):
            print(
                json.dumps(
                    {
                        "status": "noop",
                        "reason": "no unresolved questions",
                        "scope_kind": target["scope_kind"],
                    },
                    indent=2,
                )
            )
        else:
            print("autolab research: no unresolved questions; nothing written")
        return 0

    dependency_refs = build_sidecar_dependency_refs(
        repo_root,
        target["context_resolution"],
        exclude_paths={_sidecar_relpath(repo_root, research_paths["json_path"])},
    )
    sources = _build_research_source_catalog(
        repo_root=repo_root,
        context_resolution=target["context_resolution"],
        exclude_path=_sidecar_relpath(repo_root, research_paths["json_path"]),
    )
    try:
        preview_argv, _preview_env, _preview_display = _resolve_local_agent_invocation(
            repo_root,
            override_env_var="AUTOLAB_RESEARCH_AGENT_COMMAND",
            require_executable=False,
        )
    except RuntimeError as exc:
        print(f"autolab research: ERROR {exc}", file=sys.stderr)
        return 1
    research_agent_surface = resolve_agent_surface(
        repo_root,
        provider=infer_agent_surface_provider(preview_argv),
        command_name="research",
    )
    prompt_text = _build_research_prompt(
        scope_kind=target["scope_kind"],
        context_resolution=target["context_resolution"],
        questions=questions,
        sources=sources,
        agent_surface=research_agent_surface,
    )

    try:
        timeout_seconds = float(getattr(args, "timeout_seconds", 240.0))
    except Exception:
        print(
            "autolab research: ERROR --timeout-seconds must be a number",
            file=sys.stderr,
        )
        return 1
    if timeout_seconds <= 0:
        print(
            "autolab research: ERROR --timeout-seconds must be > 0",
            file=sys.stderr,
        )
        return 1

    exit_code, stdout, stderr, command_display = _run_local_agent(
        repo_root,
        prompt_text=prompt_text,
        timeout_seconds=timeout_seconds,
        override_env_var="AUTOLAB_RESEARCH_AGENT_COMMAND",
    )
    if exit_code != 0:
        detail = stderr or stdout or "agent returned no output"
        print(
            f"autolab research: ERROR agent failed with exit_code={exit_code}: {detail}",
            file=sys.stderr,
        )
        return 1

    agent_payload = _extract_json_object(stdout)
    if not isinstance(agent_payload, dict):
        print(
            "autolab research: ERROR agent output was not valid JSON",
            file=sys.stderr,
        )
        return 1
    try:
        findings, recommendations = _normalize_research_entries(
            raw_payload=agent_payload,
            questions=questions,
            sources=sources,
        )
    except StageCheckError as exc:
        print(f"autolab research: ERROR {exc}", file=sys.stderr)
        return 1

    research_payload: dict[str, Any] = {
        "schema_version": "1.0",
        "sidecar_kind": "research",
        "scope_kind": target["scope_kind"],
        "scope_root": str(research_paths["scope_root"]),
        "generated_at": _utc_now(),
        "derived_from": dependency_refs,
        "stale_if": dependency_refs,
        "questions": questions,
        "findings": findings,
        "recommendations": recommendations,
        "sources": sources,
    }
    if target["scope_kind"] == "experiment":
        research_payload["iteration_id"] = target["iteration_id"]
        research_payload["experiment_id"] = target["experiment_id"]

    try:
        _write_sidecar_outputs(
            repo_root=repo_root,
            sidecar_payload=research_payload,
            output_paths=research_paths,
        )
    except Exception as exc:
        print(f"autolab research: ERROR failed writing sidecar: {exc}", file=sys.stderr)
        return 1

    if bool(getattr(args, "json", False)):
        print(
            json.dumps(
                {
                    "status": "ok",
                    "scope_kind": target["scope_kind"],
                    "iteration_id": target["iteration_id"],
                    "experiment_id": target["experiment_id"],
                    "json_path": _sidecar_relpath(
                        repo_root, research_paths["json_path"]
                    ),
                    "markdown_path": _sidecar_relpath(
                        repo_root, research_paths["markdown_path"]
                    ),
                    "question_count": len(questions),
                    "finding_count": len(findings),
                    "recommendation_count": len(recommendations),
                    "llm_command": command_display,
                },
                indent=2,
            )
        )
    else:
        print("autolab research")
        print(f"scope_kind: {target['scope_kind']}")
        if target["iteration_id"]:
            print(f"iteration_id: {target['iteration_id']}")
        if target["experiment_id"]:
            print(f"experiment_id: {target['experiment_id']}")
        print(f"json_path: {research_paths['json_path']}")
        print(f"markdown_path: {research_paths['markdown_path']}")
        print(f"llm_command: {command_display}")
    return 0


# ---------------------------------------------------------------------------
# Issue report command
# ---------------------------------------------------------------------------


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
    return _resolve_local_agent_invocation(
        repo_root,
        override_env_var="AUTOLAB_REPORT_AGENT_COMMAND",
    )


def _run_issue_report_agent(
    repo_root: Path,
    *,
    prompt_text: str,
    timeout_seconds: float,
) -> tuple[int, str, str, str]:
    return _run_local_agent(
        repo_root,
        prompt_text=prompt_text,
        timeout_seconds=timeout_seconds,
        override_env_var="AUTOLAB_REPORT_AGENT_COMMAND",
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

    if bool(getattr(args, "campaign", False)):
        if str(args.comment or "").strip():
            print(
                "autolab report: ERROR --comment is only supported for issue reports",
                file=sys.stderr,
            )
            return 1
        if int(getattr(args, "log_tail", 500)) != 500:
            print(
                "autolab report: ERROR --log-tail is only supported for issue reports",
                file=sys.stderr,
            )
            return 1
        if float(getattr(args, "timeout_seconds", 240.0)) != 240.0:
            print(
                "autolab report: ERROR --timeout-seconds is only supported for issue reports",
                file=sys.stderr,
            )
            return 1
        try:
            campaign = _load_campaign(repo_root)
        except Exception as exc:
            print(f"autolab report: ERROR {exc}", file=sys.stderr)
            return 1
        if campaign is None:
            print(
                "autolab report: ERROR no active campaign is available",
                file=sys.stderr,
            )
            return 1
        try:
            results_payload = _refresh_campaign_results(repo_root, campaign)
        except Exception as exc:
            print(
                f"autolab report: ERROR failed to refresh campaign results: {exc}",
                file=sys.stderr,
            )
            return 1
        handoff_payload, handoff_error = _safe_refresh_handoff(state_path)
        if handoff_payload is None:
            handoff_payload = {
                "blocking_failures": [f"handoff refresh failed: {handoff_error}"]
            }
        try:
            report_payload = _campaign_build_morning_report_payload(
                repo_root,
                campaign,
                results_payload=results_payload,
                handoff_payload=handoff_payload,
            )
            report_text = _campaign_render_morning_report(
                repo_root,
                campaign,
                report_payload,
            )
        except Exception as exc:
            print(
                f"autolab report: ERROR failed to build campaign report: {exc}",
                file=sys.stderr,
            )
            return 1
        if args.output:
            output_path = Path(args.output).expanduser().resolve()
        else:
            output_path = _campaign_morning_report_path(repo_root, campaign)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report_text, encoding="utf-8")
        print(f"autolab report: wrote {output_path}")
        return 0

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


_ORACLE_APPLY_DISCUSS_COLLECTIONS = (
    "locked_decisions",
    "preferences",
    "constraints",
    "open_questions",
)
_ORACLE_APPLY_FEEDBACK_SIGNALS = {"none", "stop", "rethink"}


def _oracle_apply_input_source_label(repo_root: Path, path: Path) -> str:
    return _sidecar_relpath(repo_root, path)


def _read_oracle_apply_input(
    args: argparse.Namespace,
    *,
    repo_root: Path,
) -> tuple[str, str]:
    notes_path_text = str(getattr(args, "notes", "") or "").strip()
    reply_path_text = str(getattr(args, "reply_path", "") or "").strip()
    from_stdin = bool(getattr(args, "stdin", False))
    provided_file = notes_path_text or reply_path_text
    if sum(1 for item in (provided_file, "stdin" if from_stdin else "") if item) != 1:
        raise RuntimeError("choose exactly one of <reply.md>, --notes, or --stdin")

    if provided_file:
        notes_path = Path(provided_file).expanduser()
        if not notes_path.is_absolute():
            notes_path = (repo_root / notes_path).resolve(strict=False)
        else:
            notes_path = notes_path.resolve(strict=False)
        if not notes_path.exists() or not notes_path.is_file():
            raise RuntimeError(f"notes file not found: {notes_path}")
        try:
            note_text = notes_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            note_text = notes_path.read_text(encoding="utf-8", errors="replace")
        source_label = _oracle_apply_input_source_label(repo_root, notes_path)
    else:
        note_text = sys.stdin.read()
        source_label = "stdin"

    if not str(note_text).strip():
        raise RuntimeError("input notes are empty")
    return (source_label, str(note_text))


def _extract_markdown_section(markdown_text: str, heading: str) -> str:
    pattern = re.compile(
        rf"^## {re.escape(heading)}\s*$\n(?P<body>.*?)(?=^## \S|\Z)",
        flags=re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(markdown_text)
    if not match:
        return ""
    return str(match.group("body") or "").strip()


def _prepare_oracle_apply_notes(note_text: str) -> str:
    normalized = str(note_text or "").strip()
    if "# Autolab Oracle" not in normalized:
        return normalized
    expert_review = _extract_markdown_section(normalized, "Expert Review")
    next_steps = _extract_markdown_section(normalized, "Recommended Next Steps")
    extracted_sections: list[str] = []
    if expert_review:
        extracted_sections.extend(["## Expert Review", expert_review])
    if next_steps:
        extracted_sections.extend(["", "## Recommended Next Steps", next_steps])
    return "\n".join(extracted_sections).strip() or normalized


def _normalize_oracle_apply_discuss_updates(
    raw_payload: Any,
    *,
    scope_kind: str,
) -> dict[str, list[dict[str, Any]]]:
    if raw_payload in ("", None):
        raw_payload = {}
    if not isinstance(raw_payload, dict):
        raise StageCheckError("oracle apply discuss_updates must be an object")
    output: dict[str, list[dict[str, Any]]] = {
        name: [] for name in _ORACLE_APPLY_DISCUSS_COLLECTIONS
    }
    for collection_name in _ORACLE_APPLY_DISCUSS_COLLECTIONS:
        raw_entries = raw_payload.get(collection_name, [])
        if raw_entries in ("", None):
            raw_entries = []
        if not isinstance(raw_entries, list):
            raise StageCheckError(
                f"oracle apply discuss_updates.{collection_name} must be a list"
            )
        seen_ids: set[str] = set()
        seen_summaries: set[str] = set()
        normalized_entries: list[dict[str, Any]] = []
        for index, raw_entry in enumerate(raw_entries):
            entry = _normalize_discuss_entry(
                collection_name,
                raw_entry,
                index=index,
                scope_kind=scope_kind,
            )
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id", "")).strip()
            summary_key = _normalize_space(str(entry.get("summary", ""))).lower()
            if not item_id or item_id in seen_ids:
                continue
            if summary_key and summary_key in seen_summaries:
                continue
            seen_ids.add(item_id)
            if summary_key:
                seen_summaries.add(summary_key)
            normalized_entries.append(entry)
        output[collection_name] = normalized_entries
    return output


def _normalize_oracle_apply_research_questions(
    raw_payload: Any,
) -> list[dict[str, str]]:
    if raw_payload in ("", None):
        raw_payload = []
    if not isinstance(raw_payload, list):
        raise StageCheckError("oracle apply research_questions must be a list")
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, raw_entry in enumerate(raw_payload):
        if isinstance(raw_entry, str):
            summary = _normalize_space(raw_entry)
            detail = summary
            item_id = ""
        elif isinstance(raw_entry, dict):
            summary = _normalize_space(str(raw_entry.get("summary", "")))
            detail = _normalize_space(str(raw_entry.get("detail", ""))) or summary
            item_id = _normalize_space(str(raw_entry.get("id", "")))
        else:
            continue
        if not summary:
            continue
        item_id = item_id or _slugify_sidecar_item_id("rq", summary, index)
        dedupe_key = (item_id, summary.lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        output.append(
            {
                "id": item_id,
                "summary": summary,
                "detail": detail,
            }
        )
    return output


def _normalize_oracle_apply_todo_hints(
    raw_payload: Any,
    *,
    current_stage: str,
) -> list[dict[str, Any]]:
    if raw_payload in ("", None):
        raw_payload = []
    if not isinstance(raw_payload, list):
        raise StageCheckError("oracle apply todo_hints must be a list")
    default_stage = current_stage if current_stage in ALL_STAGES else "implementation"
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw_entry in raw_payload:
        if isinstance(raw_entry, str):
            summary = _normalize_space(raw_entry)
            detail = summary
            stage = default_stage
            priority = ""
            owner = ""
            labels: list[str] = []
        elif isinstance(raw_entry, dict):
            summary = _normalize_space(str(raw_entry.get("summary", "")))
            detail = _normalize_space(str(raw_entry.get("detail", ""))) or summary
            stage = _normalize_space(str(raw_entry.get("stage", ""))).lower()
            priority = _normalize_space(str(raw_entry.get("priority", ""))).lower()
            owner = _normalize_space(str(raw_entry.get("owner", "")))
            labels = []
            raw_labels = raw_entry.get("labels", [])
            if raw_labels not in ("", None):
                if not isinstance(raw_labels, list):
                    raise StageCheckError(
                        "oracle apply todo_hints labels must be a list"
                    )
                labels = [
                    _normalize_space(str(item)).lower()
                    for item in raw_labels
                    if _normalize_space(str(item))
                ]
        else:
            continue
        if not summary:
            continue
        if stage not in ALL_STAGES:
            stage = default_stage
        dedupe_key = (stage, summary.lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        output.append(
            {
                "summary": summary,
                "detail": detail,
                "stage": stage,
                "priority": priority,
                "owner": owner,
                "labels": labels,
            }
        )
    return output


def _normalize_oracle_apply_campaign_feedback(
    raw_payload: Any,
) -> list[dict[str, str]]:
    if raw_payload in ("", None):
        raw_payload = []
    if not isinstance(raw_payload, list):
        raise StageCheckError("oracle apply campaign_feedback must be a list")
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_entry in raw_payload:
        if isinstance(raw_entry, str):
            summary = _normalize_space(raw_entry)
            detail = summary
            signal = "none"
        elif isinstance(raw_entry, dict):
            summary = _normalize_space(str(raw_entry.get("summary", "")))
            detail = _normalize_space(str(raw_entry.get("detail", ""))) or summary
            signal = _normalize_space(str(raw_entry.get("signal", ""))).lower()
        else:
            continue
        if signal not in _ORACLE_APPLY_FEEDBACK_SIGNALS:
            raise StageCheckError(
                f"oracle apply campaign_feedback signal '{signal}' is unsupported"
            )
        if not summary and not detail:
            continue
        dedupe_key = (summary.lower(), detail.lower(), signal)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        output.append(
            {
                "summary": summary,
                "detail": detail,
                "signal": signal,
            }
        )
    return output


def _normalize_oracle_apply_payload(
    raw_payload: dict[str, Any],
    *,
    scope_kind: str,
    current_stage: str,
) -> dict[str, Any]:
    if not isinstance(raw_payload, dict):
        raise StageCheckError("oracle apply classifier must return a JSON object")
    summary = raw_payload.get("summary", "")
    if summary not in ("", None) and not isinstance(summary, str):
        raise StageCheckError("oracle apply summary must be a string")
    plan_approval_note = raw_payload.get("plan_approval_note", "")
    if plan_approval_note not in ("", None) and not isinstance(plan_approval_note, str):
        raise StageCheckError("oracle apply plan_approval_note must be a string")
    return {
        "summary": _normalize_space(str(summary or "")),
        "discuss_updates": _normalize_oracle_apply_discuss_updates(
            raw_payload.get("discuss_updates", {}),
            scope_kind=scope_kind,
        ),
        "research_questions": _normalize_oracle_apply_research_questions(
            raw_payload.get("research_questions", [])
        ),
        "todo_hints": _normalize_oracle_apply_todo_hints(
            raw_payload.get("todo_hints", []),
            current_stage=current_stage,
        ),
        "campaign_feedback": _normalize_oracle_apply_campaign_feedback(
            raw_payload.get("campaign_feedback", [])
        ),
        "plan_approval_note": _normalize_space(str(plan_approval_note or "")),
    }


def _oracle_apply_entry_key(entry: dict[str, Any]) -> tuple[str, str]:
    return (
        str(entry.get("id", "")).strip(),
        _normalize_space(str(entry.get("summary", ""))).lower(),
    )


def _merge_oracle_sidecar_entries(
    existing_entries: Any,
    additions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    output: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    if isinstance(existing_entries, list):
        for raw_entry in existing_entries:
            if not isinstance(raw_entry, dict):
                continue
            key = _oracle_apply_entry_key(raw_entry)
            if key in seen:
                continue
            seen.add(key)
            output.append(raw_entry)

    added = 0
    for entry in additions:
        if not isinstance(entry, dict):
            continue
        key = _oracle_apply_entry_key(entry)
        if key in seen:
            continue
        seen.add(key)
        output.append(entry)
        added += 1
    return (output, added)


def _build_oracle_apply_prompt(
    *,
    state: dict[str, Any],
    scope_kind: str,
    context_resolution: dict[str, Any],
    effective_discuss: dict[str, Any],
    effective_research: dict[str, Any],
    campaign_summary: dict[str, Any],
    plan_approval_summary: dict[str, Any],
    prepared_notes: str,
    source_label: str,
) -> str:
    prompt_payload = {
        "scope_kind": scope_kind,
        "stage": str(state.get("stage", "")).strip(),
        "iteration_id": str(state.get("iteration_id", "")).strip(),
        "experiment_id": str(state.get("experiment_id", "")).strip(),
        "context_resolution": {
            "compact_render": str(context_resolution.get("compact_render", "")).strip(),
            "diagnostics": list(context_resolution.get("diagnostics", []))
            if isinstance(context_resolution.get("diagnostics"), list)
            else [],
        },
        "effective_discuss": effective_discuss,
        "effective_research": effective_research,
        "campaign": campaign_summary,
        "plan_approval": plan_approval_summary,
    }
    return "\n".join(
        [
            "You are an Autolab oracle-ingestion assistant.",
            "Classify the expert feedback into steering updates for the current repository state.",
            "Use only the provided repo-local context. Do not browse the web or invent files or statuses.",
            "",
            "Return only a single JSON object with these keys:",
            "- summary: short sentence",
            "- discuss_updates: object with optional arrays locked_decisions, preferences, constraints, open_questions",
            "- research_questions: array of unresolved research questions",
            "- todo_hints: array of concrete next-step tasks",
            "- campaign_feedback: array of campaign steering notes",
            "- plan_approval_note: string",
            "",
            "Constraints:",
            f"- Allowed todo_hints.stage values: {', '.join(sorted(ALL_STAGES))}",
            "- campaign_feedback.signal must be one of: none, stop, rethink",
            "- Prefer empty arrays or empty strings when a bucket does not apply",
            "- Do not include markdown fences or commentary outside the JSON object",
            "",
            "Current context (JSON):",
            "```json",
            json.dumps(prompt_payload, indent=2, sort_keys=True),
            "```",
            "",
            f"Expert notes source: {source_label}",
            "Expert notes to classify:",
            "```markdown",
            prepared_notes.strip(),
            "```",
            "",
            "Now return only the JSON object.",
        ]
    )


def _build_oracle_apply_sidecar_payload(
    *,
    sidecar_kind: str,
    scope_kind: str,
    scope_root: Path,
    iteration_id: str,
    experiment_id: str,
    dependency_refs: list[dict[str, str]],
    existing_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "sidecar_kind": sidecar_kind,
        "scope_kind": scope_kind,
        "scope_root": str(scope_root),
        "generated_at": _utc_now(),
        "derived_from": dependency_refs,
        "stale_if": dependency_refs,
    }
    if scope_kind == "experiment":
        payload["iteration_id"] = iteration_id
        payload["experiment_id"] = experiment_id
    collections = SIDECAR_COLLECTIONS_BY_KIND.get(sidecar_kind, ())
    existing = existing_payload if isinstance(existing_payload, dict) else {}
    for collection_name in collections:
        payload[collection_name] = (
            list(existing.get(collection_name, []))
            if isinstance(existing.get(collection_name), list)
            else []
        )
    return payload


def _oracle_apply_mirrored_open_questions(
    research_questions: list[dict[str, str]],
    *,
    scope_kind: str,
) -> list[dict[str, Any]]:
    mirrored: list[dict[str, Any]] = []
    for index, question in enumerate(research_questions):
        entry = _normalize_discuss_entry(
            "open_questions",
            {
                "summary": str(question.get("summary", "")).strip(),
                "detail": str(question.get("detail", "")).strip(),
            },
            index=index,
            scope_kind=scope_kind,
        )
        if isinstance(entry, dict):
            mirrored.append(entry)
    return mirrored


def _apply_oracle_todo_hints(
    repo_root: Path,
    *,
    state: dict[str, Any],
    todo_hints: list[dict[str, Any]],
) -> tuple[list[Path], int]:
    if not todo_hints:
        return ([], 0)
    open_tasks = list_open_tasks(repo_root)
    seen = {
        (
            _normalize_space(str(task.get("stage", ""))).lower(),
            _normalize_space(str(task.get("text", ""))).lower(),
        )
        for task in open_tasks
    }
    todo_path = repo_root / "docs" / "todo.md"
    default_stage = str(state.get("stage", "")).strip().lower()
    if default_stage not in ALL_STAGES:
        default_stage = "implementation"

    inserted = 0
    for hint in todo_hints:
        summary = _normalize_space(str(hint.get("summary", "")))
        if not summary:
            continue
        stage = _normalize_space(str(hint.get("stage", ""))).lower()
        if stage not in ALL_STAGES:
            stage = default_stage
        dedupe_key = (stage, summary.lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        priority = _normalize_space(str(hint.get("priority", ""))).lower()
        owner = _normalize_space(str(hint.get("owner", "")))
        labels = [
            _normalize_space(str(item)).lower()
            for item in hint.get("labels", [])
            if _normalize_space(str(item))
        ]
        if "oracle" not in labels:
            labels.append("oracle")
        tags: list[str] = []
        if priority:
            tags.append(f"[priority:{priority}]")
        if owner:
            tags.append(f"[owner:{owner}]")
        for label in labels:
            tags.append(f"[label:{label}]")
        suffix = f" {' '.join(tags)}" if tags else ""
        _insert_todo_task_line(todo_path, line=f"- [stage:{stage}] {summary}{suffix}")
        inserted += 1

    if inserted == 0:
        return ([], 0)
    changed_files, _message = _safe_todo_pre_sync(repo_root, state)
    return ([todo_path, *changed_files], inserted)


def _oracle_reply_to_discuss_updates(
    reply: Any,
    *,
    scope_kind: str,
) -> dict[str, list[dict[str, Any]]]:
    rationale = getattr(reply, "rationale", ())
    risks = getattr(reply, "risks", ())
    updates = {name: [] for name in _ORACLE_APPLY_DISCUSS_COLLECTIONS}

    for index, item in enumerate(rationale):
        entry = _normalize_discuss_entry(
            "preferences",
            {"summary": str(item).strip(), "detail": str(item).strip()},
            index=index,
            scope_kind=scope_kind,
        )
        if isinstance(entry, dict):
            updates["preferences"].append(entry)

    risk_offset = len(updates["preferences"])
    for index, item in enumerate(risks):
        collection_name = "open_questions" if "?" in str(item) else "constraints"
        entry = _normalize_discuss_entry(
            collection_name,
            {"summary": str(item).strip(), "detail": str(item).strip()},
            index=risk_offset + index,
            scope_kind=scope_kind,
        )
        if isinstance(entry, dict):
            updates[collection_name].append(entry)
    return updates


def _oracle_reply_to_research_questions(reply: Any) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for index, item in enumerate(getattr(reply, "risks", ())):
        text = _normalize_space(str(item))
        if not text or "?" not in text:
            continue
        output.append(
            {
                "id": _slugify_sidecar_item_id("rq", text, index),
                "summary": text,
                "detail": text,
            }
        )
    return output


def _oracle_reply_to_todo_hints(
    reply: Any,
    *,
    current_stage: str,
    allow_rewind_design: bool = True,
    allow_request_human_review: bool = True,
    allow_stop_campaign: bool = True,
) -> list[dict[str, Any]]:
    default_stage = current_stage if current_stage in ALL_STAGES else "implementation"
    if getattr(reply, "verdict", "") == "rethink_design" and allow_rewind_design:
        default_stage = "design"
    elif (
        getattr(reply, "verdict", "") == "request_human_review"
        and allow_request_human_review
    ):
        default_stage = "human_review"
    elif getattr(reply, "verdict", "") == "stop_campaign" and allow_stop_campaign:
        default_stage = "decide_repeat"
    hints: list[dict[str, Any]] = []
    for action in getattr(reply, "recommended_actions", ()):
        text = _normalize_space(str(action))
        if not text:
            continue
        hints.append(
            {
                "summary": text,
                "detail": text,
                "stage": default_stage,
                "priority": "",
                "owner": "",
                "labels": ["oracle"],
            }
        )
    return hints


def _oracle_reply_to_campaign_feedback(reply: Any) -> list[dict[str, str]]:
    verdict = str(getattr(reply, "verdict", "")).strip()
    if verdict not in ORACLE_ALLOWED_VERDICTS:
        return []
    signal = "none"
    if verdict == "rethink_design":
        signal = "rethink"
    elif verdict == "stop_campaign":
        signal = "stop"
    summary = oracle_default_suggested_next_action(verdict) or verdict
    detail = "; ".join(getattr(reply, "rationale", ())) or summary
    return [{"summary": summary, "detail": detail, "signal": signal}]


def _oracle_reply_plan_note(reply: Any) -> str:
    verdict = str(getattr(reply, "verdict", "")).strip()
    if verdict not in ORACLE_ALLOWED_VERDICTS:
        return ""
    detail = "; ".join(getattr(reply, "rationale", ()))
    if detail:
        return f"Oracle verdict: {verdict}. {detail}"
    return f"Oracle verdict: {verdict}."


def _oracle_reply_disfavored_family(
    campaign_summary: dict[str, Any], reply: Any
) -> str:
    if str(getattr(reply, "verdict", "")).strip() != "switch_family":
        return ""
    return str(campaign_summary.get("idea_journal_active_family", "")).strip()


def _oracle_reply_apply_status(reply: Any) -> str:
    return _oracle_reply_apply_status_with_effects(
        reply,
        campaign_stopped=str(getattr(reply, "verdict", "")).strip() == "stop_campaign",
        recommended_human_review=bool(
            getattr(reply, "recommended_human_review", False)
        ),
    )


def _oracle_reply_apply_status_with_effects(
    reply: Any,
    *,
    campaign_stopped: bool,
    recommended_human_review: bool,
) -> str:
    if campaign_stopped:
        return "campaign_stopped"
    if recommended_human_review:
        return "human_review_recommended"
    return "applied"


def _resolve_oracle_agent_invocation(
    repo_root: Path,
) -> tuple[list[str], dict[str, str], str]:
    return _resolve_local_agent_invocation(
        repo_root,
        override_env_var="AUTOLAB_ORACLE_AGENT_COMMAND",
    )


def _run_oracle_agent(
    repo_root: Path,
    *,
    prompt_text: str,
    timeout_seconds: float,
) -> tuple[int, str, str, str]:
    return _run_local_agent(
        repo_root,
        prompt_text=prompt_text,
        timeout_seconds=timeout_seconds,
        override_env_var="AUTOLAB_ORACLE_AGENT_COMMAND",
    )


def _oracle_language_for_path(path_text: str) -> str:
    suffix = Path(path_text).suffix.lower()
    if suffix == ".md":
        return "markdown"
    if suffix == ".json":
        return "json"
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    if suffix == ".py":
        return "python"
    if suffix in {".sh", ".bash"}:
        return "bash"
    if suffix == ".toml":
        return "toml"
    if suffix == ".log":
        return "text"
    return "text"


def _oracle_build_appendix_block(
    *,
    path_text: str,
    role: str,
    status: str,
    reason: str,
    content: str,
) -> str:
    language = _oracle_language_for_path(path_text)
    body = content.rstrip("\n")
    if not body:
        body = "Artifact content is empty."
    return "\n".join(
        [
            f"### Artifact: {path_text}",
            "",
            f"- role: `{role}`",
            f"- status: `{status}`",
            f"- reason: {reason}",
            "",
            f"```{language}",
            body,
            "```",
            "",
        ]
    ).rstrip()


def _oracle_collect_sources(
    *,
    repo_root: Path,
    handoff_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    packet = handoff_payload.get("continuation_packet")
    if not isinstance(packet, dict):
        raise RuntimeError("handoff continuation_packet is missing or invalid")
    raw_pointers = packet.get("artifact_pointers")
    if not isinstance(raw_pointers, list) or not raw_pointers:
        raise RuntimeError("handoff continuation_packet has no artifact_pointers")

    diagnostics: list[str] = []
    packet_diagnostics = packet.get("diagnostics")
    if isinstance(packet_diagnostics, list):
        diagnostics.extend(
            str(item).strip() for item in packet_diagnostics if str(item).strip()
        )

    sources: list[dict[str, Any]] = []
    for raw_pointer in raw_pointers:
        if not isinstance(raw_pointer, dict):
            continue
        if not bool(raw_pointer.get("inline_in_oracle", False)):
            continue
        path_text = str(raw_pointer.get("path", "")).strip()
        if not path_text:
            continue
        role = str(raw_pointer.get("role", "artifact")).strip() or "artifact"
        status = str(raw_pointer.get("status", "")).strip() or "unknown"
        reason = (
            str(raw_pointer.get("reason", "")).strip()
            or "Relevant continuation artifact."
        )
        content = ""

        resolved_path, pointer_error = _docs_resolve_pointer_path(repo_root, path_text)
        if pointer_error:
            diagnostics.append(pointer_error)
            status = "invalid"
            content = f"Artifact resolution error: {pointer_error}"
        elif resolved_path is None or not resolved_path.exists():
            status = "missing"
            content = f"Artifact unavailable at {path_text}."
        else:
            try:
                content = resolved_path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                content = resolved_path.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                diagnostics.append(f"failed reading {path_text}: {exc}")
                status = "invalid"
                content = f"Artifact read error for {path_text}: {exc}"
            else:
                status = "present"

        appendix_block = _oracle_build_appendix_block(
            path_text=path_text,
            role=role,
            status=status,
            reason=reason,
            content=content,
        )
        sources.append(
            {
                "role": role,
                "path": path_text,
                "status": status,
                "reason": reason,
                "content": content,
                "appendix_block": appendix_block,
                "appendix_heading": f"### Artifact: {path_text}",
                "appendix_snippet": content.strip()[:120],
            }
        )

    if not sources:
        raise RuntimeError(
            "handoff continuation_packet produced no oracle-inline sources"
        )
    return (sources, diagnostics)


def _build_oracle_prompt(
    *,
    continuation_packet: dict[str, Any],
    sources: list[dict[str, Any]],
    diagnostics: list[str],
) -> str:
    source_catalog = [
        {
            "role": str(item.get("role", "")).strip(),
            "path": str(item.get("path", "")).strip(),
            "status": str(item.get("status", "")).strip(),
            "reason": str(item.get("reason", "")).strip(),
        }
        for item in sources
    ]
    appendix_blocks = "\n\n".join(
        str(item.get("appendix_block", "")).rstrip() for item in sources
    ).strip()
    return "\n".join(
        [
            "You are an Autolab oracle assistant.",
            "Use only the provided continuation packet and artifact contents.",
            "Do not browse the web. Do not invent facts or sources.",
            "",
            "Return Markdown with these sections in order:",
            "# Autolab Oracle",
            "## Summary",
            "## Continuation Packet",
            "## Expert Review",
            "## Recommended Next Steps",
            "## Artifact Guide",
            "## Appendices",
            "",
            "Requirements:",
            "- In `## Continuation Packet`, include a fenced `json` block containing the exact continuation packet JSON.",
            "- In `## Artifact Guide`, include a short markdown table with columns Path | Role | Status | Why it matters.",
            "- Under `## Appendices`, paste every appendix block provided below exactly as-is and in the same order.",
            "- Do not replace appendices with links, summaries, or references.",
            "",
            "Continuation packet (JSON):",
            "```json",
            json.dumps(continuation_packet, indent=2, sort_keys=True),
            "```",
            "",
            "Artifact catalog (JSON):",
            "```json",
            json.dumps(source_catalog, indent=2, sort_keys=True),
            "```",
            "",
            "Diagnostics:",
            "```json",
            json.dumps(diagnostics, indent=2),
            "```",
            "",
            "Required appendix blocks (paste exactly):",
            appendix_blocks,
            "",
            "Now produce the oracle document.",
        ]
    )


def _oracle_table_cell(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _render_oracle_document(
    *,
    continuation_packet: dict[str, Any],
    sources: list[dict[str, Any]],
    diagnostics: list[str],
) -> str:
    artifact_guide = [
        "| Path | Role | Status | Why it matters |",
        "| --- | --- | --- | --- |",
    ]
    for source in sources:
        artifact_guide.append(
            "| "
            f"{_oracle_table_cell(str(source.get('path', '')).strip())} | "
            f"{_oracle_table_cell(str(source.get('role', '')).strip() or 'artifact')} | "
            f"{_oracle_table_cell(str(source.get('status', '')).strip() or 'unknown')} | "
            f"{_oracle_table_cell(str(source.get('reason', '')).strip() or 'Relevant continuation artifact.')} |"
        )

    appendix_blocks = "\n\n".join(
        str(source.get("appendix_block", "")).rstrip()
        for source in sources
        if str(source.get("appendix_block", "")).strip()
    ).strip()
    expert_review_lines = [
        "- This is the canonical Autolab escalation packet for the current scope.",
        "- The continuation packet and selected inline artifacts are rendered directly from current repo state.",
        "- Any downstream Oracle reply is advisory only and must be checked against repo state and tests before behavior changes.",
    ]
    if diagnostics:
        expert_review_lines.append(
            "- Export diagnostics: "
            + "; ".join(str(item).strip() for item in diagnostics)
        )
    else:
        expert_review_lines.append("- Export diagnostics: none.")
    return "\n".join(
        [
            "# Autolab Oracle",
            "",
            "## Summary",
            (
                "Autolab packaged the current continuation packet and the minimum inline artifacts "
                "needed for Oracle escalation."
            ),
            "",
            "## Continuation Packet",
            "```json",
            json.dumps(continuation_packet, indent=2, sort_keys=True),
            "```",
            "",
            "## Expert Review",
            *expert_review_lines,
            "",
            "## Recommended Next Steps",
            "- Use this packet as the canonical evidence bundle for Oracle escalation.",
            "- Keep the attached file set tight and verify any Oracle advice against repo state and tests.",
            "",
            "## Artifact Guide",
            *artifact_guide,
            "",
            "## Appendices",
            "",
            appendix_blocks,
        ]
    ).rstrip()


def _oracle_output_includes_source(output_text: str, source: dict[str, Any]) -> bool:
    appendix_block = str(source.get("appendix_block", "")).strip()
    if appendix_block and appendix_block in output_text:
        return True
    heading = str(source.get("appendix_heading", "")).strip()
    snippet = str(source.get("appendix_snippet", "")).strip()
    if heading and heading not in output_text:
        return False
    if snippet and snippet not in output_text:
        return False
    return bool(heading)


def _validate_oracle_output(
    output_text: str,
    *,
    sources: list[dict[str, Any]],
) -> str:
    required_sections = (
        "# Autolab Oracle",
        "## Summary",
        "## Continuation Packet",
        "## Expert Review",
        "## Recommended Next Steps",
        "## Artifact Guide",
        "## Appendices",
    )
    for marker in required_sections:
        if marker not in output_text:
            return f"oracle output missing required section '{marker}'"
    for source in sources:
        if not _oracle_output_includes_source(output_text, source):
            return (
                "oracle output omitted required appendix block for "
                f"{str(source.get('path', '')).strip() or 'unknown artifact'}"
            )
    return ""


def _apply_oracle_reply_text(
    *,
    state_path: Path,
    repo_root: Path,
    state: dict[str, Any],
    source_label: str,
    raw_notes: str,
) -> dict[str, Any]:
    from autolab.config import _load_oracle_apply_policy

    reply = parse_oracle_reply(raw_notes)
    iteration_id = str(state.get("iteration_id", "")).strip()
    experiment_id = str(state.get("experiment_id", "")).strip()
    current_stage = str(state.get("stage", "")).strip().lower()
    scope_kind = "experiment" if iteration_id and experiment_id else "project_wide"
    current_oracle_epoch = _resolve_current_oracle_epoch(
        state_path=state_path,
        repo_root=repo_root,
    )
    apply_policy = _load_oracle_apply_policy(
        repo_root,
        scope_kind=scope_kind,
        stage=current_stage,
    )
    context_resolution = resolve_context_sidecars(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        scope_kind=scope_kind,
    )
    discuss_paths = resolve_sidecar_output_paths(
        repo_root,
        scope_kind=scope_kind,
        sidecar_kind="discuss",
        iteration_id=iteration_id,
        experiment_id=experiment_id,
    )
    research_paths = resolve_sidecar_output_paths(
        repo_root,
        scope_kind=scope_kind,
        sidecar_kind="research",
        iteration_id=iteration_id,
        experiment_id=experiment_id,
    )
    existing_discuss = _load_json_if_exists(discuss_paths["json_path"])
    if not isinstance(existing_discuss, dict):
        existing_discuss = None
    existing_research = _load_json_if_exists(research_paths["json_path"])
    if not isinstance(existing_research, dict):
        existing_research = None

    active_campaign = _load_campaign(repo_root)
    campaign_summary = _campaign_summary(active_campaign) if active_campaign else {}
    disfavored_family = _oracle_reply_disfavored_family(campaign_summary, reply)

    discuss_dependency_refs = build_sidecar_dependency_refs(
        repo_root,
        context_resolution,
        exclude_paths={_sidecar_relpath(repo_root, discuss_paths["json_path"])},
    )
    research_dependency_refs = build_sidecar_dependency_refs(
        repo_root,
        context_resolution,
        exclude_paths={_sidecar_relpath(repo_root, research_paths["json_path"])},
    )

    discuss_updates = _oracle_reply_to_discuss_updates(reply, scope_kind=scope_kind)
    research_questions = _oracle_reply_to_research_questions(reply)
    campaign_feedback = _oracle_reply_to_campaign_feedback(reply)
    plan_note = _oracle_reply_plan_note(reply)
    todo_hints = _oracle_reply_to_todo_hints(
        reply,
        current_stage=current_stage,
        allow_rewind_design=apply_policy.allow_rewind_design,
        allow_request_human_review=apply_policy.allow_request_human_review,
        allow_stop_campaign=apply_policy.allow_stop_campaign,
    )
    if reply.verdict == "stop_campaign" and not apply_policy.allow_stop_campaign:
        campaign_feedback = [{**item, "signal": "none"} for item in campaign_feedback]

    changed_files: list[Path] = []
    discuss_payload = _build_oracle_apply_sidecar_payload(
        sidecar_kind="discuss",
        scope_kind=scope_kind,
        scope_root=discuss_paths["scope_root"],
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        dependency_refs=discuss_dependency_refs,
        existing_payload=existing_discuss,
    )
    discuss_added_total = 0
    for collection_name in _ORACLE_APPLY_DISCUSS_COLLECTIONS:
        additions = list(discuss_updates.get(collection_name, []))
        merged_entries, added_count = _merge_oracle_sidecar_entries(
            discuss_payload.get(collection_name, []),
            additions,
        )
        discuss_payload[collection_name] = merged_entries
        discuss_added_total += added_count
    if discuss_added_total > 0:
        _write_sidecar_outputs(
            repo_root=repo_root,
            sidecar_payload=discuss_payload,
            output_paths=discuss_paths,
        )
        changed_files.extend(
            [discuss_paths["json_path"], discuss_paths["markdown_path"]]
        )

    research_questions_added = 0
    if research_questions:
        research_payload = _build_oracle_apply_sidecar_payload(
            sidecar_kind="research",
            scope_kind=scope_kind,
            scope_root=research_paths["scope_root"],
            iteration_id=iteration_id,
            experiment_id=experiment_id,
            dependency_refs=research_dependency_refs,
            existing_payload=existing_research,
        )
        merged_questions, research_questions_added = _merge_oracle_sidecar_entries(
            research_payload.get("questions", []),
            research_questions,
        )
        research_payload["questions"] = merged_questions
        if research_questions_added > 0:
            _write_sidecar_outputs(
                repo_root=repo_root,
                sidecar_payload=research_payload,
                output_paths=research_paths,
            )
            changed_files.extend(
                [research_paths["json_path"], research_paths["markdown_path"]]
            )

    todo_changed_files, todo_added = _apply_oracle_todo_hints(
        repo_root,
        state=state,
        todo_hints=todo_hints,
    )
    changed_files.extend(todo_changed_files)

    campaign_feedback_added = 0
    ignored_campaign_feedback = 0
    campaign_stopped = False
    campaign_status = str(campaign_summary.get("status", "")).strip()
    if active_campaign is not None:
        before_feedback_count = len(active_campaign.get("oracle_feedback", []))
        before_status = str(active_campaign.get("status", "")).strip()
        updated_campaign = active_campaign
        for feedback in campaign_feedback:
            maybe_updated = _append_campaign_oracle_feedback(
                repo_root,
                source=source_label,
                summary=str(feedback.get("summary", "")).strip(),
                detail=str(feedback.get("detail", "")).strip(),
                signal=str(feedback.get("signal", "")).strip(),
            )
            if isinstance(maybe_updated, dict):
                updated_campaign = maybe_updated
        if (
            isinstance(updated_campaign, dict)
            and reply.verdict == "stop_campaign"
            and apply_policy.allow_stop_campaign
        ):
            updated_campaign["status"] = "stopped"
            _write_campaign(repo_root, updated_campaign)
            campaign_stopped = True
        if isinstance(updated_campaign, dict):
            campaign_status = str(updated_campaign.get("status", "")).strip()
            after_feedback_count = len(updated_campaign.get("oracle_feedback", []))
            campaign_feedback_added = max(
                0, int(after_feedback_count) - int(before_feedback_count)
            )
            if campaign_feedback_added > 0 or campaign_status != before_status:
                changed_files.append(repo_root / ".autolab" / "campaign.json")
    else:
        ignored_campaign_feedback = len(campaign_feedback)

    plan_approval_updated = False
    if scope_kind == "experiment":
        iteration_dir, _iteration_type = _resolve_iteration_directory(
            repo_root,
            iteration_id=iteration_id,
            experiment_id=experiment_id,
            require_exists=False,
        )
    else:
        iteration_dir = None
    if (
        plan_note
        and iteration_dir is not None
        and (iteration_dir / "plan_approval.json").exists()
    ):
        before_payload = load_plan_approval(iteration_dir)
        before_notes = str(before_payload.get("notes", "")).strip()
        after_payload = append_plan_approval_note(
            iteration_dir,
            note=plan_note,
            source_label="oracle",
        )
        after_notes = str(after_payload.get("notes", "")).strip()
        if after_notes != before_notes:
            plan_approval_updated = True
            changed_files.extend(
                [
                    iteration_dir / "plan_approval.json",
                    iteration_dir / "plan_approval.md",
                ]
            )

    oracle_state = load_oracle_state(repo_root)
    if current_oracle_epoch:
        oracle_state["current_epoch"] = current_oracle_epoch
    oracle_state["verdict"] = reply.verdict
    oracle_state["suggested_next_action"] = (
        reply.suggested_next_action
        or oracle_default_suggested_next_action(reply.verdict)
    )
    oracle_state["recommended_human_review"] = (
        reply.recommended_human_review and apply_policy.allow_request_human_review
    )
    oracle_state["disfavored_family"] = (
        disfavored_family if apply_policy.allow_switch_family else ""
    )
    changed_files.append(write_oracle_state(repo_root, oracle_state))

    handoff_warning = ""
    try:
        handoff_artifacts = refresh_handoff(state_path)
    except Exception as exc:
        handoff_warning = str(exc)
    else:
        changed_files.extend(
            [
                handoff_artifacts.handoff_json_path,
                handoff_artifacts.handoff_md_path,
            ]
        )

    changed_files = list(dict.fromkeys(changed_files))
    summary = f"Applied Oracle verdict: {reply.verdict}"
    _persist_agent_result(
        repo_root,
        status="complete",
        summary=summary,
        changed_files=changed_files,
    )
    _append_log(
        repo_root,
        (
            "oracle apply: "
            f"source={source_label} "
            f"verdict={reply.verdict} "
            f"discuss={discuss_added_total} "
            f"research_questions={research_questions_added} "
            f"todo={todo_added} "
            f"campaign_feedback={campaign_feedback_added} "
            f"campaign_status={campaign_status or 'none'} "
            f"plan_approval_note={plan_approval_updated}"
        ),
    )
    return {
        "reply": reply,
        "summary": summary,
        "changed_files": changed_files,
        "discuss_updates": discuss_added_total,
        "research_questions_added": research_questions_added,
        "todo_added": todo_added,
        "campaign_feedback_added": campaign_feedback_added,
        "ignored_campaign_feedback": ignored_campaign_feedback,
        "campaign_stopped": campaign_stopped,
        "plan_approval_updated": plan_approval_updated,
        "campaign_status": campaign_status,
        "handoff_warning": handoff_warning,
        "recommended_human_review": oracle_state["recommended_human_review"],
    }


def _cmd_oracle_apply(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        print(f"autolab oracle apply: ERROR {exc}", file=sys.stderr)
        return 1

    try:
        source_label, raw_notes = _read_oracle_apply_input(args, repo_root=repo_root)
    except Exception as exc:
        print(f"autolab oracle apply: ERROR {exc}", file=sys.stderr)
        return 1

    try:
        result = _apply_oracle_reply_text(
            state_path=state_path,
            repo_root=repo_root,
            state=state,
            source_label=source_label,
            raw_notes=raw_notes,
        )
    except Exception as exc:
        print(f"autolab oracle apply: ERROR {exc}", file=sys.stderr)
        return 1

    if result["handoff_warning"]:
        print(
            "autolab oracle apply: WARN failed to refresh handoff snapshot: "
            f"{result['handoff_warning']}",
            file=sys.stderr,
        )

    print("autolab oracle apply")
    print(f"state_file: {state_path}")
    print(f"input_source: {source_label}")
    print(f"summary: {result['summary']}")
    print(f"oracle_verdict: {result['reply'].verdict}")
    print(f"oracle_recommended_action: {result['reply'].suggested_next_action}")
    print(f"discuss_updates: {result['discuss_updates']}")
    print(f"research_questions_added: {result['research_questions_added']}")
    print(f"todo_added: {result['todo_added']}")
    print(f"campaign_feedback_added: {result['campaign_feedback_added']}")
    print(f"ignored_campaign_feedback: {result['ignored_campaign_feedback']}")
    print(f"plan_approval_updated: {result['plan_approval_updated']}")
    if result["campaign_status"]:
        print(f"campaign_status: {result['campaign_status']}")
    print(f"changed_files: {len(result['changed_files'])}")
    return 0


def _export_oracle_document(
    *,
    state_path: Path,
    repo_root: Path,
    timeout_seconds: float,
    output_path: Path | None = None,
    handoff_payload: dict[str, Any] | None = None,
) -> tuple[Path, int, str]:
    if handoff_payload is None:
        try:
            artifacts = refresh_handoff(state_path)
        except Exception as exc:
            raise RuntimeError(f"failed to refresh handoff: {exc}") from exc
        handoff_payload = artifacts.payload
    continuation_packet = handoff_payload.get("continuation_packet")
    if not isinstance(continuation_packet, dict):
        raise RuntimeError("handoff continuation_packet is missing")

    sources, diagnostics = _oracle_collect_sources(
        repo_root=repo_root,
        handoff_payload=handoff_payload,
    )
    _ = timeout_seconds
    rendered_document = _render_oracle_document(
        continuation_packet=continuation_packet,
        sources=sources,
        diagnostics=diagnostics,
    )
    validation_error = _validate_oracle_output(rendered_document, sources=sources)
    if validation_error:
        raise RuntimeError(validation_error)

    resolved_output_path = output_path
    if resolved_output_path is None:
        scope_root = Path(
            str(handoff_payload.get("scope_root", "")).strip() or repo_root
        )
        if not scope_root.is_absolute():
            scope_root = (repo_root / scope_root).resolve(strict=False)
        else:
            scope_root = scope_root.resolve(strict=False)
        resolved_output_path = (scope_root / "oracle.md").resolve(strict=False)
    else:
        if not resolved_output_path.is_absolute():
            resolved_output_path = (repo_root / resolved_output_path).resolve(
                strict=False
            )
        else:
            resolved_output_path = resolved_output_path.resolve(strict=False)

    if not _docs_path_within_repo_root(repo_root, resolved_output_path):
        raise RuntimeError(
            f"output path resolves outside repository root: {resolved_output_path}"
        )

    try:
        resolved_output_path.parent.mkdir(parents=True, exist_ok=True)
        resolved_output_path.write_text(
            rendered_document.rstrip() + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        raise RuntimeError(f"failed writing oracle document: {exc}") from exc

    _mark_campaign_oracle_exported(repo_root)
    return (resolved_output_path, len(sources), "internal-render")


def _oracle_epoch_from_handoff_payload(handoff_payload: dict[str, Any]) -> str:
    continuation_packet = handoff_payload.get("continuation_packet")
    if not isinstance(continuation_packet, dict):
        return ""
    return str(continuation_packet.get("oracle_epoch", "")).strip()


def _load_existing_handoff_payload(repo_root: Path) -> dict[str, Any]:
    payload = _load_json_if_exists(repo_root / ".autolab" / "handoff.json")
    if not isinstance(payload, dict):
        return {}
    return payload


def _resolve_current_oracle_epoch(
    *,
    state_path: Path,
    repo_root: Path,
) -> str:
    try:
        handoff_payload = refresh_handoff(state_path).payload
    except Exception:
        handoff_payload = _load_existing_handoff_payload(repo_root)
    return _oracle_epoch_from_handoff_payload(handoff_payload)


@lru_cache(maxsize=1)
def _oracle_cli_help_text() -> str:
    if not shutil.which("oracle"):
        return ""
    outputs: list[str] = []
    for argv in (["oracle", "--help"], ["oracle", "--debug-help"]):
        try:
            process = subprocess.run(
                argv,
                text=True,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except Exception:
            continue
        for part in (process.stdout or "", process.stderr or ""):
            text = str(part).strip()
            if text:
                outputs.append(text)
    return "\n\n".join(outputs)


def _oracle_cli_supports_flag(flag: str) -> bool:
    return flag in _oracle_cli_help_text()


def _build_oracle_browser_argv(
    *,
    prompt_text: str,
    oracle_bundle_path: Path,
    preview: bool,
    reply_output_path: Path | None,
    timeout_seconds: float,
    browser_model_strategy: str,
    browser_auto_reattach_delay: str,
    browser_auto_reattach_interval: str,
    browser_auto_reattach_timeout: str,
) -> tuple[list[str], str]:
    argv = ["oracle"]
    if preview:
        argv.extend(["--dry-run", "summary", "--files-report"])
    else:
        argv.extend(["--engine", "browser"])
        if _oracle_cli_supports_flag("--browser-model-strategy"):
            argv.extend(["--browser-model-strategy", browser_model_strategy])
        if _oracle_cli_supports_flag("--browser-manual-login"):
            argv.append("--browser-manual-login")
        if _oracle_cli_supports_flag("--browser-auto-reattach-delay"):
            argv.extend(["--browser-auto-reattach-delay", browser_auto_reattach_delay])
        if _oracle_cli_supports_flag("--browser-auto-reattach-interval"):
            argv.extend(
                ["--browser-auto-reattach-interval", browser_auto_reattach_interval]
            )
        if _oracle_cli_supports_flag("--browser-auto-reattach-timeout"):
            argv.extend(
                ["--browser-auto-reattach-timeout", browser_auto_reattach_timeout]
            )
        if _oracle_cli_supports_flag("--timeout"):
            argv.extend(["--timeout", f"{max(1, int(timeout_seconds))}"])
        if _oracle_cli_supports_flag("--wait"):
            argv.append("--wait")
        if reply_output_path is not None and _oracle_cli_supports_flag(
            "--write-output"
        ):
            argv.extend(["--write-output", str(reply_output_path)])
    argv.extend(
        [
            "--prompt",
            prompt_text,
            "--file",
            str(oracle_bundle_path),
        ]
    )
    return (argv, " ".join(shlex.quote(token) for token in argv))


def _run_oracle_browser_cli(
    *,
    repo_root: Path,
    argv: list[str],
    timeout_seconds: float,
) -> tuple[int, str, str, str]:
    command_display = " ".join(shlex.quote(token) for token in argv)
    try:
        process = subprocess.run(
            argv,
            cwd=repo_root,
            text=True,
            capture_output=True,
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


def _run_oracle_roundtrip_auto(
    *,
    state_path: Path,
    repo_root: Path,
    trigger_reason: str,
    output_path: Path | None = None,
) -> dict[str, Any]:
    def _result(
        *,
        exit_code: int,
        attempted: bool,
        status: str,
        failure_reason: str,
        output_path_text: str,
        reply_path_text: str,
        export_command_text: str,
        browser_command_text: str,
        source_count_value: int,
        apply_status: str,
    ) -> dict[str, Any]:
        return {
            "exit_code": exit_code,
            "attempted": attempted,
            "status": status,
            "failure_reason": failure_reason,
            "output_path": output_path_text,
            "reply_path": reply_path_text,
            "export_command": export_command_text,
            "browser_command": browser_command_text,
            "source_count": source_count_value,
            "apply_status": apply_status,
        }

    handoff_payload: dict[str, Any]
    try:
        handoff_payload = refresh_handoff(state_path).payload
    except Exception as exc:
        fallback_handoff_payload = _load_existing_handoff_payload(repo_root)
        fallback_epoch = _oracle_epoch_from_handoff_payload(fallback_handoff_payload)
        if fallback_epoch:
            finish_oracle_attempt(
                repo_root,
                epoch=fallback_epoch,
                eligible=True,
                status="preview_failed",
                trigger_reason=trigger_reason,
                failure_reason=f"failed to refresh handoff before oracle export: {exc}",
            )
        return _result(
            exit_code=1,
            attempted=bool(fallback_epoch),
            status="preview_failed",
            failure_reason=f"failed to refresh handoff before oracle export: {exc}",
            output_path_text="",
            reply_path_text="",
            export_command_text="",
            browser_command_text="",
            source_count_value=0,
            apply_status="",
        )

    continuation_packet = handoff_payload.get("continuation_packet")
    epoch = _oracle_epoch_from_handoff_payload(handoff_payload)
    if not isinstance(continuation_packet, dict):
        if epoch:
            finish_oracle_attempt(
                repo_root,
                epoch=epoch,
                eligible=True,
                status="preview_failed",
                trigger_reason=trigger_reason,
                failure_reason="handoff continuation_packet is missing",
            )
        return _result(
            exit_code=1,
            attempted=bool(epoch),
            status="preview_failed",
            failure_reason="handoff continuation_packet is missing",
            output_path_text="",
            reply_path_text="",
            export_command_text="",
            browser_command_text="",
            source_count_value=0,
            apply_status="",
        )

    active_stage = continuation_packet.get("active_stage")
    if not isinstance(active_stage, dict):
        active_stage = {}
    stage_name = str(active_stage.get("stage", "")).strip()
    scope_kind = str(active_stage.get("scope_kind", "")).strip() or "experiment"
    allowed, oracle_policy = oracle_stage_auto_allowed(
        repo_root,
        stage=stage_name,
        scope_kind=scope_kind,
    )
    epoch_exhausted = bool(continuation_packet.get("oracle_epoch_exhausted", False))

    output_oracle_path = ""
    export_command = ""
    source_count = 0
    if not allowed or epoch_exhausted:
        return _result(
            exit_code=0,
            attempted=False,
            status="not_attempted",
            failure_reason=(
                "oracle auto is disabled by policy"
                if not allowed
                else "automatic oracle already attempted for this oracle epoch"
            ),
            output_path_text=output_oracle_path,
            reply_path_text="",
            export_command_text=export_command,
            browser_command_text="",
            source_count_value=source_count,
            apply_status="",
        )

    try:
        output_oracle_resolved, source_count, export_command = _export_oracle_document(
            state_path=state_path,
            repo_root=repo_root,
            timeout_seconds=240.0,
            output_path=output_path,
            handoff_payload=handoff_payload,
        )
        output_oracle_path = str(output_oracle_resolved)
    except Exception as exc:
        finish_oracle_attempt(
            repo_root,
            epoch=epoch,
            eligible=True,
            status="preview_failed",
            trigger_reason=trigger_reason,
            failure_reason=str(exc),
        )
        _safe_refresh_handoff(state_path)
        return _result(
            exit_code=1,
            attempted=True,
            status="preview_failed",
            failure_reason=str(exc),
            output_path_text=output_oracle_path,
            reply_path_text="",
            export_command_text=export_command,
            browser_command_text="",
            source_count_value=source_count,
            apply_status="",
        )

    if not shutil.which("oracle"):
        finish_oracle_attempt(
            repo_root,
            epoch=epoch,
            eligible=True,
            status="unavailable",
            trigger_reason=trigger_reason,
            failure_reason="oracle executable is not available on PATH",
        )
        _safe_refresh_handoff(state_path)
        return _result(
            exit_code=1,
            attempted=True,
            status="unavailable",
            failure_reason="oracle executable is not available on PATH",
            output_path_text=output_oracle_path,
            reply_path_text="",
            export_command_text=export_command,
            browser_command_text="",
            source_count_value=source_count,
            apply_status="",
        )
    if (
        oracle_policy.browser_manual_login_profile_required
        and not oracle_profile_ready()
    ):
        finish_oracle_attempt(
            repo_root,
            epoch=epoch,
            eligible=True,
            status="unavailable",
            trigger_reason=trigger_reason,
            failure_reason="oracle browser profile is not available",
        )
        _safe_refresh_handoff(state_path)
        return _result(
            exit_code=1,
            attempted=True,
            status="unavailable",
            failure_reason="oracle browser profile is not available",
            output_path_text=output_oracle_path,
            reply_path_text="",
            export_command_text=export_command,
            browser_command_text="",
            source_count_value=source_count,
            apply_status="",
        )

    start_oracle_attempt(
        repo_root,
        epoch=epoch,
        eligible=True,
        trigger_reason=trigger_reason,
    )
    request_text = build_oracle_roundtrip_request(
        handoff_payload=handoff_payload,
        trigger_reason=trigger_reason,
    )
    browser_command = ""
    timeout_seconds = float(oracle_policy.timeout_minutes) * 60.0
    reply_capture_path: Path | None = None
    try:
        if oracle_policy.preview_before_send:
            preview_argv, preview_display = _build_oracle_browser_argv(
                prompt_text=request_text,
                oracle_bundle_path=Path(output_oracle_path),
                preview=True,
                reply_output_path=None,
                timeout_seconds=min(timeout_seconds, 300.0),
                browser_model_strategy=oracle_policy.browser_model_strategy,
                browser_auto_reattach_delay=oracle_policy.browser_auto_reattach_delay,
                browser_auto_reattach_interval=oracle_policy.browser_auto_reattach_interval,
                browser_auto_reattach_timeout=oracle_policy.browser_auto_reattach_timeout,
            )
            preview_code, preview_stdout, preview_stderr, browser_command = (
                _run_oracle_browser_cli(
                    repo_root=repo_root,
                    argv=preview_argv,
                    timeout_seconds=min(timeout_seconds, 300.0),
                )
            )
            if preview_code != 0:
                detail = (
                    preview_stderr.strip() or preview_stdout.strip() or "preview failed"
                )
                finish_oracle_attempt(
                    repo_root,
                    epoch=epoch,
                    eligible=True,
                    status="preview_failed",
                    trigger_reason=trigger_reason,
                    failure_reason=detail,
                )
                _safe_refresh_handoff(state_path)
                return _result(
                    exit_code=1,
                    attempted=True,
                    status="preview_failed",
                    failure_reason=detail,
                    output_path_text=output_oracle_path,
                    reply_path_text="",
                    export_command_text=export_command,
                    browser_command_text=preview_display,
                    source_count_value=source_count,
                    apply_status="",
                )

        with tempfile.NamedTemporaryFile(
            suffix=".md",
            prefix="autolab_oracle_response_",
            delete=False,
        ) as handle:
            reply_capture_path = Path(handle.name)
        browser_argv, browser_command = _build_oracle_browser_argv(
            prompt_text=request_text,
            oracle_bundle_path=Path(output_oracle_path),
            preview=False,
            reply_output_path=reply_capture_path,
            timeout_seconds=timeout_seconds,
            browser_model_strategy=oracle_policy.browser_model_strategy,
            browser_auto_reattach_delay=oracle_policy.browser_auto_reattach_delay,
            browser_auto_reattach_interval=oracle_policy.browser_auto_reattach_interval,
            browser_auto_reattach_timeout=oracle_policy.browser_auto_reattach_timeout,
        )
        run_code, run_stdout, run_stderr, browser_command = _run_oracle_browser_cli(
            repo_root=repo_root,
            argv=browser_argv,
            timeout_seconds=timeout_seconds,
        )
        reply_text = ""
        if reply_capture_path.exists():
            try:
                reply_text = reply_capture_path.read_text(encoding="utf-8").strip()
            except UnicodeDecodeError:
                reply_text = reply_capture_path.read_text(
                    encoding="utf-8",
                    errors="replace",
                ).strip()
        if not reply_text:
            reply_text = run_stdout.strip()
    finally:
        if reply_capture_path is not None:
            try:
                reply_capture_path.unlink()
            except Exception:
                pass

    if run_code == 124:
        finish_oracle_attempt(
            repo_root,
            epoch=epoch,
            eligible=True,
            status="timeout",
            trigger_reason=trigger_reason,
            failure_reason=run_stderr or "timed out",
        )
        _safe_refresh_handoff(state_path)
        return _result(
            exit_code=1,
            attempted=True,
            status="timeout",
            failure_reason=run_stderr or "timed out",
            output_path_text=output_oracle_path,
            reply_path_text="",
            export_command_text=export_command,
            browser_command_text=browser_command,
            source_count_value=source_count,
            apply_status="",
        )
    if run_code != 0:
        detail = (
            run_stderr.strip() or run_stdout.strip() or "oracle browser launch failed"
        )
        failure_status = (
            "session_lost"
            if any(
                token in detail.lower() for token in ("session", "reattach", "detached")
            )
            else "launch_failed"
        )
        finish_oracle_attempt(
            repo_root,
            epoch=epoch,
            eligible=True,
            status=failure_status,
            trigger_reason=trigger_reason,
            failure_reason=detail,
        )
        _safe_refresh_handoff(state_path)
        return _result(
            exit_code=1,
            attempted=True,
            status=failure_status,
            failure_reason=detail,
            output_path_text=output_oracle_path,
            reply_path_text="",
            export_command_text=export_command,
            browser_command_text=browser_command,
            source_count_value=source_count,
            apply_status="",
        )
    if not reply_text.strip():
        finish_oracle_attempt(
            repo_root,
            epoch=epoch,
            eligible=True,
            status="capture_failed",
            trigger_reason=trigger_reason,
            failure_reason="oracle browser run completed without a captured reply",
        )
        _safe_refresh_handoff(state_path)
        return _result(
            exit_code=1,
            attempted=True,
            status="capture_failed",
            failure_reason="oracle browser run completed without a captured reply",
            output_path_text=output_oracle_path,
            reply_path_text="",
            export_command_text=export_command,
            browser_command_text=browser_command,
            source_count_value=source_count,
            apply_status="",
        )

    reply_path = write_oracle_last_response(repo_root, reply_text)
    reply_relpath = _sidecar_relpath(repo_root, reply_path)

    if not oracle_policy.apply_on_success:
        try:
            parsed_reply = parse_oracle_reply(reply_text)
        except ValueError as exc:
            finish_oracle_attempt(
                repo_root,
                epoch=epoch,
                eligible=True,
                status="parse_failed",
                trigger_reason=trigger_reason,
                failure_reason=str(exc),
                reply_path=reply_relpath,
                apply_status="not_applied",
            )
            _safe_refresh_handoff(state_path)
            return _result(
                exit_code=1,
                attempted=True,
                status="parse_failed",
                failure_reason=str(exc),
                output_path_text=output_oracle_path,
                reply_path_text=str(reply_path),
                export_command_text=export_command,
                browser_command_text=browser_command,
                source_count_value=source_count,
                apply_status="not_applied",
            )
        current_campaign = _load_campaign(repo_root)
        finish_oracle_attempt(
            repo_root,
            epoch=epoch,
            eligible=True,
            status="succeeded",
            trigger_reason=trigger_reason,
            reply_path=reply_relpath,
            apply_status="not_applied",
            verdict=parsed_reply.verdict,
            suggested_next_action=parsed_reply.suggested_next_action,
            recommended_human_review=parsed_reply.recommended_human_review,
            disfavored_family=_oracle_reply_disfavored_family(
                _campaign_summary(current_campaign) if current_campaign else {},
                parsed_reply,
            ),
        )
        _safe_refresh_handoff(state_path)
        return _result(
            exit_code=0,
            attempted=True,
            status="succeeded",
            failure_reason="",
            output_path_text=output_oracle_path,
            reply_path_text=str(reply_path),
            export_command_text=export_command,
            browser_command_text=browser_command,
            source_count_value=source_count,
            apply_status="not_applied",
        )

    try:
        apply_result = _apply_oracle_reply_text(
            state_path=state_path,
            repo_root=repo_root,
            state=_normalize_state(_load_state(state_path)),
            source_label=reply_relpath,
            raw_notes=reply_text,
        )
    except ValueError as exc:
        finish_oracle_attempt(
            repo_root,
            epoch=epoch,
            eligible=True,
            status="parse_failed",
            trigger_reason=trigger_reason,
            failure_reason=str(exc),
            reply_path=reply_relpath,
            apply_status="not_applied",
        )
        _safe_refresh_handoff(state_path)
        return _result(
            exit_code=1,
            attempted=True,
            status="parse_failed",
            failure_reason=str(exc),
            output_path_text=output_oracle_path,
            reply_path_text=str(reply_path),
            export_command_text=export_command,
            browser_command_text=browser_command,
            source_count_value=source_count,
            apply_status="not_applied",
        )
    except Exception as exc:
        finish_oracle_attempt(
            repo_root,
            epoch=epoch,
            eligible=True,
            status="apply_failed",
            trigger_reason=trigger_reason,
            failure_reason=str(exc),
            reply_path=reply_relpath,
            apply_status="failed",
        )
        _safe_refresh_handoff(state_path)
        return _result(
            exit_code=1,
            attempted=True,
            status="apply_failed",
            failure_reason=str(exc),
            output_path_text=output_oracle_path,
            reply_path_text=str(reply_path),
            export_command_text=export_command,
            browser_command_text=browser_command,
            source_count_value=source_count,
            apply_status="failed",
        )

    apply_status = _oracle_reply_apply_status_with_effects(
        apply_result["reply"],
        campaign_stopped=bool(apply_result["campaign_stopped"]),
        recommended_human_review=bool(apply_result["recommended_human_review"]),
    )
    finish_oracle_attempt(
        repo_root,
        epoch=epoch,
        eligible=True,
        status="succeeded",
        trigger_reason=trigger_reason,
        reply_path=reply_relpath,
        apply_status=apply_status,
        verdict=apply_result["reply"].verdict,
        suggested_next_action=apply_result["reply"].suggested_next_action,
        recommended_human_review=bool(apply_result["recommended_human_review"]),
        disfavored_family=str(
            load_oracle_state(repo_root).get("disfavored_family", "")
        ).strip(),
    )
    _safe_refresh_handoff(state_path)
    return _result(
        exit_code=0,
        attempted=True,
        status="succeeded",
        failure_reason="",
        output_path_text=output_oracle_path,
        reply_path_text=str(reply_path),
        export_command_text=export_command,
        browser_command_text=browser_command,
        source_count_value=source_count,
        apply_status=apply_status,
    )


def _cmd_oracle_roundtrip(args: argparse.Namespace) -> int:
    if not bool(getattr(args, "auto", False)):
        print(
            "autolab oracle roundtrip: ERROR --auto is required for this command",
            file=sys.stderr,
        )
        return 1
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    output_text = str(getattr(args, "output", "") or "").strip()
    requested_output_path = Path(output_text).expanduser() if output_text else None
    result = _run_oracle_roundtrip_auto(
        state_path=state_path,
        repo_root=repo_root,
        trigger_reason="manual automation request",
        output_path=requested_output_path,
    )
    print("autolab oracle roundtrip")
    print(f"state_file: {state_path}")
    print(f"status: {result['status']}")
    print(f"attempted: {result['attempted']}")
    print(f"output_path: {result['output_path']}")
    print(f"reply_path: {result['reply_path'] or 'none'}")
    print(f"artifacts_inlined: {result['source_count']}")
    print(f"apply_status: {result['apply_status'] or 'not_applied'}")
    print(f"oracle_export_command: {result['export_command'] or '-'}")
    print(f"oracle_browser_command: {result['browser_command'] or '-'}")
    if result["failure_reason"]:
        print(f"failure_reason: {result['failure_reason']}")
    return int(result["exit_code"])


def _cmd_oracle(args: argparse.Namespace) -> int:
    oracle_command = str(getattr(args, "oracle_command", "") or "").strip().lower()
    if oracle_command == "roundtrip":
        return _cmd_oracle_roundtrip(args)
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)

    try:
        timeout_seconds = float(getattr(args, "timeout_seconds", 240.0))
    except Exception:
        print(
            "autolab oracle: ERROR --timeout-seconds must be a number",
            file=sys.stderr,
        )
        return 1
    if timeout_seconds <= 0:
        print(
            "autolab oracle: ERROR --timeout-seconds must be > 0",
            file=sys.stderr,
        )
        return 1

    output_text = str(getattr(args, "output", "") or "").strip()
    if output_text:
        requested_output_path = Path(output_text).expanduser()
    else:
        requested_output_path = None

    try:
        output_path, source_count, command_display = _export_oracle_document(
            state_path=state_path,
            repo_root=repo_root,
            timeout_seconds=timeout_seconds,
            output_path=requested_output_path,
        )
    except Exception as exc:
        print(
            f"autolab oracle: ERROR {exc}",
            file=sys.stderr,
        )
        return 1

    print("autolab oracle")
    print(f"state_file: {state_path}")
    print(f"output_path: {output_path}")
    print(f"artifacts_inlined: {source_count}")
    print(f"llm_command: {command_display}")
    return 0


__all__ = [name for name in globals() if not name.startswith("__")]
