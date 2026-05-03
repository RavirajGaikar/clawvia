"""Skill tools — let the agent load a skill's full content on demand.

Skills are listed by name + description in the system prompt (cheap).
Their full bodies are loaded only when the agent calls `load_skill`,
which keeps the prompt small even with many installed skills.
"""

from skills.loader import get_skill_content, list_skill_info
from skills import manager as skill_manager
from skills import recipes as skill_recipes
from tools.registry import registry


@registry.register(
    "load_skill",
    "Load the full instructions of a named skill. "
    "Use this when a skill's name/triggers match the current task and you "
    "need its detailed guidance before acting.",
    {"name": "string"},
    cacheable=True,
)
def load_skill(name):
    name = (name or "").strip()
    if not name:
        return "ERROR: 'name' is required"
    body = get_skill_content(name)
    if body is None:
        available = ", ".join(s["name"] for s in list_skill_info()) or "(none)"
        return f"ERROR: skill '{name}' not found. Available: {available}"
    return body


@registry.register(
    "list_recipes",
    "List the JSON recipes shipped by a skill. "
    "Recipes are pre-baked tool sequences runnable via run_recipe.",
    {"skill": "string"},
)
def list_recipes(skill):
    skill = (skill or "").strip()
    if not skill:
        return "ERROR: 'skill' is required"
    names = skill_manager.list_recipes(skill)
    if not names:
        return f"(no recipes in skill '{skill}')"
    return "\n".join(f"- {n}" for n in names)


@registry.register(
    "run_recipe",
    "Run a JSON recipe shipped by a skill: skills/<skill>/<recipe>.json. "
    "Recipes chain existing tools with variable substitution — use them "
    "for stable, repeatable workflows. `vars` is an optional dict of "
    "overrides for the recipe's `vars` defaults.",
    {"skill": "string", "recipe": "string", "vars": "object?"},
)
def run_recipe(skill, recipe, vars=None):
    skill = (skill or "").strip()
    recipe = (recipe or "").strip()
    if not skill or not recipe:
        return "ERROR: 'skill' and 'recipe' are required"
    data, err = skill_manager.get_recipe(skill, recipe)
    if err:
        return f"ERROR: {err}"
    result = skill_recipes.run_recipe(data, overrides=vars or {})
    return skill_recipes.format_result(result)

