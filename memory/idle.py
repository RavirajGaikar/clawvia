"""Idle-time self-improvement — background tasks the agent runs when the user
is inactive.

Each idle task type has a cooldown so it doesn't repeat too often. Tasks are
prioritized and the highest-priority one that's past its cooldown runs first.
"""

import datetime

from memory import db
from llm.client import chat, LLMError
from utils.logger import get_logger

log = get_logger("memory.idle")

# Idle task definitions: (name, cooldown_hours, priority, description)
_IDLE_TASKS = [
    ("error_review",         12, 1, "Review recent errors and find patterns"),
    ("memory_consolidation", 24, 2, "Merge duplicate/overlapping notes"),
    ("profile_enrichment",    8, 3, "Extract user preferences from recent conversations"),
    ("skill_audit",          48, 4, "Check if common patterns should become skills"),
    ("goal_review",          24, 5, "Review stale goals and suggest updates"),
    ("self_review",         168, 6, "Weekly performance self-review"),
]


def get_next_idle_task():
    """Return the highest-priority idle task that's past its cooldown, or None."""
    now = datetime.datetime.utcnow()

    for name, cooldown_hours, priority, description in _IDLE_TASKS:
        last_run_str = db.kv_get(f"idle_last:{name}")
        if last_run_str:
            try:
                last_run = datetime.datetime.fromisoformat(
                    last_run_str.replace("Z", "+00:00")
                ).replace(tzinfo=None)
                if (now - last_run).total_seconds() < cooldown_hours * 3600:
                    continue  # Still in cooldown
            except (ValueError, TypeError):
                pass  # Treat as never run

        return {
            "name": name,
            "priority": priority,
            "description": description,
        }

    return None  # All tasks in cooldown


def mark_idle_task_done(name):
    """Record that an idle task was just completed."""
    now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    db.kv_set(f"idle_last:{name}", now)


def run_idle_task(task_name):
    """Execute an idle self-improvement task. Returns result string or None."""
    runners = {
        "error_review": _review_errors,
        "memory_consolidation": _consolidate_notes,
        "profile_enrichment": _enrich_profile,
        "skill_audit": _audit_skills,
        "goal_review": _review_goals,
        "self_review": _self_review,
    }

    runner = runners.get(task_name)
    if not runner:
        log.warning("Unknown idle task: %s", task_name)
        return None

    log.info("Running idle task: %s", task_name)
    try:
        result = runner()
        mark_idle_task_done(task_name)
        db.audit_log_event("idle_task", tool_name=task_name,
                           result_summary=(result or "")[:200])
        return result
    except Exception as exc:
        log.error("Idle task '%s' failed: %s", task_name, exc)
        return None


# ── Individual idle task implementations ──────────────────────────────────

def _review_errors():
    """Scan recent reflections for patterns across sessions."""
    rows = db.get_recent_reflections(hours=48, limit=20)

    if len(rows) < 2:
        return "Not enough recent errors to analyze."

    errors_text = "\n".join(
        f"- Tool: {r['tool']}, Error: {r['error'][:100]}, "
        f"Reflection: {r['reflection'][:100]}"
        for r in rows
    )

    try:
        analysis = chat(
            [{"role": "user", "content": (
                "Review these recent errors and find patterns. What mistakes "
                "keep happening? What should I remember to avoid them?\n\n"
                f"{errors_text}\n\n"
                "Respond in 2-3 sentences with actionable insights."
            )}],
            temperature=0.2,
            max_tokens=200,
        )
    except LLMError:
        return None

    if analysis and analysis.strip():
        db.save_note("error-patterns", analysis.strip(), source="idle")
        return f"Error pattern analysis saved: {analysis.strip()[:100]}"

    return None


def _consolidate_notes():
    """Find and merge duplicate/overlapping notes."""
    notes = db.get_all_notes()
    if len(notes) < 5:
        return "Not enough notes to consolidate."

    # Group by similar topics
    topic_groups = {}
    for n in notes:
        base = n["topic"].split("-")[0] if "-" in n["topic"] else n["topic"]
        topic_groups.setdefault(base, []).append(n)

    merged = 0
    for base, group in topic_groups.items():
        if len(group) < 2:
            continue
        # Check if any pair has very similar content
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                # Simple overlap check: if one topic is a prefix of another
                if (a["topic"] == b["topic"] or
                        a["topic"].startswith(b["topic"]) or
                        b["topic"].startswith(a["topic"])):
                    # Merge: keep the one with more content, append the other
                    if len(a["content"]) >= len(b["content"]):
                        if b["content"] not in a["content"]:
                            db.save_note(a["topic"], b["content"], source="merge")
                        db.delete_note(b["id"])
                    else:
                        if a["content"] not in b["content"]:
                            db.save_note(b["topic"], a["content"], source="merge")
                        db.delete_note(a["id"])
                    merged += 1
                    break
            if merged >= 3:
                break  # Don't merge too many at once
        if merged >= 3:
            break

    # Prune old low-value dreams
    pruned = db.prune_old_dreams(days=30, min_score=0.3)

    # Prune stale strategy patterns
    stale = db.prune_stale_patterns(days=30, max_count=1)

    result = f"Consolidated: {merged} note merges, {pruned} old dreams pruned, {stale} stale patterns pruned"
    return result


def _enrich_profile():
    """Review recent conversations for user preferences not yet captured."""
    messages = db.get_recent_messages_all(hours=24, limit=30)
    if len(messages) < 5:
        return "Not enough recent conversations for profile enrichment."

    existing = db.get_user_profile()
    existing_keys = {e["key"] for e in existing}

    convo = "\n".join(
        f"{m['role']}: {m['content'][:150]}"
        for m in messages[:20]
    )

    try:
        raw = chat(
            [{"role": "user", "content": (
                "From these recent conversations, extract user preferences, "
                "habits, or personal info. Only extract what's clearly stated "
                "or strongly implied.\n\n"
                f"Already known: {', '.join(existing_keys) if existing_keys else 'nothing'}\n\n"
                f"Conversations:\n{convo}\n\n"
                "Respond with a JSON object: {\"key\": \"value\"} for each new "
                "preference found. Keys should be: name, timezone, language, "
                "technical_level, common_tasks, preferences, interests, "
                "communication_style, work_hours. Empty {} if nothing new."
            )}],
            temperature=0.1,
            max_tokens=200,
        )
    except LLMError:
        return None

    if not raw:
        return None

    from llm.json_parser import extract_json
    data = extract_json(raw.strip())
    if not data or not isinstance(data, dict):
        return "No new profile data extracted."

    count = 0
    for key, value in data.items():
        if isinstance(value, str) and value.strip():
            db.upsert_user_profile(key, value.strip(), confidence=0.4, source="idle")
            count += 1

    return f"Profile enrichment: {count} entries updated" if count else "No new profile data."


def _audit_skills():
    """Check if common task patterns should become skills."""
    rows = db.get_recent_successful_tasks(min_steps=4, limit=15)

    if len(rows) < 3:
        return "Not enough task history for skill audit."

    tasks_text = "\n".join(f"- {r['task_summary']} ({r['steps_count']} steps)" for r in rows)

    # Check existing skills
    try:
        from skills.loader import load_skills
        existing = [s["name"] for s in load_skills()]
    except Exception:
        existing = []

    try:
        analysis = chat(
            [{"role": "user", "content": (
                f"Existing skills: {', '.join(existing) if existing else 'none'}\n\n"
                f"Recent successful multi-step tasks:\n{tasks_text}\n\n"
                "Are there any recurring task patterns that should become reusable "
                "skills? If yes, suggest 1-2 skill names and what they'd do. "
                "If no, say 'no new skills needed'. Be brief (2-3 sentences)."
            )}],
            temperature=0.2,
            max_tokens=200,
        )
    except LLMError:
        return None

    if analysis and analysis.strip():
        db.save_note("skill-audit", analysis.strip(), source="idle")
        return f"Skill audit: {analysis.strip()[:100]}"

    return None


def _review_goals():
    """Check for stale goals and suggest updates."""
    goals = db.list_goals(include_inactive=False)
    if not goals:
        return "No active goals to review."

    stale = []
    now = datetime.datetime.utcnow()
    for g in goals:
        try:
            created = datetime.datetime.fromisoformat(
                g["created_at"].replace("Z", "+00:00")
            ).replace(tzinfo=None)
            age_days = (now - created).days
            if age_days > 7 and not g.get("progress_notes", "").strip():
                stale.append(g)
        except (ValueError, TypeError):
            pass

    if not stale:
        return "All goals have recent progress."

    stale_text = "\n".join(
        f"- #{g['id']}: {g['description']} (created {g['created_at'][:10]})"
        for g in stale[:5]
    )

    db.save_note("stale-goals", f"Goals with no progress:\n{stale_text}", source="idle")
    return f"Found {len(stale)} stale goal(s) with no progress"


def _self_review():
    """Weekly performance self-review — the meta-learning core."""
    metrics = db.get_task_metrics_summary(days=7)
    if not metrics or not metrics.get("total"):
        return "Not enough data for self-review."

    # Gather strategy patterns (thread-safe)
    patterns = db.get_top_strategy_patterns(limit=5)

    profile = db.get_user_profile()

    context_parts = [
        f"## Performance (last 7 days)",
        f"Tasks: {metrics['total']}, "
        f"Success: {metrics.get('successes', 0)}/{metrics['total']}, "
        f"Avg steps: {metrics.get('avg_steps', 0) or 0:.1f}, "
        f"Avg errors: {metrics.get('avg_errors', 0) or 0:.1f}",
        f"Positive feedback: {metrics.get('positive_fb', 0) or 0}, "
        f"Negative feedback: {metrics.get('negative_fb', 0) or 0}",
    ]

    if patterns:
        context_parts.append("\n## Top Strategies")
        for p in patterns:
            context_parts.append(f"- (used {p['success_count']}x) {p['pattern_text'][:100]}")

    if profile:
        context_parts.append("\n## User Profile")
        for e in profile[:5]:
            context_parts.append(f"- {e['key']}: {e['value']}")

    try:
        review = chat(
            [{"role": "user", "content": (
                "You are reviewing your own performance as an AI agent. "
                "Based on the data below, write a brief self-review:\n"
                "1. What am I doing well?\n"
                "2. What should I improve?\n"
                "3. Any skills I should create?\n\n"
                + "\n".join(context_parts) +
                "\n\nBe specific and actionable. 3-5 sentences."
            )}],
            temperature=0.3,
            max_tokens=300,
        )
    except LLMError:
        return None

    if review and review.strip():
        db.save_note("self-review", review.strip(), source="self_review")
        return f"Self-review completed: {review.strip()[:100]}"

    return None
