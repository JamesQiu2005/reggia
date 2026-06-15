# PyInstaller spec for the bundled Reggia backend.
#
# Build from `desktop/` with:
#   uv run pyinstaller scripts/reggia_launcher.spec --noconfirm
#
# Output: dist/reggia-backend/  (one-folder bundle)
#         renamed + copied into src-tauri/binaries/ by build.sh.
#
# Note: this spec file is read as Python by PyInstaller and runs from the CWD
# the `pyinstaller` command is invoked in. We expect that to be `desktop/`.

from pathlib import Path

# `Path.cwd()` is `desktop/` when invoked via build.sh.
DESKTOP_DIR = Path.cwd()
REPO_DIR = DESKTOP_DIR.parent
BACKEND_PATCHED = DESKTOP_DIR / "build" / "backend"
LAUNCHER = DESKTOP_DIR / "src-launcher" / "reggia_launcher.py"
FRONTEND = REPO_DIR / "frontend"

block_cipher = None


a = Analysis(
    [str(LAUNCHER)],
    pathex=[
        str(BACKEND_PATCHED.parent),  # so `import backend.main` resolves
        str(DESKTOP_DIR / "src-launcher"),
    ],
    binaries=[],
    datas=[
        (str(FRONTEND), "frontend"),
        (str(BACKEND_PATCHED / "chat_config.json"), "backend"),
    ],
    hiddenimports=[
        # uvicorn internals — explicit because PyInstaller's importer misses
        # dynamic imports that uvicorn does at startup.
        "uvicorn.lifespan.on",
        "uvicorn.lifespan.off",
        "uvicorn.protocols.http.h11_impl",
        "uvicorn.protocols.http.auto",
        "uvicorn.protocols.websockets.auto",
        "uvicorn.protocols.websockets.wsproto_impl",
        "uvicorn.loops.auto",
        "uvicorn.loops.asyncio",
        # stdlib + third-party the backend imports
        "httpx",
        "dotenv",
        "sqlite3",
        # backend modules (defensive — PyInstaller usually picks these up
        # via the `from backend.main import app` chain, but listing them
        # makes failures louder if the path changes)
        "backend",
        "backend.main",
        "backend.sessions",
        "backend.db",
        "backend.longterm_db",
        "backend.longterm_db_patch",
        "backend.sync",
        "backend.notion_markdown",
        "backend.prompts",
        "backend.config",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # uvicorn[standard] extras that frozen apps don't like.
        "uvloop",
        "httptools",
        "watchfiles",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="reggia-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,  # keep stdout/stderr so Tauri can capture logs
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="reggia-backend",
)
