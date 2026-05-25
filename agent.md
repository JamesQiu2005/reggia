# Reggia — Codebase Architecture

## Overview

Reggia is a personal AI chat frontend backed by a personal knowledge base in Notion. The user ("Hanze") maintains structured long-term context and active items in Notion; the app provides a two-pane UI — chat on the left, knowledge base CRUD on the right — with a FastAPI backend orchestrating between the frontend, Claude Code subprocesses (routed through DeepSeek), and the Notion API.

## Directory structure

```
Reggia/
├── CLAUDE.md                       # Orchestration agent instructions (this CLI session)
├── agent.md                        # This file — architecture reference
├── per_session_control.md          # Session management spec
├── pyproject.toml                  # UV project (fastapi, uvicorn, httpx)
├── uv.lock
├── reggia.db                       # SQLite (created on first run)
│
├── backend/
│   ├── main.py                     # FastAPI app, items CRUD, longterm pages, static mount
│   ├── config.py                   # Shared config (CHAT_CONFIG, CHAT_WORKSPACE path)
│   ├── db.py                       # SQLite schema, session/message CRUD, cache stats
│   ├── prompts.py                  # Cache-optimized prompt builder, title prompt
│   ├── sessions.py                 # /sessions CRUD, /sessions/{id}/chat, debug logging
│   ├── chat_config.json            # Model list + defaults
│   ├── chat_workspace/             # Isolated cwd for frontend chat CC subprocesses
│   │   ├── CLAUDE.md               # Minimal chat instructions (~200 tokens)
│   │   └── .claude/
│   │       ├── settings.json       # Permissions: Read(chat_workspace/**), Bash(curl *localhost*), WebSearch, WebFetch
│   │       └── skills/
│   │           └── reggia.md       # Condensed skill: backend endpoints, routing, sensitivity
│   └── logs/                       # Per-session debug logs (chat_{session_id}.jsonl)
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

| | Orchestration CC (this session) | Chat CC (subprocess) |
|---|---|---|
| **cwd** | `Reggia/` (project root) | `backend/chat_workspace/` |
| **CLAUDE.md** | Full orchestration instructions | Minimal chat persona (~200 tokens) |
| **Skills** | `skills/reggia_notion.md` | `.claude/skills/reggia.md` (condensed) |
| **Notion access** | Direct Notion API (has key) | Via backend REST endpoints only |
| **Purpose** | Code editing, architecture, system control | User-facing chat |

The chat CC is spawned per-request via `asyncio.create_subprocess_exec` (async, non-blocking) with:
```
claude --output-format stream-json --verbose --permission-mode acceptEdits
       --model <model> [-resume <cc_session_id>] -p <prompt>
       [cwd = chat_workspace/]
```

## Backend endpoints

### Chat & sessions
| Endpoint | Description |
|---|---|
| `POST /sessions` | Create new session, return UUID |
| `GET /sessions` | List non-archived sessions |
| `GET /sessions/{id}` | Session metadata + full message history |
| `DELETE /sessions/{id}` | Soft delete (archive) |
| `POST /sessions/{id}/chat` | Send message, SSE stream response from CC subprocess |
| `POST /sessions/{id}/title` | Manually rename session |
| `GET /chat/config` | Model list + default |
| `GET /chat/logs` | List debug log files |
| `GET /chat/logs/{session_id}` | Read session debug log (last 200 lines) |

### Reggia items CRUD (Notion proxy)
| Endpoint | Description |
|---|---|
| `GET /reggia/items?status=active&domain=research` | Query with status/domain filters |
| `POST /reggia/items` | Create item in Notion database |
| `PATCH /reggia/items/{id}` | Update any field |
| `DELETE /reggia/items/{id}` | Soft delete (Status=dropped) or `?hard=true` to archive |

### Reggia long-term pages (Notion proxy)
| Endpoint | Description |
|---|---|
| `GET /reggia/index` | 00 Index & Query Guide (plain text) |
| `GET /reggia/longterm/{domain}` | domain ∈ {work, research, intellectual, personal} |

## Data flow

### Chat flow
```
User types message
  → app.js: POST /sessions/{id}/chat {prompt, model}
  → sessions.py: load history from SQLite, build cache-optimized prompt
  → asyncio.create_subprocess_exec: claude -p <full_prompt>  [cwd=chat_workspace/]
  → CC sends to DeepSeek, streams jsonl back
  → sessions.py: SSE stream to frontend, save assistant msg + cache stats to SQLite
  → app.js: render markdown via marked.js
```

### Reggia panel flow
```
Panel load / filter change
  → app.js: GET /reggia/items?status=active
  → main.py: Notion API query → compute urgency → return JSON
  → app.js: render collapsed cards

Quick add / edit / delete
  → app.js: POST|PATCH|DELETE /reggia/items[/{id}]
  → main.py: Notion API create|update|archive
  → app.js: reload items
```

### Chat CC accessing Reggia
```
CC required to query Reggia on every message
  → Bash: curl http://localhost:8000/reggia/index (routing guide)
  → Based on index routing, pull relevant longterm pages + active items
  → main.py: Notion API → return data
  → CC incorporates personal context into response
```

## SQLite schema

Two tables in `reggia.db` (WAL mode):

**sessions**: `id`, `title`, `created_at`, `updated_at`, `archived`
**messages**: `id`, `session_id`, `role` (user/assistant), `content`, `created_at`, `cache_hit_tokens`, `cache_miss_tokens`, `output_tokens`

Session titles are auto-generated from the first message via a lightweight CC call.

## Cache optimization

The prompt structure follows a 3-layer model to maximize DeepSeek cache hits:

1. **STATIC** — CLAUDE.md + skill files (loaded by CC from chat_workspace, identical across all requests → cached)
2. **STABLE** — conversation history (appended, never modified → cached within a session)
3. **DYNAMIC** — current user message at the very end (the only uncached token prefix per turn)

The spec (`per_session_control.md`) details this design. Cache stats are logged per message.

## Key design decisions

- **Backend is the single Notion gateway** — neither the frontend nor the chat CC has the Notion API key
- **Chat CC runs in isolated workspace** — `chat_workspace/` cwd prevents loading the orchestration CLAUDE.md
- **Session persistence in SQLite** — survives server restarts; CC subprocesses remain stateless
- **--resume for session continuity** — second message in a session resumes the CC session (prompt caching + conversation memory)
- **Permission pre-approval** — chat CC settings.json restricts Read to `chat_workspace/**`, Bash to `curl *localhost*`, plus WebSearch, WebFetch; `--permission-mode acceptEdits` auto-approves allowed tools
- **Debug logging** — all CC stdout lines written to `backend/logs/chat_{session_id}.jsonl`
- **IME-safe inputs** — `e.isComposing` check on all Enter-key handlers

## Running

```bash
cd "/Users/xiaojinqiu/Documents/Summer 2026/Reggia"
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

Requires: `NOTION_API_KEY` environment variable, `claude` binary in PATH, UV package manager.
