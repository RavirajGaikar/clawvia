"""Lightweight MCP (Model Context Protocol) client.

Hand-rolled JSON-RPC 2.0 over either:
  * stdio  — spawn a server as a subprocess, exchange line-delimited JSON
  * http   — POST JSON-RPC requests to a URL (basic Streamable HTTP)

No `mcp` SDK, no `pydantic`, no `anyio`, no `httpx`. Stdlib + the existing
`requests` dependency only. Aimed at Termux: spawn a few Python-based
reference servers (fetch, git, sequential-thinking) and expose their tools
as ClawVia tools prefixed `mcp_<server>_<tool>`.

Config file (default: `mcp_servers.json` in repo root) — JSON shape:

    {
      "servers": {
        "fetch": {
          "transport": "stdio",
          "command": "python",
          "args": ["-m", "mcp_server_fetch"],
          "env": {}
        },
        "remote": {
          "transport": "http",
          "url": "http://127.0.0.1:8765/mcp",
          "headers": {"Authorization": "Bearer ..."}
        }
      }
    }

If the file is missing or empty, no MCP servers are loaded and ClawVia
runs as before. Servers that fail to start are logged and skipped.
"""

import atexit
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import requests

import config
from tools.registry import registry
from utils.logger import get_logger

log = get_logger("tools.mcp")

# Spec versions we'll claim during initialize.
_PROTOCOL_VERSION = "2025-06-18"

# How long to wait for any single JSON-RPC reply.
_DEFAULT_TIMEOUT_SEC = 30

# All live clients, mapped by server name.
_CLIENTS = {}
_CLIENT_CONFIGS = {}
_LAST_USED = {}

# Idle timeout for MCP servers (shut down after 10 mins of inactivity)
_IDLE_TIMEOUT_SEC = 600

# Task-active flag: when True, idle watchdog skips shutdown.
# Set by agent.py during active task execution.
_task_active = False
_task_active_lock = threading.Lock()


def set_task_active(active):
    """Signal whether an agent task is actively running.

    While active, the idle watchdog will not shut down MCP servers.
    """
    global _task_active
    with _task_active_lock:
        _task_active = active


# ─── Stdio transport ─────────────────────────────────────────────────────────

class MCPStdioClient:
    """JSON-RPC 2.0 over a subprocess's stdin/stdout, line-delimited."""

    def __init__(self, name, command, args=None, env=None, cwd=None):
        self.name = name
        self.command = command
        self.args = list(args or [])
        self.env = env
        self.cwd = cwd
        self._proc = None
        self._req_id = 0
        self._lock = threading.Lock()
        self._stderr_thread = None

    def start(self):
        log.info("Spawning MCP stdio server '%s': %s %s",
                 self.name, self.command, " ".join(self.args))
        full_env = dict(os.environ)
        if self.env:
            full_env.update(self.env)
        self._proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=full_env,
            cwd=self.cwd,
            text=True,
            bufsize=1,  # line-buffered
        )
        # Drain stderr in a background thread so the server doesn't block on it.
        self._stderr_thread = threading.Thread(
            target=self._drain_stderr, daemon=True
        )
        self._stderr_thread.start()
        # Handshake.
        self._initialize()

    def _drain_stderr(self):
        try:
            for line in self._proc.stderr:
                if line.strip():
                    log.debug("[%s stderr] %s", self.name, line.rstrip())
        except Exception:
            pass

    def _next_id(self):
        self._req_id += 1
        return self._req_id

    def _send(self, payload):
        line = json.dumps(payload) + "\n"
        self._proc.stdin.write(line)
        self._proc.stdin.flush()

    def _read_reply(self, expected_id, timeout):
        """Read JSON lines until we get one matching expected_id."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self._proc.stdout.readline()
            if not line:
                # Pipe closed.
                rc = self._proc.poll()
                raise RuntimeError(
                    f"MCP server '{self.name}' exited (rc={rc}) before reply"
                )
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                log.debug("[%s] non-JSON line ignored: %s", self.name, line[:200])
                continue
            # Skip notifications and unrelated responses.
            if msg.get("id") != expected_id:
                continue
            return msg
        raise TimeoutError(f"MCP server '{self.name}' timed out (req {expected_id})")

    def _request(self, method, params=None, timeout=_DEFAULT_TIMEOUT_SEC):
        with self._lock:
            req_id = self._next_id()
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
            }
            if params is not None:
                payload["params"] = params
            self._send(payload)
            reply = self._read_reply(req_id, timeout)
        if "error" in reply:
            err = reply["error"]
            raise RuntimeError(
                f"MCP error {err.get('code')}: {err.get('message', 'unknown')}"
            )
        return reply.get("result", {})

    def _notify(self, method, params=None):
        with self._lock:
            payload = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            self._send(payload)

    def _initialize(self):
        result = self._request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "clawvia", "version": "0.1"},
        }, timeout=15)
        log.info("MCP '%s' initialized: server=%s",
                 self.name, result.get("serverInfo", {}))
        # Per spec: send 'initialized' notification before any other request.
        self._notify("notifications/initialized")

    def list_tools(self):
        result = self._request("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, tool_name, arguments):
        return self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        }, timeout=_DEFAULT_TIMEOUT_SEC)

    def shutdown(self):
        if not self._proc:
            return
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        log.info("MCP '%s' shut down", self.name)


# ─── HTTP transport ──────────────────────────────────────────────────────────

class MCPHttpClient:
    """JSON-RPC 2.0 POSTed to a single URL.

    This covers the simplest "Streamable HTTP" servers that respond with a
    single JSON body per request. Servers that require SSE or session IDs
    aren't supported here — keeping things lightweight.
    """

    def __init__(self, name, url, headers=None, timeout=_DEFAULT_TIMEOUT_SEC):
        self.name = name
        self.url = url
        self.headers = dict(headers or {})
        self.headers.setdefault("Content-Type", "application/json")
        self.headers.setdefault("Accept", "application/json")
        self.timeout = timeout
        self._req_id = 0
        self._lock = threading.Lock()

    def start(self):
        log.info("Connecting MCP HTTP server '%s' at %s", self.name, self.url)
        self._initialize()

    def _next_id(self):
        self._req_id += 1
        return self._req_id

    def _request(self, method, params=None, timeout=None):
        with self._lock:
            req_id = self._next_id()
            payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                payload["params"] = params
            resp = requests.post(
                self.url,
                headers=self.headers,
                json=payload,
                timeout=timeout or self.timeout,
            )
        resp.raise_for_status()
        msg = resp.json()
        if "error" in msg:
            err = msg["error"]
            raise RuntimeError(
                f"MCP error {err.get('code')}: {err.get('message', 'unknown')}"
            )
        return msg.get("result", {})

    def _notify(self, method, params=None):
        with self._lock:
            payload = {"jsonrpc": "2.0", "method": method}
            if params is not None:
                payload["params"] = params
            try:
                requests.post(
                    self.url, headers=self.headers,
                    json=payload, timeout=self.timeout,
                )
            except Exception as exc:
                log.debug("[%s] notify '%s' failed: %s", self.name, method, exc)

    def _initialize(self):
        result = self._request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "clawvia", "version": "0.1"},
        }, timeout=15)
        log.info("MCP '%s' initialized: server=%s",
                 self.name, result.get("serverInfo", {}))
        self._notify("notifications/initialized")

    def list_tools(self):
        result = self._request("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, tool_name, arguments):
        return self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments or {},
        })

    def shutdown(self):
        # Stateless POST, nothing to do.
        pass


# ─── Result formatting ───────────────────────────────────────────────────────

def _format_tool_result(result):
    """Flatten an MCP tools/call result into a string ClawVia can show.

    MCP returns: {"content": [{"type":"text","text":"..."}, ...], "isError": bool}
    """
    if not isinstance(result, dict):
        return str(result)
    if result.get("isError"):
        prefix = "ERROR: "
    else:
        prefix = ""
    content = result.get("content")
    if not content:
        return (prefix + "OK (no content)").strip()
    parts = []
    for item in content:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        t = item.get("type")
        if t == "text":
            parts.append(item.get("text", ""))
        elif t == "image":
            parts.append("[image: %s, %d bytes b64]" % (
                item.get("mimeType", "?"),
                len(item.get("data", "") or ""),
            ))
        elif t == "resource":
            res = item.get("resource", {})
            parts.append(f"[resource: {res.get('uri', '?')}]")
        else:
            parts.append(json.dumps(item)[:500])
    return prefix + "\n".join(p for p in parts if p)


# ─── Loader ──────────────────────────────────────────────────────────────────

def _register_cached_tools(server_name, tools):
    """Register tools from a cached list without starting the server."""
    count = 0
    for tool in tools:
        tname = tool.get("name")
        if not tname:
            continue
        full_name = f"mcp_{server_name}_{tname}"
        desc = tool.get("description") or f"MCP tool '{tname}' from server '{server_name}'"
        
        schema = tool.get("inputSchema") or {}
        props = {}
        if isinstance(schema, dict):
            for pname, pdef in (schema.get("properties") or {}).items():
                if isinstance(pdef, dict):
                    props[pname] = pdef.get("type", "any")
                else:
                    props[pname] = "any"

        # Bind the current values into the closure.
        def _make_handler(_server_name, _tname):
            def _handler(**kwargs):
                client = _get_or_start_client(_server_name)
                if not client:
                    return f"ERROR: MCP server '{_server_name}' failed to start."
                
                # Update last used time
                _LAST_USED[_server_name] = time.time()
                
                try:
                    raw = client.call_tool(_tname, kwargs)
                except Exception as exc:
                    return f"ERROR: MCP call failed: {exc}"
                return _format_tool_result(raw)
            return _handler

        registry.register_func(
            full_name,
            _make_handler(server_name, tname),
            description=desc[:500],
            args_schema=props,
            cacheable=False,
        )
        count += 1
    log.info("MCP '%s' registered %d tools (on-demand)", server_name, count)
    return count

def _get_or_start_client(name):
    """Get an active client by name, or start it if it isn't running."""
    if name in _CLIENTS:
        return _CLIENTS[name]
        
    entry = _CLIENT_CONFIGS.get(name)
    if not entry:
        return None
        
    transport = (entry.get("transport") or "stdio").lower()
    try:
        if transport == "stdio":
            client = MCPStdioClient(
                name=name,
                command=entry["command"],
                args=entry.get("args"),
                env=entry.get("env"),
                cwd=entry.get("cwd"),
            )
        elif transport == "http":
            client = MCPHttpClient(
                name=name,
                url=entry["url"],
                headers=entry.get("headers"),
                timeout=entry.get("timeout", _DEFAULT_TIMEOUT_SEC),
            )
        else:
            log.warning("MCP '%s' unknown transport: %s", name, transport)
            return None
            
        client.start()
        _CLIENTS[name] = client
        _LAST_USED[name] = time.time()
        return client
    except Exception as exc:
        log.error("MCP '%s' failed to start: %s", name, exc)
        return None


def load_mcp_servers(config_path=None):
    """Read mcp_servers.json, populate configs, and register tools from cache.
    Starts servers temporarily if they aren't in the cache to discover tools.
    """
    base_dir = Path(__file__).resolve().parent.parent
    if config_path is None:
        config_path = os.environ.get("MCP_CONFIG_PATH", str(base_dir / "mcp_servers.json"))

    path = Path(config_path)
    if not path.exists():
        log.info("No MCP config at %s — skipping MCP setup", path)
        return 0

    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not parse %s: %s", path, exc)
        return 0

    servers = cfg.get("servers") or {}
    if not servers:
        log.info("MCP config has no servers")
        return 0
        
    cache_path = base_dir / "mcp_tools_cache.json"
    tool_cache = {}
    if cache_path.exists():
        try:
            tool_cache = json.loads(cache_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    total = 0
    cache_updated = False
    
    for name, entry in servers.items():
        if entry.get("disabled"):
            continue
            
        # Store config for on-demand starting
        _CLIENT_CONFIGS[name] = entry
        
        # Check if we have cached tools for this server
        tools = tool_cache.get(name)
        
        if tools is None:
            # First time seeing this server, or cache missing. Must start to discover tools.
            log.info("No tool cache for MCP '%s', starting temporarily to discover tools...", name)
            client = _get_or_start_client(name)
            if client:
                try:
                    tools = client.list_tools()
                    tool_cache[name] = tools
                    cache_updated = True
                except Exception as exc:
                    log.warning("MCP '%s' tools/list failed: %s", name, exc)
                    
        if tools:
            total += _register_cached_tools(name, tools)

    if cache_updated:
        try:
            cache_path.write_text(json.dumps(tool_cache, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("Failed to write MCP tools cache: %s", exc)

    if _CLIENT_CONFIGS:
        # Start the idle watchdog thread
        threading.Thread(target=_idle_watchdog, daemon=True).start()
        atexit.register(_shutdown_all)
        
    return total

def _idle_watchdog():
    """Periodically shut down MCP servers that haven't been used recently.

    Skips shutdown entirely while an agent task is actively running.
    """
    while True:
        time.sleep(60)
        # Don't shut down servers while a task is in progress
        with _task_active_lock:
            if _task_active:
                continue
        now = time.time()
        to_shutdown = []
        for name, last_used in list(_LAST_USED.items()):
            if name in _CLIENTS and (now - last_used > _IDLE_TIMEOUT_SEC):
                to_shutdown.append(name)
                
        for name in to_shutdown:
            log.info("Shutting down idle MCP server: '%s'", name)
            client = _CLIENTS.pop(name, None)
            _LAST_USED.pop(name, None)
            if client:
                try:
                    client.shutdown()
                except Exception:
                    pass

def _shutdown_all():
    for name, c in list(_CLIENTS.items()):
        try:
            c.shutdown()
        except Exception:
            pass
    _CLIENTS.clear()
    _LAST_USED.clear()
