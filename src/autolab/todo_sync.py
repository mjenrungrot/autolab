from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - optional dependency
    yaml = None


ACTIVE_STAGES = (
    "hypothesis",
    "design",
    "implementation",
    "implementation_review",
    "launch",
    "extract_results",
    "update_docs",
    "decide_repeat",
)
TERMINAL_STAGES = ("human_review", "stop")
ALL_STAGES = set(ACTIVE_STAGES + TERMINAL_STAGES)
IMPLEMENTATION_PRIORITY_STAGES = ("implementation", "implementation_review")
DECISION_TO_DESIGN_STAGES = {
    "design",
    "implementation",
    "implementation_review",
    "launch",
    "extract_results",
    "update_docs",
}
FALLBACK_SCOPE_LOCAL = "policy:no_task_fallback:local"
FALLBACK_SCOPE_SLURM = "policy:no_task_fallback:slurm"
_DEFAULT_FALLBACK_TASK_TEXT_LOCAL = (
    "No remaining actionable tasks were detected on local execution context. "
    "Propose and implement a concrete codebase improvement in src/, scripts/, or experiments/ "
    "(reliability, maintainability, performance, or developer ergonomics)."
)
_DEFAULT_FALLBACK_TASK_TEXT_SLURM = (
    "No remaining actionable tasks were detected on remote SLURM execution context. "
    "Define a new experiment or analysis direction first, then plan implementation "
    "improvements only after that experiment/analysis path is explicit."
)

HYPOTHESIS_CLOSED_STATUSES = {"done", "completed", "closed", "resolved"}
EXPERIMENT_CLOSED_STATUSES = {"done", "completed", "closed", "resolved"}
EXPERIMENT_TYPES = ("plan", "in_progress", "done")
READ_ONLY_EXPERIMENT_TYPES = {"done"}
DEFAULT_EXPERIMENT_TYPE = "plan"
SUCCESS_STAGE_TRANSITIONS = {
    "hypothesis": "design",
    "design": "implementation",
    "implementation": "implementation_review",
    "implementation_review": "launch",
    "launch": "extract_results",
    "extract_results": "update_docs",
    "update_docs": "decide_repeat",
}
DEFAULT_NOTE = "Write non-task notes here. Bullets in this section are ignored by autolab steering."
DEFAULT_MAX_GENERATED_TODO_TASKS = 5


@dataclass(frozen=True)
class TodoSyncResult:
    changed_files: list[Path]
    open_count: int
    removed_count: int
    message: str


@dataclass(frozen=True)
class _ParsedBullet:
    text: str
    stage_tag: str | None
    checked: bool
    order: int
    priority: str | None = None
    owner: str | None = None
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class _GeneratedCandidate:
    stage: str
    text: str
    scope: str


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    )


def _normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _normalize_text_key(value: str) -> str:
    text = _normalize_space(value)
    text = re.sub(r"\[\s*stage\s*:[^\]]+\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\s*priority\s*:[^\]]+\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\s*owner\s*:[^\]]+\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[\s*label\s*:[^\]]+\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\[(?:x|X| )\]\s+", "", text)
    return _normalize_space(text).lower()


def _normalize_stage(stage: str | None, fallback: str) -> str:
    candidate = _normalize_space(stage or "").lower()
    if candidate in ALL_STAGES:
        return candidate
    return fallback


def _normalize_host_mode(host_mode: str | None) -> str:
    candidate = _normalize_space(str(host_mode or "")).lower()
    if candidate == "slurm":
        return "slurm"
    return "local"


def _decision_for_stage(stage: str) -> str | None:
    normalized = _normalize_space(stage).lower()
    if normalized == "hypothesis":
        return "hypothesis"
    if normalized in DECISION_TO_DESIGN_STAGES:
        return "design"
    if normalized in {"stop", "human_review"}:
        return normalized
    return None


def _fallback_candidates_for_host(host_mode: str | None) -> list[_GeneratedCandidate]:
    normalized_host_mode = _normalize_host_mode(host_mode)
    if normalized_host_mode == "slurm":
        return [
            _GeneratedCandidate(
                stage="hypothesis",
                scope=FALLBACK_SCOPE_SLURM,
                text=_DEFAULT_FALLBACK_TASK_TEXT_SLURM,
            )
        ]
    return [
        _GeneratedCandidate(
            stage="implementation",
            scope=FALLBACK_SCOPE_LOCAL,
            text=_DEFAULT_FALLBACK_TASK_TEXT_LOCAL,
        )
    ]


def _coerce_policy_fallback_candidate(
    raw_section: Any,
    *,
    default_stage: str,
    default_scope: str,
    default_text: str,
    iteration_implementation_path: str,
) -> _GeneratedCandidate:
    if not isinstance(raw_section, dict):
        return _GeneratedCandidate(
            stage=default_stage, scope=default_scope, text=default_text
        )
    raw_stage = _normalize_space(str(raw_section.get("stage", ""))).lower()
    stage = raw_stage if raw_stage in ALL_STAGES else default_stage
    scope = _normalize_space(str(raw_section.get("scope", ""))) or default_scope
    text = _normalize_space(str(raw_section.get("text", ""))) or default_text
    if iteration_implementation_path and iteration_implementation_path not in text:
        text = (
            f"{text} Scope guardrail: keep experiment-specific implementation under "
            f"`{iteration_implementation_path}` to avoid unrelated file edits."
        )
    return _GeneratedCandidate(stage=stage, scope=scope, text=text)


def _fallback_candidates_for_host_with_policy(
    repo_root: Path,
    host_mode: str | None,
    *,
    iteration_implementation_path: str,
) -> list[_GeneratedCandidate]:
    defaults = _fallback_candidates_for_host(host_mode)
    default = (
        defaults[0]
        if defaults
        else _GeneratedCandidate(
            stage="implementation",
            scope=FALLBACK_SCOPE_LOCAL,
            text=_DEFAULT_FALLBACK_TASK_TEXT_LOCAL,
        )
    )
    default_text = default.text
    if (
        iteration_implementation_path
        and iteration_implementation_path not in default_text
    ):
        default_text = (
            f"{default_text} Scope guardrail: keep experiment-specific implementation under "
            f"`{iteration_implementation_path}` to avoid unrelated file edits."
        )
        default = _GeneratedCandidate(
            stage=default.stage, scope=default.scope, text=default_text
        )
    normalized_host_mode = _normalize_host_mode(host_mode)

    try:
        from autolab.config import _load_verifier_policy

        policy = _load_verifier_policy(repo_root)
    except Exception:
        policy = {}

    autorun = policy.get("autorun") if isinstance(policy, dict) else {}
    todo_fallback = autorun.get("todo_fallback") if isinstance(autorun, dict) else {}
    if not isinstance(todo_fallback, dict):
        return [default]

    raw_section = todo_fallback.get(normalized_host_mode)
    resolved = _coerce_policy_fallback_candidate(
        raw_section,
        default_stage=default.stage,
        default_scope=default.scope,
        default_text=default.text,
        iteration_implementation_path=iteration_implementation_path,
    )
    return [resolved]


def _has_actionable_decision_candidates(candidates: list[_GeneratedCandidate]) -> bool:
    for item in candidates:
        if _decision_for_stage(item.stage) is not None:
            return True
    return False


def _manual_bullets_have_actionable_decision(
    parsed_bullets: list[_ParsedBullet],
    *,
    current_stage: str,
) -> bool:
    for parsed in parsed_bullets:
        if parsed.checked:
            continue
        if parsed.stage_tag:
            stage = _normalize_stage(parsed.stage_tag, current_stage)
        else:
            stage = _infer_manual_stage(text=parsed.text, current_stage=current_stage)
        if _decision_for_stage(stage) is not None:
            return True
    return False


def _classify_task(*, stage: str, text: str) -> str:
    lowered = _normalize_space(text).lower()
    if stage in {"hypothesis", "design", "launch", "extract_results"}:
        return "experiment"
    if stage in {"implementation", "implementation_review"}:
        return "feature"
    if stage == "update_docs":
        return "docs"
    if any(
        token in lowered
        for token in ("paper", "docs", "documentation", "wiki", "readme", "manuscript")
    ):
        return "docs"
    if any(
        token in lowered
        for token in ("hypothesis", "experiment", "baseline", "metric", "run", "launch")
    ):
        return "experiment"
    if any(
        token in lowered
        for token in ("feature", "implement", "code", "refactor", "fix", "module")
    ):
        return "feature"
    return "unknown"


def _infer_manual_stage(*, text: str, current_stage: str) -> str:
    lowered = _normalize_space(text).lower()
    token_groups = (
        (
            "update_docs",
            (
                "paper",
                "docs",
                "documentation",
                "wiki",
                "readme",
                "manuscript",
                "write docs",
            ),
        ),
        ("extract_results", ("extract", "analysis", "metrics", "summary", "aggregate")),
        (
            "launch",
            ("launch", "submit", "slurm", "run_local", "run_slurm", "sync artifacts"),
        ),
        (
            "implementation_review",
            ("implementation review", "review_result", "gate review"),
        ),
        (
            "implementation",
            ("implement", "feature", "code", "refactor", "fix", "module", "wrapper"),
        ),
        ("design", ("design", "spec", "schema", "experiment plan", "baseline")),
        ("hypothesis", ("hypothesis", "success criteria", "metric", "expected delta")),
    )
    for stage, tokens in token_groups:
        if any(token in lowered for token in tokens):
            return stage
    return _normalize_stage(None, current_stage)


def _hash_task_id(source: str, scope: str, stage: str, text_key: str) -> str:
    digest = hashlib.sha1(
        f"{source}|{scope}|{stage}|{text_key}".encode("utf-8")
    ).hexdigest()
    return f"task_{digest[:12]}"


def _write_text_if_changed(path: Path, content: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return True


def _write_json_if_changed(path: Path, payload: dict[str, Any]) -> bool:
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    return _write_text_if_changed(path, content)


def _default_todo_content() -> str:
    return (
        "# TODO\n\n"
        "## Tasks\n"
        "<!-- Add one bullet per task. Optional stage tag: [stage:design]. -->\n\n"
        "## Notes\n"
        f"{DEFAULT_NOTE}\n"
    )


def _load_json_dict(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return dict(default)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return dict(default)
    if not isinstance(payload, dict):
        return dict(default)
    return payload


def _load_todo_state(path: Path) -> dict[str, Any]:
    state = _load_json_dict(path, {"version": 1, "next_order": 1, "tasks": {}})
    if not isinstance(state.get("tasks"), dict):
        state["tasks"] = {}
    try:
        state["next_order"] = int(state.get("next_order", 1))
    except Exception:
        state["next_order"] = 1
    if state["next_order"] < 1:
        state["next_order"] = 1
    state["version"] = 1
    return state


def _load_max_generated_todo_tasks(repo_root: Path) -> int:
    try:
        from autolab.config import _load_guardrail_config

        loaded = _load_guardrail_config(repo_root).max_generated_todo_tasks
        return loaded if loaded >= 1 else 1
    except Exception:
        return DEFAULT_MAX_GENERATED_TODO_TASKS


def _extract_sections(todo_text: str) -> tuple[list[str], list[str], bool]:
    lines = todo_text.splitlines()
    tasks_lines: list[str] = []
    notes_lines: list[str] = []
    section = ""
    seen_tasks = False

    for line in lines:
        stripped = line.strip()
        lowered = stripped.lower()
        if lowered == "## tasks":
            section = "tasks"
            seen_tasks = True
            continue
        if lowered == "## notes":
            section = "notes"
            continue
        if stripped.startswith("## "):
            section = "other"
            continue

        if section == "tasks":
            tasks_lines.append(line)
        elif section == "notes":
            notes_lines.append(line)

    if not seen_tasks:
        fallback = []
        in_notes = False
        for line in lines:
            stripped = line.strip()
            lowered = stripped.lower()
            if lowered == "## notes":
                in_notes = True
                continue
            if stripped.startswith("## "):
                in_notes = False
            if not in_notes:
                fallback.append(line)
        tasks_lines = fallback

    return tasks_lines, notes_lines, seen_tasks


def _parse_bullets(tasks_lines: list[str]) -> list[_ParsedBullet]:
    bullet_pattern = re.compile(r"^([ \t]*)([-*+]|(?:\d+[\.\)]))\s+(.*)$")
    bullets: list[str] = []
    current: str | None = None
    base_indent: int | None = None
    in_comment = False

    for raw_line in tasks_lines:
        stripped = raw_line.strip()
        if not stripped:
            continue

        if in_comment:
            if "-->" in stripped:
                in_comment = False
            continue
        if stripped.startswith("<!--"):
            if "-->" not in stripped:
                in_comment = True
            continue

        match = bullet_pattern.match(raw_line)
        if match:
            indent_raw = match.group(1).replace("\t", "    ")
            indent = len(indent_raw)
            body = match.group(3).strip()
            if base_indent is None or indent < base_indent:
                base_indent = indent
            top_level = indent <= int(base_indent or 0)
            if top_level:
                if current is not None:
                    bullets.append(current)
                current = body
            else:
                if current is None:
                    current = body
                else:
                    current = _normalize_space(f"{current} {body}")
            continue

        if stripped.startswith("#"):
            if current is not None:
                bullets.append(current)
                current = None
            continue

        if current is not None:
            current = _normalize_space(f"{current} {stripped}")

    if current is not None:
        bullets.append(current)

    parsed: list[_ParsedBullet] = []
    for idx, bullet in enumerate(bullets, start=1):
        text = bullet.strip()
        checked = False

        match_checked = re.match(r"^\[(x|X)\]\s+(.*)$", text)
        if match_checked:
            checked = True
            text = match_checked.group(2).strip()
        else:
            match_unchecked = re.match(r"^\[\s\]\s+(.*)$", text)
            if match_unchecked:
                text = match_unchecked.group(1).strip()

        stage_tag = None
        match_stage = re.search(
            r"\[\s*stage\s*:\s*([a-z_]+)\s*\]", text, flags=re.IGNORECASE
        )
        if match_stage:
            stage_tag = match_stage.group(1).strip().lower()
            text = _normalize_space(
                text[: match_stage.start()] + " " + text[match_stage.end() :]
            )

        priority = None
        match_priority = re.search(
            r"\[\s*priority\s*:\s*([^\]]+)\]", text, flags=re.IGNORECASE
        )
        if match_priority:
            priority = match_priority.group(1).strip().lower()
            text = _normalize_space(
                text[: match_priority.start()] + " " + text[match_priority.end() :]
            )

        owner = None
        match_owner = re.search(
            r"\[\s*owner\s*:\s*([^\]]+)\]", text, flags=re.IGNORECASE
        )
        if match_owner:
            owner = match_owner.group(1).strip()
            text = _normalize_space(
                text[: match_owner.start()] + " " + text[match_owner.end() :]
            )

        labels: list[str] = []
        for match_label in re.finditer(
            r"\[\s*label\s*:\s*([^\]]+)\]", text, flags=re.IGNORECASE
        ):
            labels.append(match_label.group(1).strip().lower())
        text = re.sub(r"\[\s*label\s*:[^\]]+\]", "", text, flags=re.IGNORECASE)

        text = _normalize_space(text)
        if not text:
            continue

        parsed.append(
            _ParsedBullet(
                text=text,
                stage_tag=stage_tag,
                checked=checked,
                order=idx,
                priority=priority,
                owner=owner,
                labels=tuple(labels),
            )
        )

    return parsed


def _render_todo(open_tasks: list[dict[str, Any]], notes_lines: list[str]) -> str:
    lines = ["# TODO", "", "## Tasks"]
    if open_tasks:
        for task in open_tasks:
            lines.append(f"- [stage:{task['stage']}] {task['text']}")
    else:
        lines.append("<!-- No open tasks. Add bullets here. -->")

    lines.extend(["", "## Notes"])
    if notes_lines:
        for line in notes_lines:
            lines.append(line)
    else:
        lines.append(DEFAULT_NOTE)

    return "\n".join(lines).rstrip() + "\n"


def _is_closed_hypothesis_status(value: Any) -> bool:
    normalized = _normalize_space(str(value)).lower()
    return normalized in HYPOTHESIS_CLOSED_STATUSES


def _normalize_experiment_type(value: Any) -> str:
    normalized = _normalize_space(str(value)).lower()
    if normalized in EXPERIMENT_TYPES:
        return normalized
    return ""


def _is_completed_experiment_status(value: Any) -> bool:
    normalized = _normalize_space(str(value)).lower()
    return normalized in EXPERIMENT_CLOSED_STATUSES


def _is_read_only_experiment_entry(entry: dict[str, Any]) -> bool:
    experiment_type = _normalize_experiment_type(entry.get("type"))
    if experiment_type in READ_ONLY_EXPERIMENT_TYPES:
        return True
    return _is_completed_experiment_status(entry.get("status"))


def _resolve_iteration_dir(
    repo_root: Path,
    *,
    iteration_id: str,
    backlog_payload: dict[str, Any] | None = None,
) -> Path:
    normalized_iteration = _normalize_space(iteration_id)
    experiments_root = repo_root / "experiments"
    if not normalized_iteration:
        return experiments_root

    preferred_type = ""
    experiments = (
        backlog_payload.get("experiments")
        if isinstance(backlog_payload, dict)
        else None
    )
    if isinstance(experiments, list):
        for entry in experiments:
            if not isinstance(entry, dict):
                continue
            entry_iteration = _normalize_space(str(entry.get("iteration_id", "")))
            if entry_iteration != normalized_iteration:
                continue
            entry_type = _normalize_experiment_type(entry.get("type"))
            if entry_type:
                preferred_type = entry_type
                break

    candidates: list[Path] = []
    if preferred_type:
        candidates.append(experiments_root / preferred_type / normalized_iteration)
    for experiment_type in EXPERIMENT_TYPES:
        candidate = experiments_root / experiment_type / normalized_iteration
        if candidate not in candidates:
            candidates.append(candidate)

    for candidate in candidates:
        if candidate.exists():
            return candidate

    fallback_type = preferred_type or DEFAULT_EXPERIMENT_TYPE
    return experiments_root / fallback_type / normalized_iteration


def _resolve_iteration_implementation_path(
    repo_root: Path, state: dict[str, Any] | None
) -> str:
    iteration_id = _normalize_space(str((state or {}).get("iteration_id", "")))
    if not iteration_id or iteration_id.startswith("<"):
        return ""
    iteration_dir = _resolve_iteration_dir(
        repo_root, iteration_id=iteration_id, backlog_payload=None
    )
    try:
        relative = iteration_dir.relative_to(repo_root).as_posix()
    except Exception:
        relative = iteration_dir.as_posix()
    return f"{relative}/implementation"


def _is_iteration_completed_in_backlog(
    backlog_payload: dict[str, Any], iteration_id: str
) -> bool:
    normalized_iteration = _normalize_space(iteration_id)
    if not normalized_iteration:
        return False
    experiments = backlog_payload.get("experiments")
    if not isinstance(experiments, list):
        return False
    for entry in experiments:
        if not isinstance(entry, dict):
            continue
        entry_iteration = _normalize_space(str(entry.get("iteration_id", "")))
        if entry_iteration != normalized_iteration:
            continue
        if _is_read_only_experiment_entry(entry):
            return True
    return False


def _extract_open_questions(path: Path) -> list[str]:
    if not path.exists():
        return []
    questions: list[str] = []
    in_open_section = False

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        lowered = stripped.lower()

        if stripped.startswith("## "):
            in_open_section = lowered.startswith("## open questions")

        labeled = re.search(
            r"\*\*\s*Open Question\.\s*\*\*\s*(.+)$", stripped, flags=re.IGNORECASE
        )
        if labeled:
            candidate = _normalize_space(labeled.group(1))
            if candidate and "none at this stage" not in candidate.lower():
                questions.append(candidate)
            continue

        if not in_open_section:
            continue

        if stripped.startswith(">"):
            candidate = _normalize_space(stripped.lstrip(">"))
        elif stripped.startswith("- "):
            candidate = _normalize_space(stripped[2:])
        else:
            candidate = _normalize_space(stripped)

        normalized_candidate = candidate.lower()
        if normalized_candidate.startswith("## "):
            continue
        if normalized_candidate in {"open questions", "open question"}:
            continue
        if candidate and "none at this stage" not in normalized_candidate:
            questions.append(candidate)

    deduped: list[str] = []
    seen: set[str] = set()
    for question in questions:
        key = _normalize_text_key(question)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(question)
    return deduped


def _extract_pending_lines(path: Path) -> list[str]:
    if not path.exists():
        return []

    lines = path.read_text(encoding="utf-8").splitlines()
    keywords = ("pending", "defer", "deferred", "todo", "to do", "later", "next step")
    pending: list[str] = []

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith(">"):
            stripped = _normalize_space(stripped.lstrip(">"))
        if stripped.startswith("- "):
            stripped = _normalize_space(stripped[2:])

        lowered = stripped.lower()
        if "open question" in lowered:
            continue
        if any(token in lowered for token in keywords):
            pending.append(stripped)

    deduped: list[str] = []
    seen: set[str] = set()
    for item in pending:
        key = _normalize_text_key(item)
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _collect_generated_candidates(
    repo_root: Path, state: dict[str, Any] | None
) -> list[_GeneratedCandidate]:
    candidates: list[_GeneratedCandidate] = []
    iteration_id = _normalize_space(str((state or {}).get("iteration_id", "")))

    if state is not None:
        stage = _normalize_space(str(state.get("stage", ""))).lower()
        if stage == "decide_repeat":
            candidates.append(
                _GeneratedCandidate(
                    stage="decide_repeat",
                    scope="state:decide_repeat",
                    text="Choose the next workflow decision for this iteration so the run can continue with a concrete branch.",
                )
            )

        sync_status = _normalize_space(str(state.get("sync_status", ""))).lower()
        if sync_status in {"pending", "failed"}:
            candidates.append(
                _GeneratedCandidate(
                    stage="launch",
                    scope=f"state:sync:{sync_status}",
                    text=f"Resolve launch artifact synchronization because sync_status is '{sync_status}' before progressing downstream stages.",
                )
            )

    backlog_path = repo_root / ".autolab" / "backlog.yaml"
    backlog_payload: dict[str, Any] | None = None
    iteration_is_completed = False
    if backlog_path.exists() and yaml is not None:
        try:
            parsed_backlog = yaml.safe_load(backlog_path.read_text(encoding="utf-8"))
        except Exception:
            parsed_backlog = None
        if isinstance(parsed_backlog, dict):
            backlog_payload = parsed_backlog
            iteration_is_completed = _is_iteration_completed_in_backlog(
                backlog_payload, iteration_id
            )
            hypotheses = backlog_payload.get("hypotheses")
            if isinstance(hypotheses, list):
                for entry in hypotheses:
                    if not isinstance(entry, dict):
                        continue
                    if _is_closed_hypothesis_status(entry.get("status")):
                        continue
                    hypothesis_id = (
                        _normalize_space(str(entry.get("id", "hypothesis")))
                        or "hypothesis"
                    )
                    title = _normalize_space(
                        str(entry.get("title", "untitled hypothesis"))
                    )
                    metric = (
                        _normalize_space(
                            str(entry.get("success_metric", "primary metric"))
                        )
                        or "primary metric"
                    )
                    candidates.append(
                        _GeneratedCandidate(
                            stage="hypothesis",
                            scope=f"backlog:hypothesis:{hypothesis_id}",
                            text=(
                                f"Advance backlog hypothesis {hypothesis_id} ({title}) by finalizing measurable success criteria"
                                f" for {metric}."
                            ),
                        )
                    )

            experiments = backlog_payload.get("experiments")
            if isinstance(experiments, list):
                for entry in experiments:
                    if not isinstance(entry, dict):
                        continue
                    if _is_read_only_experiment_entry(entry):
                        continue
                    experiment_id = (
                        _normalize_space(str(entry.get("id", "experiment")))
                        or "experiment"
                    )
                    experiment_iteration = (
                        _normalize_space(
                            str(entry.get("iteration_id", "current iteration"))
                        )
                        or "current iteration"
                    )
                    experiment_type = _normalize_experiment_type(entry.get("type"))
                    if experiment_type == "plan":
                        target_stage = "hypothesis"
                        guidance = (
                            "by refining the plan and hypothesis before implementation."
                        )
                    elif experiment_type == "in_progress":
                        target_stage = "implementation"
                        guidance = "by progressing implementation, launch readiness, or result extraction."
                    else:
                        target_stage = "design"
                        guidance = (
                            "by keeping the design runnable and implementation-ready."
                        )
                    type_suffix = (
                        f" (type={experiment_type})" if experiment_type else ""
                    )
                    candidates.append(
                        _GeneratedCandidate(
                            stage=target_stage,
                            scope=f"backlog:experiment:{experiment_id}",
                            text=(
                                f"Advance backlog experiment {experiment_id} for iteration {experiment_iteration}{type_suffix} "
                                f"{guidance}"
                            ),
                        )
                    )

    if iteration_id and not iteration_id.startswith("<") and not iteration_is_completed:
        iteration_dir = _resolve_iteration_dir(
            repo_root,
            iteration_id=iteration_id,
            backlog_payload=backlog_payload,
        )
        analysis_summary_path = iteration_dir / "analysis" / "summary.md"
        docs_update_path = iteration_dir / "docs_update.md"

        for question in _extract_open_questions(analysis_summary_path):
            candidates.append(
                _GeneratedCandidate(
                    stage="extract_results",
                    scope=f"analysis:open_question:{_normalize_text_key(question)}",
                    text=f"Resolve analysis open question for iteration {iteration_id}: {question}",
                )
            )

        for question in _extract_open_questions(docs_update_path):
            candidates.append(
                _GeneratedCandidate(
                    stage="update_docs",
                    scope=f"docs_update:open_question:{_normalize_text_key(question)}",
                    text=f"Resolve documentation open question for iteration {iteration_id}: {question}",
                )
            )

        for pending_line in _extract_pending_lines(docs_update_path):
            candidates.append(
                _GeneratedCandidate(
                    stage="update_docs",
                    scope=f"docs_update:pending:{_normalize_text_key(pending_line)}",
                    text=f"Address pending documentation item for iteration {iteration_id}: {pending_line}",
                )
            )

    deduped: list[_GeneratedCandidate] = []
    seen: set[tuple[str, str, str]] = set()
    for item in candidates:
        stage = _normalize_stage(item.stage, "design")
        text = _normalize_space(item.text)
        key = (_normalize_text_key(text), stage, item.scope)
        if not key[0] or key in seen:
            continue
        seen.add(key)
        deduped.append(_GeneratedCandidate(stage=stage, text=text, scope=item.scope))
    return deduped


def _mark_completed(task: dict[str, Any], now: str) -> bool:
    if task.get("status") != "open":
        return False
    task["status"] = "completed"
    task["last_evidence_at"] = now
    return True


def _upsert_task(
    todo_state: dict[str, Any],
    *,
    source: str,
    scope: str,
    stage: str,
    text: str,
    now: str,
) -> str:
    tasks = todo_state["tasks"]
    clean_text = _normalize_space(text)
    norm = _normalize_text_key(clean_text)
    task_id = _hash_task_id(source=source, scope=scope, stage=stage, text_key=norm)
    task_class = _classify_task(stage=stage, text=clean_text)

    if task_id not in tasks:
        tasks[task_id] = {
            "task_id": task_id,
            "source": source,
            "scope": scope,
            "stage": stage,
            "task_class": task_class,
            "text": clean_text,
            "text_key": norm,
            "status": "open",
            "first_seen_order": int(todo_state.get("next_order", 1)),
            "first_seen_at": now,
            "last_seen_at": now,
            "last_evidence_at": "",
        }
        todo_state["next_order"] = int(todo_state.get("next_order", 1)) + 1
    else:
        task = tasks[task_id]
        task["source"] = source
        task["scope"] = scope
        task["stage"] = stage
        task["task_class"] = task_class
        task["text"] = clean_text
        task["text_key"] = norm
        task["last_seen_at"] = now
        task["status"] = "open"

    return task_id


_PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _open_tasks_sorted(todo_state: dict[str, Any]) -> list[dict[str, Any]]:
    tasks = [
        task
        for task in todo_state.get("tasks", {}).values()
        if task.get("status") == "open"
    ]
    tasks.sort(
        key=lambda item: (
            _PRIORITY_ORDER.get(str(item.get("priority", "")).lower(), 4),
            0 if str(item.get("source", "")) == "manual" else 1,
            int(item.get("first_seen_order", 0)),
            str(item.get("task_id", "")),
        )
    )
    return tasks


def _prune_non_open_tasks(todo_state: dict[str, Any]) -> int:
    tasks = todo_state.get("tasks", {})
    if not isinstance(tasks, dict):
        return 0

    removable_ids: list[str] = []
    for task_id, task in tasks.items():
        if not isinstance(task, dict):
            removable_ids.append(str(task_id))
            continue
        status = _normalize_space(str(task.get("status", ""))).lower()
        if status != "open":
            removable_ids.append(str(task_id))

    for task_id in removable_ids:
        tasks.pop(task_id, None)

    return len(removable_ids)


def _manual_task_done_by_outcome(
    task_stage: str, run_outcome: dict[str, Any] | None
) -> bool:
    if run_outcome is None:
        return False

    if int(run_outcome.get("exit_code", 1)) != 0:
        return False

    stage_before = _normalize_space(str(run_outcome.get("stage_before", ""))).lower()
    stage_after = _normalize_space(str(run_outcome.get("stage_after", ""))).lower()
    transitioned = bool(run_outcome.get("transitioned", False))

    if task_stage == "decide_repeat":
        return stage_before == "decide_repeat" and transitioned

    if task_stage in {"stop", "human_review"}:
        if (
            transitioned
            and stage_before == "decide_repeat"
            and stage_after == task_stage
        ):
            return True
        return (
            stage_before == task_stage
            and stage_after == task_stage
            and not transitioned
        )

    expected_after = SUCCESS_STAGE_TRANSITIONS.get(task_stage)
    if not expected_after:
        return False

    return transitioned and stage_before == task_stage and stage_after == expected_after


def _write_focus_snapshot(
    repo_root: Path,
    *,
    stage: str,
    open_tasks: list[dict[str, Any]],
    now: str,
    limit: int = 5,
) -> bool:
    focus_path = repo_root / ".autolab" / "todo_focus.json"
    focus = [
        {
            "task_id": task.get("task_id", ""),
            "stage": task.get("stage", ""),
            "source": task.get("source", ""),
            "text": task.get("text", ""),
        }
        for task in open_tasks
        if str(task.get("stage", "")) == stage
    ]

    if not focus:
        focus = [
            {
                "task_id": task.get("task_id", ""),
                "stage": task.get("stage", ""),
                "source": task.get("source", ""),
                "text": task.get("text", ""),
            }
            for task in open_tasks[:limit]
        ]
    else:
        focus = focus[:limit]

    payload = {
        "generated_at": now,
        "stage": stage,
        "open_task_count": len(open_tasks),
        "focus_tasks": focus,
    }
    return _write_json_if_changed(focus_path, payload)


def _sync_internal(
    repo_root: Path,
    state: dict[str, Any] | None,
    run_outcome: dict[str, Any] | None,
    *,
    host_mode: str | None = None,
) -> TodoSyncResult:
    now = _utc_now()
    changed_files: list[Path] = []
    removed_count = 0
    resolved_host_mode = _normalize_host_mode(host_mode)
    iteration_implementation_path = _resolve_iteration_implementation_path(
        repo_root, state
    )
    assistant_mode = (
        _normalize_space(str((state or {}).get("assistant_mode", ""))).lower() == "on"
    )

    todo_path = repo_root / "docs" / "todo.md"
    todo_state_path = repo_root / ".autolab" / "todo_state.json"

    if not todo_path.exists():
        if _write_text_if_changed(todo_path, _default_todo_content()):
            changed_files.append(todo_path)

    todo_text = (
        todo_path.read_text(encoding="utf-8")
        if todo_path.exists()
        else _default_todo_content()
    )
    tasks_lines, notes_lines, _ = _extract_sections(todo_text)
    parsed_bullets = _parse_bullets(tasks_lines)

    todo_state = _load_todo_state(todo_state_path)
    tasks = todo_state["tasks"]

    current_stage = _normalize_stage(
        str((state or {}).get("stage", "")).lower(), "hypothesis"
    )
    manual_has_actionable_decision = _manual_bullets_have_actionable_decision(
        parsed_bullets,
        current_stage=current_stage,
    )
    generated_candidates = _collect_generated_candidates(repo_root, state)
    if current_stage != "decide_repeat":
        generated_candidates = [
            item for item in generated_candidates if item.stage == current_stage
        ]
    if (
        current_stage == "decide_repeat"
        and not manual_has_actionable_decision
        and not _has_actionable_decision_candidates(generated_candidates)
    ):
        generated_candidates.extend(
            _fallback_candidates_for_host_with_policy(
                repo_root,
                resolved_host_mode,
                iteration_implementation_path=iteration_implementation_path,
            )
        )
    max_generated_tasks = _load_max_generated_todo_tasks(repo_root)
    if len(generated_candidates) > max_generated_tasks:
        generated_candidates = generated_candidates[:max_generated_tasks]
    generated_norm_stage = {
        (_normalize_text_key(item.text), item.stage) for item in generated_candidates
    }
    existing_generated_norm_stage = {
        (str(task.get("text_key", "")), str(task.get("stage", "")))
        for task in tasks.values()
        if task.get("source") == "generated" and task.get("status") == "open"
    }

    seen_manual_ids: set[str] = set()

    for parsed in parsed_bullets:
        if parsed.stage_tag:
            stage = _normalize_stage(parsed.stage_tag, current_stage)
        else:
            stage = _infer_manual_stage(text=parsed.text, current_stage=current_stage)
        text_key = _normalize_text_key(parsed.text)
        if not text_key:
            continue

        is_generated_echo = (text_key, stage) in generated_norm_stage or (
            text_key,
            stage,
        ) in existing_generated_norm_stage
        if is_generated_echo:
            if parsed.checked:
                for task in tasks.values():
                    if (
                        task.get("source") == "generated"
                        and task.get("status") == "open"
                        and task.get("stage") == stage
                        and task.get("text_key") == text_key
                    ):
                        if _mark_completed(task, now):
                            removed_count += 1
            continue

        manual_task_id = _upsert_task(
            todo_state,
            source="manual",
            scope="manual_user",
            stage=stage,
            text=parsed.text,
            now=now,
        )
        seen_manual_ids.add(manual_task_id)

        task_entry = tasks.get(manual_task_id)
        if task_entry is not None:
            if parsed.priority:
                task_entry["priority"] = parsed.priority
            if parsed.owner:
                task_entry["owner"] = parsed.owner
            if parsed.labels:
                task_entry["labels"] = list(parsed.labels)

        if parsed.checked:
            if _mark_completed(tasks[manual_task_id], now):
                removed_count += 1

    for task_id, task in tasks.items():
        if (
            task.get("source") == "manual"
            and task.get("status") == "open"
            and task_id not in seen_manual_ids
        ):
            task["status"] = "removed"
            task["last_evidence_at"] = now

    active_generated_ids: set[str] = set()
    for candidate in generated_candidates:
        generated_id = _upsert_task(
            todo_state,
            source="generated",
            scope=candidate.scope,
            stage=candidate.stage,
            text=candidate.text,
            now=now,
        )
        active_generated_ids.add(generated_id)

    for task_id, task in tasks.items():
        if (
            task.get("source") == "generated"
            and task.get("status") == "open"
            and task_id not in active_generated_ids
        ):
            if _mark_completed(task, now):
                removed_count += 1

    if run_outcome is not None:
        for task in tasks.values():
            if task.get("source") != "manual" or task.get("status") != "open":
                continue
            task_stage = _normalize_stage(str(task.get("stage", "")), current_stage)
            if _manual_task_done_by_outcome(task_stage, run_outcome):
                if _mark_completed(task, now):
                    removed_count += 1

    open_tasks = _open_tasks_sorted(todo_state)
    if not open_tasks and assistant_mode and current_stage not in TERMINAL_STAGES:
        for candidate in _fallback_candidates_for_host_with_policy(
            repo_root,
            resolved_host_mode,
            iteration_implementation_path=iteration_implementation_path,
        ):
            _upsert_task(
                todo_state,
                source="generated",
                scope=candidate.scope,
                stage=candidate.stage,
                text=candidate.text,
                now=now,
            )
        open_tasks = _open_tasks_sorted(todo_state)

    deduped: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str, str]] = set()
    for task in open_tasks:
        source_scope = (
            "manual"
            if task.get("source") == "manual"
            else str(task.get("scope", "generated"))
        )
        key = (str(task.get("text_key", "")), str(task.get("stage", "")), source_scope)
        if key in seen_keys:
            task["status"] = "completed"
            task["last_evidence_at"] = now
            removed_count += 1
            continue
        seen_keys.add(key)
        deduped.append(task)

    open_tasks = deduped

    removed_count += _prune_non_open_tasks(todo_state)
    open_tasks = _open_tasks_sorted(todo_state)

    rendered = _render_todo(open_tasks, notes_lines)
    if _write_text_if_changed(todo_path, rendered):
        changed_files.append(todo_path)

    if _write_json_if_changed(todo_state_path, todo_state):
        changed_files.append(todo_state_path)

    if _write_focus_snapshot(
        repo_root, stage=current_stage, open_tasks=open_tasks, now=now
    ):
        changed_files.append(repo_root / ".autolab" / "todo_focus.json")

    message = f"todo_sync open={len(open_tasks)} removed={removed_count}"
    return TodoSyncResult(
        changed_files=changed_files,
        open_count=len(open_tasks),
        removed_count=removed_count,
        message=message,
    )


def sync_todo_pre_run(
    repo_root: Path,
    state: dict[str, Any] | None,
    *,
    host_mode: str | None = None,
) -> TodoSyncResult:
    return _sync_internal(
        repo_root=repo_root, state=state, run_outcome=None, host_mode=host_mode
    )


def sync_todo_post_run(
    repo_root: Path,
    state: dict[str, Any] | None,
    *,
    run_outcome: dict[str, Any] | None,
) -> TodoSyncResult:
    return _sync_internal(repo_root=repo_root, state=state, run_outcome=run_outcome)


def _map_stage_to_decision(stage: str) -> str | None:
    return _decision_for_stage(stage)


def select_decision_from_todo(
    repo_root: Path, *, prioritize_implementation: bool = False
) -> str | None:
    state_path = repo_root / ".autolab" / "todo_state.json"
    todo_state = _load_todo_state(state_path)
    open_tasks = _open_tasks_sorted(todo_state)

    baseline_decision: str | None = None
    for task in open_tasks:
        stage = _normalize_stage(str(task.get("stage", "")), "design")
        decision = _map_stage_to_decision(stage)
        if decision is not None:
            baseline_decision = decision
            break

    if baseline_decision is None:
        return None
    if not prioritize_implementation:
        return baseline_decision
    if baseline_decision in {"stop", "human_review"}:
        return baseline_decision

    for task in open_tasks:
        stage = _normalize_stage(str(task.get("stage", "")), "design")
        if stage not in IMPLEMENTATION_PRIORITY_STAGES:
            continue
        decision = _map_stage_to_decision(stage)
        if decision is not None:
            return decision
    return baseline_decision


def select_open_task(
    repo_root: Path, *, prioritize_implementation: bool = False
) -> dict[str, Any] | None:
    state_path = repo_root / ".autolab" / "todo_state.json"
    todo_state = _load_todo_state(state_path)
    open_tasks = _open_tasks_sorted(todo_state)
    if not open_tasks:
        return None
    selected = open_tasks[0]
    if prioritize_implementation:
        for task in open_tasks:
            stage = _normalize_stage(str(task.get("stage", "")), "design")
            if stage in IMPLEMENTATION_PRIORITY_STAGES:
                selected = task
                break
    return {
        "task_id": str(selected.get("task_id", "")),
        "source": str(selected.get("source", "")),
        "stage": _normalize_stage(str(selected.get("stage", "")), "design"),
        "task_class": str(selected.get("task_class", "unknown")),
        "text": str(selected.get("text", "")),
    }


def list_open_tasks(repo_root: Path) -> list[dict[str, Any]]:
    state_path = repo_root / ".autolab" / "todo_state.json"
    todo_state = _load_todo_state(state_path)
    open_tasks = _open_tasks_sorted(todo_state)
    return [
        {
            "task_id": str(task.get("task_id", "")),
            "source": str(task.get("source", "")),
            "stage": _normalize_stage(str(task.get("stage", "")), "design"),
            "task_class": str(task.get("task_class", "unknown")),
            "text": str(task.get("text", "")),
            "priority": str(task.get("priority", "")),
            "owner": str(task.get("owner", "")),
            "labels": list(task.get("labels", []))
            if isinstance(task.get("labels"), list)
            else [],
        }
        for task in open_tasks
    ]


def _set_task_status(repo_root: Path, task_id: str, *, status: str) -> bool:
    normalized_id = _normalize_space(task_id)
    normalized_status = _normalize_space(status).lower()
    if not normalized_id or normalized_status not in {"completed", "removed"}:
        return False
    todo_state_path = repo_root / ".autolab" / "todo_state.json"
    todo_path = repo_root / "docs" / "todo.md"
    todo_state = _load_todo_state(todo_state_path)
    tasks = todo_state.get("tasks", {})
    if not isinstance(tasks, dict):
        return False
    task = tasks.get(normalized_id)
    if not isinstance(task, dict):
        return False
    now = _utc_now()
    current_status = _normalize_space(str(task.get("status", ""))).lower()
    if current_status != "open":
        return False
    if normalized_status == "completed":
        changed = _mark_completed(task, now)
        if not changed:
            return False
    else:
        task["status"] = normalized_status
        task["last_evidence_at"] = now
    _prune_non_open_tasks(todo_state)
    _write_json_if_changed(todo_state_path, todo_state)
    todo_text = (
        todo_path.read_text(encoding="utf-8")
        if todo_path.exists()
        else _default_todo_content()
    )
    _, notes_lines, _ = _extract_sections(todo_text)
    open_tasks = _open_tasks_sorted(todo_state)
    rendered = _render_todo(open_tasks, notes_lines)
    _write_text_if_changed(todo_path, rendered)
    return True


def mark_task_completed(repo_root: Path, task_id: str) -> bool:
    return _set_task_status(repo_root, task_id, status="completed")


def mark_task_removed(repo_root: Path, task_id: str) -> bool:
    return _set_task_status(repo_root, task_id, status="removed")


def build_focus_tasks(
    repo_root: Path, stage: str, limit: int = 5
) -> list[dict[str, Any]]:
    state_path = repo_root / ".autolab" / "todo_state.json"
    todo_state = _load_todo_state(state_path)
    open_tasks = _open_tasks_sorted(todo_state)
    target_stage = _normalize_stage(stage, "hypothesis")

    focus = [
        {
            "task_id": task.get("task_id", ""),
            "stage": task.get("stage", ""),
            "source": task.get("source", ""),
            "text": task.get("text", ""),
        }
        for task in open_tasks
        if str(task.get("stage", "")) == target_stage
    ]

    if not focus:
        focus = [
            {
                "task_id": task.get("task_id", ""),
                "stage": task.get("stage", ""),
                "source": task.get("source", ""),
                "text": task.get("text", ""),
            }
            for task in open_tasks[:limit]
        ]

    return focus[:limit]
