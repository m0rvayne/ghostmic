"""Tests for capture.py — audio processing, queue bounds, thread safety."""
import queue
import sys
import threading
from unittest.mock import MagicMock
import numpy as np
import pytest
from pathlib import Path

# Mock sounddevice and faster_whisper before importing capture
# (they require hardware/native libraries not available in test env)
sys.modules["sounddevice"] = MagicMock()
sys.modules["faster_whisper"] = MagicMock()

# Now we can import the actual capture module
sys.path.insert(0, str(Path(__file__).parent.parent))
import capture as capture_mod


class TestResampleLinear:
    """Test the ACTUAL _resample_linear from capture.py."""

    def test_same_rate_passthrough(self):
        data = np.random.randn(16000).astype(np.float32)
        result = capture_mod._resample_linear(data, 16000, 16000)
        np.testing.assert_array_equal(result, data)

    def test_upsample_length(self):
        data = np.ones(16000, dtype=np.float32)
        result = capture_mod._resample_linear(data, 16000, 48000)
        assert len(result) == 48000

    def test_downsample_length(self):
        data = np.ones(48000, dtype=np.float32)
        result = capture_mod._resample_linear(data, 48000, 16000)
        assert len(result) == 16000

    def test_preserves_dc(self):
        data = np.full(16000, 0.5, dtype=np.float32)
        result = capture_mod._resample_linear(data, 16000, 48000)
        np.testing.assert_allclose(result, 0.5, atol=1e-6)

    def test_sine_wave_integrity(self):
        t = np.linspace(0, 1, 16000, endpoint=False)
        data = np.sin(2 * np.pi * 100 * t).astype(np.float32)
        upsampled = capture_mod._resample_linear(data, 16000, 48000)
        assert len(upsampled) == 48000
        assert abs(upsampled[0]) < 0.1


class TestAudioMixing:
    """Mixing behavior: average with clip safety."""

    def test_average_preserves_level(self):
        a = np.full(100, 0.6, dtype=np.float32)
        b = np.full(100, 0.4, dtype=np.float32)
        mixed = np.clip((a + b) * 0.5, -1.0, 1.0)
        np.testing.assert_allclose(mixed, 0.5, atol=1e-6)

    def test_single_source_not_attenuated_excessively(self):
        a = np.full(100, 0.8, dtype=np.float32)
        b = np.zeros(100, dtype=np.float32)
        mixed = np.clip((a + b) * 0.5, -1.0, 1.0)
        np.testing.assert_allclose(mixed, 0.4, atol=1e-6)

    def test_clipping_at_boundary(self):
        a = np.full(100, 1.5, dtype=np.float32)
        b = np.full(100, 1.5, dtype=np.float32)
        mixed = np.clip((a + b) * 0.5, -1.0, 1.0)
        np.testing.assert_allclose(mixed, 1.0, atol=1e-6)

    def test_negative_clipping(self):
        a = np.full(100, -1.5, dtype=np.float32)
        b = np.full(100, -1.5, dtype=np.float32)
        mixed = np.clip((a + b) * 0.5, -1.0, 1.0)
        np.testing.assert_allclose(mixed, -1.0, atol=1e-6)


class TestBoundedQueues:
    def test_queue_drops_when_full(self):
        q = queue.Queue(maxsize=3)
        for i in range(3):
            q.put_nowait(i)
        with pytest.raises(queue.Full):
            q.put_nowait(99)
        assert q.qsize() == 3

    def test_drain_queue_pattern(self):
        q = queue.Queue()
        for i in range(5):
            q.put(i)
        items = []
        while True:
            try:
                items.append(q.get_nowait())
            except queue.Empty:
                break
        assert items == [0, 1, 2, 3, 4]


class TestShutdownEvent:
    def test_event_stops_loop(self):
        event = threading.Event()
        iterations = 0

        def loop():
            nonlocal iterations
            while not event.is_set():
                iterations += 1
                event.wait(timeout=0.01)

        t = threading.Thread(target=loop)
        t.start()
        event.wait(timeout=0.05)
        event.set()
        t.join(timeout=1)
        assert not t.is_alive()
        assert iterations > 0


class TestWriteTranscript:
    def test_write_and_read(self, tmp_path):
        f = tmp_path / "test.txt"
        line = "[14:30:00-14:30:30] Test content\n"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
        assert f.read_text() == line

    def test_append_mode(self, tmp_path):
        f = tmp_path / "test.txt"
        f.write_text("line 1\n")
        with open(f, "a", encoding="utf-8") as fh:
            fh.write("line 2\n")
        assert f.read_text() == "line 1\nline 2\n"


class TestSpeakerDiarization:
    """Test the ACTUAL _classify_speakers and _build_diarized_text from capture.py."""

    def test_remote_speaker_only(self):
        bh = np.full(16000, 0.3, dtype=np.float32)
        mic = np.full(16000, 0.01, dtype=np.float32)
        segments = capture_mod._classify_speakers(bh, mic)
        assert len(segments) > 0
        assert all(s[0] == "[Remote]" for s in segments)

    def test_local_speaker_only(self):
        bh = np.full(16000, 0.01, dtype=np.float32)
        mic = np.full(16000, 0.5, dtype=np.float32)
        segments = capture_mod._classify_speakers(bh, mic)
        assert len(segments) > 0
        assert all(s[0] == "[You]" for s in segments)

    def test_silence_skipped(self):
        bh = np.zeros(16000, dtype=np.float32)
        mic = np.zeros(16000, dtype=np.float32)
        segments = capture_mod._classify_speakers(bh, mic)
        assert len(segments) == 0

    def test_both_speaking_labels_remote(self):
        """When both channels have similar energy (mic bleed), should label [Remote]."""
        bh = np.full(16000, 0.3, dtype=np.float32)
        mic = np.full(16000, 0.3, dtype=np.float32)
        segments = capture_mod._classify_speakers(bh, mic)
        assert len(segments) > 0
        assert all(s[0] == "[Remote]" for s in segments)

    def test_build_diarized_text_single_speaker(self):
        segments = [("[You]", 0, 16000)]
        result = capture_mod._build_diarized_text("Hello world", segments, 16000)
        assert result == "[You] Hello world"

    def test_build_diarized_text_mixed(self):
        # 50/50 split — should show [You + Remote]
        segments = [("[You]", 0, 8000), ("[Remote]", 8000, 16000)]
        result = capture_mod._build_diarized_text("Hello world", segments, 16000)
        assert "[You + Remote]" in result

    def test_build_diarized_text_empty_segments(self):
        result = capture_mod._build_diarized_text("Hello world", [], 16000)
        assert result == "Hello world"
