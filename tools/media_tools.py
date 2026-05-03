"""Media tools — gives the agent ability to analyze images on demand."""

import os

import config
from media.vision import analyze_image as _analyze
from tools.registry import registry
from utils.logger import get_logger

log = get_logger("tools.media")


@registry.register(
    "analyze_image",
    "Analyze an image file with a vision model. Use to describe photos, "
    "read text from images, answer questions about visual content.",
    {"image_path": "string", "question": "string?"},
)
def analyze_image(image_path, question=None):
    """Analyze an image file at the given path.

    Args:
        image_path: Absolute path to the image file.
        question: Optional specific question about the image.
    """
    # Security: only allow files under BASE_PATH (Downloads)
    real_path = os.path.realpath(image_path)
    allowed = os.path.realpath(config.BASE_PATH)
    if not real_path.startswith(allowed + os.sep) and real_path != allowed:
        return f"ERROR: Access denied — image must be in {config.BASE_PATH}"

    if not os.path.isfile(real_path):
        return f"ERROR: File not found: {image_path}"

    result = _analyze(real_path, question)
    return result
