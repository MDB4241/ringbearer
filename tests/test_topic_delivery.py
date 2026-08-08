import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.tl.functions.messages import CreateForumTopicRequest
from telethon.tl.types import MessageActionTopicCreate

import ringbearer


class FakeTelegramClient:
    def __init__(self, *, topic_id=42, create_error=None, send_error=None):
        self.topic_id = topic_id
        self.create_error = create_error
        self.send_error = send_error
        self.requests = []
        self.sent = []

    async def __call__(self, request):
        self.requests.append(request)
        if self.create_error:
            raise self.create_error
        message = SimpleNamespace(
            id=self.topic_id,
            action=MessageActionTopicCreate(title=request.title, icon_color=0x6FB9F0),
        )
        return SimpleNamespace(updates=[SimpleNamespace(message=message)])

    async def send_message(self, entity, text, **kwargs):
        self.sent.append((entity, text, kwargs))
        if self.send_error:
            raise self.send_error


class TopicTitleTests(unittest.TestCase):
    def test_normalizes_and_truncates_title(self):
        message = "  hello\n\tworld  " + "x" * 100
        title = ringbearer.topic_title(message)
        self.assertEqual(title[:11], "hello world")
        self.assertEqual(len(title), 80)

    def test_empty_title_uses_fallback(self):
        self.assertEqual(ringbearer.topic_title(" \n\t "), "Ring capture")


class ConfigurationTests(unittest.TestCase):
    def test_delivery_mode_label_describes_topic_mode(self):
        with patch.object(ringbearer, "NEW_TOPIC_PER_CAPTURE", True):
            self.assertEqual(ringbearer.delivery_mode_label(), "new topic per capture")

    def test_delivery_mode_label_describes_direct_mode(self):
        with patch.object(ringbearer, "NEW_TOPIC_PER_CAPTURE", False):
            self.assertEqual(
                ringbearer.delivery_mode_label(), "current Telegram conversation"
            )


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_dry_probe_never_calls_delivery(self):
        deliver = AsyncMock()
        with (
            patch.object(ringbearer, "deliver", deliver),
            patch.object(ringbearer, "log_capture"),
            patch("builtins.print"),
        ):
            result = await ringbearer.send_to_assistant(
                f"{ringbearer.DRY_RUN_PREFIX} bridge probe"
            )
        deliver.assert_not_awaited()
        self.assertEqual(result, "Dry run: received, not delivered to Telegram.")

    async def test_direct_delivery_remains_default(self):
        client = FakeTelegramClient()
        entity = object()
        with patch.multiple(
            ringbearer,
            TELEGRAM_ENABLED=True,
            NEW_TOPIC_PER_CAPTURE=False,
            RING_PREFIX="mic: ",
            tg_client=client,
            assistant_entity=entity,
        ):
            self.assertTrue(await ringbearer.deliver("hello"))
        self.assertEqual(client.requests, [])
        self.assertEqual(
            client.sent,
            [(entity, "mic: hello", {"parse_mode": None})],
        )

    async def test_topic_delivery_creates_then_replies_to_topic_root(self):
        client = FakeTelegramClient(topic_id=99)
        entity = object()
        with patch.multiple(
            ringbearer,
            TELEGRAM_ENABLED=True,
            NEW_TOPIC_PER_CAPTURE=True,
            RING_PREFIX="mic: ",
            tg_client=client,
            assistant_entity=entity,
        ):
            self.assertTrue(await ringbearer.deliver("hello world"))

        self.assertEqual(len(client.requests), 1)
        request = client.requests[0]
        self.assertIsInstance(request, CreateForumTopicRequest)
        self.assertIs(request.peer, entity)
        self.assertEqual(request.title, "hello world")
        self.assertEqual(
            client.sent,
            [(entity, "mic: hello world", {"parse_mode": None, "reply_to": 99})],
        )

    async def test_topic_creation_failure_does_not_fallback(self):
        client = FakeTelegramClient(create_error=RuntimeError("no topics"))
        with patch.multiple(
            ringbearer,
            TELEGRAM_ENABLED=True,
            NEW_TOPIC_PER_CAPTURE=True,
            tg_client=client,
            assistant_entity=object(),
        ):
            with self.assertRaisesRegex(RuntimeError, "no topics"):
                await ringbearer.deliver("hello")
        self.assertEqual(client.sent, [])

    async def test_missing_topic_root_does_not_fallback(self):
        client = FakeTelegramClient(topic_id=None)
        with patch.multiple(
            ringbearer,
            TELEGRAM_ENABLED=True,
            NEW_TOPIC_PER_CAPTURE=True,
            tg_client=client,
            assistant_entity=object(),
        ):
            with self.assertRaisesRegex(RuntimeError, "topic root"):
                await ringbearer.deliver("hello")
        self.assertEqual(client.sent, [])

    async def test_threaded_send_failure_is_exposed(self):
        client = FakeTelegramClient(send_error=RuntimeError("send failed"))
        with patch.multiple(
            ringbearer,
            TELEGRAM_ENABLED=True,
            NEW_TOPIC_PER_CAPTURE=True,
            tg_client=client,
            assistant_entity=object(),
        ):
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                await ringbearer.deliver("hello")


if __name__ == "__main__":
    unittest.main()
