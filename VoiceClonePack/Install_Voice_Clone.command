#!/usr/bin/env bash
# Easy Speech - 1-Click Voice Clone Installer for macOS
# Double-clickable in Finder (Zero terminal typing required)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/local/sbin:/Library/Frameworks/Python.framework/Versions/Current/bin:$PATH"

chmod +x "$SCRIPT_DIR/install_service.sh" "$SCRIPT_DIR/start_service.sh" 2>/dev/null || true
"$SCRIPT_DIR/install_service.sh"

echo ""
echo "========================================================"
echo "Setup is complete! You can now close this window."
echo "========================================================"
read -p "Press [Enter] to exit..."
