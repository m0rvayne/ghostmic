# Meeting Transcript MCP

[![macOS](https://img.shields.io/badge/platform-macOS-blue)](https://github.com/m0rvayne/meeting-transcript-mcp)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-brightgreen)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io)

Live Zoom transcription for Claude. Records system audio via BlackHole, transcribes locally with Whisper AI, serves transcripts through MCP.

> **Currently supports Zoom.** Google Meet and Teams support is planned.

## Architecture

```
Zoom Audio
    |
    v
BlackHole 2ch (virtual audio driver)
    |
    +---> capture.py ---> Whisper AI (local, on-device)
    |                         |
    |                         v
    |                   transcripts/*.txt
    |                         |
    +---> Multi-Output -------+---> server.py (MCP)
          Device                        |
          (speakers +                   v
           BlackHole)           Claude Desktop / Claude Code
```

**watcher.py** runs as a LaunchAgent — detects Zoom meetings automatically (via `CptHost` process and port 19421), starts/stops capture with no manual intervention.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/m0rvayne/meeting-transcript-mcp/main/install.sh | bash
```

This installs: Homebrew, Python, BlackHole 2ch, Whisper model (~500 MB), Claude CLI (if missing), and configures Claude Desktop.

## Setup

After install, open Zoom and set speaker to **"Zoom + Transcript"** (Settings > Audio > Speaker). This routes audio to both your speakers and BlackHole for capture.

## Usage

**Auto mode** — detects Zoom meetings, starts and stops recording automatically:

```bash
# Already running as LaunchAgent after install.
# Check status:
launchctl list | grep meeting-transcript
# View logs:
tail -20 ~/.meeting-transcript-mcp/watcher.log
```

**Manual mode** — records until you stop it:

```bash
~/.meeting-transcript-mcp/.venv/bin/python3 ~/.meeting-transcript-mcp/capture.py
```

**In Claude Desktop** (restart first with Cmd+Q):

- "what was discussed?" — reads current transcript
- "show conference map" — builds a mind map in MindNode
- "list past meetings" — shows all recorded sessions
- "read meeting from 2026-06-20" — opens a specific past transcript

Transcripts are saved to `~/.meeting-transcript-mcp/transcripts/`.

## Update

```bash
cd meeting-transcript-mcp && bash update.sh
```

## Switching headphones

The Multi-Output Device is tied to the current output device. If you plug in or unplug headphones:

```bash
bash ~/.meeting-transcript-mcp/setup-audio.sh
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_MODEL` | `small` | Model size: `tiny`, `base`, `small`, `medium`, `large-v3` |
| `PASSTHROUGH` | `0` | Set to `1` for software audio passthrough (without Multi-Output Device) |

## Troubleshooting

**"BlackHole not found"**
```bash
brew install blackhole-2ch
# If still not detected, reboot — BlackHole requires a kernel extension reload.
```

**No sound after switching headphones**
```bash
bash ~/.meeting-transcript-mcp/setup-audio.sh
# This recreates the Multi-Output Device with your current output.
```

**Whisper model fails to download**
```bash
# Check disk space (needs ~1.5 GB):
df -h ~
# Retry download manually:
~/.meeting-transcript-mcp/.venv/bin/python3 -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
```

**Microphone permission denied**

macOS requires microphone access. Go to **System Settings > Privacy & Security > Microphone** and enable access for Terminal (or your terminal app).

**Watcher not starting on login**
```bash
# Check LaunchAgent status:
launchctl list | grep meeting-transcript
# Reload:
launchctl bootout gui/$(id -u)/com.meeting-transcript.watcher 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.meeting-transcript.watcher.plist
```

**Transcript is empty**

1. Verify Zoom speaker is set to "Zoom + Transcript" (not "MacBook Speakers")
2. Check that BlackHole is receiving audio: `~/.meeting-transcript-mcp/.venv/bin/python3 -c "import sounddevice; print(sounddevice.query_devices())"`
3. Check capture logs: `tail -50 ~/.meeting-transcript-mcp/watcher.log`

## Requirements

macOS 12 or later. Apple Silicon or Intel. ~1.5 GB disk space for the Whisper model and dependencies.
