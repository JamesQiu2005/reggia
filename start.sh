#!/bin/bash
set -e

cd "$(dirname "$0")"

# Load DEEPSEEK_API_KEY from backend/.env so docker-compose can use ${DEEPSEEK_API_KEY}
if [ -f backend/.env ]; then
  set -a
  source backend/.env
  set +a
fi

echo "=== Building Docker image (if needed) ==="
docker compose build

echo ""
echo "=== Starting CC container ==="
docker compose up -d

echo ""
echo "=== Starting backend ==="
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
