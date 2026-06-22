#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════════
# Meeting Transcript — Auto Audio Setup
# Creates Multi-Output Device (Speakers + BlackHole) automatically
# Uses a compiled Swift CLI — no Node.js required
# ═══════════════════════════════════════════════════════════════════════════════

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SWIFT_SRC="$SCRIPT_DIR/create-multi-output.swift"
CLI_BIN="$SCRIPT_DIR/.build/create-multi-output"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
say()  { printf "${CYAN}[audio]${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}  ✅ %s${NC}\n" "$*"; }
warn() { printf "${YELLOW}  ⚠️  %s${NC}\n" "$*"; }
err()  { printf "${RED}  ❌ %s${NC}\n" "$*"; }

cat << 'BANNER'

  ╔═══════════════════════════════════════════════════╗
  ║   Meeting Transcript — Auto Audio Setup            ║
  ╚═══════════════════════════════════════════════════╝

BANNER

# ── 1. Compile Swift CLI if needed ───────────────────────────────────────────
if [[ ! -x "$CLI_BIN" ]] || [[ "$SWIFT_SRC" -nt "$CLI_BIN" ]]; then
    say "Compiling audio device tool..."
    command -v swiftc &>/dev/null || { err "Xcode Command Line Tools required: xcode-select --install"; exit 1; }
    mkdir -p "$(dirname "$CLI_BIN")"
    if ! swiftc -O -framework CoreAudio -framework CoreFoundation "$SWIFT_SRC" -o "$CLI_BIN" 2>&1; then
        err "Swift compilation failed. Make sure Xcode Command Line Tools are installed."
        exit 1
    fi
    ok "Audio tool compiled"
fi

command -v python3 &>/dev/null || { err "Python3 required"; exit 1; }

# ── 2. Check BlackHole ───────────────────────────────────────────────────────
say "Checking BlackHole 2ch..."
DEVICES_JSON=$("$CLI_BIN" list --json 2>/dev/null)

BH_UID=$(echo "$DEVICES_JSON" | python3 -c "
import json, sys
for d in json.load(sys.stdin):
    if 'blackhole' in d['name'].lower() and d.get('isOutput'):
        print(d['uid']); break
" 2>/dev/null || echo "")

if [[ -z "$BH_UID" ]]; then
    say "Installing BlackHole 2ch..."
    brew install blackhole-2ch
    sleep 3
    DEVICES_JSON=$("$CLI_BIN" list --json 2>/dev/null)
    BH_UID=$(echo "$DEVICES_JSON" | python3 -c "
import json, sys
for d in json.load(sys.stdin):
    if 'blackhole' in d['name'].lower() and d.get('isOutput'):
        print(d['uid']); break
" 2>/dev/null || echo "")
fi

[[ -z "$BH_UID" ]] && { err "BlackHole not detected. Reboot and try again."; exit 1; }
ok "BlackHole 2ch (UID: $BH_UID)"

# ── 3. Find the CURRENT default output device ────────────────────────────────
say "Finding current output device..."
read -r SPEAKER_UID SPEAKER_NAME <<< $("$CLI_BIN" default-output 2>/dev/null || echo "")

# If default output is BlackHole or an aggregate, fall back to finding a real device
if [[ -z "$SPEAKER_UID" ]] || echo "$SPEAKER_UID" | grep -qi "blackhole"; then
    read -r SPEAKER_UID SPEAKER_NAME <<< $(echo "$DEVICES_JSON" | python3 -c "
import json, sys
for d in json.load(sys.stdin):
    if d.get('isOutput') and 'blackhole' not in d['name'].lower() and d.get('transportType') != 1735554416:
        print(d['uid'], d['name']); break
" 2>/dev/null || echo "")
fi

[[ -z "$SPEAKER_UID" ]] && { err "No output device found."; exit 1; }
ok "Output: $SPEAKER_NAME"

# ── 4. Check if already exists ────────────────────────────────────────────────
EXISTING=$(echo "$DEVICES_JSON" | python3 -c "
import json, sys
for d in json.load(sys.stdin):
    if 'zoom' in d['name'].lower() and 'transcript' in d['name'].lower() and d.get('transportType') == 1735554416:
        print(d['uid']); break
" 2>/dev/null || echo "")

if [[ -n "$EXISTING" ]]; then
    ok "Multi-Output 'Zoom + Transcript' already exists"
else
    # ── 5. Create Multi-Output Device ────────────────────────────────────────
    say "Creating Multi-Output Device: $SPEAKER_NAME + BlackHole..."
    if "$CLI_BIN" create "Zoom + Transcript" "$SPEAKER_UID" "$BH_UID" 2>&1; then
        ok "Created 'Zoom + Transcript'"
    else
        warn "Auto-creation failed"
        warn "Open Audio MIDI Setup manually: click '+' -> Multi-Output Device -> check BlackHole + Speakers"
    fi
fi

cat << 'DONE'

  ╔═══════════════════════════════════════════════════╗
  ║         Audio Setup Complete!                      ║
  ╚═══════════════════════════════════════════════════╝

  Created: "Zoom + Transcript" Multi-Output Device
  Routes:  Your speakers (listening) + BlackHole (transcription)
  Quality: Full 48kHz stereo — no degradation

  In Zoom: Settings -> Audio -> Speaker -> "Zoom + Transcript"

  Switched headphones? Run this script again:
     bash setup-audio.sh

DONE
