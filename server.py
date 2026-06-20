"""
MCP server: live meeting transcript for Claude.
Captures Zoom audio, transcribes locally with Whisper, serves to Claude in real time.
"""
import os
import re
import shutil
import time
from pathlib import Path
from datetime import datetime
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

INSTALL_DIR = Path(__file__).parent
CURRENT = INSTALL_DIR / "transcripts" / "meeting_transcript.txt"
TRANSCRIPTS_DIR = INSTALL_DIR / "transcripts"
PID_FILE = INSTALL_DIR / "watcher.pid"
FRESHNESS_THRESHOLD = 180  # seconds
MAX_TRANSCRIPT_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_PAST_MEETINGS = 200
MAX_SEARCH_RESULTS = 20

server = Server("meeting-transcript")


def _resolve_transcript() -> Path | None:
    """Resolve the current transcript symlink, with boundary check."""
    try:
        target = CURRENT.resolve() if CURRENT.is_symlink() else CURRENT
        if not target.exists():
            return None
        if not target.is_relative_to(TRANSCRIPTS_DIR.resolve()):
            return None
        return target
    except Exception:
        return None


def _is_recording_live() -> bool:
    target = _resolve_transcript()
    if not target:
        return False
    age = time.time() - target.stat().st_mtime
    return age < FRESHNESS_THRESHOLD


def _freshness_note() -> str:
    if _is_recording_live():
        return ""
    target = _resolve_transcript()
    if not target:
        return ""
    mtime = datetime.fromtimestamp(target.stat().st_mtime)
    return (
        f"WARNING: RECORDING IS NOT ACTIVE. This is a transcript from a PAST meeting "
        f"(file: {target.name}, last updated: {mtime.strftime('%Y-%m-%d %H:%M')}). "
        f"Do NOT refer to it as the 'current meeting'.\n\n"
    )


def _safe_transcript_path(filename: str) -> Path | None:
    """Resolve filename and ensure it stays within TRANSCRIPTS_DIR. Rejects symlinks."""
    try:
        if not filename or "/" in filename or "\\" in filename or ".." in filename:
            return None
        if "%" in filename or "\x00" in filename:
            return None
        candidate = TRANSCRIPTS_DIR / filename
        if candidate.is_symlink():
            return None
        path = candidate.resolve()
        if not path.is_relative_to(TRANSCRIPTS_DIR.resolve()):
            return None
        if not path.exists():
            return None
        return path
    except (ValueError, OSError):
        return None


def read_transcript_text() -> str:
    target = _resolve_transcript()
    if not target:
        return ""
    if target.stat().st_size > MAX_TRANSCRIPT_BYTES:
        return "[Transcript too large to read in full]"
    try:
        return target.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _filter_by_minutes(text: str, last_minutes: float) -> str:
    """Filter transcript lines to only include the last N minutes based on timestamps."""
    if not (last_minutes and last_minutes > 0 and last_minutes == last_minutes):
        return text

    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return text

    ts_pattern = re.compile(r"^\[(\d{2}:\d{2}:\d{2})")
    cutoff = None

    for line in reversed(lines):
        m = ts_pattern.match(line)
        if m:
            parts = m.group(1).split(":")
            latest_secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            cutoff = latest_secs - last_minutes * 60
            break

    if cutoff is None:
        keep = max(1, int(last_minutes * 2))
        return "\n".join(lines[-keep:])

    result = []
    for line in lines:
        m = ts_pattern.match(line)
        if m:
            parts = m.group(1).split(":")
            secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            if secs >= cutoff:
                result.append(line)
        else:
            if result:
                result.append(line)

    return "\n".join(result) if result else "\n".join(lines[-3:])


# -- Resources ----------------------------------------------------------------

@server.list_resources()
async def list_resources():
    resources = []
    if _resolve_transcript():
        resources.append(types.Resource(
            uri="meeting://current/transcript",
            name="Current meeting transcript",
            description="Live or most recent meeting transcript text",
            mimeType="text/plain",
        ))
    if TRANSCRIPTS_DIR.exists():
        count = 0
        for f in sorted(TRANSCRIPTS_DIR.glob("*.txt"), reverse=True):
            if f.name == "meeting_transcript.txt" or f.is_symlink():
                continue
            resources.append(types.Resource(
                uri=f"meeting://past/{f.name}",
                name=f"Meeting {f.stem}",
                mimeType="text/plain",
            ))
            count += 1
            if count >= MAX_PAST_MEETINGS:
                break
    return resources


@server.read_resource()
async def read_resource(uri):
    uri_str = str(uri)
    if uri_str == "meeting://current/transcript":
        text = read_transcript_text()
        if not text:
            return "Transcript is empty."
        return _freshness_note() + text

    if uri_str.startswith("meeting://past/"):
        filename = uri_str.removeprefix("meeting://past/")
        path = _safe_transcript_path(filename)
        if not path:
            return "File not found."
        if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
            return "Transcript too large."
        return path.read_text(encoding="utf-8", errors="replace")

    return "Unknown resource."


# -- Prompts ------------------------------------------------------------------

MEETING_NOTES_TEMPLATE = """You are a professional meeting note-taker. Create structured meeting notes from the transcript below.

RULES:
- Only include information explicitly stated in the transcript
- Do NOT invent, infer, or add anything not said
- If something is unclear, write [unclear] rather than guessing
- Attribute statements to speakers when labels ([You], [Remote]) are present
- Generate notes in the same language as the majority of the transcript

FORMAT:

## Meeting Overview
Date, duration, participants (if identifiable from context). 1-2 sentence summary of the meeting purpose.

## Topics Discussed

### [Topic 1 name]
- Key points discussed
- Who said what (when speaker labels are available)
- Context and details

### [Topic 2 name]
...add as many topics as needed...

## Decisions Made
- Each decision with brief reasoning/context
- If no decisions were made, write "No decisions were made."

## Action Items
| Task | Owner | Deadline |
|------|-------|----------|
| Specific task | Person (if mentioned) | Date (if mentioned) |

If no action items, write "No action items were identified."

## Open Questions
- Unresolved items or questions that need follow-up
- If none, omit this section

## Key Takeaways
- 3-5 bullet points capturing the most important outcomes

---

TRANSCRIPT:
"""


@server.list_prompts()
async def list_prompts():
    return [
        types.Prompt(
            name="meeting-notes",
            description=(
                "Generate structured meeting notes from a transcript. "
                "Organizes by topics, captures decisions, action items, and key takeaways. "
                "Uses only information from the transcript — no hallucination."
            ),
            arguments=[
                types.PromptArgument(
                    name="filename",
                    description="Transcript filename for a past meeting (optional — defaults to current)",
                    required=False,
                ),
            ],
        ),
    ]


@server.get_prompt()
async def get_prompt(name: str, arguments: dict | None):
    if name != "meeting-notes":
        raise ValueError(f"Unknown prompt: {name}")

    arguments = arguments or {}
    filename = arguments.get("filename")

    if filename:
        path = _safe_transcript_path(filename)
        if not path:
            raise ValueError(f"Transcript not found: {filename}")
        if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
            raise ValueError("Transcript too large for meeting notes.")
        transcript = path.read_text(encoding="utf-8", errors="replace")
    else:
        transcript = read_transcript_text()
        if not transcript:
            raise ValueError("No transcript available. Start a meeting recording first.")

    note = _freshness_note()

    return types.GetPromptResult(
        description="Structured meeting notes from transcript",
        messages=[
            types.PromptMessage(
                role="user",
                content=types.TextContent(
                    type="text",
                    text=note + MEETING_NOTES_TEMPLATE + "\n" + transcript,
                ),
            ),
        ],
    )


# -- Tools --------------------------------------------------------------------

@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="read_meeting_transcript",
            description=(
                "Read the current live meeting transcript. "
                "Use when asked: 'what was discussed', 'summarize the meeting', "
                "'что обсуждали', 'итоги встречи', 'what are they talking about'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "last_minutes": {
                        "type": "number",
                        "description": "Only return transcript from the last N minutes",
                    }
                },
            },
        ),
        types.Tool(
            name="list_past_meetings",
            description="List all recorded meeting transcripts with dates and sizes.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="read_past_meeting",
            description="Read a specific past meeting transcript by filename.",
            inputSchema={
                "type": "object",
                "required": ["filename"],
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "Transcript filename (e.g. '2026-06-20_14-30-00.txt')",
                    },
                },
            },
        ),
        types.Tool(
            name="search_transcripts",
            description=(
                "Search across all past meeting transcripts for a keyword or phrase. "
                "Returns matching excerpts with filenames. Use when asked: "
                "'when did we discuss X', 'find the meeting about Y'."
            ),
            inputSchema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "query": {"type": "string", "description": "Search term or phrase"},
                },
            },
        ),
        types.Tool(
            name="get_status",
            description=(
                "Get the current status of the meeting transcript system. "
                "Shows whether the watcher daemon is running, if a recording is active, "
                "disk space, and transcript count."
            ),
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None):
    arguments = arguments or {}

    if name == "read_meeting_transcript":
        text = read_transcript_text()
        if not text:
            return [types.TextContent(type="text", text="Transcript is empty.")]

        last_minutes = arguments.get("last_minutes")
        if last_minutes is not None:
            try:
                text = _filter_by_minutes(text, float(last_minutes))
            except (ValueError, TypeError):
                pass

        note = _freshness_note()
        result = note + text
        if not _is_recording_live():
            result += "\n\n---\nTIP: For structured meeting notes with topics, decisions, and action items, use the meeting-notes prompt."
        return [types.TextContent(type="text", text=result)]

    elif name == "list_past_meetings":
        if not TRANSCRIPTS_DIR.exists():
            return [types.TextContent(type="text", text="No recorded meetings.")]
        files = sorted(TRANSCRIPTS_DIR.glob("*.txt"), reverse=True)
        files = [f for f in files if f.name != "meeting_transcript.txt" and not f.is_symlink()]
        if not files:
            return [types.TextContent(type="text", text="No recorded meetings.")]
        files = files[:MAX_PAST_MEETINGS]
        lines = [f"- {f.name}  ({f.stat().st_size // 1024} KB)" for f in files]
        return [types.TextContent(type="text", text="\n".join(lines))]

    elif name == "read_past_meeting":
        filename = arguments.get("filename", "")
        path = _safe_transcript_path(filename)
        if not path:
            return [types.TextContent(type="text", text="File not found or invalid filename.")]
        if path.stat().st_size > MAX_TRANSCRIPT_BYTES:
            return [types.TextContent(type="text", text="Transcript too large to read.")]
        return [types.TextContent(type="text", text=path.read_text(encoding="utf-8", errors="replace"))]

    elif name == "search_transcripts":
        query = arguments.get("query", "").strip()
        if not query:
            return [types.TextContent(type="text", text="Empty search query.")]
        if not TRANSCRIPTS_DIR.exists():
            return [types.TextContent(type="text", text="No transcripts to search.")]

        results = []
        files = sorted(TRANSCRIPTS_DIR.glob("*.txt"), reverse=True)
        files = [f for f in files if f.name != "meeting_transcript.txt" and not f.is_symlink()]

        for f in files[:MAX_PAST_MEETINGS]:
            if f.stat().st_size > MAX_TRANSCRIPT_BYTES:
                continue
            try:
                content = f.read_text(encoding="utf-8")
            except Exception:
                continue
            if query.lower() not in content.lower():
                continue

            lines = content.split("\n")
            matches = []
            for i, line in enumerate(lines):
                if query.lower() in line.lower():
                    matches.append(line.strip())
                    if len(matches) >= 3:
                        break

            results.append(f"**{f.name}**\n" + "\n".join(f"  {m}" for m in matches))
            if len(results) >= MAX_SEARCH_RESULTS:
                break

        if not results:
            return [types.TextContent(type="text", text=f"No matches found for '{query[:200]}'.")]
        header = f"Found '{query[:200]}' in {len(results)} meeting(s):\n\n"
        return [types.TextContent(type="text", text=header + "\n\n".join(results))]

    elif name == "get_status":
        status_parts = []

        watcher_running = False
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                if pid > 0:
                    os.kill(pid, 0)
                    watcher_running = True
                    status_parts.append(f"Watcher: running (PID {pid})")
                else:
                    status_parts.append("Watcher: not running")
            except (ValueError, ProcessLookupError, PermissionError, OverflowError, OSError):
                status_parts.append("Watcher: not running")
        else:
            status_parts.append("Watcher: not running (no PID file)")

        if _is_recording_live():
            target = _resolve_transcript()
            status_parts.append(f"Recording: ACTIVE ({target.name if target else 'unknown'})")
        else:
            status_parts.append("Recording: inactive")

        if TRANSCRIPTS_DIR.exists():
            txt_files = [f for f in TRANSCRIPTS_DIR.glob("*.txt")
                         if f.name != "meeting_transcript.txt" and not f.is_symlink()]
            total_size = sum(f.stat().st_size for f in txt_files)
            status_parts.append(f"Transcripts: {len(txt_files)} meetings ({total_size // 1024 // 1024} MB)")
        else:
            status_parts.append("Transcripts: none")

        try:
            usage = shutil.disk_usage(TRANSCRIPTS_DIR.parent)
            free_gb = usage.free / (1024 ** 3)
            status_parts.append(f"Disk free: {free_gb:.1f} GB")
        except Exception:
            pass

        return [types.TextContent(type="text", text="\n".join(status_parts))]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
