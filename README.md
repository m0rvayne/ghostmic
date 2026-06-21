<div align="center">

# Meeting Transcript MCP

**Give Claude context about your Zoom meetings.**

Local Whisper transcription piped to Claude through MCP. Transcription never leaves your Mac.

[![Tests](https://github.com/m0rvayne/meeting-transcript-mcp/actions/workflows/test.yml/badge.svg)](https://github.com/m0rvayne/meeting-transcript-mcp/actions)
[![macOS 12+](https://img.shields.io/badge/platform-macOS_12%2B-blue)](https://github.com/m0rvayne/meeting-transcript-mcp)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-brightgreen)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io)

</div>

---

## Why?

You're on a Zoom call. Someone references a decision from 20 minutes ago. You didn't take notes. You can't rewind a live meeting.

Cloud transcription tools exist — but they upload your conversations to external servers and only give you a transcript after the call ends.

This connector runs Whisper AI on your Mac and delivers the transcript to Claude as the meeting happens. Transcription is fully local — audio never leaves your machine. When you ask Claude a question, it reads the latest transcript from disk.

**What it does:**

- **Local transcription.** Whisper runs on-device. No cloud APIs for speech-to-text.
- **Near-real-time.** Transcript updates every ~30 seconds (one Whisper chunk). Claude reads the latest version when you ask.
- **Auto-detection.** A background daemon detects Zoom and Google Meet calls, starts/stops recording. No buttons to press during the meeting.
- **Menu bar indicator.** Red dot when recording, pause when idle. Always know if it's working.
- **Speaker labels.** Tags `[You]` vs `[Remote]` using energy comparison between mic and system audio channels. Not ML-based diarization — a simple but effective heuristic for two-party calls.
- **Meeting notes.** Structured summaries via MCP Prompt — Claude organizes topics, decisions, and action items from the transcript.

## Features

| Feature | Description |
|---------|-------------|
| Live transcript | Transcript updates every ~30s, Claude reads on demand |
| Speaker labels | `[You]` vs `[Remote]` via dual-channel energy comparison |
| Meeting notes | Structured summaries with topics, decisions, action items |
| Search | Substring search across all past transcripts |
| Auto-detection | Detects Zoom (process) and Google Meet (browser tab) every 5s |
| Menu bar | 🔴 when recording, ⏸ when idle |
| Status | Check if recording is active, transcript count, disk space |

## Quick Start

> **Prerequisites:** macOS 12+, Xcode Command Line Tools (`xcode-select --install`), ~2 GB disk space (Whisper model + dependencies).

**1. Install:**

```bash
curl -fsSL https://raw.githubusercontent.com/m0rvayne/meeting-transcript-mcp/main/install.sh | bash
```

This installs BlackHole audio driver, creates a Python venv with Whisper, configures Claude Desktop, and sets up a background daemon (LaunchAgent). Takes 5-10 minutes on first run (Whisper model download is ~500 MB).

**2. Configure Zoom** (one-time):

> Settings → Audio → Speaker → **"Zoom + Transcript"**

**3. Use it:**

Join a Zoom call. The daemon detects it and starts recording. In Claude:

- *"What are they talking about?"*
- *"Summarize the last 10 minutes"*
- *"Make meeting notes"*
- *"Search meetings for 'product launch'"*

> Note: Claude reads the transcript when you ask — it does not stream or push updates automatically.

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

**watcher.py** runs as a LaunchAgent (persistent background process, starts on login). It polls for `CptHost` (Zoom's in-meeting process) every 5 seconds and spawns/kills **capture.py** accordingly.

**capture.py** opens two audio streams: BlackHole (remote participants) and microphone (you). Buffers audio while Whisper model loads, transcribes in 30-second chunks, writes timestamped lines to a transcript file.

**Speaker labels** use the fact that BlackHole captures only system audio (virtual device, no mic bleed) while the microphone captures your voice. Energy ratio between the two channels determines the label. This works well for two-party calls; it cannot distinguish between multiple remote speakers.

**server.py** is the MCP server (stdio transport). It reads transcript files from disk and serves them to Claude via 5 tools, 1 prompt, and MCP resources.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `WHISPER_MODEL` | `small` | Model size: `tiny`, `base`, `small`, `medium`, `large-v3` |
| `PASSTHROUGH` | `0` | Software audio passthrough (without Multi-Output Device) |
| `DIARIZATION` | `1` | Speaker labels: `[You]` vs `[Remote]` |

## MCP Tools

| Tool | What it does |
|------|-------------|
| `read_meeting_transcript` | Read the current or most recent transcript |
| `list_past_meetings` | List all recorded sessions with sizes |
| `read_past_meeting` | Read a specific past transcript by filename |
| `search_transcripts` | Substring search across all past meetings |
| `get_status` | Watcher state, recording state, disk space |

**Prompt:** `meeting-notes` — generates structured notes (topics, decisions, action items) from a transcript. Uses salted XML delimiters for prompt injection protection.

**Resources:** `meeting://current/transcript`, `meeting://past/{filename}`

## Limitations

- **Zoom and Google Meet.** Teams support is planned.
- **macOS only.** Requires BlackHole (macOS audio driver), CoreAudio, LaunchAgent.
- **~30 second latency.** Whisper processes audio in 30-second chunks. On slower hardware (Intel, `medium`/`large` models), latency can be higher.
- **Two-party speaker labels only.** Cannot distinguish between multiple remote speakers.
- **Headphone switching.** Changing audio output requires re-running `bash ~/.meeting-transcript-mcp/setup-audio.sh`.
- **Google Meet requires Automation permission.** macOS will ask once to allow controlling Chrome/Safari. Needed for tab URL detection.

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

1. Verify Zoom speaker is "Zoom + Transcript"
2. Check devices: `~/.meeting-transcript-mcp/.venv/bin/python3 -c "import sounddevice; print(sounddevice.query_devices())"`
3. Check logs: `tail -50 ~/.meeting-transcript-mcp/watcher.log`

</details>

## Update / Uninstall

```bash
cd meeting-transcript-mcp && bash update.sh    # Update
bash uninstall.sh                               # Clean removal (asks before deleting transcripts)
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). 83 tests, all green.

## License

[MIT](LICENSE)
