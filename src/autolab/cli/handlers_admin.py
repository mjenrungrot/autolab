"""Policy explain/docs/report command handlers."""

from __future__ import annotations

from autolab.cli.support import *


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

    # Resolve prompt file paths
    prompt_path = repo_root / ".autolab" / "prompts" / spec.prompt_file
    runner_prompt_path = prompt_path
    try:
        runner_prompt_path = _resolve_stage_prompt_path(
            repo_root, stage_name, prompt_role="runner"
        )
    except StageCheckError:
        runner_prompt_path = prompt_path

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
            "prompt_file": spec.prompt_file,
            "resolved_prompt_path": resolved_prompt_path,
            "runner_prompt_file": spec.runner_prompt_file or None,
            "resolved_runner_prompt_path": resolved_runner_prompt_path,
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
        print(f"prompt_file: {spec.prompt_file}")
        print(f"resolved_prompt_path: {resolved_prompt_path}")
        if spec.runner_prompt_file:
            print(f"runner_prompt_file: {spec.runner_prompt_file}")
            print(f"resolved_runner_prompt_path: {resolved_runner_prompt_path}")
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


def _cmd_docs_generate(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    registry = load_registry(repo_root)

    if not registry:
        print(
            "autolab docs generate: ERROR could not load workflow.yaml registry",
            file=sys.stderr,
        )
        return 1

    # 1. Stage flow diagram
    print("# Autolab Stage Flow")
    print("")
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
    print(" | ".join(flow_parts))
    print("")

    # 2. Artifact map
    print("## Artifact Map")
    print("")
    print("| Stage | Required Outputs |")
    print("|-------|-----------------|")
    for name, spec in registry.items():
        outputs_parts: list[str] = []
        if spec.required_outputs:
            outputs_parts.append(", ".join(spec.required_outputs))
        for group in spec.required_outputs_any_of:
            outputs_parts.append(f"one-of({', '.join(group)})")
        for conditions, outputs in spec.required_outputs_if:
            condition_text = ", ".join(f"{key}={value}" for key, value in conditions)
            outputs_parts.append(f"when {condition_text}: {', '.join(outputs)}")
        outputs = "; ".join(outputs_parts) if outputs_parts else "(none)"
        print(f"| {name} | {outputs} |")
    print("")

    # 3. Token reference
    print("## Token Reference")
    print("")
    print("| Stage | Required Tokens |")
    print("|-------|----------------|")
    for name, spec in registry.items():
        tokens = (
            ", ".join(sorted(spec.required_tokens))
            if spec.required_tokens
            else "(none)"
        )
        print(f"| {name} | {tokens} |")
    print("")

    # 4. Classifications
    print("## Classifications")
    print("")
    print("| Stage | Active | Terminal | Decision | Runner Eligible |")
    print("|-------|--------|----------|----------|----------------|")
    for name, spec in registry.items():
        print(
            f"| {name} | {spec.is_active} | {spec.is_terminal} | {spec.is_decision} | {spec.is_runner_eligible} |"
        )

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
