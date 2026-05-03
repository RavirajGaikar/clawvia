"""Background Dreaming — periodic subconscious reflection, memory consolidation,
and self-improvement.

Phase 2: Dreams are now *actionable* — they produce structured output with
concrete action items (save notes, create goals, update user profile, create
skills) in addition to reflective insights. Dream frequency adapts based on
whether recent dreams produced useful actions.
"""

import os
import json
import datetime

import config
from memory import db
from llm.client import chat
from llm.json_parser import extract_json
from utils.logger import get_logger

log = get_logger("dreaming")

DREAMS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "DREAMS.md")

_DREAM_SYSTEM_PROMPT = """You are ClawVia's subconscious dreaming process.
Your job is to review recent memories and conversations, find patterns,
connections, and insights, then produce a structured dream entry.

You MUST respond with a JSON object in this exact format:
{
  "pattern": "1-2 sentence insight about a recurring theme or connection",
  "self_critique": "1 sentence about what you could do better",
  "actions": [
    {"type": "save_note", "topic": "topic-slug", "content": "the insight to remember"},
    {"type": "create_goal", "description": "what to achieve", "interval": "24h"},
    {"type": "update_profile", "key": "preference_name", "value": "observed value"}
  ]
}

Action types available:
- save_note: Save an insight or pattern to long-term memory
- create_goal: Set a proactive goal to work toward
- update_profile: Record something learned about the user

Rules:
- 0-3 actions per dream (only if genuinely useful — don't force actions)
- Focus on patterns, recurring themes, unresolved questions, creative connections
- Do NOT fabricate facts — only reflect on what you're given
- Self-critique should be specific and actionable
- If nothing interesting stands out, return empty actions array"""

_DREAM_USER_TEMPLATE = """Here are my recent memories and conversations from the last {hours} hours.
Reflect on them and produce a structured dream entry.

## Recent Memory Notes
{notes_section}

## Recent Conversations
{messages_section}

## User Profile
{profile_section}

## My Recent Performance
{metrics_section}

Respond with the JSON dream object."""


def _score_material(notes, messages):
    """Score how interesting the available material is (0.0 to 1.0)."""
    score = 0.0
    score += min(len(notes) * 0.1, 0.4)
    score += min(len(messages) * 0.02, 0.3)
    topics = {n["topic"] for n in notes}
    score += min(len(topics) * 0.05, 0.2)
    now = datetime.datetime.utcnow()
    for n in notes[:3]:
        try:
            updated = datetime.datetime.fromisoformat(n["updated_at"].replace("Z", "+00:00"))
            age_hours = (now.replace(tzinfo=updated.tzinfo) - updated).total_seconds() / 3600
            if age_hours < 2:
                score += 0.05
        except (ValueError, TypeError):
            pass
    return min(score, 1.0)


def _format_notes(notes):
    if not notes:
        return "(no recent notes)"
    lines = []
    for n in notes[:15]:
        content = n["content"]
        if len(content) > 300:
            content = content[:300] + "..."
        lines.append(f"- **{n['topic']}**: {content}")
    return "\n".join(lines)


def _format_messages(messages):
    if not messages:
        return "(no recent conversations)"
    lines = []
    for m in messages[:30]:
        role = m["role"].upper()
        content = m["content"]
        if len(content) > 200:
            content = content[:200] + "..."
        session = m.get("session_title", "?")
        lines.append(f"[{session}/{role}] {content}")
    return "\n".join(lines)


def _format_profile():
    """Format user profile for dream context."""
    try:
        entries = db.get_user_profile()
        if not entries:
            return "(no profile data yet)"
        lines = []
        for e in entries[:8]:
            lines.append(f"- {e['key']}: {e['value']} (confidence: {e['confidence']:.1f})")
        return "\n".join(lines)
    except Exception:
        return "(profile unavailable)"


def _format_metrics():
    """Format recent performance metrics for dream context."""
    try:
        summary = db.get_task_metrics_summary(days=7)
        if not summary or not summary.get("total"):
            return "(no recent metrics)"
        total = summary["total"]
        successes = summary.get("successes", 0)
        rate = (successes / total * 100) if total else 0
        avg_steps = summary.get("avg_steps", 0) or 0
        avg_errors = summary.get("avg_errors", 0) or 0
        pos = summary.get("positive_fb", 0) or 0
        neg = summary.get("negative_fb", 0) or 0
        return (
            f"Tasks: {total}, Success rate: {rate:.0f}%, "
            f"Avg steps: {avg_steps:.1f}, Avg errors: {avg_errors:.1f}, "
            f"Positive feedback: {pos}, Negative feedback: {neg}"
        )
    except Exception:
        return "(metrics unavailable)"


def dream_cycle(hours=24):
    """Run one dream cycle: gather material, score, generate dream, execute actions.

    Returns the dream text or None if material was too thin.
    """
    log.info("Dream cycle starting (looking back %dh)", hours)

    notes = db.get_recent_notes(hours=hours)
    messages = db.get_recent_messages_all(hours=hours)

    score = _score_material(notes, messages)
    log.info("Dream material score: %.2f (min: %.2f)", score, config.DREAM_MIN_SCORE)

    if score < config.DREAM_MIN_SCORE:
        log.info("Not enough material to dream — skipping")
        return None

    prompt = _DREAM_USER_TEMPLATE.format(
        hours=hours,
        notes_section=_format_notes(notes),
        messages_section=_format_messages(messages),
        profile_section=_format_profile(),
        metrics_section=_format_metrics(),
    )

    try:
        raw = chat(
            messages=[
                {"role": "system", "content": _DREAM_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            temperature=0.7,
            max_tokens=600,
        )
    except Exception as exc:
        log.error("Dream LLM call failed: %s", exc)
        return None

    if not raw or not raw.strip():
        log.warning("Dream produced empty output")
        return None

    # Parse structured dream output
    dream_data = extract_json(raw.strip())
    actions_executed = 0

    if dream_data and isinstance(dream_data, dict):
        pattern = dream_data.get("pattern", "")
        critique = dream_data.get("self_critique", "")
        actions = dream_data.get("actions", [])

        dream_text = pattern
        if critique:
            dream_text += f"\n\nSelf-critique: {critique}"

        # Execute dream actions
        if actions and isinstance(actions, list):
            actions_executed = _execute_dream_actions(actions)
            if actions_executed:
                dream_text += f"\n\n[Executed {actions_executed} action(s)]"
    else:
        # Fallback: treat as plain text dream (backward compatible)
        dream_text = raw.strip()

    # Save to database
    source_topics = ", ".join(n["topic"] for n in notes[:5])
    db.save_dream(dream_text, source_topics, len(messages), score)

    # Append to DREAMS.md
    _append_to_dreams_file(dream_text, score, actions_executed)

    # Update adaptive dream frequency
    _update_dream_frequency(actions_executed > 0)

    log.info("Dream saved (score=%.2f, actions=%d): %s",
             score, actions_executed, dream_text[:80])
    return dream_text


def _execute_dream_actions(actions):
    """Execute dream-generated actions. Returns count of successful actions.

    Safety: dreams can only write data (notes, goals, profile) — they CANNOT
    run shell commands, modify files, or call arbitrary tools.
    """
    executed = 0
    for action in actions[:3]:  # Cap at 3 actions per dream
        if not isinstance(action, dict):
            continue
        action_type = action.get("type", "")
        try:
            if action_type == "save_note":
                topic = action.get("topic", "").strip()
                content = action.get("content", "").strip()
                if topic and content:
                    db.save_note(topic, f"[dream insight] {content}", source="dream")
                    executed += 1
                    log.info("Dream action: saved note '%s'", topic)

            elif action_type == "create_goal":
                desc = action.get("description", "").strip()
                interval_str = action.get("interval", "24h").strip()
                if desc:
                    # Parse interval
                    interval_sec = _parse_interval(interval_str)
                    db.add_goal(desc, priority=7, check_interval=interval_sec)
                    executed += 1
                    log.info("Dream action: created goal '%s'", desc[:50])

            elif action_type == "update_profile":
                key = action.get("key", "").strip()
                value = action.get("value", "").strip()
                if key and value:
                    db.upsert_user_profile(key, value, confidence=0.3, source="dream")
                    executed += 1
                    log.info("Dream action: updated profile '%s'", key)

            # Audit log each action
            if executed:
                db.audit_log_event(
                    "dream_action", tool_name=action_type,
                    args_summary=json.dumps(action)[:200],
                )
        except Exception as exc:
            log.warning("Dream action '%s' failed: %s", action_type, exc)

    return executed


def _parse_interval(interval_str):
    """Parse interval like '24h', '2d', '30m' into seconds."""
    import re
    m = re.match(r'^(\d+)\s*([mhd])$', interval_str.lower())
    if not m:
        return 86400  # Default 24h
    amount = int(m.group(1))
    unit = m.group(2)
    if unit == 'm':
        return amount * 60
    elif unit == 'h':
        return amount * 3600
    elif unit == 'd':
        return amount * 86400
    return 86400


def _update_dream_frequency(was_useful):
    """Adapt dream interval based on whether dreams are producing useful output."""
    try:
        current = int(db.kv_get("dream_interval_hours", str(config.DREAM_INTERVAL_HOURS)))
        min_interval = int(db.kv_get("dream_min_interval", "2"))
        max_interval = int(db.kv_get("dream_max_interval", "12"))

        if was_useful:
            # Dreams are productive — dream more often (decrease interval)
            new_interval = max(min_interval, current - 1)
        else:
            # Dreams aren't producing actions — dream less often
            new_interval = min(max_interval, current + 1)

        if new_interval != current:
            db.kv_set("dream_interval_hours", str(new_interval))
            log.info("Dream frequency adjusted: %dh → %dh", current, new_interval)
    except Exception as exc:
        log.debug("Dream frequency update failed: %s", exc)


def get_dream_interval():
    """Get the current adaptive dream interval in hours."""
    try:
        return int(db.kv_get("dream_interval_hours", str(config.DREAM_INTERVAL_HOURS)))
    except Exception:
        return config.DREAM_INTERVAL_HOURS


def _append_to_dreams_file(dream_text, score, actions_count=0):
    """Append a dream entry to DREAMS.md."""
    now = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    actions_note = f", actions: {actions_count}" if actions_count else ""
    entry = f"\n## {now} (score: {score:.2f}{actions_note})\n{dream_text}\n"

    if not os.path.exists(DREAMS_PATH):
        with open(DREAMS_PATH, "w", encoding="utf-8") as f:
            f.write("# ClawVia Dreams\n\nSubconscious reflections and pattern recognition.\n")

    with open(DREAMS_PATH, "a", encoding="utf-8") as f:
        f.write(entry)
