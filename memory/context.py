"""Context builder — assembles conversation context for the planner.

Memory tiers surfaced here:
  * working   — current step list (lives in agent.py, not this file)
  * session   — recent messages + recent task results
  * user      — persistent user profile (always included)
  * episodic  — top-N FTS-matched memory notes for the current task
                + most-recent failure reflection from this session
                + cross-session recall (past conversations)
  * repo      — soul.md + skills (injected by llm/prompts.py, not here)

Episodic surfacing is opt-in: callers pass `task=...` to enable it. When
no task is given (e.g. background dream cycles), behaviour is unchanged.
"""

from memory import db
from utils.logger import get_logger

log = get_logger("memory.context")

_MAX_CONTEXT_CHARS = 12000
_EPISODIC_NOTE_LIMIT = 5
_NOTE_PREVIEW_CHARS = 400


def _episodic_notes_for(task):
    """Pull a few topical notes via FTS. Empty string on any failure."""
    if not task:
        return ""
    # Use the first ~6 meaningful words as the FTS query — keeps it cheap and
    # avoids FTS5 syntax errors from punctuation in long sentences.
    words = [w.strip(".,!?;:'\"()[]") for w in task.split()]
    keywords = [w for w in words if len(w) >= 3][:6]
    if not keywords:
        return ""
    query = " OR ".join(keywords)
    try:
        notes = db.search_notes_fts(query, limit=_EPISODIC_NOTE_LIMIT)
    except Exception as exc:
        log.debug("Episodic note lookup failed: %s", exc)
        return ""
    if not notes:
        return ""
    lines = []
    for n in notes:
        preview = (n.get("content") or "")[:_NOTE_PREVIEW_CHARS]
        if len(n.get("content") or "") > _NOTE_PREVIEW_CHARS:
            preview += "..."
        lines.append(f"- [{n.get('topic')}] {preview}")
    return "## Relevant Memory\n" + "\n".join(lines)


def _recent_reflection(session_id):
    """Most recent reflection text for this session, if any."""
    try:
        rows = db.list_reflections(session_id, limit=1)
    except Exception as exc:
        log.debug("Reflection lookup failed: %s", exc)
        return ""
    if not rows:
        return ""
    r = rows[0]
    return (
        "## Recent Reflection\n"
        f"(tool={r['tool']}) {r['reflection'][:300]}"
    )


def _user_profile_block():
    """Build a compact user profile section from persistent observations."""
    try:
        entries = db.get_user_profile()
    except Exception as exc:
        log.debug("User profile lookup failed: %s", exc)
        return ""
    if not entries:
        return ""
    # Only show entries with reasonable confidence
    strong = [e for e in entries if e["confidence"] >= 0.4]
    if not strong:
        return ""
    lines = ["## User Profile"]
    total = 0
    for e in strong[:10]:
        line = f"- {e['key']}: {e['value']}"
        if total + len(line) > 400:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def _cross_session_recall(task):
    """Find relevant snippets from past conversations across all sessions."""
    if not task:
        return ""
    words = [w.strip(".,!?;:'\"()[]") for w in task.split()]
    keywords = [w for w in words if len(w) >= 3][:6]
    if not keywords:
        return ""
    query = " ".join(keywords)
    try:
        results = db.search_messages_fts(query, limit=3)
    except Exception as exc:
        log.debug("Cross-session recall failed: %s", exc)
        return ""
    if not results:
        return ""
    lines = ["## Past Conversations"]
    total = 0
    for r in results:
        content = r["content"][:200]
        session = r.get("session_title", "?")
        role = r["role"].capitalize()
        line = f"- [{session}/{role}] {content}"
        if total + len(line) > 800:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def build_context(session_id, max_messages=10, task=None):
    """Build a context string from recent conversation history and past tasks.

    Args:
        session_id: active session.
        max_messages: how many recent messages to include.
        task: optional current task text — when given, episodic memory
            (matching notes + last reflection) is appended.

    Returns:
        Formatted string ready for prompt injection, or empty string.
    """
    parts = []

    # User profile — always included (persistent understanding of the user)
    profile_block = _user_profile_block()
    if profile_block:
        parts.append(profile_block)

    # Recent conversation messages
    messages = db.get_messages(session_id, limit=max_messages)
    if messages:
        conv_lines = []
        for m in messages:
            role = m["role"].capitalize()
            content = m["content"]
            if len(content) > 1000:
                content = content[:1000] + "..."
            conv_lines.append(f"{role}: {content}")
        parts.append("## Recent Conversation\n" + "\n".join(conv_lines))

    # Recent task results (for richer context)
    tasks = db.get_recent_tasks(session_id, limit=3)
    if tasks:
        task_lines = []
        for t in tasks:
            result_preview = t["result"] or ""
            if len(result_preview) > 600:
                result_preview = result_preview[:600] + "..."
            task_lines.append(f"- Task: {t['task']}\n  Result: {result_preview}")
        parts.append("## Recent Task Results\n" + "\n".join(task_lines))

    # Episodic tier — only when we have a task to query against.
    if task:
        notes_block = _episodic_notes_for(task)
        if notes_block:
            parts.append(notes_block)
        refl_block = _recent_reflection(session_id)
        if refl_block:
            parts.append(refl_block)

        # Strategy patterns — what worked before on similar tasks
        try:
            from memory.patterns import recall_strategies
            strategy_block = recall_strategies(task)
            if strategy_block:
                parts.append(strategy_block)
        except Exception:
            pass

        # Cross-session recall — relevant snippets from past conversations
        recall_block = _cross_session_recall(task)
        if recall_block:
            parts.append(recall_block)

        # Recent dream insights
        try:
            dreams = db.get_recent_dreams(limit=2)
            if dreams:
                dream_lines = ["## Recent Insights"]
                for d in dreams:
                    text = d["dream_text"][:250]
                    dream_lines.append(f"- {text}")
                parts.append("\n".join(dream_lines))
        except Exception:
            pass

    context = "\n\n".join(parts)

    # Truncate if too long
    if len(context) > _MAX_CONTEXT_CHARS:
        context = context[:_MAX_CONTEXT_CHARS] + "\n... (context truncated)"

    return context
