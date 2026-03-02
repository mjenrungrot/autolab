from __future__ import annotations

from pathlib import Path

from autolab.tui.preview_render import build_preview_markdown


def test_preview_render_markdown_hint_passthrough() -> None:
    text = "# Title\n\n- item"
    rendered = build_preview_markdown(text, hint="markdown")
    assert rendered == text


def test_preview_render_auto_markdown_by_path_suffix() -> None:
    text = "## Stage Prompt\n\nUse this."
    rendered = build_preview_markdown(text, source_path=Path("stage_design.md"))
    assert rendered == text


def test_preview_render_json_hint_uses_fenced_json() -> None:
    text = '{\n  "a": 1\n}'
    rendered = build_preview_markdown(text, hint="json")
    assert rendered.startswith("```json\n")
    assert rendered.endswith("\n```")
    assert text in rendered


def test_preview_render_auto_yaml_by_path_suffix() -> None:
    text = "key: value\n"
    rendered = build_preview_markdown(text, source_path=Path("design.yaml"))
    assert rendered.startswith("```yaml\n")
    assert rendered.endswith("\n```")
    assert text in rendered


def test_preview_render_escapes_embedded_backticks_by_extending_fence() -> None:
    text = "line one\n````\nline two"
    rendered = build_preview_markdown(text, hint="text")
    assert rendered.startswith("`````text\n")
    assert rendered.endswith("\n`````")
    assert text in rendered


def test_preview_render_empty_content_is_fenced_placeholder() -> None:
    rendered = build_preview_markdown("", hint="markdown")
    assert rendered == "```text\n(empty)\n```"
