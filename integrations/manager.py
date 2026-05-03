"""Integration manager for ClawVia MCP servers."""

import json
import os
import re
from pathlib import Path

from integrations import node_setup
from utils.logger import get_logger

log = get_logger("integrations.manager")

_BASE_DIR = Path(__file__).resolve().parent.parent
_MCP_CONFIG = _BASE_DIR / "mcp_servers.json"
_REGISTRY_JSON = Path(__file__).parent / "registry.json"
_ENV_FILE = _BASE_DIR / ".env"

def get_registry():
    """Load the registry.json."""
    if not _REGISTRY_JSON.exists():
        return {}
    with open(_REGISTRY_JSON, "r", encoding="utf-8") as f:
        return json.load(f)

def get_installed():
    """Get the currently installed servers from mcp_servers.json."""
    if not _MCP_CONFIG.exists():
        return {}
    try:
        with open(_MCP_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
            return cfg.get("servers", {})
    except Exception:
        return {}

def install(name, auth_value):
    """Install an integration from the registry."""
    registry = get_registry()
    if name not in registry:
        return False, f"Unknown integration: {name}"
        
    spec = registry[name]
    
    # Check dependencies
    if spec.get("requires") == "nodejs":
        if not node_setup.is_node_installed():
            log.info("Node.js required for %s, installing...", name)
            if not node_setup.install_node():
                return False, "Failed to install Node.js (required for this integration)."
                
    # Update .env
    auth_config = spec.get("auth")
    if auth_config:
        env_var = auth_config["env_var"]
        # Very simple `.env` updater - just appends or replaces
        env_content = ""
        if _ENV_FILE.exists():
            env_content = _ENV_FILE.read_text(encoding="utf-8")
            
        # Remove old value if exists
        lines = env_content.splitlines()
        new_lines = [line for line in lines if not line.startswith(f"{env_var}=")]
        
        # For OAuth JSON credentials, compact to a single line
        if auth_config.get("type") == "oauth":
            try:
                parsed = json.loads(auth_value)
                auth_value = json.dumps(parsed)  # single line, no extra whitespace
            except json.JSONDecodeError:
                pass

        # Double-quote the value and escape internal double-quotes and backslashes.
        # This is safe for all value types: API keys, JSON strings, tokens with
        # special characters. python-dotenv handles double-quoted .env values.
        escaped = auth_value.replace("\\", "\\\\").replace('"', '\\"')
        new_lines.append(f'{env_var}="{escaped}"')
        _ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        
    # Update mcp_servers.json
    cfg = {}
    if _MCP_CONFIG.exists():
        try:
            with open(_MCP_CONFIG, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {"servers": {}}
            
    if "servers" not in cfg:
        cfg["servers"] = {}
        
    server_entry = {
        "transport": spec["transport"],
        "command": spec["command"],
        "args": spec["args"],
    }
    
    # Pass the env var to the server
    if auth_config:
        server_entry["env"] = {auth_config["env_var"]: auth_value}
        
    cfg["servers"][name] = server_entry
    
    with open(_MCP_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        
    # We also need to clear the mcp_tools_cache.json for this server 
    # so that it gets fetched freshly
    cache_path = _BASE_DIR / "mcp_tools_cache.json"
    if cache_path.exists():
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if name in cache:
                del cache[name]
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump(cache, f, indent=2)
        except Exception:
            pass
            
    return True, f"Successfully installed {name} integration."

def remove(name):
    """Remove an integration."""
    cfg = {}
    if _MCP_CONFIG.exists():
        try:
            with open(_MCP_CONFIG, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            return False, "Config file not parseable."
            
    servers = cfg.get("servers", {})
    if name not in servers:
        return False, f"Integration '{name}' is not installed."
        
    del servers[name]
    cfg["servers"] = servers
    
    with open(_MCP_CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        
    registry = get_registry()
    if name in registry:
        auth_config = registry[name].get("auth")
        if auth_config:
            env_var = auth_config["env_var"]
            if _ENV_FILE.exists():
                env_content = _ENV_FILE.read_text(encoding="utf-8")
                lines = env_content.splitlines()
                new_lines = [line for line in lines if not line.startswith(f"{env_var}=")]
                _ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
                
    return True, f"Successfully removed {name} integration."
