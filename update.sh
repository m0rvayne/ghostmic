#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════════
# Meeting Transcript MCP — Update runtime from repo
# Copies latest *.py files to install dir without re-downloading Whisper model
# ═══════════════════════════════════════════════════════════════════════════════

GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
say()  { printf "${CYAN}[update]${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}  ✅ %s${NC}\n" "$*"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$HOME/.meeting-transcript-mcp"

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

# Copy updated files
UPDATED=0
for f in server.py capture.py watcher.py setup-audio.sh requirements.txt; do
    if [[ -f "$SCRIPT_DIR/$f" ]]; then
        if ! diff -q "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f" &>/dev/null; then
            cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
            ok "Updated: $f"
            UPDATED=$((UPDATED + 1))
        fi
    fi
done

# Fix watcher Python path
VENV_PY="$INSTALL_DIR/.venv/bin/python3"
sed -i '' "s|PYTHON = .*|PYTHON = Path(\"$VENV_PY\")|" "$INSTALL_DIR/watcher.py" 2>/dev/null || true

# Install any new pip dependencies
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt" 2>/dev/null

if [[ "$UPDATED" -eq 0 ]]; then
    ok "Everything already up to date"
else
    ok "Updated $UPDATED file(s)"
    echo ""
    echo "  ⚠️  If watcher.py is running, restart it:"
    echo "     Kill: Ctrl+C or pkill -f watcher.py"
    echo "     Start: $VENV_PY $INSTALL_DIR/watcher.py"
fi
