from __future__ import annotations
import json
import os
import re
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from autolab.constants import (
    BACKLOG_COMPLETED_STATUSES,
    DEFAULT_EXPERIMENT_TYPE,
    PROMPT_LITERAL_TOKENS,
    PROMPT_REQUIRED_TOKENS_BY_STAGE,
    PROMPT_SHARED_INCLUDE_PATTERN,
    PROMPT_TOKEN_PATTERN,
    STAGE_BRIEF_PROMPT_FILES,
    STAGE_HUMAN_PROMPT_FILES,
    STAGE_PROMPT_FILES,
    STAGE_RUNNER_PROMPT_FILES,
)
from autolab.agent_surface import (
    build_agent_surface_guidance,
    resolve_agent_surface,
)
from autolab.config import (
    _load_agent_runner_config,
    _load_launch_execute_policy,
    _load_protected_files,
    _load_verifier_policy,
    _resolve_policy_python_bin,
)
from autolab.dataset_discovery import discover_media_inputs, summarize_root_counts
from autolab.models import RenderedPromptBundle, StageCheckError
from autolab.registry import (
    StageSpec,
    registry_brief_prompt_files,
    registry_human_prompt_files,
    load_registry,
    registry_prompt_files,
    registry_required_tokens,
    registry_runner_prompt_files,
)
from autolab.sidecar_context import resolve_context_sidecars
from autolab.sidecar_tools import build_context_guidance
from autolab.state import _resolve_iteration_directory
from autolab.utils import (
    _append_log,
    _compact_json,
    _detect_priority_host_mode,
    _extract_log_snippet,
    _extract_matching_lines,
    _load_json_if_exists,
    _safe_read_text,
    _summarize_git_changes_for_prompt,
    _utc_now,
    _write_json,
)


def _resolve_stage_prompt_path(
    repo_root: Path, stage: str, *, prompt_role: str = "runner"
) -> Path:
    registry = load_registry(repo_root)
    has_registry_workflow = bool(registry)

    if prompt_role == "runner":
        role_mapping = registry_runner_prompt_files(registry) if registry else {}
        fallback_mapping = STAGE_RUNNER_PROMPT_FILES
    elif prompt_role == "audit":
        role_mapping = registry_prompt_files(registry) if registry else {}
        fallback_mapping = STAGE_PROMPT_FILES
    elif prompt_role == "brief":
        role_mapping = registry_brief_prompt_files(registry) if registry else {}
        fallback_mapping = STAGE_BRIEF_PROMPT_FILES
    elif prompt_role == "human":
        role_mapping = registry_human_prompt_files(registry) if registry else {}
        fallback_mapping = STAGE_HUMAN_PROMPT_FILES
    else:
        raise StageCheckError(
            f"unsupported prompt role '{prompt_role}' for stage '{stage}'"
        )

    if has_registry_workflow:
        if stage not in registry:
            raise StageCheckError(
                f"no stage prompt mapping is defined for stage '{stage}' role '{prompt_role}'"
            )
        prompt_name = str(role_mapping.get(stage, "")).strip()
    else:
        prompt_name = str(fallback_mapping.get(stage, "")).strip()

    if not prompt_name:
        raise StageCheckError(
            f"no stage prompt mapping is defined for stage '{stage}' role '{prompt_role}'"
        )

    prompts_dir = repo_root / ".autolab" / "prompts"
    candidate = prompts_dir / prompt_name
    if candidate.exists():
        return candidate

    raise StageCheckError(
        f"stage prompt is missing for '{stage}' role '{prompt_role}' ({candidate})"
    )


def _resolve_prompt_shared_path(repo_root: Path, shared_name: str) -> Path:
    return repo_root / ".autolab" / "prompts" / "shared" / shared_name


def _render_prompt_includes(repo_root: Path, text: str, *, stage: str) -> str:
    """Render {{shared:...}} include directives."""
    rendered = text
    for _ in range(4):
        changed = False

        def _replace(match: re.Match[str]) -> str:
            nonlocal changed
            shared_name = match.group(1).strip()
            if not shared_name:
                return ""
            shared_path = _resolve_prompt_shared_path(repo_root, shared_name)
            if not shared_path.exists():
                raise StageCheckError(
                    f"prompt shared include '{shared_name}' is missing for stage '{stage}'"
                )
            include_text = shared_path.read_text(encoding="utf-8")
            changed = True
            return include_text

        rendered = PROMPT_SHARED_INCLUDE_PATTERN.sub(_replace, rendered)
        if not changed:
            break
    return rendered


def _default_stage_prompt_text(stage: str, *, audience: str = "audit") -> str:
    title = stage.replace("_", " ").title()
    if audience == "runner":
        return (
            f"# Stage: {stage} (runner)\n\n"
            "## ROLE\n"
            "You are the stage runner executor.\n\n"
            "## PRIMARY OBJECTIVE\n"
            f"Complete stage '{stage}' and emit required stage outputs.\n\n"
            "## OUTPUTS (STRICT)\n"
            "- Emit the required stage artifacts only.\n\n"
            "## REQUIRED INPUTS\n"
            "- `.autolab/state.json`\n\n"
            "## STOP CONDITIONS\n"
            "- Stop if required inputs are missing.\n"
            "- Stop if an edit would violate allowed edit scope from the context packet.\n"
            "- Stop if verification cannot run.\n\n"
            "## NON-NEGOTIABLES\n"
            "- Use `.autolab/prompts/rendered/{{stage}}.context.json` as the source of runtime facts.\n"
            "- Keep edits minimal and in scope.\n"
            "- Do not invent verifier outcomes.\n\n"
            "## FAILURE / RETRY BEHAVIOR\n"
            "- On failure, report blockers clearly in stage artifacts.\n"
        )
    if audience == "brief":
        return f"# Stage: {stage} (brief)\n\n## SUMMARY\n{{{{brief_summary}}}}\n"
    if audience == "human":
        return (
            f"# Stage: {stage} (human packet)\n\n"
            "## ROLE\n"
            "You are preparing a human-facing decision packet.\n\n"
            "## SUMMARY\n"
            "{{brief_summary}}\n"
        )
    return (
        f"# Stage: {title} (audit)\n\n"
        "## ROLE\n"
        "You are the stage audit contract owner.\n\n"
        "## PRIMARY OBJECTIVE\n"
        f"Define auditable policy and verifier expectations for stage '{stage}'.\n\n"
        "## OUTPUTS (STRICT)\n"
        "- Stage outputs listed in workflow contract.\n\n"
        "## REQUIRED INPUTS\n"
        "- `.autolab/state.json`\n\n"
        "## FILE CHECKLIST\n"
        "- [ ] Required outputs are concrete and schema-valid.\n\n"
        "## FAILURE / RETRY BEHAVIOR\n"
        "- Verification failure triggers retry/escalation per policy.\n"
    )


def _detect_total_memory_gb() -> int | None:
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total_pages = int(os.sysconf("SC_PHYS_PAGES"))
    except Exception:
        return None
    total_bytes = page_size * total_pages
    if total_bytes <= 0:
        return None
    return max(1, int(total_bytes / (1024**3)))


def _recommended_memory_estimate(total_memory_gb: int | None) -> str:
    if total_memory_gb is None:
        return "64GB"
    recommended_gb = min(total_memory_gb, max(64, max(1, total_memory_gb // 2)))
    return f"{recommended_gb}GB"


def _resolve_hypothesis_id(
    repo_root: Path, *, iteration_id: str, experiment_id: str
) -> str:
    candidate = ""
    if yaml is not None and iteration_id and not iteration_id.startswith("<"):
        iteration_dir, _iteration_type = _resolve_iteration_directory(
            repo_root,
            iteration_id=iteration_id,
            experiment_id=experiment_id,
            require_exists=False,
        )
        design_path = iteration_dir / "design.yaml"
        if design_path.exists():
            try:
                loaded = yaml.safe_load(design_path.read_text(encoding="utf-8"))
            except Exception:
                loaded = None
            if isinstance(loaded, dict):
                candidate = str(loaded.get("hypothesis_id", "")).strip()
                if candidate and not candidate.startswith("<"):
                    return candidate

    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    if yaml is not None and backlog_path.exists():
        try:
            loaded = yaml.safe_load(backlog_path.read_text(encoding="utf-8"))
        except Exception:
            loaded = None
        if isinstance(loaded, dict):
            experiments = loaded.get("experiments")
            if isinstance(experiments, list):
                for entry in experiments:
                    if not isinstance(entry, dict):
                        continue
                    entry_id = str(entry.get("id", "")).strip()
                    entry_iteration = str(entry.get("iteration_id", "")).strip()
                    if experiment_id and entry_id != experiment_id:
                        continue
                    if (
                        iteration_id
                        and entry_iteration
                        and entry_iteration != iteration_id
                    ):
                        continue
                    candidate = str(entry.get("hypothesis_id", "")).strip()
                    if candidate and not candidate.startswith("<"):
                        return candidate
            hypotheses = loaded.get("hypotheses")
            if isinstance(hypotheses, list):
                for entry in hypotheses:
                    if not isinstance(entry, dict):
                        continue
                    status = str(entry.get("status", "")).strip().lower()
                    if status in BACKLOG_COMPLETED_STATUSES:
                        continue
                    candidate = str(entry.get("id", "")).strip()
                    if candidate and not candidate.startswith("<"):
                        return candidate
    return "h1"


def _resolve_prompt_run_id(
    *, repo_root: Path, stage: str, state: dict[str, Any]
) -> str:
    if stage == "launch":
        pending_run_id = str(state.get("pending_run_id", "")).strip()
        if pending_run_id and not pending_run_id.startswith("<"):
            return pending_run_id
        run_context_path = repo_root / ".autolab" / "run_context.json"
        run_context_payload = _load_json_if_exists(run_context_path)
        if isinstance(run_context_payload, dict):
            context_stage = str(run_context_payload.get("stage", "")).strip()
            context_iteration = str(run_context_payload.get("iteration_id", "")).strip()
            state_iteration = str(state.get("iteration_id", "")).strip()
            context_run_id = str(run_context_payload.get("run_id", "")).strip()
            if (
                context_stage == "launch"
                and context_iteration
                and context_iteration == state_iteration
                and context_run_id
                and not context_run_id.startswith("<")
            ):
                return context_run_id
    run_id = str(state.get("last_run_id", "")).strip()
    if run_id and not run_id.startswith("<"):
        return run_id

    registry = load_registry(repo_root)
    required_tokens_by_stage = (
        registry_required_tokens(registry)
        if registry
        else PROMPT_REQUIRED_TOKENS_BY_STAGE
    )
    required_tokens = required_tokens_by_stage.get(stage, {"iteration_id"})
    if "run_id" in required_tokens:
        raise StageCheckError(
            f"prompt token '{{{{run_id}}}}' requires a resolved run_id for stage '{stage}'"
        )
    return "run_pending"


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except Exception:
        return None


def _parse_numeric_delta(value: str) -> float | None:
    text = str(value).strip()
    if not text:
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text)
    if not match:
        return None
    return _coerce_float(match.group(0))


def _parse_signed_delta(text: str) -> float | None:
    """Extract explicitly signed delta (e.g., +5.0 or -2.3%) from Success field."""
    match = re.search(r"[+-]\s*\d+(?:\.\d+)?", text)
    if match:
        return _coerce_float(match.group(0).replace(" ", ""))
    return _parse_numeric_delta(text)


def _load_metrics_payload(iteration_dir: Path, run_id: str) -> dict[str, Any] | None:
    metrics_path = iteration_dir / "runs" / run_id / "metrics.json"
    payload = _load_json_if_exists(metrics_path)
    if isinstance(payload, dict):
        return payload
    return None


def _metrics_summary_text(
    metrics_payload: dict[str, Any] | None, *, run_id: str
) -> str:
    if not isinstance(metrics_payload, dict):
        return f"unavailable: runs/{run_id}/metrics.json is missing or unreadable"
    status = str(metrics_payload.get("status", "")).strip() or "unknown"
    primary_metric = metrics_payload.get("primary_metric")
    if not isinstance(primary_metric, dict):
        return f"run_id={run_id}; status={status}; primary metric unavailable"
    name = str(primary_metric.get("name", "")).strip() or "unknown_metric"
    value = primary_metric.get("value")
    delta = primary_metric.get("delta_vs_baseline")
    return (
        f"run_id={run_id}; status={status}; "
        f"primary_metric={name}; value={value}; delta_vs_baseline={delta}"
    )


def _extract_hypothesis_target_delta(hypothesis_text: str) -> float | None:
    for line in hypothesis_text.splitlines():
        compact = line.strip()
        if not compact:
            continue
        lowered = compact.lower()
        if "target_delta" in lowered or lowered.startswith("target delta"):
            parsed = _parse_numeric_delta(compact)
            if parsed is not None:
                return parsed
    primary_metric_match = re.search(
        r"PrimaryMetric:\s*[^;]+;\s*Unit:\s*[^;]+;\s*Success:\s*(.+)",
        hypothesis_text,
        flags=re.IGNORECASE,
    )
    if primary_metric_match:
        return _parse_signed_delta(primary_metric_match.group(1))
    return None


def _extract_design_metric_mode(iteration_dir: Path) -> str:
    """Read metrics.primary.mode from design.yaml (default 'maximize')."""
    if yaml is None:
        return "maximize"
    design_path = iteration_dir / "design.yaml"
    if not design_path.exists():
        return "maximize"
    try:
        loaded = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    except Exception:
        return "maximize"
    if not isinstance(loaded, dict):
        return "maximize"
    metrics = loaded.get("metrics")
    if not isinstance(metrics, dict):
        return "maximize"
    primary = metrics.get("primary")
    if not isinstance(primary, dict):
        return "maximize"
    mode = str(primary.get("mode", "maximize")).strip().lower()
    if mode in ("maximize", "minimize"):
        return mode
    return "maximize"


def _load_design_yaml_mapping(iteration_dir: Path) -> dict[str, Any]:
    if yaml is None:
        return {}
    design_path = iteration_dir / "design.yaml"
    if not design_path.exists():
        return {}
    try:
        loaded = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _extract_design_target_delta(iteration_dir: Path) -> str:
    design_payload = _load_design_yaml_mapping(iteration_dir)
    if not isinstance(design_payload, dict):
        return ""
    metrics = design_payload.get("metrics")
    if not isinstance(metrics, dict):
        return ""
    return str(metrics.get("success_delta", "")).strip()


def _target_comparison_text(
    *,
    metrics_payload: dict[str, Any] | None,
    hypothesis_target_delta: float | None,
    design_target_delta: str,
    run_id: str,
    metric_mode: str = "maximize",
) -> tuple[str, str]:
    if not isinstance(metrics_payload, dict):
        return (
            f"unavailable: target comparison requires runs/{run_id}/metrics.json",
            "insufficient evidence in metrics/hypothesis; prefer human_review when risk is unclear",
        )
    primary_metric = metrics_payload.get("primary_metric")
    metric_delta = None
    if isinstance(primary_metric, dict):
        metric_delta = _coerce_float(primary_metric.get("delta_vs_baseline"))
        if metric_delta is None:
            metric_delta = _parse_numeric_delta(
                str(primary_metric.get("delta_vs_baseline", ""))
            )
    if metric_delta is None:
        return (
            "target comparison unavailable: primary_metric.delta_vs_baseline is missing/non-numeric",
            "results lack numeric delta evidence; choose design or human_review based on risk",
        )
    target_delta = hypothesis_target_delta
    target_source = "hypothesis.target_delta"
    if target_delta is None:
        target_delta = _parse_numeric_delta(design_target_delta)
        target_source = "design.metrics.success_delta"
    if target_delta is None:
        return (
            "target comparison unavailable: no numeric target_delta found in hypothesis/design",
            "targets are unspecified; avoid stop decisions without explicit human confirmation",
        )
    if metric_mode == "maximize" and target_delta <= 0:
        return (
            (
                "target comparison unavailable: invalid target_delta semantics "
                f"(metric_mode=maximize requires positive target_delta, got {target_delta:.4f})"
            ),
            "target semantics are inconsistent; prefer human_review instead of automated stop/design decision",
        )
    if metric_mode == "minimize" and target_delta >= 0:
        return (
            (
                "target comparison unavailable: invalid target_delta semantics "
                f"(metric_mode=minimize requires negative target_delta, got {target_delta:.4f})"
            ),
            "target semantics are inconsistent; prefer human_review instead of automated stop/design decision",
        )
    if metric_mode == "minimize":
        met_target = metric_delta <= target_delta
    else:
        met_target = metric_delta >= target_delta
    comparison = (
        f"run_id={run_id}; measured_delta={metric_delta:.4f}; "
        f"target_delta={target_delta:.4f} ({target_source}); "
        f"metric_mode={metric_mode}; met_target={str(met_target).lower()}"
    )
    suggestion = (
        "suggested decision: stop (target met)"
        if met_target
        else "suggested decision: design (target not met; iterate before escalation)"
    )
    return (comparison, suggestion)


def _suggest_decision_from_metrics(
    repo_root: Path,
    state: dict[str, Any],
) -> tuple[str | None, dict]:
    """Suggest a decide_repeat decision based on metrics vs target comparison.

    Returns (decision, evidence_record) where decision is 'stop', 'design', or None.
    """
    iteration_id = str(state.get("iteration_id", "")).strip()
    if not iteration_id:
        return (None, {})
    iteration_dir, _type = _resolve_iteration_directory(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=str(state.get("experiment_id", "")).strip(),
        require_exists=False,
    )
    run_id = str(state.get("last_run_id", "")).strip()
    if not run_id:
        return (None, {})
    metrics_payload = _load_metrics_payload(iteration_dir, run_id)
    if not isinstance(metrics_payload, dict):
        return (None, {})
    hypothesis_text = _safe_read_text(iteration_dir / "hypothesis.md", max_chars=12000)
    hypothesis_target_delta = _extract_hypothesis_target_delta(hypothesis_text)
    design_target_delta = _extract_design_target_delta(iteration_dir)
    metric_mode = _extract_design_metric_mode(iteration_dir)
    _comparison, suggestion = _target_comparison_text(
        metrics_payload=metrics_payload,
        hypothesis_target_delta=hypothesis_target_delta,
        design_target_delta=design_target_delta,
        run_id=run_id,
        metric_mode=metric_mode,
    )
    evidence = {
        "comparison": _comparison,
        "suggestion": suggestion,
        "hypothesis_target_delta": hypothesis_target_delta,
        "design_target_delta": design_target_delta,
        "metric_mode": metric_mode,
        "run_id": run_id,
    }
    if "stop" in suggestion:
        return ("stop", evidence)
    if "design" in suggestion:
        return ("design", evidence)
    return (None, evidence)


def _build_prompt_context(
    repo_root: Path,
    *,
    state: dict[str, Any],
    stage: str,
    runner_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    iteration_id = str(state.get("iteration_id", "")).strip()
    experiment_id = str(state.get("experiment_id", "")).strip()
    if not experiment_id:
        _append_log(
            repo_root,
            f"warning: experiment_id is empty for stage '{stage}'; prompt tokens referencing experiment_id will be blank",
        )
    policy = _load_verifier_policy(repo_root)
    python_bin = _resolve_policy_python_bin(policy)
    total_memory_gb = _detect_total_memory_gb()
    recommended_memory_estimate = _recommended_memory_estimate(total_memory_gb)
    available_memory_gb = (
        str(total_memory_gb) if total_memory_gb is not None else "unavailable"
    )
    paper_targets_raw = state.get("paper_targets")
    if isinstance(paper_targets_raw, list):
        paper_targets = ", ".join(
            str(item).strip() for item in paper_targets_raw if str(item).strip()
        )
    else:
        paper_targets = str(paper_targets_raw or "").strip()
    if not paper_targets:
        paper_targets = "unavailable: paper_targets not configured"
    run_id = _resolve_prompt_run_id(repo_root=repo_root, stage=stage, state=state)
    hypothesis_id = _resolve_hypothesis_id(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
    )
    host_mode = _detect_priority_host_mode()
    launch_mode = host_mode
    launch_execute = _load_launch_execute_policy(repo_root)

    iteration_dir = Path()
    iteration_path = ""
    if iteration_id:
        iteration_dir, _iteration_type = _resolve_iteration_directory(
            repo_root,
            iteration_id=iteration_id,
            experiment_id=experiment_id,
            require_exists=False,
        )
        try:
            iteration_path = iteration_dir.relative_to(repo_root).as_posix()
        except ValueError:
            iteration_path = f"experiments/{DEFAULT_EXPERIMENT_TYPE}/{iteration_id}"

    metrics_payload = None
    metrics_summary = "unavailable: metrics summary not available for this stage"
    target_comparison = "unavailable: target comparison not available for this stage"
    decision_suggestion = "insufficient evidence in metrics/hypothesis; prefer human_review when risk is unclear"
    if iteration_id and iteration_dir.exists() and run_id and run_id != "run_pending":
        metrics_payload = _load_metrics_payload(iteration_dir, run_id)
        metrics_summary = _metrics_summary_text(metrics_payload, run_id=run_id)
        hypothesis_text = _safe_read_text(
            iteration_dir / "hypothesis.md", max_chars=12000
        )
        hypothesis_target_delta = _extract_hypothesis_target_delta(hypothesis_text)
        design_target_delta = _extract_design_target_delta(iteration_dir)
        metric_mode = _extract_design_metric_mode(iteration_dir)
        target_comparison, decision_suggestion = _target_comparison_text(
            metrics_payload=metrics_payload,
            hypothesis_target_delta=hypothesis_target_delta,
            design_target_delta=design_target_delta,
            run_id=run_id,
            metric_mode=metric_mode,
        )

    auto_metrics_evidence_record: dict = {}
    if iteration_id and iteration_dir.exists() and run_id and run_id != "run_pending":
        try:
            _auto_decision, auto_metrics_evidence_record = (
                _suggest_decision_from_metrics(repo_root, state)
            )
        except Exception:
            pass

    todo_focus_payload = _load_json_if_exists(
        repo_root / ".autolab" / "todo_focus.json"
    )
    agent_result_payload = _load_json_if_exists(
        repo_root / ".autolab" / "agent_result.json"
    )
    review_result_payload = (
        _load_json_if_exists(iteration_dir / "review_result.json")
        if iteration_id
        else None
    )
    state_excerpt = {
        "stage": str(state.get("stage", "")).strip(),
        "stage_attempt": int(state.get("stage_attempt", 0) or 0),
        "max_stage_attempts": int(state.get("max_stage_attempts", 0) or 0),
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "last_run_id": str(state.get("last_run_id", "")).strip(),
        "sync_status": str(state.get("sync_status", "")).strip(),
        "assistant_mode": str(state.get("assistant_mode", "")).strip(),
        "task_cycle_stage": str(state.get("task_cycle_stage", "")).strip(),
        "current_task_id": str(state.get("current_task_id", "")).strip(),
    }

    review_feedback = (
        _safe_read_text(iteration_dir / "implementation_review.md")
        if iteration_id and iteration_dir.exists()
        else ""
    )
    if not review_feedback:
        review_feedback = (
            "unavailable: no implementation review feedback recorded for this iteration"
        )

    dry_run_output = (
        _extract_matching_lines(
            iteration_dir / "implementation_plan.md",
            keywords=("dry-run", "dry run"),
            limit=8,
        )
        if iteration_id and iteration_dir.exists()
        else ""
    )
    if not dry_run_output:
        dry_run_output = (
            "unavailable: no dry-run excerpt was found in implementation artifacts"
        )

    verifier_outputs_parts: list[str] = []
    if isinstance(review_result_payload, dict):
        required_checks = review_result_payload.get("required_checks")
        if isinstance(required_checks, dict):
            verifier_outputs_parts.append(
                f"review_result.required_checks={_compact_json(required_checks, max_chars=400)}"
            )
        status = str(review_result_payload.get("status", "")).strip()
        if status:
            verifier_outputs_parts.append(f"review_result.status={status}")
    template_fill_log = _extract_log_snippet(
        repo_root,
        keywords=(
            "template_fill:",
            "docs_targets:",
            "result_sanity:",
            "run_health:",
            "schema_checks:",
        ),
        limit=8,
    )
    if template_fill_log:
        verifier_outputs_parts.append(template_fill_log)
    verifier_outputs = "\n".join(verifier_outputs_parts).strip()
    if not verifier_outputs:
        verifier_outputs = (
            "unavailable: no verifier output snippets detected in recent artifacts/logs"
        )

    verifier_errors = _extract_log_snippet(
        repo_root,
        keywords=(
            "verification failed",
            "stagecheckerror",
            "run failure at",
            "template_fill: fail",
            "docs_targets: fail",
            "result_sanity: fail",
            "run_health: fail",
            "schema_checks: fail",
        ),
        limit=10,
    )
    if not verifier_errors:
        verifier_errors = "unavailable: no recent verifier error snippets found"

    git_summary, git_paths = _summarize_git_changes_for_prompt(repo_root, limit=12)
    diff_summary = f"{git_summary}\n" + (
        "\n".join(git_paths) if git_paths else "no changed paths"
    )

    if todo_focus_payload is None:
        todo_focus_payload = {
            "note": "unavailable: .autolab/todo_focus.json is missing or unreadable"
        }
    if agent_result_payload is None:
        agent_result_payload = {
            "note": "unavailable: .autolab/agent_result.json is missing or unreadable"
        }

    task_context_text = ""
    if str(state.get("assistant_mode", "")).strip().lower() == "on":
        current_task_id = str(state.get("current_task_id", "")).strip()
        if current_task_id:
            try:
                todo_path = repo_root / ".autolab" / "todo_state.json"
                if todo_path.exists():
                    todo_state = json.loads(todo_path.read_text(encoding="utf-8"))
                    tasks = todo_state.get("tasks", {})
                    if isinstance(tasks, dict):
                        task = tasks.get(current_task_id)
                        if isinstance(task, dict):
                            parts = [f"task_id: {current_task_id}"]
                            for field in (
                                "title",
                                "description",
                                "acceptance_criteria",
                                "text",
                                "stage",
                                "task_class",
                            ):
                                val = str(task.get(field, "")).strip()
                                if val:
                                    parts.append(f"{field}: {val}")
                            task_context_text = "\n".join(parts)
            except Exception:
                pass

    run_group = state.get("run_group", [])
    if not isinstance(run_group, list):
        run_group = []
    replicate_count = len(run_group) if run_group else 1

    scope_payload = runner_scope if isinstance(runner_scope, dict) else {}
    media_discovery = discover_media_inputs(
        repo_root,
        iteration_dir=iteration_dir if iteration_id else None,
    )
    context_resolution = resolve_context_sidecars(
        repo_root,
        iteration_id=iteration_id,
        experiment_id=experiment_id,
        scope_kind=str(scope_payload.get("scope_kind", "")).strip(),
    )
    design_payload = (
        _load_design_yaml_mapping(iteration_dir)
        if iteration_id and iteration_dir.exists()
        else {}
    )
    sidecar_guidance = build_context_guidance(
        context_resolution,
        stage=stage,
        design_payload=design_payload,
    )
    project_data_roots = [str(path) for path in media_discovery.project_roots]
    project_data_media_counts = {
        str(path): int(count)
        for path, count in media_discovery.project_root_counts.items()
    }
    stage_attempt = int(state.get("stage_attempt", 0) or 0)
    max_stage_attempts = int(state.get("max_stage_attempts", 0) or 0)
    remaining_attempts = max(0, max_stage_attempts - stage_attempt)
    registry = load_registry(repo_root)
    stage_spec = registry.get(stage) if registry else None
    stage_metadata: dict[str, Any] = {}
    if isinstance(stage_spec, StageSpec):
        stage_metadata = {
            "runner_prompt_file": stage_spec.runner_prompt_file,
            "audit_prompt_file": stage_spec.prompt_file,
            "brief_prompt_file": stage_spec.brief_prompt_file,
            "human_prompt_file": stage_spec.human_prompt_file,
            "required_outputs": list(stage_spec.required_outputs),
            "required_outputs_any_of": [
                list(group) for group in stage_spec.required_outputs_any_of
            ],
            "required_outputs_if": [
                {
                    "conditions": {key: value for key, value in conditions},
                    "outputs": list(outputs),
                }
                for conditions, outputs in stage_spec.required_outputs_if
            ],
            "required_tokens": sorted(stage_spec.required_tokens),
            "optional_tokens": sorted(stage_spec.optional_tokens),
            "verifier_categories": dict(stage_spec.verifier_categories),
        }
    protected_files = _load_protected_files(policy, auto_mode=False)
    runner_config = _load_agent_runner_config(repo_root)
    agent_surface = resolve_agent_surface(
        repo_root,
        provider=runner_config.runner,
        stage=stage,
        assistant_cycle_stage=str(state.get("task_cycle_stage", "")).strip()
        if str(state.get("assistant_mode", "")).strip().lower() == "on"
        else "",
    )
    agent_surface_guidance = build_agent_surface_guidance(agent_surface)

    retry_counters = {
        "stage_attempt": stage_attempt,
        "max_stage_attempts": max_stage_attempts,
        "remaining_attempts": remaining_attempts,
    }
    launch_execute_text = "true" if launch_execute else "false"

    return {
        "context_schema_version": "2.0",
        "generated_at": _utc_now(),
        "stage": stage,
        "host_mode": host_mode,
        "launch_mode": launch_mode,
        "launch_execute": launch_execute_text,
        "iteration_id": iteration_id,
        "iteration_path": iteration_path,
        "experiment_id": experiment_id,
        "paper_targets": paper_targets,
        "python_bin": python_bin,
        "recommended_memory_estimate": recommended_memory_estimate,
        "available_memory_gb": available_memory_gb,
        "run_id": run_id,
        "hypothesis_id": hypothesis_id,
        "state_snapshot": state_excerpt,
        "todo_focus": todo_focus_payload,
        "agent_result": agent_result_payload,
        "review_feedback": review_feedback,
        "verifier_errors": verifier_errors,
        "verifier_outputs": verifier_outputs,
        "dry_run_output": dry_run_output,
        "metrics_summary": metrics_summary,
        "target_comparison": target_comparison,
        "decision_suggestion": decision_suggestion,
        "auto_metrics_evidence": auto_metrics_evidence_record,
        "diff_summary": diff_summary,
        "git_changed_paths": git_paths,
        "runner_scope": scope_payload,
        "task_context": task_context_text,
        "run_group": run_group,
        "replicate_count": replicate_count,
        "project_data_roots": project_data_roots,
        "project_data_media_counts": project_data_media_counts,
        "project_data_media_count_summary": summarize_root_counts(
            media_discovery.project_root_counts
        ),
        "codebase_project_map_path": context_resolution.get(
            "codebase_project_map_path", ""
        ),
        "codebase_project_map_summary": context_resolution.get(
            "codebase_project_map_summary", ""
        ),
        "codebase_experiment_delta_map_path": context_resolution.get(
            "codebase_experiment_delta_map_path", ""
        ),
        "codebase_experiment_delta_summary": context_resolution.get(
            "codebase_experiment_delta_summary", ""
        ),
        "context_resolution": context_resolution,
        "discuss_summary": {
            "stage_context_lines": list(sidecar_guidance.get("stage_context_lines", []))
            if isinstance(sidecar_guidance.get("stage_context_lines"), list)
            else [],
        },
        "research_summary": {
            "brief_items": list(sidecar_guidance.get("brief_items", []))
            if isinstance(sidecar_guidance.get("brief_items"), list)
            else [],
        },
        "sidecar_guidance": sidecar_guidance,
        "agent_surface": agent_surface,
        "agent_surface_guidance": agent_surface_guidance,
        "retry_counters": retry_counters,
        "protected_files": protected_files,
        "stage_metadata": stage_metadata,
        "runtime": {
            "stage": stage,
            "host_mode": host_mode,
            "launch_mode": launch_mode,
            "launch_execute": launch_execute_text,
            "state_snapshot": state_excerpt,
            "retry_counters": retry_counters,
        },
        "scope": {
            "runner_scope": scope_payload,
            "protected_files": protected_files,
        },
        "artifacts": {
            "iteration_id": iteration_id,
            "iteration_path": iteration_path,
            "run_id": run_id,
            "hypothesis_id": hypothesis_id,
            "codebase_project_map_path": context_resolution.get(
                "codebase_project_map_path", ""
            ),
            "codebase_project_map_summary": context_resolution.get(
                "codebase_project_map_summary", ""
            ),
            "codebase_experiment_delta_map_path": context_resolution.get(
                "codebase_experiment_delta_map_path", ""
            ),
            "codebase_experiment_delta_summary": context_resolution.get(
                "codebase_experiment_delta_summary", ""
            ),
            "context_resolution": context_resolution,
            "agent_surface": agent_surface,
        },
        "verification": {
            "verifier_outputs": verifier_outputs,
            "verifier_errors": verifier_errors,
            "review_feedback": review_feedback,
            "dry_run_output": dry_run_output,
        },
        "metrics": {
            "metrics_summary": metrics_summary,
            "target_comparison": target_comparison,
            "decision_suggestion": decision_suggestion,
            "auto_metrics_evidence": auto_metrics_evidence_record,
        },
        "handoff": {
            "todo_focus": todo_focus_payload,
            "agent_result": agent_result_payload,
            "task_context": task_context_text,
            "diff_summary": diff_summary,
        },
        "policy": {
            "python_bin": python_bin,
            "paper_targets": paper_targets,
            "recommended_memory_estimate": recommended_memory_estimate,
            "available_memory_gb": available_memory_gb,
        },
        "stage_contract": stage_metadata,
    }


def _sanitize_context_resolution_for_runtime(
    context_resolution: Any,
) -> dict[str, Any]:
    if not isinstance(context_resolution, dict):
        return {}
    return {
        key: value
        for key, value in context_resolution.items()
        if key not in {"effective_discuss", "effective_research"}
    }


def _build_runtime_context_payload(
    context_payload: dict[str, Any],
) -> dict[str, Any]:
    runtime_payload = dict(context_payload)
    sanitized_context_resolution = _sanitize_context_resolution_for_runtime(
        runtime_payload.get("context_resolution")
    )
    runtime_payload["context_resolution"] = sanitized_context_resolution
    artifacts = runtime_payload.get("artifacts")
    if isinstance(artifacts, dict):
        runtime_payload["artifacts"] = {
            **artifacts,
            "context_resolution": sanitized_context_resolution,
        }
    return runtime_payload


def _sanitize_retry_blocker(text: str, *, max_chars: int = 240) -> str:
    compact = " ".join(str(text).split())
    if not compact:
        return ""
    if len(compact) <= max_chars:
        return compact
    return compact[: max_chars - 3].rstrip() + "..."


def _collect_stage_brief_items(
    repo_root: Path, *, context_payload: dict[str, Any], stage: str
) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()

    agent_surface_guidance = context_payload.get("agent_surface_guidance")
    if isinstance(agent_surface_guidance, dict):
        agent_items = agent_surface_guidance.get("brief_items")
        if isinstance(agent_items, list):
            for item in agent_items:
                candidate = _sanitize_retry_blocker(str(item))
                if candidate:
                    items.append(candidate)

    sidecar_guidance = context_payload.get("sidecar_guidance")
    if isinstance(sidecar_guidance, dict):
        sidecar_items = sidecar_guidance.get("brief_items")
        if isinstance(sidecar_items, list):
            for item in sidecar_items:
                candidate = _sanitize_retry_blocker(str(item))
                if candidate:
                    items.append(candidate)

    iteration_id = str(context_payload.get("iteration_id", "")).strip()
    experiment_id = str(context_payload.get("experiment_id", "")).strip()
    iteration_dir = Path()
    if iteration_id:
        iteration_dir, _iteration_type = _resolve_iteration_directory(
            repo_root,
            iteration_id=iteration_id,
            experiment_id=experiment_id,
            require_exists=False,
        )

    review_payload = (
        _load_json_if_exists(iteration_dir / "review_result.json")
        if iteration_id and iteration_dir.exists()
        else None
    )
    if isinstance(review_payload, dict):
        review_status = str(review_payload.get("status", "")).strip()
        if review_status in {"needs_retry", "failed"}:
            items.append(f"review_result status is '{review_status}'")
        findings = review_payload.get("blocking_findings")
        if isinstance(findings, list):
            for finding in findings:
                normalized = _sanitize_retry_blocker(str(finding))
                if normalized:
                    items.append(normalized)

    verification_payload = _load_json_if_exists(
        repo_root / ".autolab" / "verification_result.json"
    )
    if isinstance(verification_payload, dict):
        if not bool(verification_payload.get("passed", False)):
            verification_message = _sanitize_retry_blocker(
                str(verification_payload.get("message", ""))
            )
            if verification_message:
                items.append(verification_message)
        details = verification_payload.get("details")
        commands = details.get("commands") if isinstance(details, dict) else None
        if isinstance(commands, list):
            for command in commands:
                if not isinstance(command, dict):
                    continue
                status = str(command.get("status", "")).strip().lower()
                if status in {"pass", "ok", "skip"}:
                    continue
                name = str(command.get("name", "")).strip() or "verifier"
                detail = _sanitize_retry_blocker(
                    str(
                        command.get("detail")
                        or command.get("stderr")
                        or command.get("stdout")
                        or ""
                    )
                )
                if detail:
                    items.append(f"{name}: {detail}")

    for fallback_source in (
        context_payload.get("verifier_errors"),
        context_payload.get("review_feedback"),
    ):
        if not isinstance(fallback_source, str):
            continue
        for raw_line in fallback_source.splitlines():
            candidate = _sanitize_retry_blocker(raw_line)
            if not candidate:
                continue
            lowered = candidate.lower()
            if lowered.startswith("unavailable:"):
                continue
            if len(candidate) < 24:
                continue
            items.append(candidate)
            if len(items) >= 12:
                break
        if len(items) >= 12:
            break

    deduped: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(normalized)
    return deduped


def _build_stage_brief_summary(
    repo_root: Path, *, context_payload: dict[str, Any], stage: str
) -> str:
    brief_items = _collect_stage_brief_items(
        repo_root, context_payload=context_payload, stage=stage
    )
    selected = brief_items[:7]
    while len(selected) < 3:
        if len(selected) == 0:
            selected.append(
                f"No prior blocking findings were recorded for stage '{stage}'."
            )
        elif len(selected) == 1:
            selected.append(
                "No additional failing verifier commands were found in `.autolab/verification_result.json`."
            )
        else:
            selected.append(
                f"After edits, run `autolab verify --stage {stage}` and record concrete evidence in required stage artifacts."
            )

    lines = []
    lines.extend(f"- {entry}" for entry in selected)
    return "\n".join(lines)


def _context_token_values(context: dict[str, Any]) -> dict[str, str]:
    def _to_text(value: Any, fallback_label: str) -> str:
        if isinstance(value, str):
            text = value.strip()
            return text if text else f"unavailable: {fallback_label}"
        if value is None:
            return f"unavailable: {fallback_label}"
        compact = _compact_json(value, max_chars=2000)
        return compact if compact else f"unavailable: {fallback_label}"

    return {
        "iteration_id": _to_text(context.get("iteration_id"), "iteration_id"),
        "iteration_path": _to_text(context.get("iteration_path"), "iteration_path"),
        "experiment_id": context.get("experiment_id", "").strip()
        if isinstance(context.get("experiment_id"), str)
        else "",
        "paper_targets": _to_text(context.get("paper_targets"), "paper_targets"),
        "python_bin": _to_text(context.get("python_bin"), "python_bin"),
        "recommended_memory_estimate": _to_text(
            context.get("recommended_memory_estimate"), "recommended_memory_estimate"
        ),
        "available_memory_gb": _to_text(
            context.get("available_memory_gb"), "available_memory_gb"
        ),
        "stage": _to_text(context.get("stage"), "stage"),
        "stage_context": _to_text(context.get("stage_context"), "stage_context"),
        "run_id": _to_text(context.get("run_id"), "run_id"),
        "hypothesis_id": _to_text(context.get("hypothesis_id"), "hypothesis_id"),
        "review_feedback": _to_text(context.get("review_feedback"), "review_feedback"),
        "verifier_errors": _to_text(context.get("verifier_errors"), "verifier_errors"),
        "diff_summary": _to_text(context.get("diff_summary"), "diff_summary"),
        "verifier_outputs": _to_text(
            context.get("verifier_outputs"), "verifier_outputs"
        ),
        "dry_run_output": _to_text(context.get("dry_run_output"), "dry_run_output"),
        "metrics_summary": _to_text(context.get("metrics_summary"), "metrics_summary"),
        "target_comparison": _to_text(
            context.get("target_comparison"), "target_comparison"
        ),
        "decision_suggestion": _to_text(
            context.get("decision_suggestion"), "decision_suggestion"
        ),
        "auto_metrics_evidence": _to_text(
            context.get("auto_metrics_evidence"), "auto_metrics_evidence"
        ),
        "launch_mode": _to_text(context.get("launch_mode"), "launch_mode"),
        "launch_execute": _to_text(context.get("launch_execute"), "launch_execute"),
        "task_context": context.get("task_context", ""),
        "run_group": _to_text(context.get("run_group"), "run_group"),
        "replicate_count": str(context.get("replicate_count", 1)),
        "project_data_roots": _to_text(
            context.get("project_data_roots"), "project_data_roots"
        ),
        "project_data_media_counts": _to_text(
            context.get("project_data_media_counts"), "project_data_media_counts"
        ),
        "brief_summary": _to_text(context.get("brief_summary"), "brief_summary"),
    }


def _format_todo_focus_summary(todo_focus_payload: Any) -> str:
    if not isinstance(todo_focus_payload, dict):
        return "none"
    task_id = str(todo_focus_payload.get("task_id", "")).strip()
    title = str(todo_focus_payload.get("title", "")).strip()
    stage = str(todo_focus_payload.get("stage", "")).strip()
    if not any((task_id, title, stage)):
        return "none"
    parts = []
    if task_id:
        parts.append(f"task_id={task_id}")
    if stage:
        parts.append(f"stage={stage}")
    if title:
        parts.append(f"title={title}")
    return ", ".join(parts)


def _build_runtime_stage_context_block(context_payload: dict[str, Any]) -> str:
    state_snapshot = context_payload.get("state_snapshot")
    if not isinstance(state_snapshot, dict):
        state_snapshot = {}

    stage = str(context_payload.get("stage", "")).strip() or "unknown"
    iteration_id = str(context_payload.get("iteration_id", "")).strip() or "unknown"
    iteration_path = str(context_payload.get("iteration_path", "")).strip() or "unknown"
    host_mode = str(context_payload.get("host_mode", "")).strip() or "unknown"
    stage_attempt = str(state_snapshot.get("stage_attempt", "")).strip() or "-"
    max_stage_attempts = (
        str(state_snapshot.get("max_stage_attempts", "")).strip() or "-"
    )
    retry_counters = context_payload.get("retry_counters")
    if not isinstance(retry_counters, dict):
        retry_counters = {}
    remaining_attempts = str(retry_counters.get("remaining_attempts", "")).strip()
    if not remaining_attempts:
        remaining_attempts = "-"
    assistant_mode = str(state_snapshot.get("assistant_mode", "")).strip() or "off"
    current_task_id = str(state_snapshot.get("current_task_id", "")).strip() or "none"
    last_run_id = str(state_snapshot.get("last_run_id", "")).strip() or "none"
    sync_status = str(state_snapshot.get("sync_status", "")).strip() or "unknown"
    todo_focus_summary = _format_todo_focus_summary(context_payload.get("todo_focus"))
    runner_scope = context_payload.get("runner_scope")
    if not isinstance(runner_scope, dict):
        runner_scope = {}
    scope_mode = str(runner_scope.get("mode", "")).strip() or "unknown"
    scope_kind = str(runner_scope.get("scope_kind", "")).strip() or "unknown"
    scope_root = str(runner_scope.get("scope_root", "")).strip() or "unknown"
    project_wide_root = (
        str(runner_scope.get("project_wide_root", "")).strip() or "unknown"
    )
    scope_workspace = str(runner_scope.get("workspace_dir", "")).strip() or "unknown"
    allowed_dirs = runner_scope.get("allowed_edit_dirs")
    if isinstance(allowed_dirs, list):
        allowed_dirs_text = ", ".join(
            str(item).strip() for item in allowed_dirs if str(item).strip()
        )
    else:
        allowed_dirs_text = ""
    if not allowed_dirs_text:
        allowed_dirs_text = "(scope root only)"
    disallowed_text = (
        "all paths outside the scope root and allowed dirs"
        if allowed_dirs_text != "(scope root only)"
        else "all paths outside the scope root"
    )
    project_data_roots_raw = context_payload.get("project_data_roots")
    project_data_roots = (
        [str(item).strip() for item in project_data_roots_raw if str(item).strip()]
        if isinstance(project_data_roots_raw, list)
        else []
    )
    project_data_roots_text = (
        ", ".join(project_data_roots) if project_data_roots else "none"
    )
    project_data_counts_raw = context_payload.get("project_data_media_counts")
    project_data_counts_text = "none"
    if isinstance(project_data_counts_raw, dict):
        count_parts: list[str] = []
        for root, count in project_data_counts_raw.items():
            count_parts.append(f"{root}={count}")
        if count_parts:
            project_data_counts_text = ", ".join(count_parts)
    protected_files_raw = context_payload.get("protected_files")
    protected_files = (
        [str(item).strip() for item in protected_files_raw if str(item).strip()]
        if isinstance(protected_files_raw, list)
        else []
    )
    protected_files_text = ", ".join(protected_files) if protected_files else "none"
    codebase_project_map_path = (
        str(context_payload.get("codebase_project_map_path", "")).strip() or "none"
    )
    codebase_project_map_summary = (
        str(context_payload.get("codebase_project_map_summary", "")).strip() or "none"
    )
    codebase_experiment_delta_map_path = (
        str(context_payload.get("codebase_experiment_delta_map_path", "")).strip()
        or "none"
    )
    codebase_experiment_delta_summary = (
        str(context_payload.get("codebase_experiment_delta_summary", "")).strip()
        or "none"
    )
    sidecar_guidance = context_payload.get("sidecar_guidance")
    extra_context_lines: list[str] = []
    if isinstance(sidecar_guidance, dict):
        raw_lines = sidecar_guidance.get("stage_context_lines")
        if isinstance(raw_lines, list):
            extra_context_lines.extend(
                str(item).strip() for item in raw_lines if str(item).strip()
            )
    agent_surface_guidance = context_payload.get("agent_surface_guidance")
    if isinstance(agent_surface_guidance, dict):
        raw_lines = agent_surface_guidance.get("stage_context_lines")
        if isinstance(raw_lines, list):
            extra_context_lines.extend(
                str(item).strip() for item in raw_lines if str(item).strip()
            )
    extra_context_block = ""
    if extra_context_lines:
        extra_context_block = "".join(f"{line}\n" for line in extra_context_lines)

    return (
        "## Runtime Stage Context\n"
        f"- stage: {stage}\n"
        f"- iteration_id: {iteration_id}\n"
        f"- iteration_path: {iteration_path}\n"
        f"- detected_host_mode: {host_mode}\n"
        f"- stage_attempt: {stage_attempt}/{max_stage_attempts}\n"
        f"- remaining_stage_attempts: {remaining_attempts}\n"
        f"- assistant_mode: {assistant_mode}\n"
        f"- current_task_id: {current_task_id}\n"
        f"- last_run_id: {last_run_id}\n"
        f"- sync_status: {sync_status}\n"
        f"- todo_focus: {todo_focus_summary}\n"
        f"- edit_scope_mode: {scope_mode}\n"
        f"- scope_kind: {scope_kind}\n"
        f"- scope_root: {scope_root}\n"
        f"- project_wide_root: {project_wide_root}\n"
        f"- workspace_dir: {scope_workspace}\n"
        f"- allowed_edit_dirs: {allowed_dirs_text}\n"
        f"- disallowed_edit_dirs: {disallowed_text}\n"
        f"- project_data_roots: {project_data_roots_text}\n"
        f"- project_data_media_counts: {project_data_counts_text}\n"
        f"- codebase_project_map_path: {codebase_project_map_path}\n"
        f"- codebase_project_map_summary: {codebase_project_map_summary}\n"
        f"- codebase_experiment_delta_map_path: {codebase_experiment_delta_map_path}\n"
        f"- codebase_experiment_delta_summary: {codebase_experiment_delta_summary}\n"
        f"{extra_context_block}"
        f"- protected_files: {protected_files_text}\n"
    )


def _inject_registry_boilerplate(
    text: str,
    stage: str,
    registry: dict[str, StageSpec],
) -> str:
    """Auto-inject missing boilerplate sections from the workflow registry.

    Checks whether each boilerplate section heading is already present in the
    expanded template text and only injects missing sections.  Manual sections
    in the prompt always win (override semantics).
    """
    spec = registry.get(stage)
    if spec is None:
        return text

    def _has_heading_prefix(markdown: str, heading_prefix: str) -> bool:
        pattern = re.compile(
            rf"^\s*##\s*{re.escape(heading_prefix)}\b",
            flags=re.IGNORECASE | re.MULTILINE,
        )
        return bool(pattern.search(markdown))

    def _iter_output_contract_lines() -> list[str]:
        lines: list[str] = []
        for output in spec.required_outputs:
            lines.append(f"- {{{{iteration_path}}}}/{output}")
        for group in spec.required_outputs_any_of:
            group_text = " OR ".join(
                f"`{{{{iteration_path}}}}/{item}`" for item in group
            )
            lines.append(f"- one of: {group_text}")
        for conditions, outputs in spec.required_outputs_if:
            condition_text = ", ".join(f"{key}={value}" for key, value in conditions)
            outputs_text = ", ".join(f"`{item}`" for item in outputs)
            lines.append(f"- when {condition_text}: {outputs_text}")
        return lines

    sections: list[str] = []

    # 1. ## OUTPUTS (STRICT)
    output_contract_lines = _iter_output_contract_lines()
    if not _has_heading_prefix(text, "outputs") and output_contract_lines:
        lines = ["## OUTPUTS (STRICT)"]
        lines.extend(output_contract_lines)
        sections.append("\n".join(lines))

    # 2. ## REQUIRED INPUTS
    if not _has_heading_prefix(text, "required inputs") and spec.required_tokens:
        lines = ["## REQUIRED INPUTS"]
        for token in sorted(spec.required_tokens):
            lines.append(f"- `{{{{{token}}}}}`")
        sections.append("\n".join(lines))

    # 3. ## FILE CHECKLIST
    if not _has_heading_prefix(text, "file checklist") and output_contract_lines:
        lines = ["## FILE CHECKLIST"]
        for output in spec.required_outputs:
            filename = Path(output).name
            lines.append(f"- [ ] `{filename}` exists and is valid")
        for index, group in enumerate(spec.required_outputs_any_of, start=1):
            group_text = " or ".join(f"`{Path(item).name}`" for item in group)
            lines.append(f"- [ ] one-of output group {index} exists: {group_text}")
        for conditions, outputs in spec.required_outputs_if:
            condition_text = ", ".join(f"{key}={value}" for key, value in conditions)
            for output in outputs:
                lines.append(
                    f"- [ ] `{Path(output).name}` exists when {condition_text}"
                )
        sections.append("\n".join(lines))

    # 4. ## VERIFIER MAPPING
    if not _has_heading_prefix(text, "verifier mapping") and spec.verifier_categories:
        enabled = [cat for cat, active in spec.verifier_categories.items() if active]
        if enabled:
            lines = ["## VERIFIER MAPPING"]
            for cat in enabled:
                lines.append(f"- `{cat}`")
            sections.append("\n".join(lines))

    if not sections:
        return text

    injected_block = "\n\n".join(sections)

    # Insert before ## STEPS if present, otherwise append
    steps_match = re.search(
        r"^\s*##\s*steps\b", text, flags=re.IGNORECASE | re.MULTILINE
    )
    if steps_match is not None:
        steps_idx = steps_match.start()
        before = text[:steps_idx].rstrip()
        after = text[steps_idx:].lstrip("\n")
        return f"{before}\n\n{injected_block}\n\n{after}"
    return f"{text.rstrip()}\n\n{injected_block}\n"


def _render_stage_prompt(
    repo_root: Path,
    *,
    stage: str,
    state: dict[str, Any],
    template_path: Path,
    runner_scope: dict[str, Any] | None = None,
    write_outputs: bool = True,
) -> RenderedPromptBundle:
    def _runner_sentinel_violations(rendered_text: str) -> list[str]:
        violations: list[str] = []
        if re.search(r"(?i)\bunavailable:\s*", rendered_text):
            violations.append("unavailable:")
        if re.search(r"(?im)(?:[:=]\s*)(unknown|none)\s*$", rendered_text):
            violations.append("value sentinel unknown/none")
        if re.search(r"(?im)^\s*[-*]\s*(unknown|none)\s*$", rendered_text):
            violations.append("bullet sentinel unknown/none")
        return violations

    def _render_template_text(
        *,
        source_path: Path,
        source_text: str,
        audience: str,
        inject_registry_boilerplate: bool,
        required_tokens: set[str],
        token_values: dict[str, str],
        context_payload: dict[str, Any],
    ) -> str:
        template_text = source_text
        if inject_registry_boilerplate:
            template_text = _inject_registry_boilerplate(template_text, stage, registry)

        tokens_in_template = sorted(
            {
                match.group(1).strip()
                for match in PROMPT_TOKEN_PATTERN.finditer(template_text)
            }
        )
        unsupported_tokens = sorted(
            token for token in tokens_in_template if token not in token_values
        )
        if unsupported_tokens:
            _append_log(
                repo_root,
                (
                    "prompt render unsupported tokens "
                    f"stage={stage} template={source_path} tokens={unsupported_tokens}"
                ),
            )
            raise StageCheckError(
                f"prompt template has unsupported token(s) for stage '{stage}': {', '.join(unsupported_tokens)}"
            )

        required_values = {
            token: str(context_payload.get(token, "")).strip()
            for token in required_tokens
        }
        missing_required = sorted(
            token
            for token in tokens_in_template
            if token in required_tokens and not required_values.get(token, "")
        )
        if missing_required:
            _append_log(
                repo_root,
                (
                    "prompt render missing required tokens "
                    f"stage={stage} template={source_path} tokens={missing_required}"
                ),
            )
            raise StageCheckError(
                f"prompt template missing required value(s) for stage '{stage}': {', '.join(missing_required)}"
            )

        def _replace_token(match: re.Match[str]) -> str:
            token = match.group(1).strip()
            value = token_values.get(token, "")
            text = str(value).strip()
            if text:
                return text
            if audience == "runner":
                return ""
            return f"unavailable: {token}"

        rendered_text = PROMPT_TOKEN_PATTERN.sub(_replace_token, template_text)

        unresolved_tokens = sorted(
            {
                match.group(1).strip()
                for match in PROMPT_TOKEN_PATTERN.finditer(rendered_text)
            }
        )
        unresolved_literals = [
            literal for literal in PROMPT_LITERAL_TOKENS if literal in rendered_text
        ]
        if unresolved_tokens or unresolved_literals:
            _append_log(
                repo_root,
                (
                    "prompt render unresolved placeholders "
                    f"stage={stage} template={source_path} tokens={unresolved_tokens} "
                    f"literals={unresolved_literals}"
                ),
            )
            unresolved_text = (
                ", ".join([*unresolved_tokens, *unresolved_literals]) or "<unknown>"
            )
            raise StageCheckError(
                f"rendered prompt contains unresolved placeholders for stage '{stage}': {unresolved_text}"
            )

        return rendered_text

    registry = load_registry(repo_root)

    try:
        runner_template_text = template_path.read_text(encoding="utf-8")
        runner_template_text = _render_prompt_includes(
            repo_root, runner_template_text, stage=stage
        )
    except Exception as exc:
        raise StageCheckError(
            f"agent runner prompt could not be read at {template_path}: {exc}"
        ) from exc

    context_payload = _build_prompt_context(
        repo_root,
        state=state,
        stage=stage,
        runner_scope=runner_scope,
    )
    context_payload["stage_context"] = _build_runtime_stage_context_block(
        context_payload
    )
    context_payload["brief_summary"] = _build_stage_brief_summary(
        repo_root, context_payload=context_payload, stage=stage
    )
    token_values = _context_token_values(context_payload)
    reg_required = registry_required_tokens(registry) if registry else {}
    required_tokens = reg_required.get(stage) or PROMPT_REQUIRED_TOKENS_BY_STAGE.get(
        stage, {"iteration_id"}
    )

    runner_text = _render_template_text(
        source_path=template_path,
        source_text=runner_template_text,
        audience="runner",
        inject_registry_boilerplate=False,
        required_tokens=required_tokens,
        token_values=token_values,
        context_payload=context_payload,
    )
    runner_sentinel_violations = _runner_sentinel_violations(runner_text)
    if runner_sentinel_violations:
        raise StageCheckError(
            (
                f"rendered runner prompt contains disallowed sentinel marker(s) "
                f"for stage '{stage}' at {template_path}: "
                f"{', '.join(sorted(set(runner_sentinel_violations)))}"
            )
        )

    def _load_sidecar_template(prompt_role: str) -> tuple[Path, str]:
        sidecar_template_path = _resolve_stage_prompt_path(
            repo_root, stage, prompt_role=prompt_role
        )
        try:
            sidecar_template_text = sidecar_template_path.read_text(encoding="utf-8")
            sidecar_template_text = _render_prompt_includes(
                repo_root, sidecar_template_text, stage=stage
            )
            return sidecar_template_path, sidecar_template_text
        except StageCheckError:
            raise
        except Exception as exc:
            raise StageCheckError(
                f"stage {prompt_role} prompt could not be read at {sidecar_template_path}: {exc}"
            ) from exc

    audit_template_path, audit_template_text = _load_sidecar_template("audit")
    brief_template_path, brief_template_text = _load_sidecar_template("brief")
    human_template_path, human_template_text = _load_sidecar_template("human")

    audit_text = _render_template_text(
        source_path=audit_template_path,
        source_text=audit_template_text,
        audience="audit",
        inject_registry_boilerplate=True,
        required_tokens=required_tokens,
        token_values=token_values,
        context_payload=context_payload,
    )
    brief_text = _render_template_text(
        source_path=brief_template_path,
        source_text=brief_template_text,
        audience="brief",
        inject_registry_boilerplate=False,
        required_tokens=required_tokens,
        token_values=token_values,
        context_payload=context_payload,
    )
    human_text = _render_template_text(
        source_path=human_template_path,
        source_text=human_template_text,
        audience="human",
        inject_registry_boilerplate=False,
        required_tokens=required_tokens,
        token_values=token_values,
        context_payload=context_payload,
    )

    rendered_dir = repo_root / ".autolab" / "prompts" / "rendered"
    rendered_path = rendered_dir / f"{stage}.runner.md"
    context_path = rendered_dir / f"{stage}.context.json"
    audit_path = rendered_dir / f"{stage}.audit.md"
    brief_path = rendered_dir / f"{stage}.brief.md"
    human_path = rendered_dir / f"{stage}.human.md"

    if write_outputs:
        rendered_dir.mkdir(parents=True, exist_ok=True)
        rendered_path.write_text(runner_text, encoding="utf-8")
        audit_path.write_text(audit_text, encoding="utf-8")
        brief_path.write_text(brief_text, encoding="utf-8")
        human_path.write_text(human_text, encoding="utf-8")

    context_payload = {
        **context_payload,
        "template_path": str(template_path),
        "rendered_prompt_path": str(rendered_path),
        "rendered_context_path": str(context_path),
        "rendered_packets": {
            "runner": str(rendered_path),
            "audit": str(audit_path),
            "brief": str(brief_path),
            "human": str(human_path),
            "context": str(context_path),
        },
        "audit_template_path": str(audit_template_path),
        "brief_template_path": str(brief_template_path),
        "human_template_path": str(human_template_path),
        "rendered_audit_path": str(audit_path),
        "rendered_brief_path": str(brief_path),
        "rendered_human_path": str(human_path),
    }
    if write_outputs:
        _write_json(context_path, _build_runtime_context_payload(context_payload))

    return RenderedPromptBundle(
        template_path=template_path,
        rendered_path=rendered_path,
        context_path=context_path,
        prompt_text=runner_text,
        context_payload=context_payload,
        audit_template_path=audit_template_path,
        audit_path=audit_path,
        brief_template_path=brief_template_path,
        brief_path=brief_path,
        human_template_path=human_template_path,
        human_path=human_path,
        audit_text=audit_text,
        brief_text=brief_text,
        human_text=human_text,
    )
