# ClawVia — Complete Project Context

> **What is this file?** A self-contained reference for any AI assistant (Antigravity, Copilot, Cursor, etc.) to fully understand the ClawVia codebase without reading every source file. Hand this file as context when starting a new conversation.

---

## 1. Project Overview

**ClawVia** is a lightweight, self-improving AI agent that runs on Android via **Termux**. It acts as an autonomous assistant with full device access — file system, shell commands, web search, SMS, device sensors — orchestrated through an LLM-powered reasoning loop.

**Core Loop:** Plan → Execute Tool → Observe Result → Repeat (until `finish` tool called or max steps reached).

| Attribute | Value |
|-----------|-------|
| Language | Python 3 |
| LLM Provider | NVIDIA NIM API (default model: `moonshotai/kimi-k2-thinking`) |
| Vision | NVIDIA NIM (`mistralai/mistral-large-3-675b-instruct-2512`) |
| Speech-to-Text | Groq Whisper (`whisper-large-v3-turbo`) |
| Interface | Telegram Bot (polling) + FastAPI REST API |
| Database | SQLite with WAL mode (single file: `clawvia.db`) |
| Target Platform | Android / Termux |
| Pydantic | v1 only (v2 requires Rust compiler, unavailable on Termux) |

---

## 2. File Tree

```
ClawVia/
├── agent.py              # Core agent reasoning loop (plan/execute/observe)
├── config.py             # All env-based configuration with defaults
├── main.py               # FastAPI server (REST endpoints + middleware)
├── run.py                # Entry point — starts server, scheduler, Telegram bot
├── scheduler.py          # Background tasks: scheduled, goals, watchers, idle
├── telegram_bot.py       # Telegram bot: commands, message handling, approval flow
├── soul.md               # Agent persona/personality prompt
├── requirements.txt      # Python dependencies (pinned, Termux-compatible)
├── install.sh            # One-shot Termux installer
├── start_server.sh       # Production launcher (wake lock, auto-restart)
├── clawvia.db            # SQLite database (created at runtime)
│
├── llm/
│   ├── __init__.py
│   ├── client.py         # NVIDIA NIM HTTP client with retries
│   ├── json_parser.py    # Robust JSON extraction from LLM output
│   └── prompts.py        # System prompt builder (soul + skills + tools + format)
│
├── memory/
│   ├── __init__.py
│   ├── db.py             # SQLite schema (16 tables) + all DB queries
│   ├── context.py        # Builds LLM prompt context from history & memory
│   ├── compaction.py     # Auto-summarizes old messages when context is large
│   ├── dreaming.py       # Periodic background reflection/insight generation
│   ├── idle.py           # Self-improvement tasks during user inactivity
│   └── patterns.py       # Extracts reusable strategies from successful tasks
│
├── tools/
│   ├── __init__.py
│   ├── registry.py       # Decorator-based tool registry + cache + execution
│   ├── file_tools.py     # list_files, read_file, write_file, delete_file
│   ├── system_tools.py   # run_command (with dangerous-command approval)
│   ├── web_tools.py      # web_search (DDG), http_request (SSRF-protected)
│   ├── device_tools.py   # battery, storage, network, device_info, SMS
│   ├── intent_tools.py   # app_open, web_open, maps_navigate (Android intents)
│   ├── memory_tools.py   # memory_save, memory_search, memory_get, recall, audit_search
│   ├── schedule_tools.py # schedule_task, list_schedules, cancel_schedule, goal_*
│   ├── session_tools.py  # sessions_list, sessions_new, sessions_clear, sessions_history, sessions_send
│   ├── code_tools.py     # code_execute (Python sandbox)
│   ├── media_tools.py    # analyze_image (vision)
│   ├── skill_tools.py    # skill_create, skill_edit, load_skill, list_recipes, run_recipe
│   ├── todo_tools.py     # todo_add, todo_update, todo_list, todo_clear
│   ├── subagent_tools.py # task (spin off sub-investigation)
│   └── mcp_client.py     # MCP JSON-RPC client (stdio transport)
│
├── media/
│   ├── __init__.py
│   ├── vision.py         # Image analysis via NVIDIA NIM Vision API
│   └── speech.py         # Audio transcription via Groq Whisper API
│
├── security/
│   ├── __init__.py
│   └── guardrails.py     # Prompt injection detection + output sanitization
│
├── skills/
│   ├── loader.py         # Skill discovery (SKILL.md with YAML frontmatter)
│   ├── manager.py        # Install/remove/update skills from git or local
│   ├── recipes.py        # JSON-based deterministic tool sequences
│   ├── code-helper/SKILL.md
│   ├── python-sandbox/SKILL.md
│   ├── research/SKILL.md
│   ├── termux-admin/SKILL.md
│   └── roles/            # Mode overrides (planner, reviewer, qa, interrogator)
│       ├── planner.md
│       ├── reviewer.md
│       ├── qa.md
│       └── interrogator.md
│
├── mcp_servers/
│   ├── __init__.py
│   └── fetch_server.py   # Lightweight JSON-RPC 2.0 fetch tool (stdio)
│
└── utils/
    ├── __init__.py
    └── logger.py          # Structured logging with module names + secret redaction
```

---

## 3. Architecture Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                    User (Telegram / HTTP)                     │
└───────────────────┬───────────────────┬──────────────────────┘
                    │                   │
         ┌──────────▼─────────┐  ┌──────▼──────────┐
         │  telegram_bot.py   │  │    main.py       │
         │  (async polling)   │  │  (FastAPI REST)  │
         └──────────┬─────────┘  └──────┬───────────┘
                    │                   │
                    └────────┬──────────┘
                             ▼
                   ┌──────────────────┐
                   │    agent.py      │
                   │  (Core Loop)     │
                   │  Plan → Execute  │
                   │  → Observe →     │
                   │  Repeat          │
                   └───┬────┬────┬────┘
                       │    │    │
            ┌──────────┘    │    └──────────┐
            ▼               ▼               ▼
      ┌───────────┐  ┌───────────┐  ┌──────────────┐
      │ LLM       │  │ Memory    │  │ Tool Registry│
      │ (NVIDIA   │  │ (SQLite)  │  │ (30+ tools)  │
      │  NIM API) │  │           │  │              │
      └───────────┘  └─────┬─────┘  └──────┬───────┘
                           │               │
                           ▼               ▼
                    ┌────────────────────────────┐
                    │      clawvia.db            │
                    │  16 tables, FTS5 indexes   │
                    └────────────────────────────┘

┌──────────────────────────────────────────────────────────────┐
│                  Background Processes                         │
│  scheduler.py — ticks every 30s                              │
│  ├── Scheduled tasks (one-shot & recurring)                  │
│  ├── Goals (persistent objectives, periodic check-in)        │
│  ├── Event watchers (battery_low, storage_low, file_changed) │
│  ├── Idle tasks (self-improvement when user inactive 30m+)   │
│  └── Dream cycle (reflective insight generation, ~6h)        │
└──────────────────────────────────────────────────────────────┘
```

---

## 4. Core Modules

### 4.1 agent.py — The Brain

**Main function:** `run(task, session_id=None, mode=None, think_level=None) -> str`

**Agent loop logic:**
1. **Fast-path detection** — greetings/thanks bypass full loop (15+ regex patterns)
2. **Build context** — user profile + recent messages + episodic memory + reflections + strategies + cross-session recall + dreams
3. **LLM call** — NVIDIA NIM with system prompt (soul + skills index + tools schema) + context + task + step history
4. **Parse response** — expects JSON: `{"tool": "name", "args": {...}, "thought": "..."}`
5. **Execute tool** — via `registry.execute(tool_name, args)` → observation string
6. **Safety checks:**
   - If tool needs approval → store in `pending_approvals`, wait for user `/approve` or `/deny`
   - If consecutive errors ≥ `REFLEXION_AFTER_ERRORS` (2) → generate verbal reflection (cheap LLM call), inject into next step
   - If consecutive errors ≥ `STUCK_ERROR_LIMIT` (3) → bail out, ask user for help
7. **Checkpoint** — save step snapshot to DB after each iteration
8. **Loop** — repeat until `tool="finish"` (output in `args.output`) or `step ≥ MAX_AGENT_STEPS`
9. **Post-processing** — store task log, extract patterns from successful multi-step tasks, update user profile, auto-compact session if threshold exceeded

**Key data structures:**
- `steps: list[dict]` — `[{step, action: {tool, args, thought}, observation}]`
- `pending_approvals: dict` — `{session_id: {tool, args, task, step}}`
- `_subagent_local` — thread-local depth counter (max depth: `SUBAGENT_MAX_DEPTH=2`)

**Hardcoded constants:**
- `MAX_AGENT_STEPS`: 25 (overridable by think_level)
- `STUCK_ERROR_LIMIT`: 3
- `REFLEXION_AFTER_ERRORS`: 2
- `FAST_PATH_PATTERNS`: ~15 regex patterns (hi, thanks, how are you, bye, etc.)

**Session locking:** Per-session `threading.Lock` prevents concurrent agent runs on the same session.

---

### 4.2 llm/ — LLM Integration

#### llm/client.py — NVIDIA NIM HTTP Client

```python
chat(messages: list[dict], temperature=None, max_tokens=None) -> str
```
- POST to `https://integrate.api.nvidia.com/v1/chat/completions`
- 2 retries on 429 or 5xx errors
- Timeout: `config.LLM_TIMEOUT` (default 120s)
- Returns assistant text content or raises `LLMError`

#### llm/prompts.py — Prompt Construction

```python
build_system_prompt(tools_metadata, role=None, think_level=None) -> str
build_planner_messages(task, context=None, steps=None, ...) -> list[dict]
```

System prompt assembly order:
1. Soul/persona (from `soul.md`)
2. Skills index (name + description + triggers — compact, not full bodies)
3. Tool list formatted as JSON schema
4. Response format instructions (JSON only: `{"tool": "...", "args": {...}, "thought": "..."}`)
5. Chain-of-Code nudge — prefer `code_execute` for math/parsing/date arithmetic
6. Deep-think nudge (if `think_level='think'`): take time, verify, use code
7. Quick-mode nudge (if `think_level='quick'`): be decisive, finish in 1-3 calls
8. User profile (learned preferences/habits)

#### llm/json_parser.py — Robust JSON Extraction

Three strategies tried in order:
1. Direct `json.loads()`
2. Extract from markdown code blocks (` ```json ... ``` `)
3. Bracket-matching (find outermost `{...}` or `[...]`)

---

### 4.3 memory/ — Persistent & Episodic Memory

#### memory/db.py — SQLite Schema & Queries (~600 lines)

All functions decorated with `@_locked` (uses `threading.RLock` for thread safety).

**Tables:**

| Table | Purpose | Key Columns |
|-------|---------|-------------|
| `sessions` | Conversation sessions | id, title, is_active, compaction_count, created_at |
| `messages` | Chat history | session_id, role, content, timestamp + FTS5 index |
| `task_logs` | Task results with step snapshots | session_id, task, result, steps_json, created_at |
| `memory_notes` | Long-term notes | topic, content, source, confidence + FTS5 index |
| `checkpoints` | Agent loop snapshots after each step | session_id, step_number, state_json |
| `reflections` | Failure reflections (Reflexion) | session_id, error_hash, reflection_text (deduped) |
| `scheduled_tasks` | One-shot and recurring tasks | prompt, schedule_type, schedule_value, next_run, status, retries |
| `goals` | Persistent objectives | description, priority (1-10), check_interval, next_check, progress_notes, status |
| `watchers` | Event-triggered actions | event_type, threshold, action_prompt, cooldown_minutes, last_triggered |
| `todos` | Session-scoped checklist | session_id, text, status (open/in_progress/done/cancelled) |
| `dream_log` | Insights from background dreaming | pattern, self_critique, score, source_notes |
| `user_profile` | Learned preferences | key, value, confidence, source |
| `strategy_patterns` | Reusable successful approaches | task_keywords, pattern_text, success_count |
| `kv_store` | Misc persistent state | key, value (last_dream_time, idle_threshold, etc.) |
| `audit_log` | Action trail for debugging | action_type, tool_name, args_summary, result_summary, timestamp |
| `pending_approvals` | Tool calls awaiting user confirmation | session_id, tool, args_json, task, step (persists across restarts) |

**Key query functions:**
- `create_session()`, `get_active_session()`, `switch_session()`, `delete_session()`
- `add_message()`, `get_messages()`, `clear_messages()`
- `save_note()`, `get_note()`, `delete_note()`, `search_notes_fts()`, `search_messages_fts()`
- `add_scheduled_task()`, `get_due_tasks()`, `update_scheduled_task()`
- `add_goal()`, `get_due_goals()`, `update_goal()`
- `save_checkpoint()`, `list_checkpoints()`, `get_checkpoint()`
- `save_reflection()`, `get_recent_reflections()`
- `get_user_profile()`, `upsert_user_profile()`
- `search_strategy_patterns()`, `save_strategy_pattern()`
- `audit_log_event()`
- `kv_get()`, `kv_set()`
- `get_all_pending_approvals()`

#### memory/context.py — Context Building

```python
build_context(session_id, max_messages=10, task=None) -> str
```

Assembles prompt context from (in order):
1. **User Profile** — strong-confidence entries (name, timezone, preferences)
2. **Recent Conversation** — last N messages from active session
3. **Recent Task Results** — top 3 past tasks + results (~4000 chars)
4. **Episodic Memory** — FTS-matched notes relevant to the current task
5. **Recent Reflection** — most recent failure reflection for this session
6. **Strategy Patterns** — reusable approaches that worked for similar tasks
7. **Cross-Session Recall** — relevant snippets from past conversations (all sessions)
8. **Recent Dreams** — latest background insights

Output capped at ~12,000 chars to stay within LLM context budget.

#### memory/compaction.py — Conversation Summarization

```python
compact_session(session_id, instruction=None) -> dict
```

- **Trigger:** Auto when session exceeds `COMPACTION_THRESHOLD_TOKENS` (16,000)
- **Process:** Keep most recent 8 messages intact, summarize older messages with cheap LLM call, replace old messages with summary, index summary into cross-session FTS
- **Returns:** `{messages_before, messages_after, tokens_before, tokens_after, tokens_saved}`

#### memory/dreaming.py — Background Self-Improvement

```python
dream_cycle(hours=24) -> str
```

- **Frequency:** Default every 6 hours, adaptive (increases if stale, decreases if useful)
- **Process:** Gather recent notes + conversations + profile + task metrics → LLM prompt ("you are the subconscious dreaming process") → produces JSON with `pattern`, `self_critique`, `actions` (save_note, create_goal, update_profile)
- **Logged to:** `dream_log` table with score (0.0-1.0)

#### memory/idle.py — Self-Improvement During Inactivity

Runs when user inactive ≥ `idle_threshold_minutes` (default 30 min):

| Task | Cooldown | Purpose |
|------|----------|---------|
| `error_review` | 12h | Scan recent errors for patterns |
| `memory_consolidation` | 24h | Merge duplicate/overlapping notes |
| `profile_enrichment` | 8h | Extract user preferences from conversation |
| `skill_audit` | 48h | Check if common patterns should become skills |
| `goal_review` | 24h | Review stale goals and suggest updates |
| `self_review` | 168h (1 week) | Weekly performance self-review |

#### memory/patterns.py — Success Pattern Extraction

```python
extract_pattern(task, steps) -> str       # After successful 5+ step tasks
recall_strategies(task, limit=2) -> str   # Retrieve past strategies for similar tasks
```

Stores in `strategy_patterns` table, indexed by task keywords, with `success_count`.

---

### 4.4 tools/ — Tool Registry & Implementations

#### tools/registry.py — Registry

```python
@registry.register("tool_name", "description", {"arg1": "type"}, cacheable=False)
def tool_implementation(arg1):
    return result_string  # or "ERROR: ..." on failure
```

- Decorator-based registration
- In-process cache for read-only tools (TTL: `TOOL_CACHE_TTL_SEC` default 300s, max entries: 128)
- `registry.execute(name, args)` → result string (prefixed with `ERROR:` on failure)
- `get_all_metadata()` → `[{name, description, args}]` for system prompt

#### Complete Tool Reference

**File Tools** (`tools/file_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `list_files` | `path?` | ✓ | Lists files/dirs under `BASE_PATH` |
| `read_file` | `filename` | ✓ | Read content (capped 50KB) |
| `write_file` | `filename, content` | ✗ | Write/create file |
| `delete_file` | `filename` | ✗ | Delete file |

**System Tools** (`tools/system_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `run_command` | `cmd, timeout?` | ✗ | Shell execution. Blocks pipes, `rm -rf`, `wget`, `chmod 777`, etc. Dangerous commands require approval |

**Web Tools** (`tools/web_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `web_search` | `query` | ✓ | DuckDuckGo search → title + snippet + URL |
| `http_request` | `url, method?, body?` | ✗ | GET/POST. Blocks private IPs (SSRF protection) |

**Device Tools** (`tools/device_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `device_battery` | — | ✓ | Battery % + status + temp via Termux:API |
| `device_storage` | — | ✓ | `df -h` output |
| `device_network` | — | ✓ | SSID, signal, IP, location via ipinfo.io |
| `device_info` | — | ✓ | Combined battery + storage + network |
| `sms_inbox` | `type?, limit?` | ✓ | SMS list (inbox/sent/draft/outbox) via Termux:API |
| `sms_send` | `number, text` | ✗ | Send SMS (requires approval) |

**Intent Tools** (`tools/intent_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `app_open` | `package` | ✗ | Launch app via `am start` |
| `web_open` | `url` | ✗ | Open URL in browser |
| `maps_navigate` | `query` | ✗ | Open Maps to location |

**Memory Tools** (`tools/memory_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `memory_save` | `topic, content` | ✗ | Save to long-term memory |
| `memory_search` | `query` | ✗ | FTS search across notes |
| `memory_get` | `topic` | ✗ | Get specific note by topic |
| `recall` | `query, limit?` | ✗ | Cross-session message search |
| `audit_search` | `query?, action_type?, limit?` | ✗ | Search audit log |

**Schedule Tools** (`tools/schedule_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `schedule_task` | `prompt, schedule` | ✗ | Create one-shot or recurring task |
| `list_schedules` | — | ✗ | List active scheduled tasks |
| `cancel_schedule` | `task_id` | ✗ | Cancel a task |
| `goal_set` | `description, priority?, check_interval?` | ✗ | Create persistent goal |
| `goal_update` | `goal_id, progress?, status?` | ✗ | Update goal progress/status |
| `goal_list` | `include_inactive?` | ✗ | List active or all goals |

**Session Tools** (`tools/session_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `sessions_list` | — | ✗ | List all sessions |
| `sessions_new` | `title?` | ✗ | Create new session |
| `sessions_clear` | — | ✗ | Clear current session messages |
| `sessions_history` | `session_id, limit?` | ✗ | Retrieve messages from any session |
| `sessions_send` | `session_id, message` | ✗ | Send message to another session |

**Code Tools** (`tools/code_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `code_execute` | `code, timeout?` | ✗ | Python 3 sandbox. Blocks: subprocess, ctypes, eval, exec, open, importlib, signal |

**Media Tools** (`tools/media_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `analyze_image` | `image_path, question?` | ✗ | Vision analysis via NVIDIA NIM |

**Skill Tools** (`tools/skill_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `skill_create` | `name, description, triggers, content` | ✗ | Create new reusable skill |
| `skill_edit` | `name, new_content` | ✗ | Update skill instructions |
| `load_skill` | `name` | ✓ | Load full skill body on demand |
| `list_recipes` | `skill` | ✗ | List JSON recipes in a skill |
| `run_recipe` | `skill, recipe, vars?` | ✗ | Execute deterministic tool sequence |

**Todo Tools** (`tools/todo_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `todo_add` | `text` | ✗ | Add todo to current session |
| `todo_update` | `id, status?, text?` | ✗ | Update todo status/text |
| `todo_list` | `include_done?` | ✗ | List todos for session |
| `todo_clear` | — | ✗ | Delete all todos for session |

**Subagent Tools** (`tools/subagent_tools.py`):

| Tool | Args | Cacheable | Notes |
|------|------|-----------|-------|
| `task` | `prompt, think_level?` | ✗ | Spin off sub-investigation in fresh session (max depth: 2) |

---

### 4.5 security/guardrails.py — Safety

#### Input checking: `check_input(text) -> str | None`

Detects prompt injection via 20+ regex patterns:
- Direct role overrides: "ignore previous instructions", "you are now", "pretend to be"
- System prompt extraction: "reveal your system prompt", "show me your instructions"
- Tool manipulation: JSON injection patterns
- Jailbreaks: DAN mode, developer mode
- Delimiter injection: `<system>`, `[/SYSTEM]`
- Script mixing: Unicode homoglyph obfuscation
- Encoded payloads: Base64, hex escapes, URL encoding

Returns `None` if safe, warning string if suspicious.

#### Output sanitization: `sanitize_tool_output(output, tool_name) -> str`

Strips injection attempts from tool results (web pages, file contents). Replaces suspicious patterns with `[CONTENT_FILTERED]` and adds ⚠️ warning header.

#### Command approval flow:
Dangerous commands (`rm -r`, `chmod`, `apt install`, `pip install`, `kill`, etc.) → stored as pending approval → user must `/approve` or `/deny` in Telegram → agent resumes or aborts.

#### Other safety mechanisms:
- **Path traversal blocking** — `realpath` checks in file tools
- **SSRF protection** — blocks private IP ranges in web requests
- **Code sandbox** — blocks subprocess, ctypes, eval, exec, open, importlib, signal
- **User ID verification** — only `ALLOWED_USER_ID` can interact via Telegram (fail-closed)

---

### 4.6 skills/ — Procedural Memory

#### skills/loader.py — Skill Discovery

Skills are SKILL.md files with YAML frontmatter:

```yaml
---
name: code-helper
description: Programming assistance
version: 1.0
author: ClawVia
triggers: code, debug, write script, fix, refactor
requires.env: SOME_VAR
requires.bins: git, python3
recipes: morning-briefing, audit-wifi
---
# Full markdown body with instructions...
```

Key functions:
- `load_skills(skills_dir)` — scan, parse SKILL.md, check requirements
- `build_skills_prompt(max_chars=1500)` — compact index for system prompt
- `get_skill_content(name)` — load full body on demand
- `create_skill_file(name, description, triggers, content)` — create new skill
- `edit_skill_file(name, new_content)` — update body (preserves frontmatter)
- `reload_skills()` — force reload from disk

**Bundled skills (protected, cannot be removed):** `code-helper`, `research`, `python-sandbox`, `termux-admin`

**Roles** (`skills/roles/`): Mode overrides that change agent behavior:
- `planner.md` — break requests into todos (no tool use except `todo_add`)
- `reviewer.md` — audit recent work for completeness
- `qa.md` — verify completed work actually works
- `interrogator.md` — surface assumptions before acting (no tool use)

#### skills/manager.py — Installation

- `install_skill(source, name?)` — clone from git or copy from local
- `remove_skill(name)` — remove (unless bundled)
- `update_skill(name)` — re-pull from original source
- `list_installed()` — summaries with version, source, bundled status

#### skills/recipes.py — Deterministic Tool Sequences

Recipe JSON schema:
```json
{
  "name": "morning_briefing",
  "description": "Check battery, weather, SMS",
  "vars": { "city": "Bangalore" },
  "steps": [
    { "tool": "device_battery", "args": {} },
    { "tool": "web_search", "args": { "query": "weather {city}" }, "save": "weather" },
    { "tool": "todo_list", "args": {}, "optional": true },
    { "tool": "notification_send", "args": { "title": "Briefing", "content": "{weather}" }, "if": "weather" }
  ]
}
```

Features: `{var}` substitution, `if: <var>` conditional, `optional: true` for non-critical steps, `save: <key>` captures output (truncated to 4000 chars).

---

### 4.7 scheduler.py — Background Orchestration

**Class:** `Scheduler(agent_run_fn, notify_fn)`

**Tick cycle** (every `SCHEDULER_CHECK_INTERVAL` = 30 seconds):

1. **Scheduled tasks** — parse schedule: `"in 20m"`, `"at 2024-01-01T12:00:00Z"`, `"every 30m"`, `"every 2h"`, `"every 1d"`. Retry logic: 3 retries for one-shot, 2-min retry delay. Catch-up on restart.
2. **Goals** — persistent objectives with priority (1-10, lower = more important), `check_interval` (default 1h), `progress_notes`. Agent checks periodically via LLM task.
3. **Event watchers** — condition-triggered:
   - `battery_low`: trigger if battery ≤ threshold%
   - `storage_low`: trigger if available ≤ threshold MB
   - `time_of_day`: trigger at HH:MM
   - `file_changed`: trigger if file modified within last N seconds
   - Each has `cooldown_minutes` to prevent spam
4. **Idle tasks** — if user inactive ≥ 30min and 2h+ since last idle task → run next eligible self-improvement task

---

### 4.8 telegram_bot.py — User Interface

Async `python-telegram-bot` v20.3 with polling (no webhooks).

**Commands:**

| Command | Purpose |
|---------|---------|
| `/start` | Show help menu |
| `/new [title]` | Create new session |
| `/sessions` | List all sessions |
| `/switch <id>` | Switch active session |
| `/reset` | Clear current session history |
| `/tools` | List available tools |
| `/status` | Uptime + memory usage + active session info |
| `/compact [instruction]` | Compress conversation history |
| `/schedules` | List active scheduled tasks |
| `/skills` | List loaded skills |
| `/skill list\|install\|remove\|update\|recipes\|run` | Manage skills |
| `/approve` | Approve a pending command |
| `/deny` | Deny a pending command |
| `/checkpoints [id]` | List or inspect agent-loop snapshots |
| `/reflections` | Show recent failure reflections |
| `/mcp` | List MCP servers + their tools |
| `/quick <task>` | Fast mode (few steps, low temp) |
| `/think <task>` | Deep reasoning (more steps, higher temp, reflexion) |

**Message handling:**
- Non-command text → `agent.run(msg_text, session_id)`
- Image messages → vision analysis (if `VISION_MODEL` configured)
- Audio messages → speech-to-text (if `GROQ_API_KEY` configured)
- Responses split into 4096-char chunks (Telegram limit)

---

### 4.9 main.py — FastAPI Server

**Endpoints:**

| Route | Method | Body / Params | Response |
|-------|--------|---------------|----------|
| `/health` | GET | — | `{status, tool_count, session_count}` |
| `/tools` | GET | — | `[{name, description, args}]` |
| `/task` | POST | `{task, session_id?}` | `{result}` |
| `/sessions` | GET | — | `[{id, title, ...}]` |
| `/sessions` | POST | `{title?}` | `{session}` |
| `/sessions/{id}` | DELETE | — | `{ok}` |
| `/sessions/{id}/history` | GET | — | `{session, messages}` |

**Middleware:**
- **CORS** — allow `localhost:8001`, `127.0.0.1`
- **API Key Auth** — require `X-API-Key` header if `API_SECRET_KEY` is set (fail-closed)
- **Rate Limiting** — 30 requests/minute per IP (in-memory tracker)

---

### 4.10 MCP Servers

#### mcp_servers/fetch_server.py

Lightweight JSON-RPC 2.0 server (no pydantic, no SDK) exposing a `fetch` tool:
- `fetch(url, max_length?, raw?)` — fetches URL, converts HTML to readable text (strips scripts/styles), pretty-prints JSON, returns plain text as-is
- Transport: stdio (subprocess via `tools/mcp_client.py`)

---

### 4.11 media/ — Vision & Speech

#### media/vision.py
```python
analyze_image(image_path, question?) -> str
```
Base64-encodes image, sends multimodal message to NVIDIA NIM Vision API. Default question: "Describe this image in detail."

#### media/speech.py
```python
transcribe_audio(audio_path) -> str
```
Sends to Groq Whisper API (`whisper-large-v3-turbo`). Max file size: 25 MB.

---

## 5. Data Flow

### User Message → Agent Response

```
User sends message (Telegram or HTTP POST /task)
    │
    ▼
Check fast-path patterns (greeting/thanks)
    │── Yes → Return quick response, done
    │── No ↓
    ▼
Build context:
    user profile + recent messages + episodic memory +
    reflections + strategies + cross-session recall + dreams
    (~12,000 char cap)
    │
    ▼
LLM call (system prompt + context + task + step history)
    │
    ▼
Parse JSON: {tool, args, thought}
    │
    ▼
Execute tool via registry
    │
    ├── APPROVAL_REQUIRED → store pending, notify user, wait
    ├── ERROR → increment error count
    │   ├── errors ≥ 2 → generate reflection, inject into next step
    │   └── errors ≥ 3 → bail out, ask user
    └── OK → observation string
    │
    ▼
Save checkpoint (step snapshot)
    │
    ▼
Loop back with updated step history
    │
    ▼ (until tool="finish" or max steps)
    │
Extract final output from finish.args.output
    │
    ▼
Post-processing:
    - Store task log
    - Extract reusable pattern (if 5+ steps)
    - Update user profile
    - Auto-compact if token threshold exceeded
    │
    ▼
Send result to user
```

### Memory Injection Points

| When | What |
|------|------|
| Build context (every step) | Profile + recent messages + episodic notes + reflections + strategies + cross-session recall + dreams |
| After successful finish | Extract pattern, update profile, maybe create goal/skill |
| Background (scheduler) | Dreams (6h), idle tasks (30m+), compaction (auto), goal check-ins |

---

## 6. Configuration Reference

All settings via environment variables (loaded by `python-dotenv` from `.env` file).

### Required

| Variable | Purpose |
|----------|---------|
| `NVIDIA_API_KEY` | LLM API key for NVIDIA NIM |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `ALLOWED_USER_ID` | Telegram user ID (fail-closed if not set) |

### LLM

| Variable | Default | Purpose |
|----------|---------|---------|
| `NVIDIA_API_URL` | `https://integrate.api.nvidia.com/v1/chat/completions` | LLM endpoint |
| `NVIDIA_MODEL` | `moonshotai/kimi-k2-thinking` | LLM model |
| `VISION_MODEL` | `mistralai/mistral-large-3-675b-instruct-2512` | Vision model |
| `GROQ_API_KEY` | *(optional)* | Speech-to-text |
| `LLM_TEMPERATURE` | `0.2` | Sampling temperature |
| `LLM_MAX_TOKENS` | `2048` | Max tokens per call |
| `LLM_TIMEOUT` | `120` | Request timeout (seconds) |

### Agent Behavior

| Variable | Default | Purpose |
|----------|---------|---------|
| `MAX_AGENT_STEPS` | `25` | Max steps before force-finishing |
| `STUCK_ERROR_LIMIT` | `3` | Consecutive errors before giving up |
| `REFLEXION_AFTER_ERRORS` | `2` | Errors before generating reflection |
| `REFLEXION_MAX_TOKENS` | `200` | Max tokens for reflection LLM call |
| `SUBAGENT_MAX_DEPTH` | `2` | Max nesting for `task` tool |
| `SUBAGENT_MAX_STEPS` | `10` | Max steps per subagent |

### Think Levels

| Variable | Default | Purpose |
|----------|---------|---------|
| `QUICK_MAX_STEPS` | `5` | `/quick` mode step limit |
| `QUICK_TEMPERATURE` | `0.1` | `/quick` mode temperature |
| `QUICK_MAX_TOKENS` | `800` | `/quick` mode token limit |
| `THINK_MAX_STEPS` | `40` | `/think` mode step limit |
| `THINK_TEMPERATURE` | `0.4` | `/think` mode temperature |
| `THINK_MAX_TOKENS` | `4096` | `/think` mode token limit |

### Memory & Compaction

| Variable | Default | Purpose |
|----------|---------|---------|
| `COMPACTION_THRESHOLD_TOKENS` | `16000` | Auto-compact when exceeded |
| `POST_COMPACTION_KEEP_MESSAGES` | `8` | Messages kept during compaction |
| `DREAM_INTERVAL_HOURS` | `6` | Initial dream cycle interval |
| `DREAM_MIN_SCORE` | `0.3` | Minimum score for interesting dreams |
| `DB_PATH` | `clawvia.db` | SQLite database file |

### Server & Paths

| Variable | Default | Purpose |
|----------|---------|---------|
| `API_HOST` | `0.0.0.0` | FastAPI listen address |
| `API_PORT` | `8001` | FastAPI listen port |
| `API_SECRET_KEY` | *(optional)* | If set, requires `X-API-Key` header |
| `BASE_PATH` | `/storage/emulated/0/Download` | Root for file operations |
| `SOUL_PATH` | `soul.md` | Personality prompt file |
| `MEMORY_NOTES_DIR` | `memory/notes/` | Legacy notes directory |

### Scheduling & Tools

| Variable | Default | Purpose |
|----------|---------|---------|
| `SCHEDULER_CHECK_INTERVAL` | `30` | Tick interval (seconds) |
| `TIMEZONE` | `Asia/Kolkata` | IANA timezone for scheduler |
| `APPROVAL_TIMEOUT` | `300` | Pending approval expiry (seconds) |
| `TOOL_CACHE_TTL_SEC` | `300` | Read-only tool cache TTL |
| `TOOL_CACHE_MAX_ENTRIES` | `128` | Max cached tool results |
| `CODE_EXEC_TIMEOUT` | `30` | Python sandbox max runtime |
| `LOG_LEVEL` | `INFO` | Logging level |

---

## 7. Agent Persona (soul.md)

```
You are ClawVia, a helpful AI assistant running on the user's Android device.

You are concise, accurate, and action-oriented. You have access to the user's file system,
can run shell commands, search the web, and query device status.

You are a self-improving agent. You learn from every interaction, remember what works, and
proactively improve your skills. You have long-term memory that spans across sessions, and
you build a persistent understanding of your user over time.

Guidelines:
- Be direct. Avoid filler phrases.
- When a task requires multiple steps, plan before acting.
- If a tool call fails, try an alternative approach before giving up.
- For file operations, always confirm what you did.
- For search tasks, summarize the key findings.
- If you don't know something and can't find it with tools, say so.
- Keep responses mobile-friendly (short, scannable).
- Use `recall` to check if you've handled a similar task before in a previous session.
- Use `memory_save` proactively to remember important facts, user preferences, and key findings.
- When you complete a complex multi-step task successfully, consider whether the approach
  should become a reusable skill.
- Adapt your communication style to what you've learned about the user.
```

---

## 8. Dependencies

```
fastapi==0.99.1              # REST framework
pydantic==1.10.21            # Data validation (v1 — v2 needs Rust, unavailable on Termux)
uvicorn==0.22.0              # ASGI server
python-telegram-bot==20.3    # Telegram bot (async, polling)
python-dotenv==1.0.0         # .env file loader
requests==2.31.0             # HTTP client
```

**Termux constraint:** Pydantic v2 requires Rust compiler which is not available on Termux. All code must remain compatible with pydantic v1. MCP servers are bundled (no external SDK/deps).

---

## 9. Deployment

### Installation (Termux one-shot)
```bash
curl -fsSL https://raw.githubusercontent.com/yourname/ClawVia/main/install.sh | bash
```
Installs: python, git, curl, termux-api → requests storage permissions → clones repo → installs Python deps → creates `.env` template.

### Starting
```bash
bash start_server.sh
```
- Acquires Termux wake lock (prevents Android from sleeping)
- Starts SSH if available
- Runs `python run.py` in auto-restart loop (restarts on crash)
- Logs to `logs/clawvia.log`

### Startup sequence (run.py)
1. Import all tool modules → registers tools in registry
2. `load_skills()` → discover and index SKILL.md files
3. Start FastAPI server (uvicorn, port 8001) in background thread
4. Start Scheduler in background thread (30s tick cycle)
5. Start Telegram bot polling on main thread (blocking)

---

## 10. Key Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Pydantic v1 only** | Termux lacks Rust compiler needed for v2 |
| **Bundled MCP servers** | Custom lightweight JSON-RPC, no external SDK deps |
| **Polling Telegram bot** | No webhook needed — easier on Android with dynamic IPs |
| **SQLite for everything** | Portable, no external DB server, WAL mode for concurrency |
| **In-process tool cache** | Avoids Redis/memcached overhead on Android |
| **Cheap Reflexion** | 1 short LLM call after errors, deduplicated by error hash |
| **Session-scoped todos** | Tied to conversations, not global |
| **Progressive skill disclosure** | Compact index in prompt, full bodies loaded on demand |
| **Recipe system** | Deterministic tool sequences for stable, repeatable workflows |
| **Adaptive dreaming** | Frequency adjusts based on usefulness of recent dreams |
| **Fail-closed security** | Missing `ALLOWED_USER_ID` rejects all; missing `API_SECRET_KEY` rejects API calls |
| **No async agent loop** | Synchronous loop with per-session threading.Lock — simpler, avoids async SQLite issues |

---

## 11. LLM Response Format

The agent expects the LLM to respond with **only** JSON (no other text):

```json
{
  "tool": "tool_name",
  "args": { "arg1": "value1", "arg2": "value2" },
  "thought": "Brief reasoning about what I'm doing and why"
}
```

To finish:
```json
{
  "tool": "finish",
  "args": { "output": "Final response to the user" },
  "thought": "Task is complete because..."
}
```

---

## 12. Self-Improvement Mechanisms

| Mechanism | Trigger | Effect |
|-----------|---------|--------|
| **Reflexion** | 2+ consecutive errors on same tool | Verbal reflection injected into next LLM call |
| **Pattern extraction** | Successful task with 5+ steps | Reusable strategy saved to `strategy_patterns` |
| **Profile learning** | Post-task + idle enrichment | User preferences stored in `user_profile` |
| **Skill creation** | Agent decides via `skill_create` tool | New SKILL.md written to disk |
| **Dream cycle** | Every ~6h (adaptive) | Background insights, notes, goals from recent activity |
| **Idle tasks** | 30min+ user inactivity | Error review, memory consolidation, skill audit, goal review, self-review |
| **Compaction** | Session > 16K tokens | Older messages summarized, indexed for cross-session search |
| **Cross-session recall** | FTS match during context build | Past conversation snippets surfaced in current context |

---

## 13. Testing & Debugging

- **Checkpoints:** `/checkpoints` command in Telegram — inspect agent state at any step
- **Reflections:** `/reflections` — view failure pattern memory
- **Audit log:** `audit_search` tool — search all tracked actions
- **Status:** `/status` — uptime, memory usage, active session
- **Logs:** `logs/clawvia.log` — structured logging with secret redaction
- **Database:** Direct SQLite access to `clawvia.db` for inspection

---

*Generated from full codebase analysis. Last updated: 2026-05-03.*
