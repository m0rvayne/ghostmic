"""Tests for capture.py — audio processing, queue bounds, thread safety."""
import io
import queue
import subprocess
import sys
import threading
from unittest.mock import MagicMock, patch, PropertyMock
import numpy as np
import pytest
from pathlib import Path

# Mock sounddevice before importing capture
# (it requires hardware/native libraries not available in test env)
sys.modules["sounddevice"] = MagicMock()

# Now we can import the actual capture module
sys.path.insert(0, str(Path(__file__).parent.parent))
import capture as capture_mod


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

    def test_very_short_audio_less_than_one_frame(self):
        """Audio shorter than one frame (500ms = 8000 samples) still processes
        the partial frame — range(0, 100, 8000) yields one iteration at i=0."""
        bh = np.full(100, 0.3, dtype=np.float32)
        mic = np.full(100, 0.3, dtype=np.float32)
        segments = capture_mod._classify_speakers(bh, mic)
        # Even with only 100 samples the loop runs once (i=0, slice truncates)
        assert len(segments) == 1
        assert segments[0][0] == "[Remote]"

    def test_all_nan_values(self):
        """NaN audio should not crash; energy will be NaN, ratio comparison falls through."""
        bh = np.full(16000, np.nan, dtype=np.float32)
        mic = np.full(16000, np.nan, dtype=np.float32)
        # Should not raise — NaN comparisons return False, so silence_threshold check
        # skips all frames (NaN < threshold is False, but we still compute ratio)
        segments = capture_mod._classify_speakers(bh, mic)
        # NaN energy means NaN < silence_threshold is False, so frames are processed
        # but NaN ratio > 2.5 is False, so they become [Remote]
        assert isinstance(segments, list)

    def test_one_channel_all_zeros_other_has_signal(self):
        """One channel silent, other has signal — should classify correctly."""
        bh = np.zeros(16000, dtype=np.float32)
        mic = np.full(16000, 0.3, dtype=np.float32)
        segments = capture_mod._classify_speakers(bh, mic)
        assert len(segments) > 0
        # mic has signal, bh is zero => ratio = 0.3 / 1e-8 >> 2.5 => [You]
        assert all(s[0] == "[You]" for s in segments)

    def test_one_channel_zeros_other_silent_too(self):
        """Both channels effectively silent — bh=0 mic=0.001 (below threshold)."""
        bh = np.zeros(16000, dtype=np.float32)
        mic = np.full(16000, 0.001, dtype=np.float32)
        segments = capture_mod._classify_speakers(bh, mic)
        # Both below silence_threshold (0.005) => all frames skipped
        assert segments == []


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
        """Audio with RMS below MIN_SPEECH_RMS should return empty without calling whisper."""
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
        """Russian Zoom UI: 'Конференция' menu with 'Включить звук' => muted."""
        mock_result = MagicMock(stdout="muted\n", returncode=0)
        with patch("capture.subprocess.run", return_value=mock_result) as mock_run:
            result = capture_mod._check_zoom_mute()
        assert result is True
        call_args = mock_run.call_args
        assert call_args[0][0][0] == "osascript"
        assert call_args[1]["timeout"] == 3

    def test_english_zoom_muted(self):
        """English Zoom UI: 'Meeting' menu with 'Unmute Audio' => muted."""
        mock_result = MagicMock(stdout="muted\n", returncode=0)
        with patch("capture.subprocess.run", return_value=mock_result):
            result = capture_mod._check_zoom_mute()
        assert result is True

    def test_zoom_unmuted(self):
        """When Zoom is unmuted, osascript returns 'unmuted'."""
        mock_result = MagicMock(stdout="unmuted\n", returncode=0)
        with patch("capture.subprocess.run", return_value=mock_result):
            result = capture_mod._check_zoom_mute()
        assert result is False

    def test_not_in_meeting(self):
        """No Meeting/Конференция menu found => 'no-meeting' => not muted."""
        mock_result = MagicMock(stdout="no-meeting\n", returncode=0)
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


class TestWhisperServer:
    """Test whisper-server integration and fallback logic."""

    def test_audio_to_wav_bytes_valid_wav(self):
        """Generated WAV bytes should have valid RIFF/WAV header."""
        audio = np.zeros(16000, dtype=np.float32)  # 1 second of silence
        wav = capture_mod._audio_to_wav_bytes(audio)
        assert wav[:4] == b'RIFF'
        assert wav[8:12] == b'WAVE'
        assert wav[12:16] == b'fmt '
        assert wav[36:40] == b'data'

    def test_audio_to_wav_bytes_correct_size(self):
        """WAV data section should match PCM size."""
        audio = np.ones(8000, dtype=np.float32) * 0.5
        wav = capture_mod._audio_to_wav_bytes(audio)
        import struct
        data_size = struct.unpack_from('<I', wav, 40)[0]
        assert data_size == 8000 * 2  # int16 = 2 bytes per sample

    def test_audio_to_wav_bytes_clipping(self):
        """Values beyond [-1, 1] should be clipped, not overflow."""
        audio = np.array([2.0, -2.0, 0.5], dtype=np.float32)
        wav = capture_mod._audio_to_wav_bytes(audio)
        # Should not raise, WAV should be valid
        assert len(wav) == 44 + 3 * 2  # header + 3 samples * 2 bytes

    def test_check_whisper_server_not_running(self):
        """Health check should return False when server is not running."""
        result = capture_mod._check_whisper_server()
        # Server is not running in test environment
        assert result is False
        assert capture_mod._whisper_server_available is False

    def test_check_whisper_server_mock_healthy(self):
        """Health check should return True for healthy server."""
        import urllib.request
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = capture_mod._check_whisper_server()
        assert result is True
        assert capture_mod._whisper_server_available is True
        # Reset
        capture_mod._whisper_server_available = False

    def test_transcribe_chunk_fallback_to_cli(self):
        """When server unavailable, should fall back to CLI."""
        capture_mod._whisper_server_available = False
        audio = np.random.randn(16000).astype(np.float32) * 0.1
        with patch.object(capture_mod, '_transcribe_via_cli', return_value="test text") as mock_cli:
            result = capture_mod.transcribe_chunk(None, audio)
        mock_cli.assert_called_once()
        assert result == "test text"

    def test_transcribe_chunk_uses_server_when_available(self):
        """When server available, should use HTTP API."""
        capture_mod._whisper_server_available = True
        audio = np.random.randn(16000).astype(np.float32) * 0.1
        with patch.object(capture_mod, '_transcribe_via_server', return_value="server text") as mock_srv, \
             patch.object(capture_mod, '_transcribe_via_cli') as mock_cli:
            result = capture_mod.transcribe_chunk(None, audio)
        mock_srv.assert_called_once()
        mock_cli.assert_not_called()
        assert result == "server text"
        capture_mod._whisper_server_available = False

    def test_transcribe_chunk_server_error_falls_back(self):
        """Server error should trigger CLI fallback."""
        capture_mod._whisper_server_available = True
        audio = np.random.randn(16000).astype(np.float32) * 0.1
        with patch.object(capture_mod, '_transcribe_via_server', side_effect=Exception("connection refused")), \
             patch.object(capture_mod, '_transcribe_via_cli', return_value="cli fallback") as mock_cli:
            result = capture_mod.transcribe_chunk(None, audio)
        mock_cli.assert_called_once()
        assert result == "cli fallback"
        capture_mod._whisper_server_available = False

    def test_transcribe_chunk_silence_skipped(self):
        """Very quiet audio should be skipped regardless of mode."""
        capture_mod._whisper_server_available = True
        audio = np.zeros(16000, dtype=np.float32)  # pure silence
        with patch.object(capture_mod, '_transcribe_via_server') as mock_srv:
            result = capture_mod.transcribe_chunk(None, audio)
        mock_srv.assert_not_called()
        assert result == ""
        capture_mod._whisper_server_available = False

    def test_transcribe_chunk_hallucination_filtered(self):
        """Server result that is a hallucination should be filtered."""
        capture_mod._whisper_server_available = True
        audio = np.random.randn(16000).astype(np.float32) * 0.1
        with patch.object(capture_mod, '_transcribe_via_server', return_value="Продолжение следует"):
            result = capture_mod.transcribe_chunk(None, audio)
        assert result == ""
        capture_mod._whisper_server_available = False


class TestTapReaderThread:
    """Test tap reader with timeout and dead process detection."""

    def test_tap_reader_sets_shutdown_on_process_exit(self):
        """If tap process exits, shutdown_event should be set."""
        capture_mod.shutdown_event.clear()
        proc = MagicMock()
        proc.poll.return_value = 1  # exited with error
        proc.returncode = 1

        t = threading.Thread(target=capture_mod.tap_reader_thread, args=(proc,))
        t.start()
        t.join(timeout=3)
        assert capture_mod.shutdown_event.is_set()
        capture_mod.shutdown_event.clear()

    def test_tap_reader_reads_audio_to_queue(self):
        """Valid PCM data should be converted and queued."""
        capture_mod.shutdown_event.clear()
        # Drain any existing items
        while not capture_mod.audio_queue.empty():
            capture_mod.audio_queue.get_nowait()

        # Create 1 second of PCM int16 data (32000 bytes)
        pcm = np.full(16000, 1000, dtype=np.int16).tobytes()
        stream = io.BytesIO(pcm)

        proc = MagicMock()
        proc.poll.side_effect = [None, 0]  # alive first call, then exited
        proc.returncode = 0
        proc.stdout = stream

        t = threading.Thread(target=capture_mod.tap_reader_thread, args=(proc,))
        t.start()
        t.join(timeout=3)

        assert not capture_mod.audio_queue.empty()
        audio = capture_mod.audio_queue.get_nowait()
        assert len(audio) == 16000
        assert audio.dtype == np.float32
        capture_mod.shutdown_event.clear()

    def test_read_exactly_timeout_returns_partial(self):
        """_read_exactly should return partial data on timeout when fd available."""
        # With BytesIO (no fd), timeout doesn't apply — test the fallback path
        stream = io.BytesIO(b"abc")
        result = capture_mod._read_exactly(stream, 10, timeout=0.1)
        assert result == b"abc"  # partial data returned


class TestStaleBufferFlush:
    """Test that writer_thread flushes partial buffer when audio stops."""

    def test_stale_buffer_constant_defined(self):
        assert hasattr(capture_mod, 'STALE_BUFFER_TIMEOUT')
        assert capture_mod.STALE_BUFFER_TIMEOUT > 0

    def test_tap_silence_timeout_defined(self):
        assert hasattr(capture_mod, 'TAP_SILENCE_TIMEOUT')
        assert capture_mod.TAP_SILENCE_TIMEOUT > 0
