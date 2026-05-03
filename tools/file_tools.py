"""File operation tools with path sanitization."""

import os

import config
from tools.registry import registry
from utils.logger import get_logger

log = get_logger("tools.file")


def _safe_path(filename):
    """Resolve filename under BASE_PATH, preventing path traversal."""
    base = os.path.realpath(config.BASE_PATH)
    full = os.path.realpath(os.path.join(base, filename))
    if not full.startswith(base):
        raise ValueError(f"Path traversal blocked: {filename}")
    return full


@registry.register(
    "list_files",
    "List files and folders in a directory. Defaults to the base downloads folder.",
    {"path": "string (optional, relative subdirectory)"},
)
def list_files(path=""):
    target = _safe_path(path) if path else os.path.realpath(config.BASE_PATH)
    if not os.path.isdir(target):
        return f"ERROR: Not a directory: {path}"
    entries = os.listdir(target)
    if not entries:
        return "(empty directory)"
    return "\n".join(sorted(entries))


@registry.register(
    "read_file",
    "Read the contents of a file. Returns the text content.",
    {"filename": "string (relative path)"},
)
def read_file(filename):
    full = _safe_path(filename)
    if not os.path.isfile(full):
        return f"ERROR: File not found: {filename}"
    with open(full, "r", encoding="utf-8", errors="replace") as f:
        content = f.read(50000)  # cap at 50KB to avoid context bloat
    if len(content) >= 50000:
        content += "\n... (truncated at 50KB)"
    return content


@registry.register(
    "write_file",
    "Write content to a file. Creates or overwrites the file.",
    {"filename": "string (relative path)", "content": "string"},
)
def write_file(filename, content):
    full = _safe_path(filename)
    parent = os.path.dirname(full)
    if not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(content)
    log.info("Wrote %d chars to %s", len(content), filename)
    return f"Written {len(content)} chars to {filename}"


@registry.register(
    "delete_file",
    "Delete a file.",
    {"filename": "string (relative path)"},
)
def delete_file(filename):
    full = _safe_path(filename)
    if not os.path.exists(full):
        return f"ERROR: File not found: {filename}"
    if os.path.isdir(full):
        return f"ERROR: '{filename}' is a directory, not a file"
    os.remove(full)
    log.info("Deleted %s", filename)
    return f"Deleted {filename}"