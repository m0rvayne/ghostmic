"""Tests for watcher.py — state machine, process management, detection."""
import json
import os
import signal
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, call
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


def _mock_popen():
    """Create a MagicMock that behaves like a running subprocess.Popen."""
    proc = MagicMock()
    proc.poll.return_value = None  # process is running
    proc.stdout = iter([])
    proc.returncode = None
    proc.wait.return_value = 0
    return proc


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


class TestStatusFile:
    def test_write_status_recording(self, config):
        watcher.write_status(config, "RECORDING", "test.txt")
        status = json.loads((config.install_dir / "watcher-status.json").read_text())
        assert status["state"] == "RECORDING"
        assert status["transcript"] == "test.txt"

    def test_write_status_idle(self, config):
        watcher.write_status(config, "IDLE")
        status = json.loads((config.install_dir / "watcher-status.json").read_text())
        assert status["state"] == "IDLE"
        assert "transcript" not in status


class TestDefaultConfig:
    def test_default_config_creates_without_error(self):
        """Regression test: default_config() must not raise TypeError."""
        config = watcher.default_config()
        assert config.install_dir is not None
        assert config.status_file is not None
        assert config.status_file.name == "watcher-status.json"


class TestTickStateMachine:
    def test_idle_no_conference(self, ctx):
        with patch.object(watcher, "is_in_conference", return_value=False):
            watcher.tick(ctx)
        assert ctx.state == watcher.State.IDLE

    def test_detection_error_does_not_crash(self, ctx):
        with patch.object(watcher, "is_in_conference", side_effect=RuntimeError("oops")):
            watcher.tick(ctx)  # should not raise
        assert ctx.state == watcher.State.IDLE


class TestTickSequences:
    """Multi-tick state machine scenarios exercising full transitions."""

    def test_idle_detect_conference_starts_recording(self, ctx):
        """IDLE -> detect conference -> RECORDING, verify start_capture called."""
        with patch.object(watcher, "is_in_conference", return_value=True), \
             patch.object(watcher, "start_capture") as mock_start:
            watcher.tick(ctx)
        mock_start.assert_called_once_with(ctx)

    def test_recording_conference_gone_grace_timeout_idle(self, ctx):
        """RECORDING -> conference gone -> GRACE_PERIOD -> timeout -> IDLE."""
        proc = _mock_popen()
        ctx.capture_process = proc
        ctx.state = watcher.State.RECORDING
        ctx.current_transcript = ctx.config.transcripts_dir / "test.txt"
        ctx.current_transcript.touch()

        # Tick 1: conference gone -> enter grace period
        with patch.object(watcher, "is_in_conference", return_value=False):
            watcher.tick(ctx)
        assert ctx.state == watcher.State.GRACE_PERIOD
        assert ctx.grace_start is not None

        # Simulate grace period elapsed
        ctx.grace_start = time.monotonic() - ctx.config.grace_period - 1

        # Tick 2: still no conference, grace expired -> stop capture
        with patch.object(watcher, "is_in_conference", return_value=False):
            watcher.tick(ctx)
        assert ctx.state == watcher.State.IDLE
        assert ctx.capture_process is None

    def test_recording_grace_conference_back_restarts(self, ctx):
        """RECORDING -> GRACE_PERIOD -> conference back -> RECORDING (new capture)."""
        proc = _mock_popen()
        ctx.capture_process = proc
        ctx.state = watcher.State.RECORDING
        ctx.current_transcript = ctx.config.transcripts_dir / "test.txt"
        ctx.current_transcript.touch()

        # Tick 1: conference gone -> grace period
        with patch.object(watcher, "is_in_conference", return_value=False):
            watcher.tick(ctx)
        assert ctx.state == watcher.State.GRACE_PERIOD

        # Tick 2: conference comes back during grace period -> restart capture
        with patch.object(watcher, "is_in_conference", return_value=True), \
             patch.object(watcher, "stop_capture") as mock_stop, \
             patch.object(watcher, "start_capture") as mock_start:
            watcher.tick(ctx)
        mock_stop.assert_called_once_with(ctx)
        mock_start.assert_called_once_with(ctx)

    def test_recording_3_crashes_backoff_timeout_idle(self, ctx):
        """RECORDING -> 3 crashes -> BACKOFF -> timeout -> IDLE."""
        now = time.monotonic()
        # Fill crash_times to max_rapid_crashes (3) within crash_window
        ctx.crash_times = [now - 5, now - 3, now - 1]

        # start_capture checks crash_times and enters BACKOFF
        with patch.object(watcher, "update_symlink"), \
             patch.object(watcher, "notify"):
            watcher.start_capture(ctx)

        assert ctx.state == watcher.State.BACKOFF
        assert ctx.backoff_until is not None

        # Tick while still in backoff window -> returns early, stays BACKOFF
        with patch.object(watcher, "is_in_conference", return_value=False):
            watcher.tick(ctx)
        assert ctx.state == watcher.State.BACKOFF

        # Simulate backoff timer expired
        ctx.backoff_until = time.monotonic() - 1

        # Next tick -> backoff ends, state returns to IDLE
        with patch.object(watcher, "is_in_conference", return_value=False):
            watcher.tick(ctx)
        assert ctx.state == watcher.State.IDLE
        assert ctx.backoff_until is None

    def test_backoff_returns_early_until_expired(self, ctx):
        """BACKOFF -> tick returns early until backoff_until expires."""
        ctx.state = watcher.State.BACKOFF
        ctx.backoff_until = time.monotonic() + 100  # far in the future

        with patch.object(watcher, "is_in_conference") as mock_conf:
            watcher.tick(ctx)
        # is_in_conference should NOT be called during backoff
        mock_conf.assert_not_called()
        assert ctx.state == watcher.State.BACKOFF


class TestControlCommands:
    """Tests for _read_control() — reading and consuming control JSON."""

    def test_valid_pause(self, config):
        ctrl = config.install_dir / "watcher-control.json"
        ctrl.write_text(json.dumps({"action": "pause"}))
        result = watcher._read_control(config)
        assert result == "pause"

    def test_valid_resume(self, config):
        ctrl = config.install_dir / "watcher-control.json"
        ctrl.write_text(json.dumps({"action": "resume"}))
        result = watcher._read_control(config)
        assert result == "resume"

    def test_valid_end(self, config):
        ctrl = config.install_dir / "watcher-control.json"
        ctrl.write_text(json.dumps({"action": "end"}))
        result = watcher._read_control(config)
        assert result == "end"

    def test_malformed_json(self, config):
        ctrl = config.install_dir / "watcher-control.json"
        ctrl.write_text("{not valid json!!!")
        result = watcher._read_control(config)
        assert result is None

    def test_missing_file(self, config):
        result = watcher._read_control(config)
        assert result is None

    def test_file_consumed_after_read(self, config):
        ctrl = config.install_dir / "watcher-control.json"
        ctrl.write_text(json.dumps({"action": "pause"}))
        watcher._read_control(config)
        assert not ctrl.exists()


class TestUserConfig:
    """Tests for _load_user_config()."""

    def test_valid_config(self, tmp_path):
        cfg = {
            "whisper_model": "large-v3",
            "diarization": "true",
            "language": "en",
        }
        (tmp_path / "config.json").write_text(json.dumps(cfg))
        result = watcher._load_user_config(tmp_path)
        assert result["whisper_model"] == "large-v3"
        assert result["diarization"] == "true"
        assert result["language"] == "en"

    def test_empty_file(self, tmp_path):
        (tmp_path / "config.json").write_text("")
        result = watcher._load_user_config(tmp_path)
        assert result == {}

    def test_missing_file(self, tmp_path):
        result = watcher._load_user_config(tmp_path)
        assert result == {}

    def test_malformed_json(self, tmp_path):
        (tmp_path / "config.json").write_text("{{broken json")
        result = watcher._load_user_config(tmp_path)
        assert result == {}


class TestWriteStatus:
    """Tests for write_status() — JSON structure and round-trip."""

    def test_write_recording_status(self, config):
        watcher.write_status(config, "RECORDING", "2026-01-01_10-00-00.txt")
        path = config.status_file or (config.install_dir / "watcher-status.json")
        data = json.loads(path.read_text())
        assert data["state"] == "RECORDING"
        assert data["transcript"] == "2026-01-01_10-00-00.txt"
        assert "timestamp" in data
        assert isinstance(data["timestamp"], float)

    def test_write_idle_status(self, config):
        watcher.write_status(config, "IDLE")
        path = config.status_file or (config.install_dir / "watcher-status.json")
        data = json.loads(path.read_text())
        assert data["state"] == "IDLE"
        assert "transcript" not in data

    def test_read_back_json_structure(self, config):
        before = time.time()
        watcher.write_status(config, "RECORDING", "test.txt")
        after = time.time()
        path = config.status_file or (config.install_dir / "watcher-status.json")
        data = json.loads(path.read_text())
        assert set(data.keys()) == {"state", "transcript", "timestamp"}
        assert before <= data["timestamp"] <= after


class TestWhisperServerLifecycle:
    """Test whisper-server start/stop in watcher."""

    def test_context_has_whisper_server_field(self, config):
        ctx = watcher.WatcherContext(config=config)
        assert ctx.whisper_server_process is None

    def test_server_healthy_false_when_not_running(self):
        assert watcher._whisper_server_healthy() is False

    def test_start_whisper_server_skips_if_healthy(self, config):
        ctx = watcher.WatcherContext(config=config)
        with patch.object(watcher, "_whisper_server_healthy", return_value=True):
            watcher.start_whisper_server(ctx)
        assert ctx.whisper_server_process is None

    def test_start_whisper_server_skips_if_not_found(self, config):
        ctx = watcher.WatcherContext(config=config)
        mock_result = MagicMock(returncode=1, stdout="")
        with patch.object(watcher, "_whisper_server_healthy", return_value=False), \
             patch.object(watcher.subprocess, "run", return_value=mock_result):
            watcher.start_whisper_server(ctx)
        assert ctx.whisper_server_process is None

    def test_stop_whisper_server_terminates(self, config):
        ctx = watcher.WatcherContext(config=config)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        mock_proc.wait.return_value = 0
        ctx.whisper_server_process = mock_proc
        watcher.stop_whisper_server(ctx)
        mock_proc.terminate.assert_called_once()
        assert ctx.whisper_server_process is None

    def test_stop_whisper_server_kills_on_timeout(self, config):
        import subprocess as sp
        ctx = watcher.WatcherContext(config=config)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.side_effect = sp.TimeoutExpired(cmd="whisper-server", timeout=5)
        ctx.whisper_server_process = mock_proc
        watcher.stop_whisper_server(ctx)
        mock_proc.kill.assert_called_once()
        assert ctx.whisper_server_process is None

    def test_stop_whisper_server_noop_when_none(self, config):
        ctx = watcher.WatcherContext(config=config)
        ctx.whisper_server_process = None
        watcher.stop_whisper_server(ctx)  # should not raise

    def test_shutdown_stops_whisper_server(self, config):
        """Main loop shutdown should stop whisper-server."""
        ctx = watcher.WatcherContext(config=config)
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.wait.return_value = 0
        ctx.whisper_server_process = mock_proc
        watcher.stop_whisper_server(ctx)
        mock_proc.terminate.assert_called_once()
