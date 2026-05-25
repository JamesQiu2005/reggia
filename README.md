# Reggia

Personal AI chat frontend backed by a Notion knowledge base. A FastAPI backend orchestrates between a browser UI and Claude Code subprocesses, with Notion as the long-term memory and task tracking layer.

## Architecture

```
Browser (two-pane UI)
  │
  ├─ Chat pane ── SSE stream ── FastAPI backend ── Claude Code subprocess (DeepSeek)
  │                                │
  └─ Reggia panel (CRUD) ─────────┘
                                   │
                              Notion API
```

- **Chat CC** runs in an isolated `chat_workspace/` with restricted tool permissions
- **Backend** is the single Notion gateway — the chat CC has no API key, it calls the backend via `curl localhost:8000`
- **Reggia panel** provides CRUD for Notion active items directly from the UI

## Prerequisites

- Python 3.12+ with [uv](https://docs.astral.sh/uv/)
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated (`claude` in PATH)
- A Notion account with an integration

## Setup

```bash
git clone https://github.com/JamesQiu2005/reggia.git
cd reggia
uv sync
```

### Notion API key

1. Go to [Notion Integrations](https://www.notion.so/profile/integrations) → **New integration**
2. Give it a name (e.g. "Reggia"), select your workspace
3. Copy the **Internal Integration Secret** — it starts with `ntn_`
4. Go to the Notion pages and databases you want Reggia to access. For each one, click **⋯ → Connections →** add your new integration
5. Create a `.env` file:

```bash
echo 'NOTION_API_KEY=ntn_your_key_here' > backend/.env
```

The backend reads `backend/.env` on startup. This file is gitignored.

## Running

```bash
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Open `http://localhost:8000` in your browser.

## Project structure

```
Reggia/
├── backend/
│   ├── main.py              # FastAPI app, Reggia CRUD, longterm pages
│   ├── sessions.py           # Session management, CC subprocess, SSE streaming
│   ├── db.py                 # SQLite schema, message/session persistence
│   ├── prompts.py            # Cache-optimized prompt builder
│   ├── config.py             # Shared config
│   ├── chat_config.json      # Model list + defaults
│   └── chat_workspace/       # Isolated cwd for chat CC subprocesses
│       ├── CLAUDE.md         # Chat persona + tool constraints
│       └── .claude/
│           ├── settings.json # Restricted tool permissions
│           └── skills/
│               └── reggia.md # Backend endpoint reference
├── frontend/
│   ├── index.html            # Two-pane layout
│   ├── app.js                # Chat SSE, session mgmt, Reggia CRUD
│   └── styles.css            # All styles
├── skills/
│   └── reggia_notion.md      # Full Notion API reference (for orchestration)
└── pyproject.toml
```
