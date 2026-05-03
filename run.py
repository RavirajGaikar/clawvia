"""ClawVia entry point — starts FastAPI server + Telegram bot + scheduler."""

import threading
import signal
import sys

# Initialize logging before anything else
from utils.logger import setup_logging
setup_logging()

from utils.logger import get_logger
import config

log = get_logger("clawvia")


def _import_tools():
    """Import all tool modules to trigger registration."""
    import tools.file_tools       # noqa: F401
    import tools.system_tools     # noqa: F401
    import tools.web_tools        # noqa: F401
    import tools.device_tools     # noqa: F401
    import tools.session_tools    # noqa: F401
    import tools.memory_tools     # noqa: F401
    import tools.schedule_tools   # noqa: F401
    import tools.code_tools       # noqa: F401


def start_api_server():
    """Start the FastAPI server in a background thread."""
    import uvicorn
    from main import app  # noqa: F401 — ensures routes are registered

    log.info("Starting API server on %s:%d", config.API_HOST, config.API_PORT)
    uvicorn.run(
        app,
        host=config.API_HOST,
        port=config.API_PORT,
        log_level="warning",  # uvicorn's own logs
    )


def start_scheduler():
    """Start the background task scheduler."""
    import agent
    from scheduler import Scheduler, set_scheduler
    from telegram_bot import send_notification

    sched = Scheduler(
        agent_run_fn=agent.run,
        notify_fn=send_notification,
    )
    set_scheduler(sched)
    sched.start()
    log.info("Scheduler started")

    # Start dream cycle in a background timer
    _start_dream_timer()


def _start_dream_timer():
    """Schedule recurring dream cycles with adaptive frequency."""
    import threading
    from memory.dreaming import dream_cycle, get_dream_interval

    def _dream_loop():
        try:
            result = dream_cycle()
            if result:
                log.info("Dream cycle produced: %s", result[:80])
                try:
                    from telegram_bot import send_notification
                    send_notification(f"💭 Dream:\n{result}")
                except Exception:
                    pass
        except Exception as exc:
            log.error("Dream cycle error: %s", exc, exc_info=True)
        # Reschedule with adaptive interval
        interval = get_dream_interval() * 3600
        t = threading.Timer(interval, _dream_loop)
        t.daemon = True
        t.start()

    # First dream after 5 minutes (let system settle)
    t = threading.Timer(300, _dream_loop)
    t.daemon = True
    t.start()
    log.info("Dream timer started (initial interval %dh, first in 5m)",
             get_dream_interval())


def start_telegram():
    """Start the Telegram bot (blocking — runs in main thread)."""
    from telegram_bot import start_bot
    start_bot()


def main():
    # Ensure logs directory exists (for supervisord)
    import os
    os.makedirs(os.path.join(os.path.dirname(__file__), "logs"), exist_ok=True)

    log.info("=" * 50)
    log.info("ClawVia starting...")
    log.info("Model: %s", config.NVIDIA_MODEL)
    log.info("API: %s:%d", config.API_HOST, config.API_PORT)
    log.info("=" * 50)

    # Validate essential config
    config_errors = config.validate_required_config()
    for err in config_errors:
        log.warning("Config: %s", err)
    if not config.NVIDIA_API_KEY:
        log.error("NVIDIA_API_KEY not set! Add it to .env file.")
        sys.exit(1)
    if config.API_SECRET_KEY:
        log.info("API authentication enabled (X-API-Key required)")
    else:
        log.warning("API_SECRET_KEY not set — API endpoints are unauthenticated!")

    # Import all tools to register them
    _import_tools()

    # Load MCP servers
    try:
        from tools.mcp_client import load_mcp_servers
        mcp_count = load_mcp_servers()
        log.info("Loaded %d MCP tools", mcp_count)
    except Exception as exc:
        log.error("Failed to load MCP servers: %s", exc, exc_info=True)

    # Load skills
    try:
        from skills.loader import load_skills
        skills = load_skills()
        log.info("Loaded %d skills", len(skills))
    except Exception as exc:
        log.warning("Failed to load skills: %s", exc)

    if not config.TELEGRAM_BOT_TOKEN:
        log.warning("TELEGRAM_BOT_TOKEN not set — Telegram bot will not start.")
        log.info("Starting API server only...")
        start_api_server()
        return

    # Start API server in a background daemon thread
    api_thread = threading.Thread(target=start_api_server, daemon=True, name="api-server")
    api_thread.start()

    # Start scheduler in a background daemon thread
    start_scheduler()

    # Handle graceful shutdown
    def shutdown(signum, frame):
        log.info("Shutting down...")
        from scheduler import get_scheduler
        sched = get_scheduler()
        if sched:
            sched.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    # Start Telegram bot in main thread (blocking)
    start_telegram()


if __name__ == "__main__":
    main()
