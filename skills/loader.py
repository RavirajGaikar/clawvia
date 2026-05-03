"""Skills loader — scan, parse, and inject SKILL.md files into prompts.

Phase 1 model:
  * **Skills** live as `skills/<name>/SKILL.md` and are listed *by name + short
    description + triggers* in the system prompt (progressive disclosure).
    Full bodies are loaded on demand by the `load_skill` tool — keeps the
    prompt small even with 50+ skills.
  * **Roles** live as `skills/roles/<name>.md` (plain markdown, no SKILL.md
    wrapper). They are NEVER auto-injected; the agent loop pulls one in when
    a mode is active (plan/act/review/...).
"""

import os
import re
from pathlib import Path

import config
from utils.logger import get_logger

log = get_logger("skills.loader")

_skills_cache = None
_roles_cache = {}

_ROLES_SUBDIR = "roles"


def load_skills(skills_dir=None):
    """Scan the skills directory and return parsed skill entries.

    Each skill is a subdirectory containing a SKILL.md file. The `roles/`
    subdirectory is skipped — roles are not regular skills.
    Returns list of skill dicts: {name, description, triggers, requires,
    content, eligible}.
    """
    global _skills_cache
    if _skills_cache is not None:
        return _skills_cache

    if skills_dir is None:
        skills_dir = Path(__file__).parent

    skills_dir = Path(skills_dir)
    if not skills_dir.is_dir():
        log.warning("Skills directory not found: %s", skills_dir)
        _skills_cache = []
        return _skills_cache

    skills = []
    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir() or entry.name == _ROLES_SUBDIR:
            continue
        skill_file = entry / "SKILL.md"
        if not skill_file.exists():
            continue

        skill = _parse_skill_md(skill_file, entry.name)
        if skill:
            skill["eligible"] = _check_requirements(skill.get("requires", {}))
            skills.append(skill)
            log.debug("Loaded skill: %s (eligible=%s)", skill["name"], skill["eligible"])

    _skills_cache = skills
    log.info("Loaded %d skills (%d eligible)",
             len(skills), sum(1 for s in skills if s["eligible"]))
    return _skills_cache


def reload_skills():
    """Force reload skills from disk."""
    global _skills_cache, _roles_cache
    _skills_cache = None
    _roles_cache = {}
    return load_skills()


def _parse_skill_md(path, dir_name):
    """Parse a SKILL.md file with YAML-ish frontmatter + markdown body."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("Failed to read %s: %s", path, exc)
        return None

    # Extract YAML frontmatter between --- markers
    frontmatter = {}
    body = text
    fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if fm_match:
        fm_text = fm_match.group(1)
        body = fm_match.group(2).strip()
        for line in fm_text.split("\n"):
            line = line.strip()
            if ":" in line and not line.startswith("#"):
                key, _, val = line.partition(":")
                frontmatter[key.strip()] = val.strip().strip('"').strip("'")

    triggers_raw = frontmatter.get("triggers", "")
    triggers = [t.strip() for t in triggers_raw.split(",") if t.strip()]

    recipes_raw = frontmatter.get("recipes", "")
    recipes = [r.strip() for r in recipes_raw.split(",") if r.strip()]

    return {
        "name": frontmatter.get("name", dir_name),
        "description": frontmatter.get("description", ""),
        "version": frontmatter.get("version", ""),
        "author": frontmatter.get("author", ""),
        "homepage": frontmatter.get("homepage", ""),
        "source": frontmatter.get("source", ""),
        "triggers": triggers,
        "recipes": recipes,
        "requires": _parse_requires(frontmatter),
        "content": body,
        "path": str(path),
        "dir": str(path.parent),
    }


def _parse_requires(frontmatter):
    """Parse requirement fields from frontmatter."""
    requires = {}
    for key, val in frontmatter.items():
        if key.startswith("requires."):
            req_type = key.split(".", 1)[1]
            requires[req_type] = [v.strip() for v in val.split(",")]
    return requires


def _check_requirements(requires):
    """Check if skill requirements are met."""
    if not requires:
        return True

    for env_var in requires.get("env", []):
        if not os.environ.get(env_var):
            log.debug("Skill requires env var %s (not set)", env_var)
            return False

    for binary in requires.get("bins", []):
        if not _has_binary(binary):
            log.debug("Skill requires binary %s (not found)", binary)
            return False

    return True


def _has_binary(name):
    """Check if a binary exists on PATH."""
    import shutil
    return shutil.which(name) is not None


def build_skills_prompt(max_chars=1500):
    """Build a *compact* skills index for the system prompt.

    Lists only name + short description + triggers (one short block each).
    Full skill bodies are NOT included — the agent loads them on demand
    via the `load_skill` tool. This scales to 50+ skills cheaply.
    """
    skills = load_skills()
    eligible = [s for s in skills if s["eligible"]]
    if not eligible:
        return ""

    parts = [
        "# Available Skills (load full content with `load_skill(name=\"...\")`)",
    ]
    total = len(parts[0])

    for skill in eligible:
        line = f"- **{skill['name']}**"
        if skill["description"]:
            line += f" — {skill['description']}"
        if skill["triggers"]:
            line += f"  (triggers: {', '.join(skill['triggers'])})"
        if total + len(line) + 1 > max_chars:
            remaining = len(eligible) - (len(parts) - 1)
            parts.append(f"... (+{remaining} more skills available)")
            break
        parts.append(line)
        total += len(line) + 1

    return "\n".join(parts)


def get_skill_content(name):
    """Return the full body of a skill by name, or None.

    Used by the `load_skill` tool to surface a skill's full instructions
    only when the agent decides it needs them.
    """
    for s in load_skills():
        if s["name"] == name:
            if not s["eligible"]:
                return f"Skill '{name}' is not eligible (requirements not met)."
            return s["content"]
    return None


def create_skill_file(name, description="", triggers="", content=""):
    """Create a new SKILL.md file in skills/<name>/SKILL.md.

    Returns a success message or an ERROR string.
    """
    import re as _re
    # Validate name — must be a safe slug
    if not _re.match(r'^[a-z0-9][a-z0-9\-]{0,40}$', name):
        return "ERROR: Name must be a lowercase slug (letters, numbers, hyphens, max 40 chars)"
    # Prevent directory traversal
    if ".." in name or "/" in name or "\\" in name:
        return "ERROR: Invalid skill name"

    skills_dir = Path(__file__).parent
    skill_dir = skills_dir / name
    skill_file = skill_dir / "SKILL.md"

    if skill_file.exists():
        return f"ERROR: Skill '{name}' already exists. Use skill_edit to modify it."

    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"ERROR: Could not create directory: {exc}"

    # Build YAML frontmatter
    fm_lines = [
        "---",
        f"name: {name}",
        f"description: {description}",
        f"version: 1.0",
        f"author: ClawVia (auto-generated)",
    ]
    if triggers:
        fm_lines.append(f"triggers: {triggers}")
    fm_lines.append("---")
    fm_lines.append("")

    full_content = "\n".join(fm_lines) + content + "\n"

    try:
        skill_file.write_text(full_content, encoding="utf-8")
    except OSError as exc:
        return f"ERROR: Could not write skill file: {exc}"

    log.info("Created skill: %s at %s", name, skill_file)
    return f"Skill '{name}' created successfully at {skill_file}"


def edit_skill_file(name, new_content):
    """Update the body of an existing SKILL.md (preserves frontmatter).

    Returns a success message or an ERROR string.
    """
    skills_dir = Path(__file__).parent
    skill_file = skills_dir / name / "SKILL.md"

    if not skill_file.exists():
        return f"ERROR: Skill '{name}' not found"

    try:
        text = skill_file.read_text(encoding="utf-8")
    except OSError as exc:
        return f"ERROR: Could not read skill file: {exc}"

    # Extract and preserve frontmatter
    fm_match = _re_module.match(r"^---\s*\n(.*?)\n---\s*\n", text, _re_module.DOTALL)
    if fm_match:
        frontmatter = text[:fm_match.end()]
    else:
        frontmatter = ""

    full_content = frontmatter + new_content.strip() + "\n"

    try:
        skill_file.write_text(full_content, encoding="utf-8")
    except OSError as exc:
        return f"ERROR: Could not write skill file: {exc}"

    log.info("Updated skill: %s", name)
    return f"Skill '{name}' updated successfully"


# Keep a module-level reference to re for use in edit_skill_file
import re as _re_module


def list_skill_info():
    """Return skill info for display (name, description, eligible, version, source)."""
    skills = load_skills()
    return [
        {
            "name": s["name"],
            "description": s["description"],
            "eligible": s["eligible"],
            "version": s.get("version", ""),
            "source": s.get("source", ""),
            "recipes": s.get("recipes", []),
        }
        for s in skills
    ]


def get_skill(name):
    """Return the full skill dict by name, or None."""
    for s in load_skills():
        if s["name"] == name:
            return s
    return None


# ── Roles (mode-selectable system-prompt fragments) ──────────────────────────

def load_role(name, roles_dir=None):
    """Return the markdown body of `skills/roles/<name>.md`, or None.

    Cached per process. Trivially small files; no frontmatter handling.
    """
    if name in _roles_cache:
        return _roles_cache[name]
    if roles_dir is None:
        roles_dir = Path(__file__).parent / _ROLES_SUBDIR
    path = Path(roles_dir) / f"{name}.md"
    if not path.exists():
        log.debug("Role file not found: %s", path)
        _roles_cache[name] = None
        return None
    try:
        body = path.read_text(encoding="utf-8").strip()
    except Exception as exc:
        log.warning("Failed to read role %s: %s", path, exc)
        _roles_cache[name] = None
        return None
    _roles_cache[name] = body
    return body


def list_roles(roles_dir=None):
    """List role names available under skills/roles/."""
    if roles_dir is None:
        roles_dir = Path(__file__).parent / _ROLES_SUBDIR
    roles_dir = Path(roles_dir)
    if not roles_dir.is_dir():
        return []
    return sorted(p.stem for p in roles_dir.glob("*.md"))
