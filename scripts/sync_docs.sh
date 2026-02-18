#!/usr/bin/env bash
set -o errexit

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

cp "$REPO_ROOT/src/autolab/skills/codex/autolab/SKILL.md" \
   "$REPO_ROOT/docs/skills/autolab/SKILL.md"

echo "docs/skills/autolab/SKILL.md synced successfully."
