"""Success pattern extraction — learns what strategies worked."""

from llm.client import chat, LLMError
from memory import db
from utils.logger import get_logger

log = get_logger("memory.patterns")

_MIN_STEPS_FOR_EXTRACTION = 3  # Only learn from tasks with 3+ tool calls


def extract_pattern(task, steps):
    """After a successful multi-step task, extract a reusable strategy.

    Called from agent.run() after a successful finish with enough steps.
    Runs a cheap LLM call to summarize what worked.
    """
    if len(steps) < _MIN_STEPS_FOR_EXTRACTION:
        return None

    # Build a compact summary of what was done
    step_lines = []
    for s in steps[:10]:  # Cap at 10 steps to keep prompt small
        action = s.get("action", "?")
        obs_preview = s.get("observation", "")[:100]
        step_lines.append(f"  {action} → {obs_preview}")

    prompt = (
        f"Task: {task[:200]}\n\n"
        f"Steps taken (all succeeded):\n" + "\n".join(step_lines) + "\n\n"
        "In 1-2 sentences, what reusable strategy/approach made this work? "
        "Focus on the method, not the specific data."
    )

    try:
        pattern = chat(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=150,
        )
        pattern = pattern.strip()
    except LLMError as exc:
        log.warning("Pattern extraction failed: %s", exc)
        return None

    if not pattern or len(pattern) < 10:
        return None

    # Extract keywords from task for future retrieval
    keywords = _extract_keywords(task)
    if not keywords:
        return None

    db.save_strategy_pattern(keywords, pattern)
    log.info("Saved strategy pattern: %s → %s", keywords[:50], pattern[:80])
    return pattern


def recall_strategies(task, limit=2):
    """Recall relevant strategy patterns for a task."""
    patterns = db.search_strategy_patterns(task, limit=limit)
    if not patterns:
        return ""

    parts = ["## Strategies That Worked"]
    for p in patterns:
        text = p["pattern_text"][:300]
        count = p["success_count"]
        parts.append(f"- (used {count}x) {text}")

    return "\n".join(parts)


def reinforce_pattern(task):
    """Increment success count for patterns that match this task."""
    keywords = _extract_keywords(task)
    if keywords:
        db.increment_pattern_success(keywords)


def _extract_keywords(task):
    """Extract meaningful keywords from a task string."""
    import re
    words = re.findall(r'[a-zA-Z]{3,}', task.lower())
    # Filter common stop words
    stop = {"the", "and", "for", "that", "this", "with", "from", "are", "was",
            "were", "been", "have", "has", "had", "will", "would", "could",
            "should", "can", "may", "might", "shall", "not", "but", "you",
            "your", "what", "which", "who", "how", "when", "where", "why",
            "all", "each", "every", "both", "few", "more", "most", "other",
            "some", "such", "than", "too", "very", "just", "about", "also",
            "into", "only", "then", "them", "they", "there", "here"}
    keywords = [w for w in words if w not in stop][:8]
    return " ".join(sorted(set(keywords)))
