"""The bridge's own ears: audio in, text out, on this machine's CPU.

The fast door exists so a capture never waits on a cloud agent turn, which
means the transcription cannot go to a cloud either. faster-whisper runs the
model here, int8 on CPU, one capture at a time in a single worker thread — the
event loop keeps answering /healthz and the MCP route while a clip is being
decoded, and two captures arriving together queue instead of fighting over the
same cores.

Audio is a temp file for exactly as long as the engine needs it (A11): written
under STATE_DIR/tmp, deleted in a finally on every path, and never written into
captures.jsonl, which records only how many bytes arrived.

`_engine` is the seam. It is any callable taking a file path and returning the
transcript, so tests inject a fake and never touch a model; the real one is
built in the worker thread at startup by `_load`. Like the rest of the package,
mutable module state here is read through this module (`transcribe.snapshot()`),
never copied by value into another namespace.
"""

import asyncio
import os
import statistics
import tempfile
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

from . import config

ENGINE = "faster-whisper"
# TRANSCRIBE_MODEL values that mean "don't": an install that only ever sends a
# `transcription` part from the phone has no use for 150 MB of wheels warming up.
DISABLED_VALUES = {"", "off", "none"}
KEEP_DURATIONS = 50  # the window median_ms is taken over

_engine = None  # callable(path: str) -> str, or None until the model loads
_state = "loading"  # loading -> ready | error; snapshot() reports disabled
_error: str | None = None
_last_ms: int | None = None
_durations: deque = deque(maxlen=KEEP_DURATIONS)
_executor: ThreadPoolExecutor | None = None
_loading = None  # the Future for the model load; None until start_loading()


def enabled() -> bool:
    """Whether this install transcribes at all. Read live, not cached: tests
    rebind TRANSCRIBE_MODEL."""
    return config.TRANSCRIBE_MODEL.strip().lower() not in DISABLED_VALUES


def executor() -> ThreadPoolExecutor:
    """The one thread that decodes audio.

    max_workers=1 is the concurrency policy, not a placeholder: the model is
    loaded once into this thread and CPU decoding does not get faster by
    running two of them on the same cores. It also serializes the model load
    ahead of every capture submitted after it, which is what makes
    `start_loading()` at startup enough.
    """
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="transcribe")
    return _executor


def model_cache() -> Path:
    """Where faster-whisper keeps downloaded weights: STATE_DIR/models.

    The state dir is the seam that keeps the Docker image disposable, so the
    ~75 MB of weights live beside the session file and survive a rebuild. First
    start downloads; every start after reads the cache.
    """
    path = config.STATE_DIR / "models"
    path.mkdir(parents=True, exist_ok=True)
    return path


def tmp_dir() -> Path:
    """Where a capture's audio lives for the length of one transcription.

    0700 because the bytes are the user's voice, and the directory should be
    unreadable to anyone else on the box for the seconds it is occupied.
    """
    path = config.STATE_DIR / "tmp"
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _whisper_reader(model):
    """Wrap a WhisperModel as the plain callable `_engine` promises.

    faster-whisper returns a generator of segments and does the actual decoding
    while it is consumed, so the join below is the work — which is why the
    timer in `_transcribe_path` wraps this whole call and not just the return.
    """

    def read(path: str) -> str:
        segments, _info = model.transcribe(path, beam_size=1)
        return "".join(segment.text for segment in segments).strip()

    return read


def _load() -> None:
    """Build the model. Runs in the worker thread; never raises into it."""
    global _engine, _state, _error
    try:
        from faster_whisper import WhisperModel

        model = WhisperModel(
            config.TRANSCRIBE_MODEL,
            device="cpu",
            compute_type="int8",
            download_root=str(model_cache()),
        )
    except Exception as e:  # a bad model name, a missing wheel, no network
        _engine, _error, _state = None, f"{type(e).__name__}: {e}", "error"
        print(
            f"[transcribe] model {config.TRANSCRIBE_MODEL!r} failed to load: {_error}",
            flush=True,
        )
        return
    _engine, _error, _state = _whisper_reader(model), None, "ready"
    print(f"[transcribe] {ENGINE} {config.TRANSCRIBE_MODEL} ready (cpu/int8)", flush=True)


def start_loading():
    """Begin loading the model now, so the first capture does not pay for it.

    Called from the app's lifespan. Idempotent, and deliberately inert in three
    cases: transcription disabled, an engine already present (a test injected
    one), and a previous load that failed — a failed load is configuration
    shaped (wrong model name, missing wheel), so retrying it on every capture
    would stall each one to fail the same way.

    Returns: the Future for the load, or None when nothing was started.
    """
    global _loading, _state
    if not enabled() or _engine is not None or _state == "error" or _loading is not None:
        return _loading
    _state = "loading"
    _loading = executor().submit(_load)
    return _loading


def _transcribe_path(path: str) -> tuple[str, int]:
    """Decode one file in the worker thread. Returns (text, engine ms)."""
    global _last_ms
    if _engine is None:
        detail = f": {_error}" if _error else ""
        raise RuntimeError(f"transcription engine unavailable ({_state}{detail})")
    started = time.monotonic()
    text = _engine(path)
    elapsed = round((time.monotonic() - started) * 1000)
    _last_ms = elapsed
    _durations.append(elapsed)
    return text, elapsed


async def run(audio: bytes, filename: str | None = None) -> tuple[str, int]:
    """Transcribe one capture's audio without blocking the event loop.

    Parameters:
      audio (bytes): the m4a the phone posted. faster-whisper decodes it
        through PyAV, so no ffmpeg binary and no shelling out.
      filename (str | None): the phone's `<recordingId>.m4a`, used only to give
        the temp file a recognisable suffix.

    Returns: (transcript, milliseconds spent inside the engine).

    Raises: RuntimeError if the model never loaded; whatever the engine raises
      otherwise. The temp file is gone either way.
    """
    start_loading()
    suffix = Path(filename).suffix if filename else ".m4a"
    fd, path = tempfile.mkstemp(dir=tmp_dir(), prefix="capture-", suffix=suffix or ".m4a")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor(), _transcribe_path, path)
    finally:
        # A11: the audio outlives nothing. Success, engine failure, cancelled
        # request — the file goes here.
        with suppress(FileNotFoundError):
            os.unlink(path)


def snapshot() -> dict:
    """The `transcribe` block on /healthz: what the engine is and how fast it
    has been. Reads state, talks to nothing."""
    durations = list(_durations)
    return {
        "engine": ENGINE,
        "model": config.TRANSCRIBE_MODEL,
        "state": _state if enabled() else "disabled",
        "last_ms": _last_ms,
        "median_ms": round(statistics.median(durations)) if durations else None,
        "error": _error,
    }


def shutdown() -> None:
    """Drop the worker thread at process shutdown. A queued capture is
    cancelled rather than waited on: the process is going away."""
    global _executor, _loading
    if _executor is not None:
        _executor.shutdown(wait=False, cancel_futures=True)
    _executor, _loading = None, None
