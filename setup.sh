#!/usr/bin/env bash
set -euo pipefail

# Xylocopa — one-line installer (Linux + macOS)
#
# Usage (after cloning):
#   ./setup.sh
#
# Usage (curl one-liner — clones + installs):
#   curl -fsSL https://raw.githubusercontent.com/jianpengyao/xylocopa/master/setup.sh | bash

REPO="https://github.com/jianpengyao/xylocopa.git"
# XYLOCOPA_DIR is the canonical override; AGENTHIVE_DIR is accepted as a legacy alias.
INSTALL_DIR="${XYLOCOPA_DIR:-${AGENTHIVE_DIR:-$HOME/xylocopa-main}}"

# ── Ensure Node.js exists ────────────────────────────────────────────
if ! command -v node >/dev/null 2>&1; then
    echo "[!] Node.js not found."
    OS="$(uname -s)"
    if [ "$OS" = "Darwin" ]; then
        if command -v brew >/dev/null 2>&1; then
            echo "[+] Installing Node.js via Homebrew..."
            brew install node
        else
            echo "[x] Install Homebrew first: https://brew.sh"
            echo "    Then re-run this script."
            exit 1
        fi
    else
        echo "[+] Installing Node.js via apt..."
        sudo apt-get update -qq && sudo apt-get install -y -qq nodejs npm
    fi
fi

# ── Clone if running via curl (not already inside the repo) ──────────
if [ ! -f "$(pwd)/install.js" ]; then
    if [ -d "$INSTALL_DIR" ]; then
        echo "[+] Xylocopa directory exists: $INSTALL_DIR"
        cd "$INSTALL_DIR"
    else
        echo "[+] Cloning Xylocopa to $INSTALL_DIR..."
        git clone "$REPO" "$INSTALL_DIR"
        cd "$INSTALL_DIR"
    fi
fi

# ── Hand off to the cross-platform Node.js installer ─────────────────
exec node install.js
