# Reggia

Personal chat frontend + knowledge base. Single-user, local-first.

## Architecture

```
frontend/          Static SPA (vanilla HTML/CSS/JS, no framework)
  index.html       Chat pane, account settings, welcome modal, Reggia panel
  app.js           Client-side logic
  styles.css       Dark theme — no framework

backend/           FastAPI server on :8000
  main.py          App entry + route mounting
  settings.py      /settings API: .env management, avatar upload, workspace templating
  config.py        CC_MODE: "docker" or "local"
  chat_workspace/  Claude Code agent workspace (mounted into Docker container)
    CLAUDE.md      Rendered from CLAUDE.md.template via {USER_NAME}
    .claude/       Skills, settings for the CC agent
```

## Key pieces already built

| Feature | Location | Status |
|---|---|---|
| Account settings page | `frontend/index.html` (sidebar button + settings pane) | ✅ Done |
| Welcome/onboarding flow | `frontend/index.html` (2-step modal: profile → API keys) | ✅ Done |
| Settings API (CRUD .env) | `backend/settings.py` | ✅ Done |
| Avatar upload (base64) | `backend/settings.py` | ✅ Done |
| API key masking + reveal | `backend/settings.py` + frontend eye-toggle | ✅ Done |
| Workspace CLAUDE.md templating | `backend/settings.py` → `chat_workspace/CLAUDE.md.template` | ✅ Done |
| Docker CC container | `Dockerfile` + `docker-compose.yml` | See start.sh |

## Startup

```bash
./start.sh    # Backend on :8000; tries Docker CC container, skips gracefully on failure
```

The script tolerates Docker Hub being unreachable (e.g. behind the Great Firewall) — it prints a warning and continues to the backend.

## Chat workspace (backend/chat_workspace)

When `CC_MODE=local` (in `config.py`), the backend spawns Claude Code as a subprocess.
When `CC_MODE=docker` (default), the backend talks to the `regria-cc` container.

`settings.py::render_chat_workspace()` materialises `CLAUDE.md` and skills from `.template` files, replacing `{USER_NAME}` with the current env value. Called on every boot and whenever the user name changes.