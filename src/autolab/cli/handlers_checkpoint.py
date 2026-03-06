"""CLI handlers for checkpoint and hooks commands."""

from __future__ import annotations

from autolab.cli.support import *


def _cmd_checkpoint_create(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)

    try:
        state = _normalize_state(_load_state(state_path))
    except StateError as exc:
        print(f"autolab checkpoint create: ERROR {exc}", file=sys.stderr)
        return 1

    stage = str(state.get("stage", "")).strip()
    label = str(getattr(args, "label", "") or "").strip()
    iteration_id = str(getattr(args, "iteration_id", "") or "").strip()
    scope_kind = str(getattr(args, "scope", "") or "").strip()

    if not iteration_id:
        iteration_id = str(state.get("iteration_id", "")).strip()
    experiment_id = str(state.get("experiment_id", "")).strip()

    try:
        from autolab.checkpoint import create_checkpoint

        cp_id, cp_dir = create_checkpoint(
            repo_root,
            state_path=state_path,
            stage=stage,
            trigger="manual",
            label=label,
            iteration_id=iteration_id,
            experiment_id=experiment_id,
            scope_kind=scope_kind,
        )
    except Exception as exc:
        print(f"autolab checkpoint create: ERROR {exc}", file=sys.stderr)
        return 1

    print("autolab checkpoint create")
    print(f"checkpoint_id: {cp_id}")
    print(f"checkpoint_dir: {cp_dir}")
    print(f"stage: {stage}")
    if label:
        print(f"label: {label}")
    return 0


def _cmd_checkpoint_list(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)

    iteration_id = str(getattr(args, "iteration_id", "") or "").strip()
    trigger_filter = str(getattr(args, "trigger", "") or "").strip()
    as_json = bool(getattr(args, "json", False))

    if not iteration_id:
        try:
            state = _normalize_state(_load_state(state_path))
            iteration_id = str(state.get("iteration_id", "")).strip()
        except Exception:
            pass

    try:
        from autolab.checkpoint import list_checkpoints

        checkpoints = list_checkpoints(
            repo_root, iteration_id=iteration_id, trigger=trigger_filter
        )
    except Exception as exc:
        print(f"autolab checkpoint list: ERROR {exc}", file=sys.stderr)
        return 1

    if as_json:
        print(json.dumps(checkpoints, indent=2))
        return 0

    if not checkpoints:
        print("autolab checkpoint list")
        print("(no checkpoints found)")
        return 0

    print("autolab checkpoint list")
    for cp in checkpoints:
        parts = [
            cp.get("checkpoint_id", ""),
            f"stage={cp.get('stage', '')}",
            f"trigger={cp.get('trigger', '')}",
            f"artifacts={cp.get('artifact_count', 0)}",
            f"at={cp.get('created_at', '')}",
        ]
        label = cp.get("label", "")
        if label:
            parts.append(f"label={label}")
        print(f"  {' '.join(parts)}")
    return 0


def _cmd_hooks_install(args: argparse.Namespace) -> int:
    state_path = Path(args.state_file).expanduser().resolve()
    repo_root = _resolve_repo_root(state_path)

    force = bool(getattr(args, "force", False))

    # Determine hooks directory
    try:
        result = subprocess.run(
            ["git", "config", "core.hooksPath"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            hooks_dir = repo_root / result.stdout.strip()
        else:
            hooks_dir = repo_root / ".git" / "hooks"
    except Exception:
        hooks_dir = repo_root / ".git" / "hooks"

    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Find template source
    try:
        scaffold_source = _resolve_scaffold_source()
    except RuntimeError:
        scaffold_source = None

    template_dir: Path | None = None
    if scaffold_source is not None:
        candidate = scaffold_source.parent / ".githooks"
        if candidate.is_dir():
            template_dir = candidate

    installed: list[str] = []
    hook_name = "post-commit"
    hook_path = hooks_dir / hook_name

    if hook_path.exists() and not force:
        print(
            f"autolab hooks install: WARN {hook_name} already exists at {hook_path} — use --force to overwrite",
            file=sys.stderr,
        )
    else:
        template_file: Path | None = None
        if template_dir is not None:
            candidate = template_dir / f"{hook_name}.template"
            if candidate.exists():
                template_file = candidate

        if template_file is not None:
            shutil.copy2(str(template_file), str(hook_path))
        else:
            # Generate a minimal hook
            hook_content = _generate_post_commit_hook()
            hook_path.write_text(hook_content, encoding="utf-8")
        hook_path.chmod(0o755)
        installed.append(hook_name)

    print("autolab hooks install")
    print(f"hooks_dir: {hooks_dir}")
    print(
        f"installed: {', '.join(installed) if installed else '(none — already present)'}"
    )
    return 0


def _generate_post_commit_hook() -> str:
    return """\
#!/usr/bin/env bash
# Autolab post-commit hook: auto-checkpoint + version tagging
# Generated by `autolab hooks install`

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0

# Detect Python
if [ -n "$VIRTUAL_ENV" ]; then
    PYTHON="$VIRTUAL_ENV/bin/python"
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
    PYTHON="$REPO_ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON=python3
elif command -v python >/dev/null 2>&1; then
    PYTHON=python
else
    exit 0
fi

# Auto-checkpoint (non-blocking)
"$PYTHON" -m autolab.checkpoint_hook 2>/dev/null || true
"""


__all__ = [name for name in globals() if not name.startswith("__")]
