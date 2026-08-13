"""
Memory file API — CRUD for markdown files in the user's memory vault.

Files live at MEMORY_DIR (default: ~/.reggia/memory/), plain markdown,
1:1 compatible with an Obsidian vault pointed at the same directory.

Endpoints:
    GET    /api/memory/files          → list all .md files with metadata
    GET    /api/memory/files/{path}   → read file content
    PUT    /api/memory/files/{path}   → write file content
    POST   /api/memory/files          → create new file
    DELETE /api/memory/files/{path}   → delete file
    PATCH  /api/memory/files/{path}   → rename/move file
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/api/memory", tags=["memory"])

# Configurable via env, default to ~/.reggia/memory/
MEMORY_DIR = Path(os.environ.get("REGGIA_MEMORY_DIR", Path.home() / ".reggia" / "memory")).resolve()

# Wikilink link-graph cache. `_`-prefixed so Obsidian ignores it and it never
# shows in the file list (it isn't a .md file anyway). Safe to delete — it's
# rebuilt from the markdown on the next save or /links request.
LINKS_FILE = MEMORY_DIR / "_links.json"

# Obsidian wikilink target = group(1): the part before any #heading or |display.
# `!?` allows ![[embeds]]; `[^\]|#]+` requires ≥1 char so bare [[#heading]]
# (in-file links) don't match and stay out of the graph.
WIKILINK_PATTERN = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")

# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------

def _resolve(file_path: str) -> Path:
    """Resolve a request path relative to MEMORY_DIR. Reject traversal attacks."""
    target = (MEMORY_DIR / file_path).resolve()
    if not str(target).startswith(str(MEMORY_DIR)):
        raise HTTPException(status_code=400, detail="invalid path")
    return target


def _file_meta(file: Path) -> dict:
    """Return metadata dict for a single file."""
    stat = file.stat()
    return {
        "name": file.name,
        "path": str(file.relative_to(MEMORY_DIR)),
        "size": stat.st_size,
        "modified": int(stat.st_mtime * 1000),
        "is_dir": file.is_dir(),
    }


# ---------------------------------------------------------------------------
# Wikilink link graph
#
# A `_links.json` cache mapping every vault file to the pages it links to, the
# pages that link to it, and any unresolved targets. Rebuilt in full on every
# write (cheap for a personal vault). Resolved values are vault-relative .md
# paths so they're directly fetchable; `unresolved` keeps the raw targets.
#
# NOTE: the agent's fetch_memory is intentionally NOT wired to this yet — Notion
# stays the primary memory and the local vault is a backup in this phase. These
# endpoints exist so the agent can adopt them later without backend changes.
# ---------------------------------------------------------------------------

def _iter_md_files():
    """Yield every .md file in the vault, skipping hidden paths (e.g. .obsidian/)."""
    if not MEMORY_DIR.exists():
        return
    for path in sorted(MEMORY_DIR.rglob("*.md")):
        if not path.is_file():
            continue
        if any(part.startswith(".") for part in path.relative_to(MEMORY_DIR).parts):
            continue
        yield path


def extract_links(content: str) -> list[str]:
    """Unique wikilink targets (no .md suffix, no heading fragment), sorted."""
    targets = set()
    for m in WIKILINK_PATTERN.finditer(content):
        t = m.group(1).strip()
        if t:
            targets.add(t)
    return sorted(targets)


def resolve_link(target: str) -> Optional[str]:
    """Resolve a wikilink target to a vault-relative .md path, or None.
    Exact path match (±.md) wins; otherwise a filename match anywhere in the vault."""
    if not target:
        return None
    with_ext = target if target.endswith(".md") else target + ".md"
    try:
        candidate = _resolve(with_ext)
    except HTTPException:
        return None
    if candidate.exists() and candidate.is_file():
        return str(candidate.relative_to(MEMORY_DIR))
    filename = with_ext.split("/")[-1]
    for f in _iter_md_files():
        if f.name == filename:
            return str(f.relative_to(MEMORY_DIR))
    return None


def rebuild_link_index() -> dict:
    """Rebuild the whole link graph from disk and persist it to _links.json."""
    if not MEMORY_DIR.exists():
        return {}
    index: dict = {}
    files = [str(f.relative_to(MEMORY_DIR)) for f in _iter_md_files()]

    # Forward pass: outgoing links, split into resolved vs unresolved.
    for rel in files:
        try:
            content = (MEMORY_DIR / rel).read_text(encoding="utf-8")
        except OSError:
            content = ""
        links_to, unresolved = [], []
        for t in extract_links(content):
            resolved = resolve_link(t)
            (links_to if resolved else unresolved).append(resolved or t)
        index[rel] = {
            "links_to": sorted(set(links_to)),
            "unresolved": sorted(set(unresolved)),
            "linked_from": [],
        }

    # Backward pass: incoming links.
    for rel, data in index.items():
        for tgt in data["links_to"]:
            if tgt in index and rel not in index[tgt]["linked_from"]:
                index[tgt]["linked_from"].append(rel)
    for data in index.values():
        data["linked_from"].sort()

    try:
        LINKS_FILE.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass  # cache write is best-effort; never fail a file op over it
    return index


def _load_index() -> dict:
    """Return the cached link graph, rebuilding it if the cache is missing/corrupt."""
    if LINKS_FILE.exists():
        try:
            return json.loads(LINKS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return rebuild_link_index()


def _reindex_safe() -> None:
    """Rebuild the link graph after a file mutation. Best-effort: indexing
    failures must never break the file operation that triggered them."""
    try:
        rebuild_link_index()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# GET /api/memory/files — list all .md files (flat list with nested folder info)
# ---------------------------------------------------------------------------

@router.get("/files")
async def list_files():
    """List all .md files with metadata. Directories are implicitly represented
    by their contents — a file at `projects/jarvis.md` implies a `projects/` folder."""
    if not MEMORY_DIR.exists():
        MEMORY_DIR.mkdir(parents=True, exist_ok=True)
        return []

    return [_file_meta(path) for path in _iter_md_files()]


# ---------------------------------------------------------------------------
# GET /api/memory/files/{path} — read file content
# ---------------------------------------------------------------------------

@router.get("/files/{file_path:path}", response_class=PlainTextResponse)
async def read_file(file_path: str):
    """Read the full content of a memory file."""
    target = _resolve(file_path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="file not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="not a file")
    if target.suffix != ".md":
        raise HTTPException(status_code=400, detail="only .md files are supported")
    return PlainTextResponse(target.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# PUT /api/memory/files/{path} — write (create or update)
# ---------------------------------------------------------------------------

@router.put("/files/{file_path:path}")
async def write_file(file_path: str, request: Request):
    """Write content to a memory file. Accepts raw markdown text as the request body."""
    target = _resolve(file_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.suffix.endswith(".md"):
        raise HTTPException(status_code=400, detail="only .md files are supported")
    content = (await request.body()).decode("utf-8")
    target.write_text(content, encoding="utf-8")
    _reindex_safe()
    return _file_meta(target)


# ---------------------------------------------------------------------------
# POST /api/memory/files — create a new empty file
# ---------------------------------------------------------------------------

@router.post("/files")
async def create_file(payload: dict):
    """Create a new .md file. Body: {path: "folder/name.md", content?: "..."}"""
    file_path = (payload.get("path") or "").strip()
    if not file_path:
        raise HTTPException(status_code=400, detail="path is required")
    if not file_path.endswith(".md"):
        file_path += ".md"

    target = _resolve(file_path)
    if target.exists():
        raise HTTPException(status_code=409, detail="file already exists")

    target.parent.mkdir(parents=True, exist_ok=True)
    content = payload.get("content", "").strip()
    target.write_text(content, encoding="utf-8")
    _reindex_safe()
    return _file_meta(target)


# ---------------------------------------------------------------------------
# DELETE /api/memory/files/{path} — delete a file
# ---------------------------------------------------------------------------

@router.delete("/files/{file_path:path}")
async def delete_file(file_path: str):
    """Delete a memory file."""
    target = _resolve(file_path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="file not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="not a file")
    target.unlink()
    _reindex_safe()
    return {"ok": True}


# ---------------------------------------------------------------------------
# PATCH /api/memory/files/{path} — rename/move
# ---------------------------------------------------------------------------

@router.patch("/files/{file_path:path}")
async def rename_file(file_path: str, payload: dict):
    """Rename or move a file. Body: {new_path: "folder/new-name.md"}"""
    new_path = (payload.get("new_path") or "").strip()
    if not new_path:
        raise HTTPException(status_code=400, detail="new_path is required")

    source = _resolve(file_path)
    if not source.exists():
        raise HTTPException(status_code=404, detail="file not found")

    dest = _resolve(new_path)
    if dest.exists():
        raise HTTPException(status_code=409, detail="destination already exists")

    dest.parent.mkdir(parents=True, exist_ok=True)
    source.rename(dest)
    _reindex_safe()
    return {"ok": True, "path": str(dest.relative_to(MEMORY_DIR))}


# ---------------------------------------------------------------------------
# GET /api/memory/links — wikilink graph (see rebuild_link_index)
# ---------------------------------------------------------------------------

@router.get("/links")
async def get_links():
    """Full link graph: { "<path>.md": {links_to, unresolved, linked_from} }."""
    return _load_index()


@router.get("/links/{file_path:path}")
async def get_links_for(file_path: str):
    """Link data for a single file (empty record if the file has no entry)."""
    target = _resolve(file_path if file_path.endswith(".md") else file_path + ".md")
    rel = str(target.relative_to(MEMORY_DIR))
    return _load_index().get(rel, {"links_to": [], "unresolved": [], "linked_from": []})
