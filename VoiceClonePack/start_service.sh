#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"
export PYTORCH_ENABLE_MPS_FALLBACK=1

echo "========================================================"
echo "  EASY SPEECH: VOICE CLONE EXPANSION SERVICE (macOS)"
echo "========================================================"

PYTHON_EXE=""
if [ -f "$DIR/.venv/bin/python" ]; then
    PYTHON_EXE="$DIR/.venv/bin/python"
elif [ -f "$DIR/.venv/bin/python3" ]; then
    PYTHON_EXE="$DIR/.venv/bin/python3"
elif [ -f "$DIR/.python311/bin/python3" ]; then
    PYTHON_EXE="$DIR/.python311/bin/python3"
elif [ -f "$HOME/.easyspeech/python311/bin/python3" ]; then
    PYTHON_EXE="$HOME/.easyspeech/python311/bin/python3"
elif [ -f "$HOME/.easyspeech/venv/bin/python3" ]; then
    PYTHON_EXE="$HOME/.easyspeech/venv/bin/python3"
else
    PYTHON_EXE="$(which python3 2>/dev/null || echo '')"
fi

if [ -z "$PYTHON_EXE" ]; then
    echo "[!] ERROR: Python virtual environment not found."
    echo "Please run './install_service.sh' first to set up the expansion pack."
    echo ""
    read -p "Press Enter to exit..."
    exit 1
fi

echo "[*] Using Python: $PYTHON_EXE"
echo "[*] Starting Voice Clone Service on http://127.0.0.1:8765 ..."
echo "[*] Keep this window open while using Voice Cloning in Premiere Pro / After Effects."
echo ""
if [ -f "$DIR/service.pyc" ]; then
    "$PYTHON_EXE" "$DIR/service.pyc"
else
    "$PYTHON_EXE" "$DIR/service.py"
fi
if [ $? -ne 0 ]; then
    echo ""
    echo "[!] Service stopped with an error."
    read -p "Press Enter to exit..."
fi
