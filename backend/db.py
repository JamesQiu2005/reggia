import sqlite3
import time
import uuid
from datetime import date
from pathlib import Path

DB_DIR = Path(__file__).resolve().parent / "databases"
DB_PATH = DB_DIR / "reggia_session.db"
ITEMS_DB_PATH = DB_DIR / "reggia_items.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_items_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(ITEMS_DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_updated
            ON sessions(archived, updated_at DESC);
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            cache_hit_tokens INTEGER,
            cache_miss_tokens INTEGER,
            output_tokens INTEGER,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_messages_session_time
            ON messages(session_id, created_at);
    """)
    conn.commit()
    try:
        conn.execute("ALTER TABLE messages ADD COLUMN tool_calls TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.close()

    items_conn = get_items_conn()
    items_conn.executescript("""
        CREATE TABLE IF NOT EXISTS items (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            domain TEXT,
            priority TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            sensitivity TEXT,
            notes TEXT NOT NULL DEFAULT '',
            due_date TEXT,
            created_at TEXT NOT NULL,
            archived INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_items_status
            ON items(status, archived);
    """)
    items_conn.commit()
    items_conn.close()


def create_session(session_id: str) -> None:
    conn = get_conn()
    now = int(time.time() * 1000)
    conn.execute(
        "INSERT INTO sessions (id, title, created_at, updated_at) VALUES (?, NULL, ?, ?)",
        (session_id, now, now),
    )
    conn.commit()
    conn.close()


def list_sessions() -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, title, created_at, updated_at FROM sessions WHERE archived = 0 ORDER BY updated_at DESC"
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "title": r["title"], "created_at": r["created_at"], "updated_at": r["updated_at"]} for r in rows]


def get_session(session_id: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        conn.close()
        return None
    msgs = conn.execute(
        "SELECT role, content, created_at, cache_hit_tokens, cache_miss_tokens, output_tokens FROM messages WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,),
    ).fetchall()
    conn.close()
    return {
        "id": row["id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived": row["archived"],
        "messages": [dict(m) for m in msgs],
    }


def archive_session(session_id: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE sessions SET archived = 1 WHERE id = ?", (session_id,))
    conn.commit()
    conn.close()


def set_title(session_id: str, title: str) -> None:
    conn = get_conn()
    conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
    conn.commit()
    conn.close()


def append_message(session_id: str, role: str, content: str,
                   cache_hit: int = None, cache_miss: int = None, output: int = None,
                   tool_calls: str = None) -> int:
    conn = get_conn()
    now = int(time.time() * 1000)
    cur = conn.execute(
        """INSERT INTO messages (session_id, role, content, created_at, cache_hit_tokens, cache_miss_tokens, output_tokens, tool_calls)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (session_id, role, content, now, cache_hit, cache_miss, output, tool_calls),
    )
    conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


def load_history(session_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT role, content, tool_calls FROM messages WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,),
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"], "tool_calls": r["tool_calls"]} for r in rows]


def search_sessions(query: str, limit: int = 20) -> list[dict]:
    """Search sessions by title and message content. Returns sessions with a snippet."""
    conn = get_conn()
    like = f"%{query}%"
    rows = conn.execute(
        """SELECT DISTINCT s.id, s.title,
                  (SELECT substr(m2.content, max(0, instr(lower(m2.content), lower(?)) - 40), 120)
                   FROM messages m2
                   WHERE m2.session_id = s.id AND lower(m2.content) LIKE lower(?)
                   LIMIT 1) AS snippet
           FROM sessions s
           LEFT JOIN messages m ON m.session_id = s.id
           WHERE s.archived = 0
             AND (lower(s.title) LIKE lower(?) OR lower(m.content) LIKE lower(?))
           ORDER BY s.updated_at DESC
           LIMIT ?""",
        (query, like, like, like, limit),
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "title": r["title"], "snippet": r["snippet"]} for r in rows]


def get_cache_stats(days: int = 7) -> dict:
    conn = get_conn()
    cutoff = int((time.time() - days * 86400) * 1000)
    row = conn.execute(
        """SELECT COALESCE(SUM(cache_hit_tokens), 0) AS hit,
                  COALESCE(SUM(cache_miss_tokens), 0) AS miss,
                  COALESCE(SUM(output_tokens), 0) AS output
           FROM messages WHERE created_at >= ?""",
        (cutoff,),
    ).fetchone()
    conn.close()
    total = row["hit"] + row["miss"]
    return {
        "cache_hit_tokens": row["hit"],
        "cache_miss_tokens": row["miss"],
        "output_tokens": row["output"],
        "hit_rate": round(row["hit"] / total, 4) if total > 0 else 0,
        "period_days": days,
    }


# ---------------------------------------------------------------------------
# Items CRUD (local SQLite)
# ---------------------------------------------------------------------------

def _item_from_row(row: sqlite3.Row, today: date) -> dict:
    due_date = row["due_date"]
    days_until_due = None
    if due_date:
        days_until_due = (date.fromisoformat(due_date) - today).days

    created_date = date.fromisoformat(row["created_at"][:10])
    days_since_created = (today - created_date).days

    # "past due" is a derived state, not a stored status: an item that is still
    # active but whose due date has already passed. Kept active in storage (and
    # in the status=active API the agent uses) so nothing silently changes; the
    # frontend uses this flag to route overdue items to their own view.
    is_past_due = (
        days_until_due is not None
        and days_until_due < 0
        and row["status"] == "active"
    )

    return {
        "id": row["id"],
        "name": row["name"],
        "domain": row["domain"],
        "priority": row["priority"],
        "status": row["status"],
        "sensitivity": row["sensitivity"],
        "notes": row["notes"],
        "due_date": due_date,
        "days_until_due": days_until_due,
        "days_since_created": days_since_created,
        "is_past_due": is_past_due,
        "url": "",
    }


def list_items(status: str = None, domain: str = None, today_override: date = None) -> list[dict]:
    today = today_override or date.today()
    conn = get_items_conn()
    query = "SELECT * FROM items WHERE archived = 0"
    params = []
    if status:
        query += " AND status = ?"
        params.append(status)
    if domain:
        query += " AND domain = ?"
        params.append(domain)
    rows = conn.execute(query, params).fetchall()
    conn.close()
    items = [_item_from_row(r, today) for r in rows]
    items.sort(key=lambda x: (
        x["days_until_due"] is None,
        x["days_until_due"] if x["days_until_due"] is not None else 999,
    ))
    return items


def create_item(data: dict) -> dict:
    today = date.today()
    conn = get_items_conn()
    item_id = uuid.uuid4().hex
    now_iso = today.isoformat()
    conn.execute(
        """INSERT INTO items (id, name, domain, priority, status, sensitivity, notes, due_date, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item_id,
            data.get("name", ""),
            data.get("domain"),
            data.get("priority"),
            data.get("status", "active"),
            data.get("sensitivity"),
            data.get("notes", ""),
            data.get("due_date"),
            now_iso,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    return _item_from_row(row, today)


def update_item(item_id: str, data: dict) -> dict | None:
    today = date.today()
    conn = get_items_conn()
    row = conn.execute("SELECT * FROM items WHERE id = ? AND archived = 0", (item_id,)).fetchone()
    if not row:
        conn.close()
        return None

    allowed = ["name", "domain", "priority", "status", "sensitivity", "notes", "due_date"]
    updates = {}
    params = []
    for field in allowed:
        if field in data:
            updates[field] = data[field]
    if not updates:
        conn.close()
        return _item_from_row(row, today)

    set_clause = ", ".join(f"{f} = ?" for f in updates)
    params = list(updates.values()) + [item_id]
    conn.execute(f"UPDATE items SET {set_clause} WHERE id = ?", params)
    conn.commit()
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    conn.close()
    return _item_from_row(row, today)


def delete_item(item_id: str, hard: bool = False) -> dict | None:
    conn = get_items_conn()
    row = conn.execute("SELECT * FROM items WHERE id = ? AND archived = 0", (item_id,)).fetchone()
    if not row:
        conn.close()
        return None
    if hard:
        conn.execute("UPDATE items SET archived = 1 WHERE id = ?", (item_id,))
        conn.commit()
        conn.close()
        return {"id": item_id, "deleted": True}
    else:
        conn.execute("UPDATE items SET status = 'dropped' WHERE id = ?", (item_id,))
        conn.commit()
        conn.close()
        return {"id": item_id, "status": "dropped"}
