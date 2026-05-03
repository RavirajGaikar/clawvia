"""SQLite database — sessions, messages, and task logs."""

import sqlite3
import json
import datetime
import threading

import config
from utils.logger import get_logger

log = get_logger("memory.db")

_conn = None
_db_lock = threading.RLock()


def _get_conn():
    """Return the shared SQLite connection, creating it if needed.

    IMPORTANT: This function must ONLY be called while _db_lock is held
    (i.e., from within an @_locked function). The lock guards both the
    lazy initialization and all subsequent operations on the connection.
    """
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA busy_timeout=5000")
        _init_tables(_conn)
        log.info("Database opened: %s", config.DB_PATH)
    return _conn


def _init_tables(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL DEFAULT 'New Session',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0,
            compaction_count INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id)
        );

        CREATE TABLE IF NOT EXISTS task_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            task TEXT NOT NULL,
            steps_json TEXT,
            result TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS memory_notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT NOT NULL,
            content TEXT NOT NULL,
            source TEXT DEFAULT 'agent',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prompt TEXT NOT NULL,
            schedule_type TEXT NOT NULL,
            schedule_value TEXT NOT NULL,
            next_run TEXT NOT NULL,
            last_run TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_messages_session
            ON messages(session_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_task_logs_session
            ON task_logs(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_memory_notes_topic
            ON memory_notes(topic);
        CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_status
            ON scheduled_tasks(status, next_run);

        CREATE TABLE IF NOT EXISTS dream_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dream_text TEXT NOT NULL,
            source_notes TEXT,
            source_messages INTEGER DEFAULT 0,
            score REAL DEFAULT 0.0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS checkpoints (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            step INTEGER NOT NULL,
            steps_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_checkpoints_session
            ON checkpoints(session_id, created_at);

        CREATE TABLE IF NOT EXISTS reflections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            task TEXT NOT NULL,
            tool TEXT NOT NULL,
            error_hash TEXT NOT NULL,
            error TEXT NOT NULL,
            reflection TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_reflections_session
            ON reflections(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_reflections_lookup
            ON reflections(session_id, tool, error_hash);

        CREATE TABLE IF NOT EXISTS todos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_todos_session
            ON todos(session_id, status, id);
    """)
    # FTS5 for memory search (keyword search across notes)
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts
            USING fts5(topic, content)
        """)
    except Exception as exc:
        log.warning("FTS5 not available: %s (keyword search will fall back to LIKE)", exc)
    # Add compaction_count column if migrating from older schema
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN compaction_count INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass  # Column already exists
    # Add retries + last_error columns to scheduled_tasks (migration)
    try:
        conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN retries INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE scheduled_tasks ADD COLUMN last_error TEXT")
    except Exception:
        pass
    # Pending approvals table (persists across restarts)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pending_approvals (
            session_id INTEGER PRIMARY KEY,
            tool TEXT NOT NULL,
            args_json TEXT NOT NULL,
            task TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # Key-value store for misc persistent state (e.g. dream timestamps)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kv_store (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Strategy patterns — what worked (success-based learning)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS strategy_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_keywords TEXT NOT NULL,
            pattern_text TEXT NOT NULL,
            success_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    # Goals — persistent objectives the agent tracks proactively
    conn.execute("""
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            description TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 5,
            status TEXT NOT NULL DEFAULT 'active',
            progress_notes TEXT NOT NULL DEFAULT '',
            check_interval INTEGER NOT NULL DEFAULT 3600,
            next_check TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_goals_status
            ON goals(status, next_check)
    """)
    # Event watchers — condition-triggered autonomous actions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            condition_json TEXT NOT NULL,
            action_prompt TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            cooldown_minutes INTEGER NOT NULL DEFAULT 60,
            cooldown_until TEXT,
            last_triggered TEXT,
            created_at TEXT NOT NULL
        )
    """)
    # Audit log — track all significant actions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            action_type TEXT NOT NULL,
            tool_name TEXT,
            args_summary TEXT,
            result_summary TEXT,
            session_id INTEGER
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_timestamp
            ON audit_log(timestamp DESC)
    """)
    # FTS5 for cross-session message search (recall tool)
    try:
        conn.execute("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(content, session_id UNINDEXED, role UNINDEXED)
        """)
    except Exception as exc:
        log.warning("Messages FTS5 not available: %s", exc)
    # User profile — persistent understanding of the user
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_profile (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0.5,
            source TEXT DEFAULT 'inferred',
            updated_at TEXT NOT NULL
        )
    """)
    # Task metrics — performance tracking for meta-learning
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_metrics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            task_summary TEXT NOT NULL,
            steps_count INTEGER NOT NULL DEFAULT 0,
            errors_count INTEGER NOT NULL DEFAULT 0,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 1,
            user_feedback TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_task_metrics_session
            ON task_metrics(session_id, created_at)
    """)
    conn.commit()
    # Backfill messages_fts from existing messages (one-time migration)
    _backfill_messages_fts(conn)


def _backfill_messages_fts(conn):
    """One-time migration: index existing messages into messages_fts."""
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM messages_fts").fetchone()["c"]
        if count > 0:
            return  # Already populated
        rows = conn.execute(
            "SELECT rowid, content, session_id, role FROM messages"
        ).fetchall()
        if not rows:
            return
        for r in rows:
            conn.execute(
                "INSERT INTO messages_fts(rowid, content, session_id, role) "
                "VALUES (?, ?, ?, ?)",
                (r["rowid"], r["content"], r["session_id"], r["role"]),
            )
        conn.commit()
        log.info("Backfilled %d messages into FTS index", len(rows))
    except Exception as exc:
        log.debug("Messages FTS backfill skipped: %s", exc)


def _now():
    return datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _locked(func):
    """Decorator: acquire _db_lock before executing a DB function."""
    import functools

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        with _db_lock:
            return func(*args, **kwargs)

    return wrapper


# ── Sessions ──────────────────────────────────────────────────────────────

@_locked
def create_session(title="New Session"):
    conn = _get_conn()
    now = _now()
    # Deactivate all other sessions
    conn.execute("UPDATE sessions SET is_active = 0")
    cur = conn.execute(
        "INSERT INTO sessions (title, created_at, updated_at, is_active) VALUES (?, ?, ?, 1)",
        (title, now, now),
    )
    conn.commit()
    session_id = cur.lastrowid
    log.info("Created session #%d: %s", session_id, title)
    return {"id": session_id, "title": title, "created_at": now, "is_active": True}


@_locked
def get_active_session():
    conn = _get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE is_active = 1 LIMIT 1").fetchone()
    if row:
        return dict(row)
    # Auto-create a default session if none exists
    return create_session("Default Session")


@_locked
def list_sessions():
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM sessions ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def switch_session(session_id):
    conn = _get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not row:
        return None
    conn.execute("UPDATE sessions SET is_active = 0")
    conn.execute(
        "UPDATE sessions SET is_active = 1, updated_at = ? WHERE id = ?",
        (_now(), session_id),
    )
    conn.commit()
    log.info("Switched to session #%d", session_id)
    return dict(conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())


@_locked
def delete_session(session_id):
    conn = _get_conn()
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM task_logs WHERE session_id = ?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    conn.commit()
    log.info("Deleted session #%d", session_id)


# ── Messages ──────────────────────────────────────────────────────────────

@_locked
def add_message(session_id, role, content):
    conn = _get_conn()
    now = _now()
    cur = conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (session_id, role, content, now),
    )
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ?",
        (now, session_id),
    )
    # Index into cross-session FTS
    try:
        conn.execute(
            "INSERT INTO messages_fts(rowid, content, session_id, role) "
            "VALUES (?, ?, ?, ?)",
            (cur.lastrowid, content, session_id, role),
        )
    except Exception:
        pass
    conn.commit()


@_locked
def get_messages(session_id, limit=20):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT role, content, timestamp FROM messages "
        "WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


@_locked
def clear_messages(session_id):
    conn = _get_conn()
    conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
    conn.commit()
    log.info("Cleared messages for session #%d", session_id)


# ── Task Logs ─────────────────────────────────────────────────────────────

@_locked
def log_task(session_id, task, steps, result):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO task_logs (session_id, task, steps_json, result, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, task, json.dumps(steps), result, _now()),
    )
    conn.commit()


@_locked
def get_recent_tasks(session_id, limit=5):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT task, result, created_at FROM task_logs "
        "WHERE session_id = ? ORDER BY created_at DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [dict(r) for r in reversed(rows)]


# ── Checkpoints ───────────────────────────────────────────────────────────

@_locked
def save_checkpoint(session_id, task, step, steps):
    """Persist a snapshot of agent loop state after each step."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO checkpoints (session_id, task, step, steps_json, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, task, step, json.dumps(steps), _now()),
    )
    conn.commit()


@_locked
def list_checkpoints(session_id, limit=10):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, task, step, created_at FROM checkpoints "
        "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def get_checkpoint(checkpoint_id):
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, session_id, task, step, steps_json, created_at "
        "FROM checkpoints WHERE id = ?",
        (checkpoint_id,),
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["steps"] = json.loads(data.pop("steps_json") or "[]")
    except Exception:
        data["steps"] = []
    return data


@_locked
def prune_checkpoints(session_id, keep=20):
    """Keep only the most-recent `keep` checkpoints per session."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id FROM checkpoints WHERE session_id = ? ORDER BY id DESC LIMIT -1 OFFSET ?",
        (session_id, keep),
    ).fetchall()
    if not rows:
        return
    ids = [r["id"] for r in rows]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM checkpoints WHERE id IN ({placeholders})", ids)
    conn.commit()


# ── Reflections (Reflexion-style failure memory) ──────────────────────────

@_locked
def save_reflection(session_id, task, tool, error_hash, error, reflection):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO reflections (session_id, task, tool, error_hash, error, reflection, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, task, tool, error_hash, error, reflection, _now()),
    )
    conn.commit()


@_locked
def find_reflection(session_id, tool, error_hash):
    """Return existing reflection text for the same tool+error in this session, or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT reflection FROM reflections "
        "WHERE session_id = ? AND tool = ? AND error_hash = ? "
        "ORDER BY id DESC LIMIT 1",
        (session_id, tool, error_hash),
    ).fetchone()
    return row["reflection"] if row else None


@_locked
def list_reflections(session_id, limit=10):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT tool, error, reflection, created_at FROM reflections "
        "WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Todos (TodoWrite-style task tracking) ─────────────────────────────────

@_locked
def todo_add(session_id, text):
    conn = _get_conn()
    now = _now()
    cur = conn.execute(
        "INSERT INTO todos (session_id, text, status, created_at, updated_at) "
        "VALUES (?, ?, 'open', ?, ?)",
        (session_id, text, now, now),
    )
    conn.commit()
    return cur.lastrowid


@_locked
def todo_update(session_id, todo_id, status=None, text=None):
    """Update a todo's status (open/in_progress/done/cancelled) or text."""
    conn = _get_conn()
    fields, vals = [], []
    if status is not None:
        fields.append("status = ?")
        vals.append(status)
    if text is not None:
        fields.append("text = ?")
        vals.append(text)
    if not fields:
        return False
    fields.append("updated_at = ?")
    vals.append(_now())
    vals.extend([todo_id, session_id])
    cur = conn.execute(
        f"UPDATE todos SET {', '.join(fields)} WHERE id = ? AND session_id = ?",
        vals,
    )
    conn.commit()
    return cur.rowcount > 0


@_locked
def todo_list(session_id, include_done=True):
    conn = _get_conn()
    if include_done:
        rows = conn.execute(
            "SELECT id, text, status, created_at FROM todos "
            "WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, text, status, created_at FROM todos "
            "WHERE session_id = ? AND status NOT IN ('done','cancelled') "
            "ORDER BY id",
            (session_id,),
        ).fetchall()
    return [dict(r) for r in rows]


@_locked
def todo_clear(session_id):
    conn = _get_conn()
    cur = conn.execute("DELETE FROM todos WHERE session_id = ?", (session_id,))
    conn.commit()
    return cur.rowcount


@_locked
def get_session_history(session_id):
    """Get full session data including messages and tasks."""
    conn = _get_conn()
    session = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if not session:
        return None
    messages = get_messages(session_id, limit=100)
    tasks = get_recent_tasks(session_id, limit=20)
    return {
        "session": dict(session),
        "messages": messages,
        "tasks": tasks,
    }


# ── Compaction ────────────────────────────────────────────────────────────

@_locked
def replace_messages_with_summary(session_id, summary, keep_recent):
    """Replace old messages with a summary, keeping the most recent ones."""
    conn = _get_conn()
    # Get IDs of messages to keep (most recent N)
    keep_rows = conn.execute(
        "SELECT id FROM messages WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
        (session_id, keep_recent),
    ).fetchall()
    keep_ids = {r["id"] for r in keep_rows}

    if keep_ids:
        placeholders = ",".join("?" for _ in keep_ids)
        conn.execute(
            f"DELETE FROM messages WHERE session_id = ? AND id NOT IN ({placeholders})",
            (session_id, *keep_ids),
        )
    else:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))

    # Insert summary as a system message at the beginning
    # Use a timestamp just before the oldest remaining message
    oldest = conn.execute(
        "SELECT MIN(timestamp) as ts FROM messages WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    summary_ts = oldest["ts"] if oldest and oldest["ts"] else _now()
    # Make it slightly earlier
    summary_ts = summary_ts.replace("Z", "") + "Z" if summary_ts.endswith("Z") else summary_ts

    conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
        (session_id, "system", f"[Conversation Summary]\n{summary}", summary_ts),
    )
    conn.commit()
    log.info("Replaced old messages with summary for session #%d", session_id)


@_locked
def increment_compaction_count(session_id):
    conn = _get_conn()
    conn.execute(
        "UPDATE sessions SET compaction_count = compaction_count + 1 WHERE id = ?",
        (session_id,),
    )
    conn.commit()


@_locked
def get_compaction_count(session_id):
    conn = _get_conn()
    row = conn.execute(
        "SELECT compaction_count FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    return row["compaction_count"] if row else 0


# ── Memory Notes ──────────────────────────────────────────────────────────

@_locked
def save_note(topic, content, source="agent"):
    """Save or append a memory note for a topic."""
    conn = _get_conn()
    now = _now()
    # Check if topic already exists
    existing = conn.execute(
        "SELECT id, content FROM memory_notes WHERE topic = ?", (topic,)
    ).fetchone()

    if existing:
        new_content = existing["content"] + "\n" + content
        conn.execute(
            "UPDATE memory_notes SET content = ?, updated_at = ? WHERE id = ?",
            (new_content, now, existing["id"]),
        )
        note_id = existing["id"]
    else:
        cur = conn.execute(
            "INSERT INTO memory_notes (topic, content, source, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (topic, content, source, now, now),
        )
        note_id = cur.lastrowid

    # Update FTS index
    _index_note_fts(conn, note_id, topic, content if not existing else new_content)
    conn.commit()
    log.info("Saved note for topic '%s'", topic)
    return note_id


@_locked
def search_notes_fts(query, limit=5):
    """Search notes using FTS5 full-text search."""
    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT topic, content, rank FROM notes_fts "
            "WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?",
            (query, limit),
        ).fetchall()
        # FTS table doesn't have updated_at, so fetch from main table
        results = []
        for r in rows:
            note = conn.execute(
                "SELECT topic, content, updated_at FROM memory_notes WHERE topic = ?",
                (r["topic"],),
            ).fetchone()
            if note:
                results.append(dict(note))
        return results
    except Exception:
        # Fallback to LIKE search if FTS5 not available
        return search_notes_like(query, limit)


@_locked
def search_notes_like(query, limit=5):
    """Fallback keyword search using LIKE."""
    conn = _get_conn()
    pattern = f"%{query}%"
    rows = conn.execute(
        "SELECT topic, content, updated_at FROM memory_notes "
        "WHERE topic LIKE ? OR content LIKE ? "
        "ORDER BY updated_at DESC LIMIT ?",
        (pattern, pattern, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def get_note(topic):
    """Get a specific note by topic."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT topic, content, source, created_at, updated_at FROM memory_notes WHERE topic = ?",
        (topic,),
    ).fetchone()
    return dict(row) if row else None


@_locked
def list_notes():
    """List all memory note topics."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT topic, substr(content, 1, 100) as preview, updated_at "
        "FROM memory_notes ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def _index_note_fts(conn, note_id, topic, content):
    """Update the FTS5 index for a note."""
    try:
        # Delete old entry if exists
        conn.execute("DELETE FROM notes_fts WHERE rowid = ?", (note_id,))
        # Insert new
        conn.execute(
            "INSERT INTO notes_fts(rowid, topic, content) VALUES (?, ?, ?)",
            (note_id, topic, content),
        )
    except Exception as exc:
        log.debug("FTS index update skipped: %s", exc)


# ── Scheduled Tasks ───────────────────────────────────────────────────────

@_locked
def add_scheduled_task(prompt, schedule_type, schedule_value, next_run):
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO scheduled_tasks (prompt, schedule_type, schedule_value, next_run, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (prompt, schedule_type, schedule_value, next_run, _now()),
    )
    conn.commit()
    return cur.lastrowid


@_locked
def get_due_tasks(now_iso):
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM scheduled_tasks WHERE status = 'active' AND next_run <= ?",
        (now_iso,),
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def update_scheduled_task(task_id, next_run=None, last_run=None, status=None):
    conn = _get_conn()
    parts, vals = [], []
    if next_run is not None:
        parts.append("next_run = ?")
        vals.append(next_run)
    if last_run is not None:
        parts.append("last_run = ?")
        vals.append(last_run)
    if status is not None:
        parts.append("status = ?")
        vals.append(status)
    if parts:
        vals.append(task_id)
        conn.execute(f"UPDATE scheduled_tasks SET {', '.join(parts)} WHERE id = ?", vals)
        conn.commit()


@_locked
def list_scheduled_tasks(include_inactive=False):
    conn = _get_conn()
    if include_inactive:
        rows = conn.execute("SELECT * FROM scheduled_tasks ORDER BY next_run").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM scheduled_tasks WHERE status = 'active' ORDER BY next_run"
        ).fetchall()
    return [dict(r) for r in rows]


@_locked
def cancel_scheduled_task(task_id):
    conn = _get_conn()
    row = conn.execute("SELECT id FROM scheduled_tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        return False
    conn.execute("UPDATE scheduled_tasks SET status = 'cancelled' WHERE id = ?", (task_id,))
    conn.commit()
    return True


@_locked
def increment_task_retries(task_id):
    """Increment the retries counter for a scheduled task."""
    conn = _get_conn()
    conn.execute(
        "UPDATE scheduled_tasks SET retries = retries + 1 WHERE id = ?",
        (task_id,),
    )
    conn.commit()


# ── Pending Approvals ─────────────────────────────────────────────────────

@_locked
def save_pending_approval(session_id, tool, args, task=None):
    """Persist a pending approval request."""
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO pending_approvals (session_id, tool, args_json, task, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (session_id, tool, json.dumps(args), task, _now()),
    )
    conn.commit()


@_locked
def get_pending_approval(session_id):
    """Get a pending approval for a session, or None."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM pending_approvals WHERE session_id = ?", (session_id,)
    ).fetchone()
    if row:
        d = dict(row)
        d["args"] = json.loads(d["args_json"])
        return d
    return None


@_locked
def delete_pending_approval(session_id):
    """Remove a pending approval after it's been handled."""
    conn = _get_conn()
    conn.execute("DELETE FROM pending_approvals WHERE session_id = ?", (session_id,))
    conn.commit()


@_locked
def get_all_pending_approvals():
    """Get all pending approvals (for restore on startup)."""
    conn = _get_conn()
    rows = conn.execute("SELECT * FROM pending_approvals").fetchall()
    results = []
    for row in rows:
        d = dict(row)
        d["args"] = json.loads(d["args_json"])
        results.append(d)
    return results


# ── Key-Value Store ───────────────────────────────────────────────────────

@_locked
def kv_get(key, default=None):
    """Get a value from the key-value store."""
    conn = _get_conn()
    row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


@_locked
def kv_set(key, value):
    """Set a value in the key-value store."""
    conn = _get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO kv_store (key, value, updated_at) VALUES (?, ?, ?)",
        (key, str(value), _now()),
    )
    conn.commit()


# ── Dreaming ──────────────────────────────────────────────────────────────

@_locked
def get_recent_notes(hours=24, limit=20):
    """Get memory notes updated within the last N hours."""
    conn = _get_conn()
    cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
    ).isoformat(timespec="seconds") + "Z"
    rows = conn.execute(
        "SELECT topic, content, updated_at FROM memory_notes "
        "WHERE updated_at >= ? ORDER BY updated_at DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def get_recent_messages_all(hours=24, limit=50):
    """Get recent messages across all sessions for dreaming."""
    conn = _get_conn()
    cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
    ).isoformat(timespec="seconds") + "Z"
    rows = conn.execute(
        "SELECT m.role, m.content, m.timestamp, s.title as session_title "
        "FROM messages m JOIN sessions s ON m.session_id = s.id "
        "WHERE m.timestamp >= ? ORDER BY m.timestamp DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def save_dream(dream_text, source_notes, source_messages, score):
    """Save a dream entry."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO dream_log (dream_text, source_notes, source_messages, score, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (dream_text, source_notes, source_messages, score, _now()),
    )
    conn.commit()


@_locked
def get_recent_dreams(limit=5):
    """Get recent dreams."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT dream_text, score, created_at FROM dream_log "
        "ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


# ── Strategy Patterns (success-based learning) ────────────────────────────

@_locked
def save_strategy_pattern(task_keywords, pattern_text):
    """Save a new strategy pattern or increment success_count if similar exists."""
    conn = _get_conn()
    now = _now()
    # Check if a similar pattern exists (same keywords)
    existing = conn.execute(
        "SELECT id FROM strategy_patterns WHERE task_keywords = ?",
        (task_keywords,),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE strategy_patterns SET success_count = success_count + 1, "
            "updated_at = ? WHERE id = ?",
            (now, existing["id"]),
        )
    else:
        conn.execute(
            "INSERT INTO strategy_patterns (task_keywords, pattern_text, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (task_keywords, pattern_text, now, now),
        )
    conn.commit()


@_locked
def search_strategy_patterns(query, limit=3):
    """Search strategy patterns by keyword match (LIKE-based)."""
    conn = _get_conn()
    words = [w.strip() for w in query.split() if len(w.strip()) >= 3][:5]
    if not words:
        return []
    conditions = " OR ".join(["task_keywords LIKE ?"] * len(words))
    params = [f"%{w}%" for w in words]
    rows = conn.execute(
        f"SELECT pattern_text, success_count, task_keywords FROM strategy_patterns "
        f"WHERE {conditions} ORDER BY success_count DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def increment_pattern_success(task_keywords):
    """Increment success count for a pattern (reinforcement)."""
    conn = _get_conn()
    conn.execute(
        "UPDATE strategy_patterns SET success_count = success_count + 1, "
        "updated_at = ? WHERE task_keywords = ?",
        (_now(), task_keywords),
    )
    conn.commit()


# ── Goals ─────────────────────────────────────────────────────────────────

@_locked
def add_goal(description, priority=5, check_interval=3600):
    """Create a new goal. check_interval is in seconds."""
    conn = _get_conn()
    now = _now()
    next_check = (
        datetime.datetime.utcnow() + datetime.timedelta(seconds=check_interval)
    ).isoformat(timespec="seconds") + "Z"
    cur = conn.execute(
        "INSERT INTO goals (description, priority, check_interval, next_check, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (description, priority, check_interval, next_check, now, now),
    )
    conn.commit()
    return cur.lastrowid


@_locked
def get_due_goals(now_iso):
    """Get active goals whose next_check is due."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM goals WHERE status = 'active' AND next_check <= ?",
        (now_iso,),
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def update_goal(goal_id, progress=None, status=None, next_check=None):
    """Update a goal's progress, status, or next check time."""
    conn = _get_conn()
    parts, vals = [], []
    if progress is not None:
        parts.append("progress_notes = progress_notes || '\n' || ?")
        vals.append(progress)
    if status is not None:
        parts.append("status = ?")
        vals.append(status)
    if next_check is not None:
        parts.append("next_check = ?")
        vals.append(next_check)
    parts.append("updated_at = ?")
    vals.append(_now())
    vals.append(goal_id)
    if parts:
        conn.execute(f"UPDATE goals SET {', '.join(parts)} WHERE id = ?", vals)
        conn.commit()


@_locked
def list_goals(include_inactive=False):
    """List all goals."""
    conn = _get_conn()
    if include_inactive:
        rows = conn.execute("SELECT * FROM goals ORDER BY priority, next_check").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM goals WHERE status = 'active' ORDER BY priority, next_check"
        ).fetchall()
    return [dict(r) for r in rows]


# ── Watchers ──────────────────────────────────────────────────────────────

@_locked
def add_watcher(event_type, condition, action_prompt, cooldown_minutes=60):
    """Create an event watcher."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO watchers (event_type, condition_json, action_prompt, "
        "cooldown_minutes, created_at) VALUES (?, ?, ?, ?, ?)",
        (event_type, json.dumps(condition), action_prompt, cooldown_minutes, _now()),
    )
    conn.commit()
    return cur.lastrowid


@_locked
def get_active_watchers():
    """Get all active watchers."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM watchers WHERE status = 'active'"
    ).fetchall()
    results = []
    for row in rows:
        d = dict(row)
        d["condition"] = json.loads(d["condition_json"])
        results.append(d)
    return results


@_locked
def update_watcher(watcher_id, last_triggered=None, cooldown_until=None, status=None):
    """Update a watcher after triggering or deactivating."""
    conn = _get_conn()
    parts, vals = [], []
    if last_triggered is not None:
        parts.append("last_triggered = ?")
        vals.append(last_triggered)
    if cooldown_until is not None:
        parts.append("cooldown_until = ?")
        vals.append(cooldown_until)
    if status is not None:
        parts.append("status = ?")
        vals.append(status)
    if parts:
        vals.append(watcher_id)
        conn.execute(f"UPDATE watchers SET {', '.join(parts)} WHERE id = ?", vals)
        conn.commit()


@_locked
def list_watchers(include_inactive=False):
    """List all watchers."""
    conn = _get_conn()
    if include_inactive:
        rows = conn.execute("SELECT * FROM watchers ORDER BY event_type").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM watchers WHERE status = 'active' ORDER BY event_type"
        ).fetchall()
    results = []
    for row in rows:
        d = dict(row)
        d["condition"] = json.loads(d["condition_json"])
        results.append(d)
    return results


# ── Audit Log ─────────────────────────────────────────────────────────────

@_locked
def audit_log_event(action_type, tool_name=None, args_summary=None,
                    result_summary=None, session_id=None):
    """Log an auditable event."""
    conn = _get_conn()
    conn.execute(
        "INSERT INTO audit_log (timestamp, action_type, tool_name, args_summary, "
        "result_summary, session_id) VALUES (?, ?, ?, ?, ?, ?)",
        (_now(), action_type, tool_name, args_summary, result_summary, session_id),
    )
    conn.commit()


@_locked
def audit_search(query=None, action_type=None, limit=20):
    """Search audit log."""
    conn = _get_conn()
    conditions, params = [], []
    if action_type:
        conditions.append("action_type = ?")
        params.append(action_type)
    if query:
        conditions.append("(tool_name LIKE ? OR args_summary LIKE ? OR result_summary LIKE ?)")
        params.extend([f"%{query}%"] * 3)
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    rows = conn.execute(
        f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC LIMIT ?",
        params + [limit],
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def audit_prune(days=30):
    """Delete audit entries older than N days."""
    conn = _get_conn()
    cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(days=days)
    ).isoformat(timespec="seconds") + "Z"
    conn.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
    conn.commit()


# ── Cross-Session Recall (FTS5 on messages) ───────────────────────────────

@_locked
def search_messages_fts(query, limit=10):
    """Search ALL messages across ALL sessions via FTS5."""
    conn = _get_conn()
    # Sanitize query for FTS5 — use OR for each word
    words = [w.strip(".,!?;:'\"()[]") for w in query.split() if len(w.strip(".,!?;:'\"()[]")) >= 2]
    if not words:
        return []
    fts_query = " OR ".join(words[:8])
    try:
        rows = conn.execute(
            "SELECT f.content, f.session_id, f.role, f.rank, "
            "s.title as session_title, m.timestamp "
            "FROM messages_fts f "
            "JOIN messages m ON m.rowid = f.rowid "
            "JOIN sessions s ON m.session_id = s.id "
            "WHERE messages_fts MATCH ? "
            "ORDER BY f.rank LIMIT ?",
            (fts_query, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        # Fallback to LIKE search
        return _search_messages_like(query, limit)


def _search_messages_like(query, limit=10):
    """Fallback cross-session message search using LIKE.

    NOTE: This is called from search_messages_fts which already holds the lock,
    so we access _get_conn() directly (RLock allows re-entry from same thread).
    """
    conn = _get_conn()
    pattern = f"%{query}%"
    rows = conn.execute(
        "SELECT m.content, m.session_id, m.role, m.timestamp, "
        "s.title as session_title "
        "FROM messages m JOIN sessions s ON m.session_id = s.id "
        "WHERE m.content LIKE ? "
        "ORDER BY m.timestamp DESC LIMIT ?",
        (pattern, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── User Profile ──────────────────────────────────────────────────────────

@_locked
def upsert_user_profile(key, value, confidence=0.5, source="inferred"):
    """Insert or update a user profile entry. Confidence increases on repeat."""
    conn = _get_conn()
    now = _now()
    existing = conn.execute(
        "SELECT confidence FROM user_profile WHERE key = ?", (key,)
    ).fetchone()
    if existing:
        # Increase confidence on repeated observation (cap at 1.0)
        new_conf = min(1.0, existing["confidence"] + 0.15)
        conn.execute(
            "UPDATE user_profile SET value = ?, confidence = ?, source = ?, "
            "updated_at = ? WHERE key = ?",
            (value, new_conf, source, now, key),
        )
    else:
        conn.execute(
            "INSERT INTO user_profile (key, value, confidence, source, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (key, value, confidence, source, now),
        )
    conn.commit()


@_locked
def get_user_profile():
    """Get all user profile entries, ordered by confidence DESC."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT key, value, confidence, source, updated_at "
        "FROM user_profile ORDER BY confidence DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def get_profile_value(key, default=None):
    """Get a single profile value."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM user_profile WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else default


# ── Task Metrics ──────────────────────────────────────────────────────────

@_locked
def save_task_metrics(session_id, task_summary, steps_count, errors_count,
                      duration_ms, success):
    """Record performance metrics for a completed task."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO task_metrics (session_id, task_summary, steps_count, "
        "errors_count, duration_ms, success, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (session_id, task_summary[:200], steps_count, errors_count,
         duration_ms, 1 if success else 0, _now()),
    )
    conn.commit()
    return cur.lastrowid


@_locked
def update_task_feedback(metric_id, feedback):
    """Record user feedback on a task."""
    conn = _get_conn()
    conn.execute(
        "UPDATE task_metrics SET user_feedback = ? WHERE id = ?",
        (feedback, metric_id),
    )
    conn.commit()


@_locked
def get_last_task_metric(session_id):
    """Get the most recent task metric for a session."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM task_metrics WHERE session_id = ? "
        "ORDER BY created_at DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    return dict(row) if row else None


@_locked
def get_task_metrics_summary(days=7):
    """Get aggregated task metrics for the last N days."""
    conn = _get_conn()
    cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(days=days)
    ).isoformat(timespec="seconds") + "Z"
    row = conn.execute(
        "SELECT COUNT(*) as total, "
        "SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes, "
        "AVG(steps_count) as avg_steps, "
        "AVG(errors_count) as avg_errors, "
        "AVG(duration_ms) as avg_duration_ms, "
        "SUM(CASE WHEN user_feedback = 'positive' THEN 1 ELSE 0 END) as positive_fb, "
        "SUM(CASE WHEN user_feedback = 'negative' THEN 1 ELSE 0 END) as negative_fb "
        "FROM task_metrics WHERE created_at >= ?",
        (cutoff,),
    ).fetchone()
    return dict(row) if row else {}


@_locked
def get_all_notes():
    """Get all memory notes (for consolidation)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, topic, content, source, created_at, updated_at "
        "FROM memory_notes ORDER BY updated_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def delete_note(note_id):
    """Delete a memory note by ID."""
    conn = _get_conn()
    conn.execute("DELETE FROM memory_notes WHERE id = ?", (note_id,))
    # Also remove from FTS
    try:
        conn.execute("DELETE FROM notes_fts WHERE rowid = ?", (note_id,))
    except Exception:
        pass
    conn.commit()


@_locked
def prune_old_dreams(days=30, min_score=0.3):
    """Delete low-quality dreams older than N days."""
    conn = _get_conn()
    cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(days=days)
    ).isoformat(timespec="seconds") + "Z"
    cur = conn.execute(
        "DELETE FROM dream_log WHERE created_at < ? AND score < ?",
        (cutoff, min_score),
    )
    conn.commit()
    return cur.rowcount


@_locked
def prune_stale_patterns(days=30, max_count=1):
    """Delete strategy patterns unused (low success) and old."""
    conn = _get_conn()
    cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(days=days)
    ).isoformat(timespec="seconds") + "Z"
    cur = conn.execute(
        "DELETE FROM strategy_patterns WHERE updated_at < ? AND success_count <= ?",
        (cutoff, max_count),
    )
    conn.commit()
    return cur.rowcount


# ── Thread-Safe Queries for Background Tasks ──────────────────────────────

@_locked
def get_recent_reflections(hours=48, limit=20):
    """Get recent reflections (thread-safe, for idle tasks)."""
    conn = _get_conn()
    cutoff = (
        datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
    ).isoformat(timespec="seconds") + "Z"
    rows = conn.execute(
        "SELECT tool, error, reflection FROM reflections "
        "WHERE created_at >= ? ORDER BY created_at DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def get_recent_successful_tasks(min_steps=4, limit=15):
    """Get recent successful multi-step tasks (thread-safe, for idle tasks)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT task_summary, steps_count FROM task_metrics "
        "WHERE success = 1 AND steps_count >= ? "
        "ORDER BY created_at DESC LIMIT ?",
        (min_steps, limit),
    ).fetchall()
    return [dict(r) for r in rows]


@_locked
def get_top_strategy_patterns(limit=5):
    """Get top strategy patterns by success count (thread-safe)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT pattern_text, success_count FROM strategy_patterns "
        "ORDER BY success_count DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]