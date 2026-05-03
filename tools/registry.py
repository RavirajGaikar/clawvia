"""Tool registry — maps tool names to functions with metadata.

Lightweight in-process cache for read-only tools. Entries are keyed by
(tool_name, sorted-args-json) and aged out after TOOL_CACHE_TTL_SEC.
No external dependencies — plain dict + monotonic clock.
"""

import json
import threading
import time

import config
from utils.logger import get_logger

log = get_logger("tools.registry")


def _args_key(args):
    """Stable JSON key for a (small) args dict. Falls back to repr on TypeError."""
    try:
        return json.dumps(args, sort_keys=True, default=str)
    except Exception:
        return repr(sorted(args.items()))


class ToolRegistry:
    """Central registry for all agent tools."""

    def __init__(self):
        self._tools = {}
        # cache: {(name, args_key): (result_str, expires_at)}
        self._cache = {}
        self._cache_lock = threading.Lock()

    def register(self, name, description, args_schema=None, cacheable=False):
        """Decorator to register a tool function.

        Usage:
            @registry.register("my_tool", "Does something", {"arg1": "string"})
            def my_tool(arg1):
                ...

        Set cacheable=True for read-only tools whose output is stable for the
        TOOL_CACHE_TTL_SEC window (e.g. web_search, list_files).
        """
        if args_schema is None:
            args_schema = {}

        def decorator(func):
            self._tools[name] = {
                "name": name,
                "description": description,
                "args": args_schema,
                "func": func,
                "cacheable": bool(cacheable),
            }
            log.debug("Registered tool: %s (cacheable=%s)", name, cacheable)
            return func

        return decorator

    def register_func(self, name, func, description, args_schema=None, cacheable=False):
        """Register a tool function directly (non-decorator)."""
        if args_schema is None:
            args_schema = {}
        self._tools[name] = {
            "name": name,
            "description": description,
            "args": args_schema,
            "func": func,
            "cacheable": bool(cacheable),
        }
        log.debug("Registered tool: %s (cacheable=%s)", name, cacheable)

    def get(self, name):
        """Get a tool entry by name, or None."""
        return self._tools.get(name)

    # ── Cache helpers ─────────────────────────────────────────────────────

    def _cache_get(self, name, args):
        ttl = config.TOOL_CACHE_TTL_SEC
        if ttl <= 0:
            return None
        key = (name, _args_key(args))
        with self._cache_lock:
            entry = self._cache.get(key)
            if not entry:
                return None
            result, expires_at = entry
            if time.monotonic() > expires_at:
                self._cache.pop(key, None)
                return None
            return result

    def _cache_set(self, name, args, result):
        ttl = config.TOOL_CACHE_TTL_SEC
        if ttl <= 0:
            return
        with self._cache_lock:
            # Bound size: drop oldest-ish entry if full (simple, not strict LRU).
            if len(self._cache) >= config.TOOL_CACHE_MAX_ENTRIES:
                try:
                    self._cache.pop(next(iter(self._cache)))
                except StopIteration:
                    pass
            self._cache[(name, _args_key(args))] = (result, time.monotonic() + ttl)

    def cache_clear(self):
        """Drop all cached results (e.g. after write actions)."""
        with self._cache_lock:
            self._cache.clear()

    def execute(self, name, args=None):
        """Execute a tool by name with the given args dict.

        Returns the result string, or an error string prefixed with 'ERROR:'.
        Read-only tools registered with cacheable=True are served from a
        short-lived in-process cache.
        """
        if args is None:
            args = {}

        tool = self._tools.get(name)
        if tool is None:
            return f"ERROR: Unknown tool '{name}'"

        if tool.get("cacheable"):
            cached = self._cache_get(name, args)
            if cached is not None:
                log.debug("Tool cache hit: %s", name)
                return cached

        try:
            result = tool["func"](**args)
            result_str = str(result) if result is not None else "OK (no output)"
        except TypeError as exc:
            log.error("Tool '%s' argument error: %s", name, exc)
            return f"ERROR: Bad arguments for '{name}': {exc}"
        except Exception as exc:
            log.error("Tool '%s' failed: %s", name, exc, exc_info=True)
            return f"ERROR: Tool '{name}' failed: {exc}"

        if tool.get("cacheable") and not result_str.startswith("ERROR:"):
            self._cache_set(name, args, result_str)
        return result_str

    def get_all_metadata(self):
        """Return list of tool metadata dicts (name, description, args)."""
        return [
            {
                "name": t["name"],
                "description": t["description"],
                "args": t["args"],
            }
            for t in self._tools.values()
        ]

    def list_names(self):
        """Return sorted list of registered tool names."""
        return sorted(self._tools.keys())

    def __contains__(self, name):
        return name in self._tools

    def __len__(self):
        return len(self._tools)


# Global singleton
registry = ToolRegistry()
