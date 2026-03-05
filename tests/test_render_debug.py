from __future__ import annotations

from pathlib import Path

import pytest

from autolab.models import RenderedPromptBundle
from autolab.render_debug import build_render_stats, build_render_stats_report


def _make_bundle(
    tmp_path: Path,
    *,
    stage: str,
    runner_template_text: str,
    runner_text: str,
    context_payload: dict,
    template_relpath: str | None = None,
    shared_files: dict[str, str] | None = None,
) -> RenderedPromptBundle:
    prompts_dir = tmp_path / ".autolab" / "prompts"
    shared_dir = prompts_dir / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)

    template_path = prompts_dir / (
        template_relpath if template_relpath else f"stage_{stage}.runner.md"
    )
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(runner_template_text, encoding="utf-8")

    (shared_dir / "runtime_context.md").write_text(
        "runtime context\n", encoding="utf-8"
    )
    (shared_dir / "status_vocabulary.md").write_text(
        "status vocabulary\n", encoding="utf-8"
    )
    for filename, contents in (shared_files or {}).items():
        shared_path = shared_dir / filename
        shared_path.parent.mkdir(parents=True, exist_ok=True)
        shared_path.write_text(contents, encoding="utf-8")

    audit_template_path = prompts_dir / f"stage_{stage}.audit.md"
    brief_template_path = prompts_dir / f"stage_{stage}.brief.md"
    human_template_path = prompts_dir / f"stage_{stage}.human.md"
    audit_template_path.write_text("## ROLE\nAudit\n", encoding="utf-8")
    brief_template_path.write_text("## SUMMARY\nBrief\n", encoding="utf-8")
    human_template_path.write_text(
        "## ROLE\nHuman\n## SUMMARY\nSummary\n", encoding="utf-8"
    )

    rendered_dir = tmp_path / ".autolab" / "prompts" / "rendered"
    rendered_dir.mkdir(parents=True, exist_ok=True)

    return RenderedPromptBundle(
        template_path=template_path,
        rendered_path=rendered_dir / f"{stage}.runner.md",
        context_path=rendered_dir / f"{stage}.context.json",
        prompt_text=runner_text,
        context_payload=context_payload,
        audit_template_path=audit_template_path,
        audit_path=rendered_dir / f"{stage}.audit.md",
        brief_template_path=brief_template_path,
        brief_path=rendered_dir / f"{stage}.brief.md",
        human_template_path=human_template_path,
        human_path=rendered_dir / f"{stage}.human.md",
        audit_text="## ROLE\nAudit\n",
        brief_text="## SUMMARY\nBrief\n",
        human_text="## ROLE\nHuman\n## SUMMARY\nSummary\n",
    )


def test_render_debug_reports_warnings_and_dropped_sections(tmp_path: Path) -> None:
    large_json_block = "\n".join([f'  "k{i}": {i},' for i in range(30)])
    runner_text = (
        "# Stage: design (runner)\n\n"
        "## ROLE\n"
        "first role\n\n"
        "## ROLE\n"
        "duplicate role\n\n"
        "## VERIFICATION RITUAL\n"
        "This should be dropped in runner diagnostics.\n\n"
        "```json\n"
        "{\n"
        f"{large_json_block}\n"
        '  "end": 1\n'
        "}\n"
        "```\n\n"
        "result: unknown\n"
        "unavailable: verifier_outputs\n"
    )
    runner_template_text = "{{shared:runtime_context.md}}\n"

    bundle = _make_bundle(
        tmp_path,
        stage="design",
        runner_template_text=runner_template_text,
        runner_text=runner_text,
        context_payload={"stage": "design", "small": "ok"},
    )

    stats = build_render_stats(stage="design", bundle=bundle, views=["runner"])
    assert len(stats) == 1
    runner = stats[0]

    assert runner.view == "runner"
    assert runner.line_count > 0
    assert runner.token_estimate > 0
    assert runner.largest_sections

    assert any(
        item.startswith("duplicate heading: ## ROLE")
        for item in runner.dropped_sections
    )
    assert any(
        item.startswith("banned section: ## VERIFICATION RITUAL")
        for item in runner.dropped_sections
    )

    duplicate_headers = runner.warnings["duplicate_headers"]
    assert "## ROLE" in duplicate_headers

    unknown_leaks = runner.warnings["unknown_unavailable_leaks"]
    assert "contains 'unavailable:' marker" in unknown_leaks
    assert "contains key-value unknown/none sentinel" in unknown_leaks

    raw_injection = runner.warnings["raw_json_log_injection"]
    assert raw_injection

    stage_irrelevant = runner.warnings["stage_irrelevant_includes"]
    assert "shared:runtime_context.md included in runner template" in stage_irrelevant


def test_render_debug_allows_status_vocabulary_for_mutator_stage(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(
        tmp_path,
        stage="launch",
        runner_template_text="{{shared:status_vocabulary.md}}\n",
        runner_text="# Stage\n\n## STATUS VOCABULARY\nAllowed for launch\n",
        context_payload={"stage": "launch"},
    )

    stats = build_render_stats(stage="launch", bundle=bundle, views=["runner"])
    runner = stats[0]
    assert runner.warnings["stage_irrelevant_includes"] == ()
    assert "banned section: ## STATUS VOCABULARY" not in runner.dropped_sections


def test_render_debug_warns_status_vocabulary_for_non_mutator_stage(
    tmp_path: Path,
) -> None:
    bundle = _make_bundle(
        tmp_path,
        stage="design",
        runner_template_text="{{shared:status_vocabulary.md}}\n",
        runner_text="# Stage\n\n## STATUS VOCABULARY\nNot allowed for design\n",
        context_payload={"stage": "design"},
    )

    stats = build_render_stats(stage="design", bundle=bundle, views=["runner"])
    runner = stats[0]
    assert (
        "shared:status_vocabulary.md not allowed for stage 'design'"
        in runner.warnings["stage_irrelevant_includes"]
    )
    assert "banned section: ## STATUS VOCABULARY" in runner.dropped_sections


def test_render_debug_detects_nested_shared_include_closure(tmp_path: Path) -> None:
    bundle = _make_bundle(
        tmp_path,
        stage="design",
        template_relpath="runner/stage_design.runner.md",
        runner_template_text="{{shared:entry.md}}\n",
        runner_text="# Stage\n\n## ROLE\nDesign\n",
        context_payload={"stage": "design"},
        shared_files={
            "entry.md": "{{shared:nested.md}}\n",
            "nested.md": "{{shared:runtime_context.md}}\n",
        },
    )

    stats = build_render_stats(stage="design", bundle=bundle, views=["runner"])
    runner = stats[0]
    assert (
        "shared:runtime_context.md included in runner template"
        in runner.warnings["stage_irrelevant_includes"]
    )


def test_render_debug_ignores_fenced_code_headings(tmp_path: Path) -> None:
    runner_text = (
        "# Stage\n\n"
        "## ROLE\n"
        "Outside heading.\n\n"
        "```markdown\n"
        "## ROLE\n"
        "## VERIFICATION RITUAL\n"
        "```\n\n"
        "## SUMMARY\n"
        "Outside summary.\n"
    )
    bundle = _make_bundle(
        tmp_path,
        stage="design",
        runner_template_text="# no includes\n",
        runner_text=runner_text,
        context_payload={"stage": "design"},
    )

    stats = build_render_stats(stage="design", bundle=bundle, views=["runner"])
    runner = stats[0]

    assert runner.warnings["duplicate_headers"] == ()
    assert "banned section: ## VERIFICATION RITUAL" not in runner.dropped_sections
    section_names = {section_name for section_name, _count in runner.largest_sections}
    assert "## VERIFICATION RITUAL" not in section_names


def test_render_debug_context_stats_report_best_effort_for_weird_values(
    tmp_path: Path,
) -> None:
    class _Unrepr:
        def __repr__(self) -> str:
            raise RuntimeError("repr exploded")

    recursive: dict[str, object] = {}
    recursive["self"] = recursive
    bundle = _make_bundle(
        tmp_path,
        stage="design",
        runner_template_text="# no includes\n",
        runner_text="# Stage\n\n## ROLE\nDesign\n",
        context_payload={
            1: {"set": {1, 2}, ("tuple",): Path("relative/path.txt")},
            ("k", 2): _Unrepr(),
            "recursive": recursive,
        },
    )

    stats = build_render_stats(stage="design", bundle=bundle, views=["context"])
    context_stats = stats[0]
    report = build_render_stats_report(stage="design", bundle=bundle, views=["context"])

    assert context_stats.line_count > 0
    assert context_stats.largest_sections
    assert "[context]" in report
    assert "recursive" in report


def test_render_debug_view_normalization_dedup_and_unsupported(tmp_path: Path) -> None:
    bundle = _make_bundle(
        tmp_path,
        stage="design",
        runner_template_text="# no includes\n",
        runner_text="# Stage\n\n## ROLE\nDesign\n",
        context_payload={"stage": "design"},
    )

    stats = build_render_stats(
        stage="design",
        bundle=bundle,
        views=[" Runner ", "RUNNER", "context", " ", "Context"],
    )
    assert [item.view for item in stats] == ["runner", "context"]

    with pytest.raises(ValueError, match="unsupported view 'invalid'"):
        build_render_stats(stage="design", bundle=bundle, views=["invalid"])


def test_render_debug_token_estimate_counts_whitespace(tmp_path: Path) -> None:
    bundle = _make_bundle(
        tmp_path,
        stage="design",
        runner_template_text="# no includes\n",
        runner_text="    \n  ",
        context_payload={"stage": "design"},
    )

    stats = build_render_stats(stage="design", bundle=bundle, views=["runner"])
    assert stats[0].token_estimate > 0


def test_render_debug_report_includes_context_sections(tmp_path: Path) -> None:
    bundle = _make_bundle(
        tmp_path,
        stage="design",
        runner_template_text="# no includes\n",
        runner_text="# Stage\n\n## ROLE\nDesign\n",
        context_payload={
            "short": "x",
            "long": {"items": list(range(20))},
        },
    )

    report = build_render_stats_report(stage="design", bundle=bundle, views=["context"])

    assert "autolab render stats" in report
    assert "[context]" in report
    assert "largest_sections:" in report
    assert "- long:" in report
