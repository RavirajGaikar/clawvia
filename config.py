"""ClawVia configuration — loaded from environment / .env file."""

import os
from pathlib import Path

# Load .env file if present (python-dotenv)
try:
    from dotenv import load_dotenv
    _env_path = Path(__file__).parent / ".env"
    if _env_path.exists():
        load_dotenv(_env_path)
except ImportError:
    pass  # dotenv is optional; rely on real env vars

# ---------------------------------------------------------------------------
# LLM Provider (NVIDIA NIM)
# ---------------------------------------------------------------------------
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
NVIDIA_API_URL = os.environ.get(
    "NVIDIA_API_URL", "https://integrate.api.nvidia.com/v1/chat/completions"
)
NVIDIA_MODEL = os.environ.get("NVIDIA_MODEL", "minimaxai/minimax-m2.7")

# ---------------------------------------------------------------------------
# Vision model (NVIDIA NIM multimodal)
# ---------------------------------------------------------------------------
VISION_MODEL = os.environ.get(
    "VISION_MODEL", "mistralai/mistral-large-3-675b-instruct-2512"
)

# ---------------------------------------------------------------------------
# Groq (speech-to-text via Whisper)
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USER_ID = int(os.environ.get("ALLOWED_USER_ID", "0"))

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_PATH = os.environ.get("BASE_PATH", "/storage/emulated/0/Download")

# ---------------------------------------------------------------------------
# FastAPI server
# ---------------------------------------------------------------------------
API_HOST = os.environ.get("API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("API_PORT", "8001"))

# ---------------------------------------------------------------------------
# Agent behaviour
# ---------------------------------------------------------------------------
MAX_AGENT_STEPS = int(os.environ.get("MAX_AGENT_STEPS", "25"))
LLM_TEMPERATURE = float(os.environ.get("LLM_TEMPERATURE", "0.5"))
LLM_MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "2048"))
LLM_TIMEOUT = int(os.environ.get("LLM_TIMEOUT", "120"))

# How many consecutive ERROR observations before the agent gives up / asks user.
STUCK_ERROR_LIMIT = int(os.environ.get("STUCK_ERROR_LIMIT", "3"))

# Reflexion: after this many consecutive errors on the same tool, spend one extra
# LLM call to generate a verbal reflection that gets injected into the next step.
REFLEXION_AFTER_ERRORS = int(os.environ.get("REFLEXION_AFTER_ERRORS", "2"))
REFLEXION_MAX_TOKENS = int(os.environ.get("REFLEXION_MAX_TOKENS", "200"))

# Tool result cache: in-process dict, TTL in seconds. 0 disables.
TOOL_CACHE_TTL_SEC = int(os.environ.get("TOOL_CACHE_TTL_SEC", "300"))
TOOL_CACHE_MAX_ENTRIES = int(os.environ.get("TOOL_CACHE_MAX_ENTRIES", "128"))

# ---------------------------------------------------------------------------
# Reasoning depth presets (Phase 5)
# ---------------------------------------------------------------------------
# Controlled per-task via /quick or /think (or programmatic think_level=...).
# Each preset overrides MAX_AGENT_STEPS, LLM_TEMPERATURE, LLM_MAX_TOKENS for
# that one run.
THINK_LEVELS = {
    "quick": {
        "max_steps":  int(os.environ.get("QUICK_MAX_STEPS",  "5")),
        "temperature": float(os.environ.get("QUICK_TEMPERATURE", "0.1")),
        "max_tokens":  int(os.environ.get("QUICK_MAX_TOKENS",  "800")),
        "reflexion":   False,
    },
    "think": {
        "max_steps":   int(os.environ.get("THINK_MAX_STEPS",   "40")),
        "temperature": float(os.environ.get("THINK_TEMPERATURE", "0.4")),
        "max_tokens":  int(os.environ.get("THINK_MAX_TOKENS",  "4096")),
        "reflexion":   True,
    },
}

# Subagent (`task` tool) — caps to keep nested agent loops bounded.
SUBAGENT_MAX_DEPTH = int(os.environ.get("SUBAGENT_MAX_DEPTH", "2"))
SUBAGENT_MAX_STEPS = int(os.environ.get("SUBAGENT_MAX_STEPS", "10"))

# ---------------------------------------------------------------------------
# Context compaction
# ---------------------------------------------------------------------------
COMPACTION_THRESHOLD_TOKENS = int(os.environ.get("COMPACTION_THRESHOLD_TOKENS", "16000"))
POST_COMPACTION_KEEP_MESSAGES = int(os.environ.get("POST_COMPACTION_KEEP_MESSAGES", "8"))

# ---------------------------------------------------------------------------
# Memory notes
# ---------------------------------------------------------------------------
MEMORY_NOTES_DIR = os.environ.get(
    "MEMORY_NOTES_DIR", str(Path(__file__).parent / "memory" / "notes")
)

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------
SCHEDULER_CHECK_INTERVAL = int(os.environ.get("SCHEDULER_CHECK_INTERVAL", "30"))

# User timezone (IANA format, e.g. "Asia/Kolkata"). Used by scheduler for local time.
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")

# ---------------------------------------------------------------------------
# Tool approval
# ---------------------------------------------------------------------------
APPROVAL_TIMEOUT = int(os.environ.get("APPROVAL_TIMEOUT", "300"))  # 5 minutes

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("DB_PATH", "clawvia.db")

# ---------------------------------------------------------------------------
# Soul / persona file
# ---------------------------------------------------------------------------
SOUL_PATH = os.environ.get("SOUL_PATH", str(Path(__file__).parent / "soul.md"))

# ---------------------------------------------------------------------------
# Code execution
# ---------------------------------------------------------------------------
CODE_EXEC_TIMEOUT = int(os.environ.get("CODE_EXEC_TIMEOUT", "30"))

# ---------------------------------------------------------------------------
# Dreaming
# ---------------------------------------------------------------------------
DREAM_INTERVAL_HOURS = int(os.environ.get("DREAM_INTERVAL_HOURS", "6"))
DREAM_MIN_SCORE = float(os.environ.get("DREAM_MIN_SCORE", "0.3"))

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------
API_SECRET_KEY = os.environ.get("API_SECRET_KEY", "")  # If set, API requires X-API-Key header


def validate_required_config():
    """Check that essential configuration is set. Returns list of errors."""
    errors = []
    if not NVIDIA_API_KEY:
        errors.append("NVIDIA_API_KEY not set")
    if not TELEGRAM_BOT_TOKEN:
        errors.append("TELEGRAM_BOT_TOKEN not set (bot will not start)")
    if not ALLOWED_USER_ID:
        errors.append("ALLOWED_USER_ID not set (Telegram bot will reject all users)")
    return errors