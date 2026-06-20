#!/usr/bin/env python3
"""
Meeting watcher daemon.
Detects active Zoom meetings and automatically starts/stops audio capture.

Detection methods (in order of priority):
  1. Zoom CptHost process — only present during active meeting
  2. Zoom Local API (port 19421) — Zoom opens it only during a meeting
"""
import os
import sys
import time
import signal
import socket
import subprocess
import threading
from datetime import datetime
from pathlib import Path

TRANSCRIPTS_DIR = Path(__file__).parent / "transcripts"
CURRENT_TRANSCRIPT_SYMLINK = TRANSCRIPTS_DIR / "meeting_transcript.txt"
CAPTURE_SCRIPT = Path(__file__).parent / "capture.py"
PYTHON = Path(__file__).parent / ".venv/bin/python3"
POLL_INTERVAL = 5   # seconds
GRACE_PERIOD = 15   # seconds after Zoom signals meeting end before stopping
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB — truncate log when exceeded
LOG_FILE = Path(__file__).parent / "watcher.log"

TRANSCRIPTS_DIR.mkdir(exist_ok=True)

capture_process = None
conference_end_time = None
current_transcript_path = None
running = True


# --------------------------------------------------------------------------
# Preflight checks
# --------------------------------------------------------------------------

def preflight():
    """Verify required files exist before entering the main loop."""
    errors = []
    if not CAPTURE_SCRIPT.exists():
        errors.append(f"capture.py not found: {CAPTURE_SCRIPT}")
    if not PYTHON.exists():
        errors.append(f"Python venv not found: {PYTHON}")
    if errors:
        for e in errors:
            log(f"FATAL: {e}")
        sys.exit(1)


# --------------------------------------------------------------------------
# Log rotation
# --------------------------------------------------------------------------

def rotate_log_if_needed():
    """Truncate log file if it exceeds LOG_MAX_BYTES."""
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > LOG_MAX_BYTES:
            lines = LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
            # Keep last 500 lines
            keep = lines[-500:] if len(lines) > 500 else lines
            LOG_FILE.write_text("\n".join(keep) + "\n", encoding="utf-8")
            log("Log rotated (exceeded 5 MB)")
    except Exception:
        pass


# --------------------------------------------------------------------------
# Zoom Detection
# --------------------------------------------------------------------------

def zoom_cpthost_running() -> bool:
    """CptHost is Zoom's in-meeting subprocess — absent when not in a meeting."""
    r = subprocess.run(["pgrep", "-x", "CptHost"], capture_output=True)
    return r.returncode == 0


def zoom_local_api_active() -> bool:
    """
    Zoom opens a local REST API on port 19421 only during active meetings.
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.3)
        result = sock.connect_ex(("127.0.0.1", 19421))
        sock.close()
        return result == 0
    except Exception:
        return False


def is_in_conference() -> bool:
    """Return True if user is currently in a Zoom meeting."""
    return zoom_cpthost_running() or zoom_local_api_active()


# --------------------------------------------------------------------------
# Capture lifecycle
# --------------------------------------------------------------------------

def start_capture():
    global capture_process, current_transcript_path

    if current_transcript_path and current_transcript_path.exists() and current_transcript_path.stat().st_size > 0:
        log(f"Resuming transcript: {current_transcript_path.name}")
    else:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M")
        current_transcript_path = TRANSCRIPTS_DIR / f"{timestamp}.txt"

    # Point symlink to current transcript
    if CURRENT_TRANSCRIPT_SYMLINK.is_symlink() or CURRENT_TRANSCRIPT_SYMLINK.exists():
        CURRENT_TRANSCRIPT_SYMLINK.unlink()
    CURRENT_TRANSCRIPT_SYMLINK.symlink_to(current_transcript_path)

    notify("Meeting recording started", f"Transcript: {current_transcript_path.name}")

    env = os.environ.copy()
    env["TRANSCRIPT_FILE"] = str(current_transcript_path)
    for k in ("PASSTHROUGH", "WHISPER_MODEL"):
        if k in os.environ:
            env[k] = os.environ[k]

    capture_process = subprocess.Popen(
        [str(PYTHON), str(CAPTURE_SCRIPT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def stream_output():
        for line in capture_process.stdout:
            print(f"[capture] {line.decode().rstrip()}", flush=True)

    threading.Thread(target=stream_output, daemon=True).start()
    log(f"Recording started -> {current_transcript_path.name}")


def stop_capture():
    global capture_process

    if capture_process and capture_process.poll() is None:
        capture_process.send_signal(signal.SIGTERM)
        try:
            capture_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            capture_process.kill()

    capture_process = None
    log(f"Recording stopped. Transcript: {current_transcript_path}")
    notify("Meeting recording stopped", f"Saved: {current_transcript_path.name}")


def notify(title: str, message: str):
    """macOS notification."""
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[watcher {ts}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def handle_signal(sig, frame):
    global running
    log("Shutting down watcher...")
    running = False
    if capture_process and capture_process.poll() is None:
        stop_capture()
    sys.exit(0)


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)

preflight()
log("Watcher started. Monitoring for Zoom meetings...")

poll_count = 0
while running:
    in_conf = is_in_conference()
    is_recording = capture_process is not None and capture_process.poll() is None

    if in_conf:
        conference_end_time = None
        if not is_recording:
            log("Zoom meeting detected — starting capture")
            start_capture()

    else:
        if is_recording:
            if conference_end_time is None:
                conference_end_time = time.time()
                log(f"Zoom meeting ended — waiting {GRACE_PERIOD}s grace period...")
            elif time.time() - conference_end_time >= GRACE_PERIOD:
                log("Grace period elapsed — stopping capture")
                stop_capture()
                conference_end_time = None

    # Rotate log every ~100 polls (~8 min)
    poll_count += 1
    if poll_count % 100 == 0:
        rotate_log_if_needed()

    time.sleep(POLL_INTERVAL)
