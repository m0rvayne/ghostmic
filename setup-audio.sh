#!/usr/bin/env bash
set -euo pipefail

# ═══════════════════════════════════════════════════════════════════════════════
# Meeting Transcript — Auto Audio Setup
# Creates Multi-Output Device (Speakers + BlackHole) automatically via CLI
# No manual Audio MIDI Setup needed!
# ═══════════════════════════════════════════════════════════════════════════════

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RED='\033[0;31m'; NC='\033[0m'
say()  { printf "${CYAN}[audio]${NC} %s\n" "$*"; }
ok()   { printf "${GREEN}  ✅ %s${NC}\n" "$*"; }
warn() { printf "${YELLOW}  ⚠️  %s${NC}\n" "$*"; }
err()  { printf "${RED}  ❌ %s${NC}\n" "$*"; }

cat << 'BANNER'

  ╔═══════════════════════════════════════════════════╗
  ║   🎙️ Meeting Transcript — Auto Audio Setup        ║
  ╚═══════════════════════════════════════════════════╝

BANNER

# ── 1. Check deps ────────────────────────────────────────────────────────────
command -v node &>/dev/null || { err "Node.js required: brew install node"; exit 1; }
command -v python3 &>/dev/null || { err "Python3 required"; exit 1; }

# ── 2. Check BlackHole ───────────────────────────────────────────────────────
say "Checking BlackHole 2ch..."
DEVICES_JSON=$(npx -y macos-audio-devices list --json 2>/dev/null)

BH_ID=$(echo "$DEVICES_JSON" | python3 -c "
import json, sys
for d in json.load(sys.stdin):
    if 'blackhole' in d['name'].lower() and d.get('isOutput'):
        print(d['id']); break
" 2>/dev/null || echo "")

if [[ -z "$BH_ID" ]]; then
    say "Installing BlackHole 2ch..."
    brew install blackhole-2ch
    sleep 3
    DEVICES_JSON=$(npx -y macos-audio-devices list --json 2>/dev/null)
    BH_ID=$(echo "$DEVICES_JSON" | python3 -c "
import json, sys
for d in json.load(sys.stdin):
    if 'blackhole' in d['name'].lower() and d.get('isOutput'):
        print(d['id']); break
" 2>/dev/null || echo "")
fi

[[ -z "$BH_ID" ]] && { err "BlackHole not detected. Reboot and try again."; exit 1; }
ok "BlackHole 2ch (ID: $BH_ID)"

# ── 3. Find speakers/headphones (not BlackHole, not aggregate) ───────────────
say "Finding output device..."
read -r SPEAKER_ID SPEAKER_NAME <<< $(echo "$DEVICES_JSON" | python3 -c "
import json, sys
for d in json.load(sys.stdin):
    if d.get('isOutput') and 'blackhole' not in d['name'].lower() and d.get('transportType','') != 'aggregate':
        print(d['id'], d['name']); break
" 2>/dev/null || echo "")

[[ -z "$SPEAKER_ID" ]] && { err "No output device found."; exit 1; }
ok "Output: $SPEAKER_NAME (ID: $SPEAKER_ID)"

# ── 4. Check if already exists ────────────────────────────────────────────────
EXISTING=$(echo "$DEVICES_JSON" | python3 -c "
import json, sys
for d in json.load(sys.stdin):
    if 'zoom' in d['name'].lower() and 'transcript' in d['name'].lower() and d.get('transportType') == 'aggregate':
        print(d['id']); break
" 2>/dev/null || echo "")

if [[ -n "$EXISTING" ]]; then
    ok "Multi-Output 'Zoom + Transcript' already exists (ID: $EXISTING)"
else
    # ── 5. Create Multi-Output Device ────────────────────────────────────────
    say "Creating Multi-Output Device: $SPEAKER_NAME + BlackHole..."
    # NOTE: --multi-output flag MUST come BEFORE positional args (CLI bug: it gets eaten by variadic deviceIds otherwise)
    CREATE_OUTPUT=$(npx -y macos-audio-devices aggregate create --multi-output "Zoom + Transcript" "$SPEAKER_ID" "$BH_ID" 2>&1)
    if echo "$CREATE_OUTPUT" | grep -q "Zoom + Transcript"; then
        ok "Created 'Zoom + Transcript'"
    else
        warn "Auto-creation may have failed: $CREATE_OUTPUT"
        warn "Open Audio MIDI Setup manually: click '+' → Multi-Output Device → check BlackHole + Speakers"
    fi
fi

# ── 5. Done ──────────────────────────────────────────────────────────────────
cat << 'DONE'

  ╔═══════════════════════════════════════════════════╗
  ║         ✅ Audio Setup Complete!                    ║
  ╚═══════════════════════════════════════════════════╝

  Created: "Zoom + Transcript" Multi-Output Device
  Routes:  Your speakers (listening) + BlackHole (transcription)
  Quality: Full 48kHz stereo — no degradation

  ┌──────────────────────────────────────────────────────────┐
  │  In Zoom:                                                 │
  │  Settings → Audio → Speaker → "Zoom + Transcript"         │
  │                                                            │
  │  Sound goes to BOTH:                                       │
  │  🔊 Your speakers/headphones (you hear the meeting)        │
  │  🎙️ BlackHole → Whisper AI (transcription)                 │
  └──────────────────────────────────────────────────────────┘

  💡 Switched headphones? Run this script again:
     bash setup-audio.sh

DONE
