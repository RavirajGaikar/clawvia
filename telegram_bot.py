"""ClawVia Telegram bot — polling-based interface to the agent."""

import asyncio
import os
import sys
import time

from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ChatAction
from telegram.request import HTTPXRequest

import agent
import config
from media.vision import analyze_image
from media.speech import transcribe_audio
from memory import db
from memory.compaction import compact_session
from tools.registry import registry
from utils.logger import get_logger

log = get_logger("telegram")

_MAX_MESSAGE_LEN = 4096  # Telegram's limit
_start_time = time.time()

# Telegram app reference for scheduler notifications
_telegram_app = None
_notify_chat_id = None
_bot_loop = None  # event loop ref for cross-thread notifications
_CONNECT_STATE = {}  # For interactive /connect setups  {user_id: {"integration", "spec", "ts"}}
_CONNECT_TIMEOUT = 300  # Expire abandoned /connect flows after 5 minutes


def _is_allowed(update):
    """Check if the user is allowed to interact with the bot.

    FAIL-CLOSED: If ALLOWED_USER_ID is not configured (0), reject everyone.
    """
    user_id = update.effective_user.id

    # Fail-closed: no configured user = reject all
    if not config.ALLOWED_USER_ID:
        log.warning("Rejected user %d — ALLOWED_USER_ID not configured (fail-closed)", user_id)
        return False

    if user_id != config.ALLOWED_USER_ID:
        log.warning("Unauthorized user: %d (allowed: %d)", user_id, config.ALLOWED_USER_ID)
        return False
    return True


def _split_message(text):
    """Split a long message into chunks that fit Telegram's limit."""
    if len(text) <= _MAX_MESSAGE_LEN:
        return [text]

    chunks = []
    while text:
        if len(text) <= _MAX_MESSAGE_LEN:
            chunks.append(text)
            break
        # Try to split at a newline near the limit
        split_at = text.rfind("\n", 0, _MAX_MESSAGE_LEN)
        if split_at == -1 or split_at < _MAX_MESSAGE_LEN // 2:
            split_at = _MAX_MESSAGE_LEN
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    return chunks


# ── Command Handlers ──────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    await update.message.reply_text(
        "🦞 *ClawVia* is ready\\!\n\n"
        "Send me any message and I'll work on it\\.\n\n"
        "*Commands:*\n"
        "/new \\- New session\n"
        "/sessions \\- List sessions\n"
        "/switch `<id>` \\- Switch session\n"
        "/reset \\- Clear current session\n"
        "/tools \\- List available tools\n"
        "/status \\- System status\n"
        "/compact \\- Compress conversation history\n"
        "/schedules \\- List scheduled tasks\n"
        "/skills \\- List loaded skills\n"
        "/skill `install|remove|update|run` \\- Manage skills & recipes\n"
        "/connect \\- Manage external integrations \\(Notion, Calendar, etc\\)\\.\n"
        "/quick `<task>` \\- Fast answer \\(few steps\\)\n"
        "/think `<task>` \\- Deep reasoning \\(more steps\\)\n"
        "/approve \\- Approve a pending command\n"
        "/deny \\- Deny a pending command",
        parse_mode="MarkdownV2",
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    title = " ".join(context.args) if context.args else "New Session"
    session = db.create_session(title)
    await update.message.reply_text(
        f"Created session #{session['id']}: {session['title']}"
    )


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    sessions = db.list_sessions()
    if not sessions:
        await update.message.reply_text("No sessions yet. Send a message to start!")
        return

    lines = []
    for s in sessions:
        marker = "▸" if s["is_active"] else " "
        lines.append(f"{marker} #{s['id']}: {s['title']}  [{s['created_at'][:10]}]")
    await update.message.reply_text("Sessions:\n" + "\n".join(lines))


async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /switch <session_id>")
        return
    try:
        sid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Session ID must be a number.")
        return

    session = db.switch_session(sid)
    if session:
        await update.message.reply_text(f"Switched to session #{session['id']}: {session['title']}")
    else:
        await update.message.reply_text(f"Session #{sid} not found.")


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    session = db.get_active_session()
    db.clear_messages(session["id"])
    await update.message.reply_text(f"Cleared session #{session['id']}: {session['title']}")


async def cmd_tools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    tools = registry.get_all_metadata()
    lines = [f"Available tools ({len(tools)}):"]
    for t in tools:
        lines.append(f"• {t['name']}: {t['description']}")
    for chunk in _split_message("\n".join(lines)):
        await update.message.reply_text(chunk)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    uptime_secs = int(time.time() - _start_time)
    hours, remainder = divmod(uptime_secs, 3600)
    mins, secs = divmod(remainder, 60)

    session = db.get_active_session()
    sessions = db.list_sessions()
    tools_count = len(registry)

    try:
        import resource
        mem_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        mem_str = f"{mem_mb:.1f} MB"
    except ImportError:
        # Windows fallback
        try:
            import psutil
            mem_str = f"{psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024:.1f} MB"
        except ImportError:
            mem_str = "N/A"
    except Exception:
        mem_str = "N/A"

    await update.message.reply_text(
        f"ClawVia Status\n"
        f"Uptime: {hours}h {mins}m {secs}s\n"
        f"Active session: #{session['id']} ({session['title']})\n"
        f"Total sessions: {len(sessions)}\n"
        f"Tools loaded: {tools_count}\n"
        f"Memory usage: {mem_str}\n"
        f"LLM: {config.NVIDIA_MODEL}\n"
        f"Python: {sys.version.split()[0]}\n"
        f"Compactions: {db.get_compaction_count(session['id'])}"
    )


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    session = db.get_active_session()
    instruction = " ".join(context.args) if context.args else None

    await update.message.reply_text("Compacting conversation history...")
    stats = compact_session(session["id"], instruction=instruction)

    if stats:
        await update.message.reply_text(
            f"Compacted session #{stats['session_id']}:\n"
            f"Messages: {stats['messages_before']} → {stats['messages_after']}\n"
            f"Tokens: ~{stats['tokens_before']} → ~{stats['tokens_after']} "
            f"(saved ~{stats['tokens_saved']})"
        )
    else:
        await update.message.reply_text("No compaction needed — session is small enough.")


async def cmd_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    tasks = db.list_scheduled_tasks(include_inactive=False)
    if not tasks:
        await update.message.reply_text("No active scheduled tasks.")
        return
    lines = ["Active scheduled tasks:"]
    for t in tasks:
        kind = "one-shot" if t["schedule_type"] == "at" else f"every {t['schedule_value']}"
        lines.append(f"#{t['id']}: [{kind}] {t['prompt'][:60]}\n  Next: {t['next_run']}")
    await update.message.reply_text("\n".join(lines))


async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    try:
        from skills.loader import list_skill_info
        skills = list_skill_info()
    except Exception:
        skills = []
    if not skills:
        await update.message.reply_text("No skills loaded.")
        return
    lines = [f"Skills ({len(skills)}):"]
    for s in skills:
        status = "✓" if s["eligible"] else "✗"
        desc = f" — {s['description']}" if s["description"] else ""
        ver = f" v{s['version']}" if s.get("version") else ""
        src = "  [bundled]" if not s.get("source") else "  [installed]"
        lines.append(f"{status} {s['name']}{ver}{desc}{src}")
    await update.message.reply_text("\n".join(lines))


async def cmd_skill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage skills:  /skill list | install <url> | remove <name> | update <name> | recipes <skill> | run <skill> <recipe>"""
    if not _is_allowed(update):
        return
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage:\n"
            "/skill list\n"
            "/skill install <git-url-or-local-path>\n"
            "/skill remove <name>\n"
            "/skill update <name>\n"
            "/skill recipes <skill>\n"
            "/skill run <skill> <recipe>"
        )
        return

    sub = args[0].lower()
    rest = args[1:]

    def _to_thread(fn, *a, **kw):
        return asyncio.to_thread(fn, *a, **kw)

    try:
        from skills import manager as skill_manager
        from skills import recipes as skill_recipes
    except Exception as exc:
        await update.message.reply_text(f"❌ Skills subsystem unavailable: {exc}")
        return

    if sub in {"list", "ls"}:
        items = await _to_thread(skill_manager.list_installed)
        if not items:
            await update.message.reply_text("(no skills)")
            return
        lines = [f"Installed skills ({len(items)}):"]
        for s in items:
            tag = "bundled" if s["bundled"] else (s["source"] or "local")
            ver = f" v{s['version']}" if s["version"] else ""
            lines.append(f"• {s['name']}{ver}  — {s['description'] or '(no desc)'}\n    {tag}")
        await update.message.reply_text("\n".join(lines))
        return

    if sub == "install":
        if not rest:
            await update.message.reply_text("Usage: /skill install <git-url-or-local-path>")
            return
        source = rest[0]
        await update.message.reply_text(f"Installing from {source} …")
        ok, msg = await _to_thread(skill_manager.install_skill, source)
        await update.message.reply_text(("✅ " if ok else "❌ ") + msg)
        return

    if sub == "remove":
        if not rest:
            await update.message.reply_text("Usage: /skill remove <name>")
            return
        ok, msg = await _to_thread(skill_manager.remove_skill, rest[0])
        await update.message.reply_text(("✅ " if ok else "❌ ") + msg)
        return

    if sub == "update":
        if not rest:
            await update.message.reply_text("Usage: /skill update <name>")
            return
        await update.message.reply_text(f"Updating {rest[0]} …")
        ok, msg = await _to_thread(skill_manager.update_skill, rest[0])
        await update.message.reply_text(("✅ " if ok else "❌ ") + msg)
        return

    if sub == "recipes":
        if not rest:
            await update.message.reply_text("Usage: /skill recipes <skill>")
            return
        names = await _to_thread(skill_manager.list_recipes, rest[0])
        if not names:
            await update.message.reply_text(f"(no recipes in {rest[0]})")
            return
        await update.message.reply_text(
            f"Recipes in {rest[0]}:\n" + "\n".join(f"• {n}" for n in names)
        )
        return

    if sub == "run":
        if len(rest) < 2:
            await update.message.reply_text("Usage: /skill run <skill> <recipe>")
            return
        skill_name, recipe_name = rest[0], rest[1]
        data, err = await _to_thread(skill_manager.get_recipe, skill_name, recipe_name)
        if err:
            await update.message.reply_text(f"❌ {err}")
            return
        await update.message.reply_text(f"Running {skill_name}/{recipe_name} …")
        result = await _to_thread(skill_recipes.run_recipe, data, {})
        text = skill_recipes.format_result(result)
        for chunk in _split_message(text):
            await update.message.reply_text(chunk)
        return

    await update.message.reply_text(f"Unknown subcommand: {sub}")


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    session = db.get_active_session()
    pending = agent.get_pending_approval(session["id"])
    if not pending:
        await update.message.reply_text("No pending command to approve.")
        return
    await update.message.reply_text("Executing approved command...")
    result = agent.resolve_approval(session["id"], approved=True)
    chunks = _split_message(f"✅ Result:\n{result}")
    for chunk in chunks:
        await update.message.reply_text(chunk)


async def cmd_deny(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return
    session = db.get_active_session()
    pending = agent.get_pending_approval(session["id"])
    if not pending:
        await update.message.reply_text("No pending command to deny.")
        return
    agent.resolve_approval(session["id"], approved=False)
    await update.message.reply_text("❌ Command denied.")


async def cmd_checkpoints(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List recent agent-loop checkpoints for the active session.

    Use /checkpoints <id> to inspect a single checkpoint's step history.
    """
    if not _is_allowed(update):
        return
    session = db.get_active_session()

    if context.args:
        try:
            cp_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /checkpoints [id]")
            return
        cp = db.get_checkpoint(cp_id)
        if not cp or cp["session_id"] != session["id"]:
            await update.message.reply_text(f"Checkpoint #{cp_id} not found.")
            return
        lines = [f"Checkpoint #{cp['id']} — step {cp['step']} — {cp['created_at']}",
                 f"Task: {cp['task'][:120]}"]
        for s in cp["steps"]:
            obs = s.get("observation", "")
            if len(obs) > 300:
                obs = obs[:300] + "..."
            lines.append(f"\n[{s.get('step')}] {s.get('action')}\n  {obs}")
        for chunk in _split_message("\n".join(lines)):
            await update.message.reply_text(chunk)
        return

    cps = db.list_checkpoints(session["id"], limit=15)
    if not cps:
        await update.message.reply_text("No checkpoints yet.")
        return
    lines = [f"Recent checkpoints (session #{session['id']}):"]
    for c in cps:
        lines.append(f"#{c['id']}  step {c['step']}  {c['created_at']}  — {c['task'][:60]}")
    lines.append("\nUse /checkpoints <id> to inspect.")
    await update.message.reply_text("\n".join(lines))


async def cmd_reflections(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent failure reflections (Reflexion memory)."""
    if not _is_allowed(update):
        return
    session = db.get_active_session()
    refs = db.list_reflections(session["id"], limit=10)
    if not refs:
        await update.message.reply_text("No reflections recorded.")
        return
    lines = [f"Recent reflections (session #{session['id']}):"]
    for r in refs:
        lines.append(
            f"\n[{r['created_at']}] tool={r['tool']}"
            f"\n  error: {r['error'][:140]}"
            f"\n  reflection: {r['reflection'][:300]}"
        )
    for chunk in _split_message("\n".join(lines)):
        await update.message.reply_text(chunk)

async def cmd_mcp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List MCP servers + the tools each one exposes."""
    if not _is_allowed(update):
        return
    try:
        from tools.mcp_client import _CLIENTS  # noqa: WPS437
    except Exception as exc:
        await update.message.reply_text(f"MCP not available: {exc}")
        return
    if not _CLIENTS:
        await update.message.reply_text(
            "No MCP servers loaded. Add them to mcp_servers.json and restart."
        )
        return
    lines = [f"MCP servers ({len(_CLIENTS)}):"]
    for c in _CLIENTS:
        prefix = f"mcp_{c.name}_"
        own = [n for n in registry.list_names() if n.startswith(prefix)]
        lines.append(f"\n• {c.name} ({type(c).__name__}) — {len(own)} tools")
        for n in own:
            lines.append(f"    - {n}")
    for chunk in _split_message("\n".join(lines)):
        await update.message.reply_text(chunk)


async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manage MCP integrations (Notion, GitHub, Google Calendar, etc)."""
    user_id = update.effective_user.id
    if not _is_allowed(update):
        return
        
    args = context.args or []
    
    from integrations import manager as int_manager
    registry = int_manager.get_registry()
    installed = int_manager.get_installed()
    
    if not args:
        lines = ["🔌 Integrations (MCP):", ""]
        for k, v in registry.items():
            status = "✅ Installed" if k in installed else "❌ Not installed"
            lines.append(f"• {v['name']} (`{k}`) — {status}")
            lines.append(f"  {v['description']}")
            lines.append("")
        lines.append("To install: /connect <name>")
        lines.append("To remove: /connect remove <name>")
        await update.message.reply_text("\n".join(lines))
        return
        
    sub = args[0].lower()
    
    if sub == "remove":
        if len(args) < 2:
            await update.message.reply_text("Usage: /connect remove <name>")
            return
        name = args[1].lower()
        await update.message.reply_text(f"Removing {name}...")
        ok, msg = await asyncio.to_thread(int_manager.remove, name)
        await update.message.reply_text(("✅ " if ok else "❌ ") + msg + "\nRestart ClawVia to apply changes (clawvia --stop && clawvia).")
        return
        
    if sub in registry:
        if sub in installed:
            await update.message.reply_text(f"{registry[sub]['name']} is already installed.")
            return
            
        spec = registry[sub]
        auth = spec.get("auth")
        if not auth:
            await update.message.reply_text(f"Installing {spec['name']}...")
            def _do_install():
                return int_manager.install(sub, "")
            ok, msg = await asyncio.to_thread(_do_install)
            await update.message.reply_text(("✅ " if ok else "❌ ") + msg + "\nRestart ClawVia to apply changes.")
            return
            
        _CONNECT_STATE[user_id] = {"integration": sub, "spec": spec, "ts": time.time()}
        
        await update.message.reply_text(
            f"📎 Setting up {spec['name']}\n\n"
            f"{auth['instructions']}\n\n"
            f"Reply with your {auth.get('hint', 'token/key')} (or type 'cancel'):"
        )
        return
        
    await update.message.reply_text(f"Unknown integration: {sub}")

# ── Plan / Act / Review / Todo / Roles ────────────────────────────────────


async def _run_with_think(update, text, level):
    """Run the agent with a reasoning depth preset (quick/think)."""
    await update.message.chat.send_action(ChatAction.TYPING)
    try:
        # agent.run signature: (task, session_id=None, mode=None, think_level=None)
        result = await asyncio.to_thread(agent.run, text, None, None, level)
    except Exception as exc:
        log.error("Agent error (think=%s): %s", level, exc, exc_info=True)
        result = f"Error: {exc}"
    for chunk in _split_message(result):
        await update.message.reply_text(chunk)


async def cmd_quick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Fast mode — fewer steps, lower temperature, no reflexion. Use for snappy lookups."""
    if not _is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /quick <task>\nFast path — capped to ~5 steps, low temp."
        )
        return
    await _run_with_think(update, " ".join(context.args), "quick")


async def cmd_think(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deep-think mode — more steps, more tokens, reflexion on. Use for hard problems."""
    if not _is_allowed(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: /think <task>\nDeep path — bigger step budget, reflexion on."
        )
        return
    await _run_with_think(update, " ".join(context.args), "think")


async def cmd_todo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show / manage the current session's todo list.

    /todo                  list all
    /todo open             list only open/in_progress
    /todo add <text>       add a todo
    /todo done <id>        mark done
    /todo cancel <id>      mark cancelled
    /todo clear            wipe all
    """
    if not _is_allowed(update):
        return
    session = db.get_active_session()
    sid = session["id"]
    args = context.args or []
    icons = {"open": "[ ]", "in_progress": "[~]", "done": "[x]", "cancelled": "[-]"}

    if not args:
        items = db.todo_list(sid, include_done=True)
        if not items:
            await update.message.reply_text(
                "No todos. Add with /todo add <text>."
            )
            return
        lines = [f"Todos (session #{sid}):"]
        for t in items:
            lines.append(f"{icons.get(t['status'], '[?]')} #{t['id']} {t['text']}")
        await update.message.reply_text("\n".join(lines))
        return

    sub = args[0].lower()
    if sub == "open":
        items = db.todo_list(sid, include_done=False)
        if not items:
            await update.message.reply_text("No open todos.")
            return
        lines = [f"Open todos (session #{sid}):"]
        for t in items:
            lines.append(f"{icons.get(t['status'], '[?]')} #{t['id']} {t['text']}")
        await update.message.reply_text("\n".join(lines))
    elif sub == "add":
        text = " ".join(args[1:]).strip()
        if not text:
            await update.message.reply_text("Usage: /todo add <text>")
            return
        tid = db.todo_add(sid, text)
        await update.message.reply_text(f"Added todo #{tid}: {text}")
    elif sub in {"done", "cancel"}:
        if len(args) < 2:
            await update.message.reply_text(f"Usage: /todo {sub} <id>")
            return
        try:
            tid = int(args[1])
        except ValueError:
            await update.message.reply_text("Todo id must be a number.")
            return
        status = "done" if sub == "done" else "cancelled"
        ok = db.todo_update(sid, tid, status=status)
        await update.message.reply_text(
            f"todo #{tid} → {status}" if ok else f"todo #{tid} not found"
        )
    elif sub == "clear":
        n = db.todo_clear(sid)
        await update.message.reply_text(f"Cleared {n} todos.")
    else:
        await update.message.reply_text(
            "Unknown /todo subcommand. Try: /todo, /todo open, "
            "/todo add <text>, /todo done <id>, /todo cancel <id>, /todo clear"
        )


# ── Message Handler ───────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_allowed(update):
        return

    user_text = update.message.text
    if not user_text or not user_text.strip():
        return
        
    user_id = update.effective_user.id
    if user_id in _CONNECT_STATE:
        state = _CONNECT_STATE[user_id]
        # Expire stale connect flows
        if time.time() - state.get("ts", 0) > _CONNECT_TIMEOUT:
            del _CONNECT_STATE[user_id]
        elif user_text.lower() == "cancel":
            del _CONNECT_STATE[user_id]
            await update.message.reply_text("Setup cancelled.")
            return
        else:
            integration = state["integration"]
            spec = state["spec"]
            del _CONNECT_STATE[user_id]
        
        await update.message.reply_text(f"Installing {spec['name']} (this might take a minute if Node.js needs to be installed)...")
        from integrations import manager as int_manager
        
        def _do_install():
            return int_manager.install(integration, user_text.strip())
            
        ok, msg = await asyncio.to_thread(_do_install)
        await update.message.reply_text(("✅ " if ok else "❌ ") + msg + "\nRestart ClawVia to apply changes (clawvia --stop && clawvia).")
        return

    log.info("Message from %d: %s", update.effective_user.id, user_text[:80])

    # Track chat ID for scheduler notifications
    global _notify_chat_id
    _notify_chat_id = update.effective_chat.id

    # Keep typing indicator alive while agent is working
    stop_typing = asyncio.Event()

    async def _keep_typing():
        while not stop_typing.is_set():
            try:
                await update.message.chat.send_action(ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_typing.wait(), timeout=4.0)
                break
            except asyncio.TimeoutError:
                pass

    typing_task = asyncio.create_task(_keep_typing())

    try:
        result = await asyncio.to_thread(agent.run, user_text)
    except Exception as exc:
        log.error("Agent error: %s", exc, exc_info=True)
        result = f"Error: {exc}"
    finally:
        stop_typing.set()
        await typing_task

    # Send response (split if needed)
    chunks = _split_message(result)
    for chunk in chunks:
        await update.message.reply_text(chunk)


# ── Media Handlers ────────────────────────────────────────────────────────

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming photo messages — analyze with vision model."""
    if not _is_allowed(update):
        return

    log.info("Photo from %d", update.effective_user.id)

    global _notify_chat_id
    _notify_chat_id = update.effective_chat.id

    # Download the highest-resolution photo
    photo = update.message.photo[-1]  # Last = largest
    file = await context.bot.get_file(photo.file_id)

    # Save directly into Downloads
    timestamp = int(time.time())
    save_path = os.path.join(config.BASE_PATH, f"photo_{timestamp}_{photo.file_id[:8]}.jpg")
    await file.download_to_drive(save_path)
    log.info("Photo saved: %s", save_path)

    # Keep typing while processing
    stop_typing = asyncio.Event()

    async def _keep_typing():
        while not stop_typing.is_set():
            try:
                await update.message.chat.send_action(ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_typing.wait(), timeout=4.0)
                break
            except asyncio.TimeoutError:
                pass

    typing_task = asyncio.create_task(_keep_typing())

    try:
        # Analyze image
        caption = update.message.caption or ""
        description = await asyncio.to_thread(analyze_image, save_path, caption or None)

        # Build task for agent
        if caption:
            task = (
                f"[User sent a photo: {save_path}]\n"
                f"Image analysis: {description}\n\n"
                f"User's message: {caption}"
            )
        else:
            task = (
                f"[User sent a photo: {save_path}]\n"
                f"Image analysis: {description}\n\n"
                f"Respond to what you see in the image."
            )

        result = await asyncio.to_thread(agent.run, task)
    except Exception as exc:
        log.error("Photo handler error: %s", exc, exc_info=True)
        result = f"Error processing photo: {exc}"
    finally:
        stop_typing.set()
        await typing_task

    chunks = _split_message(result)
    for chunk in chunks:
        await update.message.reply_text(chunk)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle incoming voice/audio messages — transcribe and process."""
    if not _is_allowed(update):
        return

    log.info("Voice message from %d", update.effective_user.id)

    global _notify_chat_id
    _notify_chat_id = update.effective_chat.id

    # Download voice file
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    file = await context.bot.get_file(voice.file_id)
    timestamp = int(time.time())
    ext = "ogg" if update.message.voice else "mp3"
    save_path = os.path.join(config.BASE_PATH, f"voice_{timestamp}_{voice.file_id[:8]}.{ext}")
    await file.download_to_drive(save_path)
    log.info("Voice saved: %s", save_path)

    # Keep typing while processing
    stop_typing = asyncio.Event()

    async def _keep_typing():
        while not stop_typing.is_set():
            try:
                await update.message.chat.send_action(ChatAction.TYPING)
            except Exception:
                pass
            try:
                await asyncio.wait_for(stop_typing.wait(), timeout=4.0)
                break
            except asyncio.TimeoutError:
                pass

    typing_task = asyncio.create_task(_keep_typing())

    try:
        # Transcribe audio
        transcribed = await asyncio.to_thread(transcribe_audio, save_path)

        if transcribed.startswith("ERROR:"):
            result = transcribed
        else:
            # Feed transcribed text to agent as if user typed it
            result = await asyncio.to_thread(agent.run, transcribed)
    except Exception as exc:
        log.error("Voice handler error: %s", exc, exc_info=True)
        result = f"Error processing voice: {exc}"
    finally:
        stop_typing.set()
        await typing_task

    chunks = _split_message(result)
    for chunk in chunks:
        await update.message.reply_text(chunk)


# ── Notifications (used by scheduler) ─────────────────────────────────────

def send_notification(text):
    """Send a notification message to the user (called from scheduler thread).

    Fire-and-forget: schedules the coroutine on the bot's event loop without
    blocking the scheduler thread.  Errors are logged via a callback.
    """
    if not _telegram_app or not _notify_chat_id or not _bot_loop:
        log.warning("Cannot send notification: no chat ID, app, or event loop reference")
        return

    async def _send():
        try:
            chunks = _split_message(text)
            for chunk in chunks:
                await _telegram_app.bot.send_message(
                    chat_id=_notify_chat_id,
                    text=chunk,
                )
        except Exception as exc:
            log.error("Failed to send notification: %s", exc)

    def _on_done(fut):
        exc = fut.exception()
        if exc:
            log.error("Notification delivery failed: %s", exc)

    future = asyncio.run_coroutine_threadsafe(_send(), _bot_loop)
    future.add_done_callback(_on_done)


# ── Bot Startup ───────────────────────────────────────────────────────────

def start_bot():
    """Start the Telegram bot with polling."""
    if not config.TELEGRAM_BOT_TOKEN:
        log.error("TELEGRAM_BOT_TOKEN not set!")
        return

    log.info("Starting Telegram bot...")

    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_BOT_TOKEN)
        .concurrent_updates(True)
        .connection_pool_size(8)
        .pool_timeout(10.0)
        .connect_timeout(15.0)
        .read_timeout(30.0)
        .build()
    )

    # Store app reference; event loop will be captured once polling starts
    global _telegram_app
    _telegram_app = app

    # Register commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("switch", cmd_switch))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("tools", cmd_tools))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("compact", cmd_compact))
    app.add_handler(CommandHandler("schedules", cmd_schedules))
    app.add_handler(CommandHandler("skills", cmd_skills))
    app.add_handler(CommandHandler("skill", cmd_skill))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("deny", cmd_deny))
    app.add_handler(CommandHandler("checkpoints", cmd_checkpoints))
    app.add_handler(CommandHandler("reflections", cmd_reflections))
    app.add_handler(CommandHandler("mcp", cmd_mcp))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("todo", cmd_todo))
    app.add_handler(CommandHandler("quick", cmd_quick))
    app.add_handler(CommandHandler("think", cmd_think))

    # Regular messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    # Media messages
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))

    # Error handler for graceful recovery from network errors
    async def _error_handler(update, context):
        from telegram.error import NetworkError, TimedOut
        if isinstance(context.error, (NetworkError, TimedOut)):
            log.warning("Network hiccup: %s", context.error)
        else:
            log.error("Telegram handler error: %s", context.error, exc_info=context.error)

    app.add_error_handler(_error_handler)

    # Capture the REAL event loop once polling starts (post_start runs
    # inside the asyncio.run() that run_polling creates).
    async def _capture_loop(application):
        global _bot_loop
        _bot_loop = asyncio.get_running_loop()
        log.info("Event loop captured for cross-thread notifications")

    log.info("Telegram bot started, polling...")
    app.run_polling(drop_pending_updates=True, post_start=_capture_loop)