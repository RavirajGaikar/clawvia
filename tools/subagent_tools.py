"""Subagent tool — `task` lets the agent spin off a focused sub-investigation.

Why:
  * Keeps noisy multi-step research (web search → fetch → summarise → ...) out
    of the parent's step history.
  * Returns just the *final answer*, so the parent sees a clean conclusion
    instead of N intermediate observations.
  * Caps depth (SUBAGENT_MAX_DEPTH) so a misbehaving prompt can't recurse
    forever.

Recursion guard:
  * Each call to the subagent increments a thread-local depth counter on the
    parent agent module (`agent._push_depth`).
  * If depth would exceed `config.SUBAGENT_MAX_DEPTH`, we refuse and tell the
    parent to do the work inline.
"""

import config
from tools.registry import registry
from utils.logger import get_logger

log = get_logger("tools.subagent")


@registry.register(
    "task",
    "Run a focused sub-task in a fresh nested agent. Returns only the final "
    "answer, not the intermediate steps. Use for self-contained chunks (e.g. "
    "'find the population of Tokyo and the year of the data') so they don't "
    "clutter your main step history. think_level: 'quick' (default) or 'think'.",
    {"prompt": "string", "think_level": "string?"},
)
def task(prompt=None, think_level="quick"):
    if not prompt or not str(prompt).strip():
        return "ERROR: 'prompt' is required"

    # Late import: agent imports this package, so importing it at module load
    # time would create a circular import. Late import is fine because tools
    # are only ever called from inside agent.run().
    import agent as _agent

    depth = _agent._current_depth()
    if depth >= config.SUBAGENT_MAX_DEPTH:
        return (
            f"ERROR: subagent depth {depth} would exceed "
            f"SUBAGENT_MAX_DEPTH={config.SUBAGENT_MAX_DEPTH}. Do this work "
            "inline in the parent task instead of calling `task` again."
        )

    level = (think_level or "quick").lower()
    if level not in {"quick", "think"}:
        level = "quick"

    # Cap the subagent's own step budget independently of the user-facing
    # config — we'd rather the sub return a partial answer than burn 25 steps.
    capped_steps = min(
        config.SUBAGENT_MAX_STEPS,
        config.THINK_LEVELS.get(level, {}).get("max_steps", config.MAX_AGENT_STEPS),
    )

    log.info("Subagent (depth=%d → %d, think=%s, max_steps=%d): %s",
             depth, depth + 1, level, capped_steps, str(prompt)[:80])

    # Temporarily shrink the active think_level's step budget for this nested
    # call. We restore it in the finally block.
    preset = config.THINK_LEVELS.setdefault(level, {})
    saved_steps = preset.get("max_steps")
    preset["max_steps"] = capped_steps

    _agent._push_depth()
    try:
        # Subagents run in a fresh ephemeral session so their step history,
        # checkpoints, and reflections don't pollute the parent's session.
        from memory import db
        sub_session = db.create_session(title=f"[subagent] {str(prompt)[:50]}")
        result = _agent.run(
            task=str(prompt),
            session_id=sub_session["id"],
            think_level=level,
        )
        return result
    except Exception as exc:
        log.warning("Subagent crashed: %s", exc)
        return f"ERROR: subagent crashed: {exc}"
    finally:
        _agent._pop_depth()
        if saved_steps is not None:
            preset["max_steps"] = saved_steps
