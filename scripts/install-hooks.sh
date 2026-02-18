#!/usr/bin/env sh
set -eu

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

chmod +x .githooks/pre-commit
git config core.hooksPath .githooks

echo "Installed Git hooks: core.hooksPath=.githooks"
echo "Active hook: .githooks/pre-commit (auto-bumps pyproject.toml version)."
