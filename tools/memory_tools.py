"""Memory tools — save, search, and retrieve long-term agent memory."""

from tools.registry import registry
from memory import db
from utils.logger import get_logger

log = get_logger("tools.memory")


@registry.register(
    "memory_save",
    "Save a note to long-term memory. Use to remember facts, preferences, or important information.",
    {"topic": "string (category/topic name)", "content": "string (the information to remember)"},
)
def memory_save(topic, content):
    if not topic or not topic.strip():
        return "ERROR: Topic is required"
    if not content or not content.strip():
        return "ERROR: Content is required"
    topic = topic.strip().lower().replace(" ", "-")
    db.save_note(topic, content.strip())
    return f"Saved to memory under topic '{topic}'"


@registry.register(
    "memory_search",
    "Search long-term memory for relevant notes. Returns matching notes ranked by relevance.",
    {"query": "string (search keywords)"},
)
def memory_search(query):
    if not query or not query.strip():
        return "ERROR: Query is required"
    results = db.search_notes_fts(query.strip(), limit=5)
    if not results:
        return "No matching memories found."
    lines = []
    for r in results:
        content = r["content"]
        if len(content) > 400:
            content = content[:400] + "..."
        lines.append(f"[{r['topic']}] (updated {r['updated_at'][:10]})\n{content}")
    return "\n\n".join(lines)


@registry.register(
    "memory_get",
    "Get a specific memory note by topic name.",
    {"topic": "string (exact topic name)"},
)
def memory_get(topic):
    if not topic or not topic.strip():
        return "ERROR: Topic is required"
    topic = topic.strip().lower().replace(" ", "-")
    note = db.get_note(topic)
    if not note:
        # Try search as fallback
        results = db.search_notes_fts(topic, limit=1)
        if results:
            note = results[0]
        else:
            return f"No memory found for topic '{topic}'"
    return f"Topic: {note['topic']}\nUpdated: {note['updated_at']}\n\n{note['content']}"


@registry.register(
    "audit_search",
    "Search the audit log of past actions (commands executed, approvals, scheduled runs). "
    "Use to review what happened, diagnose issues, or verify past actions.",
    {
        "query": "string? (search text in tool names, args, or results)",
        "action_type": "string? (filter: 'command_exec', 'approval', 'scheduled_run')",
        "limit": "integer? (max results, default 10)",
    },
)
def audit_search_tool(query=None, action_type=None, limit=10):
    try:
        limit = max(1, min(50, int(limit)))
    except (TypeError, ValueError):
        limit = 10
    results = db.audit_search(query=query, action_type=action_type, limit=limit)
    if not results:
        return "No audit log entries found."
    lines = []
    for r in results:
        line = f"[{r['timestamp']}] {r['action_type']}"
        if r.get("tool_name"):
            line += f" | {r['tool_name']}"
        if r.get("args_summary"):
            line += f" | {r['args_summary'][:80]}"
        if r.get("result_summary"):
            line += f"\n  → {r['result_summary'][:100]}"
        lines.append(line)
    return "\n".join(lines)


# ── Cross-Session Recall ──────────────────────────────────────────────────

@registry.register(
    "recall",
    "Search ALL past conversations across ALL sessions. Use to remember what was "
    "discussed before, find previous solutions, or recall context from other sessions. "
    "Different from memory_search (which searches saved notes).",
    {"query": "string (search keywords)", "limit": "integer? (max results, default 5)"},
)
def recall(query, limit=5):
    if not query or not query.strip():
        return "ERROR: Query is required"
    try:
        limit = max(1, min(20, int(limit)))
    except (TypeError, ValueError):
        limit = 5
    results = db.search_messages_fts(query.strip(), limit=limit)
    if not results:
        return "No matching past conversations found."
    lines = []
    for r in results:
        content = r["content"]
        if len(content) > 300:
            content = content[:300] + "..."
        session = r.get("session_title", "?")
        ts = r.get("timestamp", "?")[:16]
        role = r["role"].capitalize()
        lines.append(f"[Session: {session}] [{ts}] {role}:\n{content}")
    return "\n\n".join(lines)


# ── Skill Creation & Editing ──────────────────────────────────────────────

@registry.register(
    "skill_create",
    "Create a new reusable skill from experience. Skills are procedural memory — "
    "step-by-step instructions for tasks you've learned to do well.",
    {
        "name": "string (short slug name, e.g. 'web-scraper')",
        "description": "string (one-line description)",
        "triggers": "string (comma-separated trigger phrases)",
        "content": "string (the full skill instructions in markdown)",
    },
)
def skill_create(name, description, triggers, content):
    if not name or not name.strip():
        return "ERROR: Name is required"
    if not content or not content.strip():
        return "ERROR: Content is required"
    from skills.loader import create_skill_file, reload_skills
    result = create_skill_file(
        name=name.strip(),
        description=(description or "").strip(),
        triggers=(triggers or "").strip(),
        content=content.strip(),
    )
    if result.startswith("ERROR"):
        return result
    reload_skills()
    db.audit_log_event("skill_created", tool_name="skill_create",
                       args_summary=f"name={name}")
    return result


@registry.register(
    "skill_edit",
    "Update an existing skill's instructions. Use when you've found a better approach "
    "for a task the skill covers. Preserves the skill's frontmatter (name, triggers).",
    {
        "name": "string (skill name to edit)",
        "new_content": "string (updated skill instructions in markdown)",
    },
)
def skill_edit(name, new_content):
    if not name or not name.strip():
        return "ERROR: Name is required"
    if not new_content or not new_content.strip():
        return "ERROR: Content is required"
    from skills.loader import edit_skill_file, reload_skills
    result = edit_skill_file(name=name.strip(), new_content=new_content.strip())
    if result.startswith("ERROR"):
        return result
    reload_skills()
    db.audit_log_event("skill_edited", tool_name="skill_edit",
                       args_summary=f"name={name}")
    return result
