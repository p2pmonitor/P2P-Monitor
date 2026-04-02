#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
VERSION=$(grep -m1 'P2P Monitor v' "$SCRIPT_DIR/p2p_monitor.py" | grep -oP 'v[\d.]+')

echo "========================================"
echo " P2P Monitor ${VERSION} — Installer"
echo "========================================"

echo ""
echo "[1/4] Installing system dependencies..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3 \
    python3-tk \
    python3-pip \
    xdotool \
    x11-utils \
    imagemagick

echo ""
echo "[2/4] Installing Python dependencies..."
pip3 install pystray pillow tkcalendar --break-system-packages

echo ""
echo "[3/4] Installing files to ~/.p2p_monitor/..."
INSTALL_DIR="$HOME/.p2p_monitor"
mkdir -p "$INSTALL_DIR/py"
mkdir -p "$INSTALL_DIR/ui"


cp "$SCRIPT_DIR/p2p_monitor.py"    "$INSTALL_DIR/"
cp "$SCRIPT_DIR/py/__init__.py"    "$INSTALL_DIR/py/"
cp "$SCRIPT_DIR/py/reader.py"      "$INSTALL_DIR/py/"
cp "$SCRIPT_DIR/py/history.py"     "$INSTALL_DIR/py/"
cp "$SCRIPT_DIR/py/config.py"      "$INSTALL_DIR/py/"
cp "$SCRIPT_DIR/py/util.py"        "$INSTALL_DIR/py/"
cp "$SCRIPT_DIR/py/discord.py"     "$INSTALL_DIR/py/"
cp "$SCRIPT_DIR/py/screenshot.py"  "$INSTALL_DIR/py/"
cp "$SCRIPT_DIR/py/paint.py"       "$INSTALL_DIR/py/"
cp "$SCRIPT_DIR/py/watcher.py"     "$INSTALL_DIR/py/"
cp "$SCRIPT_DIR/ui/__init__.py"      "$INSTALL_DIR/ui/"
cp "$SCRIPT_DIR/ui/monitor_tab.py"   "$INSTALL_DIR/ui/"
cp "$SCRIPT_DIR/ui/status_tab.py"    "$INSTALL_DIR/ui/"
cp "$SCRIPT_DIR/ui/history_tab.py"   "$INSTALL_DIR/ui/"
cp "$SCRIPT_DIR/ui/settings_tab.py"  "$INSTALL_DIR/ui/"

echo ""
echo "[4/4] Creating desktop shortcut..."
DESKTOP_FILE="$HOME/Desktop/P2P-Monitor.desktop"
mkdir -p "$HOME/Desktop"
cat > "$DESKTOP_FILE" << DESK
[Desktop Entry]
Version=1.0
Type=Application
Name=P2P Monitor
Comment=DreamBot P2P Master AI Log Monitor
Exec=python3 ${INSTALL_DIR}/p2p_monitor.py
Icon=utilities-terminal
Terminal=false
Categories=Utility;
DESK

chmod +x "$DESKTOP_FILE"

if command -v gio &>/dev/null; then
    gio set "$DESKTOP_FILE" metadata::trusted true 2>/dev/null || true
fi

echo ""
echo "========================================"
echo " ✅ Installation complete!"
echo ""
echo "  Installed to:  ~/.p2p_monitor/"
echo "  Config:        ~/.p2p_monitor/config.json"
echo "  History:       ~/.p2p_monitor/history/"
echo "  Desktop:       ~/Desktop/P2P-Monitor.desktop"
echo ""
echo "  DreamBot Logs: /home/debian/DreamBot/Logs"
echo "  Each subfolder = one account (set in Settings)"
echo ""
echo "  To run:   python3 ~/.p2p_monitor/p2p_monitor.py"
echo "  To update: run app → Settings → Check for Update"
echo "========================================"
