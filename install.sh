#!/usr/bin/env bash
# ==============================================================================
# Music Vault Manager — Automated Dependency & Installation Script
# Supports: Termux (Native), PRoot-Distro (Debian/Ubuntu), and Linux ARM64/x86_64
# ==============================================================================
set -e

echo "=========================================================="
echo "    Installing Music Vault Manager for Termux / Android   "
echo "=========================================================="

# 1. Detect Package Manager
if command -v pkg >/dev/null 2>&1; then
    echo "[+] Detected native Termux environment (pkg)."
    PKG_MGR="pkg"
    pkg update -y
    pkg install -y python python-pip python-cryptography libjpeg-turbo ffmpeg termux-api
elif command -v apt-get >/dev/null 2>&1; then
    echo "[+] Detected Debian/PRoot environment (apt-get)."
    PKG_MGR="apt"
    apt-get update -y
    apt-get install -y python3 python3-pip python3-mutagen python3-requests \
        python3-cryptography python3-googleapi python3-google-auth \
        python3-google-auth-oauthlib python3-google-auth-httplib2 python3-pytest
else
    echo "[!] Unrecognized package manager. Ensuring pip is available..."
fi

# 2. Install Python Package
echo "[+] Installing Music Vault Manager package and entrypoint..."
if command -v pip3 >/dev/null 2>&1; then
    pip3 install --break-system-packages -e . || pip3 install -e .
elif command -v pip >/dev/null 2>&1; then
    pip install --break-system-packages -e . || pip install -e .
fi

# 3. Secure Configuration Directory
CONFIG_DIR="$HOME/.config/musicvault"
mkdir -p "$CONFIG_DIR"
chmod 700 "$CONFIG_DIR"

if [ ! -f "$CONFIG_DIR/.env" ] && [ -f ".env.example" ]; then
    cp .env.example "$CONFIG_DIR/.env"
    chmod 600 "$CONFIG_DIR/.env"
    echo "[+] Created default .env in $CONFIG_DIR/.env (permissions 0600)"
fi

echo "=========================================================="
echo " [✓] Installation complete!"
echo "     Launch interactive menu:   musicvault"
echo "     Run setup wizard:          musicvault setup"
echo "     Run 10-track benchmark:    musicvault benchmark --size 10"
echo "=========================================================="
