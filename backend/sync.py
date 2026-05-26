"""Notion sync for long-term memory pages.

All Notion HTTP for the longterm subsystem lives here. The endpoint handlers
in `main.py` are thin wrappers that delegate to these functions.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

from . import longterm_db, notion_markdown

log = logging.getLogger(__name__)

NOTION_API = "https://api.notion.com/v1"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ.get('NOTION_API_KEY', '')}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


def _parse_iso(s: str | None) -> Optional[datetime]:
    """Parse Notion's `...Z` and our own `+00:00` ISO formats into UTC datetimes."""
    if not s:
        return None
    try:
        # datetime.fromisoformat handles `+00:00`; replace Z so it accepts Notion's format.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _title_from_page(page: dict) -> Optional[str]:
    """Notion page titles can live under `properties.title.title[].plain_text`
    OR under a property named after the parent DB column. Walk all properties
    and grab the first one of type 'title'."""
    props = page.get("properties", {}) or {}
    for prop in props.values():
        if prop.get("type") == "title":
            return "".join(t.get("plain_text", "") for t in prop.get("title", [])) or None
    return None


# ---------------------------------------------------------------------------
# Fetch (Notion → memory)
# ---------------------------------------------------------------------------

async def _fetch_page_meta(client: httpx.AsyncClient, page_id: str) -> dict:
    resp = await client.get(f"{NOTION_API}/pages/{page_id}", headers=_headers())
    resp.raise_for_status()
    return resp.json()


async def _fetch_blocks(client: httpx.AsyncClient, block_id: str) -> list[dict]:
    """Fetch all child blocks of a block/page, recursing into has_children.
    Children are attached to their parent as `_children` so the converter
    can render them without another HTTP round-trip."""
    blocks: list[dict] = []
    cursor: Optional[str] = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = await client.get(
            f"{NOTION_API}/blocks/{block_id}/children",
            headers=_headers(),
            params=params,
        )
        resp.raise_for_status()
        payload = resp.json()
        blocks.extend(payload.get("results", []))
        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")

    for b in blocks:
        if b.get("has_children"):
            try:
                b["_children"] = await _fetch_blocks(client, b["id"])
            except httpx.HTTPError as e:
                log.warning("fetch children failed for %s: %s", b.get("id"), e)
                b["_children"] = []
    return blocks


async def pull_one(domain: str) -> dict:
    """Pull one domain from Notion into SQLite. Returns a result dict
    with keys: domain, action ('noop'|'pulled'|'conflict'|'error'), detail."""
    row = longterm_db.get(domain)
    if not row:
        return {"domain": domain, "action": "error", "detail": "unknown domain"}
    page_id = row["notion_page_id"]

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            page = await _fetch_page_meta(client, page_id)
            notion_last_edited = page.get("last_edited_time")
            # Skip the fetch if Notion hasn't changed since last sync. Compare
            # via parsed datetimes so `...Z` vs `+00:00` formatting doesn't lie.
            notion_dt = _parse_iso(notion_last_edited)
            synced_dt = _parse_iso(row["synced_at"])
            if synced_dt and notion_dt and notion_dt <= synced_dt:
                return {"domain": domain, "action": "noop", "detail": "notion unchanged"}

            blocks = await _fetch_blocks(client, page_id)
        title = _title_from_page(page)
        writer = longterm_db.passthrough_writer(domain)
        new_md = notion_markdown.blocks_to_markdown(blocks, writer)

        # Conflict detection: local changed since last sync AND content differs.
        local_dt = _parse_iso(row["local_modified_at"])
        local_dirty = local_dt and (not synced_dt or local_dt > synced_dt)
        if local_dirty and new_md.strip() != (row["content_md"] or "").strip():
            longterm_db.mark_conflict(
                domain,
                notion_pending_md=new_md,
                notion_last_edited=notion_last_edited or "",
                title=title,
            )
            return {"domain": domain, "action": "conflict", "detail": "both sides changed"}

        longterm_db.overwrite_from_notion(
            domain,
            title=title,
            content_md=new_md,
            notion_last_edited=notion_last_edited or "",
        )
        longterm_db.cleanup_passthrough(domain, writer.seen)
        return {"domain": domain, "action": "pulled", "detail": f"{len(blocks)} blocks"}
    except httpx.HTTPError as e:
        log.warning("pull %s failed: %s", domain, e)
        return {"domain": domain, "action": "error", "detail": str(e)}


async def pull_all() -> list[dict]:
    results = []
    for domain in longterm_db.SEED_PAGES.keys():
        results.append(await pull_one(domain))
    return results


async def pull_all_background(timeout_s: int = 30, max_retries: int = 3) -> None:
    """Fire-and-forget with retry. Logs on completion; never raises."""
    for attempt in range(max_retries):
        try:
            await asyncio.wait_for(pull_all(), timeout=timeout_s)
            log.info("startup pull complete")
            return
        except asyncio.TimeoutError:
            if attempt < max_retries - 1:
                delay = 5 * (2 ** attempt)
                log.warning("startup pull attempt %d timed out — retrying in %ds", attempt + 1, delay)
                await asyncio.sleep(delay)
            else:
                log.warning("startup pull timed out after %d attempts", max_retries)
        except Exception as e:
            if attempt < max_retries - 1:
                delay = 5 * (2 ** attempt)
                log.warning("startup pull attempt %d failed: %s — retrying in %ds", attempt + 1, e, delay)
                await asyncio.sleep(delay)
            else:
                log.warning("startup pull failed after %d attempts: %s", max_retries, e)


# ---------------------------------------------------------------------------
# Push (memory → Notion)
# ---------------------------------------------------------------------------

async def _delete_all_children(client: httpx.AsyncClient, page_id: str) -> None:
    """Delete every direct child of the page. Notion requires per-block DELETE."""
    cursor: Optional[str] = None
    ids: list[str] = []
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        resp = await client.get(
            f"{NOTION_API}/blocks/{page_id}/children",
            headers=_headers(),
            params=params,
        )
        resp.raise_for_status()
        payload = resp.json()
        for b in payload.get("results", []):
            ids.append(b["id"])
        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")

    for bid in ids:
        try:
            r = await client.delete(f"{NOTION_API}/blocks/{bid}", headers=_headers())
            r.raise_for_status()
        except httpx.HTTPError as e:
            log.warning("delete block %s failed: %s", bid, e)


async def push_one(domain: str) -> dict:
    """Push local content_md to Notion. Replaces all children of the page."""
    row = longterm_db.get(domain)
    if not row:
        return {"domain": domain, "action": "error", "detail": "unknown domain"}
    page_id = row["notion_page_id"]

    reader = longterm_db.passthrough_reader(domain)
    blocks = notion_markdown.markdown_to_blocks(row["content_md"] or "", reader)

    try:
        async with httpx.AsyncClient(timeout=60) as client:
            # Safety check: detect external Notion edits before destructive push.
            page = await _fetch_page_meta(client, page_id)
            notion_dt = _parse_iso(page.get("last_edited_time"))
            synced_dt = _parse_iso(row["synced_at"])
            if notion_dt and synced_dt and notion_dt > synced_dt:
                return {"domain": domain, "action": "conflict",
                        "detail": "notion edited externally since last sync — pull to resolve"}

            await _delete_all_children(client, page_id)
            # PATCH children in batches of 100 (Notion limit).
            for i in range(0, len(blocks), 100):
                batch = blocks[i:i + 100]
                resp = await client.patch(
                    f"{NOTION_API}/blocks/{page_id}/children",
                    headers=_headers(),
                    json={"children": batch},
                )
                resp.raise_for_status()
            # Re-read page meta for the fresh last_edited_time.
            page = await _fetch_page_meta(client, page_id)
        longterm_db.mark_pushed(domain, notion_last_edited=page.get("last_edited_time") or "")
        return {"domain": domain, "action": "pushed", "detail": f"{len(blocks)} blocks"}
    except httpx.HTTPError as e:
        log.warning("push %s failed: %s", domain, e)
        return {"domain": domain, "action": "error", "detail": str(e)}


async def push_dirty() -> list[dict]:
    """Push every row currently in 'local_dirty' state."""
    results = []
    for row in longterm_db.get_all():
        if row["sync_state"] == "local_dirty":
            results.append(await push_one(row["domain"]))
    return results


# ---------------------------------------------------------------------------
# Append (chat CC POST → local + Notion in one shot)
# ---------------------------------------------------------------------------

async def append_block(domain: str, content: str, block_type: str) -> dict:
    """Apply the user-facing POST /reggia/longterm/{domain} contract:
    write to local first, then PATCH a single block to Notion. Returns
    {"ok": True, ...} on success or raises HTTPError on Notion failure."""
    row = longterm_db.get(domain)
    if not row:
        return {"ok": False, "reason": f"unknown domain: {domain}"}
    if row["sync_state"] == "conflict":
        return {"ok": False, "reason": "domain in conflict — user must resolve first"}

    # Markdown line representation for the local content_md.
    if block_type == "heading_3":
        md_line = f"### {content}"
    elif block_type == "bulleted_list_item":
        md_line = f"- {content}"
    elif block_type == "numbered_list_item":
        md_line = f"1. {content}"
    else:
        md_line = content
    longterm_db.append_line(domain, md_line)

    # Notion block payload (same shape the legacy endpoint built).
    block = {
        "object": "block",
        "type": block_type,
        block_type: {"rich_text": [{"type": "text", "text": {"content": content}}]},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.patch(
            f"{NOTION_API}/blocks/{row['notion_page_id']}/children",
            headers=_headers(),
            json={"children": [block]},
        )
        resp.raise_for_status()
        page = await _fetch_page_meta(client, row["notion_page_id"])

    longterm_db.mark_pushed(domain, notion_last_edited=page.get("last_edited_time") or "")
    return {"ok": True, "domain": domain, "page_id": row["notion_page_id"]}


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------

async def resolve(domain: str, winner: str) -> dict:
    if winner == "notion":
        longterm_db.resolve_keep_notion(domain)
        return {"ok": True, "domain": domain, "winner": "notion"}
    elif winner == "local":
        longterm_db.resolve_keep_local(domain)
        # Immediately push so Notion catches up.
        result = await push_one(domain)
        return {"ok": result["action"] == "pushed", "domain": domain, "winner": "local",
                "push": result}
    else:
        return {"ok": False, "reason": f"unknown winner: {winner}"}
