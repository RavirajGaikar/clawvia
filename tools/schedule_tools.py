"""Schedule tools — create, list, and cancel scheduled tasks."""

from tools.registry import registry
from memory import db
from scheduler import parse_schedule
from utils.logger import get_logger

log = get_logger("tools.schedule")


@registry.register(
    "schedule_task",
    "Schedule a task to run later or on a recurring basis. "
    "Formats: 'in 20m', 'in 2h', 'at 2024-01-01T12:00:00Z', 'every 30m', 'every 2h', 'every 1d'.",
    {"prompt": "string (what the agent should do)", "schedule": "string (when to run)"},
)
def schedule_task(prompt, schedule):
    if not prompt or not prompt.strip():
        return "ERROR: Prompt is required"
    if not schedule or not schedule.strip():
        return "ERROR: Schedule is required"

    try:
        stype, svalue, next_run = parse_schedule(schedule)
    except ValueError as exc:
        return f"ERROR: {exc}"

    task_id = db.add_scheduled_task(prompt.strip(), stype, svalue, next_run)
    kind = "one-shot" if stype == "at" else "recurring"
    return f"Scheduled {kind} task #{task_id}: '{prompt[:60]}' — next run: {next_run}"


@registry.register(
    "list_schedules",
    "List all active scheduled tasks.",
    {},
)
def list_schedules():
    tasks = db.list_scheduled_tasks(include_inactive=False)
    if not tasks:
        return "No active scheduled tasks."
    lines = []
    for t in tasks:
        kind = "one-shot" if t["schedule_type"] == "at" else f"every {t['schedule_value']}"
        lines.append(
            f"#{t['id']}: [{kind}] {t['prompt'][:60]} — next: {t['next_run']}"
        )
    return "\n".join(lines)


@registry.register(
    "cancel_schedule",
    "Cancel a scheduled task by its ID.",
    {"task_id": "integer (the schedule ID to cancel)"},
)
def cancel_schedule(task_id):
    try:
        task_id = int(task_id)
    except (TypeError, ValueError):
        return "ERROR: task_id must be an integer"
    if db.cancel_scheduled_task(task_id):
        return f"Cancelled scheduled task #{task_id}"
    return f"ERROR: Scheduled task #{task_id} not found"


# ── Goal Tools ────────────────────────────────────────────────────────────

@registry.register(
    "goal_set",
    "Set a persistent goal that the agent will check on periodically. "
    "Goals are tracked across sessions and checked automatically.",
    {
        "description": "string (what you want to achieve)",
        "priority": "integer? (1-10, lower = more important, default 5)",
        "check_interval": "string? (how often to check: '30m', '2h', '1d'. default '1h')",
    },
)
def goal_set(description, priority=5, check_interval="1h"):
    if not description or not description.strip():
        return "ERROR: Description is required"
    try:
        priority = max(1, min(10, int(priority)))
    except (TypeError, ValueError):
        priority = 5

    # Parse check_interval to seconds
    import re
    m = re.match(r"(\d+)\s*(m|h|d)", str(check_interval).lower())
    if m:
        val, unit = int(m.group(1)), m.group(2)
        seconds = val * {"m": 60, "h": 3600, "d": 86400}[unit]
    else:
        seconds = 3600  # default 1 hour

    # Minimum 5 minutes
    seconds = max(300, seconds)

    goal_id = db.add_goal(description.strip(), priority, seconds)
    return f"Goal #{goal_id} set: '{description[:80]}' (priority {priority}, check every {check_interval})"


@registry.register(
    "goal_update",
    "Update progress on a goal or mark it complete/cancelled.",
    {
        "goal_id": "integer (goal ID)",
        "progress": "string? (progress note to append)",
        "status": "string? ('active', 'completed', 'cancelled')",
    },
)
def goal_update(goal_id, progress=None, status=None):
    try:
        goal_id = int(goal_id)
    except (TypeError, ValueError):
        return "ERROR: goal_id must be an integer"

    if status and status not in ("active", "completed", "cancelled"):
        return "ERROR: status must be 'active', 'completed', or 'cancelled'"

    db.update_goal(goal_id, progress=progress, status=status)
    parts = []
    if progress:
        parts.append(f"progress noted")
    if status:
        parts.append(f"status → {status}")
    return f"Goal #{goal_id} updated: {', '.join(parts) or 'no changes'}"


@registry.register(
    "goal_list",
    "List all active goals with their progress and next check time.",
    {"include_inactive": "boolean? (include completed/cancelled goals, default false)"},
)
def goal_list(include_inactive=False):
    if isinstance(include_inactive, str):
        include_inactive = include_inactive.lower() in ("true", "1", "yes")
    goals = db.list_goals(include_inactive=include_inactive)
    if not goals:
        return "No active goals."
    lines = []
    for g in goals:
        status_icon = {"active": "🎯", "completed": "✅", "cancelled": "❌"}.get(g["status"], "?")
        progress = g["progress_notes"][-100:] if g["progress_notes"] else "no progress yet"
        lines.append(
            f"{status_icon} #{g['id']} [P{g['priority']}]: {g['description'][:60]}\n"
            f"   Next check: {g['next_check']} | Progress: {progress}"
        )
    return "\n".join(lines)


# ── Watcher Tools ─────────────────────────────────────────────────────────

@registry.register(
    "watch_create",
    "Create an event watcher that triggers an action when a condition is met. "
    "Event types: 'battery_low' (threshold%), 'storage_low' (threshold_mb), "
    "'time_of_day' (HH:MM), 'file_changed' (path).",
    {
        "event_type": "string (battery_low|storage_low|time_of_day|file_changed)",
        "condition": "string (threshold value or path)",
        "action_prompt": "string (what the agent should do when triggered)",
        "cooldown": "string? (min time between triggers: '30m', '2h'. default '1h')",
    },
)
def watch_create(event_type, condition, action_prompt, cooldown="1h"):
    valid_types = ("battery_low", "storage_low", "time_of_day", "file_changed")
    if event_type not in valid_types:
        return f"ERROR: event_type must be one of: {', '.join(valid_types)}"
    if not action_prompt or not action_prompt.strip():
        return "ERROR: action_prompt is required"

    # Parse cooldown
    import re
    m = re.match(r"(\d+)\s*(m|h|d)", str(cooldown).lower())
    cooldown_min = 60  # default
    if m:
        val, unit = int(m.group(1)), m.group(2)
        cooldown_min = val * {"m": 1, "h": 60, "d": 1440}[unit]
    cooldown_min = max(5, cooldown_min)  # minimum 5 min

    # Build condition object
    condition_obj = {"value": condition}

    watcher_id = db.add_watcher(event_type, condition_obj, action_prompt.strip(), cooldown_min)
    return (
        f"Watcher #{watcher_id} created: on '{event_type}' (condition: {condition}) "
        f"→ '{action_prompt[:60]}' (cooldown: {cooldown_min}m)"
    )


@registry.register(
    "watch_list",
    "List all active event watchers.",
    {},
)
def watch_list():
    watchers = db.list_watchers(include_inactive=False)
    if not watchers:
        return "No active watchers."
    lines = []
    for w in watchers:
        cond = w["condition"].get("value", "?")
        lines.append(
            f"#{w['id']}: [{w['event_type']}] condition={cond} → {w['action_prompt'][:50]} "
            f"(cooldown: {w['cooldown_minutes']}m)"
        )
    return "\n".join(lines)


@registry.register(
    "watch_remove",
    "Deactivate a watcher by ID.",
    {"watcher_id": "integer"},
)
def watch_remove(watcher_id):
    try:
        watcher_id = int(watcher_id)
    except (TypeError, ValueError):
        return "ERROR: watcher_id must be an integer"
    db.update_watcher(watcher_id, status="inactive")
    return f"Watcher #{watcher_id} deactivated."
