#!/usr/bin/env bash
set -euo pipefail

PYTHON_BIN="${PYTHON_BIN:-python3}"
MDFORMAT_BIN="${MDFORMAT_BIN:-mdformat}"
YAMLFIX_BIN="${YAMLFIX_BIN:-yamlfix}"

# Python formatting (Ruff).
"${PYTHON_BIN}" -m ruff format --check .

# Markdown formatting.
md_files="$(rg --files -g "*.md")"
if [ -n "${md_files}" ]; then
  printf "%s\n" "${md_files}" | xargs "${MDFORMAT_BIN}" --check
fi

# YAML formatting (CI/workflow files).
yaml_files="$(rg --files -uu -g ".github/workflows/*.yml")"
if [ -n "${yaml_files}" ]; then
  printf "%s\n" "${yaml_files}" | xargs "${YAMLFIX_BIN}" --check
fi
