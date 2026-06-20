#!/usr/bin/env python3
"""
Meeting transcript capture with built-in audio passthrough.

Architecture:
  Zoom -> BlackHole 2ch -> capture.py -> Whisper (transcription)
                                       -> Default output (speakers/headphones)
"""
import sys
import os
import queue
import signal
import time
import threading
from datetime import datetime
from pathlib import Path

import numpy as np
import sounddevice as sd
from faster_whisper import WhisperModel

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SECONDS = 30
MAX_QUEUE_CHUNKS = 8  # ~4 min of audio — prevents OOM if Whisper falls behind
_DEFAULT_TRANSCRIPT = Path(__file__).parent / "transcripts" / "meeting_transcript.txt"
TRANSCRIPT_FILE = Path(os.environ.get("TRANSCRIPT_FILE", _DEFAULT_TRANSCRIPT))
MODEL_SIZE = os.environ.get("WHISPER_MODEL", "small")
ENABLE_PASSTHROUGH = os.environ.get("PASSTHROUGH", "0") == "1"
PASSTHROUGH_RATE = 48000

# Bounded queues to prevent OOM
audio_queue = queue.Queue(maxsize=MAX_QUEUE_CHUNKS * SAMPLE_RATE * CHUNK_SECONDS // SAMPLE_RATE)
mic_queue = queue.Queue(maxsize=MAX_QUEUE_CHUNKS * SAMPLE_RATE * CHUNK_SECONDS // SAMPLE_RATE)
passthrough_queue = queue.Queue(maxsize=200)
shutdown_event = threading.Event()
error_event = threading.Event()  # set by writer_thread on fatal error


# -- Auto-detect audio devices ------------------------------------------------

def find_device(name_patterns: list[str], input_only: bool = True) -> int | None:
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if input_only and d["max_input_channels"] == 0:
            continue
        name = d["name"].lower()
        for pattern in name_patterns:
            if pattern.lower() in name:
                return i
    return None


def find_output_device() -> int | None:
    """Find current default output device (NOT BlackHole)."""
    default_out = sd.default.device[1]
    if default_out is not None:
        dev = sd.query_devices(default_out)
        if "blackhole" not in dev["name"].lower() and dev["max_output_channels"] > 0:
            return default_out
    for i, d in enumerate(sd.query_devices()):
        if d["max_output_channels"] > 0 and "blackhole" not in d["name"].lower():
            return i
    return None


def detect_devices() -> tuple[int, int | None]:
    blackhole = find_device(["blackhole 2ch", "blackhole"], input_only=True)
    if blackhole is None:
        print("[meeting] BlackHole not found! Install: brew install blackhole-2ch", file=sys.stderr)
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                print(f"  [{i}] {d['name']}", file=sys.stderr)
        sys.exit(1)

    mic = find_device([
        "macbook air micro", "macbook pro micro",
        "built-in micro", "internal micro", "microphone",
    ], input_only=True)

    if mic is None:
        default_input = sd.default.device[0]
        if default_input is not None and default_input != blackhole:
            mic = default_input

    if mic is None:
        print("[meeting] No microphone found — recording system audio only", file=sys.stderr)

    bh_name = sd.query_devices(blackhole)["name"]
    print(f"[meeting] System audio: [{blackhole}] {bh_name}", flush=True)
    if mic is not None:
        print(f"[meeting] Microphone:   [{mic}] {sd.query_devices(mic)['name']}", flush=True)
    return blackhole, mic


# -- Audio callbacks ----------------------------------------------------------

def blackhole_callback(indata, frames, time_info, status):
    if status and "input" not in str(status).lower():
        print(f"[blackhole] {status}", file=sys.stderr)
    try:
        audio_queue.put_nowait(indata.copy())
    except queue.Full:
        pass  # drop oldest data rather than OOM
    if ENABLE_PASSTHROUGH:
        try:
            passthrough_queue.put_nowait(indata.copy())
        except queue.Full:
            pass


def mic_callback(indata, frames, time_info, status):
    if status:
        print(f"[mic] {status}", file=sys.stderr)
    try:
        mic_queue.put_nowait(indata.copy())
    except queue.Full:
        pass


# -- Passthrough thread -------------------------------------------------------

def _resample_linear(data: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample audio using linear interpolation."""
    if src_rate == dst_rate:
        return data
    duration = len(data) / src_rate
    src_time = np.linspace(0, duration, len(data), endpoint=False)
    dst_samples = int(duration * dst_rate)
    dst_time = np.linspace(0, duration, dst_samples, endpoint=False)
    return np.interp(dst_time, src_time, data).astype(np.float32)


def passthrough_thread():
    current_output = None
    stream = None

    while not shutdown_event.is_set():
        new_output = find_output_device()
        if new_output != current_output:
            if stream is not None:
                try:
                    stream.stop()
                    stream.close()
                except Exception:
                    pass
            current_output = new_output
            if current_output is not None:
                try:
                    dev_info = sd.query_devices(current_output)
                    out_rate = int(dev_info.get("default_samplerate", PASSTHROUGH_RATE))
                    stream = sd.OutputStream(
                        device=current_output, samplerate=out_rate,
                        channels=1, dtype="float32", blocksize=1024,
                    )
                    stream.start()
                    print(f"[passthrough] Audio -> [{current_output}] {dev_info['name']}", flush=True)
                except Exception as e:
                    print(f"[passthrough] Cannot open output: {e}", file=sys.stderr)
                    stream = None

        try:
            chunk = passthrough_queue.get(timeout=0.5)
            if stream is not None:
                data = chunk.flatten()
                if stream.samplerate != SAMPLE_RATE:
                    data = _resample_linear(data, SAMPLE_RATE, int(stream.samplerate))
                try:
                    stream.write(data.reshape(-1, 1))
                except Exception:
                    pass
        except queue.Empty:
            pass

    if stream is not None:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass


# -- Transcription ------------------------------------------------------------

def format_time(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


def transcribe_chunk(model, audio_np: np.ndarray) -> str:
    segments, _ = model.transcribe(
        audio_np, language=None, beam_size=5,
        vad_filter=True, vad_parameters={"min_silence_duration_ms": 500},
    )
    return " ".join(seg.text.strip() for seg in segments).strip()


def _drain_queue(q: queue.Queue) -> list:
    """Drain all available items from a queue without blocking."""
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except queue.Empty:
            break
    return items


def _write_transcript(path: Path, line: str):
    """Write a line to the transcript file with error handling."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    except OSError as e:
        print(f"[meeting] WRITE ERROR: {e}", file=sys.stderr, flush=True)
        raise


def _touch_heartbeat(path: Path):
    """Touch file mtime so server.py knows we're alive even during silence."""
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
            # Drain both queues each iteration to prevent drift
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

                if has_mic and buffer_mic:
                    audio_mic = np.concatenate(buffer_mic).flatten().astype(np.float32)
                    max_len = max(len(audio_bh), len(audio_mic))
                    audio_bh = np.pad(audio_bh, (0, max(0, max_len - len(audio_bh))))
                    audio_mic = np.pad(audio_mic, (0, max(0, max_len - len(audio_mic))))
                    audio_mixed = np.clip((audio_bh + audio_mic) * 0.5, -1.0, 1.0)
                else:
                    audio_mixed = audio_bh

                buffer_bh = []
                buffer_mic = []
                chunk_end = datetime.now()

                print(f"[meeting] Transcribing {format_time(chunk_start)}-{format_time(chunk_end)}...", flush=True)
                text = transcribe_chunk(model, audio_mixed)

                if text:
                    line = f"[{format_time(chunk_start)}-{format_time(chunk_end)}] {text}\n"
                    _write_transcript(TRANSCRIPT_FILE, line)
                    print(f"[meeting] -> {text[:80]}...", flush=True)
                else:
                    print("[meeting] (silence)", flush=True)
                    _touch_heartbeat(TRANSCRIPT_FILE)

                last_heartbeat = time.time()
                chunk_start = chunk_end
            else:
                # Heartbeat during silence — touch file every 60s so server.py
                # doesn't show "recording not active" during quiet periods
                if time.time() - last_heartbeat > 60:
                    _touch_heartbeat(TRANSCRIPT_FILE)
                    last_heartbeat = time.time()

        # Flush remaining buffers (both bh and mic)
        if buffer_bh:
            audio = np.concatenate(buffer_bh).flatten().astype(np.float32)
            if has_mic and buffer_mic:
                mic_audio = np.concatenate(buffer_mic).flatten().astype(np.float32)
                max_len = max(len(audio), len(mic_audio))
                audio = np.pad(audio, (0, max(0, max_len - len(audio))))
                mic_audio = np.pad(mic_audio, (0, max(0, max_len - len(mic_audio))))
                audio = np.clip((audio + mic_audio) * 0.5, -1.0, 1.0)
            text = transcribe_chunk(model, audio)
            if text:
                _write_transcript(TRANSCRIPT_FILE,
                    f"[{format_time(chunk_start)}-{format_time(datetime.now())}] {text}\n")
        print("[meeting] Done.", flush=True)

    except Exception as e:
        print(f"[meeting] FATAL writer error: {e}", file=sys.stderr, flush=True)
        error_event.set()
        shutdown_event.set()


# -- Main --------------------------------------------------------------------

def main():
    blackhole_device, mic_device = detect_devices()
    has_mic = mic_device is not None

    TRANSCRIPT_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Write header and start audio BEFORE loading model to capture early audio
    now = datetime.now()
    with open(TRANSCRIPT_FILE, "a", encoding="utf-8") as f:
        f.write(f"\n{'='*60}\nMeeting started: {now.strftime('%Y-%m-%d %H:%M')}\n{'='*60}\n")

    # Start audio capture FIRST so we buffer audio while model loads
    streams = []
    try:
        streams.append(sd.InputStream(
            device=blackhole_device, samplerate=SAMPLE_RATE, channels=CHANNELS,
            dtype="float32", callback=blackhole_callback, blocksize=SAMPLE_RATE,
        ))
        if has_mic:
            streams.append(sd.InputStream(
                device=mic_device, samplerate=SAMPLE_RATE, channels=1,
                dtype="float32", callback=mic_callback, blocksize=SAMPLE_RATE,
            ))
        for s in streams:
            s.start()
        print("[meeting] Audio streams started, buffering...", flush=True)
    except Exception as e:
        print(f"[meeting] Failed to open audio device: {e}", file=sys.stderr)
        sys.exit(1)

    # Now load model — audio is being buffered in the queues meanwhile
    print(f"[meeting] Loading Whisper model '{MODEL_SIZE}'...", flush=True)
    model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
    print("[meeting] Model ready.", flush=True)

    def handle_signal(sig, frame):
        shutdown_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    if ENABLE_PASSTHROUGH:
        out_dev = find_output_device()
        if out_dev is not None:
            print(f"[meeting] Passthrough: Zoom audio -> {sd.query_devices(out_dev)['name']}", flush=True)
        pt = threading.Thread(target=passthrough_thread, daemon=True)
        pt.start()

    wt = threading.Thread(target=writer_thread, args=(model, has_mic), daemon=False)
    wt.start()

    print("[meeting] Recording...", flush=True)
    try:
        while not shutdown_event.is_set() and not error_event.is_set():
            shutdown_event.wait(timeout=0.5)
    finally:
        shutdown_event.set()
        for s in streams:
            try:
                s.stop()
                s.close()
            except Exception:
                pass
        wt.join(timeout=60)

    if error_event.is_set():
        print("[meeting] Exiting due to writer error", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
