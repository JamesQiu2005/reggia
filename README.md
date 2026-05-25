# Reggia

Personal AI chat frontend backed by a Notion knowledge base. A FastAPI backend orchestrates between a browser UI and Claude Code subprocesses, with Notion as the long-term memory and task tracking layer.

## Why

LLM web interfaces (ChatGPT, Claude, DeepSeek, Gemini) lose all personal context between sessions. Existing memory tools (Mem0, Zep, Claude memory, MemPalace) try to solve this by having the AI decide what's worth remembering — mining conversations, vector-searching them back later. This works for people who don't want to think about it, but it conflates two fundamentally different kinds of context: stable background (who you are, what you're working on long-term) and volatile state (what's on your plate this week).

Reggia inverts this. You curate the structure manually in Notion. Agents read it on demand, by domain, with sensitivity tags. There's no vector store, no embedding pipeline, no AI deciding what matters — because you already know what matters.

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
  ├─ Chat pane ── SSE stream ── FastAPI backend ── Claude Code subprocess (DeepSeek)
  │                                │
  └─ Reggia panel (CRUD) ─────────┘
                                   │
                              Notion API
```

- **Chat CC** runs in an isolated `chat_workspace/` with restricted permissions (Read sandboxed, Bash limited to `curl localhost*`, no Write/Edit)
- **Backend** is the single Notion gateway — the chat CC has no API key, it calls the backend via `curl localhost:8000`
- **Reggia panel** provides CRUD for Notion active items directly from the UI
- **Async subprocess** via `asyncio.create_subprocess_exec` prevents event-loop deadlock when the CC calls back into the backend

### Backend endpoints

| Endpoint | Description |
|---|---|
| `GET /sessions` | List non-archived sessions |
| `GET /sessions/search?q=` | Search sessions by title and message content |
| `POST /sessions` | Create new session |
| `POST /sessions/{id}/chat` | Send message, SSE stream response |
| `GET /chat/config` | Model list + default |
| `GET /reggia/index` | Query routing guide |
| `GET /reggia/longterm/{domain}` | Read long-term page (work, research, intellectual, personal) |
| `POST /reggia/longterm/{domain}` | Append a block to a long-term page |
| `GET /reggia/items?status=&domain=` | Query active items with filters |
| `POST /reggia/items` | Create item in Notion database |
| `PATCH /reggia/items/{id}` | Update item fields |
| `DELETE /reggia/items/{id}` | Soft-delete (set Status=dropped) |

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

## Design decisions

**Notion as storage, not vector DB.** Reggia's content is human-curated and read-on-demand. Structured Notion pages serve as a single source of truth that both you and the agent can read directly, with no embedding or indexing layer.

**Long-term vs short-term, hard split.** Long-term pages (work, research, intellectual, personal) are synthesized and rarely change. Short-term active items live in a separate Notion database with status/priority/due fields. Agents read them differently — long-term as stable context, short-term with urgency computed at query time from the current date.

**Sensitivity tags as honor-system access control.** Every long-term page is marked 🔓 agent-readable, 🟡 contextual (reasoning input only, never surface in third-party output), or 🔒 private. Not adversarial security — just behavioral guardrails for agents acting on your behalf.

**Isolated Claude Code instances.** The chat agent runs in a dedicated `chat_workspace/` with restricted permissions (no Write, no Edit, curl whitelisted to localhost:8000). Your daily Claude Code for coding uses the global config and stays untouched.

**Cache-aware prompt structure.** System prompt is fully static. Conversation history is append-only. Dynamic content (timestamps, current Reggia state) appears only in the trailing user message. This preserves DeepSeek's prefix cache across turns and across sessions, giving ~90% cache hit rates after warmup.

**Backend as the only Notion gateway.** The chat agent doesn't hold the Notion API key. It calls the backend via curl. This means a single place to audit, throttle, or modify Reggia access — and the agent literally cannot exfiltrate the key.

## How Reggia differs from other memory frameworks

| | Reggia | Mem0 / Zep / MemPalace | Letta / MemGPT |
|---|---|---|---|
| Who curates memory | User | AI (auto-extract from conversations) | Agent (self-edits via tools) |
| Storage | Notion pages + SQLite | Vector DB + graph store | Tiered runtime memory |
| Long-term vs short-term | Hard structural split | AI decides via importance/recency | Tiered (core / recall / archival) |
| Maintenance cost | User writes markdown | Zero (auto) | Zero (agent manages itself) |
| Transparency | Fully human-readable | Embeddings, opaque to user | Internal to Letta runtime |
| Lock-in | None | Vendor cloud or Letta runtime | High (Letta is the agent runtime) |
| Best for | Users who know their priorities | Users who don't want to think about memory | Long-horizon autonomous agents |

Reggia inverts the dominant assumption in agent memory research: that users either can't or won't tell the AI what matters. Instead, it asks the user to write it down once, in their own structure, and trusts that the agent's job is execution — not curation.

## Status

Personal project, built in one evening as a proof-of-concept for AI-first product development workflows. Not designed for general use — it's the system *I* wanted, and the design choices above reflect *my* preferences. Fork it, change everything, make it yours.

