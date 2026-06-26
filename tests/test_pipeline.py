"""
E2E pipeline tests for ghostmic — capture.py internals.
Tests writer_thread, tap_reader_thread, _postprocess_text, and config.json handling.
No audio hardware or Whisper model needed — uses mocks for transcription.
"""
import io
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import numpy as np
import pytest

# Mock sounddevice and mlx_lm before importing capture
sys.modules.setdefault("sounddevice", MagicMock())
sys.modules.setdefault("mlx_lm", MagicMock())

sys.path.insert(0, str(Path(__file__).parent.parent))
import capture as capture_mod

PROJECT_DIR = Path(__file__).resolve().parent.parent


class TestWriterThreadProducesFile:
    """Put synthetic float32 audio chunks into audio_queue, run writer_thread
    with mocked transcribe_chunk that returns known text, verify transcript
    file contains the expected lines with timestamps."""

    def test_writer_thread_produces_file(self, tmp_path):
        transcript_file = tmp_path / "test_writer_output.txt"
        transcript_file.touch()

        # Known text that transcribe_chunk will return for each chunk
        expected_texts = [
            "[Remote] First chunk of speech",
            "[You] Second chunk of speech",
            "[Remote] Third chunk of speech",
        ]
        call_count = [0]

        def mock_transcribe(model, audio_np):
            idx = call_count[0]
            call_count[0] += 1
            if idx < len(expected_texts):
                return expected_texts[idx]
            return ""

        # Create fresh queues and events for this test (avoid cross-test pollution)
        test_audio_queue = queue.Queue(maxsize=240)
        test_mic_queue = queue.Queue(maxsize=240)
        test_shutdown = threading.Event()
        test_error = threading.Event()

        # Feed chunks from a separate thread with small delays so that the
        # writer_thread's drain+buffer logic processes them as separate chunks
        # rather than concatenating everything into one giant audio blob.
        def feeder():
            for chunk_idx in range(3):
                # Feed exactly one CHUNK_SECONDS worth of audio per iteration
                for _ in range(capture_mod.CHUNK_SECONDS):
                    audio_block = np.random.randn(capture_mod.SAMPLE_RATE).astype(np.float32) * 0.1
                    test_audio_queue.put(audio_block)
                # Wait for writer to process this chunk before feeding the next
                time.sleep(0.3)
            # All chunks fed; signal shutdown
            test_shutdown.set()

        with patch.object(capture_mod, "audio_queue", test_audio_queue), \
             patch.object(capture_mod, "mic_queue", test_mic_queue), \
             patch.object(capture_mod, "shutdown_event", test_shutdown), \
             patch.object(capture_mod, "error_event", test_error), \
             patch.object(capture_mod, "TRANSCRIPT_FILE", transcript_file), \
             patch.object(capture_mod, "transcribe_chunk", side_effect=mock_transcribe), \
             patch.object(capture_mod, "ENABLE_DIARIZATION", False), \
             patch.object(capture_mod, "ENABLE_LLM_POST", False), \
             patch.object(capture_mod, "_prev_chunks", []), \
             patch.object(capture_mod, "_prev_speaker", ""):

            feeder_thread = threading.Thread(target=feeder, daemon=True)
            feeder_thread.start()

            wt = threading.Thread(target=capture_mod.writer_thread, args=(None, False))
            wt.start()
            wt.join(timeout=30)
            assert not wt.is_alive(), "writer_thread did not exit in time"
            feeder_thread.join(timeout=5)

        content = transcript_file.read_text(encoding="utf-8")
        lines = [l for l in content.strip().split("\n") if l.strip()]

        # Should have produced at least 2 transcript lines
        assert len(lines) >= 2, f"Expected >= 2 transcript lines, got {len(lines)}:\n{content}"

        # Each line should have a timestamp pattern [HH:MM:SS-HH:MM:SS]
        for line in lines:
            assert line.startswith("["), f"Line missing timestamp: {line}"
            assert "]" in line, f"Line missing closing bracket: {line}"

        # Verify the known text fragments appear in the output
        # (postprocessing may alter labels but the speech content should be there)
        assert "First chunk of speech" in content
        assert "Second chunk of speech" in content


class TestTapReaderThread:
    """Create a subprocess that writes known PCM data to stdout,
    start tap_reader_thread, verify audio_queue receives the data
    correctly converted to float32."""

    def test_tap_reader_thread(self):
        # Generate known PCM int16 data (1 second = 16000 samples)
        num_samples = capture_mod.SAMPLE_RATE  # 1 second
        known_values = np.array([1000, -1000, 2000, -2000, 500], dtype=np.int16)
        # Repeat to fill 1 second
        pcm_int16 = np.tile(known_values, num_samples // len(known_values) + 1)[:num_samples]
        pcm_bytes = pcm_int16.tobytes()

        # Create a subprocess that writes the known PCM bytes to stdout
        # We use python -c to write raw bytes to stdout
        script = (
            "import sys, os\n"
            f"data = {pcm_bytes!r}\n"
            "sys.stdout.buffer.write(data)\n"
            "sys.stdout.buffer.flush()\n"
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

        test_audio_queue = queue.Queue(maxsize=240)
        test_shutdown = threading.Event()

        with patch.object(capture_mod, "audio_queue", test_audio_queue), \
             patch.object(capture_mod, "shutdown_event", test_shutdown):

            reader = threading.Thread(
                target=capture_mod.tap_reader_thread,
                args=(proc,),
                daemon=True,
            )
            reader.start()

            # Wait for data to arrive in the queue
            try:
                audio_chunk = test_audio_queue.get(timeout=10)
            except queue.Empty:
                pytest.fail("tap_reader_thread did not produce any audio data")

            # Verify the conversion: int16 -> float32 / 32768.0
            expected_float32 = pcm_int16.astype(np.float32) / 32768.0
            np.testing.assert_allclose(audio_chunk, expected_float32, atol=1e-6)

            # Verify dtype
            assert audio_chunk.dtype == np.float32

        # Cleanup
        test_shutdown.set()
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        reader.join(timeout=5)


class TestPostprocessContinuityAcrossChunks:
    """Simulate 5 sequential calls to _postprocess_text with alternating speakers,
    verify labels appear correctly (label on change, ... on continuation)."""

    def test_postprocess_continuity_across_chunks(self):
        # Reset postprocessing state
        capture_mod._prev_chunks.clear()
        capture_mod._prev_speaker = ""

        # Disable LLM post-processing for deterministic output
        original_enable = capture_mod.ENABLE_LLM_POST
        original_model = capture_mod._llm_model
        capture_mod.ENABLE_LLM_POST = False
        capture_mod._llm_model = None

        try:
            inputs = [
                ("[Remote] Hello everyone, welcome to the meeting.", "[00:00:00-00:00:10]"),
                ("[Remote] Let me share my screen.", "[00:00:10-00:00:20]"),
                ("[You] Yes, I can see it now.", "[00:00:20-00:00:30]"),
                ("[You] The numbers look good.", "[00:00:30-00:00:40]"),
                ("[Remote] Great, let's move to the next topic.", "[00:00:40-00:00:50]"),
            ]

            results = []
            for text, ts in inputs:
                result = capture_mod._postprocess_text(text, ts)
                results.append(result)

            # Chunk 0: [Remote] first appearance -> should show label
            assert results[0].startswith("[Remote]"), \
                f"First Remote chunk should show label, got: {results[0]}"
            assert "Hello everyone" in results[0]

            # Chunk 1: [Remote] continuation -> should show "..." (no label)
            assert results[1].startswith("..."), \
                f"Continued Remote chunk should start with '...', got: {results[1]}"
            assert "share my screen" in results[1]

            # Chunk 2: [You] speaker change -> should show label
            assert results[2].startswith("[You]"), \
                f"Speaker change to [You] should show label, got: {results[2]}"
            assert "I can see it" in results[2]

            # Chunk 3: [You] continuation -> should show "..."
            assert results[3].startswith("..."), \
                f"Continued [You] chunk should start with '...', got: {results[3]}"
            assert "numbers look good" in results[3]

            # Chunk 4: [Remote] speaker change back -> should show label
            assert results[4].startswith("[Remote]"), \
                f"Speaker change back to [Remote] should show label, got: {results[4]}"
            assert "next topic" in results[4]

        finally:
            # Restore state
            capture_mod.ENABLE_LLM_POST = original_enable
            capture_mod._llm_model = original_model
            capture_mod._prev_chunks.clear()
            capture_mod._prev_speaker = ""


class TestConfigJsonAffectsServer:
    """Write a config.json with custom transcripts_path, verify _get_transcripts_dir()
    returns it, then delete config, verify fallback."""

    def test_config_json_affects_server(self, tmp_path):
        # Import server module
        import server as server_mod

        custom_transcripts = tmp_path / "custom_transcripts"
        custom_transcripts.mkdir()

        config_path = server_mod.INSTALL_DIR / "config.json"
        config_existed = config_path.exists()
        old_content = None
        if config_existed:
            old_content = config_path.read_text()

        try:
            # Write config.json with custom transcripts_path
            config_path.write_text(json.dumps({
                "transcripts_path": str(custom_transcripts)
            }))

            result = server_mod._get_transcripts_dir()
            assert result == custom_transcripts, \
                f"Expected {custom_transcripts}, got {result}"

            # Delete config.json and verify fallback to default
            config_path.unlink()
            result = server_mod._get_transcripts_dir()
            assert result == server_mod._DEFAULT_TRANSCRIPTS, \
                f"Expected default {server_mod._DEFAULT_TRANSCRIPTS}, got {result}"

        finally:
            # Restore original config.json state
            if config_existed and old_content is not None:
                config_path.write_text(old_content)
            elif config_path.exists():
                config_path.unlink()

    def test_config_json_invalid_json_falls_back(self, tmp_path):
        """Invalid JSON in config.json should fall back to default."""
        import server as server_mod

        config_path = server_mod.INSTALL_DIR / "config.json"
        config_existed = config_path.exists()
        old_content = None
        if config_existed:
            old_content = config_path.read_text()

        try:
            config_path.write_text("{invalid json!!!")
            result = server_mod._get_transcripts_dir()
            assert result == server_mod._DEFAULT_TRANSCRIPTS

        finally:
            if config_existed and old_content is not None:
                config_path.write_text(old_content)
            elif config_path.exists():
                config_path.unlink()

    def test_config_json_empty_path_falls_back(self, tmp_path):
        """Empty transcripts_path in config.json should fall back to default."""
        import server as server_mod

        config_path = server_mod.INSTALL_DIR / "config.json"
        config_existed = config_path.exists()
        old_content = None
        if config_existed:
            old_content = config_path.read_text()

        try:
            config_path.write_text(json.dumps({"transcripts_path": ""}))
            result = server_mod._get_transcripts_dir()
            assert result == server_mod._DEFAULT_TRANSCRIPTS

        finally:
            if config_existed and old_content is not None:
                config_path.write_text(old_content)
            elif config_path.exists():
                config_path.unlink()
