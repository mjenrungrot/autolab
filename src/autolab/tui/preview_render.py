from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

PreviewRenderHint = Literal["auto", "markdown", "json", "yaml", "toml", "log", "text"]

_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".mdown", ".mkd"})
_LANGUAGE_BY_SUFFIX: dict[str, str] = {
    ".json": "json",
    ".jsonl": "json",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".log": "text",
    ".txt": "text",
}

_LANGUAGE_BY_HINT: dict[PreviewRenderHint, str] = {
    "auto": "text",
    "markdown": "text",
    "json": "json",
    "yaml": "yaml",
    "toml": "toml",
    "log": "text",
    "text": "text",
}


def _longest_backtick_run(text: str) -> int:
    runs = re.findall(r"`+", text)
    if not runs:
        return 0
    return max(len(run) for run in runs)


def _fence_block(content: str, language: str) -> str:
    body = content if content else "(empty)"
    fence = "`" * max(3, _longest_backtick_run(body) + 1)
    info_string = language.strip()
    if info_string:
        return f"{fence}{info_string}\n{body}\n{fence}"
    return f"{fence}\n{body}\n{fence}"


def _language_for_content(
    *,
    source_path: Path | None,
    hint: PreviewRenderHint,
) -> str:
    if hint != "auto":
        return _LANGUAGE_BY_HINT[hint]
    if source_path is None:
        return "text"
    return _LANGUAGE_BY_SUFFIX.get(source_path.suffix.lower(), "text")


def _is_markdown_content(*, source_path: Path | None, hint: PreviewRenderHint) -> bool:
    if hint == "markdown":
        return True
    if hint != "auto" or source_path is None:
        return False
    return source_path.suffix.lower() in _MARKDOWN_SUFFIXES


def build_preview_markdown(
    text: str,
    *,
    source_path: Path | None = None,
    hint: PreviewRenderHint = "auto",
) -> str:
    """Convert preview text into markdown suitable for Textual Markdown widgets."""
    if not text:
        language = _language_for_content(source_path=source_path, hint=hint)
        return _fence_block("", language)
    if _is_markdown_content(source_path=source_path, hint=hint):
        return text
    language = _language_for_content(source_path=source_path, hint=hint)
    return _fence_block(text, language)
