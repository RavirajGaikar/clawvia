#!/data/data/com.termux/files/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# ClawVia — One-Command Installer for Termux
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/RavirajGaikar/clawvia/main/install.sh | bash
#
# What it does:
#   1. Installs system packages (python, git, curl, termux-api)
#   2. Requests storage permissions
#   3. Clones the repo (or pulls latest if already cloned)
#   4. Installs Python dependencies
#   5. Runs an interactive setup wizard for API keys
#   6. Creates the `clawvia` CLI command
#   7. Sets up auto-start on boot via Termux:Boot
#
# Idempotent — safe to re-run. Existing .env is never overwritten.
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Constants ─────────────────────────────────────────────────────────
REPO_URL="https://github.com/RavirajGaikar/clawvia.git"
REPO_DIR="$HOME/clawvia"
INSTALL_DIR="$REPO_DIR"
VERSION="3.0"

# ── Colors & helpers ──────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[1;36m'
BOLD='\033[1m'
DIM='\033[2m'
RESET='\033[0m'

say()  { printf "${CYAN}  ▶ %s${RESET}\n" "$*"; }
ok()   { printf "${GREEN}  ✓ %s${RESET}\n" "$*"; }
warn() { printf "${YELLOW}  ! %s${RESET}\n" "$*" >&2; }
err()  { printf "${RED}  ✗ %s${RESET}\n" "$*" >&2; }
dim()  { printf "${DIM}    %s${RESET}\n" "$*"; }

# Prompt with a default value. Usage: ask "Prompt" "default" → sets REPLY
# Always reads from /dev/tty so it works in pipe mode (curl | bash).
ask() {
    local prompt="$1"
    local default="${2:-}"
    if [ -n "$default" ]; then
        printf "${BOLD}  → %s ${DIM}[%s]${RESET}: " "$prompt" "$default"
    else
        printf "${BOLD}  → %s${RESET}: " "$prompt"
    fi
    read -r REPLY < /dev/tty
    REPLY="${REPLY:-$default}"
}

# Prompt yes/no. Usage: ask_yn "Enable X?" "y" → returns 0 (yes) or 1 (no)
ask_yn() {
    local prompt="$1"
    local default="${2:-n}"
    local hint="y/n"
    [ "$default" = "y" ] && hint="Y/n" || hint="y/N"
    printf "${BOLD}  → %s ${DIM}(%s)${RESET}: " "$prompt" "$hint"
    read -r REPLY < /dev/tty
    REPLY="${REPLY:-$default}"
    case "$REPLY" in
        [yY]*) return 0 ;;
        *)     return 1 ;;
    esac
}

# ── Banner ────────────────────────────────────────────────────────────
banner() {
    printf "\n"
    printf "${CYAN}${BOLD}"
    cat << 'BANNER'
    ╔═══════════════════════════════════════════════╗
    ║                                               ║
    ║       🦀  C L A W V I A                      ║
    ║       Your AI Assistant for Android            ║
    ║                                               ║
    ╚═══════════════════════════════════════════════╝
BANNER
    printf "${RESET}"
    printf "${DIM}    v%s • One-command installer for Termux${RESET}\n\n" "$VERSION"
}

# ── Phase 1: Environment Check ───────────────────────────────────────
phase_environment() {
    printf "\n${BOLD}  ━━━ Phase 1/6: Environment Check ━━━${RESET}\n\n"

    if [ ! -d "/data/data/com.termux" ]; then
        warn "This installer is designed for Termux on Android."
        warn "Continuing anyway, but some features may not work."
    else
        ok "Termux environment detected"
    fi

    ok "Interactive input ready"
}

# ── Phase 2: System Packages ─────────────────────────────────────────
phase_packages() {
    printf "\n${BOLD}  ━━━ Phase 2/6: System Packages ━━━${RESET}\n\n"

    say "Updating Termux package index..."
    pkg update -y > /dev/null 2>&1 || true
    pkg upgrade -y > /dev/null 2>&1 || true
    ok "Package index updated"

    say "Installing: python git curl termux-api..."
    pkg install -y python git curl termux-api > /dev/null 2>&1
    ok "System packages installed"

    # Storage access
    if command -v termux-setup-storage > /dev/null 2>&1; then
        say "Requesting storage access..."
        dim "Allow the permission dialog if it pops up"
        termux-setup-storage 2>/dev/null || warn "Storage permission not granted — file tools may fail"
        ok "Storage access configured"
    fi
}

# ── Phase 3: Clone & Dependencies ────────────────────────────────────
phase_clone() {
    printf "\n${BOLD}  ━━━ Phase 3/6: Download & Install ━━━${RESET}\n\n"

    if [ ! -d "$REPO_DIR/.git" ]; then
        say "Cloning ClawVia..."
        git clone --depth=1 "$REPO_URL" "$REPO_DIR" 2>/dev/null
        ok "Repository cloned → $REPO_DIR"
    else
        say "ClawVia already installed — pulling latest..."
        (cd "$REPO_DIR" && git pull --ff-only 2>/dev/null) || warn "git pull skipped (local changes?)"
        ok "Repository updated"
    fi

    cd "$INSTALL_DIR"

    # Python dependencies — installed automatically on first `clawvia` run via venv
    ok "Python dependencies will be installed on first start"

    # MCP servers config
    if [ ! -f mcp_servers.json ]; then
        say "Writing MCP servers config..."
        cat > mcp_servers.json <<'MCPEOF'
{
  "servers": {
    "fetch": {
      "transport": "stdio",
      "command": "python",
      "args": ["-m", "mcp_servers.fetch_server"]
    }
  }
}
MCPEOF
        ok "MCP config created"
    else
        ok "MCP config exists — keeping it"
    fi

    # Ensure logs directory
    mkdir -p logs
}

# ── Phase 4: Interactive Setup Wizard ─────────────────────────────────
phase_wizard() {
    printf "\n${BOLD}  ━━━ Phase 4/6: Configuration Wizard ━━━${RESET}\n\n"

    # Check if .env already has real values
    if [ -f "$INSTALL_DIR/.env" ]; then
        # Check if it has non-placeholder values
        local has_token has_userid has_nvidia
        has_token=$(grep -c 'TELEGRAM_BOT_TOKEN=.' "$INSTALL_DIR/.env" 2>/dev/null || echo 0)
        has_placeholder=$(grep -c 'your_.*_here' "$INSTALL_DIR/.env" 2>/dev/null || echo 0)

        if [ "$has_token" -gt 0 ] && [ "$has_placeholder" -eq 0 ]; then
            ok "Existing configuration found (.env)"
            dim "To reconfigure, delete $INSTALL_DIR/.env and re-run the installer"
            return 0
        else
            warn "Found .env with placeholder values — running wizard"
        fi
    fi

    printf "${DIM}    Let's set up your ClawVia instance.${RESET}\n"
    printf "${DIM}    You'll need a few API keys ready.${RESET}\n\n"

    # ── Step 1: Telegram Bot Token ──
    printf "  ${BOLD}Step 1/4: Telegram Bot Token${RESET}\n"
    dim "Create a bot: open Telegram → search @BotFather → /newbot"
    dim "Copy the token it gives you (looks like: 123456:ABC-DEF...)"
    printf "\n"

    local tg_token=""
    while [ -z "$tg_token" ]; do
        ask "Paste your bot token"
        tg_token="$REPLY"
        if [ -z "$tg_token" ]; then
            err "Token cannot be empty"
        elif ! echo "$tg_token" | grep -qE '^[0-9]+:'; then
            warn "That doesn't look like a Telegram token (expected format: 123456:ABC...)"
            ask_yn "Use it anyway?" "n" || tg_token=""
        fi
    done
    ok "Telegram token saved"
    printf "\n"

    # ── Step 2: Telegram User ID ──
    printf "  ${BOLD}Step 2/4: Your Telegram User ID${RESET}\n"
    dim "Find your ID: open Telegram → search @userinfobot → /start"
    dim "It will reply with your numeric ID (e.g., 123456789)"
    printf "\n"

    local tg_userid=""
    while [ -z "$tg_userid" ]; do
        ask "Paste your numeric user ID"
        tg_userid="$REPLY"
        if [ -z "$tg_userid" ]; then
            err "User ID cannot be empty"
        elif ! echo "$tg_userid" | grep -qE '^[0-9]+$'; then
            err "User ID must be a number (got: $tg_userid)"
            tg_userid=""
        fi
    done
    ok "User ID saved"
    printf "\n"

    # ── Step 3: NVIDIA NIM API Key ──
    printf "  ${BOLD}Step 3/4: NVIDIA NIM API Key${RESET}\n"
    dim "Get a free key: https://build.nvidia.com"
    dim "Sign up → API Catalog → Get API Key"
    printf "\n"

    local nvidia_key=""
    while [ -z "$nvidia_key" ]; do
        ask "Paste your NVIDIA API key"
        nvidia_key="$REPLY"
        if [ -z "$nvidia_key" ]; then
            err "API key cannot be empty"
        fi
    done
    ok "NVIDIA API key saved"
    printf "\n"

    # ── Step 4: Optional Features ──
    printf "  ${BOLD}Step 4/4: Optional Features${RESET}\n\n"

    local groq_key=""
    if ask_yn "Enable Speech-to-Text? (lets you send voice messages)" "n"; then
        printf "\n"
        dim "Groq provides fast Whisper transcription for free."
        dim "Get a key: https://console.groq.com → API Keys → Create"
        printf "\n"

        while [ -z "$groq_key" ]; do
            ask "Paste your Groq API key"
            groq_key="$REPLY"
            if [ -z "$groq_key" ]; then
                err "Groq API key cannot be empty (skip with Ctrl+C and re-run without STT)"
            fi
        done
        ok "Speech-to-Text enabled"
    else
        dim "Speech-to-Text skipped (you can enable it later in .env)"
    fi
    printf "\n"

    # ── Write .env ──
    say "Writing configuration..."

    cat > "$INSTALL_DIR/.env" <<ENVEOF
# ═══════════════════════════════════════════════════════════════
# ClawVia Configuration
# Generated by installer on $(date '+%Y-%m-%d %H:%M:%S')
# ═══════════════════════════════════════════════════════════════

# ── Required ──────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN=${tg_token}
ALLOWED_USER_ID=${tg_userid}
NVIDIA_API_KEY=${nvidia_key}

# ── LLM Model ────────────────────────────────────────────────
# NVIDIA_MODEL=moonshotai/kimi-k2-thinking
# VISION_MODEL=mistralai/mistral-large-3-675b-instruct-2512

# ── Speech-to-Text (Groq Whisper) ────────────────────────────
GROQ_API_KEY=${groq_key}

# ── Server ────────────────────────────────────────────────────
# API_HOST=0.0.0.0
# API_PORT=8001
# API_SECRET_KEY=

# ── Paths ─────────────────────────────────────────────────────
# BASE_PATH=/storage/emulated/0/Download
# DB_PATH=clawvia.db

# ── Agent Tuning ──────────────────────────────────────────────
# MAX_AGENT_STEPS=25
# LLM_TEMPERATURE=0.2
# LLM_MAX_TOKENS=2048
# LLM_TIMEOUT=120
# TIMEZONE=Asia/Kolkata
ENVEOF

    ok "Configuration written → .env"
}

# ── Phase 5: CLI Command ─────────────────────────────────────────────
phase_cli() {
    printf "\n${BOLD}  ━━━ Phase 5/6: CLI Command Setup ━━━${RESET}\n\n"

    # The clawvia launcher script is already in the repo
    chmod +x "$INSTALL_DIR/clawvia"

    # Symlink into Termux PATH
    local bin_dir="${PREFIX:-/data/data/com.termux/files/usr}/bin"
    if [ -d "$bin_dir" ]; then
        ln -sf "$INSTALL_DIR/clawvia" "$bin_dir/clawvia"
        ok "Command installed: clawvia"
        dim "Run 'clawvia --help' to see all options"
    else
        warn "Could not find Termux bin directory ($bin_dir)"
        warn "Add $INSTALL_DIR to your PATH manually"
    fi
}

# ── Phase 6: Auto-Start on Boot ───────────────────────────────────────
phase_boot() {
    printf "\n${BOLD}  ━━━ Phase 6/6: Auto-Start Setup ━━━${RESET}\n\n"

    local boot_dir="$HOME/.termux/boot"
    mkdir -p "$boot_dir"

    cat > "$boot_dir/clawvia.sh" <<BOOTEOF
#!/data/data/com.termux/files/usr/bin/bash
# ClawVia — auto-start on device boot
# Requires the Termux:Boot app from F-Droid

# Wait a moment for network to be ready
sleep 10

# Acquire wake lock to prevent Android from killing Termux
termux-wake-lock

# Start ClawVia in background (via start_server.sh which handles venv)
cd "$INSTALL_DIR"
nohup bash start_server.sh >> /dev/null 2>&1 &

# Optional: start SSH for remote access
command -v sshd > /dev/null 2>&1 && sshd 2>/dev/null
BOOTEOF

    chmod +x "$boot_dir/clawvia.sh"
    ok "Boot script created → ~/.termux/boot/clawvia.sh"
    dim "Install 'Termux:Boot' from F-Droid to enable auto-start"
}

# ── Completion ────────────────────────────────────────────────────────
phase_done() {
    printf "\n"
    printf "${GREEN}${BOLD}"
    cat << 'DONE'
    ╔═══════════════════════════════════════════════╗
    ║                                               ║
    ║    ✅  ClawVia installed successfully!         ║
    ║                                               ║
    ╚═══════════════════════════════════════════════╝
DONE
    printf "${RESET}\n"

    printf "${BOLD}  Quick Reference:${RESET}\n\n"
    printf "    ${GREEN}clawvia${RESET}           Start ClawVia\n"
    printf "    ${GREEN}clawvia --stop${RESET}    Stop the running instance\n"
    printf "    ${GREEN}clawvia --status${RESET}  Check if running\n"
    printf "    ${GREEN}clawvia --logs${RESET}    View live logs\n"
    printf "    ${GREEN}clawvia --config${RESET}  Edit configuration\n"
    printf "\n"
    printf "  ${BOLD}In Telegram:${RESET}\n"
    printf "    ${GREEN}/connect${RESET}            Add Notion, GitHub, Calendar, etc.\n"
    printf "    ${GREEN}/skills${RESET}             Manage agent capabilities\n"
    printf "\n"
    printf "${DIM}    Config:  %s/.env${RESET}\n" "$INSTALL_DIR"
    printf "${DIM}    Logs:    %s/logs/${RESET}\n" "$INSTALL_DIR"
    printf "${DIM}    Boot:    ~/.termux/boot/clawvia.sh${RESET}\n"
    printf "\n"
    printf "  ${CYAN}Open Telegram and send a message to your bot! 🚀${RESET}\n\n"
}

# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

main() {
    banner
    phase_environment
    phase_packages
    phase_clone
    phase_wizard
    phase_cli
    phase_boot
    phase_done
}

main "$@"
