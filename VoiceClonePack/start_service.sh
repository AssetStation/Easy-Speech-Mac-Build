#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"
export PYTORCH_ENABLE_MPS_FALLBACK=1

echo "========================================================"
echo "  EASY SPEECH: VOICE CLONE EXPANSION SERVICE (macOS)"
echo "========================================================"

unset PYTHONPATH
unset PYTHONHOME
unset PYTHONUSERSITE

PYTHON_EXE=""
for cand in "$DIR/.venv/bin/python" "$DIR/.venv/bin/python3" "$DIR/.python311/bin/python3" "$HOME/.easyspeech/python311/bin/python3" "$HOME/.easyspeech/venv/bin/python3"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then
        if "$cand" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)" >/dev/null 2>&1; then
            PYTHON_EXE="$cand"
            break
        fi
    fi
done

if [ -z "$PYTHON_EXE" ]; then
    echo "[!] ERROR: Dedicated Python 3.11 environment not found."
    echo "Please run './install_service.sh' first to set up the expansion pack."
    echo "(System Python versions like 3.12, 3.13, 3.14 are incompatible with bundled AI binaries)."
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
