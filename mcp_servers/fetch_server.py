#!/usr/bin/env python3
"""Lightweight MCP-compatible web-fetch server — zero external SDK.

Speaks JSON-RPC 2.0 over stdin/stdout (line-delimited), implementing just
enough of the MCP protocol for ClawVia's mcp_client.py to connect:

  initialize → tools/list → tools/call

Depends only on stdlib + `requests` (already in ClawVia's requirements).
No pydantic, no mcp SDK, no Rust toolchain — runs fine on Termux/Android.

Usage (standalone):
    python -m mcp_servers.fetch_server

Or via mcp_servers.json:
    {
      "servers": {
        "fetch": {
          "transport": "stdio",
          "command": "python",
          "args": ["-m", "mcp_servers.fetch_server"]
        }
      }
    }
"""

import json
import sys

import requests

_PROTOCOL_VERSION = "2025-06-18"
_SERVER_INFO = {"name": "clawvia-fetch", "version": "1.0"}
_MAX_RESPONSE_SIZE = 100_000  # chars — keep LLM context sane
_DEFAULT_TIMEOUT = 30
_DEFAULT_USER_AGENT = (
    "ClawVia-Fetch/1.0 (MCP; +https://github.com/yourname/ClawVia)"
)


# ─── Tools ────────────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "fetch",
        "description": (
            "Fetch a URL and return its content as text. "
            "Handles HTML (extracts readable text), JSON, and plain text. "
            "Use this to read web pages, APIs, or raw files from the internet."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch.",
                },
                "max_length": {
                    "type": "integer",
                    "description": (
                        "Maximum chars to return (default 100000). "
                        "Content is truncated with a notice if longer."
                    ),
                },
                "raw": {
                    "type": "boolean",
                    "description": (
                        "If true, return raw HTML instead of extracted text. "
                        "Default false."
                    ),
                },
            },
            "required": ["url"],
        },
    },
]


def _extract_text_from_html(html):
    """Best-effort HTML→text. Uses a simple tag-stripping approach.

    We avoid importing bs4/lxml/readability since they may not be available
    on Termux. This is intentionally simple.
    """
    import re

    # Remove script/style blocks
    html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
    # Replace block tags with newlines
    html = re.sub(r"<(br|p|div|h[1-6]|li|tr|blockquote)[^>]*/?>", "\n", html, flags=re.I)
    # Strip remaining tags
    html = re.sub(r"<[^>]+>", "", html)
    # Decode common entities
    for entity, char in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                         ("&nbsp;", " "), ("&quot;", '"'), ("&#39;", "'")]:
        html = html.replace(entity, char)
    # Collapse whitespace
    lines = [line.strip() for line in html.splitlines()]
    return "\n".join(line for line in lines if line)


def _do_fetch(url, max_length=None, raw=False):
    """Fetch a URL and return (text, is_error)."""
    if max_length is None:
        max_length = _MAX_RESPONSE_SIZE

    # Basic URL validation
    if not url or not url.startswith(("http://", "https://")):
        return "ERROR: URL must start with http:// or https://", True

    headers = {"User-Agent": _DEFAULT_USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=_DEFAULT_TIMEOUT,
                            allow_redirects=True, stream=True)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return f"ERROR: Request timed out after {_DEFAULT_TIMEOUT}s", True
    except requests.exceptions.ConnectionError as e:
        return f"ERROR: Connection failed: {e}", True
    except requests.exceptions.HTTPError as e:
        return f"ERROR: HTTP {resp.status_code}: {e}", True
    except Exception as e:
        return f"ERROR: {e}", True

    content_type = resp.headers.get("Content-Type", "")

    # Read up to a reasonable limit to avoid OOM
    body = resp.text[:max_length * 2]  # read more, then truncate after processing

    if "json" in content_type:
        # Pretty-print JSON
        try:
            parsed = json.loads(body)
            text = json.dumps(parsed, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            text = body
    elif "html" in content_type and not raw:
        text = _extract_text_from_html(body)
    else:
        text = body

    truncated = False
    if len(text) > max_length:
        text = text[:max_length]
        truncated = True

    if truncated:
        text += f"\n\n[Content truncated at {max_length} characters]"

    return text, False


def handle_call(tool_name, arguments):
    """Dispatch a tools/call request."""
    if tool_name == "fetch":
        text, is_error = _do_fetch(
            url=arguments.get("url", ""),
            max_length=arguments.get("max_length"),
            raw=arguments.get("raw", False),
        )
        result = {"content": [{"type": "text", "text": text}]}
        if is_error:
            result["isError"] = True
        return result
    else:
        raise ValueError(f"Unknown tool: {tool_name}")


# ─── JSON-RPC server loop ────────────────────────────────────────────────────

def _send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _error_response(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def _ok_response(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def main():
    """Read JSON-RPC requests from stdin, respond on stdout."""
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue

        rid = req.get("id")
        if rid is None:
            # Notification — nothing to reply
            continue

        method = req.get("method", "")

        if method == "initialize":
            _send(_ok_response(rid, {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": _SERVER_INFO,
            }))

        elif method == "tools/list":
            _send(_ok_response(rid, {"tools": TOOLS}))

        elif method == "tools/call":
            params = req.get("params") or {}
            name = params.get("name", "")
            args = params.get("arguments") or {}
            try:
                result = handle_call(name, args)
                _send(_ok_response(rid, result))
            except Exception as e:
                _send(_ok_response(rid, {
                    "isError": True,
                    "content": [{"type": "text", "text": f"ERROR: {e}"}],
                }))

        else:
            _send(_error_response(rid, -32601, f"Method not found: {method}"))


if __name__ == "__main__":
    main()
