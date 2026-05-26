import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, longterm_db, sessions as sessions_mod, settings as settings_mod, sync

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CHAT_CONFIG = config.CHAT_CONFIG

VALID_BLOCK_TYPES = {"paragraph", "bulleted_list_item", "heading_3", "numbered_list_item"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    longterm_db.init_db()
    # Materialize CLAUDE.md + skill files from templates so USER_NAME picks up
    # any change made via /settings between runs.
    settings_mod.render_chat_workspace()
    # Pull Notion in the background — never block boot on a slow Notion.
    asyncio.create_task(sync.pull_all_background(timeout_s=30))
    yield


app = FastAPI(title="Reggia", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Session management routes
app.include_router(sessions_mod.router)
app.include_router(settings_mod.router)


# ---------------------------------------------------------------------------
# Chat config
# ---------------------------------------------------------------------------

@app.get("/chat/config")
async def chat_config():
    return {
        "models": CHAT_CONFIG["models"],
        "default_model": CHAT_CONFIG["default_model"],
    }


# ---------------------------------------------------------------------------
# Reggia — longterm pages (SQLite-first; Notion via background sync)
# ---------------------------------------------------------------------------

def _serve_longterm(domain: str) -> PlainTextResponse:
    row = longterm_db.get(domain)
    if not row:
        raise HTTPException(status_code=404, detail=f"unknown domain: {domain}")
    if not (row.get("content_md") or "").strip() and not row.get("synced_at"):
        raise HTTPException(
            status_code=503,
            detail="not synced yet — run POST /reggia/sync/pull",
        )
    return PlainTextResponse(
        content=row["content_md"] or "",
        headers={"X-Reggia-Sync-State": row.get("sync_state") or "clean"},
    )


@app.get("/reggia/longterm/{domain}", response_class=PlainTextResponse)
async def get_longterm(domain: str):
    if domain == "index":
        # `index` is a real domain in the longterm table but the chat-CC
        # contract exposes it at /reggia/index instead; keep that the canonical
        # path and 404 on /reggia/longterm/index to avoid two URLs for one thing.
        raise HTTPException(status_code=404, detail="use GET /reggia/index")
    return _serve_longterm(domain)


@app.get("/reggia/index", response_class=PlainTextResponse)
async def get_index():
    return _serve_longterm("index")


@app.post("/reggia/longterm/{domain}")
async def append_longterm(domain: str, payload: dict):
    """Append a block to a long-term page (local SQLite + Notion in one request)."""
    if domain not in longterm_db.SEED_PAGES or domain == "index":
        raise HTTPException(status_code=404, detail=f"unknown domain: {domain}")

    content = (payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")

    block_type = payload.get("type", "paragraph")
    if block_type not in VALID_BLOCK_TYPES:
        raise HTTPException(status_code=400, detail=f"unsupported block type: {block_type}")

    try:
        result = await sync.append_block(domain, content, block_type)
    except httpx.HTTPError as e:
        # Local row is already marked local_dirty; tell the caller Notion side
        # failed so they can retry via POST /reggia/sync/push.
        raise HTTPException(status_code=502, detail=f"Notion push failed: {e}")

    if not result.get("ok"):
        # Conflict or domain-unknown — surface as 409 so chat CC handles it.
        raise HTTPException(status_code=409, detail=result.get("reason", "append rejected"))
    return result


# ---------------------------------------------------------------------------
# Reggia — sync control endpoints (additive)
# ---------------------------------------------------------------------------

@app.get("/reggia/sync/status")
async def sync_status():
    return longterm_db.list_status()


@app.post("/reggia/sync/pull")
async def sync_pull():
    results = await sync.pull_all()
    return {"results": results}


@app.post("/reggia/sync/push")
async def sync_push():
    results = await sync.push_dirty()
    return {"results": results}


@app.post("/reggia/sync/resolve")
async def sync_resolve(payload: dict):
    domain = (payload.get("domain") or "").strip()
    winner = (payload.get("winner") or "").strip().lower()
    if domain not in longterm_db.SEED_PAGES:
        raise HTTPException(status_code=400, detail=f"unknown domain: {domain}")
    if winner not in ("local", "notion"):
        raise HTTPException(status_code=400, detail="winner must be 'local' or 'notion'")
    return await sync.resolve(domain, winner)


# ---------------------------------------------------------------------------
# Reggia Items CRUD (local SQLite)
# ---------------------------------------------------------------------------

@app.get("/reggia/items")
async def list_items(status: Optional[str] = Query(default=None), domain: Optional[str] = Query(default=None)):
    return db.list_items(status=status, domain=domain)


@app.post("/reggia/items")
async def create_item(payload: dict):
    if not payload.get("name"):
        raise HTTPException(status_code=400, detail="name is required")
    return db.create_item(payload)


@app.patch("/reggia/items/{item_id}")
async def update_item(item_id: str, payload: dict):
    result = db.update_item(item_id, payload)
    if result is None:
        raise HTTPException(status_code=404, detail="item not found")
    return result


@app.delete("/reggia/items/{item_id}")
async def delete_item(item_id: str, hard: bool = Query(default=False)):
    result = db.delete_item(item_id, hard)
    if result is None:
        raise HTTPException(status_code=404, detail="item not found")
    return result


# ---------------------------------------------------------------------------
# Debug logs
# ---------------------------------------------------------------------------

LOG_DIR = BASE_DIR / "logs"


@app.get("/chat/logs")
async def list_logs():
    if not LOG_DIR.exists():
        return []
    files = sorted(LOG_DIR.glob("chat_*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    return [{"session_id": f.stem.replace("chat_", ""), "size": f.stat().st_size} for f in files[:50]]


@app.get("/chat/logs/{session_id}")
async def get_log(session_id: str):
    log_file = LOG_DIR / f"chat_{session_id}.jsonl"
    if not log_file.exists():
        raise HTTPException(status_code=404, detail="log not found")
    lines = log_file.read_text().strip().split("\n")
    return {"session_id": session_id, "lines": lines[-200:]}  # last 200 lines


# ---------------------------------------------------------------------------
# Static files (must be last)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
