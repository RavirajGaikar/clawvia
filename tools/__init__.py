"""Tools package — importing this registers all tools with the registry."""

from tools.registry import registry  # noqa: F401

# Import all tool modules so their @registry.register decorators run
from tools import file_tools      # noqa: F401
from tools import system_tools    # noqa: F401
from tools import web_tools       # noqa: F401
from tools import device_tools    # noqa: F401
from tools import intent_tools    # noqa: F401
from tools import session_tools   # noqa: F401
from tools import memory_tools    # noqa: F401
from tools import schedule_tools  # noqa: F401
from tools import code_tools      # noqa: F401
from tools import todo_tools      # noqa: F401
from tools import skill_tools     # noqa: F401
from tools import subagent_tools  # noqa: F401
from tools import media_tools     # noqa: F401

# Optional: spin up any configured MCP servers and register their tools.
# Failures are logged and ignored so a bad server config never breaks startup.
try:
    from tools.mcp_client import load_mcp_servers
    _mcp_loaded = load_mcp_servers()
    if _mcp_loaded:
        from utils.logger import get_logger
        get_logger("tools").info("MCP: registered %d external tools", _mcp_loaded)
except Exception as _exc:  # pragma: no cover - never break import on MCP issues
    from utils.logger import get_logger
    get_logger("tools").warning("MCP setup skipped: %s", _exc)
