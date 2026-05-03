"""Safe JSON extraction from LLM text responses. Never uses eval()."""

import json
import re

from utils.logger import get_logger

log = get_logger("json_parser")


def extract_json(text):
    """Extract a JSON object or array from LLM output text.

    Tries multiple strategies in order:
    1. Direct parse of the full text
    2. Extract from markdown code blocks (```json ... ```)
    3. Find first { ... } or [ ... ] with bracket matching
    """
    if not text or not isinstance(text, str):
        return None

    text = text.strip()

    # Strategy 1: Direct parse
    result = _try_parse(text)
    if result is not None:
        return result

    # Strategy 2: Markdown code block
    result = _try_code_block(text)
    if result is not None:
        return result

    # Strategy 3: Bracket matching
    result = _try_bracket_match(text)
    if result is not None:
        return result

    log.warning("Failed to extract JSON from text: %s", text[:200])
    return None


def _try_parse(text):
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _try_code_block(text):
    patterns = [
        r"```json\s*\n?(.*?)\n?\s*```",
        r"```\s*\n?(.*?)\n?\s*```",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.DOTALL)
        if match:
            result = _try_parse(match.group(1).strip())
            if result is not None:
                return result
    return None


def _try_bracket_match(text):
    """Find the outermost matched JSON object or array."""
    for open_char, close_char in [("{", "}"), ("[", "]")]:
        start = text.find(open_char)
        if start == -1:
            continue

        depth = 0
        in_string = False
        escape_next = False

        for i in range(start, len(text)):
            ch = text[i]

            if escape_next:
                escape_next = False
                continue

            if ch == "\\":
                if in_string:
                    escape_next = True
                continue

            if ch == '"' and not escape_next:
                in_string = not in_string
                continue

            if in_string:
                continue

            if ch == open_char:
                depth += 1
            elif ch == close_char:
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    result = _try_parse(candidate)
                    if result is not None:
                        return result
                    break
    return None
