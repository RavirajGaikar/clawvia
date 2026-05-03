"""Node.js installer for MCP integrations on Termux."""

import subprocess
from utils.logger import get_logger

log = get_logger("integrations.node")

def is_node_installed():
    """Check if node and npx are available."""
    try:
        subprocess.run(["node", "-v"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        subprocess.run(["npx", "-v"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def install_node():
    """Install Node.js silently via pkg."""
    if is_node_installed():
        return True
        
    log.info("Installing Node.js via pkg...")
    try:
        subprocess.run(
            ["pkg", "install", "-y", "nodejs"], 
            stdout=subprocess.DEVNULL, 
            stderr=subprocess.DEVNULL, 
            check=True
        )
        return is_node_installed()
    except Exception as exc:
        log.error("Failed to install Node.js: %s", exc)
        return False
