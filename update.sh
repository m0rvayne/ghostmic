#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════════
# Meeting Transcript MCP — Update runtime from repo
# Copies latest files to install dir without re-downloading Whisper model
# ═══════════════════════════════════════════════════════════════════════════════

GREEN='\033[0;32m'; CYAN='\033[0;36m'; YELLOW='\033[1;33m'; NC='\033[0m'
say()  { printf "${CYAN}[update]${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}  ✅ %s${NC}\n" "$*"; }
warn() { printf "${YELLOW}  ⚠️  %s${NC}\n" "$*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.meeting-transcript-mcp"
LAUNCH_LABEL="com.meeting-transcript.watcher"

if [[ ! -d "$INSTALL_DIR" ]]; then
    echo "❌ Install dir not found: $INSTALL_DIR"
    echo "   Run install.sh first."
    exit 1
fi

say "Updating runtime from repo..."

# Pull latest if in a git repo
if [[ -d "$SCRIPT_DIR/.git" ]]; then
    cd "$SCRIPT_DIR"
    git pull origin main 2>/dev/null && ok "Pulled latest from GitHub" || ok "Already up to date"
fi

# Copy all project files (not just a hardcoded list)
UPDATED=0
for f in "$SCRIPT_DIR"/*.py "$SCRIPT_DIR"/*.sh "$SCRIPT_DIR"/*.swift "$SCRIPT_DIR"/requirements.txt; do
    [[ ! -f "$f" ]] && continue
    fname="$(basename "$f")"
    [[ "$fname" == "install.sh" ]] && continue  # don't overwrite install.sh in install dir
    if ! diff -q "$f" "$INSTALL_DIR/$fname" &>/dev/null 2>&1; then
        cp "$f" "$INSTALL_DIR/$fname"
        ok "Updated: $fname"
        UPDATED=$((UPDATED + 1))
    fi
done

# Install any new pip dependencies
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt" 2>/dev/null

if [[ "$UPDATED" -eq 0 ]]; then
    ok "Everything already up to date"
else
    ok "Updated $UPDATED file(s)"

    # Rebuild Swift binaries if source changed
    if command -v swiftc &>/dev/null; then
        mkdir -p "$INSTALL_DIR/.build"
        if [[ -f "$INSTALL_DIR/statusbar.swift" ]]; then
            say "Rebuilding menu bar indicator..."
            swiftc -O -framework AppKit "$INSTALL_DIR/statusbar.swift" -o "$INSTALL_DIR/.build/statusbar" 2>/dev/null && ok "Menu bar rebuilt"
        fi
        if [[ -f "$INSTALL_DIR/create-multi-output.swift" ]]; then
            swiftc -O -framework CoreAudio -framework CoreFoundation "$INSTALL_DIR/create-multi-output.swift" -o "$INSTALL_DIR/.build/create-multi-output" 2>/dev/null
        fi
    fi

    # Restart watcher
    say "Restarting watcher..."
    if launchctl kickstart -k "gui/$(id -u)/$LAUNCH_LABEL" 2>/dev/null; then
        ok "Watcher restarted"
    else
        warn "Could not restart watcher"
    fi

    # Restart menu bar indicator
    STATUSBAR_LABEL="com.meeting-transcript.statusbar"
    if launchctl kickstart -k "gui/$(id -u)/$STATUSBAR_LABEL" 2>/dev/null; then
        ok "Menu bar restarted"
    else
        warn "Could not restart menu bar indicator"
    fi
fi
