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
    STAGE_PROMPT_FILES,
)
from autolab.config import _load_verifier_policy, _resolve_policy_python_bin
from autolab.models import RenderedPromptBundle, StageCheckError
from autolab.registry import StageSpec, load_registry, registry_prompt_files, registry_required_tokens
from autolab.state import _resolve_iteration_directory
from autolab.utils import (
    _append_log,
    _compact_json,
    _compact_log_text,
    _detect_priority_host_mode,
    _extract_log_snippet,
    _extract_matching_lines,
    _load_json_if_exists,
    _safe_read_text,
    _summarize_git_changes_for_prompt,
    _utc_now,
    _write_json,
)


def _resolve_stage_prompt_path(repo_root: Path, stage: str) -> Path:
    registry = load_registry(repo_root)
    reg_prompt_files = registry_prompt_files(registry) if registry else {}
    prompt_name = reg_prompt_files.get(stage) or STAGE_PROMPT_FILES.get(stage)
    if prompt_name is None:
        raise StageCheckError(f"no stage prompt mapping is defined for '{stage}'")
    candidate = repo_root / ".autolab" / "prompts" / prompt_name
    if candidate.exists():
        return candidate
    raise StageCheckError(f"stage prompt is missing for '{stage}' ({candidate})")


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


def _default_stage_prompt_text(stage: str) -> str:
    title = stage.replace("_", " ").title()
    return (
        f"# {title} Stage Prompt\n\n"
        "This prompt was bootstrapped by `autolab init`.\n"
        "Update it with your project-specific instructions for this stage.\n\n"
        "## Hard Guardrails (Read First)\n"
        "- Do not modify experiments whose backlog `type` is `done`; legacy closed statuses (`done`, `completed`, `closed`, `resolved`) are also treated as read-only unless a human explicitly re-opens them.\n\n"
        "- If the mapped experiment type is `done`, do not edit that experiment and wait for an explicit reopen/retype.\n\n"
        "## Repository Path Scope\n"
        "- Required stage artifacts may be under `{{iteration_path}}/...` and `.autolab/...` when specified.\n"
        "- Do not restrict analysis or edits to `experiments/` only.\n"
        "- `src/` contains core implementation that should work across multiple experiments or the broader codebase.\n"
        "- `experiments/` can contain experiment-specific implementation to prevent context flooding; move reusable logic to `src/` when multiple experiments need it.\n"
        "- `scripts/` contains useful miscellaneous task utilities.\n"
        "- `autolab/` is a valid target when task scope is orchestration, policy, prompt, or runner behavior.\n"
        "- Keep diffs minimal and avoid unrelated files.\n"
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


def _resolve_hypothesis_id(repo_root: Path, *, iteration_id: str, experiment_id: str) -> str:
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
                    if iteration_id and entry_iteration and entry_iteration != iteration_id:
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


def _resolve_prompt_run_id(*, repo_root: Path, stage: str, state: dict[str, Any]) -> str:
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
    if stage in {"launch", "slurm_monitor", "extract_results", "update_docs"}:
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


def _metrics_summary_text(metrics_payload: dict[str, Any] | None, *, run_id: str) -> str:
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


def _extract_design_target_delta(iteration_dir: Path) -> str:
    design_payload = None
    if yaml is not None:
        design_path = iteration_dir / "design.yaml"
        if design_path.exists():
            try:
                loaded = yaml.safe_load(design_path.read_text(encoding="utf-8"))
            except Exception:
                loaded = None
            if isinstance(loaded, dict):
                design_payload = loaded
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
            metric_delta = _parse_numeric_delta(str(primary_metric.get("delta_vs_baseline", "")))
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
        _append_log(repo_root, f"warning: experiment_id is empty for stage '{stage}'; prompt tokens referencing experiment_id will be blank")
    policy = _load_verifier_policy(repo_root)
    python_bin = _resolve_policy_python_bin(policy)
    total_memory_gb = _detect_total_memory_gb()
    recommended_memory_estimate = _recommended_memory_estimate(total_memory_gb)
    available_memory_gb = str(total_memory_gb) if total_memory_gb is not None else "unavailable"
    paper_targets_raw = state.get("paper_targets")
    if isinstance(paper_targets_raw, list):
        paper_targets = ", ".join(str(item).strip() for item in paper_targets_raw if str(item).strip())
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
        hypothesis_text = _safe_read_text(iteration_dir / "hypothesis.md", max_chars=12000)
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
            _auto_decision, auto_metrics_evidence_record = _suggest_decision_from_metrics(repo_root, state)
        except Exception:
            pass

    todo_focus_payload = _load_json_if_exists(repo_root / ".autolab" / "todo_focus.json")
    agent_result_payload = _load_json_if_exists(repo_root / ".autolab" / "agent_result.json")
    review_result_payload = _load_json_if_exists(iteration_dir / "review_result.json") if iteration_id else None
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
        review_feedback = "unavailable: no implementation review feedback recorded for this iteration"

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
        dry_run_output = "unavailable: no dry-run excerpt was found in implementation artifacts"

    verifier_outputs_parts: list[str] = []
    if isinstance(review_result_payload, dict):
        required_checks = review_result_payload.get("required_checks")
        if isinstance(required_checks, dict):
            verifier_outputs_parts.append(f"review_result.required_checks={_compact_json(required_checks, max_chars=400)}")
        status = str(review_result_payload.get("status", "")).strip()
        if status:
            verifier_outputs_parts.append(f"review_result.status={status}")
    template_fill_log = _extract_log_snippet(
        repo_root,
        keywords=("template_fill:", "docs_targets:", "result_sanity:", "run_health:", "schema_checks:"),
        limit=8,
    )
    if template_fill_log:
        verifier_outputs_parts.append(template_fill_log)
    verifier_outputs = "\n".join(verifier_outputs_parts).strip()
    if not verifier_outputs:
        verifier_outputs = "unavailable: no verifier output snippets detected in recent artifacts/logs"

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
    diff_summary = f"{git_summary}\n" + ("\n".join(git_paths) if git_paths else "no changed paths")

    if todo_focus_payload is None:
        todo_focus_payload = {"note": "unavailable: .autolab/todo_focus.json is missing or unreadable"}
    if agent_result_payload is None:
        agent_result_payload = {"note": "unavailable: .autolab/agent_result.json is missing or unreadable"}

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
                            for field in ("title", "description", "acceptance_criteria", "text", "stage", "task_class"):
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
    return {
        "generated_at": _utc_now(),
        "stage": stage,
        "host_mode": host_mode,
        "launch_mode": launch_mode,
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
    }


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
        "experiment_id": context.get("experiment_id", "").strip() if isinstance(context.get("experiment_id"), str) else "",
        "paper_targets": _to_text(context.get("paper_targets"), "paper_targets"),
        "python_bin": _to_text(context.get("python_bin"), "python_bin"),
        "recommended_memory_estimate": _to_text(
            context.get("recommended_memory_estimate"), "recommended_memory_estimate"
        ),
        "available_memory_gb": _to_text(context.get("available_memory_gb"), "available_memory_gb"),
        "stage": _to_text(context.get("stage"), "stage"),
        "stage_context": _to_text(context.get("stage_context"), "stage_context"),
        "run_id": _to_text(context.get("run_id"), "run_id"),
        "hypothesis_id": _to_text(context.get("hypothesis_id"), "hypothesis_id"),
        "review_feedback": _to_text(context.get("review_feedback"), "review_feedback"),
        "verifier_errors": _to_text(context.get("verifier_errors"), "verifier_errors"),
        "diff_summary": _to_text(context.get("diff_summary"), "diff_summary"),
        "verifier_outputs": _to_text(context.get("verifier_outputs"), "verifier_outputs"),
        "dry_run_output": _to_text(context.get("dry_run_output"), "dry_run_output"),
        "metrics_summary": _to_text(context.get("metrics_summary"), "metrics_summary"),
        "target_comparison": _to_text(context.get("target_comparison"), "target_comparison"),
        "decision_suggestion": _to_text(context.get("decision_suggestion"), "decision_suggestion"),
        "auto_metrics_evidence": _to_text(context.get("auto_metrics_evidence"), "auto_metrics_evidence"),
        "launch_mode": _to_text(context.get("launch_mode"), "launch_mode"),
        "task_context": context.get("task_context", ""),
        "run_group": _to_text(context.get("run_group"), "run_group"),
        "replicate_count": str(context.get("replicate_count", 1)),
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
    max_stage_attempts = str(state_snapshot.get("max_stage_attempts", "")).strip() or "-"
    assistant_mode = str(state_snapshot.get("assistant_mode", "")).strip() or "off"
    current_task_id = str(state_snapshot.get("current_task_id", "")).strip() or "none"
    last_run_id = str(state_snapshot.get("last_run_id", "")).strip() or "none"
    sync_status = str(state_snapshot.get("sync_status", "")).strip() or "unknown"
    todo_focus_summary = _format_todo_focus_summary(context_payload.get("todo_focus"))
    runner_scope = context_payload.get("runner_scope")
    if not isinstance(runner_scope, dict):
        runner_scope = {}
    scope_mode = str(runner_scope.get("mode", "")).strip() or "unknown"
    scope_workspace = str(runner_scope.get("workspace_dir", "")).strip() or "unknown"
    allowed_dirs = runner_scope.get("allowed_edit_dirs")
    if isinstance(allowed_dirs, list):
        allowed_dirs_text = ", ".join(str(item).strip() for item in allowed_dirs if str(item).strip())
    else:
        allowed_dirs_text = ""
    if not allowed_dirs_text:
        allowed_dirs_text = "(iteration workspace only)"
    disallowed_text = (
        "all paths outside the iteration workspace and allowed dirs"
        if allowed_dirs_text != "(iteration workspace only)"
        else "all paths outside the iteration workspace"
    )

    return (
        "## Runtime Stage Context\n"
        f"- stage: {stage}\n"
        f"- iteration_id: {iteration_id}\n"
        f"- iteration_path: {iteration_path}\n"
        f"- detected_host_mode: {host_mode}\n"
        f"- stage_attempt: {stage_attempt}/{max_stage_attempts}\n"
        f"- assistant_mode: {assistant_mode}\n"
        f"- current_task_id: {current_task_id}\n"
        f"- last_run_id: {last_run_id}\n"
        f"- sync_status: {sync_status}\n"
        f"- todo_focus: {todo_focus_summary}\n"
        f"- edit_scope_mode: {scope_mode}\n"
        f"- workspace_dir: {scope_workspace}\n"
        f"- allowed_edit_dirs: {allowed_dirs_text}\n"
        f"- disallowed_edit_dirs: {disallowed_text}\n"
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

    sections: list[str] = []

    # 1. ## OUTPUTS (STRICT)
    if not _has_heading_prefix(text, "outputs") and spec.required_outputs:
        lines = ["## OUTPUTS (STRICT)"]
        for output in spec.required_outputs:
            lines.append(f"- {{{{iteration_path}}}}/{output}")
        sections.append("\n".join(lines))

    # 2. ## REQUIRED INPUTS
    if not _has_heading_prefix(text, "required inputs") and spec.required_tokens:
        lines = ["## REQUIRED INPUTS"]
        for token in sorted(spec.required_tokens):
            lines.append(f"- `{{{{{token}}}}}`")
        sections.append("\n".join(lines))

    # 3. ## FILE CHECKLIST
    if not _has_heading_prefix(text, "file checklist") and spec.required_outputs:
        lines = ["## FILE CHECKLIST"]
        for output in spec.required_outputs:
            filename = Path(output).name
            lines.append(f"- [ ] `{filename}` exists and is valid")
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
    steps_match = re.search(r"^\s*##\s*steps\b", text, flags=re.IGNORECASE | re.MULTILINE)
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
) -> RenderedPromptBundle:
    registry = load_registry(repo_root)

    try:
        template_text = template_path.read_text(encoding="utf-8")
        template_text = _render_prompt_includes(repo_root, template_text, stage=stage)
    except Exception as exc:
        raise StageCheckError(f"agent runner prompt could not be read at {template_path}: {exc}") from exc

    template_text = _inject_registry_boilerplate(template_text, stage, registry)

    context_payload = _build_prompt_context(
        repo_root,
        state=state,
        stage=stage,
        runner_scope=runner_scope,
    )
    context_payload["stage_context"] = _build_runtime_stage_context_block(context_payload)
    token_values = _context_token_values(context_payload)

    tokens_in_template = sorted({match.group(1).strip() for match in PROMPT_TOKEN_PATTERN.finditer(template_text)})
    unsupported_tokens = sorted(token for token in tokens_in_template if token not in token_values)
    if unsupported_tokens:
        _append_log(
            repo_root,
            f"prompt render unsupported tokens stage={stage} template={template_path} tokens={unsupported_tokens}",
        )
        raise StageCheckError(
            f"prompt template has unsupported token(s) for stage '{stage}': {', '.join(unsupported_tokens)}"
        )

    reg_required = registry_required_tokens(registry) if registry else {}
    required_tokens = reg_required.get(stage) or PROMPT_REQUIRED_TOKENS_BY_STAGE.get(stage, {"iteration_id"})
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
            f"prompt render missing required tokens stage={stage} template={template_path} tokens={missing_required}",
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
        return f"unavailable: {token}"

    rendered_text = PROMPT_TOKEN_PATTERN.sub(_replace_token, template_text)
    if "{{stage_context}}" not in template_text:
        stage_context_block = token_values.get("stage_context", "").strip()
        if stage_context_block:
            rendered_text = f"{rendered_text.rstrip()}\n\n{stage_context_block}\n"

    unresolved_tokens = sorted({match.group(1).strip() for match in PROMPT_TOKEN_PATTERN.finditer(rendered_text)})
    unresolved_literals = [literal for literal in PROMPT_LITERAL_TOKENS if literal in rendered_text]
    if unresolved_tokens or unresolved_literals:
        _append_log(
            repo_root,
            (
                f"prompt render unresolved placeholders stage={stage} template={template_path} "
                f"tokens={unresolved_tokens} literals={unresolved_literals}"
            ),
        )
        unresolved_text = ", ".join([*unresolved_tokens, *unresolved_literals]) or "<unknown>"
        raise StageCheckError(
            f"rendered prompt contains unresolved placeholders for stage '{stage}': {unresolved_text}"
        )

    rendered_dir = repo_root / ".autolab" / "prompts" / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)
    rendered_path = rendered_dir / f"{stage}.md"
    context_path = rendered_dir / f"{stage}.context.json"
    rendered_path.write_text(rendered_text, encoding="utf-8")

    context_payload = {
        **context_payload,
        "template_path": str(template_path),
        "rendered_prompt_path": str(rendered_path),
        "rendered_context_path": str(context_path),
    }
    _write_json(context_path, context_payload)

    return RenderedPromptBundle(
        template_path=template_path,
        rendered_path=rendered_path,
        context_path=context_path,
        prompt_text=rendered_text,
        context_payload=context_payload,
    )
