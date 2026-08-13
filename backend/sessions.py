import asyncio
import json
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from . import agent_loop, config, db, prompts

router = APIRouter(prefix="/sessions", tags=["sessions"])

LOG_DIR = Path(__file__).resolve().parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

sessions_map: dict[str, str] = {}  # frontend_session_id → cc_session_id


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

@router.post("")
async def create_session():
    sid = str(uuid.uuid4())
    db.create_session(sid)
    return {"id": sid}


@router.get("")
async def list_sessions():
    return db.list_sessions()


@router.get("/search")
async def search_sessions(q: str = ""):
    query = q.strip()
    if not query:
        return []
    return db.search_sessions(query)


@router.get("/{session_id}")
async def get_session(session_id: str):
    s = db.get_session(session_id)
    if not s:
        raise HTTPException(status_code=404, detail="session not found")
    return s


@router.delete("/{session_id}")
async def delete_session(session_id: str):
    db.archive_session(session_id)
    return {"ok": True}


@router.post("/{session_id}/title")
async def rename_session(session_id: str, payload: dict):
    title = payload.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title is required")
    db.set_title(session_id, title)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------

@router.post("/{session_id}/chat")
async def session_chat(session_id: str, payload: dict):
    prompt = payload.get("prompt", "")
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")

    model = payload.get("model") or config.CHAT_CONFIG["default_model"]
    if model not in config.CHAT_CONFIG["models"]:
        raise HTTPException(status_code=400, detail=f"unknown model: {model}")

    thinking = payload.get("thinking", True)
    if not isinstance(thinking, bool):
        raise HTTPException(status_code=400, detail="thinking must be a boolean")

    thinking_effort = payload.get("thinking_effort", "high")
    if thinking_effort not in agent_loop.THINKING_EFFORTS:
        allowed = ", ".join(sorted(agent_loop.THINKING_EFFORTS))
        raise HTTPException(status_code=400, detail=f"thinking_effort must be one of: {allowed}")

    session = db.get_session(session_id)
    if not session:
        db.create_session(session_id)

    history = db.load_history(session_id)
    db.append_message(session_id, "user", prompt)

    # Engine swap: the lightweight in-process DeepSeek loop (default) vs. the
    # legacy Claude Code container. See config.CHAT_ENGINE.
    if config.CHAT_ENGINE == "docker":
        return await _chat_docker(session_id, prompt, model, history)

    web_search = bool(payload.get("web_search", False))
    return _chat_agent(
        session_id, prompt, model, history, web_search,
        thinking=thinking, thinking_effort=thinking_effort,
    )


def _chat_agent(
    session_id, prompt, model, history, web_search,
    *, thinking=True, thinking_effort="high",
):
    """Lightweight engine: stream one turn from the in-process DeepSeek loop
    (backend/agent_loop.py). No Docker, no Claude Code subprocess.

    agent_loop.run() is a pure SSE generator; here we forward each line to the
    client and also sniff it to accumulate the assistant text + token usage for
    persistence.
    """
    log_file = LOG_DIR / f"chat_{session_id}.jsonl"

    async def event_stream():
        full_response = []
        tool_calls_meta = []
        usage = {}

        with open(log_file, "a") as lf:
            async for sse_line in agent_loop.run(
                history, prompt, model,
                web_search=web_search,
                thinking=thinking,
                reasoning_effort=thinking_effort,
            ):
                lf.write(sse_line)
                lf.flush()
                try:
                    msg = json.loads(sse_line[6:])  # strip the "data: " prefix
                    mtype = msg.get("type")
                    if mtype == "text_delta":
                        full_response.append(msg.get("text", ""))
                    elif mtype == "tool_call":
                        tc = {"tool": msg.get("name", "")}
                        args = msg.get("args") or {}
                        if tc["tool"] in ("reggia_longterm_read", "reggia_longterm_index"):
                            tc["page"] = args.get("domain", "")
                        elif tc["tool"] == "reggia_item_detail":
                            tc["page"] = args.get("item_id", "")
                        elif tc["tool"] == "web_search":
                            tc["query"] = args.get("query", "")
                        tool_calls_meta.append(tc)
                    elif mtype == "result":
                        u = msg.get("usage") or {}
                        usage = {
                            "cache_hit": u.get("prompt_cache_hit_tokens"),
                            "cache_miss": u.get("prompt_cache_miss_tokens"),
                            "output": u.get("completion_tokens"),
                        }
                except (json.JSONDecodeError, IndexError):
                    pass
                yield sse_line

        full_text = "".join(full_response)
        tc_json = json.dumps(tool_calls_meta) if tool_calls_meta else None
        db.append_message(
            session_id, "assistant", full_text,
            cache_hit=usage.get("cache_hit"),
            cache_miss=usage.get("cache_miss"),
            output=usage.get("output"),
            tool_calls=tc_json,
        )

        if len(history) == 0 and full_text.strip():
            asyncio.create_task(_generate_title(session_id, prompt, model))

    return StreamingResponse(event_stream(), media_type="text/event-stream")


async def _chat_docker(session_id, prompt, model, history):
    """Legacy engine: stream one turn from the Claude Code container (reggia-cc)."""
    full_prompt = prompts.build_chat_prompt(history, prompt)
    log_file = LOG_DIR / f"chat_{session_id}.jsonl"

    args = [
        "docker", "exec", "-i", "reggia-cc",
        "claude",
        "--output-format", "stream-json",
        # Emit token-level deltas (content_block_delta) as they're generated
        # instead of one complete message per agent step. Without this the
        # frontend receives whole text blocks at once and can't stream-render.
        # Only valid together with --output-format stream-json.
        "--include-partial-messages",
        "--verbose",
        "--permission-mode", "acceptEdits",
        "--model", model,
        "-p", full_prompt,
    ]
    if session_id in sessions_map:
        # Resume the CC-side session. Flag order is irrelevant to the CLI, so
        # slot it right after the `claude` token.
        i = args.index("claude") + 1
        args[i:i] = ["--resume", sessions_map[session_id]]

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if proc.stdin:
        proc.stdin.close()

    async def event_stream():
        first_line = True
        full_response = []
        usage = {}

        with open(log_file, "a") as lf:
            async for line in proc.stdout:
                text = line.decode("utf-8")
                if not text.strip():
                    continue
                lf.write(text)
                lf.flush()
                try:
                    msg = json.loads(text)
                    if first_line and msg.get("type") == "system" and msg.get("session_id"):
                        sessions_map[session_id] = msg["session_id"]
                        msg["reggia_session_id"] = session_id
                        first_line = False

                    if msg.get("type") == "assistant":
                        for block in msg.get("message", {}).get("content", []):
                            if block.get("type") == "text":
                                full_response.append(block["text"])
                    elif msg.get("type") == "result":
                        u = msg.get("usage", {})
                        usage = {
                            "cache_hit": u.get("prompt_cache_hit_tokens") or u.get("cache_read_input_tokens"),
                            "cache_miss": u.get("prompt_cache_miss_tokens") or u.get("cache_creation_input_tokens"),
                            "output": u.get("completion_tokens") or u.get("output_tokens"),
                        }

                    yield f"data: {json.dumps(msg)}\n\n"
                except json.JSONDecodeError:
                    continue

        await proc.wait()
        if proc.returncode != 0:
            stderr_data = await proc.stderr.read()
            stderr = stderr_data.decode("utf-8")
            with open(log_file, "a") as lf:
                lf.write(f'{{"type":"stderr","text":{json.dumps(stderr)}}}\n')
            yield f"data: {json.dumps({'type': 'error', 'message': stderr})}\n\n"

        full_text = "".join(full_response)
        db.append_message(
            session_id, "assistant", full_text,
            cache_hit=usage.get("cache_hit"),
            cache_miss=usage.get("cache_miss"),
            output=usage.get("output"),
        )

        if len(history) == 0 and full_text.strip():
            asyncio.create_task(_generate_title(session_id, prompt, model))

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Cache stats
# ---------------------------------------------------------------------------

@router.get("/stats/cache")
async def cache_stats():
    return db.get_cache_stats()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _generate_title(session_id: str, first_message: str, model: str | None = None):
    model = model or config.CHAT_CONFIG["default_model"]
    try:
        # Agent engine: one-shot DeepSeek call, no subprocess.
        if config.CHAT_ENGINE != "docker":
            title = await agent_loop.generate_title(first_message, model)
            if title:
                db.set_title(session_id, title[:50])
            return

        # Docker engine: the DeepSeek env (ANTHROPIC_BASE_URL/token) only exists
        # inside reggia-cc, so the CLI must run there too — running it on the
        # host would hit real Anthropic with no key and fail/hang.
        args = [
            "docker", "exec", "-i", "reggia-cc",
            "claude",
            "--output-format", "stream-json",
            "--verbose",
            "--model", model,
            "-p", prompts.title_prompt(first_message),
        ]
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if proc.stdin:
            proc.stdin.close()
        title = ""
        async for line in proc.stdout:
            text = line.decode("utf-8")
            if not text.strip():
                continue
            try:
                msg = json.loads(text)
                if msg.get("type") == "result":
                    title = msg.get("result", "").strip()
            except json.JSONDecodeError:
                continue
        await proc.wait()
        if title:
            db.set_title(session_id, title[:50])
    except Exception:
        pass
