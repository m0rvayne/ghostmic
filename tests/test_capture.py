"""Tests for capture.py — audio processing, queue bounds, thread safety."""
import io
import queue
import subprocess
import sys
import threading
import types
from unittest.mock import MagicMock, patch, PropertyMock
import numpy as np
import pytest
import pathlib
from pathlib import Path

# Mock sounddevice before importing capture
# (it requires hardware/native libraries not available in test env)
sys.modules["sounddevice"] = MagicMock()

# Now we can import the actual capture module
sys.path.insert(0, str(Path(__file__).parent.parent))
import capture as capture_mod


SAMPLE_RATE = capture_mod.SAMPLE_RATE


def _speechlike(target_rms: float, seconds: float = 20.0, seed: int = 0,
                offset: int = 0, gaps: bool = True) -> np.ndarray:
    """Noise shaped like speech: loud stretches separated by pauses.

    A real chunk is never a constant level — it alternates between phrases and
    gaps, and it is the gaps every floor estimate in capture.py relies on.
    `offset` shifts the envelope so two channels can take turns; gaps=False
    models someone who never stops to breathe.
    """
    rng = np.random.default_rng(seed)
    n = int(SAMPLE_RATE * seconds)
    audio = rng.standard_normal(n).astype(np.float32)

    if gaps:
        frame = int(SAMPLE_RATE * 0.5)
        envelope = np.ones(n, dtype=np.float32)
        for i, start in enumerate(range(0, n, frame)):
            envelope[start:start + frame] = 1.0 if ((i + offset) % 5) < 3 else 0.04
        audio *= envelope

    audio *= target_rms / float(np.sqrt(np.mean(audio ** 2)))
    return audio


def _room_tone(rms: float, seconds: float = 20.0, seed: int = 1) -> np.ndarray:
    """Flat low-level noise — an empty room with the mic open."""
    rng = np.random.default_rng(seed)
    audio = rng.standard_normal(int(SAMPLE_RATE * seconds)).astype(np.float32)
    return audio * (rms / float(np.sqrt(np.mean(audio ** 2))))


def _bleed(source: np.ndarray, amount: float, floor_rms: float = 0.0005,
           seed: int = 5) -> np.ndarray:
    """What the mic picks up when the far end is played through speakers."""
    seconds = len(source) / SAMPLE_RATE
    return _room_tone(floor_rms, seconds, seed=seed) + source * amount


def _labels(segments) -> set:
    return {s[0] for s in segments}



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
    """_classify_speakers / _build_diarized_text on realistic two-channel audio.

    These used to be written against constant-DC signals, which let a channel
    read as "loud" with no dynamics at all. Real audio always has an envelope,
    and the floor-relative measure needs one, so every scenario here is built
    from shaped noise instead.
    """

    def test_remote_speaker_only(self):
        bh = _speechlike(0.02)
        mic = _room_tone(0.0005)
        assert _labels(capture_mod._classify_speakers(bh, mic)) == {"[Remote]"}

    def test_local_speaker_only(self):
        bh = _room_tone(0.0005)
        mic = _speechlike(0.03)
        assert _labels(capture_mod._classify_speakers(bh, mic)) == {"[You]"}

    def test_silence_skipped(self):
        bh = np.zeros(SAMPLE_RATE * 4, dtype=np.float32)
        mic = np.zeros(SAMPLE_RATE * 4, dtype=np.float32)
        assert capture_mod._classify_speakers(bh, mic) == []

    def test_room_tone_alone_is_not_speech(self):
        assert capture_mod._classify_speakers(_room_tone(0.0005),
                                              _room_tone(0.0006, seed=2)) == []

    def test_speaker_bleed_still_reads_as_remote(self):
        """Far end on speakers leaks into the mic — the tap must still win."""
        bh = _speechlike(0.02)
        mic = _bleed(bh, amount=0.3)
        assert _labels(capture_mod._classify_speakers(bh, mic)) == {"[Remote]"}

    def test_simultaneous_speech_marked_both(self):
        bh = _speechlike(0.02, seed=2)
        mic = _speechlike(0.03, seed=3)
        assert _labels(capture_mod._classify_speakers(bh, mic)) == {"[Both]"}

    def test_taking_turns_yields_both_labels(self):
        bh = _speechlike(0.02, seed=2, offset=0)
        mic = _speechlike(0.03, seed=3, offset=3)
        assert _labels(capture_mod._classify_speakers(bh, mic)) >= {"[You]", "[Remote]"}

    def test_one_channel_all_zeros_other_has_signal(self):
        bh = np.zeros(SAMPLE_RATE * 20, dtype=np.float32)
        mic = _speechlike(0.03)
        assert _labels(capture_mod._classify_speakers(bh, mic)) == {"[You]"}

    def test_muted_mic_does_not_explode(self):
        """capture.py feeds digital zeros while Zoom is muted."""
        bh = _speechlike(0.02)
        mic = np.zeros(len(bh), dtype=np.float32)
        assert _labels(capture_mod._classify_speakers(bh, mic)) == {"[Remote]"}

    def test_nan_frames_are_dropped_not_labelled(self):
        bh = np.full(SAMPLE_RATE * 4, np.nan, dtype=np.float32)
        mic = np.full(SAMPLE_RATE * 4, np.nan, dtype=np.float32)
        assert capture_mod._classify_speakers(bh, mic) == []

    def test_very_short_audio_does_not_crash(self):
        bh = np.full(100, 0.3, dtype=np.float32)
        mic = np.full(100, 0.3, dtype=np.float32)
        assert isinstance(capture_mod._classify_speakers(bh, mic), list)

    def test_zero_frame_size_returns_empty(self):
        assert capture_mod._classify_speakers(_speechlike(0.02),
                                              _room_tone(0.0005), frame_ms=0) == []

    # -- the point of the rewrite ------------------------------------------

    def test_labels_survive_a_mic_gain_change(self):
        """Same room, mic gain raised 10x. Absolute-ratio comparison flipped
        this case to [You]; lift over each channel's own floor must not."""
        bh = _speechlike(0.02)
        mic = _bleed(bh, amount=0.3)
        quiet = capture_mod._classify_speakers(bh, mic)
        loud = capture_mod._classify_speakers(bh, mic * 10.0)
        assert _labels(quiet) == _labels(loud) == {"[Remote]"}

    def test_labels_survive_a_volume_change(self):
        """Listener turns Zoom down. Bleed scales with it; labels must not move."""
        bh = _speechlike(0.02)
        mic = _bleed(bh, amount=0.3)
        before = capture_mod._classify_speakers(bh, mic)
        after = capture_mod._classify_speakers(bh * 0.25, _bleed(bh * 0.25, amount=0.3))
        assert _labels(before) == _labels(after) == {"[Remote]"}

    def test_rolling_floor_rescues_a_speaker_who_never_pauses(self):
        """With no gaps in the chunk there is no floor to find inside it, so
        chunk-local estimation gives up. The rolling floor from earlier audio
        is what keeps the label."""
        bh = _speechlike(0.02, gaps=False)
        mic = _room_tone(0.0005)
        assert capture_mod._classify_speakers(bh, mic) == []
        rescued = capture_mod._classify_speakers(bh, mic, bh_floor=0.0005,
                                                 mic_floor=0.0005)
        assert _labels(rescued) == {"[Remote]"}

    # -- label aggregation --------------------------------------------------

    def test_build_diarized_text_single_speaker(self):
        segments = [("[You]", 0, 16000)]
        assert capture_mod._build_diarized_text("Hello world", segments, 16000) == "[You] Hello world"

    def test_build_diarized_text_mixed(self):
        segments = [("[You]", 0, 8000), ("[Remote]", 8000, 16000)]
        assert "[You + Remote]" in capture_mod._build_diarized_text("Hello world", segments, 16000)

    def test_build_diarized_text_both_counts_for_each_side(self):
        """A chunk that is entirely overlap belongs to neither alone."""
        segments = [("[Both]", 0, 16000)]
        assert "[You + Remote]" in capture_mod._build_diarized_text("Hi", segments, 16000)

    def test_build_diarized_text_empty_segments(self):
        assert capture_mod._build_diarized_text("Hello world", [], 16000) == "Hello world"


class TestBleedCorrelation:
    """Telling speaker bleed apart from two people talking at once.

    By level these are the same picture: both channels lit, neither leading.
    They differ in shape — bleed is the tap coming back through the room, so
    it correlates with it, while independent voices do not.
    """

    def test_identical_frames_correlate(self):
        a = _room_tone(0.01, seconds=0.5)
        assert capture_mod._frame_correlation(a, a) == pytest.approx(1.0, abs=1e-6)

    def test_scaled_copy_still_correlates(self):
        a = _room_tone(0.01, seconds=0.5)
        assert capture_mod._frame_correlation(a, a * 0.05) == pytest.approx(1.0, abs=1e-6)

    def test_independent_signals_do_not_correlate(self):
        a = _room_tone(0.01, seconds=0.5, seed=1)
        b = _room_tone(0.01, seconds=0.5, seed=2)
        assert capture_mod._frame_correlation(a, b) < 0.1

    def test_flat_frame_gives_zero(self):
        a = _room_tone(0.01, seconds=0.5)
        flat = np.zeros(len(a), dtype=np.float32)
        assert capture_mod._frame_correlation(a, flat) == 0.0

    def test_mismatched_or_empty_gives_zero(self):
        a = _room_tone(0.01, seconds=0.5)
        assert capture_mod._frame_correlation(a, a[:100]) == 0.0
        assert capture_mod._frame_correlation(np.array([]), np.array([])) == 0.0

    def test_nan_frame_gives_zero(self):
        a = _room_tone(0.01, seconds=0.5)
        bad = np.full(len(a), np.nan, dtype=np.float32)
        assert capture_mod._frame_correlation(a, bad) == 0.0

    def test_moderate_bleed_reads_as_remote(self):
        """-20 dB into the mic — an ordinary laptop speaker at normal volume."""
        bh = _speechlike(0.02)
        assert _labels(capture_mod._classify_speakers(bh, _bleed(bh, 0.1))) == {"[Remote]"}

    def test_heavy_bleed_reads_as_remote(self):
        """-10 dB — speakers up loud, mic close. Level alone cannot resolve this."""
        bh = _speechlike(0.02)
        assert _labels(capture_mod._classify_speakers(bh, _bleed(bh, 0.3))) == {"[Remote]"}

    def test_equal_level_independent_voices_stay_both(self):
        """Same evidence by level as heavy bleed, opposite answer."""
        bh = _speechlike(0.02, seed=11)
        mic = _speechlike(0.02, seed=12)
        assert _labels(capture_mod._classify_speakers(bh, mic)) == {"[Both]"}


# -- Tests for _is_hallucination -----------------------------------------------

class TestIsHallucination:
    """Test all hallucination patterns and edge cases."""

    def test_empty_string(self):
        assert capture_mod._is_hallucination("") is True

    def test_single_char(self):
        assert capture_mod._is_hallucination("a") is True
        assert capture_mod._is_hallucination("X") is True

    def test_two_char_string(self):
        assert capture_mod._is_hallucination("ab") is True

    def test_russian_single_letters(self):
        assert capture_mod._is_hallucination("и") is True
        assert capture_mod._is_hallucination("а") is True

    def test_dots_only(self):
        assert capture_mod._is_hallucination("...") is True

    def test_dash(self):
        assert capture_mod._is_hallucination("-") is True
        assert capture_mod._is_hallucination("–") is True

    def test_dot(self):
        assert capture_mod._is_hallucination(".") is True

    def test_dots_and_spaces(self):
        assert capture_mod._is_hallucination(".. . ...") is True
        assert capture_mod._is_hallucination("… … …") is True

    def test_dots_dashes_spaces_mixed(self):
        assert capture_mod._is_hallucination(". - . -") is True
        assert capture_mod._is_hallucination("---") is True

    @pytest.mark.parametrize("pattern", capture_mod._HALLUCINATION_PATTERNS)
    def test_each_hallucination_pattern(self, pattern):
        assert capture_mod._is_hallucination(pattern) is True

    def test_hallucination_pattern_case_insensitive(self):
        assert capture_mod._is_hallucination("Продолжение Следует") is True
        assert capture_mod._is_hallucination("THANKS FOR WATCHING") is True
        assert capture_mod._is_hallucination("Like And Subscribe") is True

    def test_hallucination_pattern_embedded_in_text(self):
        assert capture_mod._is_hallucination("blah спасибо за просмотр blah") is True
        assert capture_mod._is_hallucination("text subscribe now") is True

    def test_hallucination_with_whitespace(self):
        assert capture_mod._is_hallucination("  продолжение следует  ") is True
        assert capture_mod._is_hallucination("  ...  ") is True

    def test_real_text_passes(self):
        assert capture_mod._is_hallucination("Привет, как дела?") is False
        assert capture_mod._is_hallucination("Hello, how are you?") is False
        assert capture_mod._is_hallucination("Let's discuss the project plan") is False
        assert capture_mod._is_hallucination("Давайте обсудим план проекта") is False

    def test_short_but_real_three_chars(self):
        # Exactly 3 chars — not filtered by length, must not match patterns
        assert capture_mod._is_hallucination("abc") is False
        assert capture_mod._is_hallucination("Нет") is False


# -- Tests for _postprocess_text -----------------------------------------------

class TestPostprocessText:
    """Test speaker continuity logic in _postprocess_text."""

    def setup_method(self):
        """Reset global state before each test."""
        capture_mod._prev_speaker = ""
        capture_mod._prev_chunks.clear()
        # Disable LLM post-processing for deterministic tests
        capture_mod._llm_model = None

    def test_first_call_with_remote_shows_label(self):
        result = capture_mod._postprocess_text("[Remote] Hello there", "10:00:00")
        assert result == "[Remote] Hello there"

    def test_same_speaker_continuation(self):
        capture_mod._postprocess_text("[Remote] Hello there", "10:00:00")
        result = capture_mod._postprocess_text("[Remote] How are you?", "10:00:10")
        assert result == "...How are you?"

    def test_speaker_change_shows_label(self):
        capture_mod._postprocess_text("[Remote] Hello there", "10:00:00")
        result = capture_mod._postprocess_text("[You] I'm fine", "10:00:10")
        assert result == "[You] I'm fine"

    def test_back_to_remote_shows_label_again(self):
        capture_mod._postprocess_text("[Remote] Hello", "10:00:00")
        capture_mod._postprocess_text("[You] Hi", "10:00:10")
        result = capture_mod._postprocess_text("[Remote] So anyway", "10:00:20")
        assert result == "[Remote] So anyway"

    def test_no_speaker_label_passthrough(self):
        result = capture_mod._postprocess_text("Just some text", "10:00:00")
        assert result == "Just some text"

    def test_no_label_does_not_reset_speaker(self):
        capture_mod._postprocess_text("[Remote] Hello", "10:00:00")
        capture_mod._postprocess_text("Just some text", "10:00:10")
        # Next [Remote] should still be continuation since _prev_speaker is still [Remote]
        result = capture_mod._postprocess_text("[Remote] More talk", "10:00:20")
        assert result == "...More talk"

    def test_you_plus_remote_label(self):
        result = capture_mod._postprocess_text("[You + Remote] Both talking", "10:00:00")
        assert result == "[You + Remote] Both talking"

    def test_you_plus_remote_continuation(self):
        capture_mod._postprocess_text("[You + Remote] Both talking", "10:00:00")
        result = capture_mod._postprocess_text("[You + Remote] Still both", "10:00:10")
        assert result == "...Still both"

    def test_prev_chunks_accumulate(self):
        for i in range(7):
            capture_mod._postprocess_text(f"[Remote] Chunk {i}", f"10:00:{i:02d}")
        # _prev_chunks should be capped at 5
        assert len(capture_mod._prev_chunks) == 5


# -- Tests for _read_exactly ---------------------------------------------------

class TestReadExactly:
    """Test reading exact byte counts from streams."""

    def test_exact_size_read(self):
        data = b"hello world"
        stream = io.BytesIO(data)
        result = capture_mod._read_exactly(stream, len(data))
        assert result == data

    def test_short_reads_reassembled(self):
        """Stream that returns data in small pieces."""
        class ChunkedStream:
            def __init__(self, data, chunk_size):
                self._data = data
                self._pos = 0
                self._chunk_size = chunk_size

            def read(self, n):
                actual = min(n, self._chunk_size, len(self._data) - self._pos)
                if actual <= 0:
                    return b""
                chunk = self._data[self._pos:self._pos + actual]
                self._pos += actual
                return chunk

        stream = ChunkedStream(b"abcdefghij", chunk_size=3)
        result = capture_mod._read_exactly(stream, 10)
        assert result == b"abcdefghij"

    def test_one_byte_chunks(self):
        """Stream returns data one byte at a time."""
        class OneByteStream:
            def __init__(self, data):
                self._data = data
                self._pos = 0

            def read(self, n):
                if self._pos >= len(self._data):
                    return b""
                byte = self._data[self._pos:self._pos + 1]
                self._pos += 1
                return byte

        stream = OneByteStream(b"ABCDE")
        result = capture_mod._read_exactly(stream, 5)
        assert result == b"ABCDE"

    def test_empty_stream(self):
        stream = io.BytesIO(b"")
        result = capture_mod._read_exactly(stream, 10)
        assert result == b""

    def test_stream_shorter_than_requested(self):
        """Stream has less data than requested — returns partial."""
        stream = io.BytesIO(b"abc")
        result = capture_mod._read_exactly(stream, 10)
        assert result == b"abc"

    def test_stream_with_raw_attribute(self):
        """Stream that has a .raw attribute (like BufferedReader wrapping RawIO)."""
        raw = io.BytesIO(b"hello")
        wrapper = MagicMock()
        wrapper.raw = raw
        result = capture_mod._read_exactly(wrapper, 5)
        assert result == b"hello"

    def test_zero_bytes_requested(self):
        stream = io.BytesIO(b"data")
        result = capture_mod._read_exactly(stream, 0)
        assert result == b""


# -- Tests for transcribe_chunk ------------------------------------------------

class TestTranscribeChunk:
    """Test transcribe_chunk with mocked subprocess calls."""

    def test_silence_below_min_rms_returns_empty(self):
        """Audio below the adaptive speech gate returns empty without calling whisper."""
        audio = np.full(16000, 0.001, dtype=np.float32)
        with patch("capture.subprocess.run") as mock_run:
            result = capture_mod.transcribe_chunk(None, audio)
        assert result == ""
        mock_run.assert_not_called()

    def test_whisper_cli_called_with_correct_args(self):
        """Verify whisper-cli is called with the expected arguments."""
        audio = np.full(16000, 0.5, dtype=np.float32)  # loud enough

        ffmpeg_result = MagicMock(returncode=0)
        whisper_result = MagicMock(
            returncode=0,
            stdout=b"Hello world\n",
        )

        with patch("capture.subprocess.run") as mock_run, \
             patch("os.unlink", return_value=None):
            mock_run.side_effect = [ffmpeg_result, whisper_result]
            result = capture_mod.transcribe_chunk(None, audio)

        assert mock_run.call_count == 2

        # Check ffmpeg call
        ffmpeg_call = mock_run.call_args_list[0]
        ffmpeg_args = ffmpeg_call[0][0]
        assert ffmpeg_args[0] == "ffmpeg"
        assert "-y" in ffmpeg_args
        assert "-f" in ffmpeg_args
        assert "s16le" in ffmpeg_args
        assert "-ar" in ffmpeg_args
        assert "16000" in ffmpeg_args
        assert "-ac" in ffmpeg_args
        assert "1" in ffmpeg_args
        assert "pipe:0" in ffmpeg_args

        # Check whisper-cli call
        whisper_call = mock_run.call_args_list[1]
        whisper_args = whisper_call[0][0]
        assert whisper_args[0] == capture_mod.WHISPER_CLI
        assert "-m" in whisper_args
        assert capture_mod.WHISPER_MODEL_PATH in whisper_args
        assert "-f" in whisper_args
        assert "--no-timestamps" in whisper_args
        assert "-t" in whisper_args
        assert "4" in whisper_args

    def test_whisper_cli_includes_language_flag(self):
        """When LANGUAGE is not 'auto', -l flag should be present."""
        audio = np.full(16000, 0.5, dtype=np.float32)
        ffmpeg_result = MagicMock(returncode=0)
        whisper_result = MagicMock(returncode=0, stdout=b"test output\n")

        with patch("capture.subprocess.run") as mock_run, \
             patch("os.unlink", return_value=None), \
             patch.object(capture_mod, "LANGUAGE", "ru"):
            mock_run.side_effect = [ffmpeg_result, whisper_result]
            capture_mod.transcribe_chunk(None, audio)

        whisper_args = mock_run.call_args_list[1][0][0]
        assert "-l" in whisper_args
        assert "ru" in whisper_args

    def test_whisper_cli_no_language_flag_when_auto(self):
        """When LANGUAGE is 'auto', -l flag should not be present."""
        audio = np.full(16000, 0.5, dtype=np.float32)
        ffmpeg_result = MagicMock(returncode=0)
        whisper_result = MagicMock(returncode=0, stdout=b"test output\n")

        with patch("capture.subprocess.run") as mock_run, \
             patch("os.unlink", return_value=None), \
             patch.object(capture_mod, "LANGUAGE", "auto"):
            mock_run.side_effect = [ffmpeg_result, whisper_result]
            capture_mod.transcribe_chunk(None, audio)

        whisper_args = mock_run.call_args_list[1][0][0]
        assert "-l" not in whisper_args

    def test_hallucination_filtered_out(self):
        """Whisper output that matches hallucination patterns returns empty string."""
        audio = np.full(16000, 0.5, dtype=np.float32)
        ffmpeg_result = MagicMock(returncode=0)
        whisper_result = MagicMock(
            returncode=0,
            stdout="Продолжение следует".encode("utf-8"),
        )

        with patch("capture.subprocess.run") as mock_run, \
             patch("os.unlink", return_value=None):
            mock_run.side_effect = [ffmpeg_result, whisper_result]
            result = capture_mod.transcribe_chunk(None, audio)

        assert result == ""

    def test_blank_audio_marker_stripped(self):
        """[BLANK_AUDIO] marker should be removed from output."""
        audio = np.full(16000, 0.5, dtype=np.float32)
        ffmpeg_result = MagicMock(returncode=0)
        whisper_result = MagicMock(
            returncode=0,
            stdout=b"[BLANK_AUDIO] Real speech here",
        )

        with patch("capture.subprocess.run") as mock_run, \
             patch("os.unlink", return_value=None):
            mock_run.side_effect = [ffmpeg_result, whisper_result]
            result = capture_mod.transcribe_chunk(None, audio)

        assert "[BLANK_AUDIO]" not in result
        assert "Real speech here" in result

    def test_subprocess_timeout_handled(self):
        """subprocess.TimeoutExpired should be caught, return empty string."""
        audio = np.full(16000, 0.5, dtype=np.float32)

        with patch("capture.subprocess.run") as mock_run, \
             patch("os.unlink", return_value=None):
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="whisper-cli", timeout=30)
            result = capture_mod.transcribe_chunk(None, audio)

        assert result == ""

    def test_subprocess_general_exception_handled(self):
        """Any exception during transcription should be caught, return empty string."""
        audio = np.full(16000, 0.5, dtype=np.float32)

        with patch("capture.subprocess.run") as mock_run, \
             patch("os.unlink", return_value=None):
            mock_run.side_effect = OSError("ffmpeg not found")
            result = capture_mod.transcribe_chunk(None, audio)

        assert result == ""

    def test_whisper_output_lines_starting_with_bracket_filtered(self):
        """Lines starting with [ (timestamps) should be filtered out."""
        audio = np.full(16000, 0.5, dtype=np.float32)
        ffmpeg_result = MagicMock(returncode=0)
        whisper_output = b"[00:00.000 --> 00:05.000] Hello\nActual text here\n"
        whisper_result = MagicMock(returncode=0, stdout=whisper_output)

        with patch("capture.subprocess.run") as mock_run, \
             patch("os.unlink", return_value=None):
            mock_run.side_effect = [ffmpeg_result, whisper_result]
            result = capture_mod.transcribe_chunk(None, audio)

        assert result == "Actual text here"

    def test_ffmpeg_receives_pcm_input(self):
        """Verify ffmpeg gets PCM data as stdin input."""
        audio = np.array([0.5, -0.5, 0.25], dtype=np.float32)
        expected_pcm = (audio * 32768).astype(np.int16).tobytes()

        ffmpeg_result = MagicMock(returncode=0)
        whisper_result = MagicMock(returncode=0, stdout=b"Some text output")

        with patch("capture.subprocess.run") as mock_run, \
             patch("os.unlink", return_value=None):
            mock_run.side_effect = [ffmpeg_result, whisper_result]
            capture_mod.transcribe_chunk(None, audio)

        ffmpeg_call = mock_run.call_args_list[0]
        assert ffmpeg_call[1]["input"] == expected_pcm


# -- Tests for _check_zoom_mute -----------------------------------------------

class TestCheckZoomMute:
    """Test Zoom mute detection via mocked osascript."""

    def test_russian_zoom_muted(self):
        """Russian Zoom UI: menu items contain 'Включить звук' => muted."""
        mock_result = MagicMock(stdout="Запись|Включить звук|Настройки\n", returncode=0)
        with patch("capture.subprocess.run", return_value=mock_result) as mock_run:
            result = capture_mod._check_zoom_mute()
        assert result is True
        call_args = mock_run.call_args
        assert call_args[0][0][0] == "osascript"
        assert call_args[1]["timeout"] == 3

    def test_english_zoom_muted(self):
        """English Zoom UI: menu items contain 'Unmute Audio' => muted."""
        mock_result = MagicMock(stdout="Record|Unmute Audio|Settings\n", returncode=0)
        with patch("capture.subprocess.run", return_value=mock_result):
            result = capture_mod._check_zoom_mute()
        assert result is True

    def test_zoom_unmuted(self):
        """When Zoom is unmuted, no unmute keywords in menu."""
        mock_result = MagicMock(stdout="Record|Mute Audio|Settings\n", returncode=0)
        with patch("capture.subprocess.run", return_value=mock_result):
            result = capture_mod._check_zoom_mute()
        assert result is False

    def test_not_in_meeting(self):
        """Empty menu output => not muted."""
        mock_result = MagicMock(stdout="\n", returncode=0)
        with patch("capture.subprocess.run", return_value=mock_result):
            result = capture_mod._check_zoom_mute()
        assert result is False

    def test_osascript_timeout(self):
        """osascript timeout should not crash, returns False."""
        with patch("capture.subprocess.run",
                   side_effect=subprocess.TimeoutExpired(cmd="osascript", timeout=3)):
            result = capture_mod._check_zoom_mute()
        assert result is False

    def test_osascript_file_not_found(self):
        """osascript not found should not crash, returns False."""
        with patch("capture.subprocess.run", side_effect=FileNotFoundError("osascript")):
            result = capture_mod._check_zoom_mute()
        assert result is False

    def test_osascript_generic_error(self):
        """Any other exception should not crash, returns False."""
        with patch("capture.subprocess.run", side_effect=OSError("some error")):
            result = capture_mod._check_zoom_mute()
        assert result is False

    def test_empty_stdout(self):
        """Empty osascript output => not 'muted' => False."""
        mock_result = MagicMock(stdout="", returncode=0)
        with patch("capture.subprocess.run", return_value=mock_result):
            result = capture_mod._check_zoom_mute()
        assert result is False


class TestParticipantDetection:
    """Test participant name detection and integration."""

    def setup_method(self):
        """Reset participant state before each test."""
        with capture_mod._participants_lock:
            capture_mod._participants.clear()

    def test_get_participants_empty(self):
        assert capture_mod.get_participants() == []

    def test_get_participants_returns_copy(self):
        with capture_mod._participants_lock:
            capture_mod._participants.extend(["Alice", "Bob"])
        result = capture_mod.get_participants()
        assert result == ["Alice", "Bob"]
        # Verify it's a copy
        result.append("Charlie")
        assert capture_mod.get_participants() == ["Alice", "Bob"]

    def test_get_remote_participants(self):
        with capture_mod._participants_lock:
            capture_mod._participants.extend(["Alice", "Bob"])
        result = capture_mod.get_remote_participants()
        assert "Alice" in result
        assert "Bob" in result

    def test_remote_label_single_participant(self):
        with capture_mod._participants_lock:
            capture_mod._participants.extend(["Sergey"])
        label = capture_mod._remote_label()
        assert label == "[Sergey]"

    def test_remote_label_multiple_participants(self):
        with capture_mod._participants_lock:
            capture_mod._participants.extend(["Alice", "Bob"])
        label = capture_mod._remote_label()
        assert label == "[Remote]"

    def test_remote_label_no_participants(self):
        label = capture_mod._remote_label()
        assert label == "[Remote]"

    def test_build_diarized_text_with_named_remote(self):
        with capture_mod._participants_lock:
            capture_mod._participants.extend(["Sergey"])
        segments = [("[Remote]", 0, 16000)]
        result = capture_mod._build_diarized_text("Hello", segments, 16000)
        assert result == "[Sergey] Hello"

    def test_build_diarized_text_mixed_with_named_remote(self):
        with capture_mod._participants_lock:
            capture_mod._participants.extend(["Sergey"])
        segments = [("[You]", 0, 8000), ("[Remote]", 8000, 16000)]
        result = capture_mod._build_diarized_text("Hello", segments, 16000)
        assert "Sergey" in result
        assert "You" in result

    def test_build_diarized_text_you_dominant_keeps_you(self):
        with capture_mod._participants_lock:
            capture_mod._participants.extend(["Sergey"])
        # 80% You
        segments = [("[You]", 0, 12800), ("[Remote]", 12800, 16000)]
        result = capture_mod._build_diarized_text("Hello", segments, 16000)
        assert result == "[You] Hello"

    def test_postprocess_handles_named_speaker_label(self):
        """Speaker labels like [Sergey] should be handled like [Remote]."""
        capture_mod._prev_speaker = ""
        result = capture_mod._postprocess_text("[Sergey] Hello world", "[12:00:00-12:00:10]")
        assert "[Sergey]" in result
        assert "Hello world" in result

    def test_postprocess_continuation_with_named_speaker(self):
        """Same named speaker on consecutive chunks shows continuation."""
        capture_mod._prev_speaker = ""
        capture_mod._prev_chunks.clear()
        capture_mod._postprocess_text("[Sergey] First part", "[12:00:00-12:00:10]")
        result = capture_mod._postprocess_text("[Sergey] Second part", "[12:00:10-12:00:20]")
        assert result.startswith("...")

    def test_postprocess_speaker_change_named_to_you(self):
        """Change from named speaker to [You] shows new label."""
        capture_mod._prev_speaker = ""
        capture_mod._prev_chunks.clear()
        capture_mod._postprocess_text("[Sergey] His text", "[12:00:00-12:00:10]")
        result = capture_mod._postprocess_text("[You] My text", "[12:00:10-12:00:20]")
        assert result.startswith("[You]")

    def _run_one_poll(self, mock_result):
        """Run _poll_participants for exactly one iteration."""
        call_count = [0]
        original_wait = capture_mod.shutdown_event.wait

        def one_shot_wait(timeout=None):
            call_count[0] += 1
            if call_count[0] >= 1:
                capture_mod.shutdown_event.set()
            return original_wait(timeout=0)

        with patch("capture.subprocess.run", return_value=mock_result), \
             patch.object(capture_mod.shutdown_event, "wait", side_effect=one_shot_wait):
            capture_mod.shutdown_event.clear()
            capture_mod._poll_participants()
            capture_mod.shutdown_event.clear()

    def test_poll_participants_subprocess_failure(self):
        """Poll should handle subprocess failures gracefully."""
        with capture_mod._participants_lock:
            capture_mod._participants.extend(["Alice"])
        mock_result = MagicMock(returncode=1, stdout=b"")
        self._run_one_poll(mock_result)
        assert capture_mod.get_participants() == ["Alice"]

    def test_poll_participants_updates_list(self):
        """Successful poll updates participant list."""
        mock_result = MagicMock(returncode=0, stdout=b'["Bob", "Charlie"]')
        self._run_one_poll(mock_result)
        assert capture_mod.get_participants() == ["Bob", "Charlie"]

    def test_poll_participants_invalid_json(self):
        """Invalid JSON should not crash or clear participants."""
        with capture_mod._participants_lock:
            capture_mod._participants.extend(["Alice"])
        mock_result = MagicMock(returncode=0, stdout=b'not json')
        self._run_one_poll(mock_result)
        assert capture_mod.get_participants() == ["Alice"]


class TestHallucinationRepetition:
    """Test repetition-based hallucination detection."""

    def test_repeated_word_is_hallucination(self):
        assert capture_mod._is_hallucination("да да да") is True

    def test_repeated_word_four_times(self):
        assert capture_mod._is_hallucination("okay okay okay okay") is True

    def test_different_words_not_hallucination(self):
        assert capture_mod._is_hallucination("hello world today") is False

    def test_two_same_words_not_hallucination(self):
        assert capture_mod._is_hallucination("да да") is False


class TestNoiseFloor:
    """Rolling noise floor estimation."""

    def test_empty_floor_reports_zero(self):
        assert capture_mod.NoiseFloor().level == 0.0

    def test_floor_finds_pauses_not_speech(self):
        # Speech at 0.02 RMS with pauses at 4% of that — the floor must land
        # near the pauses, well under the overall level.
        nf = capture_mod.NoiseFloor()
        nf.observe(_speechlike(0.02))
        assert nf.level < 0.02 / 4

    def test_floor_tracks_room_tone(self):
        nf = capture_mod.NoiseFloor()
        nf.observe(_room_tone(0.0005))
        assert 0.0003 < nf.level < 0.0008

    def test_window_is_bounded(self):
        nf = capture_mod.NoiseFloor(window=10)
        nf.observe(_room_tone(0.0005, seconds=60))
        assert len(nf._energies) == 10

    def test_short_audio_counted_whole(self):
        nf = capture_mod.NoiseFloor()
        nf.observe(np.full(100, 0.01, dtype=np.float32))  # shorter than a frame
        assert nf.level == pytest.approx(0.01, rel=1e-3)

    def test_empty_audio_ignored(self):
        nf = capture_mod.NoiseFloor()
        nf.observe(np.array([], dtype=np.float32))
        nf.observe(None)
        assert nf.level == 0.0

    def test_reset_clears(self):
        nf = capture_mod.NoiseFloor()
        nf.observe(_room_tone(0.001))
        nf.reset()
        assert nf.level == 0.0


class TestSpeechGate:
    """The gate must follow the signal, not a hardcoded guess."""

    def setup_method(self):
        capture_mod._mix_noise.reset()

    def teardown_method(self):
        capture_mod._mix_noise.reset()

    def test_unseen_signal_gates_permissively(self):
        assert capture_mod.speech_gate() == capture_mod.GATE_MIN

    def test_gate_follows_floor(self):
        capture_mod._mix_noise.observe(_room_tone(0.001))
        assert capture_mod.speech_gate() == pytest.approx(0.002, rel=0.3)

    def test_gate_never_exceeds_max(self):
        capture_mod._mix_noise.observe(_room_tone(0.5))  # very loud room
        assert capture_mod.speech_gate() == capture_mod.GATE_MAX

    def test_gate_never_drops_below_min(self):
        capture_mod._mix_noise.observe(_room_tone(1e-6))  # digital silence
        assert capture_mod.speech_gate() == capture_mod.GATE_MIN


class TestGateRegression:
    """Replays the levels that were logged on 2026-09-07.

    Every value here is a real chunk from watcher.log. Under the old fixed
    0.01 threshold the first three were discarded as silence; two slots later
    a 0.0122 chunk from the same conversation transcribed normally.
    """

    # RMS values that carried speech and must reach Whisper
    DROPPED_SPEECH = [0.0069, 0.0060, 0.0121, 0.0122]
    # RMS values that really were silence and should still be skipped
    TRUE_SILENCE = [0.0005, 0.0004]

    def setup_method(self):
        capture_mod._mix_noise.reset()

    def teardown_method(self):
        capture_mod._mix_noise.reset()

    def _transcribes(self, audio) -> bool:
        """True if the chunk reached whisper-cli."""
        with patch("capture.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=b"text\n")
            capture_mod.transcribe_chunk(None, audio)
            return mock_run.called

    @pytest.mark.parametrize("rms", DROPPED_SPEECH)
    def test_speech_level_chunks_reach_whisper(self, rms):
        # Establish a realistic floor first, as a live meeting would
        capture_mod._mix_noise.observe(_room_tone(0.0005))
        assert self._transcribes(_speechlike(rms)), (
            f"RMS={rms} was gated out; gate={capture_mod.speech_gate():.4f}")

    @pytest.mark.parametrize("rms", TRUE_SILENCE)
    def test_silent_chunks_still_skipped(self, rms):
        capture_mod._mix_noise.observe(_room_tone(0.0005))
        assert not self._transcribes(_room_tone(rms, seed=7))

    def test_quiet_speaker_on_a_quiet_line(self):
        # Someone soft-spoken on a line with almost no noise: the old constant
        # would have discarded the whole conversation.
        capture_mod._mix_noise.observe(_room_tone(0.0002))
        assert self._transcribes(_speechlike(0.003))


# -- Tests for the LLM post-processing guard -----------------------------------

class TestStripThinkBlock:
    """Qwen3 opens a reasoning block regardless of the /no_think hint."""

    def test_removes_block_and_contents(self):
        out = capture_mod._strip_think_block("<think>\n\n</think>\n\nРеальный текст")
        assert out == "Реальный текст"

    def test_removes_multiline_reasoning(self):
        out = capture_mod._strip_think_block("<think>a\nb\nc</think> текст")
        assert out == "текст"

    def test_removes_unclosed_tag(self):
        assert "think" not in capture_mod._strip_think_block("<think> текст")

    def test_plain_text_untouched(self):
        assert capture_mod._strip_think_block("просто текст") == "просто текст"


class TestLevenshtein:
    def test_identical(self):
        assert capture_mod._levenshtein("абв", "абв") == 0

    def test_empty_sides(self):
        assert capture_mod._levenshtein("", "абв") == 3
        assert capture_mod._levenshtein("абв", "") == 3

    def test_single_substitution(self):
        assert capture_mod._levenshtein("кот", "кит") == 1

    def test_insertion(self):
        assert capture_mod._levenshtein("кот", "коты") == 1


class TestLLMOutputGuard:
    """What may replace the transcribed text, and what may not."""

    ORIGINAL = ("Давайте зафиксируем: бюджет 250 тысяч, срок до пятницы, "
                "ответственный Андрей.")

    def test_accepts_a_light_repair(self):
        candidate = ("Давайте зафиксируем: бюджет 250 тысяч, срок до пятницы, "
                     "ответственный Андрей.")
        assert capture_mod._llm_output_is_safe(self.ORIGINAL, candidate)[0]

    def test_accepts_small_grammar_fix(self):
        original = "просто будут элементы"
        assert capture_mod._llm_output_is_safe(original, "просто были элементы")[0]

    def test_rejects_dropped_number(self):
        """Same length, so it is the figure rule that has to catch this."""
        candidate = ("Давайте зафиксируем: бюджет сто тысяч, срок до пятницы, "
                     "ответственный Андрей.")
        ok, reason = capture_mod._llm_output_is_safe(self.ORIGINAL, candidate)
        assert not ok and reason == "dropped-number"

    def test_rejects_changed_number(self):
        candidate = self.ORIGINAL.replace("250", "150")
        ok, reason = capture_mod._llm_output_is_safe(self.ORIGINAL, candidate)
        assert not ok and reason == "dropped-number"

    def test_rejects_truncation(self):
        """The most common real failure: the tail sentence disappears."""
        ok, reason = capture_mod._llm_output_is_safe(
            self.ORIGINAL, "Давайте зафиксируем: бюджет 250 тысяч.")
        assert not ok and reason == "length"

    def test_rejects_continuation_of_the_conversation(self):
        """Observed 14 times in 40 — output is fresh dialogue, not a repair."""
        ok, reason = capture_mod._llm_output_is_safe(
            "Короткая реплика в конце чанка.",
            "Хорошо, тогда давайте вернёмся к этому на следующей неделе. "
            "Я подготовлю смету и пришлю её вечером. Сроки пока не двигаем.")
        assert not ok and reason == "length"

    def test_rejects_rewrite_of_similar_length(self):
        """Same length, entirely different sentence — a rewrite, not a repair."""
        ok, reason = capture_mod._llm_output_is_safe(
            "Проверь, пожалуйста, вложенные разделы отчёта за квартал.",
            "Завтра утром созвонимся и обсудим план на следующий спринт.")
        assert not ok and reason == "divergence"

    def test_rejects_empty_and_tiny(self):
        assert not capture_mod._llm_output_is_safe(self.ORIGINAL, "")[0]
        assert not capture_mod._llm_output_is_safe(self.ORIGINAL, "ок")[0]

    def test_rejects_hallucination(self):
        ok, reason = capture_mod._llm_output_is_safe(
            "мы обсудили бюджет на следующий квартал подробно",
            "Спасибо за просмотр, подписывайтесь на канал!")
        assert not ok and reason == "hallucination"

    def test_rejection_reasons_are_counted_once_per_kind(self):
        capture_mod._llm_rejections.clear()
        capture_mod._note_llm_rejection("length")
        capture_mod._note_llm_rejection("length")
        capture_mod._note_llm_rejection("divergence")
        assert capture_mod._llm_rejections == {"length": 2, "divergence": 1}
        capture_mod._llm_rejections.clear()


class TestLLMPostEnabledByDefault:
    def test_on_unless_turned_off(self):
        import importlib, os
        saved = os.environ.pop("LLM_POST", None)
        try:
            assert importlib.reload(capture_mod).ENABLE_LLM_POST is True
        finally:
            if saved is not None:
                os.environ["LLM_POST"] = saved
            importlib.reload(capture_mod)

    def test_can_be_turned_off(self):
        import importlib, os
        saved = os.environ.get("LLM_POST")
        os.environ["LLM_POST"] = "0"
        try:
            assert importlib.reload(capture_mod).ENABLE_LLM_POST is False
        finally:
            if saved is None:
                os.environ.pop("LLM_POST", None)
            else:
                os.environ["LLM_POST"] = saved
            importlib.reload(capture_mod)


class TestModelResolution:
    """The menu-bar model name must reach the model file.

    It used to land in MODEL_SIZE, which nothing read — the selector offered
    five names, none of them the model the installer downloads, and picking one
    changed nothing.
    """

    def test_name_becomes_a_ggml_file(self, monkeypatch):
        monkeypatch.delenv("WHISPER_MODEL_PATH", raising=False)
        assert capture_mod.resolve_model_path("medium").name == "ggml-medium.bin"

    def test_lands_in_the_models_dir(self, monkeypatch):
        monkeypatch.delenv("WHISPER_MODEL_PATH", raising=False)
        assert capture_mod.resolve_model_path("small").parent == capture_mod.MODELS_DIR

    def test_explicit_path_wins(self, monkeypatch):
        monkeypatch.setenv("WHISPER_MODEL_PATH", "/tmp/custom.bin")
        assert str(capture_mod.resolve_model_path("medium")) == "/tmp/custom.bin"

    def test_defaults_to_the_configured_model(self, monkeypatch):
        monkeypatch.delenv("WHISPER_MODEL_PATH", raising=False)
        expected = f"ggml-{capture_mod.WHISPER_MODEL}.bin"
        assert capture_mod.resolve_model_path().name == expected


# -- Tests for vocabulary biasing ----------------------------------------------

class TestBuildWhisperPrompt:
    """The initial prompt is where names and terms get fixed — in the decoder,
    not afterwards by a model that cannot check its own work."""

    def test_no_terms_gives_no_prompt(self):
        assert capture_mod.build_whisper_prompt(participants=[], glossary=[]) == ""

    def test_includes_glossary_and_participants(self):
        p = capture_mod.build_whisper_prompt(participants=["Катя"],
                                             glossary=["Claude Code"])
        assert "Claude Code" in p and "Катя" in p

    def test_participants_come_last(self):
        """Later tokens weigh more, and a name is what whisper cannot guess."""
        p = capture_mod.build_whisper_prompt(participants=["Валера"],
                                             glossary=["MCP", "Researcher"])
        assert p.index("Researcher") < p.index("Валера")

    def test_deduplicates_case_insensitively(self):
        p = capture_mod.build_whisper_prompt(participants=["catya"],
                                             glossary=["Catya", "CATYA"])
        assert p.lower().count("catya") == 1

    def test_blank_entries_dropped(self):
        p = capture_mod.build_whisper_prompt(participants=["  ", ""],
                                             glossary=["MCP", "   "])
        assert p.count(",") == 0 and "MCP" in p

    def test_respects_the_224_token_budget(self):
        p = capture_mod.build_whisper_prompt(
            participants=["Валера"], glossary=[f"термин{i}" for i in range(300)])
        assert len(p) <= capture_mod.WHISPER_PROMPT_MAX_CHARS

    def test_trimming_keeps_the_tail(self):
        """Front is dropped, because the end of the prompt is what carries."""
        p = capture_mod.build_whisper_prompt(
            participants=["Валера"], glossary=[f"термин{i}" for i in range(300)])
        assert "Валера" in p
        assert "термин0," not in p


class TestGlossaryLoading:
    def _with_config(self, tmp_path, body):
        (tmp_path / "config.json").write_text(body, encoding="utf-8")
        return patch.object(capture_mod, "CONFIG_FILE", tmp_path / "config.json")

    def test_reads_a_list(self, tmp_path):
        with self._with_config(tmp_path, '{"glossary": ["MCP", "Researcher"]}'):
            assert capture_mod._load_glossary() == ["MCP", "Researcher"]

    def test_reads_a_comma_string(self, tmp_path):
        with self._with_config(tmp_path, '{"glossary": "MCP, Researcher"}'):
            assert capture_mod._load_glossary() == ["MCP", "Researcher"]

    def test_missing_key_is_empty(self, tmp_path):
        with self._with_config(tmp_path, '{"language": "ru"}'):
            assert capture_mod._load_glossary() == []

    def test_malformed_config_is_empty(self, tmp_path):
        with self._with_config(tmp_path, "{not json"):
            assert capture_mod._load_glossary() == []

    def test_absent_config_is_empty(self, tmp_path):
        with patch.object(capture_mod, "CONFIG_FILE", tmp_path / "nope.json"):
            assert capture_mod._load_glossary() == []


class TestPromptReachesWhisper:
    """Building the string is worthless if it never leaves the process."""

    def test_server_request_carries_the_prompt(self):
        captured = {}

        class FakeResponse:
            def read(self): return b'{"text": "ok"}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            capture_mod._transcribe_via_server(b"RIFFfake", "ru", "Термины: MCP.")

        body = captured["body"].decode("utf-8", errors="replace")
        assert 'name="prompt"' in body
        assert "Термины: MCP." in body

    def test_server_request_omits_an_empty_prompt(self):
        captured = {}

        class FakeResponse:
            def read(self): return b'{"text": "ok"}'
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            captured["body"] = req.data
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            capture_mod._transcribe_via_server(b"RIFFfake", "ru", "")

        assert 'name="prompt"' not in captured["body"].decode("utf-8", errors="replace")

    def test_cli_receives_prompt_flag(self):
        audio = np.full(16000, 0.2, dtype=np.float32)
        with patch("capture.subprocess.run") as run:
            run.return_value = MagicMock(returncode=0, stdout=b"ok\n")
            capture_mod._transcribe_via_cli(audio, "ru", "Термины: MCP.")
        whisper_call = [c for c in run.call_args_list
                        if "--prompt" in (c[0][0] if c[0] else [])]
        assert whisper_call, "whisper-cli was not given --prompt"
        cmd = whisper_call[0][0][0]
        assert cmd[cmd.index("--prompt") + 1] == "Термины: MCP."

    def test_transcribe_chunk_builds_and_forwards_the_prompt(self):
        capture_mod._mix_noise.reset()
        seen = {}
        def fake_cli(audio, lang, prompt):
            seen["prompt"] = prompt
            return capture_mod.Transcription(text="реальная реплика целиком")

        with patch.object(capture_mod, "build_whisper_prompt", return_value="P"), \
             patch.object(capture_mod, "_transcribe_via_cli", side_effect=fake_cli):
            capture_mod.transcribe_chunk(None, _speechlike(0.02))
        assert seen["prompt"] == "P"
        capture_mod._mix_noise.reset()


# -- Tests for per-word confidence ---------------------------------------------

def _verbose(words):
    """A verbose_json payload shaped like whisper-server's."""
    return {
        "text": " ".join(w for w, _ in words),
        "segments": [{
            "id": 0,
            "text": " ".join(w for w, _ in words),
            "words": [{"word": " " + w, "probability": p} for w, p in words],
        }],
    }


class TestParseVerboseJson:
    """whisper-server reports a probability per word; we used to discard it."""

    def test_extracts_text_and_words(self):
        t = capture_mod._parse_verbose_json(_verbose([("бюджет", 0.99), ("250", 0.9)]))
        assert t.text == "бюджет 250"
        assert t.words == [("бюджет", 0.99), ("250", 0.9)]

    def test_joins_multiple_segments(self):
        payload = {"text": "а б", "segments": [
            {"words": [{"word": " а", "probability": 0.9}]},
            {"words": [{"word": " б", "probability": 0.8}]},
        ]}
        assert len(capture_mod._parse_verbose_json(payload).words) == 2

    def test_survives_a_reply_with_no_words(self):
        t = capture_mod._parse_verbose_json({"text": "привет"})
        assert t.text == "привет" and not t.has_confidence

    def test_skips_malformed_entries(self):
        payload = {"text": "x", "segments": [{"words": [
            {"word": " ок", "probability": 0.9},
            {"word": "", "probability": 0.9},
            {"word": " нет-вероятности"},
            {"word": " плохая", "probability": None},
        ]}]}
        assert capture_mod._parse_verbose_json(payload).words == [("ок", 0.9)]

    def test_empty_payload(self):
        t = capture_mod._parse_verbose_json({})
        assert t.text == "" and not t.has_confidence


class TestTranscriptionConfidence:
    def test_low_confidence_words_listed(self):
        t = capture_mod.Transcription("x", [(" да", 0.99), (" мутное", 0.2)])
        assert t.low_confidence_words == ["мутное"]

    def test_fraction_computed(self):
        t = capture_mod.Transcription("x", [(" а", 0.1), (" б", 0.1), (" в", 0.9), (" г", 0.9)])
        assert t.low_confidence_fraction == 0.5

    def test_confident_chunk_is_not_noise(self):
        t = capture_mod.Transcription("x", [(f" w{i}", 0.95) for i in range(10)])
        assert t.looks_like_noise is False

    def test_mostly_guessed_chunk_is_noise(self):
        t = capture_mod.Transcription("x", [(f" w{i}", 0.2) for i in range(10)])
        assert t.looks_like_noise is True

    def test_too_short_to_judge(self):
        """Two guessed words is not evidence of anything."""
        t = capture_mod.Transcription("x", [(" а", 0.1), (" б", 0.1)])
        assert t.looks_like_noise is False

    def test_no_confidence_means_no_verdict(self):
        """The CLI path reports nothing, and must not be judged as noise."""
        assert capture_mod.Transcription("длинная реплика").looks_like_noise is False


class TestNoiseChunkDiscarded:
    def setup_method(self):
        capture_mod._mix_noise.reset()

    def teardown_method(self):
        capture_mod._mix_noise.reset()

    def _run(self, result):
        with patch.object(capture_mod, "_transcribe_via_cli", return_value=result), \
             patch.object(capture_mod, "build_whisper_prompt", return_value=""):
            return capture_mod.transcribe_chunk(None, _speechlike(0.02))

    def test_guessed_chunk_returns_nothing(self):
        junk = capture_mod.Transcription(
            "какой-то правдоподобный мусор здесь",
            [(f" w{i}", 0.15) for i in range(8)])
        assert self._run(junk) == ""

    def test_confident_chunk_survives(self):
        good = capture_mod.Transcription(
            "мы обсудили бюджет на квартал",
            [(f" w{i}", 0.95) for i in range(8)])
        assert self._run(good) == "мы обсудили бюджет на квартал"

    def test_confidence_is_kept_for_the_correction_stage(self):
        good = capture_mod.Transcription(
            "мы обсудили бюджет на квартал",
            [(" мы", 0.99), (" обсудили", 0.99), (" бюджет", 0.4),
             (" на", 0.99), (" квартал", 0.99)])
        self._run(good)
        assert capture_mod.last_transcription().low_confidence_words == ["бюджет"]

    def test_discarded_chunk_leaves_no_stale_confidence(self):
        self._run(capture_mod.Transcription("ок", [(" ок", 0.99)]))
        junk = capture_mod.Transcription("мусор", [(f" w{i}", 0.1) for i in range(8)])
        self._run(junk)
        assert capture_mod.last_transcription().tokens == []


class TestSubwordMerging:
    """whisper scores tokens, not words. 'Claude' arrives as ' Cla' + 'ude'."""

    def test_pieces_join_into_one_word(self):
        t = capture_mod.Transcription("Claude", [(" Cla", 0.35), ("ude", 1.0)])
        assert [w for w, _ in t.words] == ["Claude"]

    def test_word_takes_its_weakest_piece(self):
        t = capture_mod.Transcription("Claude", [(" Cla", 0.35), ("ude", 1.0)])
        assert t.words[0][1] == 0.35

    def test_the_whole_word_is_reported_not_the_fragment(self):
        """Observed for real: the doubtful item used to come back as 'Cla'."""
        t = capture_mod.Transcription("x", [(" Cla", 0.35), ("ude", 1.0),
                                            (" Code", 0.98)])
        assert t.low_confidence_words == ["Claude"]

    def test_hyphenated_word_stays_one_word(self):
        t = capture_mod.Transcription("x", [(" М", 0.52), ("айн", 0.75),
                                            ("д", 0.99), ("-", 0.57), ("карту", 0.9)])
        assert [w for w, _ in t.words] == ["Майнд-карту"]

    def test_first_token_without_a_space_still_starts_a_word(self):
        t = capture_mod.Transcription("x", [("Мы", 0.9), (" были", 0.9)])
        assert [w for w, _ in t.words] == ["Мы", "были"]


class TestLanguageLock:
    """Auto-detect per chunk guesses wrong on a quiet 20 seconds — which is
    where English hallucinations in Russian meetings came from. Detect once on
    a chunk worth trusting, then hold it."""

    def setup_method(self):
        capture_mod._detected_language = None
        capture_mod._language_votes.clear()
        self._saved = capture_mod.LANGUAGE
        capture_mod.LANGUAGE = "auto"

    def teardown_method(self):
        capture_mod.LANGUAGE = self._saved
        capture_mod._detected_language = None
        capture_mod._language_votes.clear()

    def _vote(self, language, times=None):
        for _ in range(times or capture_mod.LANGUAGE_LOCK_VOTES):
            capture_mod._maybe_lock_language(self._solid(language))

    def _solid(self, language):
        text = ("нормальная длинная реплика про бюджет"
                if language in ("russian", "ukrainian")
                else "a perfectly ordinary sentence about the budget")
        return capture_mod.Transcription(
            text, [(f" w{i}", 0.95) for i in range(8)], language=language)

    def test_auto_until_something_is_detected(self):
        assert capture_mod.effective_language() == "auto"

    def test_locks_after_enough_agreeing_chunks(self):
        self._vote("russian")
        assert capture_mod.effective_language() == "ru"

    def test_one_chunk_is_not_enough(self):
        self._vote("russian", times=1)
        assert capture_mod.effective_language() == "auto"

    def test_disagreement_does_not_lock(self):
        capture_mod._maybe_lock_language(self._solid("russian"))
        capture_mod._maybe_lock_language(self._solid("english"))
        capture_mod._maybe_lock_language(self._solid("russian"))
        assert capture_mod.effective_language() == "auto"

    def test_does_not_relock_once_set(self):
        self._vote("russian")
        self._vote("english")
        assert capture_mod.effective_language() == "ru"

    def test_ignores_a_guessed_chunk(self):
        junk = capture_mod.Transcription(
            "мусор", [(f" w{i}", 0.1) for i in range(8)], language="english")
        capture_mod._maybe_lock_language(junk)
        assert capture_mod.effective_language() == "auto"

    def test_ignores_a_chunk_too_short_to_judge(self):
        thin = capture_mod.Transcription("да", [(" да", 0.99)], language="english")
        capture_mod._maybe_lock_language(thin)
        assert capture_mod.effective_language() == "auto"

    def test_unknown_language_name_does_not_lock(self):
        self._vote("klingon")
        assert capture_mod.effective_language() == "auto"

    def test_english_claimed_over_cyrillic_is_not_believed(self):
        """Observed for real: the glossary prompt is English product names and
        whisper reported 'english' while transcribing correct Russian."""
        russian_text = capture_mod.Transcription(
            "мы обсудили бюджет и сроки по проекту",
            [(f" w{i}", 0.95) for i in range(8)], language="english")
        for _ in range(capture_mod.LANGUAGE_LOCK_VOTES):
            capture_mod._maybe_lock_language(russian_text)
        assert capture_mod.effective_language() == "auto"

    def test_script_check_ignores_languages_it_cannot_judge(self):
        japanese = capture_mod.Transcription(
            "ご視聴ありがとうございました、また次回",
            [(f" w{i}", 0.95) for i in range(8)], language="japanese")
        for _ in range(capture_mod.LANGUAGE_LOCK_VOTES):
            capture_mod._maybe_lock_language(japanese)
        assert capture_mod.effective_language() == "ja"

    def test_no_confidence_never_locks(self):
        """The CLI path reports neither words nor language."""
        capture_mod._maybe_lock_language(capture_mod.Transcription("текст"))
        assert capture_mod.effective_language() == "auto"

    def test_forced_language_is_never_overridden(self):
        capture_mod.LANGUAGE = "ru"
        self._vote("english")
        assert capture_mod.effective_language() == "ru"

    def test_parser_reads_the_detected_language(self):
        t = capture_mod._parse_verbose_json(
            {"text": "x", "language": "russian", "segments": []})
        assert t.language == "russian"


class TestLanguageDefault:
    def test_defaults_to_auto_not_russian(self):
        """A hardcoded 'ru' would hand every English speaker Russian output."""
        import importlib, os
        saved = os.environ.pop("LANGUAGE", None)
        try:
            assert importlib.reload(capture_mod).LANGUAGE == "auto"
        finally:
            if saved is not None:
                os.environ["LANGUAGE"] = saved
            importlib.reload(capture_mod)


class TestPromptEchoStripping:
    """whisper writes the vocabulary prompt out as speech when the audio is
    unclear — 232 lines across six live meetings started with it."""

    TERMS = ["Claude Code", "MCP", "майнд-карта", "Валера"]

    def strip(self, text):
        return capture_mod.strip_prompt_echo(text, self.TERMS)

    def test_pure_echo_becomes_empty(self):
        assert self.strip("Claude Code, MCP, майнд-карта, Валера") == ""

    def test_echo_glued_in_front_of_speech_keeps_the_speech(self):
        assert self.strip("Claude Code, MCP: Саши тоже долго думал над этим") \
            == "Саши тоже долго думал над этим"

    def test_dash_separated_echo(self):
        assert self.strip("MCP, Валера — и вот это очень важно") == "и вот это очень важно"

    def test_terms_mid_sentence_are_left_alone(self):
        """Someone saying the word is not the prompt leaking."""
        line = "мы обсудили MCP и майнд-карту подробно"
        assert self.strip(line) == line

    def test_a_single_leading_term_is_not_an_echo(self):
        """A sentence may open with a product name."""
        line = "MCP, кстати, вчера сломался"
        assert self.strip(line) == line

    def test_ordinary_speech_untouched(self):
        line = "Обычная реплика без всякого эха"
        assert self.strip(line) == line

    def test_no_terms_means_no_stripping(self):
        assert capture_mod.strip_prompt_echo("Claude Code, MCP", []) == "Claude Code, MCP"

    def test_empty_text(self):
        assert self.strip("") == ""

    def test_quoted_echo(self):
        assert self.strip('"Claude Code", "MCP": реальная реплика') == "реальная реплика"


class TestPromptIsABareList:
    def test_no_lead_in_phrase(self):
        """The lead-in was what whisper carried on writing."""
        p = capture_mod.build_whisper_prompt(participants=["Валера"],
                                             glossary=["MCP"])
        assert "Участники" not in p and "Совещание" not in p
        assert p == "MCP, Валера"

    def test_terms_recoverable_from_a_rendered_prompt(self):
        p = capture_mod.build_whisper_prompt(participants=["Валера"],
                                             glossary=["Claude Code", "MCP"])
        assert capture_mod._prompt_terms(p) == ["Claude Code", "MCP", "Валера"]


class TestChunkBounding:
    """Transcription runs near 1x realtime, so a backlog compounds: a longer
    chunk is slower, which leaves more audio buffered, which is longer still."""

    def test_chunk_is_capped(self):
        assert capture_mod.MAX_CHUNK_SECONDS <= 30

    def test_short_audio_passes_through_whole(self):
        a = np.arange(100, dtype=np.float32)
        head, rest = capture_mod.bound_chunk(a, 200)
        assert len(head) == 100 and rest is None

    def test_long_audio_is_split_not_truncated(self):
        """The original cap in 7fc7262 threw the excess away."""
        a = np.arange(500, dtype=np.float32)
        head, rest = capture_mod.bound_chunk(a, 200)
        assert len(head) == 200
        assert len(rest) == 300
        assert np.array_equal(np.concatenate([head, rest]), a), "no audio lost"

    def test_exactly_at_the_bound(self):
        a = np.arange(200, dtype=np.float32)
        head, rest = capture_mod.bound_chunk(a, 200)
        assert len(head) == 200 and rest is None

    def test_degenerate_inputs(self):
        assert capture_mod.bound_chunk(None, 10) == (None, None)
        a = np.arange(10, dtype=np.float32)
        head, rest = capture_mod.bound_chunk(a, 0)
        assert rest is None and len(head) == 10


class TestDropReasons:
    """A failure must never be reported as silence."""

    def setup_method(self):
        capture_mod._mix_noise.reset()

    def teardown_method(self):
        capture_mod._mix_noise.reset()

    def test_gated_silence_has_no_reason(self):
        capture_mod.transcribe_chunk(None, _room_tone(1e-6, seconds=2))
        assert capture_mod.last_transcription().dropped_reason == ""

    def test_server_timeout_is_recorded_as_such(self):
        capture_mod._whisper_server_available = True
        try:
            with patch.object(capture_mod, "_transcribe_via_server",
                              side_effect=TimeoutError("timed out")), \
                 patch.object(capture_mod, "build_whisper_prompt", return_value=""):
                out = capture_mod.transcribe_chunk(None, _speechlike(0.02))
        finally:
            capture_mod._whisper_server_available = False
        assert out == ""
        assert "timed out" in capture_mod.last_transcription().dropped_reason

    def test_timeout_does_not_fall_through_to_the_cli(self):
        """The CLI reloads the model and is slower — retrying doubles the loss."""
        capture_mod._whisper_server_available = True
        try:
            with patch.object(capture_mod, "_transcribe_via_server",
                              side_effect=TimeoutError("timed out")), \
                 patch.object(capture_mod, "_transcribe_via_cli") as cli, \
                 patch.object(capture_mod, "build_whisper_prompt", return_value=""):
                capture_mod.transcribe_chunk(None, _speechlike(0.02))
        finally:
            capture_mod._whisper_server_available = False
        cli.assert_not_called()

    def test_guessed_chunk_says_so(self):
        junk = capture_mod.Transcription("мусор", [(f" w{i}", 0.1) for i in range(8)])
        with patch.object(capture_mod, "_transcribe_via_cli", return_value=junk), \
             patch.object(capture_mod, "build_whisper_prompt", return_value=""):
            capture_mod.transcribe_chunk(None, _speechlike(0.02))
        assert "guessed" in capture_mod.last_transcription().dropped_reason

    def test_non_timeout_failure_still_tries_the_cli(self):
        capture_mod._whisper_server_available = True
        good = capture_mod.Transcription("реальная реплика целиком",
                                         [(f" w{i}", 0.95) for i in range(6)])
        try:
            with patch.object(capture_mod, "_transcribe_via_server",
                              side_effect=ValueError("broken json")), \
                 patch.object(capture_mod, "_transcribe_via_cli", return_value=good) as cli, \
                 patch.object(capture_mod, "build_whisper_prompt", return_value=""):
                capture_mod.transcribe_chunk(None, _speechlike(0.02))
        finally:
            capture_mod._whisper_server_available = False
        cli.assert_called_once()


class TestMutePolling:
    def test_interval_is_five_seconds(self):
        """Each poll spawns osascript; 5s halves the cost of an hour-long call
        and widens the window where a muted mic is still recorded to 5s."""
        assert capture_mod.MUTE_POLL_SECONDS == 5.0


class TestPromptAllowsTermCorrection:
    """The stage exists to fix garbled product names, among other things. The
    first version forbade exactly that with "Do not change numbers or names"."""

    def test_glossary_is_given_to_the_model(self):
        p = capture_mod._build_llm_prompt("чанк", "контекст", [], ["мейнд"],
                                          ["MindManager", "MCP"])
        assert "MindManager, MCP" in p

    def test_names_are_no_longer_frozen(self):
        p = capture_mod._build_llm_prompt("чанк", "контекст", [], None,
                                          ["MindManager"])
        assert "Do not change numbers or names" not in p
        assert "Do not change numbers." in p

    def test_model_told_to_map_garbled_words_onto_terms(self):
        """Wording tightened after the untargeted run rewrote real names; the
        permission to substitute must survive that."""
        p = capture_mod._build_llm_prompt("чанк", "контекст", [], None, ["MCP"])
        assert "may be replaced with a known term" in p
        assert "garbled Researcher" in p

    def test_inventing_unlisted_names_still_forbidden(self):
        p = capture_mod._build_llm_prompt("чанк", "контекст", [], None, ["MCP"])
        assert "not introduce names that are not listed" in p

    def test_no_glossary_means_no_block(self):
        p = capture_mod._build_llm_prompt("чанк", "контекст", [], None, [])
        assert "Known terms" not in p


class TestRestoredMigrationFixes:
    """Three fixes that existed before the whisper.cpp migration and did not
    survive it. Two were found by accident; these came from walking the
    pre-migration commits deliberately."""

    def test_no_text_context_between_segments(self):
        """condition_on_previous_text=False from d77a56f. With context on, the
        decoder repeats itself and continues the initial prompt."""
        src = pathlib.Path(capture_mod.__file__).read_text(encoding="utf-8")
        assert '"-mc", "0"' in src

    def test_language_probability_is_read(self):
        t = capture_mod._parse_verbose_json({
            "text": "x", "detected_language": "russian",
            "detected_language_probability": 0.93, "segments": []})
        assert t.language == "russian"
        assert t.language_probability == pytest.approx(0.93)

    def test_language_probability_defaults_to_zero(self):
        assert capture_mod._parse_verbose_json({"text": "x"}).language_probability == 0.0


class TestLanguageConfidenceGate:
    """2966fed: do not lock a language whisper is only guessing at."""

    def setup_method(self):
        capture_mod._detected_language = None
        capture_mod._language_votes.clear()
        self._saved = capture_mod.LANGUAGE
        capture_mod.LANGUAGE = "auto"

    def teardown_method(self):
        capture_mod.LANGUAGE = self._saved
        capture_mod._detected_language = None
        capture_mod._language_votes.clear()

    def _chunk(self, prob):
        return capture_mod.Transcription(
            "нормальная длинная реплика про бюджет",
            [(f" w{i}", 0.95) for i in range(8)],
            language="russian", language_probability=prob)

    def test_confident_detection_locks(self):
        for _ in range(capture_mod.LANGUAGE_LOCK_VOTES):
            capture_mod._maybe_lock_language(self._chunk(0.95))
        assert capture_mod.effective_language() == "ru"

    def test_low_confidence_never_locks(self):
        for _ in range(capture_mod.LANGUAGE_LOCK_VOTES * 3):
            capture_mod._maybe_lock_language(self._chunk(0.4))
        assert capture_mod.effective_language() == "auto"

    def test_missing_probability_falls_back_to_the_other_checks(self):
        """The CLI path reports no probability; votes and script still apply."""
        for _ in range(capture_mod.LANGUAGE_LOCK_VOTES):
            capture_mod._maybe_lock_language(self._chunk(0.0))
        assert capture_mod.effective_language() == "ru"


class TestReplacementScope:
    """The model was rewriting correctly heard product names into whichever
    glossary entry looked closest: "Codex" -> "Claude Code"."""

    def test_only_doubtful_words_may_be_replaced(self):
        p = capture_mod._build_llm_prompt("чанк", "ctx", [], ["ресерчер"], ["Researcher"])
        assert "Only these words may be replaced: ресерчер." in p

    def test_no_scope_line_without_doubtful_words(self):
        p = capture_mod._build_llm_prompt("чанк", "ctx", [], None, ["Researcher"])
        assert "Only these words may be replaced" not in p

    def test_real_names_are_protected_by_name(self):
        p = capture_mod._build_llm_prompt("чанк", "ctx", [], None, ["Researcher"])
        assert "Codex" in p and "must be left exactly as they are" in p


class TestMarkdownStripping:
    """The model emphasises terms it corrects. Nobody speaks in bold."""

    def test_bold_removed(self):
        assert capture_mod._strip_think_block("скажи своему **Claude Code** быстро") \
            == "скажи своему Claude Code быстро"

    def test_italic_removed(self):
        assert capture_mod._strip_think_block("это *важно* очень") == "это важно очень"

    def test_underscore_emphasis_removed(self):
        assert capture_mod._strip_think_block("это __важно__ очень") == "это важно очень"

    def test_lone_asterisk_left_alone(self):
        assert capture_mod._strip_think_block("умножить 5 * 3") == "умножить 5 * 3"

    def test_plain_text_untouched(self):
        assert capture_mod._strip_think_block("обычная реплика") == "обычная реплика"
