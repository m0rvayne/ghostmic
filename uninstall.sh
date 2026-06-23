#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════════
# ghostmic — Uninstaller
# ═══════════════════════════════════════════════════════════════════════════════

INSTALL_DIR="$HOME/.ghostmic"
CLAUDE_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
LAUNCH_LABEL="com.ghostmic.watcher"
PLIST_PATH="$HOME/Library/LaunchAgents/$LAUNCH_LABEL.plist"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
say()  { printf "${CYAN}[uninstall]${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}  ✅ %s${NC}\n" "$*"; }

cat << 'BANNER'

  ╔═══════════════════════════════════════════════════╗
  ║   ghostmic — Uninstaller             ║
  ╚═══════════════════════════════════════════════════╝

BANNER

# 1. Stop and remove LaunchAgent
say "Stopping watcher daemon and menu bar indicator..."
launchctl bootout "gui/$(id -u)/$LAUNCH_LABEL" 2>/dev/null || true
launchctl bootout "gui/$(id -u)/com.ghostmic.statusbar" 2>/dev/null || true
[[ -f "$PLIST_PATH" ]] && rm "$PLIST_PATH"
[[ -f "$HOME/Library/LaunchAgents/com.ghostmic.statusbar.plist" ]] && rm "$HOME/Library/LaunchAgents/com.ghostmic.statusbar.plist"
ok "LaunchAgents removed"

# 2. Remove MCP entry from Claude Desktop config
if [[ -f "$CLAUDE_CONFIG" ]]; then
    say "Removing MCP config from Claude Desktop..."
    UNINSTALL_PY="${INSTALL_DIR}/.venv/bin/python3"
    [[ ! -x "$UNINSTALL_PY" ]] && UNINSTALL_PY="python3"
    "$UNINSTALL_PY" -c "
import json, sys
try:
    with open('$CLAUDE_CONFIG') as f: config = json.load(f)
    if 'mcpServers' in config and 'ghostmic' in config['mcpServers']:
        del config['mcpServers']['ghostmic']
        with open('$CLAUDE_CONFIG', 'w') as f: json.dump(config, f, indent=2)
        print('  Removed ghostmic from Claude Desktop config')
    else:
        print('  No ghostmic entry found in config')
except Exception as e:
    print(f'  Could not update config: {e}', file=sys.stderr)
" 2>&1
    ok "Claude Desktop config cleaned"
fi

# 3. Ask about transcripts
if [[ -d "$INSTALL_DIR/transcripts" ]]; then
    TRANSCRIPT_COUNT=$(find "$INSTALL_DIR/transcripts" -name "*.txt" -not -name "meeting_transcript.txt" | wc -l | tr -d ' ')
    if [[ "$TRANSCRIPT_COUNT" -gt 0 ]]; then
        echo ""
        printf "  You have ${RED}%s${NC} saved meeting transcripts.\n" "$TRANSCRIPT_COUNT"
        printf "  Delete them? [y/N] "
        read -r DELETE_TRANSCRIPTS
        if [[ "$DELETE_TRANSCRIPTS" =~ ^[Yy]$ ]]; then
            rm -rf "$INSTALL_DIR/transcripts"
            ok "Transcripts deleted"
        else
            # Move transcripts out before deleting install dir
            SAVED="$HOME/Desktop/ghostmics-backup"
            mv "$INSTALL_DIR/transcripts" "$SAVED"
            ok "Transcripts saved to $SAVED"
        fi
    fi
fi

# 4. Remove install directory
if [[ -d "$INSTALL_DIR" ]]; then
    say "Removing $INSTALL_DIR..."
    rm -rf "$INSTALL_DIR"
    ok "Installation removed"
fi

cat << 'DONE'

  ╔═══════════════════════════════════════════════════╗
  ║         ✅ Meeting Transcript Uninstalled           ║
  ╚═══════════════════════════════════════════════════╝

  What was NOT removed (used by other apps):
  - BlackHole 2ch (brew uninstall blackhole-2ch)
  - "Zoom + Transcript" device (remove in Audio MIDI Setup)
  - Whisper model cache (~/.cache/huggingface/)

  Restart Claude Desktop (Cmd+Q) to apply config changes.

DONE
