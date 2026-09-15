#!/usr/bin/env bash
#
# Copy the component into the local container's config directory and restart it.
#
# There is no bind mount for the component and no hot reload: Home Assistant imports
# a custom integration into its own process, so every change needs both a copy and a
# restart. See docker-compose.yml for why it is a copy rather than a mount.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCE="$REPO_ROOT/custom_components/perific"
TARGET="$REPO_ROOT/dev/config/custom_components/perific"

mkdir -p "$(dirname "$TARGET")"
# --delete so a file removed from the source doesn't linger and keep being imported.
rsync -a --delete \
  --exclude='__pycache__' --exclude='*.pyc' \
  "$SOURCE/" "$TARGET/"

echo "Synced $(find "$TARGET" -type f | wc -l | tr -d ' ') files to dev/config/custom_components/perific"

if [ "${1:-}" = "--no-restart" ]; then
  exit 0
fi

cd "$REPO_ROOT"
docker compose restart homeassistant >/dev/null
echo "Restarted Home Assistant — http://localhost:8123"
