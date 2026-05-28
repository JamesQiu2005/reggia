# Reggia — Codebase Architecture

## Overview

Reggia is a personal AI chat frontend backed by a personal knowledge base. The user ("Hanze") maintains structured long-term context in Notion and active items in local SQLite; the app provides a two-pane UI — chat on the left, knowledge base CRUD on the right — with a FastAPI backend orchestrating between the frontend, a Docker-contained Claude Code subprocess (connected directly to DeepSeek), and the Notion API.

## Directory structure

```
Reggia/
├── CLAUDE.md                       # Orchestration agent instructions (this CLI session)
├── agent.md                        # This file — architecture reference
├── per_session_control.md          # Session management spec
├── pyproject.toml                  # UV project (fastapi, uvicorn, httpx)
├── uv.lock
├── Dockerfile                      # CC container image (node:24-alpine + claude-code)
├── docker-compose.yml              # reggia-cc service definition
├── docker-entrypoint.sh            # URL/path substitution for Docker environment
├── start.sh                        # One-command startup (build + up + uvicorn)
├── .env.example                    # Required env vars template
│
├── backend/
│   ├── main.py                     # FastAPI app, all route registration
│   ├── config.py                   # Shared config, CC_MODE flag (docker/local)
│   ├── db.py                       # SQLite: sessions, messages, items — schema + CRUD
│   ├── longterm_db.py              # SQLite: long-term memory cache + block passthrough store
│   ├── sync.py                     # Notion sync: pull, push, append, resolve
│   ├── notion_markdown.py          # Notion blocks <-> Markdown: paragraphs, headings, lists, quotes, code, tables
│   ├── test_notion_markdown.py      # Unit tests for the converter (table rendering, inline marks)
│   ├── prompts.py                  # Cache-optimized prompt builder, title prompt
│   ├── sessions.py                 # /sessions CRUD, /sessions/{id}/chat, Docker exec wrapper
│   ├── chat_config.json            # Model list + defaults (deepseek-v4-pro[1m], deepseek-v4-flash)
│   ├── test_headless_chat.py       # Integration test for chat CC
│   ├── test_notion_markdown.py      # Unit tests for Notion ↔ Markdown converter
│   ├── chat_workspace/             # Mounted into Docker container as /workspace
│   │   ├── CLAUDE.md               # Chat persona + Reggia query instructions
│   │   └── .claude/
│   │       ├── settings.json       # Permissions: Read(/workspace/**), Bash(curl *host.docker.internal*)
│   │       └── skills/
│   │           └── reggia.md       # Condensed skill: backend endpoints, routing, sensitivity
│   ├── databases/                  # SQLite files (WAL mode)
│   │   ├── reggia_session.db       # Sessions + messages
│   │   ├── reggia_items.db         # Active items (local, not Notion)
│   │   └── reggia_longterm.db      # Long-term memory cache + block passthrough
│   ├── logs/                       # Per-session debug logs (chat_{session_id}.jsonl)
│   └── .env                        # NOTION_API_KEY + DEEPSEEK_API_KEY (gitignored)
│
├── frontend/
│   ├── index.html                  # Two-pane layout: chat + Reggia panel
│   ├── styles.css                  # Design tokens, all component styles
│   └── app.js                      # Chat SSE, session mgmt, Reggia panel CRUD
│
├── skills/
│   └── reggia_notion.md            # Full Notion API reference (used by orchestration CC)
│
└── template/                       # UI mockups (reference only, not served)
    ├── reggia_frontend_mockup.html
    ├── reggia_active_panel_crud.html
    ├── reggia_create_item_form.html
    └── template_specific.md
```

## Two Claude Code instances

The project involves **two separate CC contexts**:

| | Orchestration CC (this session) | Chat CC (Docker container) |
|---|---|---|
| **Runtime** | Host machine | Docker `reggia-cc` container |
| **cwd** | `Reggia/` (project root) | `/workspace/` (volume-mount from `chat_workspace/`) |
| **CLAUDE.md** | Full orchestration instructions | Chat persona + Reggia query rules |
| **Skills** | `skills/reggia_notion.md` | `.claude/skills/reggia.md` (condensed) |
| **Notion access** | Direct Notion API (has key) | Via backend REST endpoints only |
| **API routing** | OAuth to Anthropic | `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` (direct to DeepSeek) |
| **Purpose** | Code editing, architecture, system control | User-facing chat |

The chat CC is spawned per-request from the backend via `docker exec`:
```
docker exec -i reggia-cc claude --output-format stream-json --verbose
       --permission-mode acceptEdits --model <model> [-resume <cc_session_id>] -p <prompt>
```

The `CC_MODE` env var controls the subprocess wrapper:
- `docker` (default): wraps in `docker exec -i reggia-cc`
- `local`: spawns `claude` directly with `cwd=chat_workspace/` (legacy, less isolated)

## Backend endpoints

### Chat & sessions
| Endpoint | Description |
|---|---|
| `POST /sessions` | Create new session, return UUID |
| `GET /sessions` | List non-archived sessions |
| `GET /sessions/search?q=` | Search sessions by title and message content |
| `GET /sessions/{id}` | Session metadata + full message history |
| `DELETE /sessions/{id}` | Soft delete (archive) |
| `POST /sessions/{id}/chat` | Send message, SSE stream response from Docker CC |
| `POST /sessions/{id}/title` | Manually rename session |
| `GET /sessions/stats/cache` | Aggregate cache hit rate (last 7 days) |
| `GET /chat/config` | Model list + default |
| `GET /chat/logs` | List debug log files |
| `GET /chat/logs/{session_id}` | Read session debug log (last 200 lines) |

### Reggia items CRUD (local SQLite)
| Endpoint | Description |
|---|---|
| `GET /reggia/items?status=active&domain=research` | Query with status/domain filters |
| `POST /reggia/items` | Create item in local SQLite |
| `PATCH /reggia/items/{id}` | Update any field (name, domain, priority, status, sensitivity, due_date, notes) |
| `DELETE /reggia/items/{id}` | Soft delete (status=dropped) or `?hard=true` to archive |

### Reggia long-term pages (Notion proxy, SQLite-cached)
| Endpoint | Description |
|---|---|
| `GET /reggia/index` | 00 Index & Query Guide (plain text) |
| `GET /reggia/longterm/{domain}` | Read long-term page (domain ∈ {work, research, intellectual, personal}) |
| `POST /reggia/longterm/{domain}` | Append a block to a long-term page (chat CC updates long-term memory) |

### Sync control
| Endpoint | Description |
|---|---|
| `GET /reggia/sync/status` | Sync state for all domains |
| `POST /reggia/sync/pull` | Pull all domains from Notion into local SQLite |
| `POST /reggia/sync/push` | Push local-dirty domains to Notion |
| `POST /reggia/sync/resolve` | Resolve a conflict (body: `{domain, winner: "local"|"notion"}`) |

## Data flow

### Chat flow
```
User types message
  → app.js: POST /sessions/{id}/chat {prompt, model}
  → sessions.py: load history from SQLite, build cache-optimized prompt
  → docker exec -i reggia-cc claude -p <full_prompt>
  → CC (in Docker) → DeepSeek API (via ANTHROPIC_BASE_URL), streams jsonl back
  → sessions.py: SSE stream to frontend, save assistant msg + cache stats to SQLite
  → app.js: render markdown via marked.js
```

### Reggia panel flow
```
Panel load / filter change
  → app.js: GET /reggia/items?status=active
  → main.py: SQLite query → compute urgency → return JSON
  → app.js: render collapsed cards

Quick add / edit / delete
  → app.js: POST|PATCH|DELETE /reggia/items[/{id}]
  → main.py: SQLite create|update|archive
  → app.js: reload items
```

### Chat CC accessing Reggia
```
CC (in Docker) required to query Reggia on every message
  → Bash: curl http://host.docker.internal:8000/reggia/index (routing guide)
  → Based on index routing, pull relevant longterm pages + active items
  → main.py: serves longterm from local SQLite cache (fast, ~5 ms, works offline)
    or queries items from reggia_items.db → return data
  → CC incorporates personal context into response
```

## SQLite schema

Four tables across three databases (WAL mode):

**reggia_session.db**:
- **sessions**: `id`, `title`, `created_at`, `updated_at`, `archived`
- **messages**: `id`, `session_id`, `role` (user/assistant), `content`, `created_at`, `cache_hit_tokens`, `cache_miss_tokens`, `output_tokens`

**reggia_items.db**:
- **items**: `id`, `name`, `domain`, `priority`, `status`, `sensitivity`, `notes`, `due_date`, `created_at`, `archived`

**reggia_longterm.db**:
- **long_term_memory**: `domain`, `notion_page_id`, `title`, `content_md`, `notion_pending_md`, `notion_last_edited`, `local_modified_at`, `synced_at`, `sync_state` (clean / local_dirty / conflict)
- **block_passthrough**: `domain`, `marker_id`, `raw_json` — stores unsupported Notion blocks for round-trip fidelity

Session titles are auto-generated from the first message via a lightweight CC call.

## Cache optimization

The prompt structure follows a 3-layer model to maximize DeepSeek cache hits:

1. **STATIC** — CLAUDE.md + skill files (loaded by CC from chat_workspace, identical across all requests → cached)
2. **STABLE** — conversation history (appended, never modified → cached within a session)
3. **DYNAMIC** — current user message at the very end (the only uncached token prefix per turn)

The spec (`per_session_control.md`) details this design. Cache stats are logged per message.

## Key design decisions

- **Backend is the single Notion gateway** — neither the frontend nor the chat CC has the Notion API key
- **Chat CC runs in Docker** — `docker exec` with container name `reggia-cc`; true filesystem isolation via container boundary, not just permission config
- **DeepSeek direct** — container uses `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` to bypass the Anthropic API middleman; OAuth not needed
- **Local SQLite for items + long-term cache** — active items in `reggia_items.db`; long-term pages cached in `reggia_longterm.db` with two-way Notion sync (pull on boot, push on append, conflict detection)
- **Notion Markdown round-trip** — `notion_markdown.py` converts Notion blocks ↔ Markdown bidirectionally; supported types include paragraphs, headings, lists, quotes, code, and GFM tables; unsupported block types passthrough via marker comments for fidelity
- **chat_workspace as volume mount** — `./backend/chat_workspace:/workspace` lets you edit CLAUDE.md and skill files locally; changes take effect instantly, no rebuild
- **Session persistence in SQLite** — survives server restarts; Docker CC state is container-ephemeral (`--resume` works within a container lifetime); `sessions_map` dict tracks frontend_session_id → cc_session_id in memory
- **Permission pre-approval** — chat CC settings.json restricts Read to `/workspace/**`, Bash to `curl *host.docker.internal*`, plus WebSearch, WebFetch; `--permission-mode acceptEdits` auto-approves allowed tools
- **Debug logging** — all CC stdout lines written to `backend/logs/chat_{session_id}.jsonl`
- **IME-safe inputs** — `e.isComposing` check on all Enter-key handlers

## Running

```bash
cd "/Users/xiaojinqiu/Documents/Summer 2026/Reggia"

# One command:
./start.sh

# Or step by step:
docker compose build          # first time only, or after Dockerfile changes
docker compose up -d          # start CC container
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Requires: `DEEPSEEK_API_KEY` + `NOTION_API_KEY` in `backend/.env`, Docker Desktop, UV package manager.
