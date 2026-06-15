import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CHAT_WORKSPACE = BASE_DIR / "chat_workspace"

with open(BASE_DIR / "chat_config.json") as f:
    CHAT_CONFIG = json.load(f)

# Which chat orchestrator handles a turn:
#   "agent"  = in-process DeepSeek tool-calling loop (agent_loop.py) — default.
#              Lightweight; no Docker or Claude Code required.
#   "docker" = legacy Claude Code container via `docker exec -i reggia-cc`.
CHAT_ENGINE = os.environ.get("CHAT_ENGINE", "agent")
