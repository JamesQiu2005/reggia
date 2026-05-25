"""SQLite layer for long-term memory.

Two tables in `backend/databases/reggia_longterm.db`:
  long_term_memory   — one row per Notion page (4 domains + index)
  block_passthrough  — raw JSON of unsupported Notion blocks, keyed by marker

Mirrors the conventions in `backend/db.py`: per-call sync connection, WAL,
`PRAGMA synchronous=NORMAL`, `row_factory=Row`, ISO-8601 string timestamps.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DB_DIR = Path(__file__).resolve().parent / "databases"
DB_PATH = DB_DIR / "reggia_longterm.db"

# Seed rows. Domain names mirror what the chat-CC URL contract uses
# (`/reggia/longterm/{domain}`). `index` is the singleton index page.
SEED_PAGES: dict[str, str] = {
    "work":         "36affd9902ce8191acf6cf6dd3b29eea",
    "research":     "36affd9902ce8175b2b2e52fce450723",
    "intellectual": "36affd9902ce8159bbe2d8b1febf35d6",
    "personal":     "36affd9902ce81819e90e0097ceeffe1",
    "index":        "36affd9902ce81ec9f25f5fc438765f3",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    DB_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS long_term_memory (
            domain              TEXT PRIMARY KEY,
            notion_page_id      TEXT NOT NULL UNIQUE,
            title               TEXT,
            content_md          TEXT NOT NULL DEFAULT '',
            notion_pending_md   TEXT,
            notion_last_edited  TEXT,
            local_modified_at   TEXT,
            synced_at           TEXT,
            sync_state          TEXT NOT NULL DEFAULT 'clean'
        );
        CREATE TABLE IF NOT EXISTS block_passthrough (
            domain       TEXT NOT NULL,
            marker_id    TEXT NOT NULL,
            raw_json     TEXT NOT NULL,
            PRIMARY KEY (domain, marker_id),
            FOREIGN KEY (domain) REFERENCES long_term_memory(domain) ON DELETE CASCADE
        );
    """)
    # Seed the 5 page rows on first init. INSERT OR IGNORE leaves existing
    # rows untouched on subsequent boots.
    for domain, page_id in SEED_PAGES.items():
        conn.execute(
            "INSERT OR IGNORE INTO long_term_memory (domain, notion_page_id) VALUES (?, ?)",
            (domain, page_id),
        )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Read helpers
# ---------------------------------------------------------------------------

def get_all() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT domain, notion_page_id, title, content_md, notion_pending_md, "
        "notion_last_edited, local_modified_at, synced_at, sync_state "
        "FROM long_term_memory ORDER BY domain"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get(domain: str) -> Optional[dict]:
    conn = get_conn()
    row = conn.execute(
        "SELECT domain, notion_page_id, title, content_md, notion_pending_md, "
        "notion_last_edited, local_modified_at, synced_at, sync_state "
        "FROM long_term_memory WHERE domain = ?",
        (domain,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_status() -> list[dict]:
    """Compact status payload for /reggia/sync/status."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT domain, sync_state, notion_last_edited, local_modified_at, synced_at "
        "FROM long_term_memory ORDER BY domain"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Write helpers
# ---------------------------------------------------------------------------

def overwrite_from_notion(domain: str, *, title: str | None, content_md: str,
                          notion_last_edited: str) -> None:
    """Clean pull — replace local with Notion's version."""
    conn = get_conn()
    conn.execute(
        """UPDATE long_term_memory
           SET title = ?, content_md = ?, notion_pending_md = NULL,
               notion_last_edited = ?, synced_at = ?, sync_state = 'clean'
           WHERE domain = ?""",
        (title, content_md, notion_last_edited, now_iso(), domain),
    )
    conn.commit()
    conn.close()


def mark_conflict(domain: str, *, notion_pending_md: str,
                  notion_last_edited: str, title: str | None) -> None:
    """Pull found that both sides moved. Stash Notion's version in pending."""
    conn = get_conn()
    conn.execute(
        """UPDATE long_term_memory
           SET title = COALESCE(?, title),
               notion_pending_md = ?,
               notion_last_edited = ?,
               sync_state = 'conflict'
           WHERE domain = ?""",
        (title, notion_pending_md, notion_last_edited, domain),
    )
    conn.commit()
    conn.close()


def append_line(domain: str, line: str) -> Optional[str]:
    """Append a line to content_md, set sync_state='local_dirty'.
    Returns the new content_md, or None if the row doesn't exist."""
    conn = get_conn()
    row = conn.execute(
        "SELECT content_md FROM long_term_memory WHERE domain = ?", (domain,)
    ).fetchone()
    if not row:
        conn.close()
        return None
    existing = row["content_md"] or ""
    # Always separate the appended line from prior content by a blank line.
    sep = "" if existing.endswith("\n\n") or not existing else ("\n" if existing.endswith("\n") else "\n\n")
    new_md = existing + sep + line + "\n"
    conn.execute(
        """UPDATE long_term_memory
           SET content_md = ?, local_modified_at = ?, sync_state = 'local_dirty'
           WHERE domain = ?""",
        (new_md, now_iso(), domain),
    )
    conn.commit()
    conn.close()
    return new_md


def mark_pushed(domain: str, *, notion_last_edited: str) -> None:
    conn = get_conn()
    conn.execute(
        """UPDATE long_term_memory
           SET synced_at = ?, notion_last_edited = ?, sync_state = 'clean'
           WHERE domain = ?""",
        (now_iso(), notion_last_edited, domain),
    )
    conn.commit()
    conn.close()


def resolve_keep_notion(domain: str) -> None:
    """User chose Notion. Copy pending into content, clear conflict."""
    conn = get_conn()
    row = conn.execute(
        "SELECT notion_pending_md FROM long_term_memory WHERE domain = ?", (domain,)
    ).fetchone()
    if not row:
        conn.close()
        return
    pending = row["notion_pending_md"] or ""
    conn.execute(
        """UPDATE long_term_memory
           SET content_md = ?, notion_pending_md = NULL,
               local_modified_at = NULL, synced_at = ?, sync_state = 'clean'
           WHERE domain = ?""",
        (pending, now_iso(), domain),
    )
    conn.commit()
    conn.close()


def resolve_keep_local(domain: str) -> None:
    """User chose local. Discard pending; mark dirty so a push catches it up."""
    conn = get_conn()
    conn.execute(
        """UPDATE long_term_memory
           SET notion_pending_md = NULL, sync_state = 'local_dirty'
           WHERE domain = ?""",
        (domain,),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Passthrough store
# ---------------------------------------------------------------------------

def passthrough_writer(domain: str):
    """Return a writer closure for a given domain. Replaces any prior set
    for that domain on first call within a pull, so old markers don't accumulate."""
    first_call = {"done": False}

    def write(marker_id: str, block: dict) -> None:
        conn = get_conn()
        if not first_call["done"]:
            conn.execute("DELETE FROM block_passthrough WHERE domain = ?", (domain,))
            first_call["done"] = True
        conn.execute(
            "INSERT OR REPLACE INTO block_passthrough (domain, marker_id, raw_json) VALUES (?, ?, ?)",
            (domain, marker_id, json.dumps(block)),
        )
        conn.commit()
        conn.close()
    return write


def passthrough_reader(domain: str):
    """Return a reader closure for a given domain."""
    def read(marker_id: str) -> Optional[dict]:
        conn = get_conn()
        row = conn.execute(
            "SELECT raw_json FROM block_passthrough WHERE domain = ? AND marker_id = ?",
            (domain, marker_id),
        ).fetchone()
        conn.close()
        if not row:
            return None
        try:
            return json.loads(row["raw_json"])
        except json.JSONDecodeError:
            return None
    return read
