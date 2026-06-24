#!/usr/bin/env python3
"""
ghostmic — audio capture + transcription.

Two capture modes:
  CoreAudio Tap (default, macOS 14.4+):
    process-audio-tap --bundle-id <app> → stdout → this script reads PCM
    No BlackHole, no Multi-Output Device, no user config needed.

  Legacy (CAPTURE_MODE=legacy):
    BlackHole 2ch → sounddevice → this script
    Requires BlackHole install + Multi-Output Device + Zoom speaker config.
"""
import sys
import os
import queue
import signal
import subprocess
import time
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SECONDS = 30
MAX_QUEUE_CHUNKS = 240  # ~4 min of audio in queue items
_DEFAULT_TRANSCRIPT = Path(__file__).parent / "transcripts" / "meeting_transcript.txt"
TRANSCRIPT_FILE = Path(os.environ.get("TRANSCRIPT_FILE", _DEFAULT_TRANSCRIPT))
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
ENABLE_DIARIZATION = os.environ.get("DIARIZATION", "1") == "1"
CAPTURE_MODE = os.environ.get("CAPTURE_MODE", "coreaudio")  # "coreaudio" or "legacy"
BUNDLE_ID = os.environ.get("BUNDLE_ID", "us.zoom.xos")

AUDIO_TAP_BIN = Path(__file__).parent / ".build" / "process-audio-tap"

# Bounded queues
audio_queue = queue.Queue(maxsize=MAX_QUEUE_CHUNKS)
mic_queue = queue.Queue(maxsize=MAX_QUEUE_CHUNKS)
shutdown_event = threading.Event()
error_event = threading.Event()


# -- Speaker diarization -----------------------------------------------------

_detected_language = None


def _classify_speakers(audio_bh: np.ndarray, audio_mic: np.ndarray,
                       frame_ms: int = 500) -> list[tuple[str, int, int]]:
    """Classify [You] vs [Remote] using energy ratio between the two channels."""
    frame_size = int(SAMPLE_RATE * frame_ms / 1000)
    segments = []
    silence_threshold = 0.005

    for i in range(0, min(len(audio_bh), len(audio_mic)), frame_size):
        bh_frame = audio_bh[i:i + frame_size]
        mic_frame = audio_mic[i:i + frame_size]

        energy_bh = np.sqrt(np.mean(bh_frame ** 2))
        energy_mic = np.sqrt(np.mean(mic_frame ** 2))

        if energy_bh < silence_threshold and energy_mic < silence_threshold:
            continue

        ratio = energy_mic / (energy_bh + 1e-8)
        speaker = "[You]" if ratio > 2.5 else "[Remote]"
        segments.append((speaker, i, i + frame_size))

    if not segments:
        return []
    merged = [segments[0]]
    for speaker, start, end in segments[1:]:
        if speaker == merged[-1][0]:
            merged[-1] = (speaker, merged[-1][1], end)
        else:
            merged.append((speaker, start, end))
    return merged


def _build_diarized_text(text: str, segments: list[tuple[str, int, int]],
                         audio_len: int) -> str:
    if not segments:
        return text
    speaker_time = {}
    for speaker, start, end in segments:
        speaker_time[speaker] = speaker_time.get(speaker, 0) + (end - start)
    dominant = max(speaker_time, key=speaker_time.get)
    if len(speaker_time) > 1:
        total = sum(speaker_time.values())
        you_pct = speaker_time.get("[You]", 0) / total
        if you_pct > 0.7:
            return f"[You] {text}"
        elif you_pct < 0.3:
            return f"[Remote] {text}"
        else:
            return f"[You + Remote] {text}"
    return f"{dominant} {text}"


# -- Transcription ------------------------------------------------------------

def format_time(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def transcribe_chunk(model, audio_np: np.ndarray) -> str:
    global _detected_language
    segments, info = model.transcribe(
        audio_np,
        language=_detected_language,
        beam_size=5,
        vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 500},
    )
    text_parts = []
    for seg in segments:
        text_parts.append(seg.text.strip())
    text = " ".join(text_parts).strip()

    if _detected_language is None and text and info.language:
        _detected_language = info.language
        print(f"[meeting] Language detected: {info.language} (probability: {info.language_probability:.2f})", flush=True)

    return text


def _drain_queue(q: queue.Queue) -> list:
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items


def _write_transcript(path: Path, line: str):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        print(f"[meeting] WRITE ERROR: {e}", file=sys.stderr, flush=True)
        raise


def _touch_heartbeat(path: Path):
    try:
        path.touch()
    except Exception:
        pass


def writer_thread(model, has_mic: bool):
    buffer_bh = []
    buffer_mic = []
    chunk_start = datetime.now()
    last_heartbeat = time.time()

    print(f"[meeting] Recording... transcript -> {TRANSCRIPT_FILE}", flush=True)

    try:
        while not shutdown_event.is_set() or not audio_queue.empty() or not mic_queue.empty():
            bh_chunks = _drain_queue(audio_queue)
            if not bh_chunks:
                try:
                    bh_chunks = [audio_queue.get(timeout=0.5)]
                except queue.Empty:
                    pass
            buffer_bh.extend(bh_chunks)

            if has_mic:
                buffer_mic.extend(_drain_queue(mic_queue))

            total_bh = sum(len(d) for d in buffer_bh)

            if total_bh >= SAMPLE_RATE * CHUNK_SECONDS:
                audio_bh = np.concatenate(buffer_bh).flatten().astype(np.float32)
                audio_mic_raw = None

                if has_mic and buffer_mic:
                    audio_mic_raw = np.concatenate(buffer_mic).flatten().astype(np.float32)
                    max_len = max(len(audio_bh), len(audio_mic_raw))
                    audio_bh_padded = np.pad(audio_bh, (0, max(0, max_len - len(audio_bh))))
                    audio_mic_padded = np.pad(audio_mic_raw, (0, max(0, max_len - len(audio_mic_raw))))
                    audio_mixed = np.clip((audio_bh_padded + audio_mic_padded) * 0.5, -1.0, 1.0)
                else:
                    audio_mixed = audio_bh

                buffer_bh = []
                buffer_mic = []
                chunk_end = datetime.now()

                print(f"[meeting] Transcribing {format_time(chunk_start)}-{format_time(chunk_end)}...", flush=True)
                text = transcribe_chunk(model, audio_mixed)

                if text:
                    if ENABLE_DIARIZATION and audio_mic_raw is not None:
                        try:
                            segments = _classify_speakers(audio_bh_padded, audio_mic_padded)
                            text = _build_diarized_text(text, segments, len(audio_bh_padded))
                        except Exception:
                            pass
                    line = f"[{format_time(chunk_start)}-{format_time(chunk_end)}] {text}\n"
                    _write_transcript(TRANSCRIPT_FILE, line)
                    print(f"[meeting] -> {text[:80]}...", flush=True)
                else:
                    print("[meeting] (silence)", flush=True)
                    _touch_heartbeat(TRANSCRIPT_FILE)

                last_heartbeat = time.time()
                chunk_start = chunk_end
            else:
                if time.time() - last_heartbeat > 60:
                    _touch_heartbeat(TRANSCRIPT_FILE)
                    last_heartbeat = time.time()

        # Flush remaining
        if buffer_bh:
            audio = np.concatenate(buffer_bh).flatten().astype(np.float32)
            flush_mic_padded = None
            flush_bh_padded = audio
            if has_mic and buffer_mic:
                mic_audio = np.concatenate(buffer_mic).flatten().astype(np.float32)
                max_len = max(len(audio), len(mic_audio))
                flush_bh_padded = np.pad(audio, (0, max(0, max_len - len(audio))))
                flush_mic_padded = np.pad(mic_audio, (0, max(0, max_len - len(mic_audio))))
                audio = np.clip((flush_bh_padded + flush_mic_padded) * 0.5, -1.0, 1.0)
            text = transcribe_chunk(model, audio)
            if text:
                if ENABLE_DIARIZATION and flush_mic_padded is not None:
                    try:
                        segments = _classify_speakers(flush_bh_padded, flush_mic_padded)
                        text = _build_diarized_text(text, segments, len(flush_bh_padded))
                    except Exception:
                        pass
                _write_transcript(TRANSCRIPT_FILE,
                    f"[{format_time(chunk_start)}-{format_time(datetime.now())}] {text}\n")
        print("[meeting] Done.", flush=True)

    except Exception as e:
        print(f"[meeting] FATAL writer error: {e}", file=sys.stderr, flush=True)
        error_event.set()
        shutdown_event.set()


# -- CoreAudio Tap reader (reads PCM from process-audio-tap stdout) -----------

def tap_reader_thread(proc: subprocess.Popen):
    """Read raw PCM (16-bit LE, 16kHz, mono) from tap subprocess stdout."""
    BYTES_PER_SAMPLE = 2
    CHUNK_SAMPLES = SAMPLE_RATE  # 1 second chunks
    CHUNK_BYTES = CHUNK_SAMPLES * BYTES_PER_SAMPLE

    try:
        while not shutdown_event.is_set():
            data = proc.stdout.read(CHUNK_BYTES)
            if not data:
                print("[meeting] Tap process ended", flush=True)
                shutdown_event.set()
                break
            audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            try:
                audio_queue.put_nowait(audio)
            except queue.Full:
                pass  # drop if whisper falls behind
    except Exception as e:
        print(f"[meeting] Tap reader error: {e}", file=sys.stderr, flush=True)
        shutdown_event.set()


# -- Zoom mute detection -------------------------------------------------------

_zoom_muted = False


def _check_zoom_mute() -> bool:
    """Check if Zoom mic is muted via AppleScript menu inspection."""
    try:
        r = subprocess.run(
            ["osascript", "-e", '''tell application "System Events"
    tell process "zoom.us"
        if exists (menu bar item "Meeting" of menu bar 1) then
            if exists (menu item "Unmute Audio" of menu 1 of menu bar item "Meeting" of menu bar 1) then
                return "muted"
            else
                return "unmuted"
            end if
        else
            return "no-meeting"
        end if
    end tell
end tell'''],
            capture_output=True, text=True, timeout=3
        )
        return r.stdout.strip() == "muted"
    except Exception:
        return False


def mute_monitor_thread():
    """Poll Zoom mute status every 2 seconds."""
    global _zoom_muted
    while not shutdown_event.is_set():
        _zoom_muted = _check_zoom_mute()
        shutdown_event.wait(timeout=2.0)


# -- Mic reader (sounddevice, for [You] labels) -------------------------------

def start_mic_stream():
    """Start microphone capture via sounddevice for speaker diarization.
    When Zoom is muted, mic data is replaced with silence to protect privacy."""
    try:
        import sounddevice as sd

        def find_mic() -> int | None:
            for name in ["macbook air micro", "macbook pro micro",
                          "built-in micro", "internal micro", "microphone"]:
                for i, d in enumerate(sd.query_devices()):
                    if d["max_input_channels"] > 0 and name in d["name"].lower():
                        return i
            default = sd.default.device[0]
            if default is not None:
                return default
            return None

        mic = find_mic()
        if mic is None:
            print("[meeting] No microphone found — recording remote audio only", file=sys.stderr)
            return None

        def mic_callback(indata, frames, time_info, status):
            if status:
                print(f"[mic] {status}", file=sys.stderr)
            # Privacy: when Zoom mic is muted, send silence instead of real mic audio
            # This prevents private conversations from being transcribed
            if _zoom_muted:
                try:
                    mic_queue.put_nowait(np.zeros_like(indata))
                except queue.Full:
                    pass
                return
            try:
                mic_queue.put_nowait(indata.copy())
            except queue.Full:
                pass

        stream = sd.InputStream(
            device=mic, samplerate=SAMPLE_RATE, channels=1,
            dtype="float32", callback=mic_callback, blocksize=SAMPLE_RATE,
        )
        stream.start()
        print(f"[meeting] Microphone: [{mic}] {sd.query_devices(mic)['name']}", flush=True)
        return stream

    except Exception as e:
        print(f"[meeting] Mic init failed: {e}", file=sys.stderr)
        return None


# -- Main --------------------------------------------------------------------

def main():
    TRANSCRIPT_FILE.parent.mkdir(parents=True, exist_ok=True)

    now = datetime.now()
    with open(TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\nMeeting started: {now.strftime('%Y-%m-%d %H:%M')}\n{'='*60}\n")

    # Determine capture mode
    use_tap = (CAPTURE_MODE == "coreaudio" and AUDIO_TAP_BIN.exists())

    if use_tap:
        print(f"[meeting] CoreAudio Tap mode — capturing {BUNDLE_ID}", flush=True)

        # Start tap subprocess
        tap_proc = subprocess.Popen(
            [str(AUDIO_TAP_BIN), "--bundle-id", BUNDLE_ID],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Stream tap stderr to our stderr
        def tap_stderr():
            for line in tap_proc.stderr:
                print(f"[tap] {line.decode(errors='replace').rstrip()}", file=sys.stderr, flush=True)
        threading.Thread(target=tap_stderr, daemon=True).start()

        # Start tap reader thread (fills audio_queue)
        tap_thread = threading.Thread(target=tap_reader_thread, args=(tap_proc,), daemon=True)
        tap_thread.start()

    else:
        print("[meeting] Legacy mode — using BlackHole + sounddevice", flush=True)
        import sounddevice as sd

        def find_device(patterns):
            for i, d in enumerate(sd.query_devices()):
                if d["max_input_channels"] == 0:
                    continue
                for p in patterns:
                    if p.lower() in d["name"].lower():
                        return i
            return None

        blackhole = find_device(["blackhole 2ch", "blackhole"])
        if blackhole is None:
            print("[meeting] BlackHole not found!", file=sys.stderr)
            sys.exit(1)

        def bh_callback(indata, frames, time_info, status):
            try:
                audio_queue.put_nowait(indata.copy())
            except queue.Full:
                pass

        bh_stream = sd.InputStream(
            device=blackhole, samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="float32", callback=bh_callback, blocksize=SAMPLE_RATE,
        )
        bh_stream.start()
        print(f"[meeting] BlackHole: [{blackhole}] {sd.query_devices(blackhole)['name']}", flush=True)
        tap_proc = None

    # Start mic (for speaker labels)
    mic_stream = None
    has_mic = False
    if ENABLE_DIARIZATION:
        mic_stream = start_mic_stream()
        has_mic = mic_stream is not None

    # Start Zoom mute monitor (privacy: don't record mic when muted)
    if has_mic:
        threading.Thread(target=mute_monitor_thread, daemon=True).start()
        print("[meeting] Zoom mute monitor active — mic silenced when muted", flush=True)

    # Load Whisper model
    from faster_whisper import WhisperModel
    print(f"[meeting] Loading Whisper model '{MODEL_SIZE}'...", flush=True)
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    print("[meeting] Model ready.", flush=True)

    def handle_signal(sig, frame):
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Start writer thread
    wt = threading.Thread(target=writer_thread, args=(model, has_mic), daemon=False)
    wt.start()

    print("[meeting] Recording...", flush=True)
    try:
        while not shutdown_event.is_set() and not error_event.is_set():
            shutdown_event.wait(timeout=0.5)
    finally:
        shutdown_event.set()

        # Stop tap process
        if tap_proc and tap_proc.poll() is None:
            tap_proc.terminate()
            try:
                tap_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                tap_proc.kill()

        # Stop mic stream
        if mic_stream:
            try:
                mic_stream.stop()
                mic_stream.close()
            except Exception:
                pass

        # Stop legacy blackhole stream
        if not use_tap and 'bh_stream' in dir():
            try:
                bh_stream.stop()
                bh_stream.close()
            except Exception:
                pass

        wt.join(timeout=60)

    if error_event.is_set():
        print("[meeting] Exiting due to writer error", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
