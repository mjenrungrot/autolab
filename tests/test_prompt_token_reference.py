from __future__ import annotations

import ast
import re
from pathlib import Path


def _load_allowed_tokens_from_prompt_lint(repo_root: Path) -> set[str]:
    prompt_lint_path = repo_root / "src" / "autolab" / "scaffold" / ".autolab" / "verifiers" / "prompt_lint.py"
    source = prompt_lint_path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Look for _FALLBACK_ALLOWED_TOKENS (static fallback set) or ALLOWED_TOKENS (legacy literal)
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ("_FALLBACK_ALLOWED_TOKENS", "ALLOWED_TOKENS"):
                try:
                    value = ast.literal_eval(node.value)
                except (ValueError, TypeError):
                    continue
                if isinstance(value, set):
                    return {str(token) for token in value}
    raise AssertionError("_FALLBACK_ALLOWED_TOKENS / ALLOWED_TOKENS not found in prompt_lint.py")


def _extract_tokens_from_markdown(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return set(re.findall(r"\{\{([A-Za-z0-9_]+)\}\}", text))


def test_prompt_token_reference_covers_allowed_tokens() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    allowed_tokens = _load_allowed_tokens_from_prompt_lint(repo_root)
    reference_doc = repo_root / "docs" / "prompt_token_reference.md"
    documented_tokens = _extract_tokens_from_markdown(reference_doc)

    missing = sorted(allowed_tokens - documented_tokens)
    extras = sorted(documented_tokens - allowed_tokens)
    assert not missing, f"docs/prompt_token_reference.md missing tokens: {missing}"
    assert not extras, f"docs/prompt_token_reference.md has unsupported tokens: {extras}"
