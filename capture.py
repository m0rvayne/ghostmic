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
import re
import signal
import subprocess
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SECONDS = 10
MAX_QUEUE_CHUNKS = 240  # ~4 min of audio in queue items
_DEFAULT_TRANSCRIPT = Path(__file__).parent / "transcripts" / "meeting_transcript.txt"
TRANSCRIPT_FILE = Path(os.environ.get("TRANSCRIPT_FILE", _DEFAULT_TRANSCRIPT))
ENABLE_DIARIZATION = os.environ.get("DIARIZATION", "1") == "1"
LANGUAGE = os.environ.get("LANGUAGE", "auto")  # or a code like "ru" to force one
CAPTURE_MODE = os.environ.get("CAPTURE_MODE", "coreaudio")  # "coreaudio" or "legacy"
BUNDLE_ID = os.environ.get("BUNDLE_ID", "us.zoom.xos")

AUDIO_TAP_BIN = Path(__file__).parent / ".build" / "process-audio-tap"
PARTICIPANTS_BIN = Path(__file__).parent / ".build" / "zoom-participants"
# Off by default — measured net-negative on real chunks. See
# tools/measure_llm_post.py and the note above _llm_output_is_safe.
# Set LLM_POST=1 to opt in.
ENABLE_LLM_POST = os.environ.get("LLM_POST", "0") == "1"
LLM_MODEL = os.environ.get("LLM_MODEL", "mlx-community/Qwen3-0.6B-4bit")

# Bounded queues
audio_queue = queue.Queue(maxsize=MAX_QUEUE_CHUNKS)
mic_queue = queue.Queue(maxsize=MAX_QUEUE_CHUNKS)
shutdown_event = threading.Event()
error_event = threading.Event()


# -- Zoom participant names ---------------------------------------------------

_participants: list[str] = []  # current meeting participants (excluding self)
_participants_lock = threading.Lock()


def _poll_participants():
    """Background thread: poll Zoom participants via Accessibility API."""
    import json as _json
    while not shutdown_event.is_set():
        try:
            r = subprocess.run(
                [str(PARTICIPANTS_BIN)],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                names = _json.loads(r.stdout)
                if isinstance(names, list):
                    with _participants_lock:
                        _participants.clear()
                        _participants.extend(names)
                    if names:
                        print(f"[meeting] Participants: {', '.join(names)}", flush=True)
        except Exception:
            pass
        shutdown_event.wait(timeout=10)


def get_participants() -> list[str]:
    """Get current participant list (thread-safe)."""
    with _participants_lock:
        return list(_participants)


def get_remote_participants() -> list[str]:
    """Get participants excluding self (user config name or common self-markers)."""
    all_p = get_participants()
    # The user is typically marked with "(Me)" or "(Я)" which is stripped by the Swift CLI.
    # For now return all — the user's own name will be labeled [You] via energy diarization.
    return all_p


# Whisper detects the language per request, and on a 20-second chunk of a quiet
# moment it guesses wrong — which is where the English hallucinations in Russian
# meetings came from. Detect once on a chunk worth trusting, then hold it for
# the rest of the call. (This existed before the whisper.cpp migration and was
# lost in it; _detected_language survived as an unused variable.)
_detected_language: str | None = None

_LANGUAGE_CODES = {
    "russian": "ru", "english": "en", "ukrainian": "uk", "german": "de",
    "french": "fr", "spanish": "es", "italian": "it", "portuguese": "pt",
    "dutch": "nl", "polish": "pl", "czech": "cs", "turkish": "tr",
    "arabic": "ar", "hebrew": "he", "hindi": "hi", "japanese": "ja",
    "korean": "ko", "chinese": "zh", "swedish": "sv", "norwegian": "no",
    "danish": "da", "finnish": "fi", "greek": "el", "romanian": "ro",
    "hungarian": "hu", "kazakh": "kk", "serbian": "sr", "bulgarian": "bg",
}


# Detection is not to be taken at its word. The vocabulary prompt is mostly
# English product names, and on a Russian line whisper came back with
# "english" while transcribing perfectly good Russian. Two cross-checks:
# the script actually written has to match the language claimed, and one
# chunk is not enough to decide on.
LANGUAGE_LOCK_VOTES = 3
_CYRILLIC_LANGS = {"ru", "uk", "bg", "sr", "kk", "be", "mk"}
_LATIN_LANGS = {"en", "de", "fr", "es", "it", "pt", "nl", "pl", "cs", "tr",
                "sv", "no", "da", "fi", "ro", "hu"}
_language_votes: list[str] = []


def _dominant_script(text: str) -> str:
    """Which alphabet the text is actually written in."""
    cyrillic = latin = 0
    for ch in text.lower():
        if "а" <= ch <= "я" or ch == "ё":
            cyrillic += 1
        elif "a" <= ch <= "z":
            latin += 1
    if cyrillic == latin == 0:
        return ""
    return "cyrillic" if cyrillic > latin else "latin"


def _script_contradicts(code: str, text: str) -> bool:
    """True when the claimed language cannot be what is written here."""
    expected = ("cyrillic" if code in _CYRILLIC_LANGS
                else "latin" if code in _LATIN_LANGS else "")
    if not expected:
        return False  # scripts we do not reason about — do not block on it
    script = _dominant_script(text)
    return bool(script) and script != expected


def effective_language() -> str:
    """The language code to ask whisper for on the next chunk."""
    if LANGUAGE != "auto":
        return LANGUAGE
    return _detected_language or "auto"


def _maybe_lock_language(result: "Transcription"):
    """Pin the language once a chunk is solid enough to be believed."""
    global _detected_language
    if LANGUAGE != "auto" or _detected_language:
        return
    code = _LANGUAGE_CODES.get((result.language or "").strip().lower())
    if not code:
        return
    if len(result.words) < MIN_WORDS_TO_JUDGE or result.looks_like_noise:
        return  # too thin to draw a conclusion from
    if _script_contradicts(code, result.text):
        return  # "english" over Cyrillic text: believe the text

    _language_votes.append(code)
    del _language_votes[:-LANGUAGE_LOCK_VOTES]
    if len(_language_votes) < LANGUAGE_LOCK_VOTES:
        return
    if len(set(_language_votes)) > 1:
        return  # still changing its mind

    _detected_language = code
    print(f"[meeting] Language detected: {result.language} ({code}) — "
          f"locked for this meeting", flush=True)


# -- Speaker diarization -------------------------------------------------------
#
# Comparing raw channel energies cannot work. The tap carries whatever Zoom
# renders — scaled by system volume and by how loud the far end happens to be —
# while the mic carries input gain and mouth distance. The two have no common
# zero, so a fixed ratio threshold means one thing on headphones at 30% volume
# and something else on speakers at 80%. Turning the volume knob mid-call used
# to move every label in the transcript.
#
# Measure each channel against *its own* noise floor instead. "How many times
# above its own floor is this channel right now" is dimensionless, which makes
# the two sides comparable no matter how either is amplified.

SPEAKER_ACTIVE_MULT = 3.0   # a channel counts as speaking at 3x its own floor
SPEAKER_LEAD_RATIO = 2.5    # one side must lead the other by this much to own the frame
BLEED_CORRELATION = 0.5     # above this the mic frame is the tap coming back
ABS_FLOOR = 1e-5            # keeps a muted (all-zero) channel from dividing by ~0


def _frame_correlation(a: np.ndarray, b: np.ndarray) -> float:
    """Normalised correlation of two frames. 0 when either is flat or degenerate."""
    if a.size == 0 or b.size == 0 or a.size != b.size:
        return 0.0
    a = a - a.mean()
    b = b - b.mean()
    na = float(np.sqrt(np.dot(a, a)))
    nb = float(np.sqrt(np.dot(b, b)))
    if not np.isfinite(na) or not np.isfinite(nb) or na < 1e-12 or nb < 1e-12:
        return 0.0
    corr = float(np.dot(a, b)) / (na * nb)
    return abs(corr) if np.isfinite(corr) else 0.0


def _classify_speakers(audio_bh: np.ndarray, audio_mic: np.ndarray,
                       frame_ms: int = 500,
                       bh_floor: float | None = None,
                       mic_floor: float | None = None) -> list[tuple[str, int, int]]:
    """Label each frame [You], [Remote] or [Both] by lift over each channel's floor.

    Floors come from the rolling estimate when the caller has one; otherwise
    they are taken from the chunk itself, which is enough for a 10–20s window.
    """
    frame_size = int(SAMPLE_RATE * frame_ms / 1000)
    if frame_size <= 0:
        return []

    if bh_floor is None:
        bh_floor = _estimate_floor(audio_bh, frame_ms)
    if mic_floor is None:
        mic_floor = _estimate_floor(audio_mic, frame_ms)
    bh_floor = max(bh_floor, ABS_FLOOR)
    mic_floor = max(mic_floor, ABS_FLOOR)

    segments = []
    for i in range(0, min(len(audio_bh), len(audio_mic)), frame_size):
        bh_frame = audio_bh[i:i + frame_size]
        mic_frame = audio_mic[i:i + frame_size]
        energy_bh = float(np.sqrt(np.mean(bh_frame ** 2)))
        energy_mic = float(np.sqrt(np.mean(mic_frame ** 2)))
        if not (np.isfinite(energy_bh) and np.isfinite(energy_mic)):
            continue  # corrupt frame — no opinion is better than a wrong label

        lift_bh = energy_bh / bh_floor
        lift_mic = energy_mic / mic_floor
        bh_on = lift_bh >= SPEAKER_ACTIVE_MULT
        mic_on = lift_mic >= SPEAKER_ACTIVE_MULT

        if not bh_on and not mic_on:
            continue
        if bh_on and not mic_on:
            speaker = "[Remote]"
        elif mic_on and not bh_on:
            speaker = "[You]"
        elif lift_mic >= lift_bh * SPEAKER_LEAD_RATIO:
            # Both lit but the mic leads by a wide margin: you are talking and
            # the far end is only bleeding back through the room.
            speaker = "[You]"
        elif lift_bh >= lift_mic * SPEAKER_LEAD_RATIO:
            speaker = "[Remote]"
        elif _frame_correlation(bh_frame, mic_frame) >= BLEED_CORRELATION:
            # Neither side leads on level. Heavy speaker bleed and genuine
            # simultaneous speech look identical that way, but not in shape:
            # bleed is a scaled copy of the tap, so it correlates with it,
            # while two people talking at once do not. (Room delay and
            # colouring lower the correlation, hence the loose threshold.)
            speaker = "[Remote]"
        else:
            speaker = "[Both]"
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


def _remote_label() -> str:
    """Get the label for remote speaker(s). Uses participant names if available."""
    participants = get_remote_participants()
    if len(participants) == 1:
        return f"[{participants[0]}]"
    return "[Remote]"


def _build_diarized_text(text: str, segments: list[tuple[str, int, int]],
                         audio_len: int) -> str:
    """Pick one label for the chunk from its frame-level segments."""
    if not segments:
        return text

    # A [Both] frame counts toward each side — it is time both of them held.
    you_time = 0
    remote_time = 0
    for speaker, start, end in segments:
        span = end - start
        if speaker == "[You]":
            you_time += span
        elif speaker == "[Both]":
            you_time += span
            remote_time += span
        else:
            remote_time += span

    total = you_time + remote_time
    if total <= 0:
        return text

    you_share = you_time / total
    remote = _remote_label()
    if you_share > 0.7:
        return f"[You] {text}"
    if you_share < 0.3:
        return f"{remote} {text}"
    return f"[You + {remote.strip('[]')}] {text}"


# -- Transcription (whisper.cpp with Metal GPU) --------------------------------

WHISPER_CLI = os.environ.get("WHISPER_CLI", "whisper-cli")
WHISPER_SERVER_BIN = os.environ.get("WHISPER_SERVER", "whisper-server")
MODELS_DIR = Path(__file__).parent / "models"
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "large-v3-turbo")


def resolve_model_path(name: str | None = None) -> Path:
    """File for a whisper.cpp model name. An explicit path always wins.

    The menu bar writes a model name into config.json and the watcher passes it
    through as WHISPER_MODEL. That name used to land in a variable nothing read,
    so the selector moved a label and changed no behaviour at all.
    """
    override = os.environ.get("WHISPER_MODEL_PATH")
    if override:
        return Path(override)
    wanted = MODELS_DIR / f"ggml-{name or WHISPER_MODEL}.bin"
    if wanted.exists():
        return wanted
    # A name pointing at a file that is not there must not end the recording.
    # Say so and use what is on disk.
    available = sorted(MODELS_DIR.glob("ggml-*.bin"))
    if available:
        print(f"[meeting] Model {wanted.name} not installed — using {available[0].name}",
              file=sys.stderr, flush=True)
        return available[0]
    return wanted  # nothing installed; the caller reports the missing file


WHISPER_MODEL_PATH = str(resolve_model_path())
WHISPER_SERVER_HOST = os.environ.get("WHISPER_SERVER_HOST", "127.0.0.1")
WHISPER_SERVER_PORT = int(os.environ.get("WHISPER_SERVER_PORT", "8178"))
_whisper_server_available = False  # set True after successful health check


# -- Vocabulary biasing --------------------------------------------------------
#
# whisper.cpp takes an initial prompt that biases decoding toward preferred
# spellings, and this is where names and domain terms belong. Fixing them here
# is deterministic and free; fixing them afterwards means asking a model to
# rewrite text it has no way to verify, which is what the Qwen3 stage was for
# and why it kept inventing.
#
# Measured on a synthesised line: without a prompt whisper returns
# "Клод Кот ... в Ресерчере"; with one it returns "Claude Code ... в Researcher".
#
# Only the last 224 tokens of the prompt are consumed and later tokens weigh
# more, so the list is trimmed from the front and the most specific terms —
# the people actually on this call — go last.

CONFIG_FILE = Path(__file__).parent / "config.json"
WHISPER_PROMPT_MAX_CHARS = 600  # ~224 tokens of mixed RU/EN, conservatively
_PROMPT_PREFIX = "Совещание. Участники и термины: "


def _load_glossary() -> list[str]:
    """Terms the user wants spelled a particular way, from config.json."""
    try:
        import json as _json
        data = _json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        terms = data.get("glossary", [])
        if isinstance(terms, str):
            terms = terms.split(",")
        return [str(t).strip() for t in terms if str(t).strip()]
    except Exception:
        return []


def _render_prompt(terms: list[str]) -> str:
    return f"{_PROMPT_PREFIX}{', '.join(terms)}."


def build_whisper_prompt(participants: list[str] | None = None,
                         glossary: list[str] | None = None,
                         max_chars: int = WHISPER_PROMPT_MAX_CHARS) -> str:
    """Initial prompt biasing decoding toward known names and terms."""
    if participants is None:
        participants = get_remote_participants()
    if glossary is None:
        glossary = _load_glossary()

    terms: list[str] = []
    seen: set[str] = set()
    for term in list(glossary) + list(participants):  # participants last = strongest
        cleaned = str(term).strip()
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        terms.append(cleaned)

    if not terms:
        return ""

    # Drop from the front until it fits: the tail is the part that carries.
    while len(terms) > 1 and len(_render_prompt(terms)) > max_chars:
        terms.pop(0)
    return _render_prompt(terms)[:max_chars]


def format_time(dt: datetime) -> str:
    return dt.strftime("%H:%M:%S")


# Known Whisper hallucination patterns (appears on silence/quiet audio).
# Verified against 1300+ real transcripts — every RU entry below was observed
# leaking into production output.
_HALLUCINATION_PATTERNS = [
    # RU — subtitle credits (Whisper was trained on subtitled video).
    # "спасибо за субтитры" alone accounted for 175 leaks in the archive.
    "продолжение следует", "субтитры сделал", "субтитры делал",
    "субтитры создавал", "добавил субтитры", "субтитры и перевод",
    "спасибо за субтитры", "редактор субтитров", "корректор",
    "dimatorzok", "дубровскому",
    # RU — channel outros
    "спасибо за просмотр", "подписывайтесь на канал", "подпишись на канал",
    "ставьте лайк", "до новых встреч",
    # RU — sound annotations emitted as plain text
    "динамичная музыка", "играет музыка", "музыка играет", "звучит музыка",
    # EN
    "thanks for watching", "subscribe", "like and subscribe",
    "see you next time", "please subscribe", "thank you for watching",
    # DE
    "danke fürs zuschauen", "abonnieren", "bis zum nächsten mal",
    "untertitel von", "untertitel der",
    # FR
    "merci d'avoir regardé", "abonnez-vous", "sous-titres",
    # ES
    "gracias por ver", "suscríbete",
    # ZH / JA
    "谢谢观看", "请订阅", "ご視聴ありがとう",
]

# Non-speech events Whisper brackets instead of transcribing: *музыка*, [MUSIC], (смех)
_SOUND_EVENT_WORDS = (
    "музык", "смех", "аплодисмент", "шум", "тишин", "звук", "вздох", "кашель",
    "music", "laugh", "applause", "silence", "noise", "blank_audio", "inaudible",
)
_ASTERISK_ANNOTATION_RE = re.compile(r"\*[^*]{0,60}\*")
_BRACKET_ANNOTATION_RE = re.compile(r"[\[(]([^\[\]()]{0,60})[\])]")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")


def _looks_like_sound_event(inner: str) -> bool:
    """True if bracketed text is a sound annotation, not speech."""
    stripped = inner.strip()
    if not stripped or len(stripped.split()) > 5:
        return False
    low = stripped.lower()
    if any(w in low for w in _SOUND_EVENT_WORDS):
        return True
    # ALL-CAPS bracketed text is an annotation, never speech
    return stripped.isupper() and any(c.isalpha() for c in stripped)


def _strip_hallucinations(text: str) -> str:
    """Remove canned phrases and sound annotations while keeping real speech.

    Whisper often appends an outro to a chunk that also contains genuine
    speech ("...обсудим бюджет. Продолжение следует..."). Dropping the whole
    chunk loses the real content, so strip only the offending sentences.
    """
    if not text:
        return ""

    text = _ASTERISK_ANNOTATION_RE.sub(" ", text)
    text = _BRACKET_ANNOTATION_RE.sub(
        lambda m: " " if _looks_like_sound_event(m.group(1)) else m.group(0), text)

    kept = []
    for sentence in _SENTENCE_SPLIT_RE.split(text):
        low = sentence.lower()
        if any(p in low for p in _HALLUCINATION_PATTERNS):
            continue
        kept.append(sentence)

    return re.sub(r"\s+", " ", " ".join(kept)).strip()

# -- Adaptive noise floor ------------------------------------------------------
#
# A fixed RMS gate cannot work here. The tap level follows Zoom's playback
# volume and the mic level follows input gain, so "quiet" is a different number
# on every machine and it moves during a call. The old constant (0.01) sat in
# the middle of ordinary conversation: in the archive, chunks measured at
# 0.0060 and 0.0069 were discarded as silence while a 0.0122 chunk two slots
# later transcribed into a normal sentence — same voices, same conversation.
#
# Track a rolling low percentile of frame energies instead. Speech has gaps, so
# a low percentile settles on the real noise floor even when someone talks
# without pausing, and it follows the device instead of guessing at it.

NOISE_WINDOW_FRAMES = 600   # 500 ms frames — roughly 5 minutes of history
NOISE_PERCENTILE = 10       # low percentile tracks the floor, not the speech
NOISE_FRAME_MS = 500
GATE_MULTIPLIER = 2.0       # a chunk must clear this multiple of the floor
GATE_MIN = 0.0008           # below this is near-silence on any device
GATE_MAX = 0.006            # never gate above this — protects quiet speech


class NoiseFloor:
    """Rolling estimate of a channel's noise floor from frame energies."""

    def __init__(self, window: int = NOISE_WINDOW_FRAMES,
                 percentile: int = NOISE_PERCENTILE):
        self._energies = deque(maxlen=window)
        self._percentile = percentile
        self._lock = threading.Lock()

    def observe(self, audio: np.ndarray, frame_ms: int = NOISE_FRAME_MS):
        """Record the frame energies of a chunk."""
        if audio is None or audio.size == 0:
            return
        frame_size = int(SAMPLE_RATE * frame_ms / 1000)
        frames = []
        if frame_size > 0:
            for i in range(0, len(audio) - frame_size + 1, frame_size):
                frames.append(float(np.sqrt(np.mean(audio[i:i + frame_size] ** 2))))
        if not frames:  # chunk shorter than one frame — use it whole
            frames.append(float(np.sqrt(np.mean(audio ** 2))))
        with self._lock:
            self._energies.extend(frames)

    @property
    def level(self) -> float:
        with self._lock:
            if not self._energies:
                return 0.0
            return float(np.percentile(self._energies, self._percentile))

    def reset(self):
        with self._lock:
            self._energies.clear()


def _estimate_floor(audio: np.ndarray, frame_ms: int = NOISE_FRAME_MS) -> float:
    """Noise floor of a single chunk, for callers with no rolling history."""
    if audio is None or audio.size == 0:
        return 0.0
    frame_size = int(SAMPLE_RATE * frame_ms / 1000)
    energies = []
    if frame_size > 0:
        for i in range(0, len(audio) - frame_size + 1, frame_size):
            energies.append(float(np.sqrt(np.mean(audio[i:i + frame_size] ** 2))))
    if not energies:
        energies = [float(np.sqrt(np.mean(audio ** 2)))]
    energies = [e for e in energies if np.isfinite(e)]
    if not energies:
        return 0.0
    return float(np.percentile(energies, NOISE_PERCENTILE))


_mix_noise = NoiseFloor()   # gates what reaches Whisper
_bh_noise = NoiseFloor()    # remote channel, for diarization
_mic_noise = NoiseFloor()   # local channel, for diarization


def speech_gate() -> float:
    """RMS below which a chunk is certainly silence.

    Deliberately permissive: with whisper-server the model is already resident,
    so a needless call on a quiet chunk costs a fraction of a second, while a
    wrongly dropped chunk is speech lost for good. Anything that slips through
    is cleaned up downstream by _strip_hallucinations and _is_hallucination.
    """
    floor = _mix_noise.level
    if floor <= 0:
        return GATE_MIN
    return min(GATE_MAX, max(GATE_MIN, floor * GATE_MULTIPLIER))


def _is_hallucination(text: str) -> bool:
    """Check if text is a known Whisper hallucination."""
    lower = text.lower().strip()
    if len(lower) < 3:
        return True  # single letters, dots
    if lower in ("и", "а", "...", "–", "-", "."):
        return True
    if all(c in ".-… " for c in lower):
        return True
    for pattern in _HALLUCINATION_PATTERNS:
        if pattern in lower:
            return True
    # Repetition heuristic: same word/phrase repeated 3+ times
    words = lower.split()
    if len(words) >= 3 and len(set(words)) == 1:
        return True
    return False


def _audio_to_wav_bytes(audio_np: np.ndarray) -> bytes:
    """Convert float32 numpy audio to WAV bytes (16-bit PCM, 16kHz, mono)."""
    import struct
    pcm = (audio_np * 32768).clip(-32768, 32767).astype(np.int16).tobytes()
    # Build WAV header manually — avoids ffmpeg dependency for server mode
    data_size = len(pcm)
    header = struct.pack('<4sI4s4sIHHIIHH4sI',
        b'RIFF', 36 + data_size, b'WAVE',
        b'fmt ', 16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16,
        b'data', data_size)
    return header + pcm


# -- Transcription result ------------------------------------------------------
#
# whisper-server's verbose_json gives a probability for every word. We used to
# ask for plain json and throw that away, which left no way to tell a confident
# sentence from a guessed one — and no way to aim a correction pass at the part
# that actually needs it.

WORD_CONFIDENCE_FLOOR = 0.5   # below this the word is a guess
CHUNK_JUNK_FRACTION = 0.6     # share of guessed words that makes a chunk noise
MIN_WORDS_TO_JUDGE = 4        # too few words to draw any conclusion from


@dataclass
class Transcription:
    """Text plus whatever the decoder was willing to say about its confidence."""
    text: str
    tokens: list[tuple[str, float]] = field(default_factory=list)
    language: str = ""

    @property
    def has_confidence(self) -> bool:
        return bool(self.tokens)

    @property
    def words(self) -> list[tuple[str, float]]:
        """Subword tokens merged into whole words.

        whisper scores tokens, not words, and marks a word boundary with a
        leading space: "Claude" arrives as " Cla" + "ude" with probabilities
        0.35 and 1.00. Reporting "Cla" as the doubtful item is useless, so
        pieces are joined and a word takes the confidence of its weakest
        piece — if any part was guessed, the word is a guess.
        """
        merged: list[tuple[str, float]] = []
        for raw, prob in self.tokens:
            piece = raw.strip()
            if not piece:
                continue
            if not merged or (raw[:1].isspace()):
                merged.append((piece, prob))
            else:
                word, worst = merged[-1]
                merged[-1] = (word + piece, min(worst, prob))
        return merged

    @property
    def low_confidence_words(self) -> list[str]:
        return [w for w, p in self.words if p < WORD_CONFIDENCE_FLOOR]

    @property
    def low_confidence_fraction(self) -> float:
        words = self.words
        if not words:
            return 0.0
        return len(self.low_confidence_words) / len(words)

    @property
    def looks_like_noise(self) -> bool:
        """Mostly guessed words — the decoder was reading tea leaves."""
        if len(self.words) < MIN_WORDS_TO_JUDGE:
            return False
        return self.low_confidence_fraction > CHUNK_JUNK_FRACTION


def _parse_verbose_json(payload: dict) -> Transcription:
    """Pull text and per-word probabilities out of a verbose_json reply."""
    text = (payload.get("text") or "").strip()
    tokens: list[tuple[str, float]] = []
    for segment in payload.get("segments") or []:
        for word in segment.get("words") or []:
            # Keep the raw token: the leading space is the word boundary.
            raw = word.get("word") or ""
            prob = word.get("probability")
            if raw.strip() and isinstance(prob, (int, float)):
                tokens.append((raw, float(prob)))
    return Transcription(text=text, tokens=tokens,
                         language=str(payload.get("language") or ""))


def _check_whisper_server() -> bool:
    """Check if whisper-server is healthy."""
    global _whisper_server_available
    try:
        import urllib.request
        url = f"http://{WHISPER_SERVER_HOST}:{WHISPER_SERVER_PORT}/health"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=2) as resp:
            if resp.status == 200:
                _whisper_server_available = True
                return True
    except Exception:
        pass
    _whisper_server_available = False
    return False


def _transcribe_via_server(wav_bytes: bytes, lang: str, prompt: str = "") -> Transcription:
    """Transcribe via whisper-server HTTP API. Model stays in memory."""
    import urllib.request
    import json as _json

    url = f"http://{WHISPER_SERVER_HOST}:{WHISPER_SERVER_PORT}/inference"
    boundary = "----GhostmicBoundary"

    # Build multipart/form-data body
    parts = []
    # File part
    parts.append(f"--{boundary}\r\n"
                 f"Content-Disposition: form-data; name=\"file\"; filename=\"chunk.wav\"\r\n"
                 f"Content-Type: audio/wav\r\n\r\n")
    # Form fields
    fields = {"response_format": "verbose_json", "temperature": "0.0", "beam_size": "1"}
    if lang != "auto":
        fields["language"] = lang
    if prompt:
        fields["prompt"] = prompt

    body = b""
    body += parts[0].encode()
    body += wav_bytes
    body += b"\r\n"
    for key, val in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{val}\r\n".encode()
    body += f"--{boundary}--\r\n".encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")

    with urllib.request.urlopen(req, timeout=30) as resp:
        return _parse_verbose_json(_json.loads(resp.read()))


def _transcribe_via_cli(audio_np: np.ndarray, lang: str, prompt: str = "") -> Transcription:
    """Transcribe via whisper-cli subprocess. Model loaded each time."""
    import tempfile

    pcm_data = (audio_np * 32768).astype(np.int16).tobytes()
    tmp_wav = os.path.join(tempfile.gettempdir(), "ghostmic-chunk.wav")

    try:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "s16le", "-ar", "16000", "-ac", "1", "-i", "pipe:0", tmp_wav],
            input=pcm_data, capture_output=True, timeout=10,
        )

        whisper_cmd = [
            WHISPER_CLI,
            "-m", WHISPER_MODEL_PATH,
            "-f", tmp_wav,
            "--no-timestamps",
            "-t", "4",
        ]
        if lang != "auto":
            whisper_cmd.extend(["-l", lang])
        if prompt:
            whisper_cmd.extend(["--prompt", prompt])

        r = subprocess.run(whisper_cmd, capture_output=True, timeout=30)
        text = r.stdout.decode("utf-8", errors="replace").strip()
        text = text.replace("[BLANK_AUDIO]", "").strip()
        lines = [l.strip() for l in text.split("\n") if l.strip() and not l.strip().startswith("[")]
        # No per-word confidence on this path — the CLI does not report it.
        return Transcription(text=" ".join(lines).strip())

    except Exception as e:
        print(f"[meeting] CLI transcription error: {e}", file=sys.stderr, flush=True)
        return Transcription(text="")
    finally:
        try:
            os.unlink(tmp_wav)
        except OSError:
            pass


# transcribe_chunk keeps returning a plain string — every caller and test
# expects that — while the confidence data for the chunk it just produced is
# parked here for the post-processing stage to aim with.
_last_transcription = Transcription(text="")


def _set_last_transcription(result: Transcription):
    global _last_transcription
    _last_transcription = result


def last_transcription() -> Transcription:
    return _last_transcription


def transcribe_chunk(model_unused, audio_np: np.ndarray) -> str:
    """Transcribe audio using whisper-server (preferred) or whisper-cli (fallback)."""
    if audio_np is None or audio_np.size == 0:
        return ""

    rms = float(np.sqrt(np.mean(audio_np ** 2)))
    # Observe before gating so the estimate is available on the very first
    # chunk, and so a call that starts in silence does not gate itself out.
    _mix_noise.observe(audio_np)
    gate = speech_gate()
    if rms < gate:
        return ""

    lang = effective_language()
    prompt = build_whisper_prompt()

    # Try whisper-server first (model in memory, ~10x faster)
    if _whisper_server_available:
        try:
            wav_bytes = _audio_to_wav_bytes(audio_np)
            result = _transcribe_via_server(wav_bytes, lang, prompt)
        except Exception as e:
            print(f"[meeting] Server transcription failed, falling back to CLI: {e}",
                  file=sys.stderr, flush=True)
            result = _transcribe_via_cli(audio_np, lang, prompt)
    else:
        result = _transcribe_via_cli(audio_np, lang, prompt)

    _maybe_lock_language(result)

    # A chunk the decoder mostly guessed at is noise wearing a sentence. Say so
    # rather than dropping it quietly — a silent discard is what hid the fixed
    # RMS gate for months.
    if result.looks_like_noise:
        print(f"[meeting] (discarded: {result.low_confidence_fraction:.0%} of words "
              f"below {WORD_CONFIDENCE_FLOOR} confidence) {result.text[:60]}", flush=True)
        _set_last_transcription(Transcription(text=""))
        return ""

    # Strip canned outros/sound events first — a chunk may hold real speech too
    text = _strip_hallucinations(result.text)
    if _is_hallucination(text):
        _set_last_transcription(Transcription(text=""))
        return ""

    _set_last_transcription(Transcription(text=text, tokens=result.tokens))
    return text


# -- Post-processing (rule-based + optional LLM) ------------------------------

_prev_chunks: list[str] = []
_prev_speaker: str = ""
_llm_model = None
_llm_tokenizer = None
_llm_sampler = None
_llm_error_logged = False


def _llm_budget(text: str) -> int:
    """Room to echo the chunk back plus a little slack for repairs."""
    try:
        n = len(_llm_tokenizer.encode(text))
    except Exception:
        n = len(text) // 2
    return max(64, min(1024, int(n * 1.4) + 48))


def _llm_generate(formatted_prompt: str, max_tokens: int = 150) -> str:
    """Run the post-processing LLM.

    mlx-lm dropped the `temp=` argument in favour of a sampler callable; passing
    it raises TypeError deep inside generate_step. Build the sampler explicitly
    and fall back to greedy decoding on older/newer builds.
    """
    global _llm_sampler
    from mlx_lm import generate

    if _llm_sampler is None:
        try:
            from mlx_lm.sample_utils import make_sampler
            _llm_sampler = make_sampler(temp=0.1)
        except Exception:
            _llm_sampler = False  # sampler API unavailable — use defaults

    kwargs = {"sampler": _llm_sampler} if _llm_sampler else {}
    return generate(_llm_model, _llm_tokenizer, prompt=formatted_prompt,
                    max_tokens=max_tokens, **kwargs)


_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"</?(?:chunk|context)>")


def _strip_think_block(text: str) -> str:
    """Remove Qwen3 reasoning blocks.

    The `/no_think` hint in the prompt is not binding — the chat template
    decides, and when it opens a <think> block the tags land in the output and
    from there would land in the transcript.
    """
    text = _THINK_BLOCK_RE.sub(" ", text).replace("<think>", " ").replace("</think>", " ")
    # The chunk is fenced in the prompt and the model sometimes echoes the
    # fence back around its answer.
    return _FENCE_RE.sub(" ", text).strip()


def _llm_refine(clean_text: str, context: str, participants: list[str],
                doubtful: list[str] | None = None) -> str:
    """Run one post-processing pass. Returns the model's cleaned-up output."""
    prompt = _build_llm_prompt(clean_text, context, participants, doubtful)
    messages = [{"role": "user", "content": prompt}]
    try:
        formatted = _llm_tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False,
            enable_thinking=False)
    except TypeError:
        # Older templates have no such switch — _strip_think_block covers it.
        formatted = _llm_tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, tokenize=False)
    return _strip_think_block(
        _llm_generate(formatted, max_tokens=_llm_budget(clean_text)).strip())


def _load_llm():
    """Load micro LLM for transcript post-processing. Called once."""
    global _llm_model, _llm_tokenizer
    if _llm_model is not None:
        return True
    try:
        from mlx_lm import load
        _llm_model, _llm_tokenizer = load(LLM_MODEL)
        print(f"[meeting] LLM post-processor ready ({LLM_MODEL})", flush=True)
        return True
    except Exception as e:
        print(f"[meeting] LLM not available: {e}. Using rule-based only.", file=sys.stderr, flush=True)
        return False


def _build_llm_prompt(clean_text: str, context: str, participants: list[str],
                      doubtful: list[str] | None = None) -> str:
    """The instruction handed to the post-processing model.

    Order matters here. The first version led with the rules and then put
    "Context:" immediately before "New chunk:", which reads to a small instruct
    model as an invitation to carry on writing the conversation — and that is
    what it did in 35% of measured chunks. The context is now fenced and
    labelled as reference, and the rules sit last, right before the generation
    point, where recency gives them the most weight.
    """
    people = ", ".join(participants) if participants else ""
    doubtful_block = ""
    if doubtful:
        doubtful_block = (
            "\nThe speech recogniser was unsure of these words:\n"
            f"{', '.join(doubtful)}\n")

    return f"""You are repairing one chunk of an automatic speech-to-text transcript.

Earlier chunks, for reference only. Do NOT continue them, do NOT copy from them:
<context>
{context}
</context>

The chunk to repair:
<chunk>
{clean_text}
</chunk>
{doubtful_block}{f"People in this meeting: {people}" if people else ""}
Rules:
- Output the chunk again, repairing only what is clearly a recognition error.
- Copy every other word exactly as it appears.
- Do not continue the conversation. Do not add or remove sentences.
- Do not change numbers or names.
- Keep the original language.
- If nothing can be repaired with confidence, output the chunk unchanged.

/no_think
The repaired chunk:"""


# The post-processor is a 0.6B model at 4-bit being asked to edit Russian
# speech. Measured over 40 real chunks from the archive (tools/measure_llm_post.py):
#
#   35%  fabricated — output was unrelated dialogue continued from the context
#        rather than a repair of the chunk. Caught here, by length or divergence.
#   35%  returned unchanged.
#   25%  edited, and the edit was almost always a silent truncation: the tail
#        sentence of the chunk simply disappeared. One case introduced a typo
#        into a correct word ("ожидаю" -> "ожидю").
#    2%  a real repair.
#
# Truncation is the reason this stage is off by default. A garbled sentence is
# visibly garbled; a dropped one leaves nothing behind to notice. Nothing
# downstream can tell a repair from an invention, so when the stage is enabled
# the burden is on its output to still look like its input.

LLM_MAX_DIVERGENCE = 0.35     # normalised edit distance
# Repairing a misheard word barely moves the length. Dropping the tail of the
# chunk halves it — and that, not fabrication, is what the model does most
# often once the prompt stops inviting it to continue the conversation. The
# old floor of 0.5 waved every one of those through.
LLM_MIN_LENGTH_RATIO = 0.92
LLM_MAX_LENGTH_RATIO = 1.15

_DIGIT_RUN_RE = re.compile(r"\d+")
_llm_rejections: dict[str, int] = {}


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _llm_output_is_safe(original: str, candidate: str) -> tuple[bool, str]:
    """True if `candidate` reads as a repair of `original` rather than a rewrite."""
    if not candidate or len(candidate) < 5:
        return False, "too-short"
    if _is_hallucination(candidate):
        return False, "hallucination"

    # Figures first: it is the most specific thing that can go wrong, and the
    # one nobody can sanity-check later from the text alone.
    if any(d not in candidate for d in _DIGIT_RUN_RE.findall(original)):
        return False, "dropped-number"

    ratio = len(candidate) / max(len(original), 1)
    if not (LLM_MIN_LENGTH_RATIO <= ratio <= LLM_MAX_LENGTH_RATIO):
        return False, "length"

    if _levenshtein(original, candidate) / max(len(original), 1) > LLM_MAX_DIVERGENCE:
        return False, "divergence"

    return True, ""


def _note_llm_rejection(reason: str):
    first = reason not in _llm_rejections
    _llm_rejections[reason] = _llm_rejections.get(reason, 0) + 1
    if first:
        print(f"[meeting] LLM post-processing rejected a result ({reason}) — "
              f"keeping the transcribed text", file=sys.stderr, flush=True)


def _postprocess_text(text: str, timestamp: str) -> str:
    """Post-process transcribed text: speaker continuity + optional LLM refinement."""
    global _prev_speaker

    # Phase 1: Rule-based speaker continuity
    current_speaker = ""
    clean_text = text
    # Check for speaker labels: [You], [Remote], [Name], [You + Name]
    if text.startswith("["):
        bracket_end = text.find("]")
        if bracket_end != -1:
            current_speaker = text[:bracket_end + 1]
            clean_text = text[bracket_end + 1:].strip()

    # If same speaker as last chunk and no pause, mark as continuation
    show_label = True
    if current_speaker == _prev_speaker and current_speaker:
        show_label = False  # same speaker continues

    if current_speaker:
        _prev_speaker = current_speaker

    # Phase 2: LLM refinement (if available)
    if ENABLE_LLM_POST and _llm_model is not None and _prev_chunks:
        global _llm_error_logged
        try:
            context = "\n".join(_prev_chunks[-3:])
            doubtful = last_transcription().low_confidence_words

            # Nothing the decoder was unsure about means nothing to repair.
            # Calling the model anyway is how confident text got truncated:
            # asked to improve a correct sentence, it removes the last one.
            if doubtful:
                result = _llm_refine(clean_text, context,
                                     get_remote_participants(), doubtful)
                accepted, reason = _llm_output_is_safe(clean_text, result)
                if accepted:
                    clean_text = result
                else:
                    _note_llm_rejection(reason)
        except Exception as e:
            # Never fail the transcript over post-processing — but say so once,
            # otherwise a broken LLM stage stays invisible for months.
            if not _llm_error_logged:
                _llm_error_logged = True
                print(f"[meeting] LLM post-processing disabled after error: {e}",
                      file=sys.stderr, flush=True)

    # Store for context
    _prev_chunks.append(f"{timestamp} {current_speaker} {clean_text}")
    if len(_prev_chunks) > 5:
        _prev_chunks.pop(0)

    # Build final line
    if current_speaker:
        if show_label:
            return f"{current_speaker} {clean_text}"
        else:
            return f"...{clean_text}"
    return clean_text


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


STALE_BUFFER_TIMEOUT = 5  # flush partial buffer if no new audio for this many seconds


def writer_thread(model, has_mic: bool):
    buffer_bh = []
    buffer_mic = []
    chunk_start = datetime.now()
    last_heartbeat = time.monotonic()
    last_audio_received = time.monotonic()

    print(f"[meeting] Recording... transcript -> {TRANSCRIPT_FILE}", flush=True)

    try:
        while not shutdown_event.is_set() or not audio_queue.empty() or not mic_queue.empty():
            bh_chunks = _drain_queue(audio_queue)
            if not bh_chunks:
                try:
                    bh_chunks = [audio_queue.get(timeout=0.5)]
                except queue.Empty:
                    pass
            if bh_chunks:
                last_audio_received = time.monotonic()
            buffer_bh.extend(bh_chunks)

            if has_mic:
                buffer_mic.extend(_drain_queue(mic_queue))

            total_bh = sum(d.size for d in buffer_bh)

            # Flush partial buffer if audio stopped flowing
            stale = (buffer_bh
                     and total_bh < SAMPLE_RATE * CHUNK_SECONDS
                     and time.monotonic() - last_audio_received > STALE_BUFFER_TIMEOUT)

            if total_bh >= SAMPLE_RATE * CHUNK_SECONDS or stale:
                if stale:
                    print("[meeting] No new audio — flushing partial buffer", flush=True)
                audio_bh = np.concatenate([b.flatten() for b in buffer_bh]).astype(np.float32)
                buffer_bh = []

                audio_mic_raw = None
                if has_mic and buffer_mic:
                    audio_mic_raw = np.concatenate([b.flatten() for b in buffer_mic]).astype(np.float32)
                    buffer_mic = []
                    # Trim to shorter channel — padding with zeros causes false speaker labels
                    min_len = min(len(audio_bh), len(audio_mic_raw))
                    audio_bh_padded = audio_bh[:min_len]
                    audio_mic_padded = audio_mic_raw[:min_len]
                    audio_mixed = np.clip((audio_bh_padded + audio_mic_padded) * 0.5, -1.0, 1.0)
                else:
                    audio_mixed = audio_bh
                    buffer_mic = []

                # Feed the per-channel floors from every chunk, silence
                # included — the quiet stretches are what the estimate needs.
                if audio_mic_raw is not None:
                    _bh_noise.observe(audio_bh_padded)
                    _mic_noise.observe(audio_mic_padded)

                chunk_end = datetime.now()

                rms = np.sqrt(np.mean(audio_mixed ** 2))
                nonzero = np.count_nonzero(audio_mixed)
                print(f"[meeting] Transcribing {format_time(chunk_start)}-{format_time(chunk_end)} (samples={len(audio_mixed)}, RMS={rms:.4f}, nonzero={nonzero})...", flush=True)
                text = transcribe_chunk(model, audio_mixed)

                if text:
                    if ENABLE_DIARIZATION and audio_mic_raw is not None:
                        try:
                            segments = _classify_speakers(
                                audio_bh_padded, audio_mic_padded,
                                bh_floor=_bh_noise.level or None,
                                mic_floor=_mic_noise.level or None)
                            text = _build_diarized_text(text, segments, len(audio_bh_padded))
                        except Exception:
                            pass
                    # Post-process: speaker continuity + LLM refinement
                    ts = f"[{format_time(chunk_start)}-{format_time(chunk_end)}]"
                    text = _postprocess_text(text, ts)
                    line = f"{ts}\n{text}\n\n"
                    _write_transcript(TRANSCRIPT_FILE, line)
                    print(f"[meeting] -> {text[:80]}...", flush=True)
                else:
                    # Print the gate alongside the level: a chunk dropped just
                    # under the gate is the signature of speech being lost, and
                    # without both numbers that is invisible in the log.
                    print(f"[meeting] (silence: RMS={rms:.4f}, gate={speech_gate():.4f})",
                          flush=True)
                    _touch_heartbeat(TRANSCRIPT_FILE)

                last_heartbeat = time.monotonic()
                chunk_start = chunk_end
            else:
                if time.monotonic() - last_heartbeat > 60:
                    _touch_heartbeat(TRANSCRIPT_FILE)
                    last_heartbeat = time.monotonic()

        # Flush remaining
        if buffer_bh:
            audio = np.concatenate(buffer_bh).flatten().astype(np.float32)
            flush_mic_padded = None
            flush_bh_padded = audio
            if has_mic and buffer_mic:
                mic_audio = np.concatenate(buffer_mic).flatten().astype(np.float32)
                min_len = min(len(audio), len(mic_audio))
                flush_bh_padded = audio[:min_len]
                flush_mic_padded = mic_audio[:min_len]
                audio = np.clip((flush_bh_padded + flush_mic_padded) * 0.5, -1.0, 1.0)
            text = transcribe_chunk(model, audio)
            if text:
                if ENABLE_DIARIZATION and flush_mic_padded is not None:
                    try:
                        segments = _classify_speakers(
                            flush_bh_padded, flush_mic_padded,
                            bh_floor=_bh_noise.level or None,
                            mic_floor=_mic_noise.level or None)
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

def _read_exactly(stream, n: int, timeout: float = 5.0) -> bytes:
    """Read exactly n bytes from a raw stream, with timeout.
    Returns partial data or empty bytes on timeout/EOF."""
    import select
    buf = bytearray()
    raw = stream.raw if hasattr(stream, 'raw') else stream
    try:
        fd = raw.fileno()
    except Exception:
        fd = None  # no select() support (e.g. BytesIO in tests)
    while len(buf) < n:
        if fd is not None:
            ready, _, _ = select.select([fd], [], [], timeout)
            if not ready:
                return bytes(buf) if buf else b''  # timeout
        chunk = raw.read(n - len(buf))
        if not chunk:
            return bytes(buf) if buf else b''  # EOF
        buf.extend(chunk)
    return bytes(buf)


TAP_SILENCE_TIMEOUT = 10  # seconds with no data → treat tap as dead


def tap_reader_thread(proc: subprocess.Popen):
    """Read raw PCM (16-bit LE, 16kHz, mono) from tap subprocess stdout."""
    BYTES_PER_SAMPLE = 2
    CHUNK_SAMPLES = SAMPLE_RATE  # 1 second chunks
    CHUNK_BYTES = CHUNK_SAMPLES * BYTES_PER_SAMPLE
    silence_start = None

    try:
        while not shutdown_event.is_set():
            # Check if tap process died
            if proc.poll() is not None:
                print(f"[meeting] Tap process exited (code {proc.returncode})", flush=True)
                shutdown_event.set()
                break

            data = _read_exactly(proc.stdout, CHUNK_BYTES, timeout=2.0)
            if not data:
                # No data — could be timeout or EOF
                if proc.poll() is not None:
                    print("[meeting] Tap process ended", flush=True)
                    shutdown_event.set()
                    break
                # Track consecutive silence (tap alive but no audio)
                if silence_start is None:
                    silence_start = time.monotonic()
                elif time.monotonic() - silence_start > TAP_SILENCE_TIMEOUT:
                    print("[meeting] Tap produced no audio for 10s — assuming dead", flush=True)
                    shutdown_event.set()
                    break
                continue

            silence_start = None  # got data, reset silence tracker
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


_UNMUTE_KEYWORDS = [
    "unmute audio", "включить звук",       # EN, RU
    "mikrofon einschalten", "stummschaltung aufheben",  # DE
    "réactiver le son", "activer le son",   # FR
    "reactivar audio", "activar sonido",    # ES
    "riattiva audio",                       # IT
    "ミュート解除",                           # JA
    "음소거 해제",                            # KO
    "取消静音",                               # ZH
    "ativar som",                           # PT
]


def _check_zoom_mute() -> bool:
    """Check if Zoom mic is muted via AppleScript menu inspection.
    Scans all menu items for 'unmute' keywords across locales."""
    try:
        r = subprocess.run(
            ["osascript", "-e", '''tell application "System Events"
    tell process "zoom.us"
        set allItems to ""
        repeat with mb in menu bar items of menu bar 1
            try
                set menuItems to name of every menu item of menu 1 of mb
                repeat with mi in menuItems
                    set allItems to allItems & mi & "|"
                end repeat
            end try
        end repeat
        return allItems
    end tell
end tell'''],
            capture_output=True, text=True, timeout=3
        )
        items_lower = r.stdout.strip().lower()
        if not items_lower:
            return False
        return any(kw in items_lower for kw in _UNMUTE_KEYWORDS)
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
    # Load LLM for post-processing (if enabled)
    if ENABLE_LLM_POST:
        print("[meeting] Loading LLM post-processor...", flush=True)
        _load_llm()

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
            bufsize=0,
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

    # Start participant name detection (Accessibility API)
    if PARTICIPANTS_BIN.exists():
        threading.Thread(target=_poll_participants, daemon=True).start()
        print("[meeting] Participant detection active", flush=True)
    else:
        print("[meeting] zoom-participants binary not found, using generic [Remote] labels", flush=True)

    # Check whisper-server first, then fall back to whisper-cli
    model = None  # not used — whisper-server/cli handles model
    if _check_whisper_server():
        print(f"[meeting] whisper-server ready at :{WHISPER_SERVER_PORT} (model in memory, Metal GPU)", flush=True)
    else:
        try:
            subprocess.run([WHISPER_CLI, "--help"], capture_output=True, timeout=5)
            print(f"[meeting] whisper-cli ready (Metal GPU) — consider whisper-server for faster transcription", flush=True)
        except FileNotFoundError:
            print(f"[meeting] ERROR: neither whisper-server nor whisper-cli found. Install: brew install whisper-cpp", file=sys.stderr)
            sys.exit(1)

        if not Path(WHISPER_MODEL_PATH).exists():
            model_file = Path(WHISPER_MODEL_PATH).name
            print(f"[meeting] ERROR: Model not found: {WHISPER_MODEL_PATH}", file=sys.stderr)
            print(f"[meeting] Download: curl -L -o {WHISPER_MODEL_PATH} "
                  f"https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{model_file}",
                  file=sys.stderr)
            sys.exit(1)

    print(f"[meeting] Model: {WHISPER_MODEL} ({Path(WHISPER_MODEL_PATH).name}), "
          f"Language: {LANGUAGE}", flush=True)

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
