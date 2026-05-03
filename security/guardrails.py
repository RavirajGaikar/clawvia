"""Security guardrails — prompt injection detection, input/output sanitization."""

import base64
import re
import unicodedata

from utils.logger import get_logger

log = get_logger("security")

# ── Prompt Injection Detection ────────────────────────────────────────────

# Patterns that indicate prompt injection attempts
_INJECTION_PATTERNS = [
    # Direct role override attempts
    r"(?i)ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?|rules?|guidelines?)",
    r"(?i)disregard\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|prompts?)",
    r"(?i)forget\s+(all\s+)?(previous|prior|your)\s+(instructions?|prompts?|rules?|programming)",
    r"(?i)override\s+(all\s+)?(safety|security|system)\s*(rules?|filters?|instructions?|prompts?)",
    # Role-play / identity override
    r"(?i)you\s+are\s+now\s+(a|an|the)\s+",
    r"(?i)pretend\s+(you\s+are|to\s+be)\s+",
    r"(?i)act\s+as\s+(a|an|if)\s+",
    r"(?i)switch\s+to\s+(a|an)\s+",
    r"(?i)your\s+new\s+(role|identity|persona|purpose)\s+is",
    # System prompt extraction
    r"(?i)reveal\s+(your|the|system)\s+(system\s+)?prompt",
    r"(?i)show\s+me\s+(your|the)\s+(system\s+)?(prompt|instructions)",
    r"(?i)what\s+(are|is)\s+your\s+(system\s+)?(prompt|instructions|rules)",
    r"(?i)print\s+(your|the)\s+(system\s+)?(prompt|instructions)",
    r"(?i)repeat\s+(your|the)\s+(system\s+)?(prompt|instructions)\s+verbatim",
    # JSON injection / tool manipulation
    r'(?i)\{\s*"tool"\s*:\s*"',
    r'(?i)\{\s*"action"\s*:\s*"',
    r"(?i)respond\s+with\s+(only\s+)?json",
    # Developer mode / jailbreak
    r"(?i)(enable|activate|enter)\s+(developer|dev|admin|god|sudo)\s*(mode)?",
    r"(?i)DAN\s*(mode)?",
    r"(?i)jailbreak",
    # Delimiter injection (trying to close system prompt)
    r"(?i)</?system>",
    r"(?i)\[/?SYSTEM\]",
    r"(?i)```\s*system",
    r"(?i)---\s*END\s*(OF\s+)?(SYSTEM|INSTRUCTIONS)",
]

_COMPILED_INJECTION = [re.compile(p) for p in _INJECTION_PATTERNS]

# Suspicious encoded content patterns
_ENCODING_PATTERNS = [
    # Long Base64 strings (>50 chars of base64 alphabet with no spaces)
    r"[A-Za-z0-9+/]{50,}={0,2}",
    # Hex-encoded strings
    r"(?:\\x[0-9a-fA-F]{2}){10,}",
    # Unicode escape sequences
    r"(?:\\u[0-9a-fA-F]{4}){5,}",
    # URL-encoded payloads
    r"(?:%[0-9a-fA-F]{2}){10,}",
]

_COMPILED_ENCODING = [re.compile(p) for p in _ENCODING_PATTERNS]

# ── Tool output injection patterns (what shouldn't appear in tool results) ─

_OUTPUT_INJECTION_PATTERNS = [
    r"(?i)ignore\s+(all\s+)?previous\s+instructions",
    r"(?i)you\s+are\s+now\s+",
    r"(?i)new\s+instructions?:\s*",
    r"(?i)system\s*:\s*you\s+(must|should|are)",
    r'(?i)\{\s*"tool"\s*:\s*"(run_command|delete_file|write_file)',
    r"(?i)IMPORTANT:\s*(ignore|override|disregard)",
    r"(?i)\[INST\]",
    r"(?i)<\|im_start\|>system",
]

_COMPILED_OUTPUT_INJECTION = [re.compile(p) for p in _OUTPUT_INJECTION_PATTERNS]


# ── Public API ────────────────────────────────────────────────────────────

def check_input(text):
    """Check user input for prompt injection attempts.

    Returns:
        None if input is safe.
        A warning string describing the detected threat if suspicious.
    """
    if not text:
        return None

    # Normalize unicode (catch homoglyph/invisible char tricks)
    normalized = unicodedata.normalize("NFKC", text)

    # Check for injection patterns
    for pattern in _COMPILED_INJECTION:
        match = pattern.search(normalized)
        if match:
            log.warning("Prompt injection detected: '%s' in input: %s",
                       match.group()[:50], text[:100])
            return f"Suspicious input detected: possible prompt injection"

    # Check for encoded payloads that might hide malicious instructions
    encoding_threat = _check_encoded_content(normalized)
    if encoding_threat:
        return encoding_threat

    # Check for multi-language obfuscation
    if _has_excessive_script_mixing(normalized):
        log.warning("Script mixing detected in input: %s", text[:100])
        return "Suspicious input: unusual character mixing detected"

    return None


def sanitize_tool_output(output, tool_name="unknown"):
    """Sanitize tool output before feeding it back to the agent.

    Strips or flags content that looks like prompt injection attempts
    embedded in tool results (e.g., from web pages, file contents).

    Returns:
        The sanitized output string.
    """
    if not output or not isinstance(output, str):
        return output or ""

    flagged = False
    for pattern in _COMPILED_OUTPUT_INJECTION:
        match = pattern.search(output)
        if match:
            log.warning("Injection in tool output (%s): '%s'",
                       tool_name, match.group()[:60])
            flagged = True
            # Replace the suspicious section with a warning marker
            output = pattern.sub("[CONTENT_FILTERED]", output)

    if flagged:
        output = (
            "[⚠️ Some content was filtered for security. "
            "Tool output may have contained injection attempts.]\n\n"
            + output
        )

    return output


def _check_encoded_content(text):
    """Detect potentially malicious encoded content."""
    for pattern in _COMPILED_ENCODING:
        match = pattern.search(text)
        if match:
            encoded = match.group()
            # Try to decode Base64 and check if it contains injection
            if len(encoded) >= 50 and _is_valid_base64(encoded):
                try:
                    decoded = base64.b64decode(encoded).decode("utf-8", errors="ignore")
                    # Check if decoded content contains injection patterns
                    for inj_pattern in _COMPILED_INJECTION:
                        if inj_pattern.search(decoded):
                            log.warning("Encoded injection found: %s", decoded[:100])
                            return "Suspicious input: encoded content contains hidden instructions"
                except Exception:
                    pass
    return None


def _is_valid_base64(text):
    """Check if a string is valid Base64."""
    try:
        # Must be valid base64 and decode to something meaningful
        decoded = base64.b64decode(text)
        # Check if at least 70% printable ASCII
        printable = sum(1 for b in decoded if 32 <= b <= 126)
        return printable / max(len(decoded), 1) > 0.7
    except Exception:
        return False


def _has_excessive_script_mixing(text):
    """Detect text that mixes many Unicode scripts — common obfuscation technique."""
    if len(text) < 20:
        return False

    scripts = set()
    letter_count = 0

    for char in text:
        if unicodedata.category(char).startswith("L"):  # Letter
            letter_count += 1
            try:
                script = unicodedata.name(char, "").split()[0]
                scripts.add(script)
            except (ValueError, IndexError):
                pass

    # If we have many different scripts relative to text length, it's suspicious
    # Normal text: 1-2 scripts (Latin, maybe CJK). Obfuscation: 4+ scripts
    if letter_count > 10 and len(scripts) >= 4:
        return True

    return False
