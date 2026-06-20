"""
MCP server: live meeting transcript + conference map generator.
"""
import os
import re
import subprocess
import time
from pathlib import Path
from datetime import datetime
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

CURRENT = Path(__file__).parent / "transcripts" / "meeting_transcript.txt"
TRANSCRIPTS_DIR = Path(__file__).parent / "transcripts"
MINDNODE_TMP = Path("/tmp/mindnode-mcp")
FRESHNESS_THRESHOLD = 180  # seconds
MAX_TRANSCRIPT_BYTES = 50 * 1024 * 1024  # 50 MB — refuse to read larger files
MAX_OUTLINE_BYTES = 1024 * 1024  # 1 MB
MAX_PAST_MEETINGS = 200

server = Server("meeting-transcript")


def _resolve_transcript() -> Path | None:
    """Resolve the current transcript symlink, with boundary check."""
    try:
        target = CURRENT.resolve() if CURRENT.is_symlink() else CURRENT
        if not target.exists():
            return None
        # Boundary check — symlink must point within transcripts dir
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
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    if "%" in filename:  # reject URL-encoded paths
        return None
    candidate = TRANSCRIPTS_DIR / filename
    if candidate.is_symlink():  # reject symlinks as defense-in-depth
        return None
    path = candidate.resolve()
    if not path.is_relative_to(TRANSCRIPTS_DIR.resolve()):
        return None
    if not path.exists():
        return None
    return path


def read_transcript_text() -> str:
    target = _resolve_transcript()
    if not target:
        return ""
    if target.stat().st_size > MAX_TRANSCRIPT_BYTES:
        return "[Transcript too large to read in full]"
    return target.read_text(encoding="utf-8").strip()


def _filter_by_minutes(text: str, last_minutes: float) -> str:
    """Filter transcript lines to only include the last N minutes based on timestamps."""
    if not (last_minutes and last_minutes > 0 and last_minutes == last_minutes):  # handles NaN
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


def open_in_mindnode(outline: str, title: str) -> str:
    if len(outline.encode("utf-8")) > MAX_OUTLINE_BYTES:
        raise ValueError("Outline too large (max 1 MB)")

    MINDNODE_TMP.mkdir(parents=True, exist_ok=True)
    # Clean up old files
    for old in MINDNODE_TMP.glob("*.md"):
        try:
            if time.time() - old.stat().st_mtime > 3600:
                old.unlink()
        except Exception:
            pass

    safe_title = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9_ -]', '_', title)[:100]
    filepath = MINDNODE_TMP / f"{safe_title}.md"
    filepath.write_text(outline, encoding="utf-8")
    subprocess.run(["open", "-a", "MindNode", str(filepath)], timeout=10)
    return str(filepath)


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
        return path.read_text(encoding="utf-8")

    return "Unknown resource."


# -- Tools --------------------------------------------------------------------

@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="create_conference_map",
            description=(
                "Build a mind map from the meeting transcript and open it in MindNode. "
                "Trigger phrases: 'conference map', 'meeting map', 'mind map', "
                "'карта встречи', 'карта конференции', 'карта совещания'. "
                "Read the transcript first, then call this tool with a Markdown outline. "
                "Use adaptive heading depth (# through ######) based on content complexity. "
                "Each node: noun phrase, preserve names/numbers/dates. "
                "Go deeper when a node has 3+ distinct sub-points, comparisons, or decision chains. "
                "Stay shallow for simple enumerations or briefly mentioned topics."
            ),
            inputSchema={
                "type": "object",
                "required": ["outline", "title"],
                "properties": {
                    "title": {"type": "string", "description": "Mind map title"},
                    "outline": {"type": "string", "description": "Full Markdown outline with adaptive heading depth"},
                },
            },
        ),
        types.Tool(
            name="read_meeting_transcript",
            description=(
                "Read the current live meeting transcript. "
                "Use when asked: 'what was discussed', 'summarize the meeting', "
                "'что обсуждали', 'итоги встречи'. "
                "For mind map requests use create_conference_map instead."
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
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None):
    arguments = arguments or {}

    if name == "create_conference_map":
        outline = arguments.get("outline", "")
        title = arguments.get("title", f"Meeting {datetime.now().strftime('%d.%m.%Y')}")

        if not outline:
            text = read_transcript_text()
            if not text:
                return [types.TextContent(type="text", text="Transcript is empty — recording has not started or no audio detected.")]
            note = _freshness_note()
            return [types.TextContent(type="text", text=(
                f"{note}Here is the meeting transcript. Structure it as a Markdown outline "
                f"and call create_conference_map again with the 'outline' parameter:\n\n{text}"
            ))]

        try:
            open_in_mindnode(outline, title)
            return [types.TextContent(type="text", text=f"Map '{title}' opened in MindNode.")]
        except Exception:
            return [types.TextContent(type="text", text="Failed to open in MindNode. Is MindNode installed?")]

    elif name == "read_meeting_transcript":
        text = read_transcript_text()
        if not text:
            return [types.TextContent(type="text", text="Transcript is empty.")]

        last_minutes = arguments.get("last_minutes")
        if last_minutes is not None:
            try:
                text = _filter_by_minutes(text, float(last_minutes))
            except (ValueError, TypeError):
                pass  # ignore invalid values, return full transcript

        note = _freshness_note()
        return [types.TextContent(type="text", text=note + text)]

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
        return [types.TextContent(type="text", text=path.read_text(encoding="utf-8"))]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
