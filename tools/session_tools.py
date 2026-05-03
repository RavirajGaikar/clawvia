"""Session management tools — let the agent manage conversation sessions."""

from tools.registry import registry
from utils.logger import get_logger

log = get_logger("tools.session")

# This will be set by the agent controller at startup
# to avoid circular imports
_db = None


def set_db(db_module):
    """Called by agent.py to inject the memory.db module."""
    global _db
    _db = db_module


@registry.register(
    "sessions_list",
    "List all conversation sessions with their IDs and titles.",
    {},
)
def sessions_list():
    if _db is None:
        return "ERROR: Session system not initialized"
    sessions = _db.list_sessions()
    if not sessions:
        return "No sessions found."
    lines = []
    for s in sessions:
        active = " (active)" if s["is_active"] else ""
        lines.append(f"#{s['id']}: {s['title']}{active}  [{s['created_at']}]")
    return "\n".join(lines)


@registry.register(
    "sessions_new",
    "Create a new conversation session and switch to it.",
    {"title": "string (optional session title)"},
)
def sessions_new(title="New Session"):
    if _db is None:
        return "ERROR: Session system not initialized"
    session = _db.create_session(title)
    return f"Created and switched to session #{session['id']}: {session['title']}"


@registry.register(
    "sessions_clear",
    "Clear the conversation history of the current active session.",
    {},
)
def sessions_clear():
    if _db is None:
        return "ERROR: Session system not initialized"
    session = _db.get_active_session()
    if not session:
        return "ERROR: No active session"
    _db.clear_messages(session["id"])
    return f"Cleared history for session #{session['id']}"


@registry.register(
    "sessions_history",
    "Retrieve conversation history from any session. Useful for reviewing past conversations.",
    {"session_id": "integer (session ID to read)", "limit": "integer (optional, max messages, default 20)"},
)
def sessions_history(session_id, limit=20):
    if _db is None:
        return "ERROR: Session system not initialized"
    session_id = int(session_id)
    limit = min(int(limit), 100)

    # Verify session exists
    session = _db.get_session_history(session_id)
    if not session:
        return f"ERROR: Session #{session_id} not found"

    info = session["session"]
    msgs = session["messages"][-limit:]
    if not msgs:
        return f"Session #{session_id} ({info['title']}) has no messages."

    lines = [f"Session #{session_id}: {info['title']}  (compacted {info.get('compaction_count', 0)}x)"]
    lines.append(f"Messages ({len(msgs)} of {len(session['messages'])}):")
    lines.append("---")
    for m in msgs:
        role = m["role"].upper()
        content = m["content"]
        if len(content) > 500:
            content = content[:500] + "..."
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)


@registry.register(
    "sessions_send",
    "Send a message to a different session without switching to it. "
    "The message is stored as an 'assistant' message in that session.",
    {"session_id": "integer (target session ID)", "message": "string (message content)"},
)
def sessions_send(session_id, message):
    if _db is None:
        return "ERROR: Session system not initialized"
    session_id = int(session_id)

    # Verify session exists
    history = _db.get_session_history(session_id)
    if not history:
        return f"ERROR: Session #{session_id} not found"

    _db.add_message(session_id, "assistant", message)
    title = history["session"]["title"]
    return f"Message sent to session #{session_id} ({title})"
