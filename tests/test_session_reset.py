import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import ringbearer as rb


class FakeTelegramClient:
    def __init__(self):
        self.sent = []
        self.events = []
        self.after_reset = asyncio.Event()
        self.release_reset = asyncio.Event()

    async def send_message(self, peer, text, **kwargs):
        self.events.append(f"send:{text}")
        self.sent.append((peer, text, kwargs))
        if text == "/new":
            self.after_reset.set()
            await self.release_reset.wait()
        return SimpleNamespace(id=len(self.sent))

    def is_connected(self):
        return True

    async def disconnect(self):
        self.events.append("disconnect")

    async def connect(self):
        self.events.append("connect")

    async def __call__(self, request):
        self.events.append("ping")
        return object()


class SessionResetTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = FakeTelegramClient()
        self.entity = object()
        self.state = patch.multiple(
            rb,
            TELEGRAM_ENABLED=True,
            NEW_TOPIC_PER_CAPTURE=False,
            DELIVERY_CONTEXT="conversation",
            RING_PREFIX="mic: ",
            tg_client=self.client,
            DEFAULT_ASSISTANT="assistant",
            ASSISTANT_NAME="Avery",
            ASSISTANT_ROSTER={"assistant": "@bot"},
            assistant_entities={"assistant": self.entity},
            tg_client_lock=asyncio.Lock(),
        )
        self.state.start()
        self.addCleanup(self.state.stop)

    async def test_default_delivery_does_not_reset_or_change_message_shape(self):
        self.client.release_reset.set()
        sent = await rb.deliver("keep *literal*")
        self.assertTrue(sent)
        self.assertEqual(
            self.client.sent,
            [(self.entity, "mic: keep *literal*", {"parse_mode": None, "reply_to": None})],
        )

    async def test_reset_is_plain_message_before_enveloped_transcript(self):
        self.client.release_reset.set()
        sent = await rb.deliver("keep *literal*", start_new_conversation=True)
        self.assertTrue(sent)
        self.assertEqual([item[1] for item in self.client.sent], ["/new", ANY])
        reset, transcript = self.client.sent
        self.assertEqual(reset[2], {"parse_mode": None, "reply_to": None})
        self.assertEqual(transcript[2], {"parse_mode": None, "reply_to": None})
        self.assertIn("fresh Avery session", transcript[1])
        self.assertIn("Do not send or suggest /new", transcript[1])
        self.assertEqual(transcript[1].split("Transcript:\n", 1)[1], "keep *literal*")

    async def test_reset_failure_stops_the_transcript(self):
        async def fail(peer, text, **kwargs):
            self.client.sent.append((peer, text, kwargs))
            raise ConnectionError("reset rejected")

        self.client.send_message = fail
        with self.assertRaisesRegex(ConnectionError, "reset rejected"):
            await rb.deliver("hello", start_new_conversation=True)
        self.assertEqual([item[1] for item in self.client.sent], ["/new"])

    async def test_transcript_failure_after_reset_does_not_trigger_another_send(self):
        async def fail_transcript(peer, text, **kwargs):
            self.client.sent.append((peer, text, kwargs))
            if text == "/new":
                return SimpleNamespace(id=1)
            raise ConnectionError("transcript rejected")

        self.client.send_message = fail_transcript
        with self.assertRaisesRegex(ConnectionError, "transcript rejected"):
            await rb.deliver("hello", start_new_conversation=True)
        self.assertEqual(len(self.client.sent), 2)
        self.assertEqual(self.client.sent[0][1], "/new")
        self.assertEqual(
            self.client.sent[1][1].split("Transcript:\n", 1)[1], "hello"
        )

    async def test_reset_bypasses_topic_mode_to_keep_both_messages_in_dm_root(self):
        self.client.release_reset.set()
        with patch.object(rb, "NEW_TOPIC_PER_CAPTURE", True):
            await rb.deliver("hello", start_new_conversation=True)
        self.assertEqual([item[1] for item in self.client.sent], ["/new", ANY])
        self.assertTrue(all(item[2]["reply_to"] is None for item in self.client.sent))
        self.assertNotIn("ping", self.client.events)

    async def test_other_delivery_cannot_interleave_reset_and_transcript(self):
        reset = asyncio.create_task(rb.deliver("first", start_new_conversation=True))
        await self.client.after_reset.wait()
        normal = asyncio.create_task(rb.deliver("second"))
        await asyncio.sleep(0)
        self.client.release_reset.set()
        await asyncio.gather(reset, normal)
        self.assertEqual(
            [item[1].split("Transcript:\n")[-1] for item in self.client.sent],
            ["/new", "first", "mic: second"],
        )

    async def test_reconnect_cannot_interleave_reset_and_transcript(self):
        reset = asyncio.create_task(rb.deliver("first", start_new_conversation=True))
        await self.client.after_reset.wait()
        reconnect = asyncio.create_task(rb.verify_connection(rebuild=True))
        await asyncio.sleep(0)
        self.client.release_reset.set()
        await asyncio.gather(reset, reconnect)
        self.assertEqual(
            self.client.events[:5],
            ["send:/new", ANY, "disconnect", "connect", "ping"],
        )
        self.assertTrue(self.client.events[1].startswith("send:"))

    async def test_dry_run_with_reset_intent_never_calls_delivery(self):
        deliver = AsyncMock()
        rows = []
        with (
            patch.object(rb, "deliver", deliver),
            patch.object(rb, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            result = await rb.send_to_assistant(
                f"{rb.DRY_RUN_PREFIX} bridge probe",
                start_new_conversation=True,
            )
        deliver.assert_not_awaited()
        self.assertEqual(result, "Dry run: received, not delivered to Telegram.")
        self.assertTrue(rows[0]["start_new_conversation"])

    async def test_reset_relay_has_a_budget_for_both_telegram_sends(self):
        timeouts = []

        async def observe_timeout(awaitable, timeout):
            timeouts.append(timeout)
            return await awaitable

        with (
            patch.object(rb, "deliver", AsyncMock(return_value=True)),
            patch.object(rb.asyncio, "wait_for", observe_timeout),
            patch.object(rb, "log_capture"),
            patch("builtins.print"),
        ):
            result = await rb.relay(
                "hello", "assistant", start_new_conversation=True
            )
        self.assertIn("Delivered", result)
        self.assertEqual(timeouts, [60])

    def test_one_shot_envelope_keeps_fresh_session_guidance_and_transcript(self):
        with patch.object(rb, "DELIVERY_CONTEXT", "one_shot"):
            formatted = rb.format_delivery_message(
                "do the thing", start_new_conversation=True
            )
        self.assertIn("[RING CAPTURE: ONE-SHOT]", formatted)
        self.assertIn("fresh Avery session", formatted)
        self.assertIn("execute it now without asking for confirmation", formatted)
        self.assertEqual(formatted.split("Transcript:\n", 1)[1], "do the thing")


if __name__ == "__main__":
    unittest.main()
