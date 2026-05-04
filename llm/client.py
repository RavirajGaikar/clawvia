"""HTTP client for the NVIDIA NIM LLM endpoint. Pure requests — no SDK."""

import requests
import time

import config
from utils.logger import get_logger

log = get_logger("llm.client")

_MAX_RETRIES = 2
_RETRY_DELAY = 2  # seconds


def _extract_text(data):
    """Extract assistant text from any known response format.

    Tries, in order:
      1. Standard OpenAI: choices[0].message.content
      2. choices[0].message.text
      3. choices[0].text  (older completions API)
      4. choices[0].content
      5. Top-level "content" key
      6. Top-level "text" key
      7. result.text or result (string)

    Returns the text string, or raises LLMError on failure.
    """
    if not isinstance(data, dict):
        raise LLMError(f"Response is not a dict: {str(data)[:300]}")

    # ── choices-based formats ──
    choices = data.get("choices")
    if choices and isinstance(choices, list) and len(choices) > 0:
        c0 = choices[0]
        if isinstance(c0, dict):
            # Standard: choices[0].message.content
            msg = c0.get("message")
            if isinstance(msg, dict):
                for key in ("content", "text"):
                    val = msg.get(key)
                    if val and isinstance(val, str):
                        return val
            # Older: choices[0].text or choices[0].content
            for key in ("text", "content"):
                val = c0.get(key)
                if val and isinstance(val, str):
                    return val

    # ── Top-level fields ──
    for key in ("content", "text", "output"):
        val = data.get(key)
        if val and isinstance(val, str):
            return val

    # ── result field ──
    result = data.get("result")
    if result:
        if isinstance(result, str):
            return result
        if isinstance(result, dict):
            for key in ("text", "content"):
                val = result.get(key)
                if val and isinstance(val, str):
                    return val

    raise LLMError(
        f"No recognized content field in response. "
        f"Keys: {list(data.keys())}. Preview: {str(data)[:400]}"
    )


def chat(messages, temperature=None, max_tokens=None):
    """Send a chat-completion request and return the assistant text.

    Args:
        messages: list of {"role": ..., "content": ...} dicts.
        temperature: optional override.
        max_tokens: optional override.

    Returns:
        The assistant's reply text (str), or raises on failure.
    """
    temperature = temperature if temperature is not None else config.LLM_TEMPERATURE
    max_tokens = max_tokens if max_tokens is not None else config.LLM_MAX_TOKENS

    headers = {
        "Authorization": f"Bearer {config.NVIDIA_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": config.NVIDIA_MODEL,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }

    last_error = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            log.debug(
                "LLM request attempt %d/%d (model=%s, msgs=%d, tokens=%d)",
                attempt + 1,
                _MAX_RETRIES + 1,
                config.NVIDIA_MODEL,
                len(messages),
                max_tokens,
            )
            resp = requests.post(
                config.NVIDIA_API_URL,
                headers=headers,
                json=payload,
                timeout=config.LLM_TIMEOUT,
            )

            if resp.status_code == 429 or resp.status_code >= 500:
                log.warning("LLM returned %d, retrying...", resp.status_code)
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                if attempt < _MAX_RETRIES:
                    time.sleep(_RETRY_DELAY * (attempt + 1))
                    continue
                raise LLMError(last_error)

            resp.raise_for_status()

            data = resp.json()
            text = _extract_text(data)
            log.debug("LLM response (%d chars)", len(text))
            return text

        except requests.exceptions.Timeout:
            last_error = "Request timed out"
            log.warning("LLM timeout (attempt %d)", attempt + 1)
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY)
                continue
            raise LLMError(last_error)

        except requests.exceptions.ConnectionError as exc:
            last_error = f"Connection error: {exc}"
            log.warning("LLM connection error (attempt %d): %s", attempt + 1, exc)
            if attempt < _MAX_RETRIES:
                time.sleep(_RETRY_DELAY)
                continue
            raise LLMError(last_error)

        except LLMError:
            raise

    raise LLMError(last_error or "Unknown LLM error")


class LLMError(Exception):
    """Raised when the LLM call fails after retries."""
