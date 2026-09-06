"""The engine adapter: temp files that never outlive a transcription, a single
worker thread the event loop never waits on, and the numbers /healthz reads.

Nothing here loads a model. `_engine` is the seam — a callable taking a path
and returning text — so every test injects its own and the suite stays offline
and instant. The one test that does load a real model is opt-in, at the bottom.
"""

import asyncio
import os
import threading
import time
import unittest
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi import Response

from ringbearer import config, transcribe

FIXTURES = Path(__file__).resolve().parent / "fixtures"
# 7.6 seconds of speech in the shape the phone sends — AAC-LC in M4A, mono,
# 16 kHz. What it says, which is what the real-engine test below reads back:
# "Remind me to take the trash out tonight, and add milk to the grocery list.
#  Also, what time is my dentist appointment on Thursday?"
CLIP = FIXTURES / "ring_fixture.m4a"


@contextmanager
def engine(fake, *, state="ready", error=None, durations=()):
    """Pin transcribe's mutable module state and give it a private state dir.

    Parameters:
      fake: the injected engine — any callable(path) -> str, or None to stand
        in for a model that never loaded.
      state (str): the loading state to report.
      durations (iterable[int]): seed for the median window.

    Yields: the temporary STATE_DIR, so a test can look at tmp/ and models/.
    """
    with TemporaryDirectory() as tmp:
        with (
            patch.multiple(
                transcribe,
                _engine=fake,
                _state=state,
                _error=error,
                _last_ms=None,
                _durations=deque(durations, maxlen=transcribe.KEEP_DURATIONS),
                _loading=None,
            ),
            patch.object(config, "STATE_DIR", Path(tmp)),
        ):
            yield Path(tmp)


class TempFileTests(unittest.TestCase):
    """A11: the audio is a file for the length of one transcription."""

    def test_engine_reads_the_posted_bytes_and_the_file_is_gone_after(self):
        seen = {}

        def fake(path):
            seen["path"] = path
            seen["bytes"] = Path(path).read_bytes()
            seen["mode"] = Path(path).stat().st_mode & 0o777
            return " hello there "

        with engine(fake) as state:
            text, ms = asyncio.run(transcribe.run(b"m4a-bytes", "abc123.m4a"))
            self.assertEqual(text, " hello there ")  # verbatim, never trimmed here
            self.assertGreaterEqual(ms, 0)
            self.assertEqual(seen["bytes"], b"m4a-bytes")
            self.assertTrue(seen["path"].endswith(".m4a"))
            self.assertEqual(seen["mode"], 0o600)
            self.assertEqual(list((state / "tmp").iterdir()), [])

    def test_temp_file_is_gone_after_an_engine_failure(self):
        def explodes(path):
            raise RuntimeError("engine on fire")

        with engine(explodes) as state:
            with self.assertRaisesRegex(RuntimeError, "engine on fire"):
                asyncio.run(transcribe.run(b"m4a-bytes", "abc123.m4a"))
            self.assertEqual(list((state / "tmp").iterdir()), [])

    def test_a_missing_engine_is_a_clean_failure_not_a_stalled_load(self):
        with engine(None, state="error", error="OSError: no wheel") as state:
            self.assertIsNone(transcribe.start_loading())  # a failed load is not retried
            with self.assertRaisesRegex(RuntimeError, "engine unavailable"):
                asyncio.run(transcribe.run(b"m4a-bytes", "abc123.m4a"))
            self.assertEqual(list((state / "tmp").iterdir()), [])

    def test_tmp_dir_is_private_to_this_user(self):
        with engine(lambda path: "x") as state:
            self.assertEqual(transcribe.tmp_dir(), state / "tmp")
            self.assertEqual((state / "tmp").stat().st_mode & 0o777, 0o700)

    def test_model_cache_lives_under_the_state_dir(self):
        with engine(lambda path: "x") as state:
            self.assertEqual(transcribe.model_cache(), state / "models")
            self.assertTrue((state / "models").is_dir())


class WorkerThreadTests(unittest.IsolatedAsyncioTestCase):
    """One capture at a time, and never on the event loop."""

    def test_the_executor_is_a_single_worker(self):
        self.addCleanup(transcribe.shutdown)
        self.assertEqual(transcribe.executor()._max_workers, 1)

    async def test_healthz_answers_while_a_transcription_is_running(self):
        from ringbearer.app import healthz

        running, release = threading.Event(), threading.Event()

        def blocks(path):
            running.set()
            release.wait(5)
            return "done"

        with engine(blocks):
            task = asyncio.create_task(transcribe.run(b"m4a-bytes", "abc.m4a"))
            try:
                for _ in range(500):  # wait for the worker to be inside the engine
                    if running.is_set():
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(running.is_set())
                # The worker thread is blocked; the loop must not be. What
                # /healthz says is pinned elsewhere — this only asks whether it
                # can still say it.
                health = await asyncio.wait_for(healthz(Response()), timeout=1)
                self.assertIn("connection", health)
            finally:
                release.set()
            self.assertEqual((await task)[0], "done")
        transcribe.shutdown()

    async def test_an_injected_engine_never_starts_a_model_load(self):
        with engine(lambda path: "x"):
            self.assertIsNone(transcribe.start_loading())
            self.assertIsNone(transcribe._loading)


class SnapshotTests(unittest.TestCase):
    """What /healthz reads."""

    def test_reports_engine_model_and_the_median_window(self):
        with engine(lambda path: "x", durations=(10, 20, 60)):
            snapshot = transcribe.snapshot()
        self.assertEqual(snapshot["engine"], "faster-whisper")
        self.assertEqual(snapshot["model"], config.TRANSCRIBE_MODEL)
        self.assertEqual(snapshot["state"], "ready")
        self.assertIsNone(snapshot["last_ms"])
        self.assertEqual(snapshot["median_ms"], 20)
        self.assertIsNone(snapshot["error"])

    def test_a_run_updates_last_and_median(self):
        def slow(path):
            time.sleep(0.05)
            return "spoken"

        with engine(slow):
            _text, ms = asyncio.run(transcribe.run(b"m4a-bytes", "abc.m4a"))
            snapshot = transcribe.snapshot()
        self.assertGreaterEqual(ms, 45)  # the timer wraps the engine call only
        self.assertEqual(snapshot["last_ms"], ms)
        self.assertEqual(snapshot["median_ms"], ms)

    def test_median_window_is_bounded(self):
        with engine(lambda path: "x", durations=range(200)):
            self.assertEqual(len(transcribe._durations), transcribe.KEEP_DURATIONS)

    def test_reports_disabled_when_the_model_is_switched_off(self):
        for value in ("", "off", "none", "OFF"):
            with self.subTest(value=value), engine(None, state="loading"):
                with patch.object(config, "TRANSCRIBE_MODEL", value):
                    self.assertFalse(transcribe.enabled())
                    self.assertEqual(transcribe.snapshot()["state"], "disabled")
                    self.assertIsNone(transcribe.start_loading())

    def test_reports_the_error_that_stopped_the_load(self):
        with engine(None, state="error", error="OSError: no wheel"):
            snapshot = transcribe.snapshot()
        self.assertEqual(snapshot["state"], "error")
        self.assertEqual(snapshot["error"], "OSError: no wheel")


@unittest.skipUnless(
    os.environ.get("RINGBEARER_REAL_ENGINE") == "1",
    "set RINGBEARER_REAL_ENGINE=1 to load a real model (downloads ~75 MB once)",
)
class RealEngineTests(unittest.TestCase):
    """The one test that runs faster-whisper for real, on the fixture clip.

    Opt-in because it downloads a model and takes seconds, not milliseconds.
    It is also the C77 probe: the weights land under the state dir, and a
    second construction reads them with no network at all.

    The four words asserted below — trash, milk, dentist, Thursday — are the
    load-bearing nouns of the two spoken sentences (see CLIP at the top of
    this file). Four, and not the whole transcript, because punctuation and
    casing are the model's business and change with the model.
    """

    def test_the_real_engine_reads_the_fixture_clip(self):
        self.addCleanup(transcribe.shutdown)
        with engine(None, state="loading") as state:
            transcribe.start_loading()
            text, ms = asyncio.run(transcribe.run(CLIP.read_bytes(), "fixture.m4a"))
            print(f"\n[real engine] {config.TRANSCRIBE_MODEL}: {ms} ms — {text}")
            lowered = text.lower()
            for word in ("trash", "milk", "dentist", "thursday"):
                self.assertIn(word, lowered)
            self.assertTrue(any((state / "models").iterdir()))
            self.assertEqual(list((state / "tmp").iterdir()), [])

            from faster_whisper import WhisperModel

            WhisperModel(
                config.TRANSCRIBE_MODEL,
                device="cpu",
                compute_type="int8",
                download_root=str(state / "models"),
                local_files_only=True,  # a second start makes no network request
            )


if __name__ == "__main__":
    unittest.main()
