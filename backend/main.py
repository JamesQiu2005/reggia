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

from . import config, db, sessions as sessions_mod

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

NOTION_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_KEY}",
    "Notion-Version": "2022-06-28",
    "Content-Type": "application/json",
}
CHAT_CONFIG = config.CHAT_CONFIG

LONGTERM_PAGES = {
    "work":         "36affd9902ce8191acf6cf6dd3b29eea",
    "research":     "36affd9902ce8175b2b2e52fce450723",
    "intellectual": "36affd9902ce8159bbe2d8b1febf35d6",
    "personal":     "36affd9902ce81819e90e0097ceeffe1",
}
INDEX_PAGE_ID = "36affd9902ce81ec9f25f5fc438765f3"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
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
# Reggia — longterm pages
# ---------------------------------------------------------------------------

def _check_notion_key():
    if not NOTION_KEY:
        raise HTTPException(status_code=500, detail="NOTION_API_KEY not configured")


async def _fetch_page_text(page_id: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://api.notion.com/v1/blocks/{page_id}/children?page_size=100",
            headers=NOTION_HEADERS,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Notion API error: {resp.text}")

        blocks = resp.json().get("results", [])
        lines = []

        for b in blocks:
            btype = b.get("type", "")
            text_parts = []

            if btype in ("paragraph", "heading_1", "heading_2", "heading_3",
                         "bulleted_list_item", "numbered_list_item", "toggle", "quote"):
                rich = b.get(btype, {}).get("rich_text", [])
                text_parts = [t.get("plain_text", "") for t in rich]

            elif btype == "table_row":
                cells = b.get("table_row", {}).get("cells", [])
                row = []
                for cell in cells:
                    cell_text = "".join(t.get("plain_text", "") for t in cell)
                    row.append(cell_text)
                text_parts = [" | ".join(row)]

            elif btype == "table":
                pass

            if text_parts:
                lines.append("".join(text_parts))

            if b.get("has_children"):
                try:
                    child_text = await _fetch_page_text(b["id"])
                    if child_text:
                        lines.append(child_text)
                except Exception:
                    pass

        return "\n".join(lines)


@app.get("/reggia/longterm/{domain}", response_class=PlainTextResponse)
async def get_longterm(domain: str):
    _check_notion_key()
    page_id = LONGTERM_PAGES.get(domain)
    if not page_id:
        raise HTTPException(status_code=404, detail=f"unknown domain: {domain}")
    return await _fetch_page_text(page_id)


@app.get("/reggia/index", response_class=PlainTextResponse)
async def get_index():
    _check_notion_key()
    return await _fetch_page_text(INDEX_PAGE_ID)


@app.post("/reggia/longterm/{domain}")
async def append_longterm(domain: str, payload: dict):
    """Append a block to a long-term Notion page."""
    _check_notion_key()
    page_id = LONGTERM_PAGES.get(domain)
    if not page_id:
        raise HTTPException(status_code=404, detail=f"unknown domain: {domain}")

    content = payload.get("content", "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required")

    block_type = payload.get("type", "paragraph")
    if block_type not in ("paragraph", "bulleted_list_item", "heading_3", "numbered_list_item"):
        raise HTTPException(status_code=400, detail=f"unsupported block type: {block_type}")

    block = {
        "object": "block",
        "type": block_type,
        block_type: {
            "rich_text": [{"type": "text", "text": {"content": content}}]
        },
    }

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"https://api.notion.com/v1/blocks/{page_id}/children",
            headers=NOTION_HEADERS,
            json={"children": [block]},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Notion API error: {resp.text}")

    return {"ok": True, "domain": domain, "page_id": page_id}


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
