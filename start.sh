#!/bin/bash
set -e

cd "$(dirname "$0")"

# Load DEEPSEEK_API_KEY from backend/.env so docker-compose can use ${DEEPSEEK_API_KEY}
if [ -f backend/.env ]; then
  set -a
  source backend/.env
  set +a
fi

# Try to start the CC (Claude Code) container — skip if Docker is unavailable
# or the image can't be pulled (e.g. behind a firewall / proxy).
if command -v docker &>/dev/null && docker info &>/dev/null; then
  echo "=== Building/starting CC container (Claude Code in Docker) ==="
  docker compose build 2>&1 || echo "⚠️  docker compose build failed — skipping CC container"
  docker compose up -d 2>&1 || echo "⚠️  docker compose up failed — skipping CC container"
else
  echo "⚠️  Docker not available — skipping CC container"
fi

echo ""
echo "=== Starting backend ==="
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
