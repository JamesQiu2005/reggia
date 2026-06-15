# Reggia desktop wrapper

Lightweight Tauri shell that wraps the Reggia backend for pilot distribution. Produces a `.dmg` for macOS and an `.msi` for Windows. Pilot users install no Python or Reggia source — they only need Docker Desktop.

The wrapper is fully isolated under this directory. The repo's `backend/`, `frontend/`, `docker-compose.yml`, `Dockerfile`, `start.sh`, and `pyproject.toml` are never modified. The dev workflow `./start.sh` continues to work unchanged.

## How it works

1. **`scripts/prepare_backend.py`** copies `../backend/` into `build/backend_patched/` and applies two AST-level patches via `libcst`:
   - `longterm_db.py`: the hardcoded `SEED_PAGES` dict is replaced with `from .longterm_db_patch import SEED_PAGES` (the patch module reads `NOTION_PAGE_{WORK,RESEARCH,INTELLECTUAL,PERSONAL,INDEX}` from env at import time).
   - `main.py`: `StaticFiles(directory="frontend", ...)` becomes `directory=os.environ.get("REGGIA_FRONTEND_DIR", "frontend")` so the bundled frontend can be located via env var.

   It also writes a bundled `docker-compose.yml` (with `${REGGIA_WORKSPACE}` volume mount + `image: reggia/cc:0.1.0`) and seeds the chat workspace files for first-run install.

2. **PyInstaller** freezes `src-launcher/reggia_launcher.py` into a one-folder bundle named `reggia-backend`. The launcher sets `REGGIA_FRONTEND_DIR` and runs uvicorn on `127.0.0.1:8000`.

3. **Tauri** bundles the PyInstaller output as an external binary (`externalBin` in `tauri.conf.json`), wraps the wizard HTML, and produces the platform installer.

4. On first launch, the user sees a 6-screen wizard (Welcome → Docker check → DeepSeek key → Notion token → parent page → create pages → done). The wizard saves config to `~/Library/Application Support/com.reggia.desktop/config.json` on Mac (`%APPDATA%\com.reggia.desktop\config.json` on Windows). On subsequent launches the wizard is skipped and Tauri navigates straight to `http://127.0.0.1:8000`.

## One-time prerequisites

### macOS

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
rustup target add aarch64-apple-darwin x86_64-apple-darwin
cargo install tauri-cli --version "^1.6"
# uv is already required by the repo
```

### Windows (PowerShell, admin)

```powershell
winget install Rustlang.Rustup
rustup default stable-x86_64-pc-windows-msvc
cargo install tauri-cli --version "^1.6"
winget install astral-sh.uv
# WiX 3.x for MSI bundling:
#   https://github.com/wixtoolset/wix3/releases
# MSVC C++ build tools:
winget install Microsoft.VisualStudio.2022.BuildTools `
  --override "--quiet --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```

## Per-release: publish the Docker image

The bundled `docker-compose.yml` uses `image: reggia/cc:0.1.0` instead of `build: .`. Push the image before cutting a pilot build:

```bash
# From repo root
docker build -t reggia/cc:0.1.0 .
docker login docker.io
docker push reggia/cc:0.1.0
```

If you'd rather not depend on Docker Hub, change `prepare_backend.py` to swap in `image: ghcr.io/yourname/reggia-cc:0.1.0` and push to GHCR instead. Pilot machines will `docker compose pull` on first launch.

## Build locally

### macOS (universal2 — works on Intel + Apple Silicon)

```bash
cd desktop
./build.sh
```

Faster iteration: `./build.sh arm64` (or `./build.sh x86_64`) builds for one arch only and skips lipo.

Artifacts land at `desktop/src-tauri/target/{universal-apple-darwin,release}/bundle/dmg/Reggia_0.1.0_*.dmg`.

### Windows

```powershell
cd desktop
.\build.ps1
```

Artifact at `desktop\src-tauri\target\release\bundle\msi\Reggia_0.1.0_x64_en-US.msi`.

### Cross-platform via GitHub Actions

`.github/workflows/build.yml` runs matrix builds (macos-14 + windows-2022) on tag push (`v*`) or manual dispatch. Artifacts are uploaded per platform.

## Distributing to pilots

For each pilot user:

1. Send them the `.dmg` (Mac) or `.msi` (Windows) directly.
2. Send them this short message:

   > **Reggia pilot install**
   >
   > 1. Install Docker Desktop if you don't already have it: https://www.docker.com/products/docker-desktop/. Make sure it's running.
   > 2. Double-click the installer.
   >    - On Mac: drag Reggia to Applications. The first launch will fail with "Reggia is damaged" — right-click → Open → Open Anyway. (Apple needs $99/yr for a Developer ID; we'll skip that for pilot.)
   >    - On Windows: SmartScreen will say "Unrecognized app" → More info → Run anyway.
   > 3. Reggia walks you through a 5-minute setup: a DeepSeek API key (free signup at platform.deepseek.com), a Notion integration secret, and a Notion page to use as the parent for your memory pages.
   > 4. First launch downloads the chat-agent Docker image (~200 MB) once.

## Resetting + troubleshooting

- **Reset configuration** — Reggia → tray icon → "Reset configuration". Wipes `config.json` and reloads the wizard. Notion pages already created are not deleted (the user can re-use them by editing `config.json` manually if they re-run the wizard).
- **Stop Docker container** — tray → "Stop Reggia services". Restarts on next launch (the compose file has `restart: unless-stopped`).
- **Backend won't start** — look at terminal output if launched from a terminal; in a release build, check Console.app (Mac) or Event Viewer (Windows) for stderr from the sidecar.
- **Wizard 401 on Notion** — usually the parent page isn't shared with the integration. Open it in Notion → ··· → Connections → add Reggia.

## Known gotchas

- **PyInstaller cannot cross-compile.** Building the Windows `.exe` requires a Windows machine. Use GitHub Actions (`.github/workflows/build.yml`) or a Windows VM.
- **Universal2 Mac build runs PyInstaller twice.** Once natively (your host arch) and once under Rosetta for the other arch. If you're on Apple Silicon, install x86_64 Python via `uv python install --arch x86_64 3.12` first, then run `./build.sh`.
- **`uvicorn[standard]` extras** (`uvloop`, `httptools`, `watchfiles`) are excluded from the PyInstaller spec — they're unreliable in frozen apps. The launcher uses `loop="asyncio"` to compensate.
- **WebView2 on Windows 10** is auto-downloaded on first launch (~120 MB stub bootstrapper resolves to ~150 KB). Windows 11 has it preinstalled.
- **Notion integration must be shared with the parent page before page creation.** The wizard's parent-page validation step catches this with an explicit message.
- **Per-user `chat_workspace/`** is seeded from `resources/chat_workspace_seed/` on first save. If you change `backend/chat_workspace/CLAUDE.md` upstream, existing pilot installs do NOT pick it up automatically — they'd need to delete `~/Library/Application Support/com.reggia.desktop/chat_workspace/` and relaunch.

## Folder layout

```
desktop/
├── README.md                       (this file)
├── build.sh                        # Mac orchestrator
├── build.ps1                       # Windows orchestrator
├── .github/workflows/build.yml     # CI matrix build
├── scripts/
│   ├── prepare_backend.py          # libcst patcher
│   └── reggia_launcher.spec        # PyInstaller spec
├── src-launcher/
│   ├── reggia_launcher.py          # PyInstaller entry
│   └── longterm_db_patch.py        # env-var SEED_PAGES (copied into bundled backend)
├── src-tauri/
│   ├── Cargo.toml
│   ├── tauri.conf.json
│   ├── build.rs
│   ├── icons/                      # supply your own .icns / .ico
│   ├── binaries/                   # PyInstaller output dropped here (gitignored)
│   ├── resources/
│   │   ├── compose/docker-compose.yml
│   │   └── chat_workspace_seed/
│   └── src/
│       ├── main.rs                 # entry, tray, lifecycle
│       ├── config.rs               # user config + workspace seeding
│       ├── docker.rs               # detect + compose up/down
│       ├── sidecar.rs              # spawn + wait for backend
│       └── notion_setup.rs         # wizard's Rust commands
├── wizard/                         # first-run HTML/JS/CSS
│   ├── index.html
│   ├── wizard.js
│   └── wizard.css
└── build/                          # gitignored staging
```
