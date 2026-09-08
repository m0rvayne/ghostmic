#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════════
# ghostmic — One-command installer
# Silent Zoom transcription: CoreAudio Tap -> whisper.cpp -> Qwen3 LLM -> Claude
# ═══════════════════════════════════════════════════════════════════════════════

INSTALL_DIR="$HOME/.ghostmic"
CLAUDE_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
LAUNCH_LABEL="com.ghostmic.watcher"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
say()  { printf "${CYAN}[installer]${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}  ✅ %s${NC}\n" "$*"; }
warn() { printf "${YELLOW}  ⚠️  %s${NC}\n" "$*"; }
err()  { printf "${RED}  ❌ %s${NC}\n" "$*"; }

cat << 'BANNER'

  ╔═══════════════════════════════════════════════════╗
  ║   🎙️ ghostmic — Installer           ║
  ║   Live Zoom → Whisper AI → text transcription     ║
  ╚═══════════════════════════════════════════════════╝

BANNER

[[ "$(uname)" != "Darwin" ]] && { err "macOS only."; exit 1; }

# ── 0. Self-bootstrap: if run via curl|bash, clone repo first ────────────────
if [[ "${BASH_SOURCE[0]}" == "" ]] || [[ ! -f "$(dirname "${BASH_SOURCE[0]}")/capture.py" ]]; then
    say "Downloading repository..."
    REPO_DIR="/tmp/ghostmic-$$"
    trap 'rm -rf "$REPO_DIR" 2>/dev/null' EXIT
    if command -v git &>/dev/null; then
        git clone --depth 1 https://github.com/m0rvayne/ghostmic.git "$REPO_DIR" 2>&1 | tail -1
    else
        mkdir -p "$REPO_DIR"
        curl -fsSL https://github.com/m0rvayne/ghostmic/archive/refs/heads/main.tar.gz \
            | tar -xz -C "$REPO_DIR" --strip-components=1
    fi
    # Verify download integrity
    if [[ ! -f "$REPO_DIR/capture.py" ]] || [[ ! -f "$REPO_DIR/server.py" ]]; then
        err "Download incomplete — missing critical files"
        exit 1
    fi
    ok "Downloaded to $REPO_DIR"
    SCRIPT_DIR="$REPO_DIR"
    trap - EXIT  # clear trap, re-exec will handle its own cleanup
    exec bash "$SCRIPT_DIR/install.sh" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── 0b. Integrity ────────────────────────────────────────────────────────────
# What this catches: a truncated download, a corrupted mirror, a file swapped
# after the fact. What it does NOT catch: a tampered repository, since an
# attacker able to change capture.py can change checksums.txt in the same
# commit. The real check is the commit hash printed below — compare it against
# github.com/m0rvayne/ghostmic/commits/main if that matters to you.
if [[ -f "$SCRIPT_DIR/checksums.txt" ]]; then
    say "Verifying file integrity..."
    if (cd "$SCRIPT_DIR" && shasum -a 256 -c checksums.txt >/dev/null 2>&1); then
        ok "All files match checksums.txt"
    else
        err "Integrity check FAILED — a file does not match checksums.txt"
        (cd "$SCRIPT_DIR" && shasum -a 256 -c checksums.txt 2>&1 | grep -v ': OK$' || true)
        err "Refusing to install. Re-download, or run tools/gen-checksums.sh if you edited files locally."
        exit 1
    fi
else
    warn "No checksums.txt — skipping integrity check"
fi

INSTALLING_COMMIT="$(cd "$SCRIPT_DIR" && git rev-parse --short HEAD 2>/dev/null || echo unknown)"
say "Installing commit: $INSTALLING_COMMIT"

# ── 1. Homebrew ──────────────────────────────────────────────────────────────
say "Checking Homebrew..."
if command -v brew &>/dev/null; then
    ok "Homebrew ready"
else
    say "Installing Homebrew (may ask for password)..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Support both Apple Silicon and Intel paths
    if [[ -f "/opt/homebrew/bin/brew" ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -f "/usr/local/bin/brew" ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
    ok "Homebrew installed"
fi

# ── 2. CoreAudio Tap (replaces BlackHole — no virtual driver needed) ─────────
say "Building audio capture tool..."
command -v swiftc &>/dev/null || { err "Xcode Command Line Tools required: xcode-select --install"; exit 1; }
# BlackHole is no longer required — CoreAudio Process Taps capture app audio directly

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
    export PATH="$HOME/.local/bin:$HOME/.claude/bin:$PATH"
    if [[ -f "$HOME/.zshrc" ]]; then
        grep -q '.local/bin' "$HOME/.zshrc" 2>/dev/null || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.zshrc"
    fi
    if command -v claude &>/dev/null; then
        ok "Claude CLI installed"
    else
        warn "Claude CLI installed but may need terminal restart to appear in PATH"
    fi
fi

# ── 4. Stop old watcher before copying files ─────────────────────────────────
launchctl bootout "gui/$(id -u)/$LAUNCH_LABEL" 2>/dev/null || true

# ── 5. Copy files ────────────────────────────────────────────────────────────
say "Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR/transcripts"
for f in server.py capture.py watcher.py requirements.txt update.sh process-audio-tap.swift statusbar.swift zoom-participants.swift; do
    [[ -f "$SCRIPT_DIR/$f" ]] && cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/"
done
# Compile CoreAudio tap binary
mkdir -p "$INSTALL_DIR/.build"
if swiftc -O -framework CoreAudio -framework AudioToolbox -framework AppKit "$INSTALL_DIR/process-audio-tap.swift" -o "$INSTALL_DIR/.build/process-audio-tap" 2>&1; then
    ok "Audio capture tool compiled (CoreAudio Process Taps)"
else
    err "Failed to compile audio capture tool"
    exit 1
fi
# Compile participant detection binary
if swiftc -O -framework AppKit "$INSTALL_DIR/zoom-participants.swift" -o "$INSTALL_DIR/.build/zoom-participants" 2>&1; then
    ok "Participant detection compiled (Accessibility API)"
else
    say "Warning: participant detection not compiled (speaker names unavailable)"
fi

# ── 5b. Record what was installed ────────────────────────────────────────────
# Without this there is no way to tell a running install from the repo it came
# from — a deploy can sit months behind the source with nothing to show it.
cat > "$INSTALL_DIR/build-info.json" <<BUILDEOF
{"commit": "$INSTALLING_COMMIT", "installed_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
BUILDEOF
ok "Build recorded: $INSTALLING_COMMIT"

# ── 6. Python venv + deps ───────────────────────────────────────────────────
say "Installing Python dependencies..."
$PYTHON -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
ok "Dependencies installed"

# ── 7. whisper.cpp (brew) ────────────────────────────────────────────────────
say "Checking whisper.cpp..."
if command -v whisper-cli &>/dev/null; then
    ok "whisper-cli already installed"
else
    say "Installing whisper.cpp..."
    brew install whisper-cpp
    ok "whisper-cli installed"
fi

# ── 7b. Whisper model (ggml) ────────────────────────────────────────────────
WHISPER_MODEL="$INSTALL_DIR/models/ggml-large-v3-turbo.bin"
if [[ -f "$WHISPER_MODEL" ]]; then
    ok "Whisper model ready (large-v3-turbo)"
else
    say "Downloading whisper model 'large-v3-turbo' (~1.5GB, one-time)..."
    mkdir -p "$INSTALL_DIR/models"
    if curl -L -C - --progress-bar -o "$WHISPER_MODEL" \
        https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin; then
        ok "Whisper model ready"
    else
        err "Model download failed. Check disk space (~2GB needed) and internet."
        exit 1
    fi
fi

# ── 8. No audio setup needed ────────────────────────────────────────────────
ok "CoreAudio Process Taps — no audio routing setup needed"

# ── 9. Claude Desktop config ────────────────────────────────────────────────
say "Configuring Claude Desktop..."
VENV_PY="$INSTALL_DIR/.venv/bin/python3"

if [[ -f "$CLAUDE_CONFIG" ]]; then
    cp "$CLAUDE_CONFIG" "${CLAUDE_CONFIG}.bak-$(date +%Y%m%d-%H%M%S)"
    "$INSTALL_DIR/.venv/bin/python3" << PYEOF
import json, sys

config_path = "$CLAUDE_CONFIG"
venv_py = "$VENV_PY"
server_py = "$INSTALL_DIR/server.py"

try:
    with open(config_path) as f:
        config = json.load(f)
except (json.JSONDecodeError, ValueError):
    print("  Existing config was malformed — creating fresh config")
    config = {}

config.setdefault("mcpServers", {})
config["mcpServers"]["ghostmic"] = {
    "command": venv_py,
    "args": [server_py]
}
print(f"  Set: ghostmic -> {server_py}")

with open(config_path, "w") as f:
    json.dump(config, f, indent=2)
PYEOF
else
    mkdir -p "$(dirname "$CLAUDE_CONFIG")"
    "$INSTALL_DIR/.venv/bin/python3" -c "
import json
config = {'mcpServers': {'ghostmic': {'command': '$VENV_PY', 'args': ['$INSTALL_DIR/server.py']}}}
with open('$CLAUDE_CONFIG', 'w') as f: json.dump(config, f, indent=2)
"
fi
ok "Claude Desktop configured"

# ── 10. LaunchAgent — auto-start watcher on login ────────────────────────────
say "Setting up watcher auto-start (LaunchAgent)..."
PLIST_PATH="$HOME/Library/LaunchAgents/$LAUNCH_LABEL.plist"
WATCHER_LOG="$INSTALL_DIR/watcher.log"

cat > "$PLIST_PATH" << PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LAUNCH_LABEL</string>
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

launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH" 2>/dev/null || launchctl load "$PLIST_PATH" 2>/dev/null
ok "Watcher auto-start configured (survives reboot)"

# ── 11. Menu bar indicator ──────────────────────────────────────────────────
say "Building menu bar indicator..."
STATUSBAR_LABEL="com.ghostmic.statusbar"
STATUSBAR_BIN="$INSTALL_DIR/.build/statusbar"
if command -v swiftc &>/dev/null; then
    mkdir -p "$INSTALL_DIR/.build"
    if swiftc -O -framework AppKit "$INSTALL_DIR/statusbar.swift" -o "$STATUSBAR_BIN" 2>&1; then
        STATUSBAR_PLIST="$HOME/Library/LaunchAgents/$STATUSBAR_LABEL.plist"
        launchctl bootout "gui/$(id -u)/$STATUSBAR_LABEL" 2>/dev/null || true
        cat > "$STATUSBAR_PLIST" << SBPLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$STATUSBAR_LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$STATUSBAR_BIN</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
SBPLIST
        launchctl bootstrap "gui/$(id -u)" "$STATUSBAR_PLIST" 2>/dev/null || launchctl load "$STATUSBAR_PLIST" 2>/dev/null
        ok "Menu bar indicator installed (shows recording status)"
    else
        warn "Could not compile menu bar indicator — skipping"
    fi
else
    warn "swiftc not found — menu bar indicator skipped"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
# ── 12. Auto-configure Claude Code MCP (if claude CLI available) ──────────
if command -v claude &>/dev/null; then
    say "Configuring Claude Code MCP..."
    claude mcp add ghostmic "$VENV_PY" "$INSTALL_DIR/server.py" 2>/dev/null && ok "Claude Code MCP configured" || warn "Claude Code MCP config skipped — configure manually if needed"
fi

cat << SUMMARY

  ╔═══════════════════════════════════════════════════╗
  ║         ✅ ghostmic Installed!            ║
  ╚═══════════════════════════════════════════════════╝

  Location: $INSTALL_DIR

  No audio setup needed — ghostmic captures Zoom audio directly.
  No BlackHole, no speaker changes, no Multi-Output Device.

  Everything is automatic:
  - Watcher daemon detects Zoom meetings and starts recording
  - Menu bar ghost icon shows recording status
  - Claude Desktop + Claude Code are configured

  Just restart Claude Desktop: Cmd+Q → reopen

  Then just ask Claude:
    "what are they talking about?"
    "summarize the last 10 minutes"
    "make meeting notes"

  Transcripts: $INSTALL_DIR/transcripts/
  Logs: tail -20 $INSTALL_DIR/watcher.log

SUMMARY
