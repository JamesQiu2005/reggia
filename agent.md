# Reggia — Codebase Architecture

## Overview

Reggia is a personal AI chat frontend backed by a personal knowledge base. The user ("Hanze") maintains structured long-term context in Notion and active items in local SQLite; the app provides a two-pane UI — chat on the left, knowledge base CRUD on the right — with a FastAPI backend orchestrating between the frontend, a chat engine (by default an in-process DeepSeek tool-calling loop; optionally a Docker-contained Claude Code subprocess), and the Notion API.

## Directory structure

```
Reggia/
├── CLAUDE.md                       # Orchestration agent instructions (this CLI session)
├── agent.md                        # This file — architecture reference
├── reggia-session-context-spec.md  # Prompt engineering spec (tool_calls + <ctx/> tags)
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
│   ├── memory_api.py               # Memory file CRUD API (REST endpoints for ~/.reggia/memory/)
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
│   ├── index.html                  # Chat page: sidebar + chat pane + Reggia panel + settings + welcome modal
│   ├── memory.html                 # /memory route: Milkdown WYSIWYG editor + file drawer sidebar
│   ├── styles.css                  # Design tokens, all component styles (chat, settings, memory editor)
│   ├── app.js                      # Chat SSE, session mgmt, Reggia panel CRUD, sidebar, account submenu
│   └── vendor/                     # Self-hosted, CDN-free assets (committed build output; served at /vendor/)
│       ├── milkdown-crepe.bundle.js   # Crepe + ProseMirror + CodeMirror + kit + mdast, one ESM file
│       ├── milkdown-crepe.css         # Crepe theme (common + frame), @imports inlined
│       ├── katex-fonts/               # KaTeX webfonts (referenced by the latex feature's CSS)
│       └── tabler-icons/              # Icon webfont CSS + fonts (shared by index.html + memory.html)
│
├── vendor-build/                   # Build tooling for frontend/vendor/ (node_modules gitignored)
│   ├── package.json                # @milkdown/crepe + kit + tabler + mdast; codemirror pinned to 6.0.2
│   ├── entry.js                    # JS bundle entry — re-exports Crepe, CrepeFeature, $remark, findAndReplace
│   ├── theme-entry.css             # CSS bundle entry — @imports the crepe theme barrels
│   └── build.mjs                   # esbuild: `npm install && npm run build` regenerates frontend/vendor/
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
build messages: [system, ...history (with <ctx/> tags where applicable), user]
while model returns tool_calls (max 8 rounds):
    execute each tool in-process → append results → re-request
stream text tokens to the frontend as SSE
```

When building the history, assistant messages with a non-null `tool_calls` column get a `<ctx src="page1, page2"/>` tag appended to their content, telling the model which long-term pages were already fetched in prior turns. The system prompt instructs the model to skip re-fetching those pages unless the user explicitly asks for a refresh.

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

### Memory file API (local markdown vault)
| Endpoint | Description |
|---|---|
| `GET /api/memory/files` | List all `.md` files with metadata |
| `GET /api/memory/files/{path}` | Read raw markdown content |
| `PUT /api/memory/files/{path}` | Write content (auto-save target) |
| `POST /api/memory/files` | Create new file (body: `{path, content?}`) |
| `DELETE /api/memory/files/{path}` | Delete file |
| `PATCH /api/memory/files/{path}` | Rename/move (body: `{new_path}`) |
| `GET /api/memory/links` | Wikilink graph for the whole vault (`{path: {links_to, unresolved, linked_from}}`) |
| `GET /api/memory/links/{path}` | Link record for one file |

Files live at `~/.reggia/memory/` (configurable via `REGGIA_MEMORY_DIR` env var). Plain markdown, 1:1 compatible with an Obsidian vault pointed at the same directory. The frontend `/memory` page provides a WYSIWYG editor (Milkdown Crepe) with auto-save and a file drawer sidebar. The editor and its assets are self-hosted from `frontend/vendor/` (no CDN — see `vendor-build/`), so it works fully offline. A fixed left bar (`#memory-format-bar`) replaces Crepe's floating selection toolbar and carries the full palette — inline marks (bold/italic/strike/code), block types (H1–H3, bullet/ordered list, quote, code block, divider), link, and math — driven by Milkdown commands exported from the bundle. Inline marks toggle Office-style (stored-mark "mode" with active-state reflected on the button). The `/` slash menu (Crepe BlockEdit) is kept but restyled Notion-like (`#memory-editor .milkdown-slash-menu` overrides in `styles.css`).

**Wikilinks:** Obsidian `[[links]]` are parsed by a custom remark plugin into `wikilink://`-scheme anchors (resolved client-side against the file list; click to navigate, or create-on-click when unresolved) and serialized back to `[[ ]]` on save. On every file mutation the backend rebuilds a `_links.json` link graph at the vault root (resolved `.md` paths for `links_to`/`linked_from`, raw targets for `unresolved`). The graph endpoints exist for the agent to adopt later — `fetch_memory`/`<ctx/>` are **not** wired to the local vault yet (Notion stays primary; the local vault is a backup this phase).

## Data flow

### Chat flow (default `agent` engine)
```
User types message
  → app.js: POST /sessions/{id}/chat {prompt, model, web_search}
  → sessions.py: load history from SQLite, append user msg
  → agent_loop.run(): DeepSeek /chat/completions with function tools
       while tool_calls: execute in-process (memory tools / Bocha web search) → feed back
  → SSE (text_delta / tool_call / tool_result / result) → frontend
  → sessions.py: persist assistant msg + tool_calls metadata + token usage to SQLite
  → next turn: load_history includes tool_calls → agent_loop appends <ctx/> tags
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

### Memory editor flow
```
User clicks account card → floating submenu → "Memory" → /memory page
  → memory.html: load Milkdown editor + file list from GET /api/memory/files
  → User selects a file → GET /api/memory/files/{path} → editor renders markdown
  → User edits → debounced auto-save (2s) or blur → PUT /api/memory/files/{path}
  → File drawer: create (POST), rename (PATCH), delete (DELETE)
  → "← Back to chat" returns to /
```

## SQLite schema

Four tables across three databases (WAL mode):

**reggia_session.db**:
- **sessions**: `id`, `title`, `created_at`, `updated_at`, `archived`
- **messages**: `id`, `session_id`, `role` (user/assistant), `content`, `created_at`, `cache_hit_tokens`, `cache_miss_tokens`, `output_tokens`, `tool_calls` (JSON array of `{tool, page/query}`, NULL if no tools called)

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

The spec (`reggia-session-context-spec.md`) details this design. Cache stats are logged per message.

### Context Fetch Deduplication (`<ctx/>` tags)

To avoid redundant long-term memory reads across turns, each assistant message that called `reggia_longterm_read` stores its tool metadata in the `tool_calls` column. When building the prompt for the next turn, `agent_loop.run()` appends a `<ctx src="page1, page2"/>` tag to those assistant messages. The system prompt instructs the model: if a `<ctx/>` tag shows a page was already fetched, reuse the prior answer rather than re-fetching — unless the user explicitly asks for a refresh. This reduces both token cost and API latency.

Only `reggia_longterm_read` calls generate `<ctx/>` tags. Web search and item detail calls are logged in `tool_calls` for traceability but do not produce `<ctx/>` tags (search results are time-sensitive; item details are cheap).

## Key design decisions

- **Backend is the single Notion gateway** — neither the frontend nor the chat engine holds the Notion API key
- **Two chat engines, `agent` default** — in-process DeepSeek tool-calling loop (`agent_loop.py`), no Docker; `CHAT_ENGINE=docker` switches to Claude Code in `reggia-cc` (true filesystem isolation via the container boundary)
- **DeepSeek direct** — the agent engine calls DeepSeek's OpenAI-compatible `/chat/completions` with function tools; the docker engine sets `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`. No Anthropic middleman, OAuth not needed
- **Local SQLite for items + long-term cache** — active items in `reggia_items.db`; long-term pages cached in `reggia_longterm.db` with two-way Notion sync (pull on boot, push on append, conflict detection)
- **Notion Markdown round-trip** — `notion_markdown.py` converts Notion blocks ↔ Markdown bidirectionally; supported types include paragraphs, headings, lists, quotes, code, and GFM tables; unsupported block types passthrough via marker comments for fidelity
- **chat_workspace as volume mount** — `./backend/chat_workspace:/workspace` lets you edit CLAUDE.md and skill files locally; changes take effect instantly, no rebuild
- **Session persistence in SQLite** — survives restarts. The agent engine is stateless (it replays history each turn); the docker engine resumes container-side state within a container lifetime via the in-memory `sessions_map` (frontend_session_id → cc_session_id)
- **Prompt engineering** — `tool_calls` column tracks which long-term pages were fetched per turn; `<ctx/>` tags appended to assistant messages in subsequent turns prevent redundant `reggia_longterm_read` calls, maximizing DeepSeek V4 prefix cache hits
- **Memory editor** — standalone `/memory` route with Milkdown Crepe WYSIWYG editor (ProseMirror-based, Obsidian-style live editing). Files stored as plain `.md` in `~/.reggia/memory/`. Editor is bundled and self-hosted from `frontend/vendor/` (built by `vendor-build/`, `codemirror` pinned to 6.0.2) — no runtime CDN fetch, works offline. Still degrades to a plain textarea if the bundle ever fails to load. File drawer with folder grouping, inline rename, right-click context menu.
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
