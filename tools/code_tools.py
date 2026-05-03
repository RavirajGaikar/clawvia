"""Python code execution tool — sandboxed subprocess for running Python code."""

import os
import subprocess
import tempfile

from tools.registry import registry
from utils.logger import get_logger

log = get_logger("tools.code")

_CODE_TIMEOUT = 30  # seconds
_CODE_MAX_TIMEOUT = 120  # max allowed
_MAX_OUTPUT = 8000  # chars

# Imports/patterns that are blocked inside the sandbox
_BLOCKED_IMPORTS = [
    # System access
    "shutil.rmtree",
    "os.system",
    "os.popen",
    "os.exec",
    "os.spawn",
    "os.remove",
    "os.unlink",
    "os.rmdir",
    "subprocess",
    "importlib",
    "ctypes",
    "signal",
    # Code execution escape vectors
    "exec(",
    "eval(",
    "compile(",
    "getattr(",
    "setattr(",
    "delattr(",
    "__import__",
    "__builtins__",
    "__class__",
    "__subclasses__",
    "__globals__",
    "__code__",
    "sys.modules",
    "globals()",
    "locals()",
    "vars(",
    "breakpoint(",
    "open(",
    # Network access
    "socket",
    "urllib",
    "requests",
    "http.client",
]

# Wrapper that restricts dangerous operations
_SANDBOX_WRAPPER = '''
import sys
import io

# Block dangerous modules at import level
_original_import = __builtins__.__import__ if hasattr(__builtins__, '__import__') else __import__
_BLOCKED = frozenset(["subprocess", "ctypes", "importlib", "signal", "shutil"])

def _safe_import(name, *args, **kwargs):
    if name in _BLOCKED:
        raise ImportError(f"Import of '{name}' is blocked in sandbox")
    return _original_import(name, *args, **kwargs)

try:
    __builtins__.__import__ = _safe_import
except AttributeError:
    pass

# Capture output
_captured = io.StringIO()
sys.stdout = _captured
sys.stderr = _captured

try:
{user_code}
except Exception as _exc:
    print(f"Error: {{type(_exc).__name__}}: {{_exc}}")
finally:
    sys.stdout = sys.__stdout__
    sys.stderr = sys.__stderr__
    _output = _captured.getvalue()
    if _output:
        print(_output, end="")
'''


def _check_code_safety(code):
    """Pre-check code for obviously dangerous patterns."""
    code_lower = code.lower()
    for pattern in _BLOCKED_IMPORTS:
        if pattern.lower() in code_lower:
            return f"Blocked: code contains restricted pattern '{pattern}'"
    return None


@registry.register(
    "code_execute",
    "Execute Python code in a sandboxed environment. Returns stdout/stderr output. "
    "Use for calculations, data processing, string manipulation, etc. "
    "Dangerous operations (subprocess, file deletion, ctypes) are blocked.",
    {"code": "string (Python code to execute)", "timeout": "integer? (seconds, default 30, max 120)"},
)
def code_execute(code, timeout=None):
    if not code or not code.strip():
        return "ERROR: No code provided"

    # Pre-check for dangerous patterns
    safety_issue = _check_code_safety(code)
    if safety_issue:
        return f"ERROR: {safety_issue}"

    timeout = min(max(int(timeout), 5), _CODE_MAX_TIMEOUT) if timeout else _CODE_TIMEOUT

    # Indent user code for the wrapper
    indented_code = "\n".join("    " + line for line in code.split("\n"))
    full_code = _SANDBOX_WRAPPER.replace("{user_code}", indented_code)

    # Write to temp file and execute
    tmp_file = None
    try:
        tmp_file = tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        )
        tmp_file.write(full_code)
        tmp_file.close()

        log.info("Executing Python code (%d chars, timeout=%ds)", len(code), timeout)

        result = subprocess.run(
            ["python3", "-I", tmp_file.name],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={"PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp"},
        )

        output = result.stdout
        if result.stderr:
            output += ("\n--- stderr ---\n" + result.stderr) if output else result.stderr

        if not output.strip():
            output = "(code executed successfully, no output)"

        if len(output) > _MAX_OUTPUT:
            output = output[:_MAX_OUTPUT] + f"\n... (truncated, {len(output)} total chars)"

        return output

    except subprocess.TimeoutExpired:
        log.warning("Python code execution timed out after %ds", timeout)
        return f"ERROR: Code execution timed out after {timeout}s"
    except Exception as exc:
        log.error("Code execution failed: %s", exc)
        return f"ERROR: {exc}"
    finally:
        if tmp_file and os.path.exists(tmp_file.name):
            try:
                os.unlink(tmp_file.name)
            except OSError:
                pass
