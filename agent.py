"""Agent controller — the core reasoning loop.

Implements: plan → execute → observe → repeat (up to MAX_AGENT_STEPS).
Terminates when the LLM returns tool == "finish".

Hardening:
- Reflexion: after REFLEXION_AFTER_ERRORS consecutive errors on the same tool,
  spend one extra LLM call to generate a reflection that's stored and injected
  into the next attempt (deduped by error hash so we don't burn tokens).
- Stuck counter: bail with an "ask user" answer after STUCK_ERROR_LIMIT
  consecutive ERROR observations.
- Checkpoints: snapshot the steps list to SQLite after each iteration so the
  user can inspect / restore later.
- Fast-path: simple greetings/thanks bypass the tool loop entirely.
- Meta-learning: tracks performance metrics, detects user feedback, extracts
  user profile, auto-creates skills, records idle timestamps.
"""

import hashlib
import re
import threading
import time

import config
from llm.client import chat, LLMError
from llm.json_parser import extract_json
from llm.prompts import build_planner_messages

# Global counter for batched post-task work (pattern extraction etc.)
_task_counter = 0
_task_counter_lock = threading.Lock()
_POST_TASK_INTERVAL = 10  # Run post-task LLM work every N tasks
from memory import db
from memory.context import build_context
from memory.compaction import auto_compact_if_needed
from tools.registry import registry
from tools import session_tools
from security.guardrails import check_input, sanitize_tool_output
from utils.logger import get_logger

log = get_logger("agent")

# ── Fast-path patterns (bypass full agent loop) ───────────────────────────

_FAST_PATH_PATTERNS = re.compile(
    r"^("
    r"h(i|ey|ello|owdy|ola|iya)"
    r"|yo\b"
    r"|sup\b"
    r"|good\s*(morning|afternoon|evening|night)"
    r"|thanks?(\s+you)?(\s+(so\s+)?much)?"
    r"|ty\b"
    r"|ok(ay)?\b"
    r"|got\s*it"
    r"|cool\b"
    r"|nice\b"
    r"|great\b"
    r"|awesome\b"
    r"|perfect\b"
    r"|bye\b"
    r"|gn\b"
    r"|good\s*night"
    r"|what'?s?\s*up"
    r"|how\s*are\s*you"
    r"|who\s*are\s*you"
    r"|what\s*are\s*you"
    r"|what'?s?\s*your\s*name"
    r")[\s?!.]*$",
    re.IGNORECASE,
)


def _is_fast_path(text):
    """Check if message is a simple greeting/ack that doesn't need tools."""
    return bool(_FAST_PATH_PATTERNS.match(text.strip()))


# Hardcoded greetings — zero LLM calls for the most common phrases
_GREETING_RESPONSES = {
    "hi": "Hey! How can I help you?",
    "hey": "Hey! What can I do for you?",
    "hello": "Hello! How can I help you today?",
    "howdy": "Howdy! What's on your mind?",
    "hola": "Hola! How can I help?",
    "hiya": "Hiya! What do you need?",
    "yo": "Yo! What's up?",
    "sup": "Hey! What's going on?",
    "good morning": "Good morning! How can I help?",
    "good afternoon": "Good afternoon! What can I do for you?",
    "good evening": "Good evening! How can I help?",
    "good night": "Good night! Need anything before I go?",
    "bye": "Bye! Talk to you later.",
    "gn": "Good night! Sleep well.",
    "thanks": "You're welcome! Anything else?",
    "thank you": "You're welcome! Let me know if you need anything else.",
    "ty": "No problem! Anything else?",
    "ok": "Got it! What's next?",
    "okay": "Alright! Anything else?",
    "cool": "Glad to help! Anything else?",
    "nice": "Thanks! Need anything else?",
    "great": "Glad to hear it! Need anything else?",
    "awesome": "Glad it worked! Anything else?",
    "perfect": "Great! Let me know if you need anything else.",
    "got it": "Cool! What's next?",
    "how are you": "I'm doing great, thanks for asking! How can I help?",
    "who are you": "I'm ClawVia, your Android AI assistant! Ask me anything.",
    "what are you": "I'm ClawVia — an AI assistant running on your device. I can search the web, manage files, run commands, and more!",
    "whats your name": "I'm ClawVia! Your personal AI assistant.",
    "what's your name": "I'm ClawVia! Your personal AI assistant.",
    "whats up": "Not much! What can I help you with?",
    "what's up": "Hey! What's going on?",
}


def _fast_response(task, session_id, context):
    """Generate a direct response without the tool loop.

    First tries hardcoded responses (zero LLM cost), then falls back to
    a cheap LLM call, and finally to a generic greeting.
    """
    # Try hardcoded first — instant, zero cost
    key = task.strip().lower().rstrip("!?.")
    if key in _GREETING_RESPONSES:
        return _GREETING_RESPONSES[key]

    # Fall back to LLM
    from llm.prompts import _load_soul
    soul = _load_soul()
    system = (
        f"{soul}\n\n"
        "Respond naturally and briefly. No tools needed. "
        "Keep it mobile-friendly (1-3 sentences max)."
    )
    messages = [
        {"role": "system", "content": system},
    ]
    if context:
        messages.append({"role": "user", "content": f"Context:\n{context}\n\nUser: {task}"})
    else:
        messages.append({"role": "user", "content": task})

    try:
        response = chat(messages, temperature=0.3, max_tokens=200)
        return response.strip()
    except LLMError as exc:
        log.warning("Fast-path LLM failed: %s", exc)
        # Hardcoded fallback so we NEVER fall through to the full agent loop
        return "Hey there! I'm ClawVia. How can I help you today?"

# Wire session tools to the database module
session_tools.set_db(db)

# Max consecutive JSON parse failures before bailing out
_MAX_PARSE_FAILURES = 3

# Per-session locks — prevent concurrent agent.run() calls on the same session
# from interleaving steps/messages (API, Telegram, scheduler may overlap).
_session_locks = {}           # session_id -> Lock
_session_lock_times = {}      # session_id -> last_used timestamp
_session_locks_guard = threading.Lock()
_SESSION_LOCK_MAX_AGE = 3600  # Clean locks unused for 1 hour


def _get_session_lock(session_id):
    """Get or create a lock for a specific session."""
    with _session_locks_guard:
        # Periodic cleanup: remove stale locks (every 50 calls)
        if len(_session_locks) > 50:
            now = time.monotonic()
            stale = [
                sid for sid, ts in _session_lock_times.items()
                if now - ts > _SESSION_LOCK_MAX_AGE and sid in _session_locks
                and not _session_locks[sid].locked()
            ]
            for sid in stale:
                del _session_locks[sid]
                del _session_lock_times[sid]
            if stale:
                log.debug("Cleaned %d stale session locks", len(stale))

        if session_id not in _session_locks:
            _session_locks[session_id] = threading.Lock()
        _session_lock_times[session_id] = time.monotonic()
        return _session_locks[session_id]


# Pending command approvals: session_id -> {tool, args, step, task}
_pending_approvals = {}
_approvals_lock = threading.Lock()

# Restore pending approvals from DB on module load
try:
    for _pa in db.get_all_pending_approvals():
        _pending_approvals[_pa["session_id"]] = {
            "tool": _pa["tool"],
            "args": _pa["args"],
            "task": _pa.get("task"),
        }
    if _pending_approvals:
        log.info("Restored %d pending approval(s) from DB", len(_pending_approvals))
except Exception as _exc:
    log.warning("Could not restore pending approvals: %s", _exc)

# Subagent recursion depth (per-thread). The `task` tool reads & increments
# this so nested agent loops can be bounded by SUBAGENT_MAX_DEPTH.
_subagent_local = threading.local()


def _current_depth():
    return getattr(_subagent_local, "depth", 0)


def _push_depth():
    _subagent_local.depth = _current_depth() + 1


def _pop_depth():
    d = _current_depth()
    _subagent_local.depth = max(0, d - 1)


def _error_hash(error_text):
    """Stable short hash of an error string for reflection dedup."""
    return hashlib.sha1(error_text.encode("utf-8", errors="replace")).hexdigest()[:12]


def _generate_reflection(task, tool_name, tool_args, error):
    """One cheap LLM call: 'what went wrong, what to try differently'."""
    prompt = (
        "You just attempted a tool call that failed. In 2-3 short sentences, "
        "diagnose the likely cause and suggest a concrete different approach "
        "for the next attempt. Be specific. No preamble.\n\n"
        f"Task: {task}\n"
        f"Tool: {tool_name}\n"
        f"Args: {tool_args}\n"
        f"Error: {error[:600]}"
    )
    try:
        text = chat(
            [{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=config.REFLEXION_MAX_TOKENS,
        )
        return text.strip()
    except LLMError as exc:
        log.warning("Reflection generation failed: %s", exc)
        return ""


# ── Feedback detection (regex-only, no LLM call) ─────────────────────────

_POSITIVE_FEEDBACK = re.compile(
    r"^(good|great|perfect|awesome|nice|thanks?|ty|cool|correct|right|yes|exactly|💯|👍|❤️)"
    r"[\s!.]*$",
    re.IGNORECASE,
)
_NEGATIVE_FEEDBACK = re.compile(
    r"^(bad|wrong|no|nope|incorrect|not\s+what|try\s+again|redo|fix|👎)"
    r"[\s!.]*$",
    re.IGNORECASE,
)


def _detect_feedback(text):
    """Detect if a message is user feedback on the previous task.

    Returns 'positive', 'negative', or None.
    """
    text = text.strip()
    if len(text) > 30:
        return None  # Too long to be a simple feedback signal
    if _POSITIVE_FEEDBACK.match(text):
        return "positive"
    if _NEGATIVE_FEEDBACK.match(text):
        return "negative"
    return None


# ── User profile extraction (cheap, runs every Nth task) ──────────────────

_PROFILE_EXTRACT_INTERVAL = 15  # Extract every N tasks


def _maybe_extract_profile(session_id, task):
    """Extract user preferences from the conversation, periodically."""
    try:
        count = int(db.kv_get("profile_extract_counter", "0"))
        count += 1
        db.kv_set("profile_extract_counter", str(count))

        if count % _PROFILE_EXTRACT_INTERVAL != 0:
            return  # Not time yet

        # Get recent messages for context
        messages = db.get_messages(session_id, limit=10)
        if len(messages) < 3:
            return

        convo = "\n".join(
            f"{m['role']}: {m['content'][:100]}" for m in messages[-6:]
        )

        raw = chat(
            [{"role": "user", "content": (
                "From this conversation, extract any user preferences, habits, "
                "or personal info revealed. Respond with a JSON object: "
                "{\"key\": \"value\"} or {} if nothing. Keys: name, timezone, "
                "language, technical_level, common_tasks, preferences, interests, "
                "communication_style. Only include what's clearly stated.\n\n"
                f"{convo}"
            )}],
            temperature=0.1,
            max_tokens=150,
        )

        if not raw:
            return

        data = extract_json(raw.strip())
        if data and isinstance(data, dict):
            for key, value in data.items():
                if isinstance(value, str) and value.strip():
                    db.upsert_user_profile(key, value.strip(),
                                           confidence=0.5, source="conversation")
                    log.info("Profile extracted: %s = %s", key, value.strip()[:50])
    except Exception as exc:
        log.debug("Profile extraction failed: %s", exc)


# ── Auto-skill creation (after successful multi-step tasks) ───────────────

_SKILL_MIN_STEPS = 5  # Only consider tasks with 5+ steps


def _maybe_create_skill(task, steps):
    """After a successful complex task, consider creating a skill."""
    if len(steps) < _SKILL_MIN_STEPS:
        return

    try:
        # Check if we've already created a skill recently
        last_skill = db.kv_get("last_skill_creation")
        if last_skill:
            import datetime
            last_dt = datetime.datetime.fromisoformat(
                last_skill.replace("Z", "+00:00")
            ).replace(tzinfo=None)
            if (datetime.datetime.utcnow() - last_dt).total_seconds() < 3600:
                return  # Created a skill less than 1h ago

        # Check if existing skills already cover this task
        from skills.loader import load_skills
        existing = load_skills()
        task_lower = task.lower()
        for s in existing:
            # Simple keyword overlap check
            skill_words = set(s["name"].split("-") + s.get("description", "").lower().split())
            task_words = set(w for w in task_lower.split() if len(w) >= 3)
            if len(skill_words & task_words) >= 2:
                return  # Existing skill likely covers this

        # Ask LLM if this should be a skill
        step_summary = "\n".join(
            f"  {s.get('action', '?')}" for s in steps[:8]
        )
        raw = chat(
            [{"role": "user", "content": (
                f"Task: {task[:200]}\nSteps taken:\n{step_summary}\n\n"
                "Should this be saved as a reusable skill? If yes, respond with "
                "JSON: {\"name\": \"slug-name\", \"description\": \"one line\", "
                "\"triggers\": \"comma,separated\", \"content\": \"step by step instructions\"}\n"
                "If no, respond with: {\"skip\": true}"
            )}],
            temperature=0.2,
            max_tokens=400,
        )

        if not raw:
            return

        data = extract_json(raw.strip())
        if not data or not isinstance(data, dict) or data.get("skip"):
            return

        name = data.get("name", "").strip()
        if not name:
            return

        from skills.loader import create_skill_file, reload_skills
        result = create_skill_file(
            name=name,
            description=data.get("description", ""),
            triggers=data.get("triggers", ""),
            content=data.get("content", ""),
        )
        if not result.startswith("ERROR"):
            reload_skills()
            db.kv_set("last_skill_creation",
                       datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z")
            db.audit_log_event("auto_skill_created", tool_name="skill_create",
                               args_summary=f"name={name}")
            log.info("Auto-created skill: %s", name)
    except Exception as exc:
        log.debug("Auto-skill creation failed: %s", exc)


def _summarize_observation(observation, tool_name):
    """Smart truncation for very long tool output — no LLM call needed."""
    # Keep first chunk (usually has key info/headers) and last chunk (results/errors)
    head = observation[:2000]
    tail = observation[-800:]
    omitted = len(observation) - 2800
    return (
        f"{head}\n\n"
        f"... ({omitted} chars omitted, showing last {len(tail)} chars) ...\n\n"
        f"{tail}"
    )


def get_pending_approval(session_id):
    """Get pending approval for a session, if any."""
    with _approvals_lock:
        return _pending_approvals.get(session_id)


def resolve_approval(session_id, approved):
    """Resolve a pending approval. Returns the command result or denial message."""
    with _approvals_lock:
        pending = _pending_approvals.pop(session_id, None)
    db.delete_pending_approval(session_id)  # Remove from DB too
    if not pending:
        return None

    if approved:
        # Execute the resolved command from the pending approval. Falls back
        # to the legacy 'cmd' arg if nothing was extracted (older flow).
        from tools.system_tools import _execute_command
        cmd = pending.get("cmd") or pending["args"].get("cmd", "")
        if not cmd:
            return "❌ No command stored for approval."
        result = _execute_command(cmd)
        db.add_message(session_id, "assistant", f"✅ Command approved and executed:\n{result}")
        return result
    else:
        msg = "❌ Command denied by user."
        db.add_message(session_id, "assistant", msg)
        return msg


def run(task, session_id=None, mode=None, think_level=None):
    """Run the agent on a task.

    Args:
        task: The user's request string.
        session_id: Optional session ID. If None, uses the active session.
        mode: Optional role override — 'planner', 'reviewer', 'qa',
            'interrogator', or None (default behaviour). Loaded from
            skills/roles/<mode>.md and appended to the system prompt.
        think_level: Optional reasoning depth — 'quick' (fast, fewer steps,
            no reflexion), 'think' (deeper, more steps, reflexion on),
            or None (default config).

    Returns:
        The agent's final response string.
    """
    # Resolve reasoning preset for this run
    preset = config.THINK_LEVELS.get(think_level) if think_level else None
    max_steps   = preset["max_steps"]   if preset else config.MAX_AGENT_STEPS
    llm_temp    = preset["temperature"] if preset else None  # None = client default
    llm_max_tok = preset["max_tokens"]  if preset else None
    use_reflexion = preset["reflexion"] if preset else True

    # Resolve session
    if session_id is None:
        session = db.get_active_session()
        session_id = session["id"]

    # Acquire per-session lock to prevent interleaved agent runs
    session_lock = _get_session_lock(session_id)
    if not session_lock.acquire(timeout=300):
        return "Another task is still running on this session. Please wait or try again."

    try:
        return _run_locked(
            task, session_id, max_steps, llm_temp, llm_max_tok,
            use_reflexion, mode, think_level,
        )
    finally:
        session_lock.release()


def _run_locked(task, session_id, max_steps, llm_temp, llm_max_tok,
                use_reflexion, mode, think_level):
    """Internal agent loop — called while holding the per-session lock."""
    # ── Security: check input for prompt injection ────────────────────────
    injection_warning = check_input(task)
    if injection_warning:
        log.warning("Input guardrail triggered: %s — task: %s", injection_warning, task[:100])
        db.add_message(session_id, "user", task)
        rejection = (
            "⚠️ Your message was flagged by security filters. "
            "If this is a legitimate request, please rephrase it."
        )
        db.add_message(session_id, "assistant", rejection)
        return rejection

    # Track user interaction time for idle detection
    import datetime as _dt
    db.kv_set("last_user_interaction",
              _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z")

    # Check for user feedback on previous task
    feedback = _detect_feedback(task)
    if feedback:
        try:
            last_metric = db.get_last_task_metric(session_id)
            if last_metric:
                db.update_task_feedback(last_metric["id"], feedback)
                log.info("Recorded %s feedback on task #%d", feedback, last_metric["id"])
        except Exception as exc:
            log.debug("Feedback recording failed: %s", exc)

    # Save the user message
    db.add_message(session_id, "user", task)

    # Start performance timer
    _task_start_time = time.monotonic()

    # Auto-compact if session is too long
    auto_compact_if_needed(session_id)

    # Build context from memory (with episodic tier seeded by current task)
    context = build_context(session_id, task=task)

    # ── Fast-path: simple greetings/acks skip the tool loop ──────────────
    if think_level != "think" and _is_fast_path(task):
        log.info("Fast-path detected for: %s", task[:40])
        response = _fast_response(task, session_id, context)
        if response:
            db.add_message(session_id, "assistant", response)
            return response
    # Get tool metadata
    tools_meta = registry.get_all_metadata()

    # Signal MCP servers: task is active — don't idle-shutdown
    try:
        from tools.mcp_client import set_task_active
        set_task_active(True)
    except Exception:
        pass

    steps = []
    final_answer = None

    # Reflexion / stuck tracking
    consecutive_errors = 0
    last_failed_tool = None
    last_failed_tool_errors = 0  # consecutive errors on the *same* tool
    pending_reflection = None  # injected into the next step's observation
    parse_failures = 0  # consecutive JSON parse failures

    for step_num in range(1, max_steps + 1):
        log.info("Step %d/%d for task: %s", step_num, max_steps, task[:80])

        # Build messages for the LLM
        messages = build_planner_messages(
            task=task,
            context=context,
            steps=steps if steps else None,
            tools_metadata=tools_meta,
            role=mode,
            think_level=think_level,
        )

        # Call the LLM
        try:
            raw_response = chat(messages, temperature=llm_temp, max_tokens=llm_max_tok)
        except LLMError as exc:
            log.error("LLM error at step %d: %s", step_num, exc)
            final_answer = f"Sorry, I encountered an error communicating with the AI: {exc}"
            break

        # Parse the JSON response
        parsed = extract_json(raw_response)

        if parsed is None:
            parse_failures += 1
            log.warning("Failed to parse LLM response (%d/%d)", parse_failures, _MAX_PARSE_FAILURES)
            if parse_failures >= _MAX_PARSE_FAILURES:
                final_answer = (
                    "I'm having trouble generating a valid response format. "
                    "Could you rephrase your request?"
                )
                break
            # Feed error back as an observation and try again
            steps.append({
                "step": step_num,
                "action": "(parse error)",
                "observation": (
                    "Your previous response was not valid JSON. "
                    "Please respond with ONLY a JSON object."
                ),
            })
            continue

        parse_failures = 0  # Reset on successful parse

        tool_name = parsed.get("tool", "")
        tool_args = parsed.get("args", {})
        thought = parsed.get("thought", "")

        if thought:
            log.info("Thought: %s", thought)

        # Check for finish
        if tool_name == "finish":
            final_answer = tool_args.get("output", "Done.")
            log.info("Agent finished: %s", final_answer[:100])
            break

        # Check if tool exists
        if tool_name not in registry:
            log.warning("Unknown tool: %s", tool_name)
            steps.append({
                "step": step_num,
                "action": f"{tool_name}({tool_args})",
                "observation": (
                    f"ERROR: Unknown tool '{tool_name}'. "
                    f"Available tools: {', '.join(registry.list_names())}"
                ),
            })
            consecutive_errors += 1
            continue

        # Execute the tool
        log.info("Executing tool: %s with args: %s", tool_name, tool_args)
        observation = registry.execute(tool_name, tool_args)

        # Handle approval-required commands
        if observation.startswith("APPROVAL_REQUIRED:"):
            log.info("Tool requires approval: %s", tool_name)
            # Extract the actual shell command from the sentinel payload.
            # Tools may emit either:
            #   APPROVAL_REQUIRED: <cmd>
            #   APPROVAL_REQUIRED: This command needs ... : <cmd>
            payload = observation[len("APPROVAL_REQUIRED:"):].strip()
            if ":" in payload and not payload.lstrip().startswith(("termux-", "am ", "/")):
                _, _, payload = payload.partition(":")
            cmd_for_approval = (payload or tool_args.get("cmd", "")).strip()
            with _approvals_lock:
                _pending_approvals[session_id] = {
                    "tool": tool_name,
                    "args": tool_args,
                    "cmd": cmd_for_approval,
                    "step": step_num,
                    "task": task,
                }
            # Persist to DB so approval survives restarts
            db.save_pending_approval(session_id, tool_name, tool_args, task)
            final_answer = (
                f"⚠️ **Approval Required**\n\n"
                f"Tool: `{tool_name}`\n"
                f"Command: `{cmd_for_approval}`\n\n"
                f"Reply /approve to execute or /deny to cancel."
            )
            db.add_message(session_id, "assistant", final_answer)
            db.log_task(session_id, task, steps, final_answer)
            return final_answer

        # Handle long observations: smart truncation to preserve key info
        if len(observation) > 4000:
            # Very long output — summarize with a cheap LLM call
            observation = _summarize_observation(observation, tool_name)
        elif len(observation) > 2000:
            # Moderately long — keep first + last sections
            observation = (
                observation[:1500] +
                "\n\n... (middle truncated, showing end) ...\n\n" +
                observation[-400:]
            )

        # ── Security: sanitize tool output ─────────────────────────────────
        observation = sanitize_tool_output(observation, tool_name)

        log.info("Observation (%d chars): %s", len(observation), observation[:200])

        # Reflexion: track per-tool failure streak; on threshold, generate or
        # reuse a stored reflection and prepend it to the observation so the
        # LLM sees it on the next planning round.
        is_error = observation.startswith("ERROR:")
        if is_error:
            consecutive_errors += 1
            if tool_name == last_failed_tool:
                last_failed_tool_errors += 1
            else:
                last_failed_tool = tool_name
                last_failed_tool_errors = 1

            if last_failed_tool_errors >= config.REFLEXION_AFTER_ERRORS and use_reflexion:
                err_hash = _error_hash(observation)
                cached = db.find_reflection(session_id, tool_name, err_hash)
                if cached:
                    pending_reflection = cached
                    log.info("Reused cached reflection for %s/%s", tool_name, err_hash)
                else:
                    reflection = _generate_reflection(
                        task, tool_name, tool_args, observation
                    )
                    if reflection:
                        db.save_reflection(
                            session_id, task, tool_name, err_hash,
                            observation[:500], reflection,
                        )
                        pending_reflection = reflection
                        log.info("Generated reflection: %s", reflection[:120])
        else:
            consecutive_errors = 0
            last_failed_tool = None
            last_failed_tool_errors = 0

        # Inject any pending reflection into this observation
        if pending_reflection:
            observation = (
                f"[Reflection on prior failures]\n{pending_reflection}\n\n"
                f"[Latest observation]\n{observation}"
            )
            pending_reflection = None

        steps.append({
            "step": step_num,
            "action": f"{tool_name}({tool_args})",
            "observation": observation,
        })

        # Persist a checkpoint after each step
        try:
            db.save_checkpoint(session_id, task, step_num, steps)
        except Exception as exc:
            log.debug("Checkpoint save failed: %s", exc)

        # Stuck-out: too many consecutive errors → bail and ask the user
        if consecutive_errors >= config.STUCK_ERROR_LIMIT:
            log.warning(
                "Agent stuck after %d consecutive errors, bailing out",
                consecutive_errors,
            )
            final_answer = (
                f"I hit {consecutive_errors} consecutive errors and don't want to "
                f"keep guessing. Last error:\n\n{observation[-800:]}\n\n"
                f"Could you give me more detail or try a different angle?"
            )
            break

    # If we exhausted steps without finishing
    if final_answer is None:
        if steps:
            last_obs = steps[-1].get("observation", "")
            final_answer = (
                f"I reached the maximum number of steps ({max_steps}). "
                f"Here's what I found so far:\n\n{last_obs}"
            )
        else:
            final_answer = "I wasn't able to process that request."

    # Signal MCP servers: task is done — idle shutdown allowed again
    try:
        from tools.mcp_client import set_task_active
        set_task_active(False)
    except Exception:
        pass

    # Save the assistant message
    db.add_message(session_id, "assistant", final_answer)

    # Log the task
    db.log_task(session_id, task, steps, final_answer)

    # Calculate performance metrics
    duration_ms = int((time.monotonic() - _task_start_time) * 1000)
    errors_count = sum(1 for s in steps if s.get("observation", "").startswith("ERROR:"))
    is_success = not final_answer.startswith(("Sorry,", "Error:", "I hit", "I'm having"))

    # Save task metrics (cheap, no LLM call)
    try:
        db.save_task_metrics(
            session_id=session_id,
            task_summary=task,
            steps_count=len(steps),
            errors_count=errors_count,
            duration_ms=duration_ms,
            success=is_success,
        )
    except Exception as exc:
        log.debug("Task metrics save failed: %s", exc)

    # ── Background post-task work (every N tasks, in a thread) ────────
    # Pattern extraction, profile extraction, auto-skill creation all
    # involve LLM calls. Run them in a background thread and only every
    # _POST_TASK_INTERVAL tasks to avoid blocking the user.
    if is_success:
        global _task_counter
        with _task_counter_lock:
            _task_counter += 1
            should_run_background = (_task_counter % _POST_TASK_INTERVAL == 0)

        if should_run_background and len(steps) >= 3:
            _bg_args = (session_id, task, list(steps))
            threading.Thread(
                target=_background_post_task,
                args=_bg_args,
                daemon=True,
                name="post-task-bg",
            ).start()
            log.debug("Queued background post-task work (task #%d)", _task_counter)

    # Trim old checkpoints so the table stays small
    try:
        db.prune_checkpoints(session_id, keep=20)
    except Exception as exc:
        log.debug("Checkpoint prune failed: %s", exc)

    return final_answer


def _background_post_task(session_id, task, steps):
    """Run post-task LLM work in a background thread.

    Called every _POST_TASK_INTERVAL successful tasks. Extracts patterns,
    user profile, and auto-creates skills — all of which involve LLM calls
    that would otherwise block the user.
    """
    try:
        from memory.patterns import extract_pattern
        extract_pattern(task, steps)
    except Exception as exc:
        log.debug("Background pattern extraction failed: %s", exc)

    try:
        _maybe_extract_profile(session_id, task)
    except Exception as exc:
        log.debug("Background profile extraction failed: %s", exc)

    try:
        _maybe_create_skill(task, steps)
    except Exception as exc:
        log.debug("Background skill creation failed: %s", exc)
