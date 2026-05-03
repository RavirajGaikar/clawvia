<div align="center">
  <h1>🦀 ClawVia</h1>
  <p><b>Your Powerful, On-Device AI Assistant for Android</b></p>
  <p><i>Running natively on Termux, accessible anywhere via Telegram.</i></p>

  <p>
    <a href="#highlights">Highlights</a> •
    <a href="#quick-start">Quick Start</a> •
    <a href="#operator-quick-refs">Usage</a> •
    <a href="#configuration">Configuration</a> •
    <a href="#architecture">Architecture</a>
  </p>
</div>

---

## 🌟 Highlights

- **📱 Termux-Native** — Designed to run entirely on your Android device through Termux. No complex cloud deployment required.
- **💬 Telegram Interface** — Interact with ClawVia seamlessly through a Telegram Bot. It's like chatting with a super-powered friend.
- **🔌 MCP Integrations** — Model Context Protocol support allows ClawVia to securely connect with external services like Notion, Google Calendar, and more.
- **🛠️ First-Class Tools** — Equipped with tools for scheduling, media handling, local memory, and system security.
- **🧠 Advanced Agentic Core** — Driven by customizable prompts (`soul.md`, `context.md`) and built to learn, adapt, and assist you autonomously.
- **🔄 Auto-healing & Resilient** — Built-in auto-restart loops and update commands keep your agent alive and up-to-date.

---

## 🚀 Quick Start

Getting started with ClawVia is just one command away. Open your Termux app and run the automated installer:

```bash
curl -fsSL https://raw.githubusercontent.com/RavirajGaikar/clawvia/main/install.sh | bash
```

This script will:
1. Set up the necessary Termux packages.
2. Clone the repository.
3. Configure your Python virtual environment.
4. Prepare the `.env` file for you to configure.

---

## ⚙️ Configuration

The installation script includes an interactive setup wizard that will automatically prompt you for the necessary keys. Have these ready:

1. **Telegram Bot Token** — Create a bot via [@BotFather](https://t.me/BotFather) on Telegram.
2. **Telegram User ID** — Get your numeric ID from [@userinfobot](https://t.me/userinfobot) (this ensures only you can talk to the agent).
3. **NVIDIA API Key** — Sign up for a free key at [build.nvidia.com](https://build.nvidia.com).

*If you ever need to manually update your keys or enable additional features (like Groq for Voice Messages), just run `clawvia --config` to edit the `.env` file.*

---

## 🕹️ Operator Quick Refs

ClawVia comes with a global CLI tool that acts as your control plane. From anywhere in Termux, you can use the `clawvia` command.

### CLI Commands

- `clawvia` — Start the ClawVia server (runs with auto-restart on crash).
- `clawvia --status` — Check if the agent is currently running, along with its PID and Uptime.
- `clawvia --stop` — Safely stop the running instance.
- `clawvia --logs` — Tail the live log output to see what the agent is thinking and doing.
- `clawvia --config` — Open the `.env` configuration file in your editor.
- `clawvia --update` — Pull the latest code from GitHub and automatically flag dependencies for a reinstall.

---

## 🏗️ Architecture

ClawVia's architecture is modular and designed for extensibility:

- **`agent.py` & `main.py`** — The brain of the operation, managing LLM interactions and state.
- **`telegram_bot.py`** — The primary user interface layer, routing messages to and from the agent.
- **`integrations/` & `mcp_servers/`** — Pluggable modules for external API connectivity (e.g., Notion, Calendar).
- **`skills/` & `tools/`** — Discrete capabilities that the agent can utilize to perform actions on the device or web.
- **`scheduler.py`** — Handles cron-like jobs and delayed actions requested by the user.

---

<div align="center">
  <i>"Exfoliate the complexity, keep the claw sharp."</i>
</div>
