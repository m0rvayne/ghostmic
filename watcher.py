#!/usr/bin/env python3
"""
Meeting watcher daemon.
Detects active Zoom meetings and automatically starts/stops audio capture.

Detection methods (in order of priority):
  1. Zoom CptHost process — only present during active meeting
  2. Zoom Local API (port 19421) — Zoom opens it only during a meeting
"""
import fcntl
import json
import logging
import os
import sys
import time
import signal
import socket
import subprocess
import threading
from dataclasses import dataclass, field
from enum import Enum, auto
from logging.handlers import RotatingFileHandler
from datetime import datetime
from pathlib import Path
from typing import Optional


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class WatcherConfig:
    install_dir: Path
    transcripts_dir: Path
    capture_script: Path
    python_path: Path
    pid_file: Path
    log_file: Path
    symlink_path: Path
    poll_interval: float = 5.0
    grace_period: float = 15.0
    max_rapid_crashes: int = 3
    crash_window: float = 60.0
    status_file: Optional[Path] = None
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 3


def _load_user_config(install_dir: Path) -> dict:
    """Load user config from config.json (written by menu bar settings)."""
    config_file = install_dir / "config.json"
    if config_file.exists():
        try:
            return json.loads(config_file.read_text())
        except Exception:
            pass
    return {}


def default_config() -> WatcherConfig:
    install_dir = Path(__file__).parent
    user = _load_user_config(install_dir)

    # User can override transcripts path via menu bar settings
    transcripts_path = user.get("transcripts_path", "")
    transcripts = Path(transcripts_path) if transcripts_path else install_dir / "transcripts"

    return WatcherConfig(
        install_dir=install_dir,
        transcripts_dir=transcripts,
        capture_script=install_dir / "capture.py",
        python_path=install_dir / ".venv/bin/python3",
        pid_file=install_dir / "watcher.pid",
        log_file=install_dir / "watcher.log",
        symlink_path=transcripts / "meeting_transcript.txt",
        status_file=install_dir / "watcher-status.json",
    )


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------

class State(Enum):
    IDLE = auto()
    RECORDING = auto()
    PAUSED = auto()
    GRACE_PERIOD = auto()
    BACKOFF = auto()


@dataclass
class WatcherContext:
    config: WatcherConfig
    state: State = State.IDLE
    capture_process: Optional[subprocess.Popen] = None
    current_transcript: Optional[Path] = None
    grace_start: Optional[float] = None
    crash_times: list[float] = field(default_factory=list)
    backoff_until: Optional[float] = None
    running: bool = True
    whisper_server_process: Optional[subprocess.Popen] = None
    _whisper_log_file: Optional[object] = None


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

logger = logging.getLogger("watcher")


def setup_logging(config: WatcherConfig):
    logger.setLevel(logging.INFO)

    fh = RotatingFileHandler(
        config.log_file,
        maxBytes=config.log_max_bytes,
        backupCount=config.log_backup_count,
    )
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)

    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter(
        "[watcher %(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    logger.addHandler(sh)


# --------------------------------------------------------------------------
# Single instance guard
# --------------------------------------------------------------------------

_lock_fd = None


def acquire_lock(config: WatcherConfig):
    global _lock_fd
    config.pid_file.parent.mkdir(parents=True, exist_ok=True)
    _lock_fd = open(config.pid_file, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"Another watcher is already running (pidfile: {config.pid_file})", file=sys.stderr)
        sys.exit(1)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()


# --------------------------------------------------------------------------
# Preflight checks
# --------------------------------------------------------------------------

def preflight(config: WatcherConfig):
    errors = []
    if not config.capture_script.exists():
        errors.append(f"capture.py not found: {config.capture_script}")
    if not config.python_path.exists():
        errors.append(f"Python venv not found: {config.python_path}")
    if errors:
        for e in errors:
            logger.error(f"FATAL: {e}")
        sys.exit(1)


def kill_orphan_capture(config: WatcherConfig):
    try:
        result = subprocess.run(
            ["pgrep", "-f", f"python.*{config.capture_script.name}"],
            capture_output=True, text=True
        )
        if result.returncode == 0:
            for pid_str in result.stdout.strip().split("\n"):
                pid_str = pid_str.strip()
                if pid_str and pid_str != str(os.getpid()):
                    logger.info(f"Killing orphan capture process (pid {pid_str})")
                    os.kill(int(pid_str), signal.SIGTERM)
    except Exception:
        pass


# --------------------------------------------------------------------------
# Meeting Detection (Zoom + Google Meet)
# --------------------------------------------------------------------------

def zoom_cpthost_running() -> bool:
    try:
        r = subprocess.run(["pgrep", "-x", "CptHost"], capture_output=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False


def zoom_local_api_active() -> bool:
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
# Status file (for menu bar indicator)
# --------------------------------------------------------------------------

def write_status(config: WatcherConfig, state: str, transcript: str = None,
                  backoff_until: float = None):
    """Write status JSON for the menu bar indicator to read."""
    data = {"state": state, "timestamp": time.time()}
    if transcript:
        data["transcript"] = transcript
    if backoff_until is not None:
        data["backoff_until"] = backoff_until
    try:
        path = config.status_file or (config.install_dir / "watcher-status.json")
        path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _read_persisted_backoff(config: WatcherConfig) -> float | None:
    """Read backoff_until from status file (wall clock). Returns None if expired or absent."""
    try:
        path = config.status_file or (config.install_dir / "watcher-status.json")
        data = json.loads(path.read_text())
        backoff = data.get("backoff_until")
        if backoff and backoff > time.time():
            return backoff
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------
# Symlink management
# --------------------------------------------------------------------------

def update_symlink(config: WatcherConfig, target: Path):
    """Atomically replace the current transcript symlink."""
    tmp_link = config.symlink_path.with_suffix(".tmp")
    tmp_link.unlink(missing_ok=True)
    tmp_link.symlink_to(target)
    tmp_link.rename(config.symlink_path)


# --------------------------------------------------------------------------
# Whisper-server lifecycle
# --------------------------------------------------------------------------

WHISPER_SERVER_PORT = 8178


def _whisper_server_healthy() -> bool:
    """Check if whisper-server is responding."""
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{WHISPER_SERVER_PORT}/health", method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception:
        return False


def start_whisper_server(ctx: WatcherContext):
    """Start whisper-server if not already running. Model stays in memory."""
    if _whisper_server_healthy():
        logger.info("whisper-server already running")
        return

    # Find whisper-server binary
    try:
        r = subprocess.run(["which", "whisper-server"], capture_output=True, text=True)
        if r.returncode != 0:
            logger.warning("whisper-server not found, using whisper-cli (slower)")
            return
    except Exception:
        return

    server_bin = r.stdout.strip()
    user_cfg = _load_user_config(ctx.config.install_dir)
    model_path = os.environ.get("WHISPER_MODEL_PATH",
        str(ctx.config.install_dir / "models" / "ggml-large-v3-turbo.bin"))
    language = user_cfg.get("language", os.environ.get("LANGUAGE", "ru"))

    cmd = [
        server_bin,
        "-m", model_path,
        "--host", "127.0.0.1",
        "--port", str(WHISPER_SERVER_PORT),
        "-t", "4",
        "--convert",
    ]
    if language and language != "auto":
        cmd.extend(["-l", language])

    log_path = ctx.config.install_dir / "whisper-server.log"
    ctx._whisper_log_file = open(log_path, "a")

    ctx.whisper_server_process = subprocess.Popen(
        cmd, stdout=ctx._whisper_log_file, stderr=ctx._whisper_log_file,
    )
    logger.info(f"whisper-server started (PID {ctx.whisper_server_process.pid}, port {WHISPER_SERVER_PORT})")

    # Wait for server to be ready (model loading takes a few seconds)
    for _ in range(30):  # up to 30s
        time.sleep(1)
        if _whisper_server_healthy():
            logger.info("whisper-server ready (model loaded)")
            return
        if ctx.whisper_server_process.poll() is not None:
            logger.error(f"whisper-server exited with code {ctx.whisper_server_process.returncode}")
            ctx.whisper_server_process = None
            return
    logger.warning("whisper-server did not become healthy in 30s")


def stop_whisper_server(ctx: WatcherContext):
    """Stop whisper-server if we started it."""
    if ctx.whisper_server_process and ctx.whisper_server_process.poll() is None:
        ctx.whisper_server_process.terminate()
        try:
            ctx.whisper_server_process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            ctx.whisper_server_process.kill()
        logger.info("whisper-server stopped")
    ctx.whisper_server_process = None
    if ctx._whisper_log_file:
        try:
            ctx._whisper_log_file.close()
        except Exception:
            pass
        ctx._whisper_log_file = None


# --------------------------------------------------------------------------
# Capture lifecycle
# --------------------------------------------------------------------------

def start_capture(ctx: WatcherContext):
    config = ctx.config

    # Crash rate limiting
    now = time.monotonic()
    ctx.crash_times = [t for t in ctx.crash_times if now - t < config.crash_window]
    if len(ctx.crash_times) >= config.max_rapid_crashes:
        logger.warning(f"capture.py crashed {config.max_rapid_crashes} times in {config.crash_window}s — backing off 60s")
        ctx.crash_times.clear()
        ctx.state = State.BACKOFF
        ctx.backoff_until = now + 60.0
        write_status(config, "BACKOFF", backoff_until=time.time() + 60.0)
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    ctx.current_transcript = config.transcripts_dir / f"{timestamp}.txt"
    ctx.current_transcript.touch()

    update_symlink(config, ctx.current_transcript)
    write_status(config, "RECORDING", ctx.current_transcript.name)

    # Start whisper-server if not running (model stays in memory)
    start_whisper_server(ctx)

    env = os.environ.copy()
    env["TRANSCRIPT_FILE"] = str(ctx.current_transcript)
    env["WHISPER_SERVER_PORT"] = str(WHISPER_SERVER_PORT)

    # Read user config for whisper model and diarization
    user_cfg = _load_user_config(config.install_dir)
    if user_cfg.get("whisper_model"):
        env["WHISPER_MODEL"] = user_cfg["whisper_model"]
    if user_cfg.get("diarization"):
        env["DIARIZATION"] = user_cfg["diarization"]
    if user_cfg.get("language"):
        env["LANGUAGE"] = user_cfg["language"]

    # CoreAudio Tap mode — pass bundle ID
    env.setdefault("CAPTURE_MODE", "coreaudio")
    env.setdefault("BUNDLE_ID", "us.zoom.xos")

    # Env vars override config (for manual testing)
    for k in ("PASSTHROUGH", "WHISPER_MODEL", "DIARIZATION", "LANGUAGE", "CAPTURE_MODE", "BUNDLE_ID"):
        if k in os.environ:
            env[k] = os.environ[k]

    ctx.capture_process = subprocess.Popen(
        [str(config.python_path), str(config.capture_script)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    def stream_output():
        try:
            for line in ctx.capture_process.stdout:
                logger.info(f"[capture] {line.decode(errors='replace').rstrip()}")
        except Exception:
            pass

    threading.Thread(target=stream_output, daemon=True).start()
    ctx.state = State.RECORDING
    logger.info(f"Recording started -> {ctx.current_transcript.name}")


def stop_capture(ctx: WatcherContext):
    if ctx.capture_process and ctx.capture_process.poll() is None:
        ctx.capture_process.send_signal(signal.SIGTERM)
        try:
            ctx.capture_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            ctx.capture_process.kill()

    if ctx.capture_process and ctx.capture_process.returncode and ctx.capture_process.returncode != 0:
        ctx.crash_times.append(time.monotonic())

    saved_name = ctx.current_transcript.name if ctx.current_transcript else "unknown"
    ctx.capture_process = None
    ctx.current_transcript = None
    write_status(ctx.config, "IDLE")
    ctx.state = State.IDLE
    logger.info(f"Recording stopped. Transcript: {saved_name}")


def notify(title: str, message: str):
    safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{safe_msg}" with title "{safe_title}"'],
            capture_output=True, timeout=5
        )
    except Exception:
        pass


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

def _read_control(config: WatcherConfig) -> str | None:
    """Read and consume a control command from the menu bar app."""
    control_path = config.install_dir / "watcher-control.json"
    if not control_path.exists():
        return None
    try:
        data = json.loads(control_path.read_text())
        control_path.unlink()
        return data.get("action")
    except Exception:
        return None


def tick(ctx: WatcherContext):
    """Single iteration of the watcher state machine."""
    config = ctx.config

    # Handle backoff: return early until timer expires
    if ctx.state == State.BACKOFF:
        if time.monotonic() < ctx.backoff_until:
            return
        logger.info("Backoff period ended — resuming normal operation")
        ctx.state = State.IDLE
        ctx.backoff_until = None

    # Check for menu bar control commands
    control = _read_control(config)
    if control:
        is_active = ctx.capture_process is not None and ctx.capture_process.poll() is None
        if control == "pause" and is_active and ctx.state == State.RECORDING:
            ctx.capture_process.send_signal(signal.SIGSTOP)
            ctx.state = State.PAUSED
            write_status(config, "PAUSED")
            logger.info("Recording paused (user request)")
        elif control == "resume" and ctx.state == State.PAUSED:
            ctx.capture_process.send_signal(signal.SIGCONT)
            ctx.state = State.RECORDING
            name = ctx.current_transcript.name if ctx.current_transcript else "unknown"
            write_status(config, "RECORDING", name)
            logger.info("Recording resumed (user request)")
        elif control == "end" and is_active:
            if ctx.state == State.PAUSED:
                ctx.capture_process.send_signal(signal.SIGCONT)
            logger.info("Recording ended (user request)")
            stop_capture(ctx)
            return

    try:
        in_conf = is_in_conference()
    except Exception as e:
        logger.warning(f"Detection error: {e}")
        return

    is_recording = (
        ctx.capture_process is not None
        and ctx.capture_process.poll() is None
    )

    # Detect crashed capture process (exited but not cleaned up via stop_capture)
    if ctx.capture_process is not None and not is_recording:
        logger.warning(f"Capture process died (exit code {ctx.capture_process.returncode})")
        stop_capture(ctx)  # records crash_time for backoff

    if in_conf:
        if ctx.state == State.GRACE_PERIOD and is_recording:
            # Conference came back during grace period — this could be a reconnect
            # (same meeting) or a new meeting. We can't tell the difference, so
            # stop the old recording and start fresh to be safe.
            logger.info("New meeting detected during grace period — restarting capture")
            stop_capture(ctx)
            ctx.grace_start = None
            start_capture(ctx)
        elif not is_recording and ctx.state != State.PAUSED:
            ctx.grace_start = None
            logger.info("Meeting detected — starting capture")
            start_capture(ctx)
        else:
            ctx.grace_start = None
    else:
        if is_recording or ctx.state == State.PAUSED:
            if ctx.grace_start is None:
                ctx.grace_start = time.monotonic()
                ctx.state = State.GRACE_PERIOD
                logger.info(f"Meeting ended — waiting {ctx.config.grace_period}s grace period...")
            elif time.monotonic() - ctx.grace_start >= ctx.config.grace_period:
                logger.info("Grace period elapsed — stopping capture")
                if ctx.state == State.PAUSED and ctx.capture_process:
                    ctx.capture_process.send_signal(signal.SIGCONT)
                stop_capture(ctx)
                ctx.grace_start = None


def main():
    config = default_config()
    config.transcripts_dir.mkdir(exist_ok=True)
    setup_logging(config)
    acquire_lock(config)
    preflight(config)
    kill_orphan_capture(config)

    ctx = WatcherContext(config=config)

    # Restore backoff state from previous run (prevents crash loop on restart)
    persisted_backoff = _read_persisted_backoff(config)
    if persisted_backoff:
        remaining = persisted_backoff - time.time()
        ctx.state = State.BACKOFF
        ctx.backoff_until = time.monotonic() + remaining
        logger.info(f"Resuming backoff from previous run ({remaining:.0f}s remaining)")

    def handle_signal(sig, frame):
        ctx.running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    write_status(config, "IDLE")
    logger.info("Watcher started. Monitoring for Zoom meetings...")

    while ctx.running:
        tick(ctx)
        time.sleep(config.poll_interval)

    logger.info("Shutting down watcher...")
    if ctx.capture_process and ctx.capture_process.poll() is None:
        stop_capture(ctx)
    stop_whisper_server(ctx)
    write_status(config, "IDLE")


if __name__ == "__main__":
    main()
