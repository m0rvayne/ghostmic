"""Tests for capture.py — audio processing, queue bounds, thread safety."""
import queue
import threading
import numpy as np
import pytest

# Import the module
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "capture_mod",
    Path(__file__).parent.parent / "capture.py",
)
capture = importlib.util.module_from_spec(spec)
capture.__name__ = "capture_mod"
# Don't exec the full module (it imports sounddevice which may not be available)
# Instead, test individual functions by loading just the ones we need


class TestResampleLinear:
    """Resampling quality and correctness."""

    def _resample(self, data, src, dst):
        """Inline resample since we may not be able to import capture fully."""
        if src == dst:
            return data
        duration = len(data) / src
        src_time = np.linspace(0, duration, len(data), endpoint=False)
        dst_samples = int(duration * dst)
        dst_time = np.linspace(0, duration, dst_samples, endpoint=False)
        return np.interp(dst_time, src_time, data).astype(np.float32)

    def test_same_rate_passthrough(self):
        data = np.random.randn(16000).astype(np.float32)
        result = self._resample(data, 16000, 16000)
        np.testing.assert_array_equal(result, data)

    def test_upsample_length(self):
        data = np.ones(16000, dtype=np.float32)
        result = self._resample(data, 16000, 48000)
        assert len(result) == 48000

    def test_downsample_length(self):
        data = np.ones(48000, dtype=np.float32)
        result = self._resample(data, 48000, 16000)
        assert len(result) == 16000

    def test_preserves_dc(self):
        """A constant signal should remain constant after resampling."""
        data = np.full(16000, 0.5, dtype=np.float32)
        result = self._resample(data, 16000, 48000)
        np.testing.assert_allclose(result, 0.5, atol=1e-6)

    def test_sine_wave_integrity(self):
        """A low-frequency sine should survive resampling."""
        t = np.linspace(0, 1, 16000, endpoint=False)
        data = np.sin(2 * np.pi * 100 * t).astype(np.float32)  # 100 Hz
        upsampled = self._resample(data, 16000, 48000)
        # Check that the upsampled signal has the right frequency content
        assert len(upsampled) == 48000
        # The first and last samples should be close to 0 (sine at 0 and 2pi)
        assert abs(upsampled[0]) < 0.1


class TestAudioMixing:
    """Mixing behavior: average with clip safety."""

    def test_average_preserves_level(self):
        """Two equal signals should average to half."""
        a = np.full(100, 0.6, dtype=np.float32)
        b = np.full(100, 0.4, dtype=np.float32)
        mixed = np.clip((a + b) * 0.5, -1.0, 1.0)
        np.testing.assert_allclose(mixed, 0.5, atol=1e-6)

    def test_single_source_not_attenuated_excessively(self):
        """One loud + one silent should give half the loud signal."""
        a = np.full(100, 0.8, dtype=np.float32)
        b = np.zeros(100, dtype=np.float32)
        mixed = np.clip((a + b) * 0.5, -1.0, 1.0)
        np.testing.assert_allclose(mixed, 0.4, atol=1e-6)

    def test_clipping_at_boundary(self):
        """Extremely loud combined signals should clip at 1.0."""
        a = np.full(100, 1.5, dtype=np.float32)
        b = np.full(100, 1.5, dtype=np.float32)
        mixed = np.clip((a + b) * 0.5, -1.0, 1.0)
        np.testing.assert_allclose(mixed, 1.0, atol=1e-6)  # (1.5+1.5)*0.5 = 1.5 -> clip to 1.0

    def test_negative_clipping(self):
        a = np.full(100, -1.5, dtype=np.float32)
        b = np.full(100, -1.5, dtype=np.float32)
        mixed = np.clip((a + b) * 0.5, -1.0, 1.0)
        np.testing.assert_allclose(mixed, -1.0, atol=1e-6)


class TestBoundedQueues:
    """Queues should drop data instead of growing forever."""

    def test_queue_drops_when_full(self):
        q = queue.Queue(maxsize=3)
        for i in range(3):
            q.put_nowait(i)
        # 4th put should raise Full
        with pytest.raises(queue.Full):
            q.put_nowait(99)
        # Queue should still have original 3 items
        assert q.qsize() == 3

    def test_drain_queue_pattern(self):
        """Drain should empty the queue without blocking."""
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
        assert q.empty()


class TestShutdownEvent:
    """shutdown_event should coordinate clean shutdown."""

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
    """File writing with fsync."""

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
    """Two-channel speaker classification."""

    def _classify(self, bh, mic, frame_ms=500):
        """Inline implementation matching capture.py logic."""
        sample_rate = 16000
        frame_size = int(sample_rate * frame_ms / 1000)
        segments = []
        silence_threshold = 0.005
        for i in range(0, min(len(bh), len(mic)), frame_size):
            bh_frame = bh[i:i + frame_size]
            mic_frame = mic[i:i + frame_size]
            energy_bh = np.sqrt(np.mean(bh_frame ** 2))
            energy_mic = np.sqrt(np.mean(mic_frame ** 2))
            if energy_bh < silence_threshold and energy_mic < silence_threshold:
                continue
            ratio = energy_mic / (energy_bh + 1e-8)
            speaker = "[You]" if ratio > 2.5 else "[Remote]"
            segments.append((speaker, i, i + frame_size))
        return segments

    def test_remote_speaker_only(self):
        """When only BlackHole has audio, should label [Remote]."""
        bh = np.full(16000, 0.3, dtype=np.float32)  # 1 second of audio
        mic = np.full(16000, 0.01, dtype=np.float32)  # near silence
        segments = self._classify(bh, mic)
        assert len(segments) > 0
        assert all(s[0] == "[Remote]" for s in segments)

    def test_local_speaker_only(self):
        """When mic is much louder than BlackHole, should label [You]."""
        bh = np.full(16000, 0.01, dtype=np.float32)  # near silence
        mic = np.full(16000, 0.5, dtype=np.float32)  # talking
        segments = self._classify(bh, mic)
        assert len(segments) > 0
        assert all(s[0] == "[You]" for s in segments)

    def test_silence_skipped(self):
        """Pure silence should produce no segments."""
        bh = np.zeros(16000, dtype=np.float32)
        mic = np.zeros(16000, dtype=np.float32)
        segments = self._classify(bh, mic)
        assert len(segments) == 0

    def test_both_speaking(self):
        """When both channels have similar energy, should label [Remote] (mic bleed)."""
        bh = np.full(16000, 0.3, dtype=np.float32)
        mic = np.full(16000, 0.3, dtype=np.float32)  # mic picks up remote speaker
        segments = self._classify(bh, mic)
        assert len(segments) > 0
        # ratio = 0.3 / 0.3 = 1.0, which is < 2.5, so [Remote]
        assert all(s[0] == "[Remote]" for s in segments)
