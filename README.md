<div align="center">

# Meeting Transcript MCP

**Claude knows what you're discussing right now.**

Live Zoom transcription, running entirely on your Mac. No cloud, no API keys, no third-party accounts.

[![Tests](https://github.com/m0rvayne/meeting-transcript-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/m0rvayne/meeting-transcript-mcp/actions)
[![macOS 12+](https://img.shields.io/badge/platform-macOS_12%2B-blue)](https://github.com/m0rvayne/meeting-transcript-mcp)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-brightgreen)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io)

<!-- Replace with actual demo GIF: -->
<!-- ![Demo](assets/demo.gif) -->

</div>

---

## Why?

You're on a Zoom call. Someone mentions a decision from 20 minutes ago. You didn't take notes. You can't rewind a live meeting.

Cloud transcription tools exist — but they upload your private conversations to external servers, require paid accounts, and only give you a transcript *after* the meeting ends.

**Meeting Transcript MCP** works differently:

- **Local-only.** Whisper AI runs on your Mac. Audio never leaves your machine.
- **Real-time.** Claude sees the transcript as words are spoken. Ask questions mid-meeting.
- **Zero friction.** Detects Zoom calls automatically. No buttons to press, nothing to start.
- **Speaker labels.** Distinguishes `[You]` from `[Remote]` using dual-channel audio analysis.
- **No accounts.** No signup, no API keys for transcription, no cloud dependency.

## Features

| Feature | Description |
|---------|-------------|
| Live transcript | Claude reads what's being said as it happens |
| Speaker diarization | `[You]` vs `[Remote]` labels via mic/system audio channels |
| Meeting notes | Structured summaries with topics, decisions, and action items |
| Search | Find past discussions: "when did we discuss the budget?" |
| Auto-detection | LaunchAgent daemon detects Zoom meetings, starts/stops recording |
| Status check | Ask Claude if recording is active, how many meetings are saved |

## Quick Start

**1. Install** (one command):

```bash
curl -fsSL https://raw.githubusercontent.com/m0rvayne/meeting-transcript-mcp/main/install.sh | bash
```

This sets up everything: BlackHole audio driver, Python venv, Whisper model (~500 MB), Claude Desktop config, and a background daemon.

**2. Configure Zoom** (one-time):

> Settings → Audio → Speaker → **"Zoom + Transcript"**

**3. Use it:**

Join a Zoom call. Claude already knows what's being said. Ask:

- *"What are they talking about?"*
- *"Summarize the last 10 minutes"*
- *"Make meeting notes"*
- *"Search meetings for 'product launch'"*

## How It Works

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

**watcher.py** runs as a LaunchAgent — detects Zoom meetings via `CptHost` process and port 19421, starts/stops capture automatically.

**Speaker diarization** uses the fact that BlackHole captures only remote audio (virtual device, zero mic bleed), while the microphone captures your voice. Energy ratio between the two channels determines who is speaking — no ML models, no extra dependencies.

**Meeting notes** use an MCP Prompt with salted XML tags and anti-hallucination rules. Claude structures the transcript into topics, decisions, action items, and key takeaways — using only what was actually said.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_MODEL` | `small` | Model size: `tiny`, `base`, `small`, `medium`, `large-v3` |
| `PASSTHROUGH` | `0` | Software audio passthrough (without Multi-Output Device) |
| `DIARIZATION` | `1` | Speaker labels: `[You]` vs `[Remote]` |

## MCP Tools

| Tool | What it does |
|------|-------------|
| `read_meeting_transcript` | Read the live or most recent transcript |
| `list_past_meetings` | List all recorded sessions with sizes |
| `read_past_meeting` | Read a specific past transcript |
| `search_transcripts` | Full-text search across all past meetings |
| `get_status` | Check if watcher/recording is active, disk space |

Plus: `meeting-notes` prompt for structured note generation, and MCP resources for passive transcript access.

<details>
<summary><strong>Troubleshooting</strong></summary>

**"BlackHole not found"**
```bash
brew install blackhole-2ch
# May need a reboot for the kernel extension to load.
```

**No sound after switching headphones**
```bash
bash ~/.meeting-transcript-mcp/setup-audio.sh
```

**Whisper model fails to download**
```bash
df -h ~  # Check disk space (needs ~1.5 GB)
~/.meeting-transcript-mcp/.venv/bin/python3 -c "from faster_whisper import WhisperModel; WhisperModel('small', device='cpu', compute_type='int8')"
```

**Microphone permission denied**

System Settings → Privacy & Security → Microphone → enable for Terminal.

**Watcher not starting**
```bash
launchctl list | grep meeting-transcript
launchctl bootout gui/$(id -u)/com.meeting-transcript.watcher 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.meeting-transcript.watcher.plist
```

**Transcript is empty**

1. Verify Zoom speaker is "Zoom + Transcript" (not "MacBook Speakers")
2. Check devices: `~/.meeting-transcript-mcp/.venv/bin/python3 -c "import sounddevice; print(sounddevice.query_devices())"`
3. Check logs: `tail -50 ~/.meeting-transcript-mcp/watcher.log`

</details>

## Update / Uninstall

```bash
cd meeting-transcript-mcp && bash update.sh    # Update
bash uninstall.sh                               # Clean removal (asks before deleting transcripts)
```

## Requirements

macOS 12 or later. Apple Silicon or Intel. ~1.5 GB disk space.

> **Currently supports Zoom.** Google Meet and Teams support is planned.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
