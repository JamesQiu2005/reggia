import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
CHAT_WORKSPACE = BASE_DIR / "chat_workspace"

with open(BASE_DIR / "chat_config.json") as f:
    CHAT_CONFIG = json.load(f)

# "docker" = run CC via `docker exec -i reggia-cc` (isolated)
# "local"  = run CC directly as subprocess (legacy, less isolated)
CC_MODE = os.environ.get("CC_MODE", "docker")
