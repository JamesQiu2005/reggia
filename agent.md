# Reggia — Codebase Architecture

## Overview

Reggia is a personal AI chat frontend backed by a personal knowledge base. The user ("Hanze") maintains structured long-term context in Notion and active items in local SQLite; the app provides a two-pane UI — chat on the left, knowledge base CRUD on the right — with a FastAPI backend orchestrating between the frontend, a chat engine (by default an in-process DeepSeek tool-calling loop; optionally a Docker-contained Claude Code subprocess), and the Notion API.

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
│   ├── config.py                   # Shared config, CHAT_ENGINE flag (agent/docker)
│   ├── db.py                       # SQLite: sessions, messages, items — schema + CRUD
│   ├── longterm_db.py              # SQLite: long-term memory cache + block passthrough store
│   ├── sync.py                     # Notion sync: pull, push, append, resolve
│   ├── notion_markdown.py          # Notion blocks <-> Markdown: paragraphs, headings, lists, quotes, code, tables
│   ├── test_notion_markdown.py      # Unit tests for the converter (table rendering, inline marks)
│   ├── prompts.py                  # Cache-optimized prompt builder, title prompt
│   ├── sessions.py                 # /sessions CRUD, /sessions/{id}/chat — agent loop + legacy Docker exec
│   ├── agent_loop.py               # In-process DeepSeek tool-calling loop (default chat engine)
│   ├── chat_config.json            # Model list + defaults (deepseek-v4-pro, deepseek-v4-flash)
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
│   └── .env                        # DEEPSEEK_API_KEY + NOTION_API_KEY + BOCHA_API_KEY (gitignored)
│
├── frontend/
│   ├── index.html                  # Two-pane layout: chat + Reggia panel
│   ├── styles.css                  # Design tokens, all component styles
│   └── app.js                      # Chat SSE (+ web-search toggle), session mgmt, Reggia panel CRUD
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

## Chat engines

The chat side has two interchangeable engines, selected by the `CHAT_ENGINE` env var:

| | `agent` (default) | `docker` (legacy) |
|---|---|---|
| **Runtime** | In-process, inside the backend | Docker `reggia-cc` container |
| **Implementation** | `agent_loop.py` — DeepSeek tool-calling loop | Claude Code via `docker exec -i reggia-cc` |
| **API routing** | DeepSeek `/chat/completions` (OpenAI-compatible, function tools) | `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` |
| **Memory access** | Tools call `db` / `longterm_db` in-process | `curl host.docker.internal:8000` backend endpoints |
| **Persona / rules** | `agent_loop._system_prompt()` | `chat_workspace/CLAUDE.md` + `.claude/skills/reggia.md` |
| **Web search** | `web_search` tool → 博查 (Bocha) API | CC's own WebSearch / WebFetch |
| **Deps** | none beyond `httpx` | Docker + the `reggia-cc` image |

### Agent loop (`agent_loop.run`)

A while-loop around DeepSeek's tool-calling API:

```
build messages: [system, ...history, user]
while model returns tool_calls (max 8 rounds):
    execute each tool in-process → append results → re-request
stream text tokens to the frontend as SSE
```

Tools offered to the model:

| Tool | Tier | Purpose |
|---|---|---|
| `reggia_index` | L0 | Query routing guide (call first in a session) |
| `reggia_longterm_index` | L1 | TOC of the four long-term pages (cheap) |
| `reggia_longterm_read` | L2 | Full content of one long-term page (expensive) |
| `reggia_items_list` | L1 | Summary list of active items (cheap) |
| `reggia_item_detail` | L2 | Full detail of one item |
| `web_search` | — | 博查 (Bocha) web search — only offered when the composer toggle is ON (`web_search=true`) |

SSE event types: `text_delta`, `tool_call`, `tool_result`, `result`, `error`.

### Legacy Docker engine

Spawned per-request from the backend via `docker exec`:
```
docker exec -i reggia-cc claude --output-format stream-json --include-partial-messages
       --verbose --permission-mode acceptEdits --model <model> [--resume <cc_session_id>] -p <prompt>
```
Emits Claude Code stream-json events (`assistant`, `stream_event`, `result`, …). The frontend's `handleStreamMessage` switch handles both engines' event vocabularies.

### Orchestration CC (this dev session)

Separate from the chat engines: the Claude Code session editing this repo runs on the host with full tooling, the full `CLAUDE.md`, and direct Notion access via `skills/reggia_notion.md`. It is the development agent, not the user-facing chat.

## Backend endpoints

### Chat & sessions
| Endpoint | Description |
|---|---|
| `POST /sessions` | Create new session, return UUID |
| `GET /sessions` | List non-archived sessions |
| `GET /sessions/search?q=` | Search sessions by title and message content |
| `GET /sessions/{id}` | Session metadata + full message history |
| `DELETE /sessions/{id}` | Soft delete (archive) |
| `POST /sessions/{id}/chat` | Send message (`{prompt, model, web_search}`); SSE stream from the chat engine (agent loop by default, or Docker CC) |
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

### Chat flow (default `agent` engine)
```
User types message
  → app.js: POST /sessions/{id}/chat {prompt, model, web_search}
  → sessions.py: load history from SQLite, append user msg
  → agent_loop.run(): DeepSeek /chat/completions with function tools
       while tool_calls: execute in-process (memory tools / Bocha web search) → feed back
  → SSE (text_delta / tool_call / tool_result / result) → frontend
  → sessions.py: persist assistant msg + token usage to SQLite
  → app.js: render markdown via marked.js

Legacy (CHAT_ENGINE=docker):
  → docker exec -i reggia-cc claude -p <full_prompt>
  → CC (in Docker) → DeepSeek API (via ANTHROPIC_BASE_URL), streams jsonl back
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

### Memory access
```
agent engine:  tool call → agent_loop executor → db / longterm_db (in-process, ~ms)
docker engine: curl http://host.docker.internal:8000/reggia/* → main.py → SQLite cache
```
Both read the same local SQLite (long-term cache + active items). Long-term serves
from cache (fast, ~5 ms, works offline), syncing with Notion in the background.
The two-level strategy (L1 index/list before L2 read/detail) keeps token cost low.

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

Session titles are auto-generated from the first message via a lightweight DeepSeek call (`agent_loop.generate_title`); the legacy engine uses a one-shot CC call.

## Cache optimization

The prompt structure follows a 3-layer model to maximize DeepSeek cache hits:

1. **STATIC** — the system prompt (`agent_loop._system_prompt()`; or CLAUDE.md + skill files for the docker engine) — identical across requests → cached
2. **STABLE** — conversation history (appended, never modified → cached within a session)
3. **DYNAMIC** — current user message at the very end (the only uncached token prefix per turn)

The spec (`per_session_control.md`) details this design. Cache stats are logged per message.

## Key design decisions

- **Backend is the single Notion gateway** — neither the frontend nor the chat engine holds the Notion API key
- **Two chat engines, `agent` default** — in-process DeepSeek tool-calling loop (`agent_loop.py`), no Docker; `CHAT_ENGINE=docker` switches to Claude Code in `reggia-cc` (true filesystem isolation via the container boundary)
- **DeepSeek direct** — the agent engine calls DeepSeek's OpenAI-compatible `/chat/completions` with function tools; the docker engine sets `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`. No Anthropic middleman, OAuth not needed
- **Local SQLite for items + long-term cache** — active items in `reggia_items.db`; long-term pages cached in `reggia_longterm.db` with two-way Notion sync (pull on boot, push on append, conflict detection)
- **Notion Markdown round-trip** — `notion_markdown.py` converts Notion blocks ↔ Markdown bidirectionally; supported types include paragraphs, headings, lists, quotes, code, and GFM tables; unsupported block types passthrough via marker comments for fidelity
- **chat_workspace as volume mount** — `./backend/chat_workspace:/workspace` lets you edit CLAUDE.md and skill files locally; changes take effect instantly, no rebuild
- **Session persistence in SQLite** — survives restarts. The agent engine is stateless (it replays history each turn); the docker engine resumes container-side state within a container lifetime via the in-memory `sessions_map` (frontend_session_id → cc_session_id)
- **Permission pre-approval (docker engine)** — chat CC settings.json restricts Read to `/workspace/**`, Bash to `curl *host.docker.internal*`, plus WebSearch, WebFetch; `--permission-mode acceptEdits` auto-approves allowed tools
- **Debug logging** — both engines write their SSE lines to `backend/logs/chat_{session_id}.jsonl`
- **IME-safe inputs** — `e.isComposing` check on all Enter-key handlers

## Running

```bash
cd "/Users/xiaojinqiu/Documents/Summer 2026/Reggia"

# One command:
./start.sh

# Or step by step (default agent engine — no Docker needed):
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000

# Legacy docker engine:
docker compose build          # first time only, or after Dockerfile changes
CHAT_ENGINE=docker docker compose up -d
CHAT_ENGINE=docker uv run uvicorn backend.main:app --port 8000
```

Requires: `DEEPSEEK_API_KEY` (+ `NOTION_API_KEY`, optional `BOCHA_API_KEY`) in `backend/.env` and the UV package manager. Docker Desktop only for the legacy engine.
