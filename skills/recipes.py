"""Recipe runner — replay a JSON sequence of tool calls.

A recipe is a small JSON document that drives the existing tool registry
without going through the LLM. Use it when a workflow is stable enough
to deserve a fixed shortcut ("morning briefing", "wifi audit", "share
my location with mom").

Schema:

    {
      "name": "morning_briefing",
      "description": "Battery + weather + first 3 unread SMS, spoken aloud.",
      "vars": { "city": "Bangalore" },          // optional defaults
      "steps": [
        { "tool": "device_battery", "args": {} },
        { "tool": "web_search", "args": { "query": "weather {city}" }, "save": "weather" },
        { "tool": "sms_inbox", "args": { "limit": 3 } },
        { "tool": "notification_send", "args": { "title": "Briefing", "content": "Battery checked. Weather: {weather}" } }
      ]
    }

Features:
  * `args` values undergo `{var}` substitution from `vars` and from
    earlier steps' `save:` outputs.
  * `save: <key>` stores a step's result in `vars` (truncated for safety).
  * `if: <var>` skips the step when the named var is empty/falsy.
  * `optional: true` means a step error doesn't abort the recipe.
  * Steps that return `APPROVAL_REQUIRED:` are reported but **not**
    auto-approved — the recipe pauses and the result is surfaced.
"""

import re
from typing import Any, Dict

from tools.registry import registry
from utils.logger import get_logger

log = get_logger("skills.recipes")

_VAR_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")
_MAX_SAVE_LEN = 4000


def _substitute(value: Any, vars: Dict[str, Any]) -> Any:
    if isinstance(value, str):
        def repl(m):
            key = m.group(1)
            return str(vars.get(key, m.group(0)))
        return _VAR_PATTERN.sub(repl, value)
    if isinstance(value, list):
        return [_substitute(v, vars) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, vars) for k, v in value.items()}
    return value


def run_recipe(recipe: Dict[str, Any], overrides: Dict[str, Any] = None) -> Dict[str, Any]:
    """Execute a recipe and return a structured result.

    Returns:
      {
        "name": ...,
        "ok": bool,
        "results": [ {step, tool, status, output}, ... ],
        "vars": final vars dict
      }
    """
    if not isinstance(recipe, dict):
        return {"ok": False, "error": "recipe must be a JSON object"}
    steps = recipe.get("steps") or []
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "error": "recipe has no 'steps' list"}

    vars = dict(recipe.get("vars") or {})
    if overrides:
        vars.update(overrides)

    results = []
    overall_ok = True

    for i, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            results.append({"step": i, "status": "error", "output": "step must be an object"})
            overall_ok = False
            break

        tool_name = step.get("tool")
        if not tool_name:
            results.append({"step": i, "status": "error", "output": "missing 'tool' field"})
            overall_ok = False
            break

        # Conditional skip
        cond = step.get("if")
        if cond and not vars.get(cond):
            results.append({"step": i, "tool": tool_name, "status": "skipped", "output": f"if:{cond} not set"})
            continue

        args = _substitute(step.get("args") or {}, vars)

        try:
            output = registry.execute(tool_name, args)
        except Exception as exc:
            output = f"ERROR: {exc}"

        output_str = str(output) if output is not None else ""

        # Save output to a var if requested
        save_key = step.get("save")
        if save_key:
            vars[save_key] = output_str[:_MAX_SAVE_LEN]

        # Status detection
        if output_str.startswith("APPROVAL_REQUIRED:"):
            status = "approval_required"
            results.append({"step": i, "tool": tool_name, "status": status, "output": output_str})
            log.info("Recipe paused at step %d (approval required for %s)", i, tool_name)
            overall_ok = False
            break
        if output_str.startswith("ERROR"):
            status = "error"
        else:
            status = "ok"

        results.append({"step": i, "tool": tool_name, "status": status, "output": output_str})

        if status == "error" and not step.get("optional"):
            overall_ok = False
            break

    return {
        "name": recipe.get("name", ""),
        "description": recipe.get("description", ""),
        "ok": overall_ok,
        "results": results,
        "vars": vars,
    }


def format_result(result: Dict[str, Any], max_chars_per_step: int = 600) -> str:
    """Render a recipe result as a human-readable string."""
    lines = []
    name = result.get("name") or "(unnamed)"
    head = f"Recipe: {name}  →  {'OK' if result.get('ok') else 'INCOMPLETE'}"
    lines.append(head)
    if result.get("description"):
        lines.append(result["description"])
    lines.append("")
    for r in result.get("results", []):
        out = (r.get("output") or "").strip()
        if len(out) > max_chars_per_step:
            out = out[:max_chars_per_step] + " ..."
        lines.append(f"[{r['step']}] {r.get('tool', '?')} · {r['status']}")
        if out:
            for ln in out.splitlines():
                lines.append(f"    {ln}")
    if not result.get("ok") and result.get("results"):
        last = result["results"][-1]
        if last.get("status") == "approval_required":
            lines.append("")
            lines.append("⏸  Pending approval — reply /approve to continue this step.")
    return "\n".join(lines)
