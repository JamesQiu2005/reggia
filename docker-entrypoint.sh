#!/bin/sh
set -e

# Substitute host-specific values for the Docker environment.
# The container reaches the host backend at host.docker.internal:8000
# instead of localhost:8000.

for f in /workspace/CLAUDE.md /workspace/.claude/skills/reggia.md; do
  if [ -f "$f" ]; then
    sed -i 's|localhost:8000|host.docker.internal:8000|g' "$f"
  fi
done

# Fix settings.json: update Bash permission and Read paths
SETTINGS="/workspace/.claude/settings.json"
if [ -f "$SETTINGS" ]; then
  # Change curl permission from localhost to host.docker.internal
  sed -i 's|\*localhost\*|*host.docker.internal*|g' "$SETTINGS"
  # Replace host-specific Read paths with /workspace/**
  sed -i 's|Read([^)]*chat_workspace[^)]*)|Read(/workspace/**)|g' "$SETTINGS"
fi

exec "$@"
