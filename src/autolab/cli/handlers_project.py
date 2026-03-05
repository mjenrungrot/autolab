"""Project lifecycle, maintenance, and setup CLI handlers."""

from __future__ import annotations

from autolab.cli.support import *


def _cmd_guardrails(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)

    try:
        state = _load_state(state_path)
    except RuntimeError as exc:
        print(f"autolab guardrails: ERROR {exc}", file=sys.stderr)
        return 1

    try:
        guardrail_cfg = _load_guardrail_config(repo_root)
    except Exception as exc:
        print(
            f"autolab guardrails: ERROR loading guardrail config: {exc}",
            file=sys.stderr,
        )
        return 1

    repeat_guard = state.get("repeat_guard", {})
    if not isinstance(repeat_guard, dict):
        repeat_guard = {}

    print("autolab guardrails")
    print(f"state_file: {state_path}")
    print(f"on_breach: {guardrail_cfg.on_breach}")
    print("")

    # Define the counter/threshold pairs
    counters = [
        (
            "same_decision_streak",
            int(repeat_guard.get("same_decision_streak", 0)),
            guardrail_cfg.max_same_decision_streak,
        ),
        (
            "no_progress_decisions",
            int(repeat_guard.get("no_progress_decisions", 0)),
            guardrail_cfg.max_no_progress_decisions,
        ),
        (
            "update_docs_cycle_count",
            int(repeat_guard.get("update_docs_cycle_count", 0)),
            guardrail_cfg.max_update_docs_cycles,
        ),
    ]

    print("guardrail counters:")
    for name, current, threshold in counters:
        distance = threshold - current
        breach_marker = " [BREACHED]" if distance <= 0 else ""
        print(f"  {name}: {current}/{threshold} (distance: {distance}){breach_marker}")

    print(f"  max_generated_todo_tasks: {guardrail_cfg.max_generated_todo_tasks}")

    # Additional repeat_guard state
    last_decision = str(repeat_guard.get("last_decision", "")).strip()
    last_verification = repeat_guard.get("last_verification_passed", False)
    print("")
    print(f"last_decision: {last_decision or '<none>'}")
    print(f"last_verification_passed: {last_verification}")

    # Show meaningful-change config if available
    try:
        meaningful_cfg = _load_meaningful_change_config(repo_root)
        print("")
        print("meaningful_change config:")
        print(f"  require_verification: {meaningful_cfg.require_verification}")
        print(
            f"  require_implementation_progress: {meaningful_cfg.require_implementation_progress}"
        )
        print(f"  require_git_for_progress: {meaningful_cfg.require_git_for_progress}")
        print(f"  on_non_git_behavior: {meaningful_cfg.on_non_git_behavior}")
        print(f"  exclude_paths: {list(meaningful_cfg.exclude_paths)}")
        print(
            "  require_non_review_progress_in_implementation_cycle: "
            f"{meaningful_cfg.require_non_review_progress_in_implementation_cycle}"
        )
        print(
            "  implementation_cycle_exclude_paths: "
            f"{list(meaningful_cfg.implementation_cycle_exclude_paths)}"
        )
    except Exception:
        pass

    return 0


def _cmd_configure(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    check_only = bool(args.check)

    print("autolab configure")
    print(f"state_file: {state_path}")
    print(f"check_only: {check_only}")
    print("")

    all_pass = True
    has_warn = False

    # 1. Check .autolab/ directory exists
    if autolab_dir.exists() and autolab_dir.is_dir():
        print(f"  [PASS] .autolab directory: {autolab_dir}")
    else:
        print(f"  [FAIL] .autolab directory: not found at {autolab_dir}")
        print("         Run `autolab init` to create the project scaffold.")
        all_pass = False

    # 2. Check verifier_policy.yaml exists and is valid YAML
    policy_path = autolab_dir / "verifier_policy.yaml"
    policy: dict[str, Any] = {}
    if policy_path.exists():
        if _yaml_mod is not None:
            try:
                loaded = _yaml_mod.safe_load(policy_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    policy = loaded
                    print(f"  [PASS] verifier_policy.yaml: valid ({policy_path})")
                else:
                    print(
                        f"  [FAIL] verifier_policy.yaml: not a valid YAML mapping ({policy_path})"
                    )
                    all_pass = False
            except Exception as exc:
                print(f"  [FAIL] verifier_policy.yaml: parse error: {exc}")
                all_pass = False
        else:
            print(
                f"  [WARN] verifier_policy.yaml: exists but PyYAML is not installed; cannot validate"
            )
            has_warn = True
    else:
        print(f"  [FAIL] verifier_policy.yaml: not found at {policy_path}")
        all_pass = False

    # 3. Check python_bin is resolvable
    python_bin = _resolve_policy_python_bin(policy)
    try:
        proc = subprocess.run(
            [python_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0:
            version = proc.stdout.strip() or proc.stderr.strip()
            print(f"  [PASS] python_bin: {python_bin} ({version})")
        else:
            print(
                f"  [FAIL] python_bin: {python_bin} exited with code {proc.returncode}"
            )
            all_pass = False
    except FileNotFoundError:
        print(f"  [FAIL] python_bin: {python_bin} not found on PATH")
        all_pass = False
    except subprocess.TimeoutExpired:
        print(f"  [FAIL] python_bin: {python_bin} timed out")
        all_pass = False
    except Exception as exc:
        print(f"  [FAIL] python_bin: {python_bin} error: {exc}")
        all_pass = False

    # 4. Check test_command is configured
    test_command = str(policy.get("test_command", "")).strip()
    if test_command:
        print(f"  [PASS] test_command: {test_command}")
    else:
        print("  [WARN] test_command: not configured")
        has_warn = True

    # 5. Check dry_run_command is configured
    dry_run_command = str(policy.get("dry_run_command", "")).strip()
    if dry_run_command:
        # Check if it is the default stub that always fails
        if "AUTOLAB DRY-RUN STUB" in dry_run_command:
            print(
                "  [WARN] dry_run_command: using default stub (will fail until customized)"
            )
            has_warn = True
        else:
            print(f"  [PASS] dry_run_command: {dry_run_command}")
    else:
        print("  [WARN] dry_run_command: not configured")
        has_warn = True

    # Summary
    print("")
    if all_pass and not has_warn:
        print("summary: all checks passed")
    elif all_pass and has_warn:
        print("summary: passed with warnings")
    else:
        print("summary: some checks failed")

    # Offer to write missing defaults if not --check
    if not check_only and not all_pass:
        if not autolab_dir.exists():
            print("\nTo create the .autolab scaffold, run: autolab init")
        if not policy_path.exists() and autolab_dir.exists():
            print(f"\nWriting default verifier_policy.yaml to {policy_path}")
            policy_path.parent.mkdir(parents=True, exist_ok=True)
            policy_path.write_text(DEFAULT_VERIFIER_POLICY, encoding="utf-8")
            print("  written: verifier_policy.yaml (default)")

    return 0 if all_pass else 1


def _cmd_sync_scaffold(args: argparse.Namespace) -> int:
    try:
        source_root = _resolve_scaffold_source()
    except RuntimeError as exc:
        print(f"autolab sync-scaffold: ERROR {exc}", file=sys.stderr)
        return 1

    destination = Path(args.dest).expanduser().resolve()
    copied, skipped = _sync_scaffold_bundle(
        source_root,
        destination,
        overwrite=bool(args.force),
    )
    print("autolab sync-scaffold")
    print(f"source: {source_root}")
    print(f"destination: {destination}")
    print(f"copied_files: {copied}")
    print(f"skipped_files: {skipped}")
    if not args.force and skipped and copied == 0:
        print("No files copied. Add --force to overwrite existing files.")
    return 0


def _cmd_update(args: argparse.Namespace) -> int:
    del args
    try:
        result = run_update(Path.cwd())
    except Exception as exc:
        print(f"autolab update: ERROR {exc}", file=sys.stderr)
        return 1

    print("autolab update")
    print(f"current_version: {result.current_version}")
    print(f"latest_tag: {result.latest_tag}")
    if not result.upgraded:
        print("action: already up to date")
        return 0

    print("action: upgrading")
    if result.synced_scaffold:
        print("action: syncing scaffold")
    elif result.sync_skipped_reason:
        print(f"action: sync skipped ({result.sync_skipped_reason})")
    return 0


def _cmd_install_skill(args: argparse.Namespace) -> int:
    provider = _normalize_skill_provider(str(getattr(args, "provider", "")).strip())
    project_root = Path(getattr(args, "project_root", ".")).expanduser().resolve()
    single_skill = getattr(args, "skill", None)

    if single_skill is not None:
        skill_names = [str(single_skill).strip()]
    else:
        try:
            skill_names = _list_bundled_skills(provider)
        except Exception as exc:
            print(f"autolab install-skill: ERROR {exc}", file=sys.stderr)
            return 1

    print("autolab install-skill")
    print(f"provider: {provider}")
    print(f"install_root: {_skill_install_root(project_root, provider)}")

    installed = 0
    for skill_name in skill_names:
        destination = (
            _skill_install_root(project_root, provider) / skill_name / "SKILL.md"
        )
        try:
            template_text = _load_packaged_skill_template_text(provider, skill_name)
        except Exception as exc:
            print(f"  {skill_name}: ERROR {exc}", file=sys.stderr)
            return 1

        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(template_text, encoding="utf-8")
        except Exception as exc:
            print(
                f"  {skill_name}: ERROR writing {destination}: {exc}", file=sys.stderr
            )
            return 1

        print(f"  {skill_name}: installed -> {destination}")
        installed += 1

    print(f"skills_installed: {installed}")
    print("status: installed (overwritten if existing)")
    return 0


def _cmd_slurm_job_list(args: argparse.Namespace) -> int:
    action = str(getattr(args, "action", "")).strip().lower()
    manifest_path = Path(args.manifest).expanduser()
    doc_path = Path(args.doc).expanduser()
    if action not in {"append", "verify"}:
        print(
            f"autolab slurm-job-list: invalid action '{action}' (expected append|verify)",
            file=sys.stderr,
        )
        return 1

    try:
        manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(
            f"autolab slurm-job-list: ERROR loading manifest {manifest_path}: {exc}",
            file=sys.stderr,
        )
        return 1
    if not isinstance(manifest_payload, dict):
        print(
            f"autolab slurm-job-list: ERROR manifest {manifest_path} must be a JSON object",
            file=sys.stderr,
        )
        return 1

    if action == "append":
        try:
            if not is_slurm_manifest(manifest_payload):
                print(
                    f"autolab slurm-job-list: manifest is non-SLURM; append skipped for {manifest_path}"
                )
                return 0
            if doc_path.parent != manifest_path.parent:
                doc_path.parent.mkdir(parents=True, exist_ok=True)
            run_id = required_run_id(manifest_payload)
            canonical = canonical_slurm_job_bullet(manifest_payload)
            existing_text = (
                doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""
            )
            next_text, updated = append_entry_idempotent(
                existing_text, canonical, run_id
            )
            if updated:
                doc_path.write_text(next_text, encoding="utf-8")
                print(f"autolab slurm-job-list: appended run_id={run_id} -> {doc_path}")
            else:
                print(
                    f"autolab slurm-job-list: run_id={run_id} already present in {doc_path}"
                )
            return 0
        except Exception as exc:
            print(f"autolab slurm-job-list: ERROR {exc}", file=sys.stderr)
            return 1

    try:
        if not is_slurm_manifest(manifest_payload):
            print(
                f"autolab slurm-job-list: manifest is non-SLURM; verify skipped for {manifest_path}"
            )
            return 0
        run_id = required_run_id(manifest_payload)
        job_id = required_slurm_job_id(manifest_payload)
        expected = canonical_slurm_job_bullet(manifest_payload)
        ledger_text = doc_path.read_text(encoding="utf-8") if doc_path.exists() else ""
        if not ledger_contains_entry(ledger_text, expected):
            print(
                f"autolab slurm-job-list: FAIL run_id={run_id}, job_id={job_id}, missing ledger entry in {doc_path}"
            )
            return 1
        print(f"autolab slurm-job-list: PASS job_id={job_id}, run_id={run_id}")
        return 0
    except Exception as exc:
        print(
            f"autolab slurm-job-list: ERROR verifying {manifest_path}: {exc}",
            file=sys.stderr,
        )
        return 1


def _apply_init_policy_defaults(
    policy_path: Path,
    *,
    interactive: bool,
) -> tuple[bool, str]:
    if _yaml_mod is None or not policy_path.exists():
        return (False, "")
    try:
        policy = _load_yaml_mapping(policy_path)
    except Exception as exc:
        return (False, f"autolab init: WARN could not parse policy for defaults: {exc}")

    original = _yaml_mod.safe_dump(policy, sort_keys=False)
    selected_command = ""
    configured_python_bin = str(policy.get("python_bin", "")).strip()
    if not configured_python_bin or configured_python_bin in {"python", "python3"}:
        policy["python_bin"] = sys.executable
    if interactive:
        print("")
        print("autolab init policy setup")
        print(
            "Configure a dry-run command now (leave empty to skip dry-run for implementation stages)."
        )
        try:
            selected_command = input("dry_run_command> ").strip()
        except EOFError:
            selected_command = ""

    requirements_by_stage = policy.get("requirements_by_stage", {})
    if not isinstance(requirements_by_stage, dict):
        requirements_by_stage = {}
        policy["requirements_by_stage"] = requirements_by_stage

    implementation_cfg = requirements_by_stage.get("implementation", {})
    if not isinstance(implementation_cfg, dict):
        implementation_cfg = {}
    implementation_review_cfg = requirements_by_stage.get("implementation_review", {})
    if not isinstance(implementation_review_cfg, dict):
        implementation_review_cfg = {}

    warning = ""
    if selected_command:
        policy["dry_run_command"] = selected_command
        implementation_cfg["dry_run"] = True
        implementation_review_cfg["dry_run"] = True
    else:
        implementation_cfg["dry_run"] = False
        implementation_review_cfg["dry_run"] = False
        warning = (
            "autolab init: WARN dry_run_command is not configured. "
            "Set verifier_policy.yaml dry_run_command before enabling dry_run requirements."
        )

    requirements_by_stage["implementation"] = implementation_cfg
    requirements_by_stage["implementation_review"] = implementation_review_cfg
    policy["requirements_by_stage"] = requirements_by_stage

    rendered = _yaml_mod.safe_dump(policy, sort_keys=False)
    changed = rendered != original
    if changed:
        policy_path.write_text(rendered, encoding="utf-8")
    return (changed, warning)


def _cmd_policy_apply_preset(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    preset_name = str(args.preset).strip()

    if preset_name not in POLICY_PRESET_NAMES:
        print(
            f"autolab policy apply preset: ERROR unsupported preset '{preset_name}'",
            file=sys.stderr,
        )
        return 1

    if _yaml_mod is None:
        print("autolab policy apply preset: ERROR PyYAML is required", file=sys.stderr)
        return 1

    try:
        scaffold_source = _resolve_scaffold_source()
    except RuntimeError as exc:
        print(f"autolab policy apply preset: ERROR {exc}", file=sys.stderr)
        return 1

    preset_path = scaffold_source / "policy" / f"{preset_name}.yaml"
    if not preset_path.exists():
        print(
            f"autolab policy apply preset: ERROR preset file missing at {preset_path}",
            file=sys.stderr,
        )
        return 1

    policy_path = autolab_dir / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    current_policy: dict[str, Any] = {}
    if policy_path.exists():
        try:
            current_policy = _load_yaml_mapping(policy_path)
        except Exception as exc:
            print(
                f"autolab policy apply preset: ERROR could not parse current policy: {exc}",
                file=sys.stderr,
            )
            return 1
    try:
        preset_policy = _load_yaml_mapping(preset_path)
    except Exception as exc:
        print(
            f"autolab policy apply preset: ERROR could not parse preset: {exc}",
            file=sys.stderr,
        )
        return 1

    merged = _deep_merge_dict(current_policy, preset_policy)
    policy_path.write_text(
        _yaml_mod.safe_dump(merged, sort_keys=False), encoding="utf-8"
    )

    print("autolab policy apply preset")
    print(f"preset: {preset_name}")
    print(f"policy_file: {policy_path}")
    print("status: applied")
    return 0


def _cmd_init(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = repo_root / ".autolab"
    created: list[Path] = []

    for directory in (
        autolab_dir,
        autolab_dir / "logs",
        autolab_dir / "logs" / "iterations",
        autolab_dir / "prompts" / "shared",
        autolab_dir / "schemas",
        autolab_dir / "verifiers",
        repo_root / "experiments",
    ):
        if not directory.exists():
            directory.mkdir(parents=True, exist_ok=True)
            created.append(directory)

    backlog_path = autolab_dir / "backlog.yaml"
    verifier_policy_path = autolab_dir / "verifier_policy.yaml"
    agent_result_path = autolab_dir / "agent_result.json"
    from_existing = bool(getattr(args, "from_existing", False))
    brownfield_result = None
    scaffold_copied = 0
    scaffold_skipped = 0

    try:
        scaffold_source = _resolve_scaffold_source()
    except RuntimeError:
        scaffold_source = None
    if scaffold_source is not None:
        copied, skipped = _sync_scaffold_bundle(
            scaffold_source, autolab_dir, overwrite=False
        )
        scaffold_copied = copied
        scaffold_skipped = skipped

    iteration_id = ""
    if state_path.exists():
        try:
            state = _normalize_state(_load_state(state_path))
        except StateError as exc:
            print(f"autolab init: ERROR {exc}", file=sys.stderr)
            return 1
        iteration_id = state["iteration_id"]
    else:
        iteration_id = _parse_iteration_from_backlog(backlog_path)
        if not iteration_id:
            iteration_id = _bootstrap_iteration_id()
        _ensure_json_file(state_path, _default_state(iteration_id), created)

    _ensure_text_file(
        backlog_path,
        DEFAULT_BACKLOG_TEMPLATE.format(iteration_id=iteration_id),
        created,
    )
    _ensure_text_file(verifier_policy_path, DEFAULT_VERIFIER_POLICY, created)
    interactive = bool(getattr(args, "interactive", False))
    no_interactive = bool(getattr(args, "no_interactive", False))
    if not interactive and not no_interactive:
        interactive = sys.stdin.isatty()
    policy_updated, policy_warning = _apply_init_policy_defaults(
        verifier_policy_path,
        interactive=interactive and not no_interactive,
    )
    if policy_updated and verifier_policy_path not in created:
        created.append(verifier_policy_path)
    _ensure_json_file(agent_result_path, _default_agent_result(), created)
    if scaffold_source is None:
        for stage in STAGE_PROMPT_FILES.keys():
            audience_files = (
                ("audit", STAGE_PROMPT_FILES.get(stage, "")),
                ("runner", STAGE_RUNNER_PROMPT_FILES.get(stage, "")),
                ("brief", STAGE_BRIEF_PROMPT_FILES.get(stage, "")),
                ("human", STAGE_HUMAN_PROMPT_FILES.get(stage, "")),
            )
            for audience, prompt_file in audience_files:
                if not prompt_file:
                    continue
                _ensure_text_file(
                    autolab_dir / "prompts" / prompt_file,
                    _default_stage_prompt_text(stage, audience=audience),
                    created,
                )
    init_experiment_type = (
        _resolve_experiment_type_from_backlog(
            repo_root,
            iteration_id=iteration_id,
            experiment_id="",
        )
        or DEFAULT_EXPERIMENT_TYPE
    )
    _ensure_iteration_skeleton(
        repo_root,
        iteration_id,
        created,
        experiment_type=init_experiment_type,
    )
    if from_existing:
        try:
            brownfield_result = run_brownfield_bootstrap(
                repo_root,
                state_path=state_path,
                backlog_path=backlog_path,
                policy_path=verifier_policy_path,
            )
        except Exception as exc:
            print(
                f"autolab init: ERROR brownfield bootstrap failed: {exc}",
                file=sys.stderr,
            )
            return 1
        for path in brownfield_result.changed_files:
            if path not in created:
                created.append(path)
        iteration_id = brownfield_result.focus_iteration_id or iteration_id
    try:
        init_state = _normalize_state(_load_state(state_path))
    except StateError:
        init_state = None
    todo_sync_changed, _ = _safe_todo_pre_sync(repo_root, init_state)
    for path in todo_sync_changed:
        if path not in created:
            created.append(path)

    _append_log(
        repo_root,
        f"init completed for iteration {iteration_id}; created={len(created)}",
    )

    print("autolab init")
    print(f"state_file: {state_path}")
    print(f"iteration_id: {iteration_id}")
    print(f"created_entries: {len(created)}")
    print(f"scaffold_copied_files: {scaffold_copied}")
    print(f"scaffold_skipped_files: {scaffold_skipped}")
    print(f"from_existing: {str(from_existing).lower()}")
    if brownfield_result is not None:
        print(f"brownfield_focus_iteration_id: {brownfield_result.focus_iteration_id}")
        print(
            f"brownfield_focus_experiment_id: {brownfield_result.focus_experiment_id}"
        )
        print(f"brownfield_backlog_action: {brownfield_result.backlog_action}")
        print(
            "brownfield_policy_seeded: "
            f"{str(bool(brownfield_result.policy_seeded)).lower()}"
        )
        print(f"brownfield_project_map: {brownfield_result.project_map_path}")
        print(
            "brownfield_experiment_delta_map: "
            f"{brownfield_result.experiment_delta_map_path}"
        )
        print(f"brownfield_context_bundle: {brownfield_result.context_bundle_path}")
    for path in created:
        print(f"- {path}")
    if brownfield_result is not None and brownfield_result.warnings:
        print("\nBrownfield warnings:")
        for warning in brownfield_result.warnings:
            print(f"- {warning}")

    # Phase 7c: placeholder detection reminder
    print("\nReminder: Review and customize the following before your first run:")
    print("  - .autolab/backlog.yaml (update hypothesis titles and metrics)")
    print("  - .autolab/prompts/stage_*.md (add project-specific instructions)")
    if policy_warning:
        print(f"\n{policy_warning}")

    return 0


def _cmd_reset(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)

    try:
        source_root = _resolve_scaffold_source()
    except RuntimeError as exc:
        print(f"autolab reset: ERROR {exc}", file=sys.stderr)
        return 1

    if autolab_dir.exists():
        try:
            shutil.rmtree(autolab_dir)
        except Exception as exc:
            print(
                f"autolab reset: ERROR removing {autolab_dir}: {exc}", file=sys.stderr
            )
            return 1

    copied, skipped = _sync_scaffold_bundle(
        source_root,
        autolab_dir,
        overwrite=True,
    )
    backlog_path = autolab_dir / "backlog.yaml"
    iteration_id = _parse_iteration_from_backlog(backlog_path)
    if not iteration_id:
        iteration_id = _bootstrap_iteration_id()

    try:
        _write_json(state_path, _default_state(iteration_id))
    except OSError as exc:
        print(
            f"autolab reset: ERROR writing state file {state_path}: {exc}",
            file=sys.stderr,
        )
        return 1

    print("autolab reset")
    print(f"state_file: {state_path}")
    print(f"autolab_dir: {autolab_dir}")
    print(f"copied_files: {copied}")
    print(f"skipped_files: {skipped}")
    return 0


__all__ = [name for name in globals() if not name.startswith("__")]
