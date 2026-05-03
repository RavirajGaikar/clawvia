"""Todo tools — TodoWrite-style task tracking for the agent.

The agent uses these to maintain an explicit checklist on multi-step
tasks. Lists are scoped to the active session and persisted in SQLite,
so they survive bot restarts and show up in `/todo` from Telegram.

Statuses: open | in_progress | done | cancelled
"""

from memory import db
from tools.registry import registry

_VALID_STATUS = {"open", "in_progress", "done", "cancelled"}


def _active_session_id():
    return db.get_active_session()["id"]


@registry.register(
    "todo_add",
    "Append a single todo item to the current session's checklist. "
    "Use one item per call. Returns the new todo id.",
    {"text": "string"},
)
def todo_add(text):
    text = (text or "").strip()
    if not text:
        return "ERROR: empty todo text"
    todo_id = db.todo_add(_active_session_id(), text)
    return f"todo #{todo_id} added: {text}"


@registry.register(
    "todo_update",
    "Update a todo's status (open|in_progress|done|cancelled) or text. "
    "Pass either status, text, or both.",
    {"id": "integer", "status": "string?", "text": "string?"},
)
def todo_update(id=None, status=None, text=None):
    if id is None:
        return "ERROR: 'id' is required"
    try:
        todo_id = int(id)
    except (TypeError, ValueError):
        return f"ERROR: bad id: {id!r}"
    if status is not None and status not in _VALID_STATUS:
        return f"ERROR: status must be one of {sorted(_VALID_STATUS)}"
    ok = db.todo_update(_active_session_id(), todo_id, status=status, text=text)
    if not ok:
        return f"ERROR: todo #{todo_id} not found in this session"
    return f"todo #{todo_id} updated"


@registry.register(
    "todo_list",
    "List todos for the current session. "
    "Set include_done=false to hide completed/cancelled items.",
    {"include_done": "boolean?"},
    cacheable=False,
)
def todo_list(include_done=True):
    items = db.todo_list(_active_session_id(), include_done=bool(include_done))
    if not items:
        return "(no todos)"
    icons = {"open": "[ ]", "in_progress": "[~]", "done": "[x]", "cancelled": "[-]"}
    return "\n".join(
        f"{icons.get(t['status'], '[?]')} #{t['id']} {t['text']}"
        for t in items
    )


@registry.register(
    "todo_clear",
    "Delete all todos for the current session. Use sparingly.",
    {},
)
def todo_clear():
    n = db.todo_clear(_active_session_id())
    return f"cleared {n} todos"
