#!/usr/bin/env python3
"""
Meeting watcher daemon.
Detects active Zoom meetings and automatically starts/stops audio capture.

Detection methods (in order of priority):
  1. Zoom CptHost process — only present during active meeting
  2. Zoom Local API (port 19421) — Zoom opens it only during a meeting
"""
import fcntl
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

PID_FILE = Path(__file__).parent / "watcher.pid"
MAX_RAPID_CRASHES = 3
CRASH_WINDOW = 60  # seconds


def log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[watcher {ts}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Single instance guard
# --------------------------------------------------------------------------

_lock_fd = None

def acquire_lock():
    """Ensure only one watcher instance runs at a time."""
    global _lock_fd
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    _lock_fd = open(PID_FILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"Another watcher is already running (pidfile: {PID_FILE})", file=sys.stderr)
        sys.exit(1)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()


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


def kill_orphan_capture():
    """Kill any leftover capture.py from a previous watcher crash."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"python.*{CAPTURE_SCRIPT.name}"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            for pid in result.stdout.strip().split("\n"):
                pid = pid.strip()
                if pid and pid != str(os.getpid()):
                    log(f"Killing orphan capture process (pid {pid})")
                    os.kill(int(pid), signal.SIGTERM)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Zoom Detection
# --------------------------------------------------------------------------

def zoom_cpthost_running() -> bool:
    """CptHost is Zoom's in-meeting subprocess — absent when not in a meeting."""
    try:
        r = subprocess.run(["pgrep", "-x", "CptHost"], capture_output=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def zoom_local_api_active() -> bool:
    """Zoom opens a local REST API on port 19421 only during active meetings."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            return sock.connect_ex(("127.0.0.1", 19421)) == 0
    except Exception:
        return False


def is_in_conference() -> bool:
    """Return True if user is currently in a Zoom meeting."""
    return zoom_cpthost_running() or zoom_local_api_active()


# --------------------------------------------------------------------------
# Capture lifecycle
# --------------------------------------------------------------------------

capture_process = None
conference_end_time = None
current_transcript_path = None
_crash_times: list[float] = []


def _update_symlink(target: Path):
    """Atomically replace the current transcript symlink."""
    tmp_link = CURRENT_TRANSCRIPT_SYMLINK.with_suffix(".tmp")
    tmp_link.unlink(missing_ok=True)
    tmp_link.symlink_to(target)
    tmp_link.rename(CURRENT_TRANSCRIPT_SYMLINK)


def start_capture():
    global capture_process, current_transcript_path

    # Crash rate limiting — don't spin if capture keeps dying
    now = time.time()
    _crash_times[:] = [t for t in _crash_times if now - t < CRASH_WINDOW]
    if len(_crash_times) >= MAX_RAPID_CRASHES:
        log(f"capture.py crashed {MAX_RAPID_CRASHES} times in {CRASH_WINDOW}s — backing off 60s")
        time.sleep(60)
        _crash_times.clear()

    # Always start a fresh transcript for a new meeting
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    current_transcript_path = TRANSCRIPTS_DIR / f"{timestamp}.txt"
    current_transcript_path.touch()

    _update_symlink(current_transcript_path)

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
        try:
            for line in capture_process.stdout:
                print(f"[capture] {line.decode(errors='replace').rstrip()}", flush=True)
        except Exception:
            pass

    threading.Thread(target=stream_output, daemon=True).start()
    log(f"Recording started -> {current_transcript_path.name}")


def stop_capture():
    global capture_process, current_transcript_path

    if capture_process and capture_process.poll() is None:
        capture_process.send_signal(signal.SIGTERM)
        try:
            capture_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            capture_process.kill()

    if capture_process and capture_process.returncode and capture_process.returncode != 0:
        _crash_times.append(time.time())

    saved_name = current_transcript_path.name if current_transcript_path else "unknown"
    capture_process = None
    current_transcript_path = None  # C1 fix: reset so next meeting gets a fresh file
    log(f"Recording stopped. Transcript: {saved_name}")
    notify("Meeting recording stopped", f"Saved: {saved_name}")


def notify(title: str, message: str):
    """macOS notification with proper escaping."""
    safe_title = title.replace('"', '\\"').replace("\\", "\\\\")
    safe_msg = message.replace('"', '\\"').replace("\\", "\\\\")
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_msg}" with title "{safe_title}"'],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def main():
    global conference_end_time

    TRANSCRIPTS_DIR.mkdir(exist_ok=True)
    acquire_lock()
    preflight()
    kill_orphan_capture()

    running = True

    def handle_signal(sig, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    log("Watcher started. Monitoring for Zoom meetings...")

    while running:
        try:
            in_conf = is_in_conference()
        except Exception as e:
            log(f"Detection error: {e}")
            time.sleep(POLL_INTERVAL)
            continue

        is_recording = capture_process is not None and capture_process.poll() is None

        if in_conf:
            # If we're in grace period and meeting comes back — it's likely a reconnect
            if conference_end_time is not None and is_recording:
                log("Meeting reconnected during grace period")
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

        time.sleep(POLL_INTERVAL)

    # Clean shutdown
    log("Shutting down watcher...")
    if capture_process and capture_process.poll() is None:
        stop_capture()


if __name__ == "__main__":
    main()
