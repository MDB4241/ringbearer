import asyncio
import unittest
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
        DELIVERY_CONTEXT="conversation",
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


class DeliveryContextTests(unittest.TestCase):
    def test_conversation_context_preserves_existing_message_shape(self):
        with patch.multiple(
            ringbearer, DELIVERY_CONTEXT="conversation", RING_PREFIX="mic: "
        ):
            self.assertEqual(ringbearer.format_delivery_message("hello"), "mic: hello")

    def test_one_shot_context_explains_the_noninteractive_contract(self):
        with patch.multiple(
            ringbearer, DELIVERY_CONTEXT="one_shot", RING_PREFIX="mic: "
        ):
            formatted = ringbearer.format_delivery_message("send the report")

        self.assertTrue(formatted.startswith("mic: [RING CAPTURE: ONE-SHOT]"))
        self.assertIn("may not see any reply", formatted)
        self.assertIn("execute it now without asking for confirmation", formatted)
        self.assertIn("Resolve minor ambiguity", formatted)
        self.assertIn("leave a concise blocker", formatted)
        self.assertTrue(formatted.endswith("Transcript:\nsend the report"))

    def test_one_shot_context_preserves_transcript_verbatim(self):
        transcript = "  Keep *this* exactly.\nSecond line [still literal].  "
        with patch.multiple(
            ringbearer, DELIVERY_CONTEXT="one_shot", RING_PREFIX="mic: "
        ):
            formatted = ringbearer.format_delivery_message(transcript)

        self.assertEqual(formatted.split("Transcript:\n", 1)[1], transcript)


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
    `assistant` argument — the phone-visible contract must not change for
    single-assistant installs."""

    def _schema(self, roster):
        from mcp.server import MCPServer

        server = MCPServer("schema-test")
        with patch.multiple(
            ringbearer, ASSISTANT_ROSTER=roster, DEFAULT_ASSISTANT="assistant"
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
