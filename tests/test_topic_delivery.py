import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.tl.functions.messages import CreateForumTopicRequest
from telethon.tl.types import MessageActionTopicCreate, UpdateMessageID

import ringbearer


class FakeTelegramClient:
    def __init__(
        self,
        *,
        topic_id=42,
        create_error=None,
        send_error=None,
        raw_response_factory=None,
    ):
        self.topic_id = topic_id
        self.create_error = create_error
        self.send_error = send_error
        self.raw_response_factory = raw_response_factory
        self.requests = []
        self.sent = []

    async def __call__(self, request):
        self.requests.append(request)
        if self.create_error:
            raise self.create_error
        if self.raw_response_factory is not None:
            return self.raw_response_factory(request)
        message = SimpleNamespace(
            id=self.topic_id,
            action=MessageActionTopicCreate(title=request.title, icon_color=0x6FB9F0),
        )
        return SimpleNamespace(updates=[SimpleNamespace(message=message)])

    async def send_message(self, entity, text, **kwargs):
        self.sent.append((entity, text, kwargs))
        if self.send_error:
            raise self.send_error


def routing(entity, **extra_entities):
    """Patch kwargs pinning the routing globals, so tests are hermetic
    against whatever .env the checkout happens to carry."""
    roster = {"assistant": "@assistant_bot"}
    entities = {"assistant": entity}
    for name, ent in extra_entities.items():
        roster[name] = f"@{name}_bot"
        entities[name] = ent
    return dict(
        DEFAULT_ASSISTANT="assistant",
        ASSISTANT_ROSTER=roster,
        assistant_entities=entities,
    )


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


class ParseAssistantsTests(unittest.TestCase):
    def test_blank_means_no_extras(self):
        self.assertEqual(ringbearer.parse_assistants(""), {})
        self.assertEqual(ringbearer.parse_assistants("  ,  "), {})

    def test_parses_pairs_and_coerces_numeric_chats(self):
        self.assertEqual(
            ringbearer.parse_assistants("plutus:@plutus_bot, qm:-100123"),
            {"plutus": "@plutus_bot", "qm": -100123},
        )

    def test_missing_chat_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "name:chat"):
            ringbearer.parse_assistants("plutus")
        with self.assertRaisesRegex(ValueError, "name:chat"):
            ringbearer.parse_assistants("plutus:")

    def test_non_token_name_is_rejected(self):
        for bad in ("Plutus:@x", "plu tus:@x", "9lives:@x", "plu-tus:@x"):
            with self.assertRaisesRegex(ValueError, "lowercase token"):
                ringbearer.parse_assistants(bad)

    def test_duplicate_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            ringbearer.parse_assistants("plutus:@a,plutus:@b")


class ToolSchemaTests(unittest.TestCase):
    """Progressive disclosure lives in the schema: no ASSISTANTS, no
    `assistant` argument; no topic mode, no `thread` argument — the
    phone-visible contract must not change for installs that configured
    neither feature."""

    def _schema(self, roster, topics=False):
        from mcp.server import MCPServer

        server = MCPServer("schema-test")
        with patch.multiple(
            ringbearer,
            ASSISTANT_ROSTER=roster,
            DEFAULT_ASSISTANT="assistant",
            NEW_TOPIC_PER_CAPTURE=topics,
        ):
            ringbearer.register_capture_tool(server, roster)
        (tool,) = asyncio.run(server.list_tools())
        return tool.input_schema

    def test_single_assistant_schema_is_message_only(self):
        schema = self._schema({"assistant": "@a"})
        self.assertEqual(list(schema["properties"]), ["message"])

    def test_multi_assistant_schema_gains_optional_enum(self):
        schema = self._schema({"assistant": "@a", "plutus": "@p"})
        arg = schema["properties"]["assistant"]
        self.assertEqual(arg["enum"], ["assistant", "plutus"])
        self.assertEqual(arg["default"], "assistant")
        self.assertEqual(schema["required"], ["message"])
        self.assertNotIn("thread", schema["properties"])

    def test_topic_mode_schema_gains_optional_thread(self):
        schema = self._schema({"assistant": "@a"}, topics=True)
        self.assertEqual(list(schema["properties"]), ["message", "thread"])
        arg = schema["properties"]["thread"]
        self.assertEqual(arg["enum"], ["new", "continue"])
        self.assertEqual(arg["default"], "new")
        self.assertEqual(schema["required"], ["message"])

    def test_topic_mode_multi_assistant_schema_has_both_arguments(self):
        schema = self._schema({"assistant": "@a", "plutus": "@p"}, topics=True)
        self.assertEqual(
            list(schema["properties"]), ["message", "assistant", "thread"]
        )
        self.assertEqual(schema["properties"]["assistant"]["enum"], ["assistant", "plutus"])
        self.assertEqual(schema["properties"]["thread"]["enum"], ["new", "continue"])
        self.assertEqual(schema["required"], ["message"])


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
            **routing(entity),
        ):
            self.assertTrue(await ringbearer.deliver("hello"))
        self.assertEqual(client.requests, [])
        self.assertEqual(
            client.sent,
            [(entity, "mic: hello", {"parse_mode": None, "reply_to": None})],
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
            **routing(entity),
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
            **routing(object()),
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
            **routing(object()),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread identifier"):
                await ringbearer.deliver("hello")
        self.assertEqual(client.sent, [])

    async def test_threaded_send_failure_is_exposed(self):
        client = FakeTelegramClient(send_error=RuntimeError("send failed"))
        with patch.multiple(
            ringbearer,
            TELEGRAM_ENABLED=True,
            NEW_TOPIC_PER_CAPTURE=True,
            tg_client=client,
            **routing(object()),
        ):
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                await ringbearer.deliver("hello")

    async def test_topic_id_extracted_from_updateshort(self):
        # UpdateShort carries a single .update instead of an .updates list —
        # a successfully created topic in that shape must not read as missing.
        short = SimpleNamespace(
            update=SimpleNamespace(
                message=SimpleNamespace(
                    id=7,
                    action=MessageActionTopicCreate(title="t", icon_color=0),
                )
            )
        )
        client = FakeTelegramClient(raw_response_factory=lambda req: short)
        with patch.object(ringbearer, "tg_client", client):
            self.assertEqual(await ringbearer.create_topic("a title", object()), 7)

    async def test_topic_id_prefers_correlated_update_message_id(self):
        # The UpdateMessageID whose random_id echoes our request is the
        # authoritative mapping (the same one Telethon's parser uses) — it
        # must work even when no service message appears at all.
        client = FakeTelegramClient(
            raw_response_factory=lambda req: SimpleNamespace(
                updates=[UpdateMessageID(id=321, random_id=req.random_id)]
            )
        )
        with patch.object(ringbearer, "tg_client", client):
            self.assertEqual(await ringbearer.create_topic("a title", object()), 321)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    """The `assistant` argument routes; it never rewrites."""

    async def test_named_assistant_routes_to_its_chat(self):
        client = FakeTelegramClient()
        default_entity, plutus_entity = object(), object()
        rows = []
        with (
            patch.multiple(
                ringbearer,
                TELEGRAM_ENABLED=True,
                NEW_TOPIC_PER_CAPTURE=False,
                RING_PREFIX="mic: ",
                tg_client=client,
                **routing(default_entity, plutus=plutus_entity),
            ),
            patch.object(ringbearer, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            result = await ringbearer.relay("ask plutus about my portfolio", "plutus")
        self.assertEqual(
            client.sent,
            [(
                plutus_entity,
                "mic: ask plutus about my portfolio",
                {"parse_mode": None, "reply_to": None},
            )],
        )
        self.assertEqual(result, "Delivered. plutus will reply in Telegram.")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["assistant"], "plutus")
        self.assertTrue(rows[0]["forwarded"])

    async def test_default_assistant_when_argument_omitted(self):
        client = FakeTelegramClient()
        default_entity = object()
        rows = []
        with (
            patch.multiple(
                ringbearer,
                TELEGRAM_ENABLED=True,
                NEW_TOPIC_PER_CAPTURE=False,
                RING_PREFIX="mic: ",
                tg_client=client,
                **routing(default_entity, plutus=object()),
            ),
            patch.object(ringbearer, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            await ringbearer.relay("hello", "assistant")
        self.assertEqual(client.sent[0][0], default_entity)
        self.assertEqual(rows[0]["assistant"], "assistant")

    async def test_unknown_assistant_sends_nothing_and_names_the_valid(self):
        client = FakeTelegramClient()
        rows = []
        with (
            patch.multiple(
                ringbearer,
                TELEGRAM_ENABLED=True,
                tg_client=client,
                **routing(object(), plutus=object()),
            ),
            patch.object(ringbearer, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            result = await ringbearer.relay("hello", "ghost")
        self.assertEqual(client.sent, [])
        self.assertIn("Unknown assistant 'ghost'", result)
        self.assertIn("assistant, plutus", result)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["forwarded"])
        self.assertEqual(rows[0]["assistant"], "ghost")
        self.assertIn("unknown assistant", rows[0]["error"])

    async def test_topic_mode_creates_topic_in_target_chat(self):
        client = FakeTelegramClient(topic_id=55)
        default_entity, plutus_entity = object(), object()
        rows = []
        with (
            patch.multiple(
                ringbearer,
                TELEGRAM_ENABLED=True,
                NEW_TOPIC_PER_CAPTURE=True,
                RING_PREFIX="mic: ",
                tg_client=client,
                **routing(default_entity, plutus=plutus_entity),
            ),
            patch.object(ringbearer, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            await ringbearer.relay("hello", "plutus")
        self.assertIs(client.requests[0].peer, plutus_entity)
        self.assertEqual(
            client.sent,
            [(plutus_entity, "mic: hello", {"parse_mode": None, "reply_to": 55})],
        )

    async def test_dry_run_row_carries_the_assistant(self):
        rows = []
        with (
            patch.multiple(ringbearer, **routing(object(), plutus=object())),
            patch.object(ringbearer, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            result = await ringbearer.relay(
                f"{ringbearer.DRY_RUN_PREFIX} probe", "plutus"
            )
        self.assertEqual(result, "Dry run: received, not delivered to Telegram.")
        self.assertEqual(rows[0]["assistant"], "plutus")
        self.assertTrue(rows[0]["dry_run"])


class ThreadContinuationTests(unittest.IsolatedAsyncioTestCase):
    """`thread="continue"` files into the last topic this bridge created —
    from its own durable state, never from reading Telegram."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.pointer_file = Path(self._tmp.name) / "last_topics.json"
        patcher = patch.object(ringbearer, "LAST_TOPICS", self.pointer_file)
        patcher.start()
        self.addCleanup(patcher.stop)

    def seed(self, pointers):
        self.pointer_file.write_text(json.dumps(pointers))

    def topic_mode(self, client, **extra_entities):
        return patch.multiple(
            ringbearer,
            TELEGRAM_ENABLED=True,
            NEW_TOPIC_PER_CAPTURE=True,
            RING_PREFIX="mic: ",
            tg_client=client,
            **routing(self.default_entity, **extra_entities),
        )

    def setUpEntities(self):
        self.default_entity = object()

    async def asyncSetUp(self):
        self.setUpEntities()

    async def test_continue_replies_to_stored_topic(self):
        self.seed({"assistant": 41})
        client = FakeTelegramClient()
        with self.topic_mode(client):
            self.assertTrue(await ringbearer.deliver("okay so", thread="continue"))
        self.assertEqual(client.requests, [])
        self.assertEqual(
            client.sent,
            [(self.default_entity, "mic: okay so", {"parse_mode": None, "reply_to": 41})],
        )

    async def test_continue_is_per_assistant(self):
        self.seed({"assistant": 41, "plutus": 77})
        client = FakeTelegramClient()
        plutus_entity = object()
        with self.topic_mode(client, plutus=plutus_entity):
            await ringbearer.deliver("okay so", "plutus", "continue")
        self.assertEqual(client.requests, [])
        self.assertEqual(
            client.sent,
            [(plutus_entity, "mic: okay so", {"parse_mode": None, "reply_to": 77})],
        )

    async def test_create_stores_pointer_durably(self):
        client = FakeTelegramClient(topic_id=99)
        with self.topic_mode(client):
            await ringbearer.deliver("hello")
        self.assertEqual(json.loads(self.pointer_file.read_text()), {"assistant": 99})
        self.assertEqual(self.pointer_file.stat().st_mode & 0o777, 0o600)
        self.assertEqual(ringbearer.load_last_topic("assistant"), 99)

    async def test_continue_without_pointer_falls_back_to_new(self):
        client = FakeTelegramClient(topic_id=42)
        with self.topic_mode(client):
            self.assertTrue(await ringbearer.deliver("hello", thread="continue"))
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.sent[0][2]["reply_to"], 42)
        self.assertEqual(ringbearer.load_last_topic("assistant"), 42)

    async def test_corrupt_pointer_file_degrades_to_new(self):
        self.pointer_file.write_text("not json {{")
        client = FakeTelegramClient(topic_id=42)
        with self.topic_mode(client):
            self.assertTrue(await ringbearer.deliver("hello", thread="continue"))
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.sent[0][2]["reply_to"], 42)

    async def test_junk_thread_value_means_new(self):
        self.seed({"assistant": 41})
        client = FakeTelegramClient(topic_id=42)
        with self.topic_mode(client):
            await ringbearer.deliver("hello", thread="followup")
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(client.sent[0][2]["reply_to"], 42)

    async def test_dangling_pointer_names_recovery(self):
        self.seed({"assistant": 41})
        client = FakeTelegramClient(send_error=RuntimeError("MSG_ID_INVALID"))
        with self.topic_mode(client):
            with self.assertRaisesRegex(RuntimeError, 'retry with thread="new"'):
                await ringbearer.deliver("okay so", thread="continue")
        self.assertEqual(client.requests, [])

    async def test_dm_mode_ignores_thread(self):
        self.seed({"assistant": 41})
        client = FakeTelegramClient()
        with patch.multiple(
            ringbearer,
            TELEGRAM_ENABLED=True,
            NEW_TOPIC_PER_CAPTURE=False,
            RING_PREFIX="mic: ",
            tg_client=client,
            **routing(self.default_entity),
        ):
            self.assertTrue(await ringbearer.deliver("hello", thread="continue"))
        self.assertEqual(client.requests, [])
        self.assertEqual(
            client.sent,
            [(self.default_entity, "mic: hello", {"parse_mode": None, "reply_to": None})],
        )


class ThreadRowTests(unittest.IsolatedAsyncioTestCase):
    """The captures row records the requested thread value verbatim in
    topic mode, and stays untouched in DM mode."""

    async def _relay(self, message, thread, topics):
        rows = []
        with (
            patch.multiple(ringbearer, NEW_TOPIC_PER_CAPTURE=topics, **routing(object())),
            patch.object(ringbearer, "deliver", AsyncMock(return_value=True)),
            patch.object(ringbearer, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            await ringbearer.relay(message, "assistant", thread)
        return rows[0]

    async def test_topic_mode_row_carries_requested_thread_verbatim(self):
        row = await self._relay("hello", "followup", topics=True)
        self.assertEqual(row["thread"], "followup")

    async def test_dry_run_row_carries_thread_in_topic_mode(self):
        row = await self._relay(f"{ringbearer.DRY_RUN_PREFIX} probe", "continue", topics=True)
        self.assertEqual(row["thread"], "continue")
        self.assertTrue(row["dry_run"])

    async def test_dm_mode_row_has_no_thread_field(self):
        row = await self._relay("hello", "continue", topics=False)
        self.assertNotIn("thread", row)


class DurableLoggingTests(unittest.IsolatedAsyncioTestCase):
    """The capture row is the only copy of the transcript — it must be
    written no matter how delivery dies."""

    async def test_cancellation_still_logs_capture(self):
        rows = []
        deliver = AsyncMock(side_effect=asyncio.CancelledError)
        with (
            patch.object(ringbearer, "deliver", deliver),
            patch.object(ringbearer, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            with self.assertRaises(asyncio.CancelledError):
                await ringbearer.send_to_assistant("hello")
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["forwarded"])
        self.assertIn("CancelledError", rows[0]["error"])

    async def test_delivery_error_still_logs_capture(self):
        rows = []
        deliver = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(ringbearer, "deliver", deliver),
            patch.object(ringbearer, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            result = await ringbearer.send_to_assistant("hello")
        self.assertIn("delivery failed", result)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["forwarded"])
        self.assertIn("boom", rows[0]["error"])


if __name__ == "__main__":
    unittest.main()
