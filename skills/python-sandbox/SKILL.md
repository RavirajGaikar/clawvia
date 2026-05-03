---
name: python-sandbox
description: When to use code_execute vs run_command for Python tasks
triggers: calculate, compute, run python, data, json, math
---

When the user asks you to calculate, process data, or run Python code:
- Use `code_execute` for pure computation: math, string ops, list processing, JSON parsing, data analysis
- Use `run_command` only for installing packages (`pip install`) or running existing scripts (`python script.py`)
- `code_execute` is sandboxed: subprocess, ctypes, shutil, and signal modules are blocked
- For multi-line code, use proper indentation and newlines
- Always print() your results — only stdout is captured
- If code needs external packages, first install via `run_command("pip install X")`, then use `code_execute`
- For file I/O in code, prefer the dedicated `read_file`/`write_file` tools instead
- Timeout defaults to 30s, max 60s — keep computations reasonable
