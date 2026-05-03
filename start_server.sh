#!/data/data/com.termux/files/usr/bin/bash
# ──────────────────────────────────────────────────────────────
# ClawVia — Termux Startup Script
# Usage: bash start_server.sh  (or just: clawvia)
# ──────────────────────────────────────────────────────────────

CLAWVIA_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$CLAWVIA_DIR"

# ── Pre-flight checks ────────────────────────────────────────
# Ensure .env exists and has real values (not placeholders)
if [ ! -f .env ]; then
    echo ""
    echo "  ✗ No .env file found!"
    echo ""
    echo "  Run the installer to configure ClawVia:"
    echo "    curl -fsSL https://raw.githubusercontent.com/RavirajGaikar/clawvia/main/install.sh | bash"
    echo ""
    echo "  Or create .env manually with at minimum:"
    echo "    TELEGRAM_BOT_TOKEN=your_token"
    echo "    ALLOWED_USER_ID=your_id"
    echo "    NVIDIA_API_KEY=your_key"
    echo ""
    exit 1
fi

# Check for placeholder values
if grep -q 'your_.*_here' .env 2>/dev/null; then
    echo ""
    echo "  ! Your .env still has placeholder values."
    echo "  Edit it before starting:"
    echo "    clawvia --config"
    echo ""
    exit 1
fi

# Create logs directory
mkdir -p logs

echo ""
echo "  ═══════════════════════════════════════"
echo "  🦀 ClawVia Starting"
echo "  ═══════════════════════════════════════"
echo "  Directory: $CLAWVIA_DIR"

# Prevent phone from sleeping
if command -v termux-wake-lock &> /dev/null; then
    termux-wake-lock 2>/dev/null
    echo "  ✓ Wake lock acquired"
fi

# Start SSH if available
if command -v sshd &> /dev/null; then
    sshd 2>/dev/null || true
    echo "  ✓ SSH started"
fi

# ── Virtual environment setup (first-run) ─────────────────────
VENV_DIR="$CLAWVIA_DIR/venv"
MARKER_FILE="$VENV_DIR/.venv_ready"
REQ_HASH=""
if [ -f requirements.txt ]; then
    REQ_HASH=$(md5sum requirements.txt 2>/dev/null | cut -d' ' -f1 || shasum requirements.txt 2>/dev/null | cut -d' ' -f1)
fi

if [ ! -d "$VENV_DIR" ]; then
    echo "  ▶ First run — creating virtual environment..."
    python3 -m venv "$VENV_DIR"
    echo "  ✓ Virtual environment created"
fi

source "$VENV_DIR/bin/activate"
echo "  ✓ Virtual environment activated"

# Install/update requirements if marker is missing or requirements.txt changed
if [ ! -f "$MARKER_FILE" ] || [ "$(cat "$MARKER_FILE" 2>/dev/null)" != "$REQ_HASH" ]; then
    echo "  ▶ Installing dependencies..."
    pip install -r requirements.txt > /dev/null 2>&1
    echo "$REQ_HASH" > "$MARKER_FILE"
    echo "  ✓ Dependencies installed"
else
    echo "  ✓ Dependencies up to date"
fi

echo "  ✓ Starting ClawVia (auto-restart on crash)"
echo "    Press Ctrl+C to stop"
echo "  ═══════════════════════════════════════"
echo ""

# Auto-restart loop — restarts ClawVia if it crashes
while true; do
    python3 run.py 2>&1 | tee -a logs/clawvia.log
    EXIT_CODE=$?
    echo ""
    echo "  ! ClawVia exited with code $EXIT_CODE"
    if [ $EXIT_CODE -eq 0 ]; then
        echo "  ✓ Clean shutdown"
        break
    fi
    echo "  ↻ Restarting in 5 seconds... (Ctrl+C to stop)"
    sleep 5
done
