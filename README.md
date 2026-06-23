<div align="center">

# ghostmic

**No bot joins the call.**

Silent meeting transcription for Claude. Local Whisper AI, no cloud, no one knows you're recording.

[![Tests](https://github.com/m0rvayne/ghostmic/actions/workflows/test.yml/badge.svg)](https://github.com/m0rvayne/ghostmic/actions)
[![macOS 12+](https://img.shields.io/badge/platform-macOS_12%2B-blue)](https://github.com/m0rvayne/ghostmic)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-brightgreen)](https://python.org)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow)](LICENSE)
[![MCP](https://img.shields.io/badge/protocol-MCP-purple)](https://modelcontextprotocol.io)

</div>

---

## The problem

Every meeting transcription tool puts a bot on your call:

```
Fathom:    👤 👤 👤 🤖 "Fathom Notetaker joined"
Otter:     👤 👤 👤 🤖 "Otter.ai joined"
Fireflies: 👤 👤 👤 🤖 "Fireflies.ai Notetaker joined"

ghostmic:  👤 👤 👤     (nothing. it's already recording.)
```

Your conversations go to their cloud. Everyone on the call sees the bot. People change how they talk.

**ghostmic** records through macOS system audio. Whisper runs on your Mac. Nobody knows. Nothing leaves your machine.

## How it works

- **Invisible.** No bot joins the call. Captures audio through BlackHole virtual driver.
- **Local AI.** Whisper transcribes on-device. Audio never touches the cloud.
- **Live to Claude.** Transcript updates every ~30 seconds. Ask Claude anything mid-meeting.
- **Auto-detection.** Background daemon detects Zoom calls. No buttons to press.
- **Speaker labels.** `[You]` vs `[Remote]` via dual-channel energy comparison.
- **Menu bar indicator.** 🔴 when recording, settings, pause/stop controls.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/m0rvayne/ghostmic/main/install.sh | bash
```

One command. Sets up everything: BlackHole, Python venv, Whisper model, Claude Desktop config, LaunchAgent, menu bar indicator.

Then in Zoom: **Settings → Audio → Speaker → "Zoom + Transcript"**

Restart Claude Desktop (Cmd+Q → reopen). Done.

## Usage

Join a Zoom call. ghostmic detects it and starts recording. In Claude:

- *"What are they talking about?"*
- *"Summarize the last 10 minutes"*
- *"Make meeting notes"* — structured output with topics, decisions, action items
- *"Search meetings for 'budget'"*

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

**watcher.py** — background daemon, detects Zoom via `CptHost` process, manages capture lifecycle with crash backoff and grace period.

**Speaker labels** — BlackHole has only remote audio, mic has your voice. Energy ratio determines who's speaking. No ML models needed.

**Meeting notes** — MCP Prompt with anti-hallucination rules and injection protection (salted XML tags).

## Configuration

Settings available in the menu bar indicator (⚙):

| Setting | Default | Description |
|---------|---------|-------------|
| Whisper Model | `small` | `tiny`, `base`, `small`, `medium`, `large-v3` |
| Speaker Labels | On | `[You]` vs `[Remote]` diarization |
| Save Path | `~/.ghostmic/transcripts` | Where transcripts are saved |

## Limitations

- **Zoom only.** Google Meet and Teams support planned.
- **macOS only.** Requires BlackHole, CoreAudio, LaunchAgent.
- **~30 second latency.** Whisper processes in 30-second chunks.
- **Two-party labels.** Cannot distinguish multiple remote speakers.

<details>
<summary><strong>Troubleshooting</strong></summary>

**"BlackHole not found"** — `brew install blackhole-2ch`, may need reboot.

**No sound after switching headphones** — `bash ~/.ghostmic/setup-audio.sh`

**Transcript is empty** — Verify Zoom speaker is "Zoom + Transcript", check `tail -50 ~/.ghostmic/watcher.log`

**Microphone permission** — System Settings → Privacy & Security → Microphone → enable Terminal.

</details>

## Update / Uninstall

```bash
cd ghostmic && bash update.sh    # Update
bash uninstall.sh                # Clean removal
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). 86 tests, all green.

## License

[MIT](LICENSE)
