#!/bin/bash
cd "$(dirname "$0")"

echo "Stopping Reggia..."
docker compose down 2>/dev/null || true
colima stop 2>/dev/null || true
echo "All stopped."
