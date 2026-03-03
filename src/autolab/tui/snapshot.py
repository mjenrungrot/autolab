from __future__ import annotations

import json
import mimetypes
from pathlib import Path
from typing import Any

from autolab.constants import (
    ALL_STAGES,
    ACTIVE_STAGES,
    STAGE_PROMPT_FILES,
    TERMINAL_STAGES,
)
from autolab.prompts import (
    _render_stage_prompt,
    _resolve_stage_prompt_path as _resolve_render_template_path,
)
from autolab.state import (
    _load_backlog_yaml,
    _load_state,
    _normalize_state,
    _resolve_autolab_dir,
    _resolve_iteration_directory,
    _resolve_repo_root,
)
from autolab.todo_sync import list_open_tasks
from autolab.utils import _is_backlog_status_completed
from autolab.tui.models import (
    ArtifactItem,
    BacklogExperimentItem,
    BacklogHypothesisItem,
    CockpitSnapshot,
    RenderPreview,
    RecommendedAction,
    RunItem,
    StageItem,
    TodoItem,
    VerificationSummary,
)

_STAGE_SUMMARY: dict[str, str] = {
    "hypothesis": "Define the hypothesis and measurable target delta.",
    "design": "Specify design.yaml with entrypoint, compute, metrics, and variants.",
    "implementation": "Implement changes and capture implementation evidence.",
    "implementation_review": "Record implementation review outcome and required checks.",
    "launch": "Run launch command and produce run manifest.",
    "slurm_monitor": "Track manifest/scheduler progress and sync evidence.",
    "extract_results": "Generate run metrics and analysis summary artifacts.",
    "update_docs": "Update docs/paper with run evidence and references.",
    "decide_repeat": "Choose next action: hypothesis, design, stop, or human_review.",
    "human_review": "Human intervention required before continuing.",
    "stop": "Terminal stage; experiment completed.",
}

_STAGE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "hypothesis": ("hypothesis.md",),
    "design": ("design.yaml",),
    "implementation": ("implementation_plan.md",),
    "implementation_review": ("implementation_review.md", "review_result.json"),
    "launch": (
        "launch/run_local.sh",
        "launch/run_slurm.sbatch",
        "runs/{run_id}/run_manifest.json",
    ),
    "slurm_monitor": ("runs/{run_id}/run_manifest.json", "runs/{run_id}/metrics.json"),
    "extract_results": ("runs/{run_id}/metrics.json", "analysis/summary.md"),
    "update_docs": ("docs_update.md",),
    "decide_repeat": ("decision_result.json",),
    "human_review": ("implementation_review.md", "review_result.json"),
    "stop": (),
}

_COMMON_ARTIFACTS: tuple[str, ...] = (
    ".autolab/state.json",
    ".autolab/verification_result.json",
    ".autolab/todo_state.json",
    "docs/todo.md",
)

_TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".txt", ".json", ".yaml", ".yml", ".log", ".py", ".sh", ".toml"}
)
_RENDER_EXCERPT_MAX_LINES = 14
_RENDER_EXCERPT_MAX_CHARS = 1200


def _safe_json_load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _coerce_int(
    raw_value: object,
    *,
    default: int,
    minimum: int | None = None,
) -> int:
    try:
        value = int(raw_value)  # type: ignore[arg-type]
    except Exception:
        value = default
    if minimum is not None and value < minimum:
        value = minimum
    return value


def _format_attempts(
    *, stage_name: str, current_stage: str, stage_attempt: int, max_stage_attempts: int
) -> str:
    if stage_name != current_stage:
        return "-"
    normalized_attempt = _coerce_int(stage_attempt, default=0, minimum=0)
    normalized_max = _coerce_int(max_stage_attempts, default=1, minimum=1)
    return f"{normalized_attempt}/{normalized_max}"


def _build_stage_items(
    *,
    current_stage: str,
    stage_attempt: int,
    max_stage_attempts: int,
    verification: VerificationSummary | None,
) -> tuple[StageItem, ...]:
    ordered_stages = list(ACTIVE_STAGES) + list(TERMINAL_STAGES)
    current_index = (
        ordered_stages.index(current_stage) if current_stage in ordered_stages else -1
    )
    current_blocked = (
        bool(
            verification
            and not verification.passed
            and verification.stage_effective == current_stage
        )
        or current_stage == "human_review"
    )

    items: list[StageItem] = []
    for index, stage_name in enumerate(ordered_stages):
        if stage_name == current_stage:
            status = "blocked" if current_blocked else "current"
        elif (
            current_index >= 0 and index < current_index and stage_name in ACTIVE_STAGES
        ):
            status = "complete"
        else:
            status = "upcoming"
        items.append(
            StageItem(
                name=stage_name,
                status=status,
                attempts=_format_attempts(
                    stage_name=stage_name,
                    current_stage=current_stage,
                    stage_attempt=stage_attempt,
                    max_stage_attempts=max_stage_attempts,
                ),
                is_current=stage_name == current_stage,
            )
        )
    return tuple(items)


def _resolve_stage_artifacts(
    *,
    repo_root: Path,
    iteration_dir: Path | None,
    last_run_id: str,
) -> dict[str, tuple[ArtifactItem, ...]]:
    artifact_map: dict[str, tuple[ArtifactItem, ...]] = {}
    for stage_name in [*ACTIVE_STAGES, *TERMINAL_STAGES]:
        paths: list[ArtifactItem] = []
        for template in _STAGE_ARTIFACTS.get(stage_name, ()):
            relative = (
                template.format(run_id=last_run_id)
                if "{run_id}" in template
                else template
            )
            if "{run_id}" in template and not last_run_id:
                continue
            if relative.startswith(".autolab/") or relative.startswith("docs/"):
                target = repo_root / relative
            elif iteration_dir is None:
                continue
            else:
                target = iteration_dir / relative
            paths.append(
                ArtifactItem(
                    path=target,
                    exists=target.exists(),
                    source="stage",
                )
            )
        artifact_map[stage_name] = tuple(paths)
    return artifact_map


def _load_runs(iteration_dir: Path | None) -> tuple[RunItem, ...]:
    if iteration_dir is None:
        return ()
    runs_root = iteration_dir / "runs"
    if not runs_root.exists():
        return ()
    runs: list[RunItem] = []
    for manifest_path in sorted(runs_root.glob("*/run_manifest.json")):
        payload = _safe_json_load(manifest_path) or {}
        run_id = str(payload.get("run_id", "")).strip() or manifest_path.parent.name
        timestamps = (
            payload.get("timestamps", {})
            if isinstance(payload.get("timestamps"), dict)
            else {}
        )
        started_at = str(timestamps.get("started_at", "")).strip()
        completed_at = str(timestamps.get("completed_at", "")).strip()
        status = str(payload.get("status", "")).strip()
        host_mode = (
            str(payload.get("host_mode", "")).strip()
            or str(payload.get("launch_mode", "")).strip()
            or "unknown"
        )
        sync_payload = payload.get("artifact_sync_to_local", {})
        sync_status = ""
        if isinstance(sync_payload, dict):
            sync_status = str(sync_payload.get("status", "")).strip()
        job_id = str(payload.get("job_id", "")).strip()
        if not job_id:
            nested_slurm = payload.get("slurm", {})
            if isinstance(nested_slurm, dict):
                job_id = str(nested_slurm.get("job_id", "")).strip()
        if not status:
            status = sync_status
        if not status:
            status = "unknown"
        metrics_path = manifest_path.parent / "metrics.json"
        runs.append(
            RunItem(
                run_id=run_id,
                status=status,
                host_mode=host_mode,
                job_id=job_id,
                sync_status=sync_status,
                started_at=started_at,
                completed_at=completed_at,
                manifest_path=manifest_path,
                metrics_path=metrics_path,
            )
        )
    runs.sort(
        key=lambda item: (item.started_at, item.run_id, item.manifest_path.as_posix()),
        reverse=True,
    )
    return tuple(runs)


def _load_verification(autolab_dir: Path) -> VerificationSummary | None:
    payload = _safe_json_load(autolab_dir / "verification_result.json")
    if payload is None:
        return None
    details = payload.get("details", {})
    failing_commands: list[str] = []
    if isinstance(details, dict):
        raw_commands = details.get("commands", [])
        if isinstance(raw_commands, list):
            for command_result in raw_commands:
                if not isinstance(command_result, dict):
                    continue
                status = str(command_result.get("status", "")).strip().lower()
                if status not in {"fail", "timeout", "error"}:
                    continue
                name = str(command_result.get("name", "")).strip() or "unknown"
                detail = (
                    str(command_result.get("detail", "")).strip()
                    or str(command_result.get("stderr", "")).strip()
                    or str(command_result.get("stdout", "")).strip()
                    or "verification command returned non-zero"
                )
                failing_commands.append(f"{name}: {detail}")
    return VerificationSummary(
        generated_at=str(payload.get("generated_at", "")).strip(),
        stage_effective=str(payload.get("stage_effective", "")).strip(),
        passed=bool(payload.get("passed", False)),
        message=str(payload.get("message", "")).strip(),
        failing_commands=tuple(failing_commands),
    )


def _load_todos(repo_root: Path) -> tuple[TodoItem, ...]:
    try:
        raw_todos = list_open_tasks(repo_root)
    except Exception:
        return ()
    todos: list[TodoItem] = []
    for raw in raw_todos:
        if not isinstance(raw, dict):
            continue
        todos.append(
            TodoItem(
                task_id=str(raw.get("task_id", "")).strip(),
                source=str(raw.get("source", "")).strip(),
                stage=str(raw.get("stage", "")).strip(),
                task_class=str(raw.get("task_class", "")).strip(),
                text=str(raw.get("text", "")).strip(),
                priority=str(raw.get("priority", "")).strip(),
            )
        )
    return tuple(todos)


def _load_review_blockers(iteration_dir: Path | None) -> tuple[str, ...]:
    if iteration_dir is None:
        return ()
    review_payload = _safe_json_load(iteration_dir / "review_result.json")
    if review_payload is None:
        return ()
    findings = review_payload.get("blocking_findings", [])
    if not isinstance(findings, list):
        return ()
    blockers: list[str] = []
    for finding in findings:
        text = str(finding).strip()
        if text:
            blockers.append(text)
    return tuple(blockers)


def _load_backlog_items(
    *,
    repo_root: Path,
    current_iteration_id: str,
    current_experiment_id: str,
) -> tuple[tuple[BacklogExperimentItem, ...], tuple[BacklogHypothesisItem, ...], str]:
    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    payload, load_error = _load_backlog_yaml(backlog_path)
    if payload is None:
        return ((), (), load_error)

    errors: list[str] = []

    experiments_raw = payload.get("experiments")
    experiment_items: list[BacklogExperimentItem] = []
    if not isinstance(experiments_raw, list):
        errors.append("backlog experiments list is missing")
    else:
        for raw in experiments_raw:
            if not isinstance(raw, dict):
                continue
            experiment_id = str(raw.get("id", "")).strip()
            iteration_id = str(raw.get("iteration_id", "")).strip()
            hypothesis_id = str(raw.get("hypothesis_id", "")).strip()
            experiment_type = str(raw.get("type", "")).strip()
            status = str(raw.get("status", "")).strip()
            is_current = (
                bool(experiment_id)
                and bool(iteration_id)
                and experiment_id == current_experiment_id
                and iteration_id == current_iteration_id
            )
            experiment_items.append(
                BacklogExperimentItem(
                    experiment_id=experiment_id,
                    iteration_id=iteration_id,
                    hypothesis_id=hypothesis_id,
                    experiment_type=experiment_type,
                    status=status,
                    is_current=is_current,
                )
            )

    hypotheses_raw = payload.get("hypotheses")
    hypothesis_items: list[BacklogHypothesisItem] = []
    if not isinstance(hypotheses_raw, list):
        errors.append("backlog hypotheses list is missing")
    else:
        for raw in hypotheses_raw:
            if not isinstance(raw, dict):
                continue
            hypothesis_id = str(raw.get("id", "")).strip()
            title = str(raw.get("title", "")).strip()
            status = str(raw.get("status", "")).strip()
            hypothesis_items.append(
                BacklogHypothesisItem(
                    hypothesis_id=hypothesis_id,
                    title=title,
                    status=status,
                    is_completed=_is_backlog_status_completed(status),
                )
            )

    return (
        tuple(experiment_items),
        tuple(hypothesis_items),
        "; ".join(errors),
    )


def _build_common_artifacts(
    repo_root: Path,
    iteration_dir: Path | None,
) -> tuple[ArtifactItem, ...]:
    entries: list[ArtifactItem] = []
    for relative in _COMMON_ARTIFACTS:
        path = repo_root / relative
        entries.append(ArtifactItem(path=path, exists=path.exists(), source="common"))
    if iteration_dir is not None:
        review_path = iteration_dir / "review_result.json"
        entries.append(
            ArtifactItem(path=review_path, exists=review_path.exists(), source="common")
        )
    return tuple(entries)


def _merge_blockers(
    verification: VerificationSummary | None,
    review_blockers: tuple[str, ...],
) -> tuple[str, ...]:
    blockers: list[str] = []
    seen: set[str] = set()
    if verification is not None:
        if not verification.passed and verification.message:
            blockers.append(verification.message)
        blockers.extend(verification.failing_commands)
    blockers.extend(review_blockers)
    deduped: list[str] = []
    for blocker in blockers:
        normalized = blocker.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
    return tuple(deduped[:10])


def _build_render_excerpt(
    text: str,
    *,
    max_lines: int = _RENDER_EXCERPT_MAX_LINES,
    max_chars: int = _RENDER_EXCERPT_MAX_CHARS,
) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return "(rendered prompt is empty)"

    clipped_lines = lines[: max(1, max_lines)]
    excerpt = "\n".join(clipped_lines)
    truncated_by_lines = len(lines) > len(clipped_lines)

    if len(excerpt) > max_chars:
        excerpt = excerpt[:max_chars].rstrip()
        truncated_by_lines = True
    if truncated_by_lines:
        excerpt = f"{excerpt}\n..."
    return excerpt


def _load_render_preview(
    *,
    repo_root: Path,
    current_stage: str,
    state: dict[str, Any],
) -> RenderPreview:
    stage_name = str(current_stage).strip()
    if not stage_name:
        return RenderPreview(
            stage="",
            status="unavailable",
            template_path=None,
            prompt_text="",
            prompt_excerpt="Render preview unavailable: no stage is selected.",
            context_payload={},
            error_message="no stage is selected",
        )

    try:
        template_path = _resolve_render_template_path(repo_root, stage_name)
    except Exception as exc:
        message = str(exc).strip() or "unable to resolve stage prompt template"
        return RenderPreview(
            stage=stage_name,
            status="error",
            template_path=None,
            prompt_text="",
            prompt_excerpt=f"Render preview unavailable.\n{message}",
            context_payload={},
            error_message=message,
        )

    state_for_render = dict(state)
    state_for_render["stage"] = stage_name
    try:
        bundle = _render_stage_prompt(
            repo_root,
            stage=stage_name,
            state=state_for_render,
            template_path=template_path,
            runner_scope=None,
            write_outputs=False,
        )
    except Exception as exc:
        message = str(exc).strip() or "render failed"
        return RenderPreview(
            stage=stage_name,
            status="error",
            template_path=template_path,
            prompt_text="",
            prompt_excerpt=f"Render preview failed.\n{message}",
            context_payload={},
            error_message=message,
        )

    return RenderPreview(
        stage=stage_name,
        status="ok",
        template_path=template_path,
        prompt_text=bundle.prompt_text,
        prompt_excerpt=_build_render_excerpt(bundle.prompt_text),
        context_payload=dict(bundle.context_payload),
        error_message="",
    )


def _build_recommended_actions(
    *,
    current_stage: str,
    render_preview: RenderPreview,
    verification: VerificationSummary | None,
    stage_artifacts: tuple[ArtifactItem, ...],
    blockers: tuple[str, ...],
    todos: tuple[TodoItem, ...],
) -> tuple[RecommendedAction, ...]:
    recommended: list[RecommendedAction] = []

    if current_stage == "human_review":
        recommended.append(
            RecommendedAction(
                action_id="resolve_human_review",
                reason="Record pass, retry, or stop to resolve this human review gate.",
            )
        )
        if render_preview.status == "ok":
            recommended.append(
                RecommendedAction(
                    action_id="open_rendered_prompt",
                    reason="Preview the exact resolved prompt context for this stage.",
                )
            )
            recommended.append(
                RecommendedAction(
                    action_id="open_render_context",
                    reason="Inspect rendered token values before deciding.",
                )
            )
            recommended.append(
                RecommendedAction(
                    action_id="open_stage_prompt",
                    reason="Open stage guidance and decision instructions.",
                )
            )
        else:
            recommended.append(
                RecommendedAction(
                    action_id="open_stage_prompt",
                    reason="Open stage guidance and decision instructions.",
                )
            )
        recommended.append(
            RecommendedAction(
                action_id="open_state_history",
                reason="Review recent stage transitions and blockers before deciding.",
            )
        )
        if todos:
            recommended.append(
                RecommendedAction(
                    action_id="todo_sync",
                    reason="Open todo tasks found; sync todo state with docs.",
                )
            )
    else:
        if render_preview.status == "ok":
            recommended.append(
                RecommendedAction(
                    action_id="open_rendered_prompt",
                    reason="Start here: preview the exact resolved prompt for this stage.",
                )
            )
            recommended.append(
                RecommendedAction(
                    action_id="open_render_context",
                    reason="Check render context values before running commands.",
                )
            )
            recommended.append(
                RecommendedAction(
                    action_id="open_stage_prompt",
                    reason="Open the prompt template source for stage-specific edits.",
                )
            )
        else:
            recommended.append(
                RecommendedAction(
                    action_id="open_stage_prompt",
                    reason="Start here: open stage guidance and resolve prompt issues.",
                )
            )

        missing_required = [item for item in stage_artifacts if not item.exists]
        if missing_required:
            recommended.append(
                RecommendedAction(
                    action_id="run_once",
                    reason=(
                        f"{len(missing_required)} required file(s) are missing for stage '{current_stage}'."
                    ),
                )
            )
        else:
            recommended.append(
                RecommendedAction(
                    action_id="run_once",
                    reason="Required files look ready; run one transition.",
                )
            )

    if verification is None:
        recommended.append(
            RecommendedAction(
                action_id="verify_current_stage",
                reason="No verification result found for this workspace.",
            )
        )
    elif not verification.passed and verification.stage_effective == current_stage:
        recommended.append(
            RecommendedAction(
                action_id="verify_current_stage",
                reason="Verification is failing for this stage.",
            )
        )

        if verification.failing_commands:
            recommended.append(
                RecommendedAction(
                    action_id="open_verification_result",
                    reason="Open the verification result to inspect failing command details.",
                )
            )
    else:
        recommended.append(
            RecommendedAction(
                action_id="open_verification_result",
                reason="Review the most recent verification output and metrics.",
            )
        )

    if blockers:
        recommended.append(
            RecommendedAction(
                action_id="open_state_history",
                    reason="Review state and blockers before retrying.",
                )
            )

        if todos:
            recommended.append(
                RecommendedAction(
                    action_id="todo_sync",
                    reason="Open todo tasks found; sync todo state with docs.",
                )
            )

    deduped: list[RecommendedAction] = []
    seen: set[str] = set()
    for item in recommended:
        if item.action_id in seen:
            continue
        seen.add(item.action_id)
        deduped.append(item)
    return tuple(deduped[:6])


def load_cockpit_snapshot(state_path: Path) -> CockpitSnapshot:
    resolved_state_path = state_path.expanduser().resolve()
    repo_root = _resolve_repo_root(resolved_state_path)
    autolab_dir = _resolve_autolab_dir(resolved_state_path, repo_root)

    state_payload = _load_state(resolved_state_path)
    try:
        state = _normalize_state(state_payload)
    except Exception:
        fallback_state = dict(state_payload)
        fallback_stage = str(fallback_state.get("stage", "")).strip()
        if fallback_stage not in ALL_STAGES:
            fallback_stage = ACTIVE_STAGES[0]
        state = fallback_state
        state["stage"] = fallback_stage
        state["stage_attempt"] = _coerce_int(
            fallback_state.get("stage_attempt"),
            default=0,
            minimum=0,
        )
        state["max_stage_attempts"] = _coerce_int(
            fallback_state.get("max_stage_attempts"),
            default=1,
            minimum=1,
        )
        state["max_total_iterations"] = _coerce_int(
            fallback_state.get("max_total_iterations"),
            default=1,
            minimum=1,
        )
        state["last_run_id"] = str(fallback_state.get("last_run_id", "")).strip()
        state["iteration_id"] = str(fallback_state.get("iteration_id", "")).strip()
        state["experiment_id"] = str(fallback_state.get("experiment_id", "")).strip()
    current_stage = str(state.get("stage", "")).strip()
    stage_attempt = _coerce_int(state.get("stage_attempt"), default=0, minimum=0)
    max_stage_attempts = _coerce_int(
        state.get("max_stage_attempts"), default=1, minimum=1
    )
    last_run_id = str(state.get("last_run_id", "")).strip()
    iteration_id = str(state.get("iteration_id", "")).strip()
    experiment_id = str(state.get("experiment_id", "")).strip()

    iteration_dir: Path | None = None
    if iteration_id:
        try:
            iteration_dir, _iteration_type = _resolve_iteration_directory(
                repo_root,
                iteration_id=iteration_id,
                experiment_id=experiment_id,
                require_exists=False,
            )
        except Exception:
            iteration_dir = None

    verification = _load_verification(autolab_dir)
    render_preview = _load_render_preview(
        repo_root=repo_root,
        current_stage=current_stage,
        state=state,
    )
    stage_items = _build_stage_items(
        current_stage=current_stage,
        stage_attempt=stage_attempt,
        max_stage_attempts=max_stage_attempts,
        verification=verification,
    )
    runs = _load_runs(iteration_dir)
    todos = _load_todos(repo_root)
    review_blockers = _load_review_blockers(iteration_dir)
    blockers = _merge_blockers(verification, review_blockers)
    artifacts_by_stage = _resolve_stage_artifacts(
        repo_root=repo_root,
        iteration_dir=iteration_dir,
        last_run_id=last_run_id,
    )
    backlog_experiments, backlog_hypotheses, backlog_error = _load_backlog_items(
        repo_root=repo_root,
        current_iteration_id=iteration_id,
        current_experiment_id=experiment_id,
    )
    common_artifacts = _build_common_artifacts(repo_root, iteration_dir)
    current_stage_artifacts = artifacts_by_stage.get(current_stage, ())
    recommended_actions = _build_recommended_actions(
        current_stage=current_stage,
        render_preview=render_preview,
        verification=verification,
        stage_artifacts=current_stage_artifacts,
        blockers=blockers,
        todos=todos,
    )
    primary_blocker = blockers[0] if blockers else "none"
    secondary_blockers = blockers[1:4] if blockers else ()

    return CockpitSnapshot(
        repo_root=repo_root,
        state_path=resolved_state_path,
        autolab_dir=autolab_dir,
        iteration_dir=iteration_dir,
        current_stage=current_stage,
        stage_attempt=stage_attempt,
        max_stage_attempts=max_stage_attempts,
        last_run_id=last_run_id,
        stage_items=stage_items,
        runs=runs,
        todos=todos,
        verification=verification,
        render_preview=render_preview,
        top_blockers=blockers,
        primary_blocker=primary_blocker,
        secondary_blockers=secondary_blockers,
        backlog_experiments=backlog_experiments,
        backlog_hypotheses=backlog_hypotheses,
        backlog_error=backlog_error,
        recommended_actions=recommended_actions,
        stage_summaries=dict(_STAGE_SUMMARY),
        artifacts_by_stage=artifacts_by_stage,
        common_artifacts=common_artifacts,
    )


def is_text_artifact(path: Path) -> bool:
    suffix = path.suffix.lower()
    if suffix in _TEXT_EXTENSIONS:
        return True
    guessed, _encoding = mimetypes.guess_type(str(path))
    return bool(guessed and guessed.startswith("text/"))


def load_artifact_text(
    path: Path,
    *,
    max_chars: int | None = None,
) -> tuple[str, bool]:
    if max_chars is not None and max_chars <= 0:
        max_chars = 1
    if not path.exists():
        return ("File does not exist.", False)
    if not is_text_artifact(path):
        try:
            size = path.stat().st_size
        except OSError as exc:
            return (f"Binary/unsupported artifact (size unavailable: {exc}).", False)
        return (
            f"Binary/unsupported artifact ({size} bytes). Use external editor.",
            False,
        )

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            if max_chars is None:
                text = handle.read()
            else:
                text = handle.read(max_chars + 1)
    except OSError as exc:
        return (f"Unable to read file: {exc}", False)

    truncated = False
    if max_chars is not None and len(text) > max_chars:
        truncated = True
        text = text[:max_chars]

    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(text)
            text = json.dumps(payload, indent=2, sort_keys=True)
            if max_chars is not None and len(text) > max_chars:
                truncated = True
                text = text[:max_chars]
        except Exception:
            pass
    if truncated:
        text = text + "\n\n... [truncated]"
    return (text, truncated)


def resolve_stage_prompt_path(
    snapshot: CockpitSnapshot, stage_name: str
) -> Path | None:
    _fallback_prompt_filename = STAGE_PROMPT_FILES.get(stage_name)
    try:
        return _resolve_render_template_path(snapshot.repo_root, stage_name)
    except Exception:
        if not _fallback_prompt_filename:
            return None
        return snapshot.autolab_dir / "prompts" / _fallback_prompt_filename
