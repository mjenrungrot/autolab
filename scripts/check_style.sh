#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
MDFORMAT_BIN="${MDFORMAT_BIN:-mdformat}"
YAMLFIX_BIN="${YAMLFIX_BIN:-yamlfix}"

# Python formatting (Ruff).
"${PYTHON_BIN}" -m ruff format --check .
# Keep lint check focused on high-signal correctness categories until
# the full codebase is cleaned for strict all-rule linting.
"${PYTHON_BIN}" -m ruff check . --select E9,F63,F7,F82

# Markdown formatting.
md_files="$(git ls-files '*.md' | grep -Ev '(^|/)\.' || true)"
if [ -n "${md_files}" ]; then
  printf '%s\n' "${md_files}" | xargs "${MDFORMAT_BIN}" --check
fi

# YAML formatting (CI/workflow files).
yaml_files="$(git ls-files '.github/workflows/*.yml' '.github/workflows/*.yaml')"
if [ -n "${yaml_files}" ]; then
  printf '%s\n' "${yaml_files}" | xargs "${YAMLFIX_BIN}" --check
fi
