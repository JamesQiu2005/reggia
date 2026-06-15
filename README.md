# Reggia

Personal AI chat frontend backed by a Notion knowledge base and local SQLite. A FastAPI backend orchestrates between a browser UI and a lightweight in-process DeepSeek agent loop (function-calling — no Docker or Claude Code required), with Notion for long-term memory and SQLite for active item tracking. A Docker-contained Claude Code engine remains available as an opt-in legacy fallback.

## Why

LLM web interfaces (ChatGPT, Claude, DeepSeek, Gemini) lose personal context between sessions. Current web interfaces has vague memory support and is not intelligent enough on when to use the memory. Existing memory tools (Mem0, Zep, Claude memory, MemPalace) try to solve this by having the AI decide what's worth remembering — mining conversations, vector-searching them back later. This works for people who don't want to think about it, but it conflates two fundamentally different kinds of context: stable background (who you are, what you're working on long-term) and volatile state (what's on your plate this week).

Reggia inverts this. You curate the structure manually. Agents read it on demand, by domain, with sensitivity tags. There's no vector store, no embedding pipeline, no AI deciding what matters — because you already know what matters.

This is the philosophy of *define your own reward function* applied to AI memory: the user, not the model, decides what counts as signal.

## When Reggia is the right tool

- You already have a strong sense of your own direction and priorities
- You want LLMs to act with personal context but don't want them mining your conversations
- You prefer human-readable, human-editable storage over opaque vector embeddings
- You're willing to write down what matters once, in exchange for full control afterward

## When it's not

- You want zero-maintenance memory — go use MemPalace or Mem0
- You need cross-user/team shared context — Reggia is single-user
- You want the AI to surface forgotten threads you didn't know you needed

## Architecture

```
Browser (two-pane UI)
  │
  ├─ Chat pane ── SSE stream ── FastAPI backend ── DeepSeek agent loop (default)
  │                                │
  └─ Reggia panel (CRUD) ─────────┘
                                   │
                          ┌────────┴────────┐
                          │                 │
                      SQLite            Notion API
                   (active items)    (long-term pages)
```

- **Chat engine (default `agent`)** — an in-process DeepSeek tool-calling loop (`backend/agent_loop.py`). The backend calls DeepSeek's `/chat/completions` with function tools; tools run in-process against local SQLite, the Notion-backed long-term cache, and the 博查 (Bocha) web-search API; output streams to the frontend via SSE. No Docker, no Claude Code.
- **Legacy engine (`CHAT_ENGINE=docker`)** — Claude Code inside a `reggia-cc` container connected to DeepSeek's Anthropic-compatible endpoint, with true filesystem isolation (Read `/workspace/**`, Bash `curl *host.docker.internal*`, no Write/Edit). Kept as an opt-in fallback.
- **Auth** — `DEEPSEEK_API_KEY` for chat; `BOCHA_API_KEY` (optional) for web search, toggled per-message from the composer.
- **Backend** is the single gateway for both SQLite items and Notion long-term pages.
- **Reggia panel** provides full CRUD for active items (name, domain, priority, status, sensitivity, due date, notes) directly from the UI.

### Backend endpoints

#### Chat & sessions
| Endpoint | Description |
|---|---|
| `GET /sessions` | List non-archived sessions |
| `GET /sessions/search?q=` | Search sessions by title and message content |
| `POST /sessions` | Create new session |
| `GET /sessions/{id}` | Session metadata + full message history |
| `DELETE /sessions/{id}` | Soft delete (archive) |
| `POST /sessions/{id}/chat` | Send message (`{prompt, model, web_search}`); SSE stream from the chat engine (agent loop by default, or Docker CC) |
| `POST /sessions/{id}/title` | Manually rename session |
| `GET /sessions/stats/cache` | Aggregate cache hit rate (last 7 days) |
| `GET /chat/config` | Model list + default |
| `GET /chat/logs` | List debug log files |
| `GET /chat/logs/{session_id}` | Read session debug log (last 200 lines) |

#### Reggia items CRUD (local SQLite)
| Endpoint | Description |
|---|---|
| `GET /reggia/items?status=&domain=` | Query active items with filters |
| `POST /reggia/items` | Create item |
| `PATCH /reggia/items/{id}` | Update any field |
| `DELETE /reggia/items/{id}` | Soft-delete (status=dropped) or `?hard=true` to archive |

#### Reggia long-term pages (Notion proxy, SQLite-cached)
| Endpoint | Description |
|---|---|
| `GET /reggia/index` | Query routing guide |
| `GET /reggia/longterm/{domain}` | Read long-term page (work, research, intellectual, personal) |
| `POST /reggia/longterm/{domain}` | Append a block to a long-term page |

#### Sync control
| Endpoint | Description |
|---|---|
| `GET /reggia/sync/status` | Sync state for all domains |
| `POST /reggia/sync/pull` | Pull all domains from Notion |
| `POST /reggia/sync/push` | Push local-dirty domains to Notion |
| `POST /reggia/sync/resolve` | Resolve a conflict (winner: local or notion) |

## Prerequisites

- Python 3.12+ with [uv](https://docs.astral.sh/uv/)
- A [DeepSeek API key](https://platform.deepseek.com/api_keys)
- A Notion account with an integration (for long-term pages only)
- (optional) A 博查 (Bocha) API key — enables the `web_search` tool
- (optional) [Docker Desktop](https://www.docker.com/products/docker-desktop/) — only for the legacy `CHAT_ENGINE=docker` engine

## Setup

```bash
git clone https://github.com/JamesQiu2005/reggia.git
cd reggia
uv sync
```

### Environment

Create `backend/.env`:

```bash
DEEPSEEK_API_KEY=sk-your_deepseek_key_here     # required — the chat engine
NOTION_API_KEY=ntn_your_notion_key_here        # long-term pages
BOCHA_API_KEY=sk-your_bocha_key_here           # optional — web_search tool
```

The DeepSeek key is required for chat. The Notion key powers long-term pages. The Bocha key is optional — without it the web-search toggle returns a "not configured" notice.

### (Optional) Build the legacy CC container

Only needed for the legacy engine (`CHAT_ENGINE=docker`). The default agent engine needs no container.

```bash
docker compose build
```

This builds the `reggia-cc` image (Node 24 Alpine + Claude Code + sandbox dependencies).

## Running

```bash
./start.sh
```

This sources `backend/.env`, attempts to start the Docker container (skipped gracefully if Docker is unavailable), then starts the backend on the default `agent` engine. Open `http://localhost:8000`.

Or step by step:

```bash
uv run uvicorn backend.main:app --host 0.0.0.0 --port 8000   # default: agent engine

# Legacy Docker engine instead:
CHAT_ENGINE=docker docker compose up -d                      # start CC container
CHAT_ENGINE=docker uv run uvicorn backend.main:app --port 8000
```

### Editing chat CC configuration (legacy `docker` engine only)

`backend/chat_workspace/` is mounted as a volume into the container. Edit files locally and changes take effect on the next chat message — no rebuild needed:

```bash
vi backend/chat_workspace/CLAUDE.md               # chat persona + instructions
vi backend/chat_workspace/.claude/skills/reggia.md # endpoint reference
vi backend/chat_workspace/.claude/settings.json    # tool permissions
```

Or shell into the container:

```bash
docker exec -it reggia-cc sh
```

## Project structure

```
Reggia/
├── Dockerfile                  # CC container image
├── docker-compose.yml          # reggia-cc service
├── docker-entrypoint.sh        # Docker env setup (URL/path substitution)
├── start.sh                    # One-command startup
├── .env.example                # Required env vars
├── backend/
│   ├── main.py                 # FastAPI app, all route registration
│   ├── sessions.py             # Session CRUD, chat SSE — agent loop + legacy Docker exec
│   ├── agent_loop.py           # In-process DeepSeek tool-calling loop (default chat engine)
│   ├── db.py                   # SQLite: sessions, messages, items — schema + CRUD
│   ├── longterm_db.py          # SQLite: long-term memory cache + passthrough store
│   ├── sync.py                 # Notion sync: pull, push, append, resolve
│   ├── notion_markdown.py      # Bidirectional Notion blocks <-> Markdown converter
│   ├── prompts.py              # Cache-optimized prompt builder, title prompt
│   ├── config.py               # Shared config, CHAT_ENGINE flag (agent/docker)
│   ├── chat_config.json        # Model list (deepseek-v4-pro, deepseek-v4-flash)
│   ├── test_headless_chat.py   # Integration test for chat CC
├── test_notion_markdown.py  # Unit tests for Notion ↔ Markdown converter
│   ├── chat_workspace/         # Volume-mounted into container as /workspace
│   │   ├── CLAUDE.md           # Chat persona + tool constraints
│   │   └── .claude/
│   │       ├── settings.json   # Restricted permissions
│   │       └── skills/
│   │           └── reggia.md   # Endpoint reference for CC
│   ├── databases/              # SQLite files (WAL mode)
│   │   ├── reggia_session.db   # Sessions + messages
│   │   ├── reggia_items.db     # Active items (local)
│   │   └── reggia_longterm.db  # Long-term memory cache + block passthrough
│   └── logs/                   # Per-session debug logs (chat_{session_id}.jsonl)
├── frontend/
│   ├── index.html              # Two-pane layout
│   ├── app.js                  # Chat SSE (+ web-search toggle), session mgmt, Reggia CRUD
│   └── styles.css              # All styles
├── skills/
│   └── reggia_notion.md        # Full Notion API reference (for orchestration)
├── template/                   # UI mockups (reference only)
└── pyproject.toml
```

## Design decisions

**Two interchangeable chat engines.** The default `agent` engine is an in-process DeepSeek tool-calling loop — lightweight, no container, easy to ship in a desktop build. The legacy `docker` engine runs Claude Code in a `reggia-cc` container for true filesystem isolation (the container boundary is the security boundary, not just permission config). `CHAT_ENGINE` selects between them; both stream to the frontend over the same SSE contract.

**DeepSeek direct, no Anthropic middleman.** The agent engine calls DeepSeek's OpenAI-compatible `/chat/completions` with function tools. The legacy engine sets `ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic` so Claude Code talks to DeepSeek natively. Either way auth is a single API key — no OAuth, no browser login, no keychain dependency.

**Local SQLite for active items, Notion for long-term.** Active items (tasks, deadlines) benefit from fast local queries and offline resilience. Long-term pages (stable knowledge about the user) stay in Notion for human readability and editing. The API contract is identical regardless of storage backend.

**Long-term vs short-term, hard split.** Long-term pages (work, research, intellectual, personal) are synthesized and rarely change. Active items have status/priority/due fields with urgency computed at query time from the current date. Agents read them differently — long-term as stable context, active items with computed urgency.

**Sensitivity tags as honor-system access control.** Every active item can be tagged `agent-readable` (use freely), `contextual` (reasoning only, never surface in third-party output), or `private` (skip entirely). Not adversarial security — behavioral guardrails for agents acting on your behalf.

**Isolated Claude Code instances (legacy engine).** When run as `CHAT_ENGINE=docker`, the chat agent runs in its own Docker container, completely separated from your daily-use Claude Code — different config, auth, and permissions.

**Cache-aware prompt structure.** System prompt is fully static. Conversation history is append-only. Dynamic content appears only in the trailing user message. This preserves DeepSeek's prefix cache across turns and sessions.

**Backend as the only gateway.** Memory access funnels through one layer: the agent engine's tools call the SQLite / Notion-cache helpers in-process, and the legacy engine curls the same backend endpoints. A single place to audit, throttle, or modify Reggia access.

## How Reggia differs from other memory frameworks

| | Reggia | Mem0 / Zep / MemPalace | Letta / MemGPT |
|---|---|---|---|
| Who curates memory | User | AI (auto-extract from conversations) | Agent (self-edits via tools) |
| Storage | Notion + SQLite | Vector DB + graph store | Tiered runtime memory |
| Long-term vs short-term | Hard structural split | AI decides via importance/recency | Tiered (core / recall / archival) |
| Maintenance cost | User writes markdown | Zero (auto) | Zero (agent manages itself) |
| Transparency | Fully human-readable | Embeddings, opaque to user | Internal to Letta runtime |
| Lock-in | None | Vendor cloud or Letta runtime | High (Letta is the agent runtime) |
| Best for | Users who know their priorities | Users who don't want to think about memory | Long-horizon autonomous agents |

Reggia inverts the dominant assumption in agent memory research: that users either can't or won't tell the AI what matters. Instead, it asks the user to write it down once, in their own structure, and trusts that the agent's job is execution — not curation.

## Status

Personal project, built in one evening as a proof-of-concept for AI-first product development workflows. Not designed for general use — it's the system *I* wanted, and the design choices above reflect *my* preferences. Fork it, change everything, make it yours.
