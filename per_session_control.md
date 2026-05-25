# Reggia Backend — Session Management & Cache Optimization Spec

## Goal

Add per-session conversation management with local SQLite persistence to the existing Reggia backend, while structuring prompts to maximize DeepSeek's context cache hit rate.

## Background

- DeepSeek's API is stateless. Conversation history must be re-sent each request.
- Context caching is automatic but **prefix-strict**: cache hits only count from token 0. Any variation in the early part of the prompt invalidates the entire downstream prefix.
- Cache-hit tokens are billed at ~10% of cache-miss tokens. For multi-turn chat, optimizing for cache hits dramatically reduces both cost and first-token latency.
- The Reggia backend spawns a fresh `claude` subprocess per request (via Claude Code CLI in stream-json mode, configured to route through DeepSeek). Subprocesses are stateless — conversation state lives in SQLite.

## Architecture overview

```
Frontend ──HTTP/SSE──▶ Backend ──┬─▶ SQLite (sessions, messages, cache_stats)
                                 │
                                 └─▶ subprocess: claude → DeepSeek API
                                     (stateless; full history piped in)
```

Backend is HTTP server, stateless itself. SQLite is the only source of truth for chat state. CC subprocess is spawned per request, dies after streaming completes.

---

## Part 1: SQLite schema

Create `reggia.db` with the following schema. Use WAL mode for better concurrent read/write performance.

```sql
-- Enable WAL mode (run once on first open)
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,                  -- UUID v4
    title TEXT,                           -- auto-generated from first message, nullable until set
    created_at INTEGER NOT NULL,          -- unix epoch ms
    updated_at INTEGER NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0   -- 0 = active, 1 = archived (soft delete)
);

CREATE INDEX IF NOT EXISTS idx_sessions_updated ON sessions(archived, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    
    -- Cache telemetry (from DeepSeek usage field on assistant responses)
    cache_hit_tokens INTEGER,             -- only set for assistant rows
    cache_miss_tokens INTEGER,            -- only set for assistant rows
    output_tokens INTEGER,                -- only set for assistant rows
    
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_session_time ON messages(session_id, created_at);
```

### Write patterns

All writes go through these functions. Use parameterized queries; never string-format SQL.

```python
def create_session(conn, session_id: str) -> None:
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, NULL, ?, ?)",
        (session_id, now, now),
    )
    conn.commit()

def append_message(conn, session_id: str, role: str, content: str,
                   cache_hit: int = None, cache_miss: int = None, output: int = None) -> int:
    now = int(time.time() * 1000)
    cur = conn.execute(
        """INSERT INTO messages 
           (session_id, role, content, created_at, cache_hit_tokens, cache_miss_tokens, output_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, role, content, now, cache_hit, cache_miss, output),
    )
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (now, session_id),
    )
    conn.commit()
    return cur.lastrowid

def load_history(conn, session_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,),
    ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in rows]
```

**Connection handling**: open one `sqlite3.Connection` per request, close at end. Or use a connection pool if framework supports it. Do NOT share connections across threads without proper locking.

---

## Part 2: Prompt assembly — cache-optimized

### The hard rule

**Everything before the current turn's user message must be byte-identical to the previous request in this session.** Any character that changes between turns kills the cache for everything after it.

### Three-layer structure

```python
def build_messages(session_history: list[dict], new_user_msg: str, 
                   dynamic_state: dict = None) -> list[dict]:
    """
    Layer 1: STATIC system prompt — same for every request, every session.
    Layer 2: STABLE conversation history — append-only, never modified.
    Layer 3: DYNAMIC content — only in the current user message, at the end.
    """
    messages = [
        {"role": "system", "content": REGGIA_SYSTEM_PROMPT},
    ]
    
    messages.extend(session_history)  # append-only; no edits to past turns
    
    # All variable content goes here, at the very end
    user_content = new_user_msg
    if dynamic_state:
        # Format dynamic state cleanly so it parses well, but place it BEFORE user_msg
        # within the same final user message (this is fine — the cache split happens
        # at message boundaries, not inside a message)
        state_str = format_dynamic_state(dynamic_state)
        user_content = f"{state_str}\n\n{new_user_msg}"
    
    messages.append({"role": "user", "content": user_content})
    return messages
```

### REGGIA_SYSTEM_PROMPT — what MUST NOT appear here

- ❌ Current date/time
- ❌ Session ID, user ID, request ID
- ❌ Current Active Items list (this varies as items change)
- ❌ Random seeds, UUIDs, timestamps of any kind
- ❌ Anything pulled from Notion at request time

### REGGIA_SYSTEM_PROMPT — what SHOULD appear here

- ✅ Reggia's persona description
- ✅ Tool/endpoint descriptions (URL patterns, not actual data)
- ✅ Routing rules (when to read which Reggia domain)
- ✅ Sensitivity handling rules
- ✅ Output style guidelines

This content is identical across every session and every turn → DeepSeek caches the entire system prompt prefix once, every subsequent request hits.

### Reggia state injection — DO NOT preload, let the agent fetch

Wrong:

```python
# ❌ This kills cache every turn because items change frequently
system_prompt = f"You are Reggia. Current active items: {fetch_active_items()}"
```

Right:

```python
# ✅ Tell the agent how to fetch, don't fetch for it
system_prompt = """You are Reggia. 
To check the user's active items, call: GET http://localhost:8000/reggia/items?status=active
To read long-term context, call: GET http://localhost:8000/reggia/longterm/{domain}
..."""
```

The agent uses tools (via Claude Code's bash/curl permission) to fetch Reggia state only when relevant. This is both cache-friendly and token-efficient — most turns won't need full Reggia state.

### Dynamic state that IS legitimately per-request

Things like current timestamp, if needed at all, go in the current user message:

```python
user_content = f"[Now: {datetime.now().isoformat()}]\n\n{user_msg}"
```

This is fine because the variation is in the LAST message, after all cacheable prefix.

---

## Part 3: HTTP endpoints

```
POST   /sessions                       → create new session, return session_id
GET    /sessions                       → list all non-archived sessions (id, title, updated_at)
GET    /sessions/{id}                  → get session metadata + full message history
DELETE /sessions/{id}                  → soft delete (set archived=1)
POST   /sessions/{id}/chat             → send message, SSE stream response
POST   /sessions/{id}/title            → manually rename
GET    /stats/cache                    → aggregate cache hit rate over last 7 days
```

### POST /sessions/{id}/chat — the main flow

```python
@app.post("/sessions/{session_id}/chat")
async def chat(session_id: str, payload: ChatRequest):
    # 1. Load history from SQLite (single read)
    history = load_history(conn, session_id)
    
    # 2. Build messages with cache-optimized structure
    messages = build_messages(history, payload.message)
    
    # 3. Persist user message BEFORE spawning subprocess
    #    (so if the subprocess crashes, we don't lose user input)
    append_message(conn, session_id, "user", payload.message)
    
    # 4. Spawn CC subprocess with isolated config dir
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = "/path/to/reggia/.claude-chat"
    
    proc = subprocess.Popen(
        ["claude", "--output-format", "stream-json", "--verbose", "-p", 
         serialize_messages_as_prompt(messages)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    
    # 5. Stream response back to client via SSE, accumulate full text
    async def event_stream():
        full_response = []
        usage = {}
        for line in proc.stdout:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
                # Extract text deltas to accumulate
                if msg.get("type") == "assistant":
                    full_response.append(extract_text(msg))
                # Extract usage from result message
                elif msg.get("type") == "result":
                    usage = msg.get("usage", {})
                yield f"data: {json.dumps(msg)}\n\n"
            except json.JSONDecodeError:
                continue
        
        # 6. After stream ends, persist assistant message + cache stats
        append_message(
            conn, session_id, "assistant", "".join(full_response),
            cache_hit=usage.get("prompt_cache_hit_tokens"),
            cache_miss=usage.get("prompt_cache_miss_tokens"),
            output=usage.get("completion_tokens"),
        )
        
        # 7. If this is session's first turn, async-generate title
        if len(history) == 0:
            asyncio.create_task(generate_title(session_id, payload.message))
    
    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

### Title generation (background)

After first user message, spawn a separate lightweight call:

```python
async def generate_title(session_id: str, first_message: str):
    # Use a cheap separate call (could be DeepSeek directly, no CC overhead)
    title = await deepseek_complete(
        f"用 6 个字以内总结这段对话主题，只返回标题本身：\n{first_message}"
    )
    conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title.strip(), session_id))
    conn.commit()
```

---

## Part 4: Cache hit verification

After implementation, verify cache behavior:

```bash
# Send same first message twice in two new sessions, check stats
curl -X POST /sessions -d '{}'  # create session A
curl -X POST /sessions/A/chat -d '{"message": "hello"}'

curl -X POST /sessions -d '{}'  # create session B
curl -X POST /sessions/B/chat -d '{"message": "hello"}'

# Check stats — session B's first turn should show high cache_hit_tokens
# (the entire system prompt should hit, since it's identical across sessions)
curl /stats/cache
```

Expected behavior:
- First request in entire DB lifetime: 0% hit (cold cache)
- Second request with same system prompt: ~100% hit on system prompt portion
- Multi-turn within a session: hit rate climbs with each turn (history accumulates as cached prefix)

If observed hit rates are much lower than expected, the most likely cause is dynamic content sneaking into the system prompt or early messages. Audit `build_messages` and `REGGIA_SYSTEM_PROMPT`.

---

## Part 5: What NOT to implement (yet)

- ❌ Conversation summarization for long sessions — adds complexity, breaks cache. Just let users start new sessions.
- ❌ Cross-session memory linking — out of scope.
- ❌ Streaming JSON input to CC (multi-turn within one process) — kept simple with spawn-per-request.
- ❌ Connection pooling for SQLite — overkill for personal use, one-connection-per-request is fine.

## Files to create/modify

```
reggia/
├── backend/
│   ├── db.py              ← NEW: SQLite schema + read/write functions
│   ├── prompts.py         ← NEW: REGGIA_SYSTEM_PROMPT + build_messages
│   ├── sessions.py        ← NEW: session CRUD endpoints
│   ├── chat.py            ← MODIFY: integrate session_id + history + cache logging
│   └── main.py            ← MODIFY: register new routes, init db on startup
├── .claude-chat/
│   └── CLAUDE.md          ← already exists, ensure no dynamic content
└── reggia.db              ← NEW: created on first run
```

## Acceptance criteria

1. POST /sessions creates a new session, returns UUID
2. POST /sessions/{id}/chat persists user message, streams response, persists assistant message with cache stats
3. GET /sessions/{id} returns full history in chronological order
4. Two new sessions sending identical first message: second one shows ≥80% prompt_cache_hit_tokens on system prompt portion
5. Within one session, turn 3's cache hit ratio > turn 2's > turn 1's
6. Killing the backend mid-stream does not corrupt SQLite (user message persisted, assistant message just missing — acceptable, user can retry)