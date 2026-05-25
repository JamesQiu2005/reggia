import json
import os
import subprocess
import uuid
from contextlib import asynccontextmanager
from datetime import date
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
ACTIVE_DB_ID = "ab0e28e53b474394815913bee3329241"
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


# ---------------------------------------------------------------------------
# Reggia Items CRUD
# ---------------------------------------------------------------------------

def _item_from_page(page: dict, today: date) -> dict:
    props = page.get("properties", {})
    name = _extract_title(props.get("Title", {}))
    domain = _extract_select(props.get("Domain", {}))
    priority = _extract_select(props.get("Priority", {}))
    status = _extract_select(props.get("Status", {}))
    sensitivity = _extract_select(props.get("Sensitivity", {}))
    notes = _extract_rich_text(props.get("Notes", {}))
    due_date = _extract_date(props.get("Due", {}))
    created_time = page.get("created_time", "")

    days_until_due = None
    if due_date:
        days_until_due = (due_date - today).days

    created_date = None
    days_since_created = None
    if created_time:
        created_date = date.fromisoformat(created_time[:10])
        days_since_created = (today - created_date).days

    return {
        "id": page["id"],
        "name": name,
        "domain": domain,
        "priority": priority,
        "status": status,
        "sensitivity": sensitivity,
        "notes": notes,
        "due_date": due_date.isoformat() if due_date else None,
        "days_until_due": days_until_due,
        "days_since_created": days_since_created,
        "url": page.get("url", ""),
    }


@app.get("/reggia/items")
async def list_items(status: Optional[str] = Query(default=None), domain: Optional[str] = Query(default=None)):
    _check_notion_key()
    today = date.today()

    conditions = []
    if status:
        conditions.append({"property": "Status", "select": {"equals": status}})
    if domain:
        conditions.append({"property": "Domain", "select": {"equals": domain}})

    filter_payload = {}
    if len(conditions) == 1:
        filter_payload = {"filter": conditions[0]}
    elif len(conditions) > 1:
        filter_payload = {"filter": {"and": conditions}}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"https://api.notion.com/v1/databases/{ACTIVE_DB_ID}/query",
            headers=NOTION_HEADERS,
            json=filter_payload,
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Notion API error: {resp.text}")

        results = resp.json().get("results", [])
        items = [_item_from_page(r, today) for r in results]
        items.sort(key=lambda x: (
            x["days_until_due"] is None,
            x["days_until_due"] if x["days_until_due"] is not None else 999,
        ))
        return items


@app.post("/reggia/items")
async def create_item(payload: dict):
    _check_notion_key()

    properties = {
        "Title": {"title": [{"text": {"content": payload.get("name", "")}}]},
    }

    if payload.get("domain"):
        properties["Domain"] = {"select": {"name": payload["domain"]}}
    if payload.get("priority"):
        properties["Priority"] = {"select": {"name": payload["priority"]}}
    if payload.get("status"):
        properties["Status"] = {"select": {"name": payload["status"]}}
    else:
        properties["Status"] = {"select": {"name": "active"}}
    if payload.get("due_date"):
        properties["Due"] = {"date": {"start": payload["due_date"]}}
    if payload.get("sensitivity"):
        properties["Sensitivity"] = {"select": {"name": payload["sensitivity"]}}
    if payload.get("notes"):
        properties["Notes"] = {"rich_text": [{"text": {"content": payload["notes"]}}]}

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            "https://api.notion.com/v1/pages",
            headers=NOTION_HEADERS,
            json={"parent": {"database_id": ACTIVE_DB_ID}, "properties": properties},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Notion API error: {resp.text}")

        return _item_from_page(resp.json(), date.today())


@app.patch("/reggia/items/{item_id}")
async def update_item(item_id: str, payload: dict):
    _check_notion_key()

    properties = {}
    if "name" in payload:
        properties["Title"] = {"title": [{"text": {"content": payload["name"]}}]}
    if "domain" in payload:
        properties["Domain"] = {"select": {"name": payload["domain"]}}
    if "priority" in payload:
        properties["Priority"] = {"select": {"name": payload["priority"]}}
    if "status" in payload:
        properties["Status"] = {"select": {"name": payload["status"]}}
    if "due_date" in payload:
        properties["Due"] = {"date": {"start": payload["due_date"]} if payload["due_date"] else {"date": None}}
    if "sensitivity" in payload:
        properties["Sensitivity"] = {"select": {"name": payload["sensitivity"]}}
    if "notes" in payload:
        properties["Notes"] = {"rich_text": [{"text": {"content": payload["notes"]}}]}

    if not properties:
        raise HTTPException(status_code=400, detail="no properties to update")

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"https://api.notion.com/v1/pages/{item_id}",
            headers=NOTION_HEADERS,
            json={"properties": properties},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=f"Notion API error: {resp.text}")

        return _item_from_page(resp.json(), date.today())


@app.delete("/reggia/items/{item_id}")
async def delete_item(item_id: str, hard: bool = Query(default=False)):
    _check_notion_key()

    if hard:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.patch(
                f"https://api.notion.com/v1/pages/{item_id}",
                headers=NOTION_HEADERS,
                json={"archived": True},
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Notion API error: {resp.text}")
        return {"id": item_id, "deleted": True}
    else:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.patch(
                f"https://api.notion.com/v1/pages/{item_id}",
                headers=NOTION_HEADERS,
                json={"properties": {"Status": {"select": {"name": "dropped"}}}},
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=502, detail=f"Notion API error: {resp.text}")
        return {"id": item_id, "status": "dropped"}


# ---------------------------------------------------------------------------
# Extract helpers
# ---------------------------------------------------------------------------

def _extract_title(prop: dict) -> str:
    titles = prop.get("title", [])
    if not titles:
        return ""
    return "".join(t.get("plain_text", "") for t in titles)


def _extract_select(prop: dict) -> str | None:
    sel = prop.get("select")
    if sel is None:
        return None
    return sel.get("name")


def _extract_date(prop: dict) -> date | None:
    d = prop.get("date")
    if d is None or d.get("start") is None:
        return None
    return date.fromisoformat(d["start"])


def _extract_rich_text(prop: dict) -> str:
    texts = prop.get("rich_text", [])
    return "".join(t.get("plain_text", "") for t in texts)


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
