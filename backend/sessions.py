import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from . import config, db, prompts

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

    session = db.get_session(session_id)
    if not session:
        db.create_session(session_id)

    history = db.load_history(session_id)
    db.append_message(session_id, "user", prompt)

    full_prompt = prompts.build_chat_prompt(history, prompt)
    log_file = LOG_DIR / f"chat_{session_id}.jsonl"

    args = [
        "claude",
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "acceptEdits",
        "--model", model,
        "-p", full_prompt,
    ]
    if session_id in sessions_map:
        args.insert(4, "--resume")
        args.insert(5, sessions_map[session_id])

    if config.CC_MODE == "docker":
        args = ["docker", "exec", "-i", "reggia-cc"] + args

    # Force unbuffered stdout so streaming events arrive line-by-line.
    # Without this, the child process may batch output in 4–8 KB blocks
    # and the frontend gets nothing until the response is complete.
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    kwargs = dict(
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    if config.CC_MODE != "docker":
        kwargs["cwd"] = str(config.CHAT_WORKSPACE)

    proc = await asyncio.create_subprocess_exec(*args, **kwargs)
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
            asyncio.create_task(_generate_title(session_id, prompt))

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

async def _generate_title(session_id: str, first_message: str):
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "--output-format", "stream-json",
            "--verbose",
            "--model", config.CHAT_CONFIG["default_model"],
            "-p", prompts.title_prompt(first_message),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(config.CHAT_WORKSPACE),
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
