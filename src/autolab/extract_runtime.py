from __future__ import annotations

import importlib
import inspect
import json
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None

from autolab.config import (
    _load_extract_runtime_config,
    _load_verifier_policy,
    _resolve_policy_command,
    _resolve_policy_python_bin,
)
from autolab.models import StageCheckError
from autolab.state import _resolve_iteration_directory
from autolab.utils import _append_log


_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHELL_META_PATTERN = re.compile(r"[|&;<>()$`]")
_PYTHON_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class ExtractExecutionResult:
    run_id: str
    changed_files: tuple[Path, ...]


def _validate_run_id(run_id: str) -> str:
    value = str(run_id).strip()
    if not value:
        raise StageCheckError("extract_results execution requires non-empty run_id")
    if not _RUN_ID_PATTERN.fullmatch(value):
        raise StageCheckError(
            "run_id contains unsafe characters; use [A-Za-z0-9._-] and avoid path separators"
        )
    return value


def _ensure_path_within(base_dir: Path, candidate: Path, *, field: str) -> Path:
    base_resolved = base_dir.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    try:
        candidate_resolved.relative_to(base_resolved)
    except ValueError as exc:
        raise StageCheckError(
            f"{field} must stay within {base_resolved} (got {candidate_resolved})"
        ) from exc
    return candidate


def _resolve_run_dir(iteration_dir: Path, run_id: str) -> Path:
    normalized_run_id = _validate_run_id(run_id)
    runs_dir = iteration_dir / "runs"
    run_dir = runs_dir / normalized_run_id
    return _ensure_path_within(runs_dir, run_dir, field="run directory")


def _parse_timeout_seconds(raw_value: Any, *, default: float, field_name: str) -> float:
    if raw_value is None:
        return default
    try:
        timeout = float(raw_value)
    except Exception as exc:
        raise StageCheckError(f"{field_name} must be a positive number") from exc
    if timeout <= 0:
        raise StageCheckError(f"{field_name} must be > 0")
    return timeout


def _validate_python_module_name(module_name: str) -> str:
    value = str(module_name).strip()
    if not value:
        raise StageCheckError("extract parser hook module name must be non-empty")
    for part in value.split("."):
        if not _PYTHON_IDENTIFIER_PATTERN.fullmatch(part):
            raise StageCheckError(
                f"extract parser hook module name '{module_name}' is invalid"
            )
    return value


def _resolve_repo_python_module_source(repo_root: Path, module_name: str) -> Path:
    normalized_module_name = _validate_python_module_name(module_name)
    module_path = Path(*normalized_module_name.split("."))
    source_candidates = (
        repo_root / module_path.with_suffix(".py"),
        repo_root / module_path / "__init__.py",
    )
    for candidate in source_candidates:
        if not candidate.is_file():
            continue
        _ensure_path_within(
            repo_root,
            candidate,
            field=f"extract parser hook module '{normalized_module_name}'",
        )
        return candidate
    raise StageCheckError(
        "extract parser hook module must resolve to a file under the repository root"
    )


def _looks_like_path_token(token: str) -> bool:
    return (
        Path(token).is_absolute()
        or "/" in token
        or "\\" in token
        or token.startswith(".")
    )


def _resolve_real_path(path: Path) -> Path | None:
    try:
        return path.resolve(strict=True)
    except Exception:
        return None


def _resolve_executable_real_path(executable: str, *, cwd: Path) -> Path | None:
    token = str(executable).strip()
    if not token:
        return None
    if _looks_like_path_token(token):
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = cwd / candidate
        return _resolve_real_path(candidate)
    resolved = shutil.which(token)
    if not resolved:
        return None
    return _resolve_real_path(Path(resolved))


def _load_extract_parser_security_policy(repo_root: Path) -> dict[str, Any]:
    policy = _load_verifier_policy(repo_root)
    extract = policy.get("extract_results")
    if not isinstance(extract, dict):
        extract = {}
    parser_block = extract.get("parser")
    if not isinstance(parser_block, dict):
        parser_block = {}

    allow_command_hook = bool(parser_block.get("allow_command_hook", True))
    allow_external_python_modules = bool(
        parser_block.get("allow_external_python_modules", False)
    )
    raw_allowlist = parser_block.get("command_allowlist")
    if not isinstance(raw_allowlist, list):
        raw_allowlist = parser_block.get("command_allowlist_prefixes")
    command_allowlist: list[str] = []
    if isinstance(raw_allowlist, list):
        for item in raw_allowlist:
            token = str(item).strip()
            if token and token not in command_allowlist:
                command_allowlist.append(token)

    timeout_seconds = _parse_timeout_seconds(
        parser_block.get("command_timeout_seconds"),
        default=300.0,
        field_name="extract_results.parser.command_timeout_seconds",
    )
    return {
        "allow_command_hook": allow_command_hook,
        "allow_external_python_modules": allow_external_python_modules,
        "command_allowlist": tuple(command_allowlist),
        "command_timeout_seconds": timeout_seconds,
    }


def _load_design_payload(iteration_dir: Path) -> dict[str, Any]:
    design_path = iteration_dir / "design.yaml"
    if not design_path.exists():
        raise StageCheckError(f"extract_results requires design.yaml at {design_path}")
    if yaml is None:
        raise StageCheckError("extract_results execution requires PyYAML")
    try:
        loaded = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageCheckError(f"could not parse design.yaml: {exc}") from exc
    if not isinstance(loaded, dict):
        raise StageCheckError("design.yaml must contain a mapping")
    return loaded


def _resolve_extract_parser_hook(
    design_payload: dict[str, Any],
) -> dict[str, Any] | None:
    candidates: list[Any] = []
    candidates.append(design_payload.get("extract_parser"))

    extract_results = design_payload.get("extract_results")
    if isinstance(extract_results, dict):
        candidates.append(extract_results.get("parser"))

    extract_block = design_payload.get("extract")
    if isinstance(extract_block, dict):
        candidates.append(extract_block.get("parser"))

    hook: dict[str, Any] | None = None
    for candidate in candidates:
        if isinstance(candidate, dict):
            hook = candidate
            break
    if hook is None:
        return None

    kind = str(hook.get("kind") or hook.get("type") or "").strip().lower()
    if kind not in {"python", "command"}:
        raise StageCheckError(
            "design extract parser hook kind must be 'python' or 'command'"
        )

    if kind == "python":
        module = str(hook.get("module", "")).strip()
        callable_name = str(
            hook.get("callable") or hook.get("function") or "parse_results"
        ).strip()
        if not module:
            raise StageCheckError(
                "design extract parser hook kind=python requires 'module'"
            )
        if not callable_name:
            raise StageCheckError(
                "design extract parser hook kind=python requires callable/function name"
            )
        return {"kind": "python", "module": module, "callable": callable_name}

    command = str(hook.get("command") or hook.get("template") or "").strip()
    if not command:
        raise StageCheckError(
            "design extract parser hook kind=command requires 'command'"
        )
    working_dir = str(hook.get("working_dir", "")).strip()
    timeout_seconds = _parse_timeout_seconds(
        hook.get("timeout_seconds"),
        default=0.0,
        field_name="design extract parser hook timeout_seconds",
    )
    return {
        "kind": "command",
        "command": command,
        "working_dir": working_dir,
        "timeout_seconds": timeout_seconds,
    }


def _dedupe_paths(paths: list[Path]) -> tuple[Path, ...]:
    ordered: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return tuple(ordered)


def _write_text_if_changed(path: Path, text: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_text(encoding="utf-8") == text:
                return False
        except Exception:
            pass
    path.write_text(text, encoding="utf-8")
    return True


def _write_json_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    rendered = json.dumps(payload, indent=2) + "\n"
    return _write_text_if_changed(path, rendered)


def _render_template_command(template: str, variables: dict[str, str]) -> str:
    escaped_variables = {
        key: shlex.quote(str(value)) for key, value in variables.items()
    }
    try:
        return template.format_map(escaped_variables)
    except Exception as exc:
        raise StageCheckError(
            f"invalid extract command template '{template}': {exc}"
        ) from exc


def _template_command_argv(
    template: str,
    variables: dict[str, str],
    *,
    context: str,
) -> list[str]:
    rendered = _render_template_command(template, variables)
    if _SHELL_META_PATTERN.search(rendered):
        raise StageCheckError(
            f"{context} contains shell metacharacters; use an argv-safe command template"
        )
    try:
        argv = shlex.split(rendered)
    except ValueError as exc:
        raise StageCheckError(f"{context} could not be parsed: {exc}") from exc
    if not argv:
        raise StageCheckError(f"{context} resolved to an empty command")
    return argv


def _command_matches_allowlist(
    command: str, allowlist: tuple[str, ...], *, cwd: Path
) -> bool:
    if not allowlist:
        return True
    executable_real = _resolve_executable_real_path(command, cwd=cwd)
    if executable_real is None:
        return False
    executable_name = executable_real.name
    for raw_entry in allowlist:
        entry = str(raw_entry).strip()
        if not entry:
            continue
        if not _looks_like_path_token(entry):
            if executable_name == entry:
                return True
            continue
        entry_path = Path(entry)
        if not entry_path.is_absolute():
            entry_path = cwd / entry_path
        entry_real = _resolve_real_path(entry_path)
        if entry_real is None:
            continue
        if entry_real.is_dir():
            try:
                executable_real.relative_to(entry_real)
                return True
            except ValueError:
                continue
        if executable_real == entry_real:
            return True
    return False


def _apply_parser_result(
    *,
    payload: Any,
    run_dir: Path,
    iteration_dir: Path,
    changed_files: list[Path],
) -> None:
    if not isinstance(payload, dict):
        return
    metrics_payload = payload.get("metrics")
    if isinstance(metrics_payload, dict):
        metrics_path = run_dir / "metrics.json"
        if _write_json_if_changed(metrics_path, metrics_payload):
            changed_files.append(metrics_path)

    summary_value = payload.get("summary_markdown")
    if summary_value is None:
        summary_value = payload.get("summary")
    if isinstance(summary_value, str) and summary_value.strip():
        summary_path = iteration_dir / "analysis" / "summary.md"
        rendered_summary = summary_value.rstrip() + "\n"
        if _write_text_if_changed(summary_path, rendered_summary):
            changed_files.append(summary_path)


def _execute_python_parser_hook(
    *,
    repo_root: Path,
    iteration_dir: Path,
    run_id: str,
    hook: dict[str, Any],
    state: dict[str, Any],
    design_payload: dict[str, Any],
    allow_external_python_modules: bool,
    changed_files: list[Path],
) -> None:
    module_name = _validate_python_module_name(str(hook["module"]))
    callable_name = str(hook["callable"])
    expected_module_source: Path | None = None
    if not allow_external_python_modules:
        expected_module_source = _resolve_repo_python_module_source(
            repo_root, module_name
        )

    original_sys_path: list[str] | None = None
    if not allow_external_python_modules:
        repo_root_str = str(repo_root)
        original_sys_path = list(sys.path)
        sys.path = [
            repo_root_str,
            *[entry for entry in sys.path if entry != repo_root_str],
        ]
    try:
        try:
            module = importlib.import_module(module_name)
        finally:
            if original_sys_path is not None:
                sys.path = original_sys_path
    except Exception as exc:
        raise StageCheckError(
            f"extract parser hook could not import module '{module_name}': {exc}"
        ) from exc

    module_source = (
        getattr(module, "__file__", "") or inspect.getsourcefile(module) or ""
    )
    if not allow_external_python_modules:
        if not module_source:
            raise StageCheckError(
                "extract parser hook module must resolve to a file under the repository root"
            )
        resolved_module_source = _ensure_path_within(
            repo_root,
            Path(module_source),
            field=f"extract parser hook module '{module_name}'",
        )
        if expected_module_source is not None:
            expected_resolved = expected_module_source.resolve(strict=False)
            module_resolved = resolved_module_source.resolve(strict=False)
            if module_resolved != expected_resolved:
                raise StageCheckError(
                    f"extract parser hook module '{module_name}' resolved to unexpected source '{module_resolved}'"
                )

    current: Any = module
    for part in callable_name.split("."):
        attr = part.strip()
        if not attr:
            raise StageCheckError(
                f"extract parser hook callable '{callable_name}' is invalid"
            )
        if not _PYTHON_IDENTIFIER_PATTERN.fullmatch(attr):
            raise StageCheckError(
                f"extract parser hook callable '{callable_name}' contains unsafe attribute '{attr}'"
            )
        if not hasattr(current, attr):
            raise StageCheckError(
                f"extract parser hook callable '{callable_name}' is missing in module '{module_name}'"
            )
        current = getattr(current, attr)
    if not callable(current):
        raise StageCheckError(
            f"extract parser hook target '{module_name}:{callable_name}' is not callable"
        )

    run_id = _validate_run_id(run_id)
    kwargs = {
        "repo_root": str(repo_root),
        "iteration_dir": str(iteration_dir),
        "run_id": run_id,
        "state": state,
        "design": design_payload,
    }
    run_dir = _resolve_run_dir(iteration_dir, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    try:
        signature = inspect.signature(current)
        if any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in signature.parameters.values()
        ):
            result = current(**kwargs)
        else:
            accepted_kwargs = {
                key: value
                for key, value in kwargs.items()
                if key in signature.parameters
            }
            result = current(**accepted_kwargs)
    except TypeError:
        # Backward-compatible fallback for simple positional parser hooks.
        result = current(str(iteration_dir), run_id)
    except Exception as exc:
        raise StageCheckError(
            f"extract parser hook '{module_name}:{callable_name}' failed for run_id={run_id}: {exc}"
        ) from exc

    _apply_parser_result(
        payload=result,
        run_dir=run_dir,
        iteration_dir=iteration_dir,
        changed_files=changed_files,
    )


def _execute_command_parser_hook(
    *,
    repo_root: Path,
    iteration_dir: Path,
    run_id: str,
    hook: dict[str, Any],
    command_allowlist: tuple[str, ...],
    timeout_seconds: float,
    changed_files: list[Path],
) -> None:
    run_id = _validate_run_id(run_id)
    command_template = str(hook["command"]).strip()
    working_dir = str(hook.get("working_dir", "")).strip()
    run_dir = _resolve_run_dir(iteration_dir, run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    variables = {
        "run_id": run_id,
        "iteration_id": iteration_dir.name,
        "iteration_path": str(iteration_dir),
        "run_dir": str(run_dir),
        "repo_root": str(repo_root),
    }
    command = _template_command_argv(
        command_template,
        variables,
        context=f"extract command parser for run_id={run_id}",
    )
    # Some environments only expose the active interpreter as `python3` or a
    # versioned executable. Preserve explicit `python`/`python3` hooks when
    # available, but fall back to the current interpreter if the requested shim
    # does not exist.
    if (
        command
        and command[0] in {"python", "python3"}
        and shutil.which(command[0]) is None
    ):
        command[0] = sys.executable
    cwd = repo_root
    if working_dir:
        raw_dir = Path(working_dir)
        cwd_candidate = raw_dir if raw_dir.is_absolute() else (repo_root / raw_dir)
        cwd = _ensure_path_within(
            repo_root,
            cwd_candidate,
            field="extract parser hook working_dir",
        )
    if not _command_matches_allowlist(command[0], command_allowlist, cwd=cwd):
        raise StageCheckError(
            "extract command parser hook executable is not allowed by "
            "extract_results.parser.command_allowlist"
        )

    try:
        proc = subprocess.run(
            command,
            cwd=cwd,
            shell=False,
            text=True,
            capture_output=True,
            check=False,
            timeout=float(timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        raise StageCheckError(
            f"extract command parser timed out for run_id={run_id}: {exc}"
        ) from exc
    except OSError as exc:
        raise StageCheckError(
            f"extract command parser execution failed for run_id={run_id}: {exc}"
        ) from exc

    stdout = str(proc.stdout or "")
    stderr = str(proc.stderr or "")
    stdout_path = logs_dir / "extract.parser.stdout.log"
    stderr_path = logs_dir / "extract.parser.stderr.log"
    if _write_text_if_changed(stdout_path, stdout):
        changed_files.append(stdout_path)
    if _write_text_if_changed(stderr_path, stderr):
        changed_files.append(stderr_path)

    if int(proc.returncode) != 0:
        raise StageCheckError(
            f"extract command parser failed for run_id={run_id} with exit_code={proc.returncode}"
        )

    parsed_payload: Any = None
    if stdout.strip():
        try:
            parsed_payload = json.loads(stdout)
        except Exception:
            parsed_payload = None
    _apply_parser_result(
        payload=parsed_payload,
        run_dir=run_dir,
        iteration_dir=iteration_dir,
        changed_files=changed_files,
    )


def _ensure_summary_with_llm_command(
    *,
    repo_root: Path,
    iteration_dir: Path,
    run_id: str,
    llm_command_template: str,
    timeout_seconds: float,
    changed_files: list[Path],
) -> None:
    summary_path = iteration_dir / "analysis" / "summary.md"
    existing_text = (
        summary_path.read_text(encoding="utf-8").strip()
        if summary_path.exists()
        else ""
    )
    if existing_text:
        return

    policy = _load_verifier_policy(repo_root)
    python_bin = _resolve_policy_python_bin(policy)
    command_template = _resolve_policy_command(
        llm_command_template, python_bin=python_bin
    )
    if not command_template:
        raise StageCheckError(
            "extract_results summary is missing and extract_results.summary.llm_command is not configured"
        )

    run_id = _validate_run_id(run_id)
    run_dir = _resolve_run_dir(iteration_dir, run_id)
    variables = {
        "run_id": run_id,
        "iteration_id": iteration_dir.name,
        "iteration_path": str(iteration_dir),
        "run_dir": str(run_dir),
        "repo_root": str(repo_root),
    }
    command = _template_command_argv(
        command_template,
        variables,
        context=f"extract summary llm command for run_id={run_id}",
    )
    try:
        proc = subprocess.run(
            command,
            cwd=repo_root,
            shell=False,
            text=True,
            capture_output=True,
            check=False,
            timeout=float(timeout_seconds),
        )
    except subprocess.TimeoutExpired as exc:
        raise StageCheckError(
            f"extract summary llm command timed out for run_id={run_id}: {exc}"
        ) from exc
    except OSError as exc:
        raise StageCheckError(
            f"extract summary llm command failed for run_id={run_id}: {exc}"
        ) from exc

    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = logs_dir / "extract.summary_llm.stdout.log"
    stderr_path = logs_dir / "extract.summary_llm.stderr.log"
    if _write_text_if_changed(stdout_path, str(proc.stdout or "")):
        changed_files.append(stdout_path)
    if _write_text_if_changed(stderr_path, str(proc.stderr or "")):
        changed_files.append(stderr_path)

    if int(proc.returncode) != 0:
        raise StageCheckError(
            f"extract summary llm command failed for run_id={run_id} with exit_code={proc.returncode}"
        )

    generated_text = (
        summary_path.read_text(encoding="utf-8").strip()
        if summary_path.exists()
        else ""
    )
    if not generated_text:
        raise StageCheckError(
            "extract summary llm command completed but analysis/summary.md is still missing or empty"
        )
    changed_files.append(summary_path)


def _execute_extract_runtime(
    repo_root: Path, *, state: dict[str, Any]
) -> ExtractExecutionResult:
    iteration_id = str(state.get("iteration_id", "")).strip()
    if not iteration_id:
        raise StageCheckError("extract_results execution requires state.iteration_id")
    iteration_dir, _iteration_type = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=str(state.get("experiment_id", "")).strip(),
        require_exists=False,
    )

    raw_run_id = str(state.get("last_run_id", "")).strip()
    if not raw_run_id:
        raise StageCheckError("extract_results execution requires state.last_run_id")
    run_id = _validate_run_id(raw_run_id)

    runtime_cfg = _load_extract_runtime_config(repo_root)
    parser_security = _load_extract_parser_security_policy(repo_root)
    try:
        design_payload = _load_design_payload(iteration_dir)
    except StageCheckError:
        if runtime_cfg.require_parser_hook:
            raise
        _append_log(
            repo_root,
            "extract runtime skipped parser hook resolution (design.yaml unavailable)",
        )
        return ExtractExecutionResult(run_id=run_id, changed_files=())

    parser_hook = _resolve_extract_parser_hook(design_payload)
    if runtime_cfg.require_parser_hook and parser_hook is None:
        raise StageCheckError(
            "extract_results parser hook is required by policy but missing from design.yaml"
        )
    if parser_hook is None:
        return ExtractExecutionResult(run_id=run_id, changed_files=())
    if parser_hook["kind"] == "command" and not parser_security["allow_command_hook"]:
        raise StageCheckError(
            "extract command parser hook is disabled by policy "
            "(extract_results.parser.allow_command_hook=false)"
        )

    run_group_raw = state.get("run_group")
    run_group: list[str] = []
    if isinstance(run_group_raw, list):
        for item in run_group_raw:
            candidate = str(item).strip()
            if not candidate:
                continue
            normalized = _validate_run_id(candidate)
            if normalized not in run_group:
                run_group.append(normalized)
    target_run_ids: list[str] = [run_id]
    if run_group:
        for replicate_id in run_group:
            if replicate_id not in target_run_ids:
                target_run_ids.append(replicate_id)

    changed_files: list[Path] = []
    for target_run_id in target_run_ids:
        if parser_hook["kind"] == "python":
            _execute_python_parser_hook(
                repo_root=repo_root,
                iteration_dir=iteration_dir,
                run_id=target_run_id,
                hook=parser_hook,
                state=state,
                design_payload=design_payload,
                allow_external_python_modules=bool(
                    parser_security["allow_external_python_modules"]
                ),
                changed_files=changed_files,
            )
        else:
            hook_timeout_seconds = float(parser_hook.get("timeout_seconds", 0.0) or 0.0)
            effective_timeout_seconds = (
                hook_timeout_seconds
                if hook_timeout_seconds > 0
                else float(parser_security["command_timeout_seconds"])
            )
            _execute_command_parser_hook(
                repo_root=repo_root,
                iteration_dir=iteration_dir,
                run_id=target_run_id,
                hook=parser_hook,
                command_allowlist=parser_security["command_allowlist"],
                timeout_seconds=effective_timeout_seconds,
                changed_files=changed_files,
            )

    if runtime_cfg.summary_mode == "llm_on_demand":
        _ensure_summary_with_llm_command(
            repo_root=repo_root,
            iteration_dir=iteration_dir,
            run_id=run_id,
            llm_command_template=runtime_cfg.summary_llm_command,
            timeout_seconds=runtime_cfg.summary_llm_timeout_seconds,
            changed_files=changed_files,
        )

    for target_run_id in target_run_ids:
        metrics_path = _resolve_run_dir(iteration_dir, target_run_id) / "metrics.json"
        if (
            not metrics_path.exists()
            or not metrics_path.read_text(encoding="utf-8").strip()
        ):
            raise StageCheckError(
                f"extract parser hook did not produce metrics.json for run_id={target_run_id}"
            )

    summary_path = iteration_dir / "analysis" / "summary.md"
    if (
        not summary_path.exists()
        or not summary_path.read_text(encoding="utf-8").strip()
    ):
        raise StageCheckError("extract parser hook did not produce analysis/summary.md")

    _append_log(
        repo_root,
        (
            "extract runtime complete "
            f"run_id={run_id} parser_kind={parser_hook['kind']} targets={target_run_ids}"
        ),
    )
    return ExtractExecutionResult(
        run_id=run_id,
        changed_files=_dedupe_paths(changed_files),
    )
