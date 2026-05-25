import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "reggia.db"


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
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
    conn.close()


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
                   cache_hit: int = None, cache_miss: int = None, output: int = None) -> int:
    conn = get_conn()
    now = int(time.time() * 1000)
    cur = conn.execute(
        """INSERT INTO messages (session_id, role, content, created_at, cache_hit_tokens, cache_miss_tokens, output_tokens)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (session_id, role, content, now, cache_hit, cache_miss, output),
    )
    conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, session_id))
    conn.commit()
    rowid = cur.lastrowid
    conn.close()
    return rowid


def load_history(session_id: str) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,),
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "content": r["content"]} for r in rows]


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
