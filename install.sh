#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════════
# Meeting Transcript MCP — One-command installer
# Live Zoom transcription: BlackHole → Whisper AI → text
# Audio passthrough: hear Zoom through speakers/headphones, auto headphone switch
# ═══════════════════════════════════════════════════════════════════════════════

INSTALL_DIR="$HOME/.meeting-transcript-mcp"
CLAUDE_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
say()  { printf "${CYAN}[installer]${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}  ✅ %s${NC}\n" "$*"; }
warn() { printf "${YELLOW}  ⚠️  %s${NC}\n" "$*"; }

cat << 'BANNER'

  ╔═══════════════════════════════════════════════════╗
  ║   🎙️ Meeting Transcript MCP — Installer           ║
  ║   Live Zoom → Whisper AI → text transcription     ║
  ╚═══════════════════════════════════════════════════╝

BANNER

[[ "$(uname)" != "Darwin" ]] && { echo "❌ macOS only."; exit 1; }

# ── 0. Self-bootstrap: if run via curl|bash, clone repo first ────────────────
if [[ "${BASH_SOURCE[0]}" == "" ]] || [[ ! -f "$(dirname "${BASH_SOURCE[0]}")/capture.py" ]]; then
    say "Downloading repository..."
    REPO_DIR="/tmp/meeting-transcript-mcp-$$"
    if command -v git &>/dev/null; then
        git clone --depth 1 https://github.com/morvayne1/meeting-transcript-mcp.git "$REPO_DIR" 2>&1 | tail -1
    else
        # No git yet — use curl + tar from GitHub
        mkdir -p "$REPO_DIR"
        curl -fsSL https://github.com/morvayne1/meeting-transcript-mcp/archive/refs/heads/main.tar.gz \
            | tar -xz -C "$REPO_DIR" --strip-components=1
    fi
    ok "Downloaded to $REPO_DIR"
    SCRIPT_DIR="$REPO_DIR"
    # Re-exec from the downloaded copy
    exec bash "$SCRIPT_DIR/install.sh" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 1. Homebrew ──────────────────────────────────────────────────────────────
say "Checking Homebrew..."
if command -v brew &>/dev/null; then
    ok "Homebrew ready"
else
    say "Installing Homebrew (may ask for password)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    [[ -f "/opt/homebrew/bin/brew" ]] && eval "$(/opt/homebrew/bin/brew shellenv)"
    ok "Homebrew installed"
fi

# ── 2. BlackHole ─────────────────────────────────────────────────────────────
say "Installing BlackHole 2ch (virtual audio driver)..."
brew install blackhole-2ch 2>/dev/null || ok "Already installed"
ok "BlackHole 2ch ready"

# ── 3. Python ────────────────────────────────────────────────────────────────
say "Checking Python..."
PYTHON=""
for p in python3.12 python3.13 python3.14 python3; do
    if command -v "$p" &>/dev/null; then
        minor=$("$p" -c "import sys; print(sys.version_info.minor)" 2>/dev/null || echo 0)
        [[ "$minor" -ge 10 ]] && { PYTHON="$(command -v "$p")"; break; }
    fi
done
if [[ -z "$PYTHON" ]]; then
    brew install python@3.12
    PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
fi
ok "Python: $($PYTHON --version)"

# ── 3b. Claude CLI ───────────────────────────────────────────────────────────
say "Checking Claude CLI..."
if command -v claude &>/dev/null; then
    ok "Claude CLI already installed ($(claude --version 2>/dev/null || echo 'installed'))"
else
    say "Installing Claude CLI..."
    curl -fsSL https://claude.ai/install.sh | sh
    # Add to PATH for this session
    export PATH="$HOME/.local/bin:$HOME/.claude/bin:$PATH"
    # Persist PATH
    if [[ -f "$HOME/.zshrc" ]]; then
        grep -q '.local/bin' "$HOME/.zshrc" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
    fi
    if command -v claude &>/dev/null; then
        ok "Claude CLI installed"
    else
        warn "Claude CLI installed but may need terminal restart to appear in PATH"
    fi
fi

# ── 4. Copy files ────────────────────────────────────────────────────────────
say "Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR/transcripts"
for f in server.py capture.py watcher.py setup-audio.sh requirements.txt; do
    [[ -f "$SCRIPT_DIR/$f" ]] && cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/"
done
chmod +x "$INSTALL_DIR/setup-audio.sh" 2>/dev/null || true

# ── 5. Python venv + deps ───────────────────────────────────────────────────
say "Installing Python dependencies..."
$PYTHON -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
ok "Dependencies installed"

# ── 6. Whisper model ─────────────────────────────────────────────────────────
say "Downloading Whisper AI model 'small' (~500MB, one-time)..."
say "This may take 3-7 minutes depending on internet speed..."
"$INSTALL_DIR/.venv/bin/python3" -c "
import sys, os
os.environ['HF_HUB_DISABLE_PROGRESS_BARS'] = '0'
from faster_whisper import WhisperModel
print('  Downloading model files...', flush=True)
WhisperModel('small', device='cpu', compute_type='int8')
print('  Done.', flush=True)
" 2>&1
ok "Whisper model ready"

# ── 7. Fix watcher Python path ──────────────────────────────────────────────
VENV_PY="$INSTALL_DIR/.venv/bin/python3"
sed -i '' "s|PYTHON = .*|PYTHON = Path(\"$VENV_PY\")|" "$INSTALL_DIR/watcher.py" 2>/dev/null || true

# ── 8. Auto-create Multi-Output Device ───────────────────────────────────────
say "Setting up audio routing (Multi-Output Device)..."
bash "$INSTALL_DIR/setup-audio.sh" 2>&1 || warn "Audio setup had issues — run manually: bash $INSTALL_DIR/setup-audio.sh"

# ── 9. Claude Desktop config ────────────────────────────────────────────────
say "Configuring Claude Desktop..."
MCP_ENTRY="{\"meeting-transcript\":{\"command\":\"$VENV_PY\",\"args\":[\"$INSTALL_DIR/server.py\"]}}"

if [[ -f "$CLAUDE_CONFIG" ]]; then
    cp "$CLAUDE_CONFIG" "${CLAUDE_CONFIG}.bak-$(date +%Y%m%d-%H%M%S)"
    $PYTHON << PYEOF
import json
with open("$CLAUDE_CONFIG") as f: config = json.load(f)
config.setdefault("mcpServers", {})
new = json.loads('$MCP_ENTRY')
for k, v in new.items():
    if k not in config["mcpServers"]:
        config["mcpServers"][k] = v
        print(f"  Added: {k}")
    else:
        print(f"  Skipped (exists): {k}")
with open("$CLAUDE_CONFIG", "w") as f: json.dump(config, f, indent=2)
PYEOF
else
    mkdir -p "$(dirname "$CLAUDE_CONFIG")"
    echo "{\"mcpServers\": $MCP_ENTRY}" > "$CLAUDE_CONFIG"
fi
ok "Claude Desktop configured"

# ── 10. LaunchAgent — auto-start watcher on login ────────────────────────────
say "Setting up watcher auto-start (LaunchAgent)..."
PLIST_PATH="$HOME/Library/LaunchAgents/com.meeting-transcript.watcher.plist"
WATCHER_LOG="$INSTALL_DIR/watcher.log"

cat > "$PLIST_PATH" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.meeting-transcript.watcher</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_PY</string>
        <string>$INSTALL_DIR/watcher.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$INSTALL_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$WATCHER_LOG</string>
    <key>StandardErrorPath</key>
    <string>$WATCHER_LOG</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
PLISTEOF

# Load the agent (unload first if exists)
launchctl bootout "gui/$(id -u)/com.meeting-transcript.watcher" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || launchctl load "$PLIST_PATH" 2>/dev/null
ok "Watcher auto-start configured (survives reboot)"

# ── Done ─────────────────────────────────────────────────────────────────────
cat << SUMMARY

  ╔═══════════════════════════════════════════════════╗
  ║         ✅ Meeting Transcript Installed!            ║
  ╚═══════════════════════════════════════════════════╝

  Location: $INSTALL_DIR

  ┌──────────────────────────────────────────────────────────┐
  │  ONE STEP — In Zoom:                                      │
  │                                                            │
  │  Settings → Audio → Speaker → "Zoom + Transcript"          │
  │                                                            │
  │  Full quality audio (48kHz stereo) to your speakers        │
  │  + BlackHole capture for Whisper transcription.             │
  │                                                            │
  │  💡 Switched headphones? Run:                               │
  │     bash $INSTALL_DIR/setup-audio.sh                       │
  └──────────────────────────────────────────────────────────┘

  Watcher is running as a LaunchAgent — it auto-starts on login
  and auto-detects Zoom meetings. No manual launch needed.

  Transcripts saved to: $INSTALL_DIR/transcripts/

  In Claude Desktop (after Cmd+Q → reopen):
    "what was discussed?" — reads transcript
    "show conference map" — builds mind map

  Diagnostics:
    launchctl list | grep meeting-transcript
    tail -20 $INSTALL_DIR/watcher.log

SUMMARY
