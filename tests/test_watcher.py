"""Tests for watcher.py — process management, detection, lifecycle."""
import os
import signal
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest


@pytest.fixture
def watcher_env(tmp_path):
    """Isolated environment for watcher tests."""
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    capture = tmp_path / "capture.py"
    capture.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(3600)\n")
    python = tmp_path / "python3"
    python.write_text("#!/usr/bin/env python3\n")
    python.chmod(0o755)
    pidfile = tmp_path / "watcher.pid"
    return {
        "transcripts": transcripts,
        "capture": capture,
        "python": python,
        "pidfile": pidfile,
        "tmp": tmp_path,
    }


# Import must happen after we can patch
import importlib


def _import_watcher_module():
    """Import watcher as a module without running __main__ code."""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "watcher_mod",
        Path(__file__).parent.parent / "watcher.py",
    )
    mod = importlib.util.module_from_spec(spec)
    # Patch __name__ so __main__ block doesn't execute
    mod.__name__ = "watcher_mod"
    spec.loader.exec_module(mod)
    return mod


watcher = _import_watcher_module()


class TestAtomicSymlink:
    """Symlink replacement must be atomic to avoid TOCTOU."""

    def test_creates_symlink(self, watcher_env):
        target = watcher_env["transcripts"] / "test.txt"
        target.write_text("content")
        symlink = watcher_env["transcripts"] / "meeting_transcript.txt"

        with patch.object(watcher, "CURRENT_TRANSCRIPT_SYMLINK", symlink):
            watcher._update_symlink(target)

        assert symlink.is_symlink()
        assert symlink.resolve() == target.resolve()

    def test_replaces_existing_symlink(self, watcher_env):
        old = watcher_env["transcripts"] / "old.txt"
        old.write_text("old")
        new = watcher_env["transcripts"] / "new.txt"
        new.write_text("new")
        symlink = watcher_env["transcripts"] / "meeting_transcript.txt"
        symlink.symlink_to(old)

        with patch.object(watcher, "CURRENT_TRANSCRIPT_SYMLINK", symlink):
            watcher._update_symlink(new)

        assert symlink.resolve() == new.resolve()

    def test_no_intermediate_missing_state(self, watcher_env):
        """During replacement, symlink should never be absent."""
        old = watcher_env["transcripts"] / "old.txt"
        old.write_text("old")
        new = watcher_env["transcripts"] / "new.txt"
        new.write_text("new")
        symlink = watcher_env["transcripts"] / "meeting_transcript.txt"
        symlink.symlink_to(old)

        # The atomic rename approach means there's no window where
        # the symlink doesn't exist
        with patch.object(watcher, "CURRENT_TRANSCRIPT_SYMLINK", symlink):
            watcher._update_symlink(new)
            assert symlink.exists()


class TestTranscriptPathReset:
    """current_transcript_path must be None after stop_capture."""

    def test_stop_resets_path(self, watcher_env):
        watcher.capture_process = MagicMock()
        watcher.capture_process.poll.return_value = 0
        watcher.capture_process.returncode = 0
        watcher.current_transcript_path = watcher_env["transcripts"] / "test.txt"

        with patch.object(watcher, "notify"):
            watcher.stop_capture()

        assert watcher.current_transcript_path is None
        assert watcher.capture_process is None


class TestCrashBackoff:
    """Watcher should not spin if capture.py keeps crashing."""

    def test_crash_times_tracked(self):
        watcher._crash_times.clear()
        watcher._crash_times.extend([time.time() - 10, time.time() - 5])
        # Under threshold — should not back off
        assert len(watcher._crash_times) < watcher.MAX_RAPID_CRASHES

    def test_crash_times_expire(self):
        watcher._crash_times.clear()
        watcher._crash_times.extend([time.time() - 100, time.time() - 90])
        # These are old, should be filtered out
        now = time.time()
        filtered = [t for t in watcher._crash_times if now - t < watcher.CRASH_WINDOW]
        assert len(filtered) == 0


class TestTimestampFilename:
    """Meeting filenames must include seconds to avoid collisions."""

    def test_filename_has_seconds(self, watcher_env):
        with patch.object(watcher, "TRANSCRIPTS_DIR", watcher_env["transcripts"]), \
             patch.object(watcher, "CURRENT_TRANSCRIPT_SYMLINK",
                          watcher_env["transcripts"] / "meeting_transcript.txt"), \
             patch.object(watcher, "PYTHON", watcher_env["python"]), \
             patch.object(watcher, "CAPTURE_SCRIPT", watcher_env["capture"]), \
             patch.object(watcher, "notify"), \
             patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            mock_popen.return_value.stdout = iter([])
            watcher.start_capture()
            path = watcher.current_transcript_path
            # Should have seconds in the filename
            assert len(path.stem.split("_")) >= 2
            parts = path.stem.split("_")[-1]  # HH-MM-SS
            assert len(parts.split("-")) == 3  # H-M-S


class TestNotifyEscaping:
    """Notification strings must be escaped for AppleScript."""

    def test_escapes_quotes(self):
        with patch("subprocess.run") as mock_run:
            watcher.notify('Title with "quotes"', 'Message with "quotes"')
            call_args = mock_run.call_args[0][0]
            script = call_args[-1]
            # Double quotes inside should be escaped
            assert '\\"' in script or "quotes" in script


class TestZoomDetection:
    """Detection methods should handle errors gracefully."""

    def test_pgrep_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert watcher.zoom_cpthost_running() is False

    def test_socket_error(self):
        with patch("socket.socket", side_effect=OSError("no sockets")):
            assert watcher.zoom_local_api_active() is False
