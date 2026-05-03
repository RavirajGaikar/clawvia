"""System command execution tools with safety measures."""

import shlex
import subprocess

from tools.registry import registry
from utils.logger import get_logger

log = get_logger("tools.system")

_CMD_TIMEOUT = 30  # seconds (default)
_CMD_MAX_TIMEOUT = 120  # max allowed timeout
_MAX_OUTPUT = 6000  # chars

# Shell metacharacters that indicate command chaining/injection
_SHELL_METACHARACTERS = [";", "||", "&&", "|", "`", "$(", "${", ">", ">>", "<", "\n", "\r"]

# Commands that should never be run
_BLOCKED_PATTERNS = [
    "rm -rf /",
    "mkfs.",
    "> /dev/sd",
    "dd if=",
    ":(){",
    "fork bomb",
    "chmod 777 /",
    "chown root",
    "/dev/null",
    "wget",  # downloading arbitrary files is risky
    "curl -o",  # saving downloaded content
    "curl --output",
]

# Commands that require user approval before execution
_APPROVAL_PATTERNS = [
    "rm -r",
    "rm -f",
    "rmdir",
    "apt install",
    "apt remove",
    "apt purge",
    "pkg install",
    "pkg uninstall",
    "pip install",
    "pip uninstall",
    "chmod",
    "chown",
    "kill ",
    "killall",
    "reboot",
    "shutdown",
    "poweroff",
    "systemctl",
    "service ",
    "crontab",
    "passwd",
    "useradd",
    "userdel",
    "mount",
    "umount",
    "iptables",
]


def needs_approval(cmd):
    """Check if a command requires user approval."""
    cmd_lower = cmd.lower().strip()
    for pattern in _APPROVAL_PATTERNS:
        if pattern in cmd_lower:
            return True
    return False


@registry.register(
    "run_command",
    "Run a shell command and return the output. Use for system tasks, package management, etc. "
    "Optional timeout (5-120s, default 30) for long-running commands like apt update.",
    {"cmd": "string (shell command)", "timeout": "integer? (seconds, default 30, max 120)"},
)
def run_command(cmd, timeout=None):
    if not cmd or not cmd.strip():
        return "ERROR: Empty command"

    # Normalize: strip whitespace, collapse spaces
    cmd_normalized = " ".join(cmd.strip().split())
    cmd_lower = cmd_normalized.lower()

    # Block shell metacharacters (prevents command chaining/injection)
    for meta in _SHELL_METACHARACTERS:
        if meta in cmd:
            log.warning("Blocked shell metacharacter '%s' in: %s", meta, cmd[:100])
            return (
                f"ERROR: Command contains blocked character '{meta}'. "
                f"Only single commands are allowed (no chaining, pipes, or redirects)."
            )

    for pattern in _BLOCKED_PATTERNS:
        if pattern in cmd_lower:
            log.warning("Blocked dangerous command: %s", cmd)
            return f"ERROR: Command blocked for safety: contains '{pattern}'"

    # Check if approval is needed
    if needs_approval(cmd):
        return f"APPROVAL_REQUIRED: This command needs user approval before executing: {cmd}"

    # Resolve timeout
    effective_timeout = _CMD_TIMEOUT
    if timeout is not None:
        try:
            effective_timeout = max(5, min(int(timeout), _CMD_MAX_TIMEOUT))
        except (TypeError, ValueError):
            pass

    return _execute_command(cmd, effective_timeout)


def _execute_command(cmd, timeout=None):
    """Actually execute a shell command. Called directly for approved commands."""
    effective_timeout = timeout or _CMD_TIMEOUT
    try:
        log.info("Executing: %s", cmd)
        # Use shlex.split for safe argument parsing (no shell injection)
        try:
            args = shlex.split(cmd)
        except ValueError as exc:
            return f"ERROR: Invalid command syntax: {exc}"

        result = subprocess.run(
            args,
            shell=False,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
        )

        output = result.stdout
        if result.stderr:
            output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr

        if not output.strip():
            output = f"(command exited with code {result.returncode})"

        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + f"\n... (truncated, {len(output)} total chars)"

        # Audit log
        try:
            from memory.db import audit_log_event
            audit_log_event(
                "command_exec", "run_command",
                cmd[:200], output[:200],
            )
        except Exception:
            pass

        return output

    except subprocess.TimeoutExpired:
        log.warning("Command timed out: %s", cmd)
        return f"ERROR: Command timed out after {effective_timeout}s"
    except Exception as exc:
        log.error("Command failed: %s — %s", cmd, exc)
        return f"ERROR: {exc}"