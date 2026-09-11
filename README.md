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
- **Local transcription.** whisper.cpp (large-v3-turbo) with Metal GPU, model resident in memory via whisper-server. Canned-outro and sound-annotation filtering tuned on 1300+ real transcripts.
- **Live to Claude.** Transcript updates every ~15 seconds. Ask Claude anything mid-meeting.
- **Zero config.** No BlackHole, no Multi-Output Device, no Zoom speaker settings. Plug in any headphones, switch anytime — recording never breaks.
- **Auto-detection.** Background daemon detects Zoom calls. No buttons to press.
- **Speaker labels.** `[You]`, `[Remote]` and `[Both]`, from how far each channel sits above its own noise floor — so the labels survive a volume or mic-gain change mid-call. Mic auto-silences when you're muted in Zoom — private conversations stay private.
- **Menu bar app.** Ghost icon with pause/stop controls, live timer, settings.

## The AI pipeline

```
Zoom Audio
    |
    v
CoreAudio Process Tap (captures Zoom by bundle ID — no virtual driver)
    |
    v
Vocabulary biasing (participant names + your glossary as the initial prompt)
    |
    v
whisper.cpp large-v3-turbo (Metal GPU, model resident via whisper-server)
    |
    v
Rule-based cleanup (canned outros, sound annotations, repetition)
    |
    v
LLM repair pass (only on words whisper was unsure of; guarded)
    |
    v
Transcript file ──> server.py (MCP) ──> Claude Desktop / Claude Code
```

### The LLM repair pass

A Qwen3-4B stage runs after transcription. Turn it off with `LLM_POST=0`,
pick a different model with `LLM_MODEL`.

Model size matters here more than it usually does. On three garbled product
names — "Майндменеджер", "Клод Кот", "ресерчер" — Qwen3-0.6B and Qwen3-1.7B
changed nothing at all, while Qwen3-4B returned MindManager, Claude Code and
Researcher. Mapping a misheard word onto the right term needs enough model to
hold the context. 4B costs 2.1 GB on disk, about 1 GB resident and ~2s per
chunk, and it only runs on the chunks whisper was unsure of.

It is deliberately quiet. It is only called on chunks where whisper reported
low confidence in a word — with nothing doubtful there is nothing to repair,
and a model asked to improve a correct sentence tends to delete the last one.
When it does run, the doubtful words are named in the prompt so it knows where
to look, and its output has to get past a guard before it replaces anything:

- a lost or altered number → rejected
- length outside 0.92–1.15× the original → rejected (this is what catches
  truncation, the most common failure)
- normalised edit distance over 0.35 → rejected
- a known hallucination phrase → rejected

Every accepted edit is printed to `watcher.log` with the text before and after,
so you can see what it is doing rather than trust that it is doing something.

**Measured, so you know what you are getting.** `tools/measure_llm_post.py`
runs the production prompt through the production model over real chunks from
your own archive — pass `--model` to compare candidates on the same material.

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/m0rvayne/ghostmic/main/install.sh | bash
```

One command. Sets up everything: Python venv, whisper.cpp model, Claude Desktop config, LaunchAgent, menu bar indicator. No audio drivers to install.

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
| Whisper Model | `large-v3-turbo` | Lists the models present in `~/.ghostmic/models` |
| Speaker Labels | On | `[You]` / `[Remote]` / `[Both]` diarization |
| Language | `ru` | Forced language (set `auto` for auto-detect) |
| Save Path | `~/.ghostmic/transcripts` | Where transcripts are saved |

### Glossary

Names and domain terms go in `~/.ghostmic/config.json`:

```json
{ "glossary": ["Claude Code", "MCP", "майнд-карта", "Researcher"] }
```

These are fed to whisper as an initial prompt — a bare comma-separated list,
deliberately — which biases decoding toward the spelling you want. It is the
difference between

```
without:  «Мы обсудили Клод Кот и Майнд Карту в Ресерчере»
with:     «Мы обсудили Claude Code и Майнд-карту в Researcher»
```

Participant names detected from the Zoom window are appended automatically, and
go last because the end of the prompt carries the most weight. Only the last 224
tokens are used, so keep the list to terms whisper actually gets wrong. Edits
take effect on the next chunk — no restart.

Keep it short for a second reason. A prompt becomes context the decoder can
continue instead of transcribing, which is a documented whisper behaviour: an
earlier version opened with a sentence and whisper wrote that sentence into 232
transcript lines. The prompt is a bare list now, and leading echoes of it are
stripped from the output, but a long list is still more surface for this.

## Why ghostmic vs competitors

| | Fathom / Otter / Fireflies | ghostmic |
|---|---|---|
| **Visibility** | Bot joins the call, everyone sees | Nothing. Invisible. |
| **Privacy** | Audio goes to their cloud | Never leaves your Mac |
| **Audio setup** | None (bot captures) | None (CoreAudio tap) |
| **Headphone switching** | N/A | No impact on recording |
| **Transcription** | Cloud ASR | whisper.cpp large-v3-turbo, on-device |
| **Claude integration** | None | Native MCP — live transcript in Claude |
| **Cost** | $15-30/month | Free, open source |

## Limitations

- **Zoom only.** Google Meet and Teams support planned.
- **macOS 14.4+.** Requires CoreAudio Process Taps API.
- **~15 second latency.** Audio chunks processed every ~10 seconds + AI pipeline.
- **Two-party speaker labels.** Cannot distinguish multiple remote speakers by voice.
- **The LLM pass is a repair, not a rewrite.** It fixes words whisper flagged as uncertain; it does not turn speech into polished prose.

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
