"""Image analysis via NVIDIA NIM VLM endpoint."""

import base64
import mimetypes
import requests

import config
from utils.logger import get_logger

log = get_logger("media.vision")

_DEFAULT_QUESTION = (
    "Describe this image in detail. Include all visible text, numbers, "
    "names, and key visual elements."
)


def analyze_image(image_path, question=None):
    """Analyze an image file using the configured vision model.

    Args:
        image_path: Path to the image file.
        question: Optional question about the image. Uses a generic
                  description prompt if not provided.

    Returns:
        Description/analysis text, or an error string prefixed with "ERROR:".
    """
    question = question or _DEFAULT_QUESTION

    # Read and encode the image
    try:
        with open(image_path, "rb") as f:
            image_data = f.read()
    except OSError as exc:
        return f"ERROR: Cannot read image file: {exc}"

    if not image_data:
        return "ERROR: Image file is empty"

    # Determine MIME type
    mime_type, _ = mimetypes.guess_type(image_path)
    if not mime_type or not mime_type.startswith("image/"):
        mime_type = "image/jpeg"  # Safe default for Telegram photos

    b64 = base64.b64encode(image_data).decode("ascii")
    data_url = f"data:{mime_type};base64,{b64}"

    # Build multimodal message
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]

    headers = {
        "Authorization": f"Bearer {config.NVIDIA_API_KEY}",
        "Content-Type": "application/json",
    }

    payload = {
        "model": config.VISION_MODEL,
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 1024,
    }

    try:
        resp = requests.post(
            config.NVIDIA_API_URL,
            headers=headers,
            json=payload,
            timeout=60,
        )

        if resp.status_code == 429:
            return "ERROR: Vision API rate limited. Try again in a moment."

        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        log.info("Vision analysis complete (%d chars)", len(text))
        return text.strip()

    except requests.exceptions.Timeout:
        return "ERROR: Vision API request timed out."
    except requests.exceptions.RequestException as exc:
        log.error("Vision API error: %s", exc)
        return f"ERROR: Vision API request failed: {exc}"
    except (KeyError, IndexError) as exc:
        log.error("Vision API unexpected response: %s", exc)
        return "ERROR: Unexpected response from vision API."
