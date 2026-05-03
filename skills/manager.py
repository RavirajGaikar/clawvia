"""Skill manager — install / remove / update skills from git or local paths.

A *skill package* is any directory containing a `SKILL.md` at its root.
The package can ship extra files alongside it — recipes (JSON), helper
scripts, READMEs — and they'll travel with the skill when copied into
`skills/<name>/`.

Install sources:
  * `https://github.com/user/repo[.git]` — git clone --depth=1
  * `git@github.com:user/repo.git`       — git clone --depth=1
  * `/local/path/to/skill_dir`           — copytree

Skills installed via this manager are recorded with `source:` in their
SKILL.md frontmatter so `/skill update <name>` knows where to re-pull
from.

Bundled skills (those shipped with ClawVia — code-helper, research,
python-sandbox, termux-admin) are protected from removal by the
`_BUNDLED` set.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from utils.logger import get_logger
from skills import loader as skills_loader

log = get_logger("skills.manager")

_BUNDLED = {"code-helper", "research", "python-sandbox", "termux-admin"}
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,49}$", re.IGNORECASE)
_GIT_TIMEOUT = 60


def _skills_root():
    return Path(__file__).parent


def _skill_dir(name):
    return _skills_root() / name


def _safe_name(name):
    """Reject anything that isn't a simple identifier — no slashes, no '..'."""
    if not name or not _SKILL_NAME_RE.match(name):
        return False
    return name not in {".", ".."}


def _run_git(args, cwd=None, timeout=_GIT_TIMEOUT):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return None, (result.stderr or result.stdout or "git failed").strip()
        return (result.stdout or "").strip(), None
    except FileNotFoundError:
        return None, "git not installed (pkg install git)"
    except subprocess.TimeoutExpired:
        return None, "git command timed out"
    except Exception as exc:
        return None, str(exc)


def _find_skill_md(root):
    """Return Path to the SKILL.md inside a freshly-cloned/copied tree.

    Accepts either:
      * <root>/SKILL.md
      * <root>/<single-subdir>/SKILL.md
    """
    direct = Path(root) / "SKILL.md"
    if direct.exists():
        return direct
    # Some repos wrap the skill in a subfolder
    for child in Path(root).iterdir():
        if child.is_dir() and (child / "SKILL.md").exists():
            return child / "SKILL.md"
    return None


def _read_frontmatter_name(skill_md_path):
    """Pull the `name:` field out of a SKILL.md frontmatter."""
    try:
        text = Path(skill_md_path).read_text(encoding="utf-8")
    except Exception:
        return None
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    for line in m.group(1).splitlines():
        line = line.strip()
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip('"').strip("'")
    return None


def _stamp_source(skill_dir, source_url):
    """Add or update `source:` in the SKILL.md frontmatter so `update` works."""
    md = Path(skill_dir) / "SKILL.md"
    if not md.exists():
        return
    try:
        text = md.read_text(encoding="utf-8")
    except Exception:
        return
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        # No frontmatter at all — synthesize a minimal one
        new = f"---\nsource: {source_url}\n---\n{text}"
    else:
        fm, body = m.group(1), m.group(2)
        if re.search(r"^\s*source:", fm, re.MULTILINE):
            fm = re.sub(
                r"^\s*source:.*$",
                f"source: {source_url}",
                fm,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            fm = fm.rstrip() + f"\nsource: {source_url}"
        new = f"---\n{fm}\n---\n{body}"
    try:
        md.write_text(new, encoding="utf-8")
    except Exception as exc:
        log.warning("Could not stamp source into %s: %s", md, exc)


def list_installed():
    """Return a list of installed skill summaries (name, version, source, bundled)."""
    skills_loader.reload_skills()
    items = []
    for s in skills_loader.list_skill_info():
        items.append({
            "name": s["name"],
            "description": s["description"],
            "version": s.get("version", ""),
            "source": s.get("source", ""),
            "bundled": s["name"] in _BUNDLED,
            "eligible": s["eligible"],
        })
    return items


def install_skill(source, name=None):
    """Install a skill from a git URL or a local path.

    Returns (ok: bool, message: str).
    """
    source = (source or "").strip()
    if not source:
        return False, "ERROR: source is required (git url or local path)"

    src_path = Path(source)
    is_git = source.startswith(("http://", "https://", "git@", "git://")) or source.endswith(".git")
    is_local = src_path.exists()

    if not is_git and not is_local:
        return False, f"ERROR: not a git url and not an existing local path: {source}"

    tmpdir = tempfile.mkdtemp(prefix="clawvia-skill-")
    try:
        staging = Path(tmpdir) / "pkg"
        if is_git:
            log.info("Cloning skill from %s", source)
            _, err = _run_git(["clone", "--depth=1", source, str(staging)])
            if err:
                return False, f"ERROR: clone failed: {err}"
        else:
            log.info("Copying local skill from %s", src_path)
            shutil.copytree(src_path, staging)

        # Locate SKILL.md
        skill_md = _find_skill_md(staging)
        if not skill_md:
            return False, "ERROR: no SKILL.md found at root or in any single subdirectory"

        # Determine the name
        resolved = name or _read_frontmatter_name(skill_md) or skill_md.parent.name
        if not _safe_name(resolved):
            return False, f"ERROR: invalid skill name '{resolved}' (use letters/digits/_/-)"

        target = _skill_dir(resolved)
        if target.exists():
            return False, f"ERROR: skill '{resolved}' already installed (use /skill update or /skill remove first)"

        pkg_dir = skill_md.parent
        # Drop .git so we don't ship an unintended subrepo
        git_dir = pkg_dir / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir, ignore_errors=True)

        shutil.copytree(pkg_dir, target)

        if is_git:
            _stamp_source(target, source)

        skills_loader.reload_skills()
        return True, f"installed skill '{resolved}' → {target.relative_to(_skills_root().parent)}"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def remove_skill(name):
    """Remove an installed skill. Bundled skills are protected."""
    if not _safe_name(name):
        return False, f"ERROR: invalid name '{name}'"
    if name in _BUNDLED:
        return False, f"ERROR: '{name}' is bundled with ClawVia and can't be removed"
    target = _skill_dir(name)
    if not target.exists():
        return False, f"ERROR: skill '{name}' is not installed"
    try:
        shutil.rmtree(target)
    except Exception as exc:
        return False, f"ERROR: failed to remove: {exc}"
    skills_loader.reload_skills()
    return True, f"removed skill '{name}'"


def update_skill(name):
    """Re-pull a previously git-installed skill from its `source` URL."""
    if not _safe_name(name):
        return False, f"ERROR: invalid name '{name}'"
    skill = skills_loader.get_skill(name)
    if not skill:
        return False, f"ERROR: skill '{name}' not installed"
    src = skill.get("source", "")
    if not src:
        return False, f"ERROR: skill '{name}' has no source URL recorded — reinstall manually"

    # Remove + reinstall under the same name
    ok, msg = remove_skill(name)
    if not ok:
        return False, msg
    ok, msg = install_skill(src, name=name)
    if not ok:
        return False, f"update failed (reinstall step): {msg}"
    return True, f"updated skill '{name}' from {src}"


def get_recipe(skill_name, recipe_name):
    """Load a recipe JSON file `skills/<skill>/<recipe>.json` and return parsed dict."""
    if not _safe_name(skill_name):
        return None, f"invalid skill name: {skill_name!r}"
    sd = _skill_dir(skill_name)
    if not sd.exists():
        return None, f"skill '{skill_name}' not installed"
    # Resist path traversal in recipe_name
    if "/" in recipe_name or "\\" in recipe_name or ".." in recipe_name:
        return None, f"invalid recipe name: {recipe_name!r}"
    fname = recipe_name if recipe_name.endswith(".json") else f"{recipe_name}.json"
    rp = sd / fname
    if not rp.exists():
        return None, f"recipe not found: {sd.name}/{fname}"
    try:
        return json.loads(rp.read_text(encoding="utf-8")), None
    except Exception as exc:
        return None, f"failed to parse recipe: {exc}"


def list_recipes(skill_name):
    """Return a list of *.json files inside a skill folder."""
    if not _safe_name(skill_name):
        return []
    sd = _skill_dir(skill_name)
    if not sd.exists():
        return []
    return sorted(p.stem for p in sd.glob("*.json"))
