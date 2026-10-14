#!/usr/bin/env bash
# Easy Speech - Voice Clone Expansion Pack Installer for macOS
# Compatible with Apple Silicon (M1/M2/M3/M4) and Intel Macs

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================================"
echo "  EASY SPEECH: VOICE CLONE EXPANSION INSTALLER (macOS)"
echo "========================================================"
echo ""

# 1. Verify macOS
OS_NAME="$(uname -s)"
if [ "$OS_NAME" != "Darwin" ]; then
    echo "[!] Error: This script is intended for macOS only (detected: $OS_NAME)."
    exit 1
fi

ARCH="$(uname -m)"
echo "[*] System Architecture: $ARCH"
if [ "$ARCH" = "arm64" ]; then
    echo "[✓] Apple Silicon (M1/M2/M3/M4) detected! Metal (MPS) GPU acceleration supported."
else
    echo "[*] Intel Mac detected. Diffusion will run with multi-threaded CPU acceleration."
fi
echo ""

# 2. Locate suitable Python 3
PYTHON_BIN=""
for cand in "/opt/homebrew/bin/python3.11" "/opt/homebrew/bin/python3.10" "/opt/homebrew/bin/python3" \
            "/usr/local/bin/python3.11" "/usr/local/bin/python3.10" "/usr/local/bin/python3" \
            "$(command -v python3.11 2>/dev/null || true)" \
            "$(command -v python3.10 2>/dev/null || true)" \
            "$(command -v python3 2>/dev/null || true)"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then
        VER="$($cand -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || true)"
        MAJOR="$(echo "$VER" | cut -d. -f1)"
        MINOR="$(echo "$VER" | cut -d. -f2)"
        if [ "$MAJOR" = "3" ] && [ "$MINOR" -ge 9 ] && [ "$MINOR" -le 12 ]; then
            PYTHON_BIN="$cand"
            break
        fi
    fi
done

if [ -z "$PYTHON_BIN" ]; then
    echo "[*] Python 3 not found on system."
    echo "[*] Automatically downloading portable standalone Python 3.11 for macOS ($ARCH)..."
    if [ "$ARCH" = "arm64" ]; then
        PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20240814/cpython-3.11.9+20240814-aarch64-apple-darwin-install_only.tar.gz"
    else
        PY_URL="https://github.com/astral-sh/python-build-standalone/releases/download/20240814/cpython-3.11.9+20240814-x86_64-apple-darwin-install_only.tar.gz"
    fi
    curl -L -o "$SCRIPT_DIR/python-mac.tar.gz" "$PY_URL"
    mkdir -p "$SCRIPT_DIR/_py_tmp"
    tar -xzf "$SCRIPT_DIR/python-mac.tar.gz" -C "$SCRIPT_DIR/_py_tmp"
    rm "$SCRIPT_DIR/python-mac.tar.gz"
    rm -rf "$SCRIPT_DIR/.venv"
    mv "$SCRIPT_DIR/_py_tmp/python" "$SCRIPT_DIR/.venv"
    rm -rf "$SCRIPT_DIR/_py_tmp"
    chmod -R +x "$SCRIPT_DIR/.venv/bin"
    PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python3"
    echo "[✓] Portable Python 3.11 configured in $SCRIPT_DIR/.venv"
else
    echo "[✓] Using System Python: $PYTHON_BIN ($($PYTHON_BIN --version))"
fi
echo ""

# 3. Check for FFmpeg (optional but recommended)
if command -v ffmpeg >/dev/null 2>&1; then
    echo "[✓] System FFmpeg found at $(command -v ffmpeg)"
else
    echo "[i] Tip: System FFmpeg not found in PATH."
    echo "    imageio-ffmpeg will be used automatically, or you can install FFmpeg via:"
    echo "        brew install ffmpeg"
fi
echo ""

# 4. Create dedicated virtual environment
VENV_DIR="$SCRIPT_DIR/.venv"
if [ -d "$VENV_DIR" ]; then
    echo "[*] Existing virtual environment found at $VENV_DIR"
else
    echo "[*] Creating virtual environment at $VENV_DIR..."
    "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python3"
VENV_PIP="$VENV_DIR/bin/pip"

if [ ! -x "$VENV_PYTHON" ]; then
    echo "[!] Failed to locate venv python executable at $VENV_PYTHON"
    exit 1
fi

echo "[*] Upgrading pip, setuptools, and wheel..."
"$VENV_PYTHON" -m pip install --upgrade pip setuptools wheel
"$VENV_PYTHON" -m pip install "numpy<2"

# 5. Install PyTorch with native Apple Metal (MPS) support
echo ""
echo "[*] Installing PyTorch (with Apple Silicon Metal/MPS support)..."
"$VENV_PIP" install "torch>=2.5.0" torchvision "torchaudio>=2.5.0"

# 6. Install F5-TTS, Faster-Whisper, and Audio Processing packages
echo ""
echo "[*] Installing F5-TTS, Faster-Whisper, SoundFile, and audio processing tools..."
"$VENV_PIP" install f5-tts faster-whisper soundfile imageio_ffmpeg pydub

# 7. Make helper scripts executable
chmod +x "$SCRIPT_DIR/start_service.sh" 2>/dev/null || true
chmod +x "$SCRIPT_DIR/install_service.sh" 2>/dev/null || true
chmod +x "$VENV_PYTHON" 2>/dev/null || true

# 8. Verify installation
echo ""
echo "[*] Verifying installation..."
"$VENV_PYTHON" -c "
import sys
import torch
import soundfile
import f5_tts
import faster_whisper

mps = hasattr(torch.backends, 'mps') and torch.backends.mps.is_available()
cuda = torch.cuda.is_available()
dev = 'CUDA' if cuda else ('Apple Metal (MPS)' if mps else 'CPU')
print(f'[✓] All dependencies imported successfully!')
print(f'[✓] Active Compute Acceleration: {dev}')
"

echo ""
echo "========================================================"
echo "  INSTALLATION COMPLETED SUCCESSFULLY!"
echo "========================================================"
echo "Easy Speech in Premiere Pro / After Effects will now automatically"
echo "spawn this Voice Clone service in the background whenever needed."
echo ""
echo "You can also manually start the service at any time by running:"
echo "    ./start_service.sh"
echo "========================================================"
