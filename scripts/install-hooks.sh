#!/usr/bin/env sh
set -eu

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

chmod +x .githooks/pre-commit .githooks/post-commit
chmod +x scripts/sync_release_tags.py
git config core.hooksPath .githooks

echo "Installed Git hooks: core.hooksPath=.githooks"
echo "Active hooks:"
echo "- .githooks/pre-commit (auto-bumps pyproject + syncs README tag)"
echo "- .githooks/post-commit (pushes current release tag and keeps last 10 tags)"
