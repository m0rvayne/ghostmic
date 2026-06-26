<div align="center">

<img src="assets/logo.svg" alt="ghostmic" width="100%">

**Your meetings, transcribed invisibly. No bot. No cloud. No one knows.**

Give Claude live context from your Zoom calls — everything runs locally on your Mac, nothing shows up in the participant list.

[![Tests](https://github.com/m0rvayne/ghostmic/actions/workflows/test.yml/badge.svg)](https://github.com/m0rvayne/ghostmic/actions)
[![macOS 14.4+](https://img.shields.io/badge/platform-macOS_14.4%2B-blue)](https://github.com/m0rvayne/ghostmic)
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

**ghostmic** captures audio directly from the Zoom process via CoreAudio. No virtual drivers, no audio routing, no setup. Nobody knows. Nothing leaves your machine.

## How it works

- **Invisible.** No bot joins the call. CoreAudio Process Taps capture Zoom audio at the system level — no virtual drivers, no speaker changes.
- **Two-stage AI.** whisper.cpp (large-v3-turbo) transcribes with Metal GPU in ~3 seconds. Then Qwen3-0.6B micro LLM refines the text — fixes recognition errors, merges sentence fragments, maintains speaker continuity across chunks.
- **Live to Claude.** Transcript updates every ~15 seconds. Ask Claude anything mid-meeting.
- **Zero config.** No BlackHole, no Multi-Output Device, no Zoom speaker settings. Plug in any headphones, switch anytime — recording never breaks.
- **Auto-detection.** Background daemon detects Zoom calls. No buttons to press.
- **Speaker labels.** `[You]` vs `[Remote]` via dual-channel energy comparison. Mic auto-silences when you're muted in Zoom — private conversations stay private.
- **Menu bar app.** Ghost icon with pause/stop controls, live timer, settings.

## The AI pipeline

```
Zoom Audio
    |
    v
CoreAudio Process Tap (captures Zoom by bundle ID — no virtual driver)
    |
    v
whisper.cpp large-v3-turbo (Metal GPU, ~3s per 10s chunk)
    |
    v
Qwen3-0.6B micro LLM (fixes errors, merges fragments, speaker continuity)
    |
    v
Transcript file ──> server.py (MCP) ──> Claude Desktop / Claude Code
```

Most transcription tools stop at step 3. We add a micro LLM that reads the last 3 chunks as context and fixes what Whisper got wrong — misheard terms, sentence fragments split across chunk boundaries, repeated speaker labels. The result reads like someone actually took notes.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/m0rvayne/ghostmic/main/install.sh | bash
```

One command. Sets up everything: Python venv, whisper.cpp model, Qwen3 LLM, Claude Desktop config, LaunchAgent, menu bar indicator. No audio drivers to install.

Then restart Claude Desktop (Cmd+Q → reopen). Done.

## Usage

Join a Zoom call. ghostmic detects it and starts recording. In Claude:

- *"What are they talking about?"*
- *"Summarize the last 10 minutes"*
- *"Make meeting notes"* — structured output with topics, decisions, action items
- *"Search meetings for 'budget'"*

## Configuration

Settings available in the menu bar indicator (ghost icon → ⚙ Settings):

| Setting | Default | Description |
|---------|---------|-------------|
| Whisper Model | `large-v3-turbo` | Model for transcription |
| Speaker Labels | On | `[You]` vs `[Remote]` diarization |
| Language | `ru` | Forced language (set `auto` for auto-detect) |
| Save Path | `~/.ghostmic/transcripts` | Where transcripts are saved |

## Why ghostmic vs competitors

| | Fathom / Otter / Fireflies | ghostmic |
|---|---|---|
| **Visibility** | Bot joins the call, everyone sees | Nothing. Invisible. |
| **Privacy** | Audio goes to their cloud | Never leaves your Mac |
| **Audio setup** | None (bot captures) | None (CoreAudio tap) |
| **Headphone switching** | N/A | No impact on recording |
| **AI quality** | Cloud LLM | whisper.cpp + Qwen3 LLM post-processing |
| **Claude integration** | None | Native MCP — live transcript in Claude |
| **Cost** | $15-30/month | Free, open source |

## Limitations

- **Zoom only.** Google Meet and Teams support planned.
- **macOS 14.4+.** Requires CoreAudio Process Taps API.
- **~15 second latency.** Audio chunks processed every ~10 seconds + AI pipeline.
- **Two-party speaker labels.** Cannot distinguish multiple remote speakers by voice.

<details>
<summary><strong>Troubleshooting</strong></summary>

**Screen Recording permission** — macOS will ask once to grant permission for audio capture. Go to System Settings → Privacy & Security → Screen Recording.

**Transcript is empty** — Check `tail -50 ~/.ghostmic/watcher.log` for errors.

**Microphone permission** — System Settings → Privacy & Security → Microphone → enable Terminal.

</details>

## Update / Uninstall

```bash
cd ghostmic && bash update.sh    # Update
bash uninstall.sh                # Clean removal
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
