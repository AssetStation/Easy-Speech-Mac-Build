#!/usr/bin/env bash
# Easy Speech - Voice Clone Expansion Service Launcher for macOS

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python3"
SERVICE_SCRIPT="$SCRIPT_DIR/service.py"

if [ ! -f "$SERVICE_SCRIPT" ]; then
    SERVICE_SCRIPT="$SCRIPT_DIR/../backend/voice_clone_service.py"
fi

if [ ! -x "$VENV_PYTHON" ]; then
    echo "========================================================"
    echo "  Easy Speech Voice Clone Expansion Service"
    echo "========================================================"
    echo "[!] Error: Virtual environment not found at $SCRIPT_DIR/.venv"
    echo "    Please run the installation script first:"
    echo "        ./install_service.sh"
    echo "========================================================"
    exit 1
fi

chmod +x "$VENV_PYTHON" 2>/dev/null || true

echo "[*] Starting Easy Speech Voice Clone Service..."
exec "$VENV_PYTHON" "$SERVICE_SCRIPT"
