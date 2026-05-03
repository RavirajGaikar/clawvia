"""Prompt templates for the ClawVia agent planner."""

import json
from pathlib import Path

import config
from utils.logger import get_logger

log = get_logger("llm.prompts")

_soul_cache = None


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
    """Format tool metadata list into a string for the system prompt."""
    lines = []
    for tool in tools_metadata:
        args_str = json.dumps(tool.get("args", {}))
        lines.append(f'- {tool["name"]}: {tool["description"]}  args={args_str}')
    return "\n".join(lines)


def build_system_prompt(tools_metadata, role=None, think_level=None):
    """Build the full system prompt including persona, skills, and tool descriptions.

    If `role` is given (e.g. 'planner', 'reviewer', 'qa', 'interrogator'),
    that role's prompt fragment is appended to override default behaviour
    for the current task.
    If `think_level` is 'quick' or 'think', a small nudge block tunes the
    agent's verbosity / depth for that run.
    """
    soul = _load_soul()
    tools_text = format_tools_for_prompt(tools_metadata)

    parts = []

    if soul:
        parts.append(soul)
        parts.append("")

    # Inject skills if available
    try:
        from skills.loader import build_skills_prompt
        skills_prompt = build_skills_prompt()
        if skills_prompt:
            parts.append(skills_prompt)
            parts.append("")
    except Exception as exc:
        log.debug("Skills not loaded: %s", exc)

    parts.append("# Available Tools")
    parts.append(tools_text)
    parts.append("")
    parts.append("# Response Format")
    parts.append(
        "You MUST respond with ONLY a valid JSON object. No extra text.\n"
        "To call a tool:\n"
        '{"tool": "tool_name", "args": {<arguments>}, "thought": "brief reasoning"}\n\n'
        "To give a final answer (when done or no tool needed):\n"
        '{"tool": "finish", "args": {"output": "your answer"}, "thought": "brief reasoning"}\n\n'
        "IMPORTANT RULES:\n"
        "- Always respond with a single JSON object\n"
        "- Use the exact tool names and argument names shown above\n"
        "- If a tool returns an error, you may retry with different args or finish with an explanation\n"
        "- When the task is complete, you MUST use the finish tool\n"
        "- Keep thoughts concise\n"
        "- Use memory_save to remember important facts, user preferences, or key findings\n"
        "- Use memory_search to recall previously saved information"
    )

    # Chain-of-Code nudge — small, always-on, cheap. Steers the agent toward
    # `code_execute` for anything quantitative (math, parsing, transforming
    # data, regexes, date arithmetic) instead of guessing in natural language.
    parts.append("")
    parts.append("# Chain-of-Code")
    parts.append(
        "For ANY task that involves arithmetic, counting, sorting, parsing, "
        "date/time math, regex, JSON/CSV manipulation, or string transforms: "
        "prefer the `code_execute` tool over reasoning in prose. Writing 3 lines "
        "of Python and reading the output is more reliable than computing in your "
        "head. For multi-part research tasks, delegate self-contained "
        "sub-questions to the `task` subagent so its work doesn't pollute your "
        "main step history.\n\n"
        "When to use the `task` subagent:\n"
        "- Research tasks that need multiple web searches + page fetches\n"
        "- Any sub-problem that would take 3+ steps on its own\n"
        "- Parallel-safe work that doesn't depend on your main context\n"
        "When NOT to delegate: single tool calls, tasks that need your conversation history."
    )

    # Reasoning depth nudge — paired with the temperature/step caps set by
    # agent.run() when the user picks /quick or /think.
    if think_level == "quick":
        parts.append("")
        parts.append("# Quick Mode")
        parts.append(
            "Be decisive and terse. Aim to finish in 1–3 tool calls. Skip the "
            "warm-up; if the answer is obvious, call `finish` directly. Do not "
            "explore alternatives — pick the most obvious tool and ship."
        )
    elif think_level == "think":
        parts.append("")
        parts.append("# Deep-Think Mode")
        parts.append(
            "Take your time. Decompose the task in your `thought` fields, verify "
            "intermediate results before moving on, and prefer `code_execute` for "
            "any computation. Use the `task` subagent to spin off self-contained "
            "investigations. Quality over speed."
        )

    # User context — inject persistent user profile into system prompt
    try:
        from memory.db import get_user_profile
        profile = get_user_profile()
        if profile:
            strong = [e for e in profile if e["confidence"] >= 0.5]
            if strong:
                parts.append("")
                parts.append("# User Context")
                profile_lines = [f"- {e['key']}: {e['value']}" for e in strong[:6]]
                parts.append(
                    "Adapt your responses based on what you know about the user:\n"
                    + "\n".join(profile_lines)
                )
    except Exception:
        pass

    return "\n".join(parts)


def build_planner_messages(task, context=None, steps=None, tools_metadata=None, role=None, think_level=None):
    """Build the full message list for the planner LLM call.

    Args:
        task: The user's task/question.
        context: Optional context string (memory/history).
        steps: List of previous step dicts.
        tools_metadata: List of tool metadata dicts.
        role: Optional role name to override default behaviour.
        think_level: Optional 'quick' | 'think' depth nudge.

    Returns:
        List of message dicts for the LLM.
    """
    if tools_metadata is None:
        tools_metadata = []

    messages = [{
        "role": "system",
        "content": build_system_prompt(tools_metadata, role=role, think_level=think_level),
    }]

    # Build the user message with context and step history
    user_parts = []

    if context:
        user_parts.append(f"# Context\n{context}")

    user_parts.append(f"# Task\n{task}")

    if steps:
        user_parts.append("# Previous Steps")
        for s in steps:
            user_parts.append(
                f"Step {s['step']}: Called {s['action']}\n"
                f"Observation: {s['observation']}"
            )
        user_parts.append(
            "\nBased on the above, decide the next step or finish."
        )

    messages.append({"role": "user", "content": "\n\n".join(user_parts)})

    return messages
