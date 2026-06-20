"""Tests for server.py — path traversal, input validation, resources, tools."""
import asyncio
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
import pytest

_tmpdir = None
_transcripts = None


@pytest.fixture(autouse=True)
def setup_dirs(tmp_path):
    global _tmpdir, _transcripts
    _tmpdir = tmp_path
    _transcripts = tmp_path / "transcripts"
    _transcripts.mkdir()

    with patch.multiple(
        "server",
        TRANSCRIPTS_DIR=_transcripts,
        CURRENT=_transcripts / "meeting_transcript.txt",
    ):
        yield


@pytest.fixture
def sample_transcript():
    txt = _transcripts / "2026-06-20_14-30-00.txt"
    txt.write_text(
        "============================================================\n"
        "Meeting started: 2026-06-20 14:30\n"
        "============================================================\n"
        "[14:30:05-14:30:35] Hello everyone, let's discuss the roadmap.\n"
        "[14:30:35-14:31:05] First item is the Q3 launch timeline.\n"
        "[14:31:05-14:31:35] We need to finalize the API by July 15th.\n"
        "[14:31:35-14:32:05] Design team will deliver mockups next week.\n"
        "[14:32:05-14:32:35] Any concerns about the deadline?\n",
        encoding="utf-8",
    )
    symlink = _transcripts / "meeting_transcript.txt"
    symlink.symlink_to(txt)
    os.utime(txt, None)
    return txt


import server


def _run(coro):
    """Run an async coroutine in a fresh event loop."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestSafeTranscriptPath:
    def test_normal_filename(self):
        f = _transcripts / "2026-01-01_10-00-00.txt"
        f.write_text("test")
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = server._safe_transcript_path("2026-01-01_10-00-00.txt")
        assert result is not None

    def test_path_traversal_dotdot(self):
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            assert server._safe_transcript_path("../../etc/passwd") is None

    def test_path_traversal_slash(self):
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            assert server._safe_transcript_path("/etc/passwd") is None

    def test_path_traversal_backslash(self):
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            assert server._safe_transcript_path("..\\..\\etc\\passwd") is None

    def test_url_encoded_traversal(self):
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            assert server._safe_transcript_path("..%2F..%2Fetc%2Fpasswd") is None

    def test_empty_filename(self):
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            assert server._safe_transcript_path("") is None

    def test_nonexistent_file(self):
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            assert server._safe_transcript_path("nonexistent.txt") is None

    def test_symlink_rejected(self):
        target = _transcripts / "real.txt"
        target.write_text("real content")
        link = _transcripts / "sneaky.txt"
        link.symlink_to(target)
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            assert server._safe_transcript_path("sneaky.txt") is None

    def test_symlink_outside_rejected(self):
        outside = _tmpdir / "secret.txt"
        outside.write_text("secret")
        link = _transcripts / "escape.txt"
        link.symlink_to(outside)
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            assert server._safe_transcript_path("escape.txt") is None


class TestResolveTranscript:
    def test_valid_symlink(self, sample_transcript):
        with patch("server.CURRENT", _transcripts / "meeting_transcript.txt"), \
             patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = server._resolve_transcript()
        assert result is not None
        assert result.name == "2026-06-20_14-30-00.txt"

    def test_symlink_outside_boundary(self):
        outside = _tmpdir / "evil.txt"
        outside.write_text("gotcha")
        symlink = _transcripts / "meeting_transcript.txt"
        symlink.symlink_to(outside)
        with patch("server.CURRENT", symlink), \
             patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = server._resolve_transcript()
        assert result is None

    def test_dangling_symlink(self):
        symlink = _transcripts / "meeting_transcript.txt"
        symlink.symlink_to(_transcripts / "nonexistent.txt")
        with patch("server.CURRENT", symlink), \
             patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = server._resolve_transcript()
        assert result is None

    def test_no_symlink(self):
        with patch("server.CURRENT", _transcripts / "meeting_transcript.txt"), \
             patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = server._resolve_transcript()
        assert result is None


class TestFilterByMinutes:
    def test_last_5_minutes(self):
        text = (
            "[14:00:00-14:00:30] Old stuff\n"
            "[14:25:00-14:25:30] Recent stuff\n"
            "[14:28:00-14:28:30] Very recent\n"
            "[14:30:00-14:30:30] Latest\n"
        )
        result = server._filter_by_minutes(text, 5.0)
        assert "Old stuff" not in result
        assert "Recent stuff" in result
        assert "Latest" in result

    def test_negative_returns_full(self):
        text = "[14:00:00-14:00:30] Something\n"
        result = server._filter_by_minutes(text, -1.0)
        assert "Something" in result

    def test_zero_returns_full(self):
        text = "[14:00:00-14:00:30] Something\n"
        result = server._filter_by_minutes(text, 0.0)
        assert "Something" in result

    def test_nan_returns_full(self):
        text = "[14:00:00-14:00:30] Something\n"
        result = server._filter_by_minutes(text, float("nan"))
        assert "Something" in result

    def test_no_timestamps(self):
        text = "Line one\nLine two\nLine three\n"
        result = server._filter_by_minutes(text, 5.0)
        assert "Line" in result

    def test_empty_text(self):
        result = server._filter_by_minutes("", 5.0)
        assert result == ""


class TestReadTranscriptText:
    def test_reads_through_symlink(self, sample_transcript):
        with patch("server.CURRENT", _transcripts / "meeting_transcript.txt"), \
             patch("server.TRANSCRIPTS_DIR", _transcripts):
            text = server.read_transcript_text()
        assert "Hello everyone" in text

    def test_too_large_file(self, sample_transcript):
        with patch("server.CURRENT", _transcripts / "meeting_transcript.txt"), \
             patch("server.TRANSCRIPTS_DIR", _transcripts), \
             patch("server.MAX_TRANSCRIPT_BYTES", 10):
            text = server.read_transcript_text()
        assert "too large" in text.lower()

    def test_no_file(self):
        with patch("server.CURRENT", _transcripts / "meeting_transcript.txt"), \
             patch("server.TRANSCRIPTS_DIR", _transcripts):
            text = server.read_transcript_text()
        assert text == ""


class TestCallTool:
    def test_unknown_tool(self):
        result = _run(server.call_tool("nonexistent_tool", {}))
        assert "Unknown tool" in result[0].text

    def test_none_arguments(self):
        result = _run(server.call_tool("list_past_meetings", None))
        assert result is not None

    def test_read_past_meeting_traversal(self):
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = _run(server.call_tool("read_past_meeting", {"filename": "../../etc/passwd"}))
        assert "not found" in result[0].text.lower() or "invalid" in result[0].text.lower()

    def test_last_minutes_string_value(self):
        f = _transcripts / "meeting_transcript.txt"
        f.write_text("[14:00:00-14:00:30] Test content\n")
        with patch("server.CURRENT", f), \
             patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = _run(server.call_tool("read_meeting_transcript", {"last_minutes": "not_a_number"}))
        assert "Test content" in result[0].text


class TestListPastMeetings:
    def test_empty_dir(self):
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = _run(server.call_tool("list_past_meetings", {}))
        assert "No recorded" in result[0].text

    def test_filters_symlinks(self, sample_transcript):
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = _run(server.call_tool("list_past_meetings", {}))
        assert "2026-06-20_14-30-00.txt" in result[0].text
        assert result[0].text.count("2026-06-20_14-30-00.txt") == 1

    def test_respects_max_limit(self):
        for i in range(10):
            (_transcripts / f"meeting_{i:03d}.txt").write_text(f"content {i}")
        with patch("server.TRANSCRIPTS_DIR", _transcripts), \
             patch("server.MAX_PAST_MEETINGS", 3):
            result = _run(server.call_tool("list_past_meetings", {}))
        lines = [l for l in result[0].text.split("\n") if l.strip().startswith("-")]
        assert len(lines) == 3


class TestSearchTranscripts:
    def test_finds_matching_content(self):
        (_transcripts / "meeting_a.txt").write_text("[14:00:00-14:00:30] We discussed the budget for Q3\n")
        (_transcripts / "meeting_b.txt").write_text("[14:00:00-14:00:30] Sprint planning session\n")
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = _run(server.call_tool("search_transcripts", {"query": "budget"}))
        assert "budget" in result[0].text.lower()
        assert "meeting_a.txt" in result[0].text
        assert "meeting_b.txt" not in result[0].text

    def test_case_insensitive(self):
        (_transcripts / "meeting.txt").write_text("[14:00:00-14:00:30] The API launch is scheduled\n")
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = _run(server.call_tool("search_transcripts", {"query": "api"}))
        assert "API" in result[0].text

    def test_no_results(self):
        (_transcripts / "meeting.txt").write_text("[14:00:00-14:00:30] Nothing relevant here\n")
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = _run(server.call_tool("search_transcripts", {"query": "xyznonexistent"}))
        assert "No matches" in result[0].text

    def test_empty_query(self):
        with patch("server.TRANSCRIPTS_DIR", _transcripts):
            result = _run(server.call_tool("search_transcripts", {"query": ""}))
        assert "Empty" in result[0].text


class TestGetStatus:
    def test_returns_status_info(self):
        with patch("server.TRANSCRIPTS_DIR", _transcripts), \
             patch("server.PID_FILE", _tmpdir / "nonexistent.pid"), \
             patch("server.INSTALL_DIR", _tmpdir):
            result = _run(server.call_tool("get_status", {}))
        text = result[0].text
        assert "Watcher:" in text
        assert "Recording:" in text
        assert "Transcripts:" in text

    def test_shows_transcript_count(self):
        for i in range(5):
            (_transcripts / f"meeting_{i}.txt").write_text(f"content {i}")
        with patch("server.TRANSCRIPTS_DIR", _transcripts), \
             patch("server.PID_FILE", _tmpdir / "nonexistent.pid"), \
             patch("server.INSTALL_DIR", _tmpdir):
            result = _run(server.call_tool("get_status", {}))
        assert "5 meetings" in result[0].text
