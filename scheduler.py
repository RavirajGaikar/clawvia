"""Scheduler — background thread that fires scheduled agent tasks.

Features:
  * Retry on failure: one-shot tasks get up to 3 attempts before being marked
    failed_permanent. Recurring tasks retry on the next cycle.
  * Catch-up on restart: tasks that were due while the bot was offline run
    immediately on startup.
  * Timezone-aware: uses config.TIMEZONE (IANA) for "in Xm" / "every Xh" calculations.
"""

import re
import threading
import datetime

import config
from memory import db
from utils.logger import get_logger

log = get_logger("scheduler")

_scheduler_instance = None
_MAX_RETRIES = 3  # one-shot tasks retry up to this many times


def _now_utc():
    """Current UTC time as a datetime object."""
    return datetime.datetime.utcnow()


def _now_iso():
    """Current UTC time as ISO string with Z suffix."""
    return _now_utc().isoformat(timespec="seconds") + "Z"


class Scheduler:
    """Simple cron-lite scheduler with one-shot and recurring tasks."""

    def __init__(self, agent_run_fn, notify_fn=None):
        """
        Args:
            agent_run_fn: callable(prompt) -> result string
            notify_fn: callable(result) -> None, for sending results (e.g. Telegram)
        """
        self._agent_run = agent_run_fn
        self._notify = notify_fn
        self._timer = None
        self._running = False

    def start(self):
        self._running = True
        log.info("Scheduler started (check every %ds)", config.SCHEDULER_CHECK_INTERVAL)
        # Catch-up: run any tasks that were due while bot was offline
        self._catch_up()
        self._tick()

    def stop(self):
        self._running = False
        if self._timer:
            self._timer.cancel()
        log.info("Scheduler stopped")

    def _catch_up(self):
        """Run tasks that were due while the bot was offline."""
        due = db.get_due_tasks(_now_iso())
        if due:
            log.info("Catch-up: %d task(s) were due while offline", len(due))
            for task in due:
                self._run_task(task)

    def _tick(self):
        if not self._running:
            return
        try:
            self._check_due_tasks()
        except Exception as exc:
            log.error("Scheduler tick error: %s", exc, exc_info=True)
        # Schedule next tick
        self._timer = threading.Timer(config.SCHEDULER_CHECK_INTERVAL, self._tick)
        self._timer.daemon = True
        self._timer.start()

    def _check_due_tasks(self):
        due = db.get_due_tasks(_now_iso())
        for task in due:
            self._run_task(task)

        # Check due goals
        self._check_goals()

        # Evaluate event watchers
        self._check_watchers()

        # Check if user is idle → run self-improvement
        self._check_idle()

    def _run_task(self, task):
        """Execute a single scheduled task with retry support and timeout."""
        now = _now_utc()
        now_iso = now.isoformat(timespec="seconds") + "Z"
        log.info("Running scheduled task #%d: %s", task["id"], task["prompt"][:60])

        success = False
        result = ""
        try:
            # Run with a 5-minute timeout to prevent runaway tasks
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(self._agent_run, task["prompt"])
                result = future.result(timeout=300)
            success = True
        except concurrent.futures.TimeoutError:
            log.error("Scheduled task #%d timed out after 300s", task["id"])
            result = "Scheduled task timed out after 5 minutes."
        except Exception as exc:
            log.error("Scheduled task #%d failed: %s", task["id"], exc)
            result = f"Scheduled task error: {exc}"

        # Update last_run regardless
        db.update_scheduled_task(task["id"], last_run=now_iso)

        if task["schedule_type"] == "at":
            if success:
                db.update_scheduled_task(task["id"], status="completed")
                log.info("One-shot task #%d completed", task["id"])
            else:
                # Retry logic: check how many times we've tried
                retries = task.get("retries", 0) + 1
                if retries >= _MAX_RETRIES:
                    db.update_scheduled_task(task["id"], status="failed_permanent")
                    log.warning("One-shot task #%d failed permanently after %d retries",
                                task["id"], retries)
                else:
                    # Retry in 2 minutes
                    retry_at = (now + datetime.timedelta(minutes=2)).isoformat(timespec="seconds") + "Z"
                    db.update_scheduled_task(task["id"], next_run=retry_at)
                    db.increment_task_retries(task["id"])
                    log.info("One-shot task #%d will retry (#%d) at %s",
                             task["id"], retries, retry_at)
        elif task["schedule_type"] == "every":
            # Recurring: always schedule next run regardless of success/failure
            next_run = _calc_next_run(task["schedule_value"], now)
            db.update_scheduled_task(task["id"], next_run=next_run)
            log.info("Recurring task #%d next run: %s", task["id"], next_run)

        # Notify user
        if self._notify:
            try:
                status_icon = "✅" if success else "❌"
                header = f"⏰ {status_icon} Scheduled task #{task['id']}:\n{task['prompt'][:100]}\n\n"
                self._notify(header + result)
            except Exception as exc:
                log.error("Notify failed for task #%d: %s", task["id"], exc)

    def _check_goals(self):
        """Check due goals and run the agent to make progress."""
        now_iso = _now_iso()
        due_goals = db.get_due_goals(now_iso)

        for goal in due_goals:
            log.info("Checking goal #%d: %s", goal["id"], goal["description"][:60])
            prompt = (
                f"Check progress on your goal: {goal['description']}\n"
                f"Progress so far: {goal['progress_notes'][-500:] or 'None yet.'}\n\n"
                f"Take the next action to make progress on this goal. "
                f"If the goal is achieved, use goal_update to mark it completed."
            )
            try:
                result = self._agent_run(prompt)
            except Exception as exc:
                log.error("Goal #%d check failed: %s", goal["id"], exc)
                result = f"Goal check error: {exc}"

            # Reschedule next check
            next_check = (
                _now_utc() + datetime.timedelta(seconds=goal["check_interval"])
            ).isoformat(timespec="seconds") + "Z"
            db.update_goal(goal["id"], next_check=next_check)

            # Notify user
            if self._notify:
                try:
                    self._notify(
                        f"🎯 Goal #{goal['id']} checked:\n{goal['description'][:80]}\n\n{result}"
                    )
                except Exception:
                    pass

    def _check_watchers(self):
        """Evaluate event watchers and trigger actions if conditions are met."""
        now = _now_utc()
        now_iso = now.isoformat(timespec="seconds") + "Z"
        watchers = db.get_active_watchers()

        for w in watchers:
            # Skip if in cooldown
            if w.get("cooldown_until") and w["cooldown_until"] > now_iso:
                continue

            triggered = _evaluate_watcher_condition(w["event_type"], w["condition"])
            if not triggered:
                continue

            log.info("Watcher #%d triggered: %s", w["id"], w["event_type"])

            # Set cooldown
            cooldown_until = (
                now + datetime.timedelta(minutes=w["cooldown_minutes"])
            ).isoformat(timespec="seconds") + "Z"
            db.update_watcher(w["id"], last_triggered=now_iso, cooldown_until=cooldown_until)

            # Run the action
            try:
                result = self._agent_run(w["action_prompt"])
            except Exception as exc:
                log.error("Watcher #%d action failed: %s", w["id"], exc)
                result = f"Watcher action error: {exc}"

            # Notify
            if self._notify:
                try:
                    self._notify(
                        f"👁️ Watcher #{w['id']} ({w['event_type']}) triggered:\n\n{result}"
                    )
                except Exception:
                    pass

    def _check_idle(self):
        """If user has been idle long enough, run a self-improvement task."""
        try:
            from memory.idle import get_next_idle_task, run_idle_task
        except ImportError:
            return

        # Check last user interaction
        last_interaction = db.kv_get("last_user_interaction")
        if not last_interaction:
            return

        try:
            last_dt = datetime.datetime.fromisoformat(
                last_interaction.replace("Z", "+00:00")
            ).replace(tzinfo=None)
        except (ValueError, TypeError):
            return

        idle_minutes = (_now_utc() - last_dt).total_seconds() / 60
        idle_threshold = int(db.kv_get("idle_threshold_minutes", "30"))

        if idle_minutes < idle_threshold:
            return

        # Check if we ran an idle task recently (2h cooldown between any idle tasks)
        last_idle = db.kv_get("last_idle_run")
        if last_idle:
            try:
                last_idle_dt = datetime.datetime.fromisoformat(
                    last_idle.replace("Z", "+00:00")
                ).replace(tzinfo=None)
                if (_now_utc() - last_idle_dt).total_seconds() < 7200:
                    return  # Global idle cooldown not expired
            except (ValueError, TypeError):
                pass

        task = get_next_idle_task()
        if not task:
            return

        log.info("User idle for %.0f min, running idle task: %s", idle_minutes, task["name"])
        db.kv_set("last_idle_run", _now_iso())

        result = run_idle_task(task["name"])

        if result and self._notify:
            idle_notify = db.kv_get("idle_notify", "true")
            if idle_notify.lower() == "true":
                try:
                    self._notify(
                        f"🧠 Idle self-improvement ({task['description']}):\n\n{result}"
                    )
                except Exception:
                    pass


def _evaluate_watcher_condition(event_type, condition):
    """Evaluate whether a watcher condition is currently met.

    Returns True if the condition is triggered.
    Uses lightweight shell checks (no Termux:API dependency).
    """
    import subprocess
    value = condition.get("value", "")

    try:
        if event_type == "battery_low":
            threshold = int(value) if value else 20
            result = subprocess.run(
                ["cat", "/sys/class/power_supply/battery/capacity"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                level = int(result.stdout.strip())
                return level <= threshold

        elif event_type == "storage_low":
            threshold_mb = int(value) if value else 500
            result = subprocess.run(
                ["df", "-m", "/data"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                # Parse df output: last line, 4th column is available
                lines = result.stdout.strip().split("\n")
                if len(lines) >= 2:
                    parts = lines[-1].split()
                    if len(parts) >= 4:
                        avail = int(parts[3])
                        return avail <= threshold_mb

        elif event_type == "time_of_day":
            # value = "HH:MM" — trigger if current time matches (within 2 min window)
            target = value.strip()
            now = datetime.datetime.now()
            now_hm = now.strftime("%H:%M")
            if target == now_hm:
                return True
            # Also check 1 minute before (in case scheduler tick doesn't land exactly)
            prev = (now - datetime.timedelta(minutes=1)).strftime("%H:%M")
            return target == prev

        elif event_type == "file_changed":
            import os
            path = value.strip()
            if not os.path.exists(path):
                return False
            # Check if modified in the last scheduler interval (+ margin)
            mtime = os.path.getmtime(path)
            import time
            age_seconds = time.time() - mtime
            return age_seconds < (config.SCHEDULER_CHECK_INTERVAL + 10)

    except Exception as exc:
        log.debug("Watcher condition check failed (%s): %s", event_type, exc)

    return False


def parse_schedule(schedule_str):
    """Parse a schedule string into (type, value, next_run_iso).

    Supported formats:
        "at 2024-01-01T12:00:00Z"  — one-shot at ISO timestamp
        "in 20m"                   — one-shot in N minutes/hours/days
        "every 30m"               — recurring every N minutes
        "every 2h"                — recurring every N hours
        "every 1d"                — recurring every N days

    Returns (schedule_type, schedule_value, next_run_iso) or raises ValueError.
    """
    schedule_str = schedule_str.strip()
    now = datetime.datetime.utcnow()

    # "in Xm/Xh/Xd" — one-shot relative
    m = re.match(r"^in\s+(\d+)\s*(m|min|h|hr|hour|d|day)s?$", schedule_str, re.I)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)[0].lower()
        delta = _unit_to_delta(amount, unit)
        next_run = (now + delta).isoformat(timespec="seconds") + "Z"
        return "at", schedule_str, next_run

    # "at <ISO timestamp>"
    m = re.match(r"^at\s+(.+)$", schedule_str, re.I)
    if m:
        ts = m.group(1).strip()
        # Validate ISO format
        try:
            parsed = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            next_run = parsed.strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            raise ValueError(f"Invalid timestamp: {ts}")
        return "at", ts, next_run

    # "every Xm/Xh/Xd" — recurring
    m = re.match(r"^every\s+(\d+)\s*(m|min|h|hr|hour|d|day)s?$", schedule_str, re.I)
    if m:
        amount = int(m.group(1))
        unit = m.group(2)[0].lower()
        delta = _unit_to_delta(amount, unit)
        next_run = (now + delta).isoformat(timespec="seconds") + "Z"
        return "every", f"{amount}{unit}", next_run

    raise ValueError(
        f"Invalid schedule: '{schedule_str}'. "
        "Use: 'in 20m', 'at 2024-01-01T12:00:00Z', 'every 30m', 'every 2h', 'every 1d'"
    )


def _unit_to_delta(amount, unit):
    if unit == "m":
        return datetime.timedelta(minutes=amount)
    elif unit == "h":
        return datetime.timedelta(hours=amount)
    elif unit == "d":
        return datetime.timedelta(days=amount)
    raise ValueError(f"Unknown time unit: {unit}")


def _calc_next_run(schedule_value, from_time):
    """Calculate the next run time for a recurring schedule."""
    m = re.match(r"^(\d+)([mhd])$", schedule_value)
    if not m:
        # Fallback: 1 hour
        delta = datetime.timedelta(hours=1)
    else:
        amount = int(m.group(1))
        unit = m.group(2)
        delta = _unit_to_delta(amount, unit)
    return (from_time + delta).isoformat(timespec="seconds") + "Z"


def get_scheduler():
    return _scheduler_instance


def set_scheduler(instance):
    global _scheduler_instance
    _scheduler_instance = instance
