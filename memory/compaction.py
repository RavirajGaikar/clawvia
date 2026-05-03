"""Context compaction — summarize old messages to stay within token budget."""

from memory import db
from llm.client import chat, LLMError
from utils.logger import get_logger

import config

log = get_logger("memory.compaction")


def estimate_tokens(text):
    """Rough token estimate: ~4 chars per token for English."""
    return len(text) // 4


def _estimate_session_tokens(session_id):
    """Estimate total tokens in a session's message history."""
    messages = db.get_messages(session_id, limit=200)
    total_chars = sum(len(m["content"]) for m in messages)
    return total_chars // 4, len(messages)


def needs_compaction(session_id):
    """Check if session has exceeded the compaction threshold."""
    token_est, msg_count = _estimate_session_tokens(session_id)
    threshold = config.COMPACTION_THRESHOLD_TOKENS
    return token_est > threshold and msg_count > config.POST_COMPACTION_KEEP_MESSAGES


def compact_session(session_id, instruction=None):
    """Compact a session by summarizing old messages.

    Keeps the most recent POST_COMPACTION_KEEP_MESSAGES messages intact
    and replaces everything older with an LLM-generated summary.

    Args:
        session_id: The session to compact.
        instruction: Optional focus instruction (e.g. "focus on decisions made").

    Returns:
        dict with compaction stats, or None if compaction was not needed.
    """
    keep_count = config.POST_COMPACTION_KEEP_MESSAGES
    all_messages = db.get_messages(session_id, limit=200)

    if len(all_messages) <= keep_count:
        log.info("Session #%d has only %d messages, skipping compaction", session_id, len(all_messages))
        return None

    # Split: old messages to summarize, recent to keep
    old_messages = all_messages[:-keep_count]
    old_tokens = estimate_tokens("".join(m["content"] for m in old_messages))

    if old_tokens < 500:
        log.info("Session #%d old messages only ~%d tokens, skipping", session_id, old_tokens)
        return None

    # Build summarization prompt
    summary = _summarize_messages(old_messages, instruction)
    if not summary:
        log.warning("Failed to generate summary for session #%d", session_id)
        return None

    # Replace old messages with summary in the database
    pre_count = len(all_messages)
    pre_tokens = estimate_tokens("".join(m["content"] for m in all_messages))

    db.replace_messages_with_summary(session_id, summary, keep_count)
    db.increment_compaction_count(session_id)

    # Index the summary into cross-session FTS so compacted conversations
    # remain searchable via the recall tool.
    try:
        db.add_message(session_id, "system", f"[Compaction Summary]\n{summary}")
    except Exception:
        pass

    post_messages = db.get_messages(session_id, limit=200)
    post_tokens = estimate_tokens("".join(m["content"] for m in post_messages))

    stats = {
        "session_id": session_id,
        "messages_before": pre_count,
        "messages_after": len(post_messages),
        "tokens_before": pre_tokens,
        "tokens_after": post_tokens,
        "tokens_saved": pre_tokens - post_tokens,
    }
    log.info("Compacted session #%d: %d→%d messages, ~%d tokens saved",
             session_id, pre_count, len(post_messages), pre_tokens - post_tokens)
    return stats


def _summarize_messages(messages, instruction=None):
    """Use the LLM to summarize a list of messages."""
    convo_lines = []
    for m in messages:
        role = m["role"].capitalize()
        content = m["content"]
        if len(content) > 800:
            content = content[:800] + "..."
        convo_lines.append(f"{role}: {content}")

    convo_text = "\n".join(convo_lines)
    # Cap input to summarizer
    if len(convo_text) > 8000:
        convo_text = convo_text[:8000] + "\n... (truncated)"

    focus = ""
    if instruction:
        focus = f"\nFocus: {instruction}"

    prompt_messages = [
        {
            "role": "system",
            "content": (
                "You are a conversation summarizer. Produce a concise summary of the "
                "conversation below. Preserve key facts, decisions, file paths, commands, "
                "results, and user preferences. Be factual and complete but brief."
                f"{focus}"
            ),
        },
        {
            "role": "user",
            "content": f"Summarize this conversation:\n\n{convo_text}",
        },
    ]

    try:
        summary = chat(prompt_messages, temperature=0.1, max_tokens=2048)
        return summary.strip()
    except LLMError as exc:
        log.error("Summarization failed: %s", exc)
        return None


def auto_compact_if_needed(session_id):
    """Check and auto-compact if the session exceeds the threshold.

    Returns compaction stats dict if compacted, None otherwise.
    """
    if needs_compaction(session_id):
        log.info("Auto-compacting session #%d (threshold exceeded)", session_id)
        return compact_session(session_id)
    return None
