"""
Lightweight agent loop: DeepSeek tool-calling + SSE streaming.

Replaces the Docker/Claude Code subprocess with a direct while-loop around
the DeepSeek chat API. The "agent" is just:

    while model returns tool_calls:
        execute them → feed results back → loop
    stream text tokens to frontend.

Transport is httpx (already a project dependency) — no extra SDK needed.
"""

from __future__ import annotations

import json
import os
import re
from typing import AsyncGenerator

import httpx

from . import db, longterm_db, prompts

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEEPSEEK_BASE = "https://api.deepseek.com"
CHAT_URL = f"{DEEPSEEK_BASE}/chat/completions"

DEFAULT_MODEL = "deepseek-v4-pro"

MAX_TOOL_ROUNDS = 8  # safety valve — prevent infinite loops

# ---------------------------------------------------------------------------
# System prompt (roughly mirrors chat_workspace/CLAUDE.md.template)
# ---------------------------------------------------------------------------

def _user_name() -> str:
    return os.environ.get("USER_NAME", "").strip() or "the user"


def _system_prompt(web_search: bool = False) -> str:
    """Build the system prompt at call time so a USER_NAME change (via /settings)
    takes effect without a restart, and so the web-search section only appears
    when the tool is actually offered. Roughly mirrors
    chat_workspace/CLAUDE.md.template."""
    name = _user_name()

    tool_list = (
        "reggia_index, reggia_longterm_index, reggia_longterm_read, "
        "reggia_items_list, reggia_item_detail"
    )
    web_block = ""
    if web_search:
        tool_list += ", web_search"
        web_block = (
            "\n\n# Web search\n"
            "web_search is enabled this turn. Use it only for current/external "
            f"facts not in Reggia (news, live prices, docs). For anything about "
            f"{name}'s own context, prefer the Reggia tools."
        )

    return f"""You are chatting with {name} through Reggia, {name}'s personal knowledge base frontend.

# Preferences
Direct, no flattery. Hand decisions to {name} rather than making them on your behalf.
Brief responses by default; expand only when a question genuinely needs it.

# When to query Reggia
Read long-term memory and active items whenever the task touches personal context.
Skip for generic technical questions or quick greetings.

# Two-level tool strategy — ALWAYS use L1 first

Reggia tools are split into Level 1 (cheap index, ~100 tokens) and Level 2
(full content, 5–30K tokens). The rule is simple:

  1. Call the L1 tool first to see what's available.
  2. Read the L1 result, decide which specific pages/items are relevant.
  3. Call the L2 tool ONLY for the pages/items you actually need.
  4. Never call L2 without L1 first. Never bulk-fetch "to be safe."

## First message of a session

Always start with `reggia_index` (the routing guide). Then use L1 tools to
survey available long-term pages and active items. Then pull only what's relevant
via L2 tools. Typical flow: index → longterm_index → longterm_read(one domain)
+ items_list → item_detail(one item).

## Subsequent messages

Skip the index (it's static per session). But still use L1 tools before L2 —
the items list may have changed, and the long-term TOC helps you decide
which page to read.

# Sensitivity
- agent-readable: use freely
- contextual: reasoning only, never surface in third-party output
- private: skip entirely

# Updating
Never silently mutate. Propose changes explicitly and call append/update endpoints
only after {name} confirms.{web_block}

Long-term memory: reggia_longterm_index (L1) + reggia_longterm_read (L2).
Short-term memory: reggia_items_list (L1) + reggia_item_detail (L2).
Available tools: {tool_list}. Always prefer L1 over L2."""

# ---------------------------------------------------------------------------
# Tool definitions (OpenAI function-calling format)
# ---------------------------------------------------------------------------

REGGIA_TOOLS = [
    # -------------------------------------------------------------------
    # L0: Routing guide (always first in a session)
    # -------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "reggia_index",
            "description": (
                "Read the query routing guide — maps question types to which "
                "long-term pages and item queries to fetch. Always call this "
                "FIRST in a new conversation, before any other Reggia tool."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    # -------------------------------------------------------------------
    # L1: Long-term memory index (cheap — just section headings)
    # -------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "reggia_longterm_index",
            "description": (
                "Get a TABLE OF CONTENTS for all four long-term memory pages. "
                "Returns section headings (## / ###) grouped by domain. "
                "CHEAP — ~100 tokens. Always call this BEFORE reggia_longterm_read "
                "so you know which domain and which sections are relevant. "
                "Do NOT skip this and go straight to reggia_longterm_read."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": (
                            "Optional. If omitted, returns TOC for all 4 domains. "
                            "If provided, returns TOC for just that one domain."
                        ),
                        "enum": ["work", "research", "intellectual", "personal"],
                    },
                },
            },
        },
    },
    # -------------------------------------------------------------------
    # L2: Long-term memory full page (expensive — full Markdown)
    # -------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "reggia_longterm_read",
            "description": (
                "Read the FULL content of one long-term memory page. "
                "EXPENSIVE — 5–30K tokens. Only call this AFTER consulting "
                "reggia_longterm_index to confirm this page is relevant. "
                "Read at most ONE domain per turn unless the question genuinely "
                "spans multiple domains."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "domain": {
                        "type": "string",
                        "description": "Which page to read in full",
                        "enum": ["work", "research", "intellectual", "personal"],
                    },
                },
                "required": ["domain"],
            },
        },
    },
    # -------------------------------------------------------------------
    # L1: Active items list (cheap — names + status only, no notes)
    # -------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "reggia_items_list",
            "description": (
                "Get a SUMMARY LIST of active items — name, priority, status, "
                "domain, due_date, is_past_due only. NO notes or full details. "
                "CHEAP — ~200 tokens for a typical list. Always call this BEFORE "
                "reggia_item_detail so you know which items exist and which are "
                "relevant to the user's question. Do NOT skip this and go straight "
                "to reggia_item_detail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "pending", "completed", "dropped"],
                        "description": "Filter by status. Default: active if omitted.",
                    },
                    "domain": {
                        "type": "string",
                        "enum": ["research", "application", "work", "admin", "writing", "personal"],
                        "description": "Filter by domain. Prefer this over status=active to reduce noise.",
                    },
                },
            },
        },
    },
    # -------------------------------------------------------------------
    # L2: Active item detail (expensive — full notes + metadata)
    # -------------------------------------------------------------------
    {
        "type": "function",
        "function": {
            "name": "reggia_item_detail",
            "description": (
                "Read the FULL detail of ONE active item — including notes, "
                "sensitivity tags, full dates. Use only AFTER reggia_items_list "
                "to pick the specific item(s) you need. Call once per relevant item."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "The item ID from reggia_items_list",
                    },
                },
                "required": ["item_id"],
            },
        },
    },
]

# ---------------------------------------------------------------------------
# Tool #1: Web search (博查 / Bocha). Only offered to the model when the
# composer toggle is ON (run(..., web_search=True)).
# ---------------------------------------------------------------------------
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web. The query should be concise search keywords "
            "(2-6 words), not a full sentence. "
            "Example: '博查API 定价' not '博查API的定价是多少钱？'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Concise search keywords (2-6 words).",
                },
            },
            "required": ["query"],
        },
    },
}
# ---------------------------------------------------------------------------
# Tool executors
# ---------------------------------------------------------------------------

# -- L0: Routing guide -------------------------------------------------------

async def _exec_reggia_index() -> str:
    """Return the index page (query routing guide)."""
    row = longterm_db.get("index")
    if not row or not (row.get("content_md") or "").strip():
        return "[Reggia index is empty — not synced yet. Fall back to the routing table in your instructions.]"
    return row["content_md"]


# -- L1: Long-term memory TOC ------------------------------------------------

def _extract_headings(markdown: str) -> list[str]:
    """Extract ## and ### headings from Markdown content.

    Returns a flat list like ['## Work & Academic', '### Current Courses', ...]
    so the model sees the document structure without the body text.
    """
    headings: list[str] = []
    for line in markdown.split("\n"):
        stripped = line.strip()
        if stripped.startswith("### "):
            headings.append(stripped)
        elif stripped.startswith("## "):
            headings.append(stripped)
        elif stripped.startswith("# ") and not stripped.startswith("## "):
            headings.append(stripped)
    return headings


async def _exec_reggia_longterm_index(domain: str | None = None) -> str:
    """Return a table of contents for long-term memory pages.

    - No domain: returns TOC for all 4 domains (~100 tokens)
    - With domain: returns headings for just that domain
    """
    domains = [domain] if domain else ["work", "research", "intellectual", "personal"]

    result: dict[str, list[str]] = {}
    for d in domains:
        row = longterm_db.get(d)
        if not row:
            result[d] = ["[not synced yet]"]
            continue
        content = row.get("content_md") or ""
        headings = _extract_headings(content)
        if not headings:
            headings = ["[page is empty]"]
        result[d] = headings

    return json.dumps(result, ensure_ascii=False, indent=2)


# -- L2: Long-term memory full page ------------------------------------------

async def _exec_reggia_longterm_read(domain: str) -> str:
    """Return the full Markdown content of one long-term memory page."""
    row = longterm_db.get(domain)
    if not row:
        return json.dumps({"error": f"unknown domain: {domain}"})
    if not (row.get("content_md") or "").strip() and not row.get("synced_at"):
        return json.dumps({
            "error": "not synced yet",
            "hint": "Tell the user to run POST /reggia/sync/pull from the frontend.",
        })
    return row["content_md"] or "[page is empty]"


# -- L1: Active items summary list -------------------------------------------

async def _exec_reggia_items_list(status: str = "active", domain: str = None) -> str:
    """Return a lightweight summary of items: name, priority, status, domain,
    due_date, is_past_due. No notes, no full metadata. ~200 tokens for a typical list.
    """
    items = db.list_items(status=status, domain=domain)
    slim = [
        {
            "id": it["id"],
            "name": it["name"],
            "domain": it["domain"],
            "priority": it["priority"],
            "status": it["status"],
            "due_date": it["due_date"],
            "days_until_due": it["days_until_due"],
            "is_past_due": it["is_past_due"],
            "sensitivity": it["sensitivity"],
        }
        for it in items
    ]
    return json.dumps(slim, ensure_ascii=False)


# -- L2: Active item full detail ---------------------------------------------

async def _exec_reggia_item_detail(item_id: str) -> str:
    """Return the FULL detail of a single item, including notes and all metadata."""
    # list_items doesn't support single-id lookup, so we scan.
    # For a production version, you might add db.get_item(item_id) to db.py.
    all_items = db.list_items()  # no filter — need to find by id
    for it in all_items:
        if it["id"] == item_id:
            return json.dumps(it, ensure_ascii=False, indent=2)
    return json.dumps({"error": f"item not found: {item_id}"})


# -- Tool #1: Web search (博查 / Bocha) ---------------------------------------

BOCHA_SEARCH_URL = "https://api.bocha.cn/v1/web-search"


async def _exec_web_search(query: str) -> str:
    """Search the web via 博查 (Bocha) API. Returns formatted results for the model."""
    api_key = os.environ.get("BOCHA_API_KEY", "")
    if not api_key:
        return json.dumps({"error": "BOCHA_API_KEY not set"})

    payload = {
        "query": query,
        "summary": True,
        "freshness": "noLimit",
        "count": 8,
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                BOCHA_SEARCH_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )

            if resp.status_code != 200:
                body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                return json.dumps({
                    "error": f"Bocha API {resp.status_code}",
                    "message": body.get("message", resp.text[:200]),
                })

            data = resp.json()

            if data.get("code") != 200:
                return json.dumps({
                    "error": f"Bocha error {data.get('code')}",
                    "message": data.get("msg", "unknown"),
                })

            pages = (data.get("data") or {}).get("webPages", {}).get("value") or []
            if not pages:
                return json.dumps({"results": [], "note": "No results found."})

            # Format results compactly for the model
            results = []
            for i, p in enumerate(pages, 1):
                content = p.get("summary") or p.get("snippet") or ""
                entry = f"[{i}] {p.get('name', 'Untitled')}"
                if p.get("siteName"):
                    entry += f" ({p['siteName']})"
                if p.get("datePublished"):
                    entry += f" [{p['datePublished'][:10]}]"
                entry += f"\n{content}\nURL: {p.get('url', '')}"
                results.append(entry)

            return "\n\n".join(results)

    except httpx.TimeoutException:
        return json.dumps({"error": "Bocha API timeout (30s)"})
    except Exception as e:
        return json.dumps({"error": f"web_search failed: {str(e)}"})


# -- Registry ----------------------------------------------------------------

TOOL_EXECUTORS = {
    "reggia_index": _exec_reggia_index,
    "reggia_longterm_index": _exec_reggia_longterm_index,
    "reggia_longterm_read": _exec_reggia_longterm_read,
    "reggia_items_list": _exec_reggia_items_list,
    "reggia_item_detail": _exec_reggia_item_detail,
    "web_search": _exec_web_search,
}

# ---------------------------------------------------------------------------
# DeepSeek V4 quirk: ~11% of tool calls come as plain text instead of
# structured tool_calls. This fallback parser catches them.
# ---------------------------------------------------------------------------

_TOOL_CALL_JSON_RE = re.compile(
    r'\{\s*"name"\s*:\s*"(reggia_index|reggia_longterm_index|reggia_longterm_read|reggia_items_list|reggia_item_detail|web_search)"\s*,\s*"arguments"\s*:\s*(\{.*?\})\s*\}',
    re.DOTALL,
)

def _parse_fallback_tool_calls(content: str) -> list[dict]:
    """Try to extract tool calls from plain-text content.

    Returns a list of {"name": str, "arguments": dict} dicts, or empty list.
    """
    if not content:
        return []
    calls: list[dict] = []
    for m in _TOOL_CALL_JSON_RE.finditer(content):
        try:
            calls.append({
                "name": m.group(1),
                "arguments": json.loads(m.group(2)),
            })
        except json.JSONDecodeError:
            continue
    return calls


# ---------------------------------------------------------------------------
# The agent loop
# ---------------------------------------------------------------------------

async def run(
    history: list[dict],
    user_message: str,
    model: str = DEFAULT_MODEL,
    web_search: bool = False,
) -> AsyncGenerator[str, None]:
    """Run the tool-calling loop. Yields SSE-formatted strings.

    `web_search` toggles whether the web_search tool is offered to the model
    (driven by the composer toggle in the frontend).

    Usage from sessions.py::

        async for sse_line in agent_loop.run(history, prompt, model, web_search=web_search):
            yield sse_line

    SSE event types emitted:
      - {"type": "text_delta", "text": "..."}       streaming text
      - {"type": "tool_call", "name": "...", "args": {...}}
      - {"type": "tool_result", "name": "...", "ok": true/false}
      - {"type": "result", "usage": {...}}
      - {"type": "error", "message": "..."}
    """

    # ---- Build messages ---------------------------------------------------
    messages: list[dict] = [
        {"role": "system", "content": _system_prompt(web_search)},
    ]

    # Append existing conversation history
    for msg in history:
        role = msg["role"]
        if role not in ("user", "assistant"):
            continue
        messages.append({"role": role, "content": msg["content"]})

    # Append the new user message
    messages.append({"role": "user", "content": user_message})

    # Tools offered this run. Reggia memory tools are always available; web
    # search is opt-in via the composer toggle.
    tools = REGGIA_TOOLS + ([WEB_SEARCH_TOOL] if web_search else [])

    # ---- Tool-calling loop ------------------------------------------------
    tool_round = 0

    async with httpx.AsyncClient(timeout=120) as client:
        while tool_round < MAX_TOOL_ROUNDS:
            tool_round += 1

            # Build the API request
            body = {
                "model": model,
                "messages": messages,
                "stream": True,
            }
            if tools:
                body["tools"] = tools
                body["tool_choice"] = "auto"

            # ---- Stream from DeepSeek --------------------------------------
            collected_content: list[str] = []
            collected_tool_calls: list[dict] = []
            finish_reason: str | None = None
            usage: dict | None = None

            async with client.stream(
                "POST",
                CHAT_URL,
                headers={
                    "Authorization": f"Bearer {os.environ.get('DEEPSEEK_API_KEY', '')}",
                    "Content-Type": "application/json",
                },
                json=body,
            ) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    yield _sse("error", {
                        "message": f"DeepSeek API error {response.status_code}: {error_text.decode()[:500]}",
                    })
                    return

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data: "):
                        continue
                    data_str = line[6:]
                    if data_str.strip() == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choice = (chunk.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    finish_reason = finish_reason or choice.get("finish_reason")

                    # --- Text content ---
                    if delta.get("content"):
                        text = delta["content"]
                        collected_content.append(text)
                        yield _sse("text_delta", {"text": text})

                    # --- Tool calls (streamed as deltas; accumulate) ---
                    for tc_delta in delta.get("tool_calls") or []:
                        idx = tc_delta.get("index", 0)
                        # Grow the accumulator list if needed
                        while len(collected_tool_calls) <= idx:
                            collected_tool_calls.append({
                                "id": "",
                                "function": {"name": "", "arguments": ""},
                            })
                        tc = collected_tool_calls[idx]
                        if tc_delta.get("id"):
                            tc["id"] = tc_delta["id"]
                        func = tc_delta.get("function") or {}
                        if func.get("name"):
                            tc["function"]["name"] += func["name"]
                        if func.get("arguments"):
                            tc["function"]["arguments"] += func["arguments"]

                    # --- Usage (usually in last chunk) ---
                    if chunk.get("usage"):
                        usage = chunk["usage"]

            # ---- Process response ------------------------------------------
            full_text = "".join(collected_content)

            # Case 1: normal tool calls
            if finish_reason == "tool_calls" and collected_tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": full_text or None,
                    "tool_calls": [
                        {
                            "id": tc["id"],
                            "type": "function",
                            "function": tc["function"],
                        }
                        for tc in collected_tool_calls
                    ],
                })

                for tc in collected_tool_calls:
                    name = tc["function"]["name"]
                    try:
                        args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        args = {}

                    yield _sse("tool_call", {"name": name, "args": args})

                    executor = TOOL_EXECUTORS.get(name)
                    if executor:
                        try:
                            result = await executor(**args)
                            ok = True
                        except Exception as e:
                            result = json.dumps({"error": str(e)})
                            ok = False
                    else:
                        result = json.dumps({"error": f"unknown tool: {name}"})
                        ok = False

                    yield _sse("tool_result", {"name": name, "ok": ok})

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })

                # Loop back — model sees tool results and decides next step
                continue

            # Case 2: model stopped but emitted tool calls as plain text (bug)
            fallback_calls = _parse_fallback_tool_calls(full_text) if full_text else []
            if finish_reason == "stop" and fallback_calls and not collected_tool_calls:
                messages.append({
                    "role": "assistant",
                    "content": full_text,
                })

                for fc in fallback_calls:
                    name = fc["name"]
                    args = fc.get("arguments", {})

                    yield _sse("tool_call", {"name": name, "args": args, "fallback": True})

                    executor = TOOL_EXECUTORS.get(name)
                    if executor:
                        try:
                            result = await executor(**args)
                            ok = True
                        except Exception as e:
                            result = json.dumps({"error": str(e)})
                            ok = False
                    else:
                        result = json.dumps({"error": f"unknown tool: {name}"})
                        ok = False

                    yield _sse("tool_result", {"name": name, "ok": ok})

                    messages.append({
                        "role": "tool",
                        "tool_call_id": f"fallback_{name}",
                        "content": result,
                    })

                continue

            # Case 3: normal stop — model is done, streamed text already sent
            # Append the full assistant response to messages for context
            if full_text:
                messages.append({
                    "role": "assistant",
                    "content": full_text,
                })

            yield _sse("result", {
                "finish_reason": finish_reason,
                "usage": usage,
                "tool_rounds": tool_round,
            })
            return

        # ---- Exceeded max rounds -------------------------------------------
        yield _sse("error", {
            "message": f"Agent exceeded maximum tool rounds ({MAX_TOOL_ROUNDS}). Stopping to prevent infinite loop.",
        })


# ---------------------------------------------------------------------------
# One-shot title generation (no tools, no streaming)
# ---------------------------------------------------------------------------

async def generate_title(first_message: str, model: str = DEFAULT_MODEL) -> str:
    """Generate a short session title from the first user message.

    Single non-streaming DeepSeek call, no tools. Returns "" on any failure so
    the caller can silently skip renaming.
    """
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompts.title_prompt(first_message)}],
        "stream": False,
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                CHAT_URL,
                headers={
                    "Authorization": f"Bearer {os.environ.get('DEEPSEEK_API_KEY', '')}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            if resp.status_code != 200:
                return ""
            data = resp.json()
            content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            return content.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sse(event_type: str, payload: dict | None = None) -> str:
    """Format a single SSE line."""
    msg = {"type": event_type}
    if payload:
        msg.update(payload)
    return f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
