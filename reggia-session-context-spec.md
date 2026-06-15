# Reggia Session Context Architecture Spec

## Overview

This document specifies how to update Reggia's session management to support:
1. Storing function call metadata alongside plain-text messages in SQLite
2. Constructing prompts for the DeepSeek V4 API with cache-optimal ordering
3. Using `<ctx/>` tags to prevent redundant memory page fetches

---

## 1. SQLite Schema Update

### Current Schema (before)

```sql
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,          -- 'user' | 'assistant'
    content TEXT NOT NULL,       -- plain text only
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Updated Schema

```sql
ALTER TABLE messages ADD COLUMN tool_calls TEXT DEFAULT NULL;
```

`tool_calls` stores a JSON array of tool call metadata. Only populated for assistant messages that involved function calls. Store **tool name + key parameters only**, not the full tool call payload or tool result content.

#### Example values

```json
-- assistant message that fetched two memory pages
[{"tool": "fetch_memory", "page": "skills.md"}, {"tool": "fetch_memory", "page": "projects/jarvis.md"}]

-- assistant message that used a different tool
[{"tool": "web_search", "query": "DeepSeek V4 API pricing"}]

-- assistant message with no tool calls
NULL
```

### Write Logic

When storing an assistant turn after the agent loop completes:
1. `content` ← final assistant response text only (strip all function call markup, tool results, thinking tokens, etc.)
2. `tool_calls` ← JSON array of `{"tool": "<name>", ...key_params}` for each tool call made during this turn. Set to `NULL` if no tools were called.

---

## 2. Prompt Construction for DeepSeek V4

### Context Layout

```
┌─────────────────────────────────────────────┐
│  1. System Prompt (fixed, never changes)    │  ← cache-stable prefix start
├─────────────────────────────────────────────┤
│  2. Session History (append-only)           │  ← grows each turn, prefix stays stable
│     - user messages: stored plain text      │
│     - assistant messages: stored plain text  │
│       + <ctx/> tag appended if tool_calls   │
│       is not NULL                           │
├─────────────────────────────────────────────┤
│  3. Current User Message                    │  ← new each turn
└─────────────────────────────────────────────┘
```

### Why This Order

DeepSeek V4 disk cache is prefix-matched. Cache hits require an exact match of a previously cached prefix unit. In a multi-turn conversation, each request appends to the previous one, so the entire prior conversation is a prefix match → cache hit from turn 2 onward. This is the "Example 1" pattern in DeepSeek's documentation.

**Critical rules for maximizing cache hits:**
- **Never modify or reorder historical turns.** Every turn, once stored, is immutable.
- **Never inject fetched memory page content into the middle of the history.** Memory pages are fetched via function calling within the current turn and are ephemeral — they appear in the live API exchange but are not persisted into session history.
- **System prompt must not change within a session.** If long-term memory content needs to be referenced, it is fetched on-demand via tool calls, not embedded in the system prompt.

### Constructing the `messages` Array

```python
def build_messages(session_id: str, current_user_message: str) -> list[dict]:
    messages = []

    # 1. System prompt (fixed)
    messages.append({
        "role": "system",
        "content": SYSTEM_PROMPT  # constant string, loaded once at startup
    })

    # 2. Session history from SQLite
    rows = db.execute(
        "SELECT role, content, tool_calls FROM messages "
        "WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,)
    ).fetchall()

    for role, content, tool_calls_json in rows:
        if role == "assistant" and tool_calls_json is not None:
            # Append <ctx/> tag to assistant content
            tool_calls = json.loads(tool_calls_json)
            ctx_sources = [tc["page"] for tc in tool_calls if tc.get("page")]
            if ctx_sources:
                content += f'\n<ctx src="{", ".join(ctx_sources)}"/>'
        messages.append({"role": role, "content": content})

    # 3. Current user message
    messages.append({
        "role": "user",
        "content": current_user_message
    })

    return messages
```

### Notes on Non-Memory Tool Calls

The `<ctx/>` tag is specifically for `fetch_memory` calls. Other tool calls (e.g., `web_search`, `execute_code`) are logged in `tool_calls` column for debugging/traceability but do **not** generate `<ctx/>` tags, because:
- Web search results are time-sensitive and should never be treated as "already fetched"
- Code execution results are ephemeral by nature

If other tools need dedup logic in the future, define separate tags (e.g., `<exec/>`, `<search/>`). Don't overload `<ctx/>`.

---

## 3. System Prompt: `<ctx/>` Tag Rule

Add the following block to the system prompt:

```
## Context Fetch Deduplication

Session history 中 assistant 消息末尾可能包含 <ctx/> 标签，
格式为 <ctx src="page1.md, page2.md"/>，
表示该轮回答已获取过这些 long-term memory page 的内容。

规则：
- 如果当前用户请求涉及的信息在历史 <ctx/> 标签中已出现过对应的 page，
  直接基于当时回答中的相关内容作答，不要重复调用 fetch_memory。
- 例外：用户明确要求"重新获取"、"刷新"、或"再看一下 xxx"时，重新 fetch。
- <ctx/> 标签仅用于 fetch_memory 的去重判断，不影响其他工具的使用。
```

---

## 4. Agent Loop Integration

### After Model Response

```python
def process_and_store_response(session_id: str, response: dict):
    """Called after the agent loop (including all tool calls) completes."""

    # Extract final assistant text (strip tool call markup)
    final_text = extract_plain_text(response)

    # Extract tool call metadata
    tool_calls = extract_tool_metadata(response)
    # Returns: [{"tool": "fetch_memory", "page": "skills.md"}, ...] or None

    # Store in SQLite
    db.execute(
        "INSERT INTO messages (session_id, role, content, tool_calls) VALUES (?, ?, ?, ?)",
        (session_id, "assistant", final_text,
         json.dumps(tool_calls) if tool_calls else None)
    )
```

### `extract_tool_metadata` Implementation

```python
def extract_tool_metadata(response: dict) -> list[dict] | None:
    """Extract minimal metadata from tool calls in the response."""
    metadata = []
    for block in response.get("content", []):
        if block.get("type") == "tool_use":
            entry = {"tool": block["name"]}
            # Add key params based on tool type
            if block["name"] == "fetch_memory":
                entry["page"] = block["input"].get("page")
            elif block["name"] == "web_search":
                entry["query"] = block["input"].get("query")
            # Add other tools as needed
            metadata.append(entry)
    return metadata if metadata else None
```

---

## 5. Cache Behavior Summary

| Scenario | Cache Behavior |
|----------|---------------|
| Normal multi-turn conversation | Turn N+1 cache-hits all of turns 1..N |
| User edits/retries a message | Cache miss from edit point onward; DeepSeek auto-detects common prefix after 2-3 requests and re-caches |
| System prompt changes between sessions | Full cache miss on first turn of new session (expected) |
| Memory page fetched via tool call | Tool result is ephemeral, not in stored history → no cache impact |
| `<ctx/>` tag appended to assistant message | Tiny token addition, becomes part of cached prefix for subsequent turns |

---

## 6. Migration Checklist

- [ ] Run `ALTER TABLE messages ADD COLUMN tool_calls TEXT DEFAULT NULL;`
- [ ] Update agent loop to call `extract_tool_metadata()` and store result
- [ ] Update `build_messages()` to append `<ctx/>` tags when constructing history
- [ ] Add context fetch dedup rule to system prompt
- [ ] Verify cache hit rates via `usage.prompt_cache_hit_tokens` in API responses (log this)
- [ ] Monitor for redundant `fetch_memory` calls in first few sessions — if still occurring, tighten system prompt wording
