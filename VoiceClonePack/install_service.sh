#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

# Ensure Homebrew and python.org standard install paths are available in GUI shell
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"
export PYTORCH_ENABLE_MPS_FALLBACK=1

# Sanitize Python environment variables to prevent external Python versions (e.g., Python 3.14) from interfering
unset PYTHONPATH
unset PYTHONHOME
unset PYTHONUSERSITE

NO_PAUSE=0
if [ "$1" = "--no-pause" ]; then
    NO_PAUSE=1
fi

echo "======================================================================"
echo "      EASY SPEECH PRO: VOICE CLONE PACK INSTALLER (macOS)"
echo "   Local AI Voice Cloning Setup Engine for Premiere Pro & AE"
echo "======================================================================"
echo ""

# Clear any stale completion marker
rm -f "$DIR/.installed_success" 2>/dev/null

# Fast Check: If existing environment is already 100% healthy, avoid re-downloading!
HEALTHY_PY=""
for cand_py in "$DIR/.venv/bin/python" "$HOME/.easyspeech/venv/bin/python"; do
    if [ -f "$cand_py" ]; then
        HEALTHY_PY="$cand_py"
        break
    fi
done

if [ -n "$HEALTHY_PY" ]; then
    echo "[PROGRESS:15] Checking existing AI environment integrity..."
    echo "[*] Checking health of existing Voice Clone installation..."
    if "$HEALTHY_PY" -c "import sys, torch, f5_tts, faster_whisper; assert sys.version_info[:2] == (3, 11), 'Python 3.11 required'" >/dev/null 2>&1; then
        echo "[HEALTH_CHECK:PASSED]"
        echo "[PROGRESS:100] All AI packages are healthy and verified!"
        echo "[✓] Fast Health Check Passed: All AI packages are already installed and 100% functional!"
        echo "[✓] Zero re-downloading required. Your Voice Clone Pack is ready."
        echo "1.0.0" > "$DIR/.installed_success"
        echo ""
        echo "======================================================================"
        echo "              HEALTH CHECK VERIFIED: ALL PACKAGES HEALTHY"
        echo "======================================================================"
        echo "PyTorch, F5-TTS, and Whisper engines are 100% intact and verified."
        echo "Zero re-downloading required. Your Voice Clone Pack is ready."
        echo "======================================================================"
        echo ""
        if [ "$NO_PAUSE" != "1" ]; then
            read -p "Press Enter to exit..."
        fi
        exit 0
    else
        echo "[PROGRESS:20] Missing or damaged packages detected. Proceeding with automated repair..."
        echo "[!] Missing or damaged packages detected. Proceeding with automated repair..."
        echo ""
    fi
fi

# -----------------------------------------------------------------------
# Step 0: Pre-Flight Network Connectivity Check
# -----------------------------------------------------------------------
echo "[PROGRESS:5] Checking network connectivity..."
echo "[*] Step 0/4: Checking internet connection..."
ONLINE=0
for test_url in "https://pypi.org" "https://download.pytorch.org" "https://www.google.com"; do
    if curl -s --head --connect-timeout 3 "$test_url" >/dev/null 2>&1; then
        ONLINE=1
        break
    fi
done

if [ "$ONLINE" -ne 1 ]; then
    echo "[!] ERROR: No active internet connection detected."
    echo "    Voice Clone Pack requires an active internet connection to download dependencies."
    echo "    Please check your network connection and retry setup."
    if [ "$NO_PAUSE" != "1" ]; then
        read -p "Press Enter to exit..."
    fi
    exit 2
fi
echo "[✓] Internet connection is active."
echo ""

# -----------------------------------------------------------------------
# Step 0b: Pre-Flight Free Disk Space Check (Requires >= 8 GB)
# -----------------------------------------------------------------------
echo "[PROGRESS:7] Checking free disk space..."
echo "[*] Checking available storage on drive..."
FREE_KB=$(df -k -P "$DIR" | awk 'NR==2 {print $4}')
if [ -n "$FREE_KB" ] && [ "$FREE_KB" -lt 8388608 ]; then
    echo "[!] ERROR: Insufficient disk space."
    echo "    Voice Clone Pack requires at least 8 GB of free disk space for AI neural engines."
    echo "    Available: $((FREE_KB / 1024 / 1024)) GB"
    if [ "$NO_PAUSE" != "1" ]; then
        read -p "Press Enter to exit..."
    fi
    exit 3
fi
echo "[✓] Sufficient disk space available (>= 8 GB)."
echo ""

# -----------------------------------------------------------------------
# Step 1: Check Mac Hardware
# -----------------------------------------------------------------------
echo "[PROGRESS:10] Checking Mac hardware..."
echo "[*] Step 1/4: Checking hardware acceleration..."
CPU_BRAND="$(sysctl -n machdep.cpu.brand_string 2>/dev/null || echo '')"
if echo "$CPU_BRAND" | grep -qi "Apple"; then
    echo "[✓] Detected Apple Silicon: $CPU_BRAND"
    echo "[✓] Metal Performance Shaders (MPS) hardware acceleration available."
else
    echo "[i] Detected Intel Mac: $CPU_BRAND"
    echo "[i] Neural models will run with multi-threaded CPU acceleration."
fi
echo ""

# -----------------------------------------------------------------------
# Step 2: Check or Auto-Install Standalone Python
# -----------------------------------------------------------------------
echo "[PROGRESS:25] Checking Python environment..."
echo "[*] Step 2/4: Checking Python 3.11 installation..."
PYTHON_EXE=""

for cand in "$DIR/.python311/bin/python3" "$HOME/.easyspeech/python311/bin/python3" "/opt/homebrew/bin/python3.11" "/usr/local/bin/python3.11" "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then
        if "$cand" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)" >/dev/null 2>&1; then
            PYTHON_EXE="$cand"
            break
        fi
    fi
done

if [ -z "$PYTHON_EXE" ]; then
    echo "[!] No existing Python 3.11 detected on your Mac."
    if which brew >/dev/null 2>&1; then
        echo "[PROGRESS:35] Installing Python 3.11 via Homebrew..."
        brew install python@3.11 2>/dev/null
        for b_cand in "/opt/homebrew/bin/python3.11" "/usr/local/bin/python3.11"; do
            if [ -n "$b_cand" ] && [ -x "$b_cand" ]; then
                if "$b_cand" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)" >/dev/null 2>&1; then
                    PYTHON_EXE="$b_cand"
                    break
                fi
            fi
        done
    fi
fi

# Fallback for standard non-developer Mac video editors: Use bundled or download isolated standalone Python 3.11 (Zero permissions / No sudo required)
if [ -z "$PYTHON_EXE" ]; then
    ARCH_RAW="$(uname -m)"
    if [ "$ARCH_RAW" = "arm64" ]; then
        PY_ARCH="aarch64"
    else
        PY_ARCH="x86_64"
    fi

    INSTALL_TARGET="$DIR/.python311"
    mkdir -p "$INSTALL_TARGET" 2>/dev/null
    if [ ! -w "$INSTALL_TARGET" ]; then
        INSTALL_TARGET="$HOME/.easyspeech/python311"
        mkdir -p "$INSTALL_TARGET" 2>/dev/null
    fi

    # Priority 1: Check for local bundled Python archive inside VoiceClonePack (zero download required)
    LOCAL_TAR=""
    for cand_tar in \
        "$DIR/cpython-3.11.9-${PY_ARCH}-apple-darwin.tar.gz" \
        "$DIR/cpython-3.11.9+20240726-${PY_ARCH}-apple-darwin-install_only.tar.gz" \
        "$DIR/python-3.11-${PY_ARCH}.tar.gz" \
        "$DIR/python-mac.tar.gz" \
        "$DIR/../bin/mac/cpython-3.11.9-${PY_ARCH}-apple-darwin.tar.gz"; do
        if [ -f "$cand_tar" ]; then
            LOCAL_TAR="$cand_tar"
            break
        fi
    done

    if [ -n "$LOCAL_TAR" ]; then
        echo "[PROGRESS:35] Extracting local bundled Python 3.11 for Mac..."
        echo "[✓] Local bundled Python archive detected: $(basename "$LOCAL_TAR")"
        echo "[*] Extracting portable runtime into $INSTALL_TARGET (zero internet download required)..."
        rm -rf "$INSTALL_TARGET"/* 2>/dev/null
        tar -xzf "$LOCAL_TAR" -C "$INSTALL_TARGET" --strip-components 1
        chmod -R +x "$INSTALL_TARGET/bin" 2>/dev/null || true
        xattr -cr "$INSTALL_TARGET" 2>/dev/null || true
        if [ -x "$INSTALL_TARGET/bin/python3" ]; then
            if "$INSTALL_TARGET/bin/python3" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)" >/dev/null 2>&1; then
                PYTHON_EXE="$INSTALL_TARGET/bin/python3"
                echo "[✓] Local bundled Python 3.11 extracted and verified successfully."
            fi
        fi
    fi

    # Priority 2: If no local bundled archive was found, download standalone runtime
    if [ -z "$PYTHON_EXE" ]; then
        echo "[PROGRESS:30] Downloading official portable Python 3.11 for macOS..."
        echo "[*] Automatically downloading isolated Python 3.11 runtime (~18 MB, no admin password required)..."
        PY_URL="https://github.com/indygreg/python-build-standalone/releases/download/20240726/cpython-3.11.9%2B20240726-${PY_ARCH}-apple-darwin-install_only.tar.gz"
        TMP_TAR="/tmp/cpython_setup_${RANDOM}.tar.gz"

        echo "[*] Downloading standalone runtime..."
        if curl -fSL --retry 3 --connect-timeout 10 -o "$TMP_TAR" "$PY_URL"; then
            echo "[*] Extracting portable runtime into $INSTALL_TARGET..."
            rm -rf "$INSTALL_TARGET"/* 2>/dev/null
            tar -xzf "$TMP_TAR" -C "$INSTALL_TARGET" --strip-components 1
            rm -f "$TMP_TAR"
            chmod -R +x "$INSTALL_TARGET/bin" 2>/dev/null || true
            xattr -cr "$INSTALL_TARGET" 2>/dev/null || true
            if [ -x "$INSTALL_TARGET/bin/python3" ]; then
                if "$INSTALL_TARGET/bin/python3" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3, 11) else 1)" >/dev/null 2>&1; then
                    PYTHON_EXE="$INSTALL_TARGET/bin/python3"
                    echo "[✓] Standalone Python 3.11 configured successfully (isolated, 0 permissions required)."
                fi
            fi
        else
            rm -f "$TMP_TAR"
            echo "[!] Automatic standalone download failed due to network loss."
        fi
    fi
fi

if [ -z "$PYTHON_EXE" ]; then
    echo "[!] Error: Python 3.10, 3.11, or 3.12 is required (macOS default 3.9 is incompatible with F5-TTS)."
    echo "    Please install Python 3.11 from https://www.python.org/downloads/macos/ or run 'brew install python@3.11'."
    if [ "$NO_PAUSE" != "1" ]; then
        read -p "Press Enter to exit..."
    fi
    exit 1
fi

echo "[✓] Using Python: $PYTHON_EXE"
"$PYTHON_EXE" --version
echo ""

# -----------------------------------------------------------------------
# Step 3: Check & Auto-Extract Any Downloaded Model Archives
# -----------------------------------------------------------------------
echo "[PROGRESS:55] Checking model archives..."
echo "[*] Step 3/4: Checking for downloaded model packages..."
mkdir -p "$DIR/models"

if [ -f "$DIR/models.zip" ]; then
    echo "[*] Extracting models.zip into models folder..."
    unzip -o "$DIR/models.zip" -d "$DIR/models" >/dev/null 2>&1
    echo "[✓] Successfully extracted models.zip."
fi

if [ -f "$DIR/VoiceClonePack_models.zip" ]; then
    echo "[*] Extracting VoiceClonePack_models.zip into models folder..."
    unzip -o "$DIR/VoiceClonePack_models.zip" -d "$DIR/models" >/dev/null 2>&1
    echo "[✓] Successfully extracted VoiceClonePack_models.zip."
fi

if [ -d "$DIR/models/hub/models--SWivid--F5-TTS" ]; then
    echo "[✓] F5-TTS offline neural models detected."
else
    echo "[i] Model weights will be automatically fetched on first voice generation."
fi
echo ""

# -----------------------------------------------------------------------
# Step 4: Create Virtual Environment and Install Dependencies
# -----------------------------------------------------------------------
echo "[PROGRESS:65] Creating isolated AI environment (.venv)..."
echo "[*] Step 4/4: Setting up isolated AI environment (.venv)..."

VENV_DIR="$DIR/.venv"
if [ ! -w "$DIR" ]; then
    VENV_DIR="$HOME/.easyspeech/venv"
    mkdir -p "$VENV_DIR" 2>/dev/null
fi

if [ ! -f "$VENV_DIR/bin/python" ]; then
    if [ -d "$VENV_DIR" ]; then
        echo "[*] Cleaning incomplete virtual environment..."
        rm -rf "$VENV_DIR" 2>/dev/null
    fi
    echo "[*] Creating virtual environment at $VENV_DIR ..."
    "$PYTHON_EXE" -m venv --clear "$VENV_DIR"
    if [ $? -ne 0 ]; then
        echo "[!] Failed to create virtual environment."
        if [ "$NO_PAUSE" != "1" ]; then
            read -p "Press Enter to exit..."
        fi
        exit 1
    fi
    chmod -R +x "$VENV_DIR/bin" 2>/dev/null || true
    xattr -cr "$VENV_DIR" 2>/dev/null || true
    echo "[✓] Virtual environment created."
else
    chmod -R +x "$VENV_DIR/bin" 2>/dev/null || true
    xattr -cr "$VENV_DIR" 2>/dev/null || true
    echo "[✓] Existing virtual environment detected."
fi

VENV_PY="$VENV_DIR/bin/python"

if ! "$VENV_PY" -m pip --version >/dev/null 2>&1; then
    echo "[*] Installing pip into virtual environment..."
    "$VENV_PY" -m ensurepip --default-pip >/dev/null 2>&1 || true
fi

echo ""
echo "[*] Upgrading pip, setuptools, and wheel..."
"$VENV_PY" -m pip install --upgrade pip setuptools wheel --retries 10 --timeout 60 2>/dev/null || true

echo ""
PYTORCH_ATTEMPTS=0
while [ $PYTORCH_ATTEMPTS -lt 3 ]; do
    PYTORCH_ATTEMPTS=$((PYTORCH_ATTEMPTS + 1))
    echo "[PROGRESS:75] Installing PyTorch with Metal acceleration (takes ~10-15 mins)..."
    echo "[*] Installing PyTorch with Metal MPS acceleration (attempt $PYTORCH_ATTEMPTS of 3)..."
    echo "    (This package is ~2 GB and typically takes 10-15 minutes depending on your internet speed)"
    if "$VENV_PY" -m pip install torch torchvision torchaudio --no-compile --retries 15 --timeout 180; then
        break
    fi
    if [ $PYTORCH_ATTEMPTS -lt 3 ]; then
        echo "[!] Connection momentarily dropped. Resuming download in 5 seconds..."
        sleep 5
    else
        echo "[!] ERROR: Failed to install PyTorch after multiple attempts. Network connection may have dropped."
        if [ "$NO_PAUSE" != "1" ]; then
            read -p "Press Enter to exit..."
        fi
        exit 2
    fi
done

echo ""
F5_ATTEMPTS=0
while [ $F5_ATTEMPTS -lt 3 ]; do
    F5_ATTEMPTS=$((F5_ATTEMPTS + 1))
    echo "[PROGRESS:88] Installing F5-TTS Neural Diffusion and Whisper..."
    echo "[*] Installing F5-TTS, Faster-Whisper, soundfile, and audio libraries (attempt $F5_ATTEMPTS of 3)..."
    if "$VENV_PY" -m pip install f5-tts faster-whisper soundfile imageio_ffmpeg pydub --no-compile --retries 15 --timeout 180; then
        break
    fi
    if [ $F5_ATTEMPTS -lt 3 ]; then
        echo "[!] Connection momentarily dropped. Resuming download in 5 seconds..."
        sleep 5
    else
        echo "[!] ERROR: Failed to install F5-TTS dependencies after multiple attempts. Download interrupted."
        if [ "$NO_PAUSE" != "1" ]; then
            read -p "Press Enter to exit..."
        fi
        exit 2
    fi
done

echo ""
echo "[*] Applying neural voice cloning stability patch..."
if [ -f "$DIR/patch_f5_tts.pyc" ]; then
    "$VENV_PY" "$DIR/patch_f5_tts.pyc" || true
else
    "$VENV_PY" "$DIR/patch_f5_tts.py" || true
fi

echo ""
echo "[PROGRESS:95] Verifying AI runtime engine..."
echo "[*] Verifying all installed AI packages and portable FFmpeg..."
find "$VENV_DIR" -name "ffmpeg*" -type f -exec chmod +x {} + 2>/dev/null || true
find "$VENV_DIR" -name "ffmpeg*" -type f -exec xattr -cr {} + 2>/dev/null || true
"$VENV_PY" -c "import torch, f5_tts, faster_whisper, imageio_ffmpeg; print('[✓] AI packages verified. FFmpeg binary:', imageio_ffmpeg.get_ffmpeg_exe())"
if [ $? -ne 0 ]; then
    echo "[!] ERROR: Package verification failed. One or more packages are incomplete."
    if [ "$NO_PAUSE" != "1" ]; then
        read -p "Press Enter to exit..."
    fi
    exit 1
fi

# Create verified completion marker file
echo "1.0.0" > "$DIR/.installed_success"

echo ""
echo "[PROGRESS:100] Installation Complete!"
echo "======================================================================"
echo "              INSTALLATION COMPLETED SUCCESSFULLY!"
echo "======================================================================"
echo "Voice Clone Pack is now ready on your Mac."
echo ""
echo "Next Steps:"
echo " 1. You can launch './start_service.sh' to start the service manually, OR"
echo " 2. Open Easy Speech Pro inside Premiere Pro / After Effects."
echo "    Easy Speech Pro will automatically detect and launch this service for you!"
echo "======================================================================"
echo ""
if [ "$NO_PAUSE" != "1" ]; then
    read -p "Press Enter to exit..."
fi
exit 0
