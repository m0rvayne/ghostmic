# Changelog

## 0.5.0 (2026-06-21)

First public release.

### Features
- Auto-detection of Zoom meetings via CptHost process and port 19421
- Local transcription with faster-whisper (small model, CPU, int8)
- Speaker labels ([You] vs [Remote]) via dual-channel energy comparison
- 5 MCP tools: read transcript, list/read past meetings, search, status
- Meeting notes MCP prompt with anti-hallucination rules and salted XML tags
- MCP resources for current and past transcripts
- One-command installer (BlackHole, Python venv, Whisper model, Claude Desktop config, LaunchAgent)
- Background daemon with crash backoff, grace period, atomic symlink replacement
- Swift CLI for Multi-Output Device creation (no Node.js dependency)
- Menu bar indicator (red dot when recording, pause when idle)
- Uninstall script with transcript preservation option

### Architecture
- Dataclass state machine (WatcherConfig + WatcherContext + State enum)
- Bounded audio queues (OOM prevention)
- RotatingFileHandler logging (10MB, 3 backups)
- Path traversal defense with symlink rejection and boundary checks
- Language detection lock (detect once, use for all chunks)
- Status file (watcher-status.json) for menu bar communication

### Testing
- 86 tests (unit + integration via MCP SDK stdio_client)
- CI: pytest on Python 3.10/3.12/3.13, Swift build on macOS 15, shell syntax checks
