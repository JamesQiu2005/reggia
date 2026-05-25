import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CHAT_WORKSPACE = BASE_DIR / "chat_workspace"

with open(BASE_DIR / "chat_config.json") as f:
    CHAT_CONFIG = json.load(f)
