# Meeting Transcript MCP

Live Zoom transcription. Records system audio via BlackHole, transcribes locally with Whisper, serves transcripts to Claude Desktop through MCP.

## Install

```
curl -fsSL https://raw.githubusercontent.com/morvayne1/meeting-transcript-mcp/main/install.sh | bash
```

Installs Homebrew, Python, Node.js, Claude CLI (if missing), BlackHole 2ch, Whisper model (~500MB), and configures Claude Desktop.

## Setup

After install, open Zoom and set speaker to `Zoom + Transcript` (Settings > Audio > Speaker). This routes audio to both your speakers and BlackHole for capture.

## Usage

Auto mode — detects Zoom meetings, starts and stops recording automatically:

```
~/.meeting-transcript-mcp/.venv/bin/python3 ~/.meeting-transcript-mcp/watcher.py
```

Manual mode — records until you stop it:

```
~/.meeting-transcript-mcp/.venv/bin/python3 ~/.meeting-transcript-mcp/capture.py
```

In Claude Desktop (restart first with Cmd+Q):

- "what was discussed?" — reads current transcript
- "show conference map" — builds mind map
- "list past meetings" — shows all recorded sessions

Transcripts are saved to `~/.meeting-transcript-mcp/transcripts/`.

## Update

```
cd meeting-transcript-mcp && bash update.sh
```

## Architecture

BlackHole 2ch is a virtual audio driver that captures system audio. A Multi-Output Device routes Zoom audio to both your speakers and BlackHole simultaneously at full 48kHz quality. capture.py reads from BlackHole and the built-in microphone, mixes both streams, and feeds them to Whisper (small model, runs on CPU). watcher.py monitors for active Zoom meetings by checking for the CptHost process and port 19421.

## Switching headphones

The Multi-Output Device is tied to the current output device. If you plug in or unplug headphones, run:

```
bash ~/.meeting-transcript-mcp/setup-audio.sh
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| WHISPER_MODEL | small | Model size: tiny, base, small, medium, large-v3 |
| PASSTHROUGH | 0 | Set to 1 for software audio passthrough (without Multi-Output Device) |

## Requirements

macOS 12 or later. Apple Silicon or Intel. About 1.5GB disk space for the Whisper model and dependencies.
