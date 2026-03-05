"""Backlog steering and experiment lifecycle CLI handlers."""

from __future__ import annotations

from autolab.cli.support import *
from autolab.cli.handlers_observe import _safe_refresh_handoff


def _cmd_focus(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)

    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        print(f"autolab focus: ERROR {exc}", file=sys.stderr)
        return 1

    lock_error = _ensure_no_active_lock(autolab_dir / "lock")
    if lock_error:
        print(f"autolab focus: ERROR {lock_error}", file=sys.stderr)
        return 1

    requested_iteration_id = _normalize_space(getattr(args, "iteration_id", ""))
    requested_experiment_id = _normalize_space(getattr(args, "experiment_id", ""))
    if not requested_iteration_id and not requested_experiment_id:
        print(
            "autolab focus: ERROR set --iteration-id and/or --experiment-id",
            file=sys.stderr,
        )
        return 2

    _payload, entry, resolve_error = _resolve_backlog_target_entry(
        repo_root,
        iteration_id=requested_iteration_id,
        experiment_id=requested_experiment_id,
    )
    if entry is None:
        print(f"autolab focus: ERROR {resolve_error}", file=sys.stderr)
        return 1

    target_iteration_id = _normalize_space(str(entry.get("iteration_id", "")))
    target_experiment_id = _normalize_space(str(entry.get("id", "")))
    id_error = _validate_target_identifiers(target_iteration_id, target_experiment_id)
    if id_error:
        print(f"autolab focus: ERROR {id_error}", file=sys.stderr)
        return 1

    try:
        iteration_dir, iteration_type = _resolve_iteration_directory(
            repo_root,
            iteration_id=target_iteration_id,
            experiment_id=target_experiment_id,
            require_exists=True,
        )
    except Exception as exc:
        print(f"autolab focus: ERROR {exc}", file=sys.stderr)
        return 1

    stage_before = _normalize_space(str(state.get("stage", "")))
    target_stage = "stop" if _is_entry_completed(entry) else "hypothesis"
    state["iteration_id"] = target_iteration_id
    state["experiment_id"] = target_experiment_id
    _reset_state_for_manual_handoff(state, stage=target_stage)

    summary = (
        f"manual focus updated to iteration_id='{target_iteration_id}' "
        f"experiment_id='{target_experiment_id}' (stage={target_stage})"
    )
    _append_state_history(
        state,
        stage_before=stage_before,
        stage_after=target_stage,
        status="manual_focus",
        summary=summary,
    )
    _write_json(state_path, state)
    todo_changed, todo_message = _safe_todo_pre_sync(repo_root, state)
    changed_files = [state_path, *todo_changed]
    result_summary = summary if not todo_message else f"{summary}; {todo_message}"
    _persist_agent_result(
        repo_root,
        status="complete",
        summary=result_summary,
        changed_files=changed_files,
    )
    _append_log(
        repo_root,
        (
            f"focus: {stage_before} -> {target_stage} "
            f"iteration={target_iteration_id} experiment={target_experiment_id}"
        ),
    )
    _handoff_payload, _handoff_error = _safe_refresh_handoff(state_path)
    if _handoff_payload is None:
        print(
            f"autolab focus: WARN failed to refresh handoff snapshot: {_handoff_error}",
            file=sys.stderr,
        )

    print("autolab focus")
    print(f"state_file: {state_path}")
    print(f"iteration_id: {target_iteration_id}")
    print(f"experiment_id: {target_experiment_id}")
    print(f"stage: {target_stage}")
    print(f"iteration_dir: {iteration_dir}")
    print(f"iteration_type: {iteration_type}")
    print(f"changed_files: {len(changed_files)}")
    return 0


def _cmd_todo(args: argparse.Namespace) -> int:
    action = _normalize_space(str(getattr(args, "todo_action", ""))).lower()
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        print(f"autolab todo: ERROR {exc}", file=sys.stderr)
        return 1

    changed_pre, pre_message = _safe_todo_pre_sync(repo_root, state)
    changed_files: list[Path] = [*changed_pre]

    if action == "sync":
        open_tasks = list_open_tasks(repo_root)
        print("autolab todo sync")
        print(f"state_file: {state_path}")
        print(f"open_tasks: {len(open_tasks)}")
        print(f"changed_files: {len(changed_files)}")
        for path in changed_files:
            print(f"- {path}")
        if pre_message:
            print(f"summary: {pre_message}")
        return 0

    if action == "list":
        open_tasks = list_open_tasks(repo_root)
        if bool(getattr(args, "json", False)):
            print(
                json.dumps(
                    {"open_count": len(open_tasks), "tasks": open_tasks}, indent=2
                )
            )
            return 0
        print("autolab todo list")
        print(f"open_tasks: {len(open_tasks)}")
        for index, task in enumerate(open_tasks, start=1):
            print(
                f"{index}. {task.get('task_id', '')} "
                f"[stage:{task.get('stage', '')}] "
                f"[source:{task.get('source', '')}] "
                f"{task.get('text', '')}"
            )
        return 0

    if action == "add":
        raw_text = _normalize_space(str(getattr(args, "text", "")))
        if not raw_text:
            print("autolab todo add: ERROR task text is empty", file=sys.stderr)
            return 2
        default_stage = _normalize_space(
            str(state.get("stage", "implementation"))
        ).lower()
        stage = (
            _normalize_space(str(getattr(args, "stage", ""))).lower() or default_stage
        )
        if stage not in ALL_STAGES:
            if default_stage in ALL_STAGES:
                stage = default_stage
            else:
                stage = "implementation"
        if stage not in ALL_STAGES:
            print(
                f"autolab todo add: ERROR invalid stage '{stage}'",
                file=sys.stderr,
            )
            return 2

        tags: list[str] = []
        priority = _normalize_space(str(getattr(args, "priority", ""))).lower()
        owner = _normalize_space(str(getattr(args, "owner", "")))
        labels_raw = getattr(args, "label", []) or []
        labels = [
            _normalize_space(str(item)).lower()
            for item in labels_raw
            if _normalize_space(str(item))
        ]
        if priority:
            tags.append(f"[priority:{priority}]")
        if owner:
            tags.append(f"[owner:{owner}]")
        for label in labels:
            tags.append(f"[label:{label}]")
        suffix = f" {' '.join(tags)}" if tags else ""
        todo_path = repo_root / "docs" / "todo.md"
        _insert_todo_task_line(
            todo_path,
            line=f"- [stage:{stage}] {raw_text}{suffix}",
        )
        changed_files.append(todo_path)

        changed_post, post_message = _safe_todo_pre_sync(repo_root, state)
        changed_files.extend(changed_post)
        open_tasks = list_open_tasks(repo_root)
        resolved_task = None
        for task in reversed(open_tasks):
            if (
                str(task.get("source", "")) == "manual"
                and str(task.get("stage", "")) == stage
                and _normalize_space(str(task.get("text", ""))) == raw_text
            ):
                resolved_task = task
                break
        if resolved_task is None and open_tasks:
            resolved_task = open_tasks[-1]

        print("autolab todo add")
        print(f"state_file: {state_path}")
        if resolved_task is not None:
            print(f"task_id: {resolved_task.get('task_id', '')}")
            print(f"stage: {resolved_task.get('stage', '')}")
            print(f"text: {resolved_task.get('text', '')}")
        print(f"open_tasks: {len(open_tasks)}")
        print(f"changed_files: {len({str(path) for path in changed_files})}")
        if post_message:
            print(f"summary: {post_message}")
        return 0

    if action in {"done", "remove"}:
        selector = _normalize_space(str(getattr(args, "selector", "")))
        open_tasks = list_open_tasks(repo_root)
        task_id, selector_error = _resolve_todo_selector(open_tasks, selector)
        if selector_error:
            print(f"autolab todo {action}: ERROR {selector_error}", file=sys.stderr)
            return 1

        if action == "done":
            updated = mark_task_completed(repo_root, task_id)
        else:
            updated = mark_task_removed(repo_root, task_id)
        if not updated:
            print(
                f"autolab todo {action}: ERROR task '{task_id}' is not open",
                file=sys.stderr,
            )
            return 1

        changed_post, post_message = _safe_todo_pre_sync(repo_root, state)
        changed_files.extend(changed_post)
        open_after = list_open_tasks(repo_root)
        print(f"autolab todo {action}")
        print(f"task_id: {task_id}")
        print(f"open_tasks: {len(open_after)}")
        print(f"changed_files: {len({str(path) for path in changed_files})}")
        if post_message:
            print(f"summary: {post_message}")
        return 0

    print(f"autolab todo: ERROR unknown action '{action}'", file=sys.stderr)
    return 2


def _cmd_experiment_create(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        print(f"autolab experiment create: ERROR {exc}", file=sys.stderr)
        return 1

    lock_error = _ensure_no_active_lock(autolab_dir / "lock")
    if lock_error:
        print(f"autolab experiment create: ERROR {lock_error}", file=sys.stderr)
        return 1

    experiment_id = _normalize_space(str(getattr(args, "experiment_id", "")))
    iteration_id = _normalize_space(str(getattr(args, "iteration_id", "")))
    requested_hypothesis_id = _normalize_space(str(getattr(args, "hypothesis_id", "")))

    id_error = _validate_target_identifiers(iteration_id, experiment_id)
    if id_error:
        print(f"autolab experiment create: ERROR {id_error}", file=sys.stderr)
        return 1

    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    backlog_payload, load_error = _load_backlog_yaml(backlog_path)
    if backlog_payload is None:
        print(f"autolab experiment create: ERROR {load_error}", file=sys.stderr)
        return 1
    if not isinstance(backlog_payload.get("hypotheses"), list):
        print(
            "autolab experiment create: ERROR backlog hypotheses list is missing",
            file=sys.stderr,
        )
        return 1
    experiments = backlog_payload.get("experiments")
    if not isinstance(experiments, list):
        print(
            "autolab experiment create: ERROR backlog experiments list is missing",
            file=sys.stderr,
        )
        return 1

    resolved_hypothesis_id, hypothesis_error = _resolve_create_hypothesis_id(
        backlog_payload,
        hypothesis_id=requested_hypothesis_id,
    )
    if hypothesis_error:
        print(f"autolab experiment create: ERROR {hypothesis_error}", file=sys.stderr)
        return 1

    uniqueness_error = _validate_experiment_create_uniqueness(
        repo_root,
        backlog_payload,
        experiment_id=experiment_id,
        iteration_id=iteration_id,
    )
    if uniqueness_error:
        print(f"autolab experiment create: ERROR {uniqueness_error}", file=sys.stderr)
        return 1

    created_entries: list[Path] = []
    iteration_dir = repo_root / "experiments" / "plan" / iteration_id
    backlog_changed = False
    new_entry = {
        "id": experiment_id,
        "hypothesis_id": resolved_hypothesis_id,
        "status": "open",
        "type": "plan",
        "iteration_id": iteration_id,
    }
    experiments.append(new_entry)
    try:
        _ensure_iteration_skeleton(
            repo_root,
            iteration_id,
            created_entries,
            experiment_type="plan",
        )
        backlog_changed, backlog_write_error = _write_backlog_yaml(
            backlog_path,
            backlog_payload,
        )
        if backlog_write_error:
            raise RuntimeError(backlog_write_error)
        if not backlog_changed:
            raise RuntimeError("backlog update produced no changes")
    except Exception as exc:
        rollback_notes: list[str] = []
        try:
            experiments.remove(new_entry)
        except ValueError:
            pass
        if iteration_dir.exists():
            try:
                shutil.rmtree(iteration_dir)
            except Exception as rollback_exc:
                rollback_notes.append(f"iteration rollback failed: {rollback_exc}")
        detail = str(exc)
        if rollback_notes:
            detail = f"{detail}; {' | '.join(rollback_notes)}"
        print(f"autolab experiment create: ERROR {detail}", file=sys.stderr)
        return 1

    stage_before = _normalize_space(str(state.get("stage", "")))
    summary = (
        f"manual experiment create: experiment_id='{experiment_id}' "
        f"iteration_id='{iteration_id}' hypothesis_id='{resolved_hypothesis_id}' type=plan"
    )
    _append_state_history(
        state,
        stage_before=stage_before,
        stage_after=stage_before,
        status="manual_experiment_create",
        summary=summary,
    )
    _write_json(state_path, state)
    todo_changed, todo_message = _safe_todo_pre_sync(repo_root, state)
    changed_files = [
        state_path,
        backlog_path,
        iteration_dir,
        *created_entries,
        *todo_changed,
    ]
    result_summary = summary if not todo_message else f"{summary}; {todo_message}"
    _persist_agent_result(
        repo_root,
        status="complete",
        summary=result_summary,
        changed_files=changed_files,
    )
    _append_log(
        repo_root,
        (
            f"experiment create: {experiment_id} iteration={iteration_id} "
            f"hypothesis={resolved_hypothesis_id} type=plan created={len(created_entries)}"
        ),
    )
    _handoff_payload, _handoff_error = _safe_refresh_handoff(state_path)
    if _handoff_payload is None:
        print(
            f"autolab experiment create: WARN failed to refresh handoff snapshot: {_handoff_error}",
            file=sys.stderr,
        )

    print("autolab experiment create")
    print(f"state_file: {state_path}")
    print(f"experiment_id: {experiment_id}")
    print(f"hypothesis_id: {resolved_hypothesis_id}")
    print(f"iteration_id: {iteration_id}")
    print("type: plan")
    print(f"iteration_dir: {iteration_dir}")
    print(f"created_entries: {len(created_entries)}")
    print(f"backlog_changed: {backlog_changed}")
    print(f"changed_files: {len({str(path) for path in changed_files})}")
    return 0


def _cmd_experiment_move(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)
    autolab_dir = _resolve_autolab_dir(state_path, repo_root)
    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        print(f"autolab experiment move: ERROR {exc}", file=sys.stderr)
        return 1

    lock_error = _ensure_no_active_lock(autolab_dir / "lock")
    if lock_error:
        print(f"autolab experiment move: ERROR {lock_error}", file=sys.stderr)
        return 1

    target_type, type_error = _normalize_experiment_stage(str(getattr(args, "to", "")))
    if type_error:
        print(f"autolab experiment move: ERROR {type_error}", file=sys.stderr)
        return 2

    requested_iteration_id = _normalize_space(str(getattr(args, "iteration_id", "")))
    requested_experiment_id = _normalize_space(str(getattr(args, "experiment_id", "")))
    if not requested_iteration_id and not requested_experiment_id:
        requested_iteration_id = _normalize_space(str(state.get("iteration_id", "")))
        requested_experiment_id = _normalize_space(str(state.get("experiment_id", "")))

    backlog_payload, entry, resolve_error = _resolve_backlog_target_entry(
        repo_root,
        iteration_id=requested_iteration_id,
        experiment_id=requested_experiment_id,
    )
    if entry is None or backlog_payload is None:
        print(f"autolab experiment move: ERROR {resolve_error}", file=sys.stderr)
        return 1

    iteration_id = _normalize_space(str(entry.get("iteration_id", "")))
    experiment_id = _normalize_space(str(entry.get("id", "")))
    id_error = _validate_target_identifiers(iteration_id, experiment_id)
    if id_error:
        print(f"autolab experiment move: ERROR {id_error}", file=sys.stderr)
        return 1

    try:
        source_dir, source_type = _resolve_iteration_directory(
            repo_root,
            iteration_id=iteration_id,
            experiment_id=experiment_id,
            require_exists=True,
        )
    except Exception as exc:
        print(f"autolab experiment move: ERROR {exc}", file=sys.stderr)
        return 1

    destination_dir = repo_root / "experiments" / target_type / iteration_id
    source_type_from_path = source_dir.parent.name
    if source_type_from_path in EXPERIMENT_TYPES:
        source_type = source_type_from_path
    if source_dir.resolve() != destination_dir.resolve() and destination_dir.exists():
        print(
            (
                f"autolab experiment move: ERROR destination already exists: {destination_dir}"
            ),
            file=sys.stderr,
        )
        return 1

    original_entry_type = str(entry.get("type", ""))
    original_entry_status = str(entry.get("status", ""))
    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    moved = False
    backlog_changed = False
    rewritten_paths: list[Path] = []
    try:
        if source_dir.resolve() != destination_dir.resolve():
            destination_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_dir), str(destination_dir))
            moved = True

        entry["type"] = target_type
        entry["status"] = _mapped_backlog_status_for_type(target_type)
        backlog_changed, backlog_write_error = _write_backlog_yaml(
            backlog_path,
            backlog_payload,
        )
        if backlog_write_error:
            raise RuntimeError(backlog_write_error)

        old_prefix = f"experiments/{source_type}/{iteration_id}"
        new_prefix = f"experiments/{target_type}/{iteration_id}"
        rewritten_paths, rewrite_error = _rewrite_iteration_prefix_scoped(
            repo_root,
            iteration_dir=destination_dir if moved else source_dir,
            old_prefix=old_prefix,
            new_prefix=new_prefix,
        )
        if rewrite_error:
            raise RuntimeError(rewrite_error)
    except Exception as exc:
        rollback_notes: list[str] = []

        # Revert backlog payload + file.
        entry["type"] = original_entry_type
        entry["status"] = original_entry_status
        _rollback_changed, rollback_backlog_error = _write_backlog_yaml(
            backlog_path,
            backlog_payload,
        )
        if rollback_backlog_error:
            rollback_notes.append(f"backlog rollback failed: {rollback_backlog_error}")

        # Revert directory move if needed.
        if moved:
            try:
                if source_dir.exists():
                    rollback_notes.append(
                        f"source path already exists during rollback: {source_dir}"
                    )
                elif destination_dir.exists():
                    source_dir.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(destination_dir), str(source_dir))
                else:
                    rollback_notes.append(
                        f"destination path missing during rollback: {destination_dir}"
                    )
            except Exception as rollback_exc:
                rollback_notes.append(f"directory rollback failed: {rollback_exc}")

        detail = str(exc)
        if rollback_notes:
            detail = f"{detail}; {' | '.join(rollback_notes)}"
        print(f"autolab experiment move: ERROR {detail}", file=sys.stderr)
        return 1

    stage_before = _normalize_space(str(state.get("stage", "")))
    state_iteration_id = _normalize_space(str(state.get("iteration_id", "")))
    state_experiment_id = _normalize_space(str(state.get("experiment_id", "")))
    focused_match = (
        bool(state_experiment_id)
        and state_iteration_id == iteration_id
        and state_experiment_id == experiment_id
    )
    if focused_match:
        state["iteration_id"] = iteration_id
        state["experiment_id"] = experiment_id
        target_stage = "stop" if target_type == "done" else "hypothesis"
        _reset_state_for_manual_handoff(state, stage=target_stage)
    else:
        target_stage = stage_before

    summary = (
        f"manual experiment move: experiment_id='{experiment_id}' "
        f"{source_type} -> {target_type} (iteration_id='{iteration_id}')"
    )
    _append_state_history(
        state,
        stage_before=stage_before,
        stage_after=target_stage,
        status="manual_experiment_move",
        summary=summary,
    )
    _write_json(state_path, state)
    todo_changed, todo_message = _safe_todo_pre_sync(repo_root, state)

    changed_files: list[Path] = [state_path, *todo_changed, *rewritten_paths]
    if backlog_changed:
        changed_files.append(repo_root / ".autolab" / "backlog.yaml")
    if moved:
        changed_files.append(destination_dir)
    result_summary = summary if not todo_message else f"{summary}; {todo_message}"
    _persist_agent_result(
        repo_root,
        status="complete",
        summary=result_summary,
        changed_files=changed_files,
    )
    _append_log(
        repo_root,
        (
            f"experiment move: {experiment_id} {source_type}->{target_type} "
            f"iteration={iteration_id} moved={moved} rewrites={len(rewritten_paths)}"
        ),
    )
    _handoff_payload, _handoff_error = _safe_refresh_handoff(state_path)
    if _handoff_payload is None:
        print(
            f"autolab experiment move: WARN failed to refresh handoff snapshot: {_handoff_error}",
            file=sys.stderr,
        )

    print("autolab experiment move")
    print(f"state_file: {state_path}")
    print(f"experiment_id: {experiment_id}")
    print(f"iteration_id: {iteration_id}")
    print(f"from_type: {source_type}")
    print(f"to_type: {target_type}")
    print(f"moved_directory: {moved}")
    print(f"source_dir: {source_dir}")
    print(f"destination_dir: {destination_dir}")
    print(f"rewritten_paths: {len(rewritten_paths)}")
    print(f"backlog_changed: {backlog_changed}")
    print(f"state_stage: {target_stage}")
    return 0


__all__ = [name for name in globals() if not name.startswith("__")]
