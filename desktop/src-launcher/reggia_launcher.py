"""PyInstaller entry point for the bundled Reggia backend.

Tauri spawns this binary as a sidecar with all required env vars already set:
  NOTION_API_KEY
  NOTION_PAGE_{WORK,RESEARCH,INTELLECTUAL,PERSONAL,INDEX}
  DEEPSEEK_API_KEY        (read by docker-compose, not by the backend itself)
  CC_MODE=docker
  REGGIA_FRONTEND_DIR=<path to bundled frontend/>

When frozen by PyInstaller, sys._MEIPASS points at the temp extraction dir
holding the bundled frontend/ and the patched backend/ package.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _bundle_root() -> Path:
    """Return the directory containing bundled resources (frontend/, etc.)."""
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


def _default_data_dir() -> Path:
    """Per-user writable dir for SQLite DBs and chat logs. Mirrors the location
    the Tauri wrapper writes config.json into, so the wrapper and a standalone
    binary launch land on the same data."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "com.reggia.desktop"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "com.reggia.desktop"
    return Path.home() / ".reggia"


def main() -> None:
    root = _bundle_root()
    os.environ.setdefault("REGGIA_FRONTEND_DIR", str(root / "frontend"))
    data_dir = Path(os.environ.get("REGGIA_DATA_DIR", str(_default_data_dir())))
    os.environ["REGGIA_DATA_DIR"] = str(data_dir)
    (data_dir / "databases").mkdir(parents=True, exist_ok=True)
    (data_dir / "logs").mkdir(parents=True, exist_ok=True)

    # Switch CWD to the bundle root so any other relative paths the backend
    # might use (none today, but defensive) resolve under bundled resources
    # rather than wherever the user double-clicked from.
    os.chdir(str(root))

    import uvicorn

    from backend.main import app

    uvicorn.run(
        app,
        host="127.0.0.1",
        port=8000,
        loop="asyncio",
        log_level="info",
        access_log=False,
    )


if __name__ == "__main__":
    main()
