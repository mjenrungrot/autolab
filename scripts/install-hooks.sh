#!/usr/bin/env sh
set -eu

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

chmod +x .githooks/pre-commit .githooks/post-commit
chmod +x scripts/sync_release_tags.py
chmod +x scripts/check_style.sh
chmod +x scripts/changelog.py
git config core.hooksPath .githooks

echo "Installed Git hooks: core.hooksPath=.githooks"
echo "Active hooks:"
echo "- .githooks/pre-commit (staged-file formatting + repo style check + default-branch-only version bump + strict CHANGELOG validation + README version freshness check + README tag sync)"
echo "- .githooks/post-commit (default-branch-only release tag sync; pruning disabled by default)"
echo "Set AUTOLAB_DISABLE_VERSION_BUMP=1 to skip both hooks without uninstalling."
