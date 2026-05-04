"""HTTP client for the NVIDIA NIM LLM endpoint. Pure requests — no SDK."""

import requests
import time

import config
from utils.logger import get_logger

log = get_logger("llm.client")

_MAX_RETRIES = 2
_RETRY_DELAY = 2  # seconds


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
                "LLM request attempt %d/%d (model=%s, msgs=%d)",
                attempt + 1,
                _MAX_RETRIES + 1,
                config.NVIDIA_MODEL,
                len(messages),
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
            log.debug("LLM raw response keys: %s", list(data.keys()) if isinstance(data, dict) else "not a dict")
            
            # Try standard OpenAI format
            try:
                text = data["choices"][0]["message"]["content"]
            except (KeyError, IndexError, TypeError):
                # Fallback: try alternative formats
                log.warning("Standard format failed, trying alternatives. Response: %s", str(data)[:500])
                if isinstance(data, dict):
                    # Try direct content field
                    if "content" in data:
                        text = data["content"]
                    # Try text field
                    elif "text" in data:
                        text = data["text"]
                    # Try result field
                    elif "result" in data:
                        result = data["result"]
                        if isinstance(result, dict) and "text" in result:
                            text = result["text"]
                        elif isinstance(result, str):
                            text = result
                        else:
                            raise LLMError(f"Unexpected response format in 'result': {str(data)[:300]}")
                    else:
                        raise LLMError(f"No recognized content field in response: {str(data)[:300]}")
                else:
                    raise LLMError(f"Response is not a dict: {str(data)[:300]}")
            
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
