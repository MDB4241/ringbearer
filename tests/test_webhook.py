"""The fast door, driven the way the phone drives it.

Every request here is a hand-built multipart body in the app's own wire order
(`audio`, `transcription`, `test`, `recordedAt`, `client`) with the app's own
headers, because that is the contract this route exists to satisfy — a
TestClient convenience that sends something tidier would test the wrong thing.

The engine is injected (`transcribe._engine`), so nothing loads a model, and
delivery is a mock: what these tests assert is which text reached `deliver`,
which assistant it was addressed to, and what the capture row says afterwards.
"""

import json
import os
import subprocess
import sys
import unittest
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from ringbearer import config, telegram, transcribe
from ringbearer.app import app
from ringbearer.route import webhook as route_webhook

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent
CLIP = TESTS / "fixtures" / "ring_fixture.m4a"

TOKEN = "test-bridge-token"
URL = route_webhook.WEBHOOK_PATH
# Grabbed before any patch: one test wants the real writer back.
REAL_LOG_CAPTURE = telegram.log_capture


def phone_body(*, audio=None, transcription=None, test=None, recorded_at="1757100000000"):
    """Build the multipart body the Pebble app builds, in its field order.

    Parameters:
      audio (tuple[str, bytes] | None): (filename, bytes) for the `audio` part.
      transcription (str | None): the `transcription` part, when the phone
        transcribed it itself.
      test (str | None): the `test` part, present only on test events.

    Returns: (body bytes, Content-Type header value).
    """
    boundary = uuid4().hex
    chunks = []

    def part(disposition, body: bytes, content_type=None):
        head = f"--{boundary}\r\nContent-Disposition: form-data; {disposition}\r\n"
        if content_type:
            head += f"Content-Type: {content_type}\r\n"
        chunks.append(head.encode() + b"\r\n" + body + b"\r\n")

    if audio is not None:
        name, data = audio
        part(f'name="audio"; filename="{name}"', data, "audio/mp4")
    if transcription is not None:
        part('name="transcription"', transcription.encode())
    if test is not None:
        part('name="test"', test.encode())
    part('name="recordedAt"', recorded_at.encode())  # accepted and ignored
    part('name="client"', b"ring")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def post(client, *, path=URL, token=TOKEN, trigger="single-click-hold", test_header=None, **payload):
    """POST one capture the way the app would, headers included."""
    body, content_type = phone_body(**payload)
    headers = {
        "Content-Type": content_type,
        "User-Agent": "CoreApp/1.2.3",
        "Accept": "application/json",
        "Accept-Encoding": "gzip, deflate",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if trigger is not None:
        headers["X-Index-Trigger"] = trigger
    if test_header is not None:
        headers["X-Index-Test"] = test_header
    if payload.get("audio") is not None:
        headers["X-Audio-Size"] = str(len(payload["audio"][1]))
    return client.post(path, content=body, headers=headers)


@contextmanager
def bridge(rows, *, deliver=None, engine=None, roster=None, webhook_assistant="assistant"):
    """Stand the app up with a pinned config, a mocked link and no model.

    Yields: a namespace with `client` (redirects never followed, so a 3xx is a
    failure and not a silent retry), `deliver` and the temporary state dir.
    """
    deliver = deliver if deliver is not None else AsyncMock(return_value=True)
    with TemporaryDirectory() as tmp:
        with (
            patch.multiple(
                config,
                BRIDGE_TOKEN=TOKEN,
                DEFAULT_ASSISTANT="assistant",
                ASSISTANT_ROSTER=roster or {"assistant": "@assistant_bot"},
                WEBHOOK_ASSISTANT=webhook_assistant,
                STATE_DIR=Path(tmp),
                TELEGRAM_ENABLED=True,
            ),
            patch.object(telegram, "log_capture", rows.append),
            patch.object(telegram, "deliver", deliver),
            patch.object(route_webhook, "_seen", deque(maxlen=route_webhook.SEEN_LIMIT)),
            # No test loads a model: the engine below is injected, and the
            # lifespan's warm-up is a no-op here.
            patch.object(transcribe, "start_loading", lambda: None),
            patch.multiple(
                transcribe,
                _engine=engine,
                _state="ready",
                _error=None,
                _last_ms=None,
                _durations=deque(maxlen=transcribe.KEEP_DURATIONS),
                _loading=None,
            ),
            patch("builtins.print"),
        ):
            yield SimpleNamespace(
                client=TestClient(app, follow_redirects=False),
                deliver=deliver,
                state=Path(tmp),
            )


class CaptureTests(unittest.TestCase):
    """C59: the shape of an ordinary capture, in and out."""

    def test_a_transcript_bearing_capture_is_logged_and_relayed_verbatim(self):
        rows = []
        spoken = "remind me to take the trash out *tonight*"
        with bridge(rows) as b:
            response = post(b.client, transcription=spoken, trigger="double-click-hold")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["forwarded"], True)
        (row,) = rows
        self.assertEqual(row["source"], "webhook")
        self.assertEqual(row["trigger"], "double-click-hold")
        self.assertEqual(row["assistant"], "assistant")
        self.assertEqual(row["transcription"], spoken)
        self.assertTrue(row["forwarded"])
        self.assertIsInstance(row["telegram_ms"], int)
        self.assertFalse(row["test"])
        self.assertFalse(row["duplicate"])
        self.assertNotIn("transcribe_ms", row)
        self.assertNotIn("audio_bytes", row)
        b.deliver.assert_awaited_once_with(spoken, "assistant")

    def test_the_row_reaches_captures_jsonl_as_json(self):
        """log_capture is the real one here — the row must serialize."""
        rows = []
        with bridge(rows) as b, patch.object(telegram, "log_capture", REAL_LOG_CAPTURE):
            with patch.object(config, "CAPTURES", b.state / "captures.jsonl"):
                post(b.client, transcription="hello", audio=("rec-1.m4a", b"\x00" * 12))
                written = [json.loads(line) for line in (b.state / "captures.jsonl").read_text().splitlines()]
        self.assertEqual(len(written), 1)
        self.assertEqual(written[0]["source"], "webhook")
        self.assertEqual(written[0]["audio_bytes"], 12)

    def test_a_delivery_failure_is_still_a_two_hundred_and_a_row(self):
        rows = []
        deliver = AsyncMock(side_effect=RuntimeError("boom"))
        with bridge(rows, deliver=deliver) as b, patch.object(telegram, "request_recheck"):
            response = post(b.client, transcription="hello")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["forwarded"])
        self.assertIn("boom", response.json()["error"])
        (row,) = rows
        self.assertFalse(row["forwarded"])
        self.assertIn("boom", row["error"])
        self.assertEqual(row["transcription"], "hello")

    def test_a_dry_run_prefix_is_logged_and_never_delivered(self):
        rows = []
        with bridge(rows) as b:
            response = post(b.client, transcription=f"{config.DRY_RUN_PREFIX} webhook probe")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["dry_run"])
        b.deliver.assert_not_awaited()
        self.assertTrue(rows[0]["dry_run"])
        self.assertFalse(rows[0]["forwarded"])


class AuthTests(unittest.TestCase):
    """C60: the door is shut without the token, and shut quietly."""

    def test_a_missing_token_is_rejected_and_logs_nothing(self):
        rows = []
        with bridge(rows) as b:
            response = post(b.client, token=None, transcription="hello")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(rows, [])
        b.deliver.assert_not_awaited()

    def test_a_wrong_token_is_rejected_and_logs_nothing(self):
        rows = []
        with bridge(rows) as b:
            response = post(b.client, token="not-the-token", transcription="hello")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(rows, [])
        b.deliver.assert_not_awaited()


class TestEventTests(unittest.TestCase):
    """C61 / A12: the app's Send test event button proves the endpoint and
    nothing else — it must never reach a real assistant."""

    def test_the_apps_test_event_is_logged_and_never_delivered(self):
        rows = []
        with bridge(rows) as b:
            response = post(
                b.client,
                transcription="Index webhook test event",
                test="true",
                test_header="true",
                trigger="test-event",
            )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["test"])
        b.deliver.assert_not_awaited()
        (row,) = rows
        self.assertTrue(row["test"])
        self.assertFalse(row["forwarded"])
        self.assertEqual(row["trigger"], "test-event")
        self.assertEqual(row["transcription"], "Index webhook test event")

    def test_the_test_header_alone_is_enough(self):
        rows = []
        with bridge(rows) as b:
            response = post(b.client, transcription="hello", test_header="true")
        self.assertEqual(response.status_code, 200)
        b.deliver.assert_not_awaited()
        self.assertTrue(rows[0]["test"])

    def test_a_test_event_carrying_audio_never_runs_the_engine(self):
        rows = []
        engine = unittest.mock.Mock(side_effect=AssertionError("engine must not run"))
        with bridge(rows, engine=engine) as b:
            response = post(b.client, audio=("rec-t.m4a", b"\x00" * 8), test="true")
        self.assertEqual(response.status_code, 200)
        engine.assert_not_called()
        self.assertTrue(rows[0]["test"])


class TranscriptionTests(unittest.TestCase):
    """C62 / C63: who makes the text, and who does not."""

    def test_audio_only_is_transcribed_here_and_the_text_relayed(self):
        rows = []
        seen = {}

        def engine(path):
            seen["bytes"] = Path(path).read_bytes()
            return "take the trash out tonight"

        with bridge(rows, engine=engine) as b:
            response = post(b.client, audio=("rec-42.m4a", b"fake-m4a-bytes"))
            self.assertEqual(list((b.state / "tmp").iterdir()), [])  # A11
        self.assertEqual(response.status_code, 200)
        self.assertEqual(seen["bytes"], b"fake-m4a-bytes")
        b.deliver.assert_awaited_once_with("take the trash out tonight", "assistant")
        (row,) = rows
        self.assertEqual(row["transcription"], "take the trash out tonight")
        self.assertEqual(row["audio_bytes"], len(b"fake-m4a-bytes"))
        self.assertIsInstance(row["transcribe_ms"], int)
        self.assertEqual(row["engine"], "faster-whisper")
        self.assertEqual(row["model"], config.TRANSCRIBE_MODEL)
        self.assertTrue(row["forwarded"])

    def test_a_transcription_part_wins_even_with_audio_beside_it(self):
        rows = []
        engine = unittest.mock.Mock(side_effect=AssertionError("engine must not run"))
        with bridge(rows, engine=engine) as b:
            response = post(
                b.client,
                audio=("rec-43.m4a", b"fake-m4a-bytes"),
                transcription="the phone already did this",
            )
        self.assertEqual(response.status_code, 200)
        engine.assert_not_called()
        (row,) = rows
        self.assertEqual(row["transcription"], "the phone already did this")
        self.assertEqual(row["audio_bytes"], len(b"fake-m4a-bytes"))
        self.assertNotIn("transcribe_ms", row)
        b.deliver.assert_awaited_once_with("the phone already did this", "assistant")

    def test_a_blank_transcription_part_falls_back_to_the_audio(self):
        rows = []
        with bridge(rows, engine=lambda path: "recovered from the audio") as b:
            response = post(b.client, audio=("rec-44.m4a", b"bytes"), transcription="   ")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(rows[0]["transcription"], "recovered from the audio")
        self.assertIn("transcribe_ms", rows[0])

    def test_an_empty_payload_is_two_hundred_with_a_reason(self):
        rows = []
        with bridge(rows) as b:
            response = post(b.client)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["forwarded"])
        self.assertEqual(response.json()["reason"], route_webhook.NOTHING_TO_SAY)
        b.deliver.assert_not_awaited()
        (row,) = rows
        self.assertFalse(row["forwarded"])
        self.assertEqual(row["reason"], route_webhook.NOTHING_TO_SAY)
        self.assertIsNone(row["transcription"])

    def test_an_engine_failure_is_logged_not_five_hundred(self):
        rows = []

        def engine(path):
            raise RuntimeError("engine on fire")

        with bridge(rows, engine=engine) as b:
            response = post(b.client, audio=("rec-45.m4a", b"bytes"))
            self.assertEqual(list((b.state / "tmp").iterdir()), [])  # A11
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["forwarded"])
        b.deliver.assert_not_awaited()
        (row,) = rows
        self.assertEqual(row["reason"], "transcription failed")
        self.assertIn("engine on fire", row["error"])
        self.assertNotIn("transcribe_ms", row)

    def test_the_row_carries_a_byte_count_never_the_audio(self):
        """A11: nothing writes audio into captures.jsonl."""
        rows = []
        audio = CLIP.read_bytes()
        with bridge(rows, engine=lambda path: "spoken words") as b:
            post(b.client, audio=("rec-46.m4a", audio))
        (row,) = rows
        self.assertEqual(row["audio_bytes"], len(audio))
        serialized = json.dumps(row).encode()
        self.assertNotIn(audio[:64], serialized)
        self.assertLess(len(serialized), 1000)


class FixedTargetTests(unittest.TestCase):
    """C64 / A10: the door never reads the words to choose a destination."""

    def test_addressing_another_assistant_by_voice_changes_nothing(self):
        rows = []
        roster = {"assistant": "@assistant_bot", "second": 123456}
        with bridge(rows, roster=roster, webhook_assistant="assistant") as b:
            post(b.client, transcription="Hey second, remind me to buy milk")
        b.deliver.assert_awaited_once_with(
            "Hey second, remind me to buy milk", "assistant"
        )
        self.assertEqual(rows[0]["assistant"], "assistant")

    def test_the_configured_assistant_is_the_one_that_gets_it(self):
        rows = []
        roster = {"assistant": "@assistant_bot", "second": 123456}
        with bridge(rows, roster=roster, webhook_assistant="second") as b:
            post(b.client, transcription="hello")
        b.deliver.assert_awaited_once_with("hello", "second")
        self.assertEqual(rows[0]["assistant"], "second")


class StartupValidationTests(unittest.TestCase):
    """C64's other half: a name that is not in the roster stops the process
    while someone is looking, the same way ASSISTANTS does."""

    def _import_config(self, **env_overrides):
        env = {k: v for k, v in os.environ.items() if k not in ("ASSISTANTS", "WEBHOOK_ASSISTANT")}
        env["TELEGRAM_ENABLED"] = "false"
        env.update(env_overrides)
        return subprocess.run(
            [sys.executable, "-c", "from ringbearer import config; print(config.WEBHOOK_ASSISTANT)"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_an_unknown_webhook_assistant_fails_startup_by_name(self):
        result = self._import_config(
            ASSISTANTS="second:123456", WEBHOOK_ASSISTANT="ghost"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'ghost'", result.stderr)
        self.assertIn("WEBHOOK_ASSISTANT", result.stderr)

    def test_a_roster_name_is_accepted(self):
        result = self._import_config(
            ASSISTANTS="second:123456", WEBHOOK_ASSISTANT="second"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "second")

    def test_the_default_is_the_default_assistant(self):
        result = self._import_config(ASSISTANT_NAME="Hermes")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "hermes")


class DedupeTests(unittest.TestCase):
    """C66: the app's own dedupe is empty after a restart, so ours is not."""

    def test_the_same_recording_is_forwarded_once_and_logged_twice(self):
        rows = []
        with bridge(rows, engine=lambda path: "same words") as b:
            first = post(b.client, audio=("rec-99.m4a", b"bytes"))
            second = post(b.client, audio=("rec-99.m4a", b"bytes"))
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(b.deliver.await_count, 1)
        self.assertEqual(len(rows), 2)
        self.assertFalse(rows[0]["duplicate"])
        self.assertTrue(rows[1]["duplicate"])
        self.assertTrue(rows[0]["forwarded"])
        self.assertFalse(rows[1]["forwarded"])
        self.assertEqual(rows[1]["reason"], "duplicate capture")

    def test_a_different_recording_is_not_a_duplicate(self):
        rows = []
        with bridge(rows, engine=lambda path: "words") as b:
            post(b.client, audio=("rec-1.m4a", b"bytes"))
            post(b.client, audio=("rec-2.m4a", b"bytes"))
        self.assertEqual(b.deliver.await_count, 2)
        self.assertFalse(rows[1]["duplicate"])

    def test_payloads_without_audio_have_no_id_and_are_never_deduped(self):
        rows = []
        with bridge(rows) as b:
            post(b.client, transcription="same words twice")
            post(b.client, transcription="same words twice")
        self.assertEqual(b.deliver.await_count, 2)
        self.assertFalse(rows[0]["duplicate"])
        self.assertFalse(rows[1]["duplicate"])

    def test_the_seen_set_is_bounded(self):
        self.assertEqual(route_webhook._seen.maxlen, 256)


class RoutingTests(unittest.TestCase):
    """C67: the app follows redirects on a POST and re-sends the whole body,
    so this route never issues one."""

    def test_both_paths_answer_directly(self):
        for path in (URL, f"{URL}/"):
            with self.subTest(path=path):
                rows = []
                with bridge(rows) as b:
                    response = post(b.client, path=path, transcription="hello")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(rows), 1)

    def test_the_mcp_mount_still_owns_its_own_paths(self):
        """The webhook route is registered before the MCP mount; it must not
        have swallowed the MCP endpoint on the way past. Runs the lifespan,
        because the MCP door only answers with its session manager running."""
        rows = []
        with (
            patch.multiple(config, BRIDGE_TOKEN=TOKEN, TELEGRAM_ENABLED=False),
            patch.object(telegram, "log_capture", rows.append),
            patch.object(transcribe, "start_loading", lambda: None),
            patch("builtins.print"),
        ):
            with TestClient(app, follow_redirects=False) as client:
                response = client.post(
                    f"{config.MCP_MOUNT}/mcp",
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Accept": "application/json, text/event-stream",
                    },
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [t["name"] for t in response.json()["result"]["tools"]],
            [config.TOOL_NAME],
        )
        self.assertEqual(rows, [])


class HealthTests(unittest.TestCase):
    """C78: the glance, beside the record."""

    def test_healthz_carries_the_transcribe_block(self):
        rows = []
        with bridge(rows, engine=lambda path: "words") as b:
            before = b.client.get("/healthz").json()["transcribe"]
            post(b.client, audio=("rec-7.m4a", b"bytes"))
            after = b.client.get("/healthz").json()["transcribe"]
        self.assertEqual(
            sorted(before),
            ["engine", "error", "last_ms", "median_ms", "model", "state"],
        )
        self.assertEqual(before["engine"], "faster-whisper")
        self.assertEqual(before["model"], config.TRANSCRIBE_MODEL)
        self.assertEqual(before["state"], "ready")
        self.assertIsNone(before["last_ms"])
        self.assertIsNone(before["median_ms"])
        self.assertIsInstance(after["last_ms"], int)
        self.assertIsInstance(after["median_ms"], int)


if __name__ == "__main__":
    unittest.main()
