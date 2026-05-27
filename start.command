#!/bin/bash
set -e

cd "$(dirname "$0")"

# --- Docker runtime ---
# If Docker is already functional (Docker Desktop, OrbStack, etc.), use it as-is.
# Otherwise install Colima — a lightweight VM that provides the Docker socket.
if ! docker info &>/dev/null 2>&1; then
  echo ">>> Installing Colima + Docker CLI (one-time setup)..."
  if ! command -v brew &>/dev/null; then
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv 2>/dev/null)"
  fi
  brew install colima docker docker-compose
  colima start --cpu 1 --memory 1 --disk 10
  echo ""
fi

# --- Docker CC container ---
if ! docker inspect reggia-cc &>/dev/null 2>&1; then
  echo ">>> Building CC container (one-time)..."
  docker compose build
  docker compose up -d
elif ! docker inspect -f '{{.State.Running}}' reggia-cc 2>/dev/null | grep -q true; then
  docker compose up -d
fi

# --- Python deps ---
echo ">>> Syncing Python dependencies..."
uv sync

# --- Onboarding check ---
if [ ! -f backend/.env ]; then
  echo ""
  echo "=============================================="
  echo "  Welcome to Reggia! First-time setup."
  echo "=============================================="
  echo ""
  read -rp "Your name: " USER_NAME
  read -rp "DeepSeek API Key (sk-...): " DS_KEY
  read -rp "Notion API Key (ntn-...): " NTN_KEY
  cat > backend/.env <<DOTENV
USER_NAME=${USER_NAME}
DISPLAY_NAME=${USER_NAME}
DEEPSEEK_API_KEY=${DS_KEY}
NOTION_API_KEY=${NTN_KEY}
CC_MODE=docker
DOTENV
  echo ""
  echo ".env written — you can edit it later in the Account Settings page."
  echo ""
fi

# --- Load .env so docker-compose can use ${DEEPSEEK_API_KEY} ---
set -a
source backend/.env
set +a

# --- Open browser ---
sleep 1
open http://localhost:8000

# --- Start backend (foreground — close this Terminal to stop) ---
echo ""
echo "  Reggia is running at http://localhost:8000"
echo "  Close this Terminal window to stop."
echo ""
exec uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
