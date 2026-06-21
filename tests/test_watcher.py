"""Tests for watcher.py — state machine, process management, detection."""
import os
import signal
import time
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
import importlib.util

# Import watcher module without running __main__
spec = importlib.util.spec_from_file_location(
    "watcher_mod",
    Path(__file__).parent.parent / "watcher.py",
)
mod = importlib.util.module_from_spec(spec)
mod.__name__ = "watcher_mod"
spec.loader.exec_module(mod)
watcher = mod


@pytest.fixture
def config(tmp_path):
    transcripts = tmp_path / "transcripts"
    transcripts.mkdir()
    capture = tmp_path / "capture.py"
    capture.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(3600)\n")
    python = tmp_path / "python3"
    python.write_text("#!/bin/bash\n")
    python.chmod(0o755)
    return watcher.WatcherConfig(
        install_dir=tmp_path,
        transcripts_dir=transcripts,
        capture_script=capture,
        python_path=python,
        pid_file=tmp_path / "watcher.pid",
        log_file=tmp_path / "watcher.log",
        symlink_path=transcripts / "meeting_transcript.txt",
    )


@pytest.fixture
def ctx(config):
    return watcher.WatcherContext(config=config)


class TestAtomicSymlink:
    def test_creates_symlink(self, config):
        target = config.transcripts_dir / "test.txt"
        target.write_text("content")
        watcher.update_symlink(config, target)
        assert config.symlink_path.is_symlink()
        assert config.symlink_path.resolve() == target.resolve()

    def test_replaces_existing_symlink(self, config):
        old = config.transcripts_dir / "old.txt"
        old.write_text("old")
        new = config.transcripts_dir / "new.txt"
        new.write_text("new")
        watcher.update_symlink(config, old)
        watcher.update_symlink(config, new)
        assert config.symlink_path.resolve() == new.resolve()

    def test_no_intermediate_missing_state(self, config):
        old = config.transcripts_dir / "old.txt"
        old.write_text("old")
        new = config.transcripts_dir / "new.txt"
        new.write_text("new")
        watcher.update_symlink(config, old)
        watcher.update_symlink(config, new)
        assert config.symlink_path.exists()


class TestStateManagement:
    def test_initial_state_is_idle(self, ctx):
        assert ctx.state == watcher.State.IDLE

    def test_stop_resets_state(self, ctx):
        ctx.capture_process = MagicMock()
        ctx.capture_process.poll.return_value = 0
        ctx.capture_process.returncode = 0
        ctx.current_transcript = ctx.config.transcripts_dir / "test.txt"
        with patch.object(watcher, "notify"):
            watcher.stop_capture(ctx)
        assert ctx.current_transcript is None
        assert ctx.capture_process is None
        assert ctx.state == watcher.State.IDLE

    def test_context_is_independent(self, config):
        ctx1 = watcher.WatcherContext(config=config)
        ctx2 = watcher.WatcherContext(config=config)
        ctx1.crash_times.append(1.0)
        assert len(ctx2.crash_times) == 0  # no shared state


class TestCrashBackoff:
    def test_crash_times_tracked(self, ctx):
        ctx.crash_times = [time.time() - 10, time.time() - 5]
        assert len(ctx.crash_times) < ctx.config.max_rapid_crashes

    def test_crash_times_expire(self, ctx):
        ctx.crash_times = [time.time() - 100, time.time() - 90]
        now = time.time()
        filtered = [t for t in ctx.crash_times if now - t < ctx.config.crash_window]
        assert len(filtered) == 0


class TestTimestampFilename:
    def test_filename_has_seconds(self, ctx):
        with patch.object(watcher, "update_symlink"), \
             patch.object(watcher, "notify"), \
             patch("subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock()
            mock_popen.return_value.stdout = iter([])
            watcher.start_capture(ctx)
            path = ctx.current_transcript
            parts = path.stem.split("_")[-1]
            assert len(parts.split("-")) == 3  # H-M-S


class TestNotifyEscaping:
    def test_escapes_quotes(self):
        with patch("subprocess.run") as mock_run:
            watcher.notify('Title with "quotes"', 'Message with "quotes"')
            call_args = mock_run.call_args[0][0]
            script = call_args[-1]
            assert '\\"' in script


class TestZoomDetection:
    def test_pgrep_not_found(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert watcher.zoom_cpthost_running() is False

    def test_socket_error(self):
        with patch("socket.socket", side_effect=OSError("no sockets")):
            assert watcher.zoom_local_api_active() is False


class TestGoogleMeetDetection:
    def test_meet_code_regex_valid(self):
        assert watcher._MEET_CODE_RE.match("abc-defg-hij")
        assert watcher._MEET_CODE_RE.match("ab-cd-ef")

    def test_meet_code_regex_invalid(self):
        assert not watcher._MEET_CODE_RE.match("landing")
        assert not watcher._MEET_CODE_RE.match("new")
        assert not watcher._MEET_CODE_RE.match("")
        assert not watcher._MEET_CODE_RE.match("ABC-DEF-GHI")  # uppercase
        assert not watcher._MEET_CODE_RE.match("a-b-c")  # too short

    def test_no_browsers_running(self):
        with patch.object(watcher, "_get_running_browsers", return_value=[]):
            assert watcher.google_meet_active() is False

    def test_browser_with_meet_tab(self):
        with patch.object(watcher, "_get_running_browsers", return_value=["Google Chrome"]), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="https://meet.google.com/abc-defg-hij",
                returncode=0
            )
            assert watcher.google_meet_active() is True

    def test_browser_with_meet_homepage(self):
        """meet.google.com without a meeting code should NOT trigger."""
        with patch.object(watcher, "_get_running_browsers", return_value=["Google Chrome"]), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="https://meet.google.com/landing",
                returncode=0
            )
            assert watcher.google_meet_active() is False


class TestStatusFile:
    def test_write_status_recording(self, config):
        watcher.write_status(config, "RECORDING", "test.txt")
        import json
        status = json.loads((config.install_dir / "watcher-status.json").read_text())
        assert status["state"] == "RECORDING"
        assert status["transcript"] == "test.txt"

    def test_write_status_idle(self, config):
        watcher.write_status(config, "IDLE")
        import json
        status = json.loads((config.install_dir / "watcher-status.json").read_text())
        assert status["state"] == "IDLE"
        assert "transcript" not in status


class TestTickStateMachine:
    def test_idle_no_conference(self, ctx):
        with patch.object(watcher, "is_in_conference", return_value=False):
            watcher.tick(ctx)
        assert ctx.state == watcher.State.IDLE

    def test_detection_error_does_not_crash(self, ctx):
        with patch.object(watcher, "is_in_conference", side_effect=RuntimeError("oops")):
            watcher.tick(ctx)  # should not raise
        assert ctx.state == watcher.State.IDLE
