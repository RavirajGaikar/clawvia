"""Prompt templates for the ClawVia agent planner.

Optimised for speed:
  * Tool metadata is trimmed — only tools relevant to the task category are
    included (halves prompt size for most queries).
  * Skills index uses progressive disclosure (names + triggers only).
  * Step history uses a sliding window — last N steps verbatim, older steps
    summarised to one line each.
"""

import json
import re
from pathlib import Path

import config
from utils.logger import get_logger

log = get_logger("llm.prompts")

_soul_cache = None

# ── Tool categories ──────────────────────────────────────────────────────────
# Maps a category tag → set of tool names. The planner only sees tools whose
# category matches the inferred task type, plus ALWAYS-ON tools.

_ALWAYS_ON_TOOLS = {
    "finish", "memory_save", "memory_search", "recall",
}

_TOOL_CATEGORIES = {
    "web": {"web_search", "http_request", "mcp_fetch_fetch"},
    "file": {"list_files", "read_file", "write_file", "delete_file"},
    "code": {"code_execute", "code_explain"},
    "device": {
        "device_battery", "device_storage", "device_network", "device_info",
        "sms_inbox", "call_log", "contacts_query", "clipboard_get", "clipboard_set",
        "notification_send", "notification_remove", "dialog_input", "dialog_confirm",
        "volume_get", "volume_set", "brightness_set", "telephony_info",
    },
    "intent": {
        "open_app", "launch_activity", "share_text", "open_url", "send_sms",
        "make_call", "take_photo", "set_alarm", "set_timer",
        "calendar_event", "media_play",
    },
    "system": {"run_command"},
    "session": {"session_list", "session_create", "session_switch",
                "session_clear", "session_rename"},
    "memory": {"memory_save", "memory_search", "recall", "user_profile",
               "audit_log", "performance_stats", "memory_note"},
    "schedule": {"schedule_task", "schedule_list", "schedule_cancel",
                 "schedule_pause", "schedule_resume", "schedule_edit",
                 "schedule_details", "schedule_run_now", "schedule_cleanup"},
    "todo": {"todo_add", "todo_list", "todo_update", "todo_clear"},
    "skill": {"load_skill", "skill_create", "skill_edit"},
    "subagent": {"task"},
    "media": {"image_describe"},
}

# Keywords that hint at which categories to include
_CATEGORY_HINTS = {
    "web": re.compile(
        r"\b(search|news|weather|stock|price|latest|trending|web|site|url|"
        r"http|fetch|online|internet|google|browse|article|wiki)\b", re.I,
    ),
    "file": re.compile(
        r"\b(file|folder|dir|path|read|write|save|delete|create|move|copy|"
        r"download|list)\b", re.I,
    ),
    "code": re.compile(
        r"\b(code|python|script|execute|run|compute|calculate|program|"
        r"math|sort|parse|regex|csv|json|convert)\b", re.I,
    ),
    "device": re.compile(
        r"\b(battery|storage|network|wifi|sms|call|contact|clipboard|"
        r"notification|volume|brightness|phone|device|sim|telephony)\b", re.I,
    ),
    "intent": re.compile(
        r"\b(open|launch|share|send|sms|call|photo|alarm|timer|calendar|"
        r"play|media|app|camera)\b", re.I,
    ),
    "system": re.compile(
        r"\b(command|shell|terminal|apt|pkg|install|pip|process|kill|"
        r"ps|top|df|ls|cat|grep)\b", re.I,
    ),
    "schedule": re.compile(
        r"\b(schedule|remind|alarm|every|daily|weekly|cron|timer|recurring)\b", re.I,
    ),
    "todo": re.compile(
        r"\b(todo|task|checklist|plan|step|item)\b", re.I,
    ),
    "skill": re.compile(
        r"\b(skill|recipe|template|workflow)\b", re.I,
    ),
    "subagent": re.compile(
        r"\b(delegate|subagent|sub-task|parallel|research)\b", re.I,
    ),
    "media": re.compile(
        r"\b(image|photo|picture|screenshot|describe|vision|see|look)\b", re.I,
    ),
}


def _infer_tool_categories(task):
    """Return set of category names relevant to the task text."""
    cats = set()
    for cat, pattern in _CATEGORY_HINTS.items():
        if pattern.search(task):
            cats.add(cat)
    # Always include subagent for complex-looking tasks
    if len(cats) >= 2:
        cats.add("subagent")
    return cats


def _filter_tools(tools_metadata, task):
    """Return only tools relevant to the task + always-on tools."""
    cats = _infer_tool_categories(task)
    if not cats:
        # Can't infer — return all tools (safe fallback)
        return tools_metadata

    allowed = set(_ALWAYS_ON_TOOLS)
    for cat in cats:
        allowed |= _TOOL_CATEGORIES.get(cat, set())
    # Always include finish
    allowed.add("finish")

    filtered = [t for t in tools_metadata if t["name"] in allowed]

    # Safety: if filtering is too aggressive (< 3 tools), return all
    if len(filtered) < 3:
        return tools_metadata

    log.debug("Tool filter: %d/%d tools for categories %s",
              len(filtered), len(tools_metadata), cats)
    return filtered


def _load_soul():
    global _soul_cache
    if _soul_cache is not None:
        return _soul_cache
    try:
        path = Path(config.SOUL_PATH)
        if path.exists():
            _soul_cache = path.read_text(encoding="utf-8").strip()
            log.info("Loaded soul from %s (%d chars)", path, len(_soul_cache))
        else:
            _soul_cache = ""
            log.warning("Soul file not found: %s", path)
    except Exception as exc:
        log.warning("Failed to load soul: %s", exc)
        _soul_cache = ""
    return _soul_cache


def format_tools_for_prompt(tools_metadata):
    """Format tool metadata into a compact string — name: description (args)."""
    lines = []
    for tool in tools_metadata:
        args = tool.get("args", {})
        if args:
            args_brief = ", ".join(f"{k}: {v}" for k, v in args.items())
            lines.append(f'- {tool["name"]}({args_brief}): {tool["description"]}')
        else:
            lines.append(f'- {tool["name"]}(): {tool["description"]}')
    return "\n".join(lines)


def build_system_prompt(tools_metadata, role=None, think_level=None, task=None):
    """Build the full system prompt including persona, skills, and tool descriptions.

    If `task` is provided, only tools relevant to that task are included.
    If `role` is given, that role's prompt fragment is appended.
    If `think_level` is 'quick' or 'think', a nudge tunes depth.
    """
    soul = _load_soul()

    # Filter tools based on task if provided
    if task:
        tools_metadata = _filter_tools(tools_metadata, task)

    tools_text = format_tools_for_prompt(tools_metadata)

    parts = []

    if soul:
        parts.append(soul)
        parts.append("")

    # Inject skills if available (compact index only)
    try:
        from skills.loader import build_skills_prompt
        skills_prompt = build_skills_prompt()
        if skills_prompt:
            parts.append(skills_prompt)
            parts.append("")
    except Exception as exc:
        log.debug("Skills not loaded: %s", exc)

    parts.append("# Tools")
    parts.append(tools_text)
    parts.append("")
    parts.append(
        "# Response Format\n"
        "Respond with ONLY a valid JSON object. No extra text.\n"
        'To call a tool: {"tool": "name", "args": {<arguments>}, "thought": "brief reasoning"}\n'
        'To finish: {"tool": "finish", "args": {"output": "your answer"}, "thought": "brief reasoning"}\n\n'
        "Rules:\n"
        "- Single JSON object per response\n"
        "- Use exact tool/arg names shown above\n"
        "- On error, retry with different args or finish with explanation\n"
        "- When done, MUST use finish tool\n"
        "- For math/parsing/data tasks, prefer code_execute over reasoning in prose\n"
        "- For web research: use search snippets directly when page fetches return junk HTML/CSS\n"
        "- Stop fetching after 3 failed page loads and answer from what you have"
    )

    # Reasoning depth nudge
    if think_level == "quick":
        parts.append(
            "\n# Quick Mode\n"
            "Be decisive. Finish in 1-3 tool calls. If obvious, call finish directly."
        )
    elif think_level == "think":
        parts.append(
            "\n# Deep-Think Mode\n"
            "Decompose carefully. Verify intermediate results. Use code_execute for computation."
        )

    # Role override (planner/reviewer/qa/interrogator)
    if role:
        try:
            from skills.loader import load_role
            role_text = load_role(role)
            if role_text:
                parts.append(f"\n# Role: {role}\n{role_text}")
        except Exception:
            pass

    return "\n".join(parts)


# ── Sliding-window step history ──────────────────────────────────────────────

_RECENT_STEPS_WINDOW = 4  # Include full detail for last N steps
_SUMMARY_LINE_MAX = 120   # Max chars per summarised old step


def _summarize_old_steps(steps):
    """Summarize older steps into one line each."""
    lines = []
    for s in steps:
        action = s.get("action", "?")
        obs = s.get("observation", "")
        # Extract just the key result (first non-empty line, truncated)
        first_line = ""
        for line in obs.split("\n"):
            line = line.strip()
            if line and not line.startswith(("/*", "//", "@", ".", "{", "<")):
                first_line = line
                break
        if not first_line:
            first_line = obs[:80]
        if len(first_line) > _SUMMARY_LINE_MAX:
            first_line = first_line[:_SUMMARY_LINE_MAX] + "..."
        is_error = obs.startswith("ERROR:")
        prefix = "❌ " if is_error else "✓ "
        lines.append(f"  Step {s['step']}: {prefix}{action} → {first_line}")
    return lines


def build_planner_messages(task, context=None, steps=None, tools_metadata=None,
                           role=None, think_level=None):
    """Build the full message list for the planner LLM call.

    Uses a sliding window: last N steps with full observations, older steps
    compressed to one-line summaries. This keeps prompt size bounded.
    """
    if tools_metadata is None:
        tools_metadata = []

    messages = [{
        "role": "system",
        "content": build_system_prompt(
            tools_metadata, role=role, think_level=think_level, task=task,
        ),
    }]

    # Build the user message with context and step history
    user_parts = []

    if context:
        user_parts.append(f"# Context\n{context}")

    user_parts.append(f"# Task\n{task}")

    if steps:
        total = len(steps)
        if total > _RECENT_STEPS_WINDOW:
            old_steps = steps[:-_RECENT_STEPS_WINDOW]
            recent_steps = steps[-_RECENT_STEPS_WINDOW:]

            # Old steps → one-line summaries
            summary_lines = _summarize_old_steps(old_steps)
            user_parts.append(
                f"# Earlier Steps (summary, {len(old_steps)} steps)\n"
                + "\n".join(summary_lines)
            )

            # Recent steps → full detail
            user_parts.append(f"# Recent Steps (last {len(recent_steps)})")
            for s in recent_steps:
                obs = s['observation']
                if len(obs) > 1500:
                    obs = obs[:1200] + "\n...(truncated)..." + obs[-200:]
                user_parts.append(
                    f"Step {s['step']}: Called {s['action']}\n"
                    f"Observation: {obs}"
                )
        else:
            # Few enough steps — include all with full detail
            user_parts.append("# Previous Steps")
            for s in steps:
                obs = s['observation']
                if len(obs) > 1500:
                    obs = obs[:1200] + "\n...(truncated)..." + obs[-200:]
                user_parts.append(
                    f"Step {s['step']}: Called {s['action']}\n"
                    f"Observation: {obs}"
                )

        user_parts.append(
            "\nBased on the above, decide the next step or finish."
        )

    messages.append({"role": "user", "content": "\n\n".join(user_parts)})

    return messages
