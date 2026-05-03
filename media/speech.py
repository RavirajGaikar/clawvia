"""Speech-to-text via Groq Whisper API."""

import os
import requests

import config
from utils.logger import get_logger

log = get_logger("media.speech")

_GROQ_STT_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_GROQ_MODEL = "whisper-large-v3-turbo"


def transcribe_audio(audio_path):
    """Transcribe an audio file using Groq Whisper.

    Args:
        audio_path: Path to the audio file (OGG/Opus, MP3, WAV, etc.)

    Returns:
        Transcribed text, or an error string prefixed with "ERROR:".
    """
    if not config.GROQ_API_KEY:
        return "ERROR: Voice transcription unavailable — GROQ_API_KEY not configured."

    if not os.path.isfile(audio_path):
        return f"ERROR: Audio file not found: {audio_path}"

    file_size = os.path.getsize(audio_path)
    if file_size == 0:
        return "ERROR: Audio file is empty."
    if file_size > 25 * 1024 * 1024:  # Groq limit: 25MB
        return "ERROR: Audio file too large (max 25MB)."

    headers = {
        "Authorization": f"Bearer {config.GROQ_API_KEY}",
    }

    try:
        with open(audio_path, "rb") as f:
            files = {"file": (os.path.basename(audio_path), f)}
            data = {"model": _GROQ_MODEL}

            resp = requests.post(
                _GROQ_STT_URL,
                headers=headers,
                files=files,
                data=data,
                timeout=60,
            )

        if resp.status_code == 429:
            return "ERROR: Groq rate limited. Try again in a moment."

        resp.raise_for_status()
        result = resp.json()
        text = result.get("text", "").strip()

        if not text:
            return "ERROR: No speech detected in audio."

        log.info("Transcribed audio (%d chars)", len(text))
        return text

    except requests.exceptions.Timeout:
        return "ERROR: Transcription request timed out."
    except requests.exceptions.RequestException as exc:
        log.error("Groq STT error: %s", exc)
        return f"ERROR: Transcription failed: {exc}"
    except (KeyError, ValueError) as exc:
        log.error("Groq STT unexpected response: %s", exc)
        return "ERROR: Unexpected response from transcription API."
