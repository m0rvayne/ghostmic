"""
MCP server: live meeting transcript + conference map generator.
"""
import os
import re
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp import types

CURRENT = Path(__file__).parent / "transcripts" / "meeting_transcript.txt"
TRANSCRIPTS_DIR = Path(__file__).parent / "transcripts"
MINDNODE_TMP = Path("/tmp/mindnode-mcp")

server = Server("meeting-transcript")


def read_transcript_text() -> str:
    if not CURRENT.exists():
        return ""
    return CURRENT.read_text(encoding="utf-8").strip()


def open_in_mindnode(outline: str, title: str) -> str:
    MINDNODE_TMP.mkdir(parents=True, exist_ok=True)
    safe_title = re.sub(r'[^a-zA-Zа-яА-ЯёЁ0-9_ -]', '_', title)
    filepath = MINDNODE_TMP / f"{safe_title}.md"
    filepath.write_text(outline, encoding="utf-8")
    subprocess.run(["open", "-a", "MindNode", str(filepath)], timeout=10)
    return str(filepath)


@server.list_tools()
async def list_tools():
    return [
        types.Tool(
            name="create_conference_map",
            description=(
                "TRIGGER: when user says 'покажи карту текущей конференции', "
                "'покажи карту конференции', 'карта встречи', 'карта совещания', "
                "'show conference map', 'meeting map'. "
                "Read the transcript, then build a Markdown mind map outline and call this tool "
                "with the 'outline' parameter. Opens the result in MindNode. "
                "IMPORTANT: call immediately with complete outline — do NOT ask for clarification.\n\n"
                "ADAPTIVE DEPTH RULES — choose nesting depth based on content volume and logical structure:\n\n"
                "LEVEL 1 (#) — meeting title only.\n\n"
                "LEVEL 2 (##) — main themes/blocks of the meeting (2–7 nodes). "
                "Use for: agenda items, problem areas, project blocks, key decisions made.\n\n"
                "LEVEL 3 (###) — always present. Sub-topics within each theme: "
                "specific questions discussed, named proposals, identified problems.\n\n"
                "LEVEL 4 (####) — add when a sub-topic has its own internal structure: "
                "arguments for/against, solution options compared, task breakdown, "
                "named participants' positions, step-by-step plans.\n\n"
                "LEVEL 5 (#####) — add when level-4 node itself branches: "
                "pros/cons of a specific option, sub-steps of a plan, "
                "details of a specific person's position, criteria for a decision.\n\n"
                "LEVEL 6 (######) — use only for deeply nested logical chains: "
                "e.g. Decision → Rationale → Constraint → Consequence → Action → Owner.\n\n"
                "SIGNALS that require going deeper (add one more level):\n"
                "- A node has 3+ distinct sub-points that are not just list items\n"
                "- Discussion contains comparison of 2+ alternatives with their own attributes\n"
                "- A decision has a chain: what decided → why → what changes → who does what → deadline\n"
                "- A problem has: symptom → root cause → proposed fix → expected result\n"
                "- A task has: goal → steps → responsible → deadline → success criteria\n"
                "- Participant expressed a position with supporting arguments\n\n"
                "SIGNALS to stay shallow (do NOT add depth artificially):\n"
                "- Node content is a simple enumeration without internal logic\n"
                "- Sub-points are variations of the same thing, not a hierarchy\n"
                "- Topic was mentioned briefly without development\n\n"
                "DEPTH CALIBRATION by meeting length:\n"
                "- Short meeting / few topics → 3 levels is enough\n"
                "- Medium meeting with detailed discussion → 4 levels typical\n"
                "- Long meeting, complex decisions, multiple participants → 4–5 levels\n"
                "- Strategic session, multi-layer decisions, detailed plans → up to 6 levels\n\n"
                "Each node must be a noun phrase (not a sentence). Preserve names, numbers, dates."
            ),
            inputSchema={
                "type": "object",
                "required": ["outline", "title"],
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Title for the mind map, e.g. 'Совещание 27.03.2026'",
                    },
                    "outline": {
                        "type": "string",
                        "description": (
                            "Full Markdown outline. Depth is adaptive — use as many heading levels "
                            "as the content logically requires (# through ######)."
                        ),
                    },
                },
            },
        ),
        types.Tool(
            name="read_meeting_transcript",
            description=(
                "Read the raw live meeting transcript. "
                "Call this when user asks: 'что обсуждали', 'расскажи что говорили', "
                "'сделай резюме', 'итоги встречи', 'what did we discuss'. "
                "For MAP requests use create_conference_map instead."
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
            description="List all recorded meeting transcripts with dates.",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="read_past_meeting",
            description="Read a transcript from a past meeting by filename.",
            inputSchema={
                "type": "object",
                "required": ["filename"],
                "properties": {
                    "filename": {"type": "string"},
                },
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict):
    if name == "create_conference_map":
        outline = arguments.get("outline", "")
        title = arguments.get("title", f"Совещание {datetime.now().strftime('%d.%m.%Y')}")

        if not outline:
            # Fallback: read transcript and return it for Claude to structure
            text = read_transcript_text()
            if not text:
                return [types.TextContent(type="text", text="Транскрипт пуст — запись ещё не началась или нет аудио.")]
            return [types.TextContent(type="text", text=(
                f"Вот транскрипт встречи. Структурируй его как Markdown outline "
                f"и вызови create_conference_map снова с параметром outline:\n\n{text}"
            ))]

        # Read transcript to embed it as context (optional, for display)
        transcript = read_transcript_text()

        try:
            filepath = open_in_mindnode(outline, title)
            return [types.TextContent(type="text", text=f"Карта '{title}' открыта в MindNode.")]
        except Exception as e:
            return [types.TextContent(type="text", text=f"Ошибка открытия в MindNode: {e}")]

    elif name == "read_meeting_transcript":
        text = read_transcript_text()
        if not text:
            return [types.TextContent(type="text", text="Транскрипт пуст.")]

        last_minutes = arguments.get("last_minutes")
        if last_minutes:
            lines = [l for l in text.split("\n") if l.strip()]
            keep = max(1, int(last_minutes * 2))
            text = "\n".join(lines[-keep:] if len(lines) > keep else lines)

        return [types.TextContent(type="text", text=text)]

    elif name == "list_past_meetings":
        if not TRANSCRIPTS_DIR.exists():
            return [types.TextContent(type="text", text="Нет записанных встреч.")]
        files = sorted(TRANSCRIPTS_DIR.glob("*.txt"), reverse=True)
        if not files:
            return [types.TextContent(type="text", text="Нет записанных встреч.")]
        lines = [f"- {f.name}  ({f.stat().st_size // 1024} KB)" for f in files]
        return [types.TextContent(type="text", text="\n".join(lines))]

    elif name == "read_past_meeting":
        path = TRANSCRIPTS_DIR / arguments.get("filename", "")
        if not path.exists():
            return [types.TextContent(type="text", text="Файл не найден.")]
        return [types.TextContent(type="text", text=path.read_text(encoding="utf-8"))]

    return [types.TextContent(type="text", text=f"Unknown tool: {name}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
