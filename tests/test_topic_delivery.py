import asyncio
import os
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon.tl.functions.messages import CreateForumTopicRequest
from telethon.tl.types import MessageActionTopicCreate, UpdateMessageID

from ringbearer import config, telegram
from ringbearer.route import mcp as route_mcp

REPO_ROOT = Path(__file__).resolve().parent.parent
# Every name that decides a dial, global or per route. Cleared from the
# environment before a subprocess reads them, so the checkout's own .env-less
# environment cannot colour the result.
DIAL_KEYS = (
    "NEW_TOPIC_PER_CAPTURE",
    "DELIVERY_CONTEXT",
    "MCP_TOPIC_PER_CAPTURE",
    "MCP_DELIVERY_CONTEXT",
    "WEBHOOK_TOPIC_PER_CAPTURE",
    "WEBHOOK_DELIVERY_CONTEXT",
)


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


@contextmanager
def routing(entity, **extra_entities):
    """Pin the routing globals, so tests are hermetic against whatever .env
    the checkout happens to carry. A context manager rather than patch
    kwargs because the names it pins live in two modules now: the roster and
    the delivery context are configuration, the resolved entities are
    connection state."""
    roster = {"assistant": "@assistant_bot"}
    entities = {"assistant": entity}
    for name, ent in extra_entities.items():
        roster[name] = f"@{name}_bot"
        entities[name] = ent
    with (
        patch.multiple(
            config,
            DEFAULT_ASSISTANT="assistant",
            ASSISTANT_ROSTER=roster,
            DELIVERY_CONTEXT="conversation",
        ),
        patch.object(telegram, "assistant_entities", entities),
    ):
        yield


class TopicTitleTests(unittest.TestCase):
    def test_normalizes_and_truncates_title(self):
        message = "  hello\n\tworld  " + "x" * 100
        title = telegram.topic_title(message)
        self.assertEqual(title[:11], "hello world")
        self.assertEqual(len(title), 80)

    def test_empty_title_uses_fallback(self):
        self.assertEqual(telegram.topic_title(" \n\t "), "Ring capture")


class ConfigurationTests(unittest.TestCase):
    def test_delivery_mode_label_describes_topic_mode(self):
        with patch.object(config, "NEW_TOPIC_PER_CAPTURE", True):
            self.assertEqual(telegram.delivery_mode_label(), "new topic per capture")

    def test_delivery_mode_label_describes_direct_mode(self):
        with patch.object(config, "NEW_TOPIC_PER_CAPTURE", False):
            self.assertEqual(
                telegram.delivery_mode_label(), "current Telegram conversation"
            )


class DeliveryContextTests(unittest.TestCase):
    def test_conversation_context_preserves_existing_message_shape(self):
        with patch.multiple(
            config, DELIVERY_CONTEXT="conversation", RING_PREFIX="mic: "
        ):
            self.assertEqual(telegram.format_delivery_message("hello"), "mic: hello")

    def test_one_shot_context_explains_the_noninteractive_contract(self):
        with patch.multiple(
            config, DELIVERY_CONTEXT="one_shot", RING_PREFIX="mic: "
        ):
            formatted = telegram.format_delivery_message("send the report")

        self.assertTrue(formatted.startswith("mic: [RING CAPTURE: ONE-SHOT]"))
        self.assertIn("may not see any reply", formatted)
        self.assertIn("execute it now without asking for confirmation", formatted)
        self.assertIn("Resolve minor ambiguity", formatted)
        self.assertIn("leave a concise blocker", formatted)
        self.assertTrue(formatted.endswith("Transcript:\nsend the report"))

    def test_one_shot_context_preserves_transcript_verbatim(self):
        transcript = "  Keep *this* exactly.\nSecond line [still literal].  "
        with patch.multiple(
            config, DELIVERY_CONTEXT="one_shot", RING_PREFIX="mic: "
        ):
            formatted = telegram.format_delivery_message(transcript)

        self.assertEqual(formatted.split("Transcript:\n", 1)[1], transcript)


class ParseAssistantsTests(unittest.TestCase):
    def test_blank_means_no_extras(self):
        self.assertEqual(config.parse_assistants(""), {})
        self.assertEqual(config.parse_assistants("  ,  "), {})

    def test_parses_pairs_and_coerces_numeric_chats(self):
        self.assertEqual(
            config.parse_assistants("plutus:@plutus_bot, qm:-100123"),
            {"plutus": "@plutus_bot", "qm": -100123},
        )

    def test_missing_chat_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "name:chat"):
            config.parse_assistants("plutus")
        with self.assertRaisesRegex(ValueError, "name:chat"):
            config.parse_assistants("plutus:")

    def test_non_token_name_is_rejected(self):
        for bad in ("Plutus:@x", "plu tus:@x", "9lives:@x", "plu-tus:@x"):
            with self.assertRaisesRegex(ValueError, "lowercase token"):
                config.parse_assistants(bad)

    def test_duplicate_name_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            config.parse_assistants("plutus:@a,plutus:@b")


class ToolSchemaTests(unittest.TestCase):
    """Progressive disclosure lives in the schema: no ASSISTANTS, no
    `assistant` argument — the phone-visible contract must not change for
    single-assistant installs."""

    def _schema(self, roster):
        from mcp.server import MCPServer

        server = MCPServer("schema-test")
        with patch.multiple(
            config, ASSISTANT_ROSTER=roster, DEFAULT_ASSISTANT="assistant"
        ):
            route_mcp.register_capture_tool(server, roster)
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
            patch.object(telegram, "deliver", deliver),
            patch.object(telegram, "log_capture"),
            patch("builtins.print"),
        ):
            result = await route_mcp.send_to_assistant(
                f"{config.DRY_RUN_PREFIX} bridge probe"
            )
        deliver.assert_not_awaited()
        self.assertEqual(result, "Dry run: received, not delivered to Telegram.")

    async def test_direct_delivery_remains_default(self):
        client = FakeTelegramClient()
        entity = object()
        with (
            routing(entity),
            patch.object(telegram, "tg_client", client),
            patch.multiple(
                config,
                TELEGRAM_ENABLED=True,
                NEW_TOPIC_PER_CAPTURE=False,
                RING_PREFIX="mic: ",
            ),
        ):
            self.assertTrue(await telegram.deliver("hello"))
        self.assertEqual(client.requests, [])
        self.assertEqual(
            client.sent,
            [(entity, "mic: hello", {"parse_mode": None, "reply_to": None})],
        )

    async def test_topic_delivery_creates_then_replies_to_topic_root(self):
        client = FakeTelegramClient(topic_id=99)
        entity = object()
        with (
            routing(entity),
            patch.object(telegram, "tg_client", client),
            patch.multiple(
                config,
                TELEGRAM_ENABLED=True,
                NEW_TOPIC_PER_CAPTURE=True,
                RING_PREFIX="mic: ",
            ),
        ):
            self.assertTrue(await telegram.deliver("hello world"))

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
        with (
            routing(object()),
            patch.object(telegram, "tg_client", client),
            patch.multiple(
                config, TELEGRAM_ENABLED=True, NEW_TOPIC_PER_CAPTURE=True
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "no topics"):
                await telegram.deliver("hello")
        self.assertEqual(client.sent, [])

    async def test_missing_topic_root_does_not_fallback(self):
        client = FakeTelegramClient(topic_id=None)
        with (
            routing(object()),
            patch.object(telegram, "tg_client", client),
            patch.multiple(
                config, TELEGRAM_ENABLED=True, NEW_TOPIC_PER_CAPTURE=True
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread identifier"):
                await telegram.deliver("hello")
        self.assertEqual(client.sent, [])

    async def test_threaded_send_failure_is_exposed(self):
        client = FakeTelegramClient(send_error=RuntimeError("send failed"))
        with (
            routing(object()),
            patch.object(telegram, "tg_client", client),
            patch.multiple(
                config, TELEGRAM_ENABLED=True, NEW_TOPIC_PER_CAPTURE=True
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "send failed"):
                await telegram.deliver("hello")

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
        with patch.object(telegram, "tg_client", client):
            self.assertEqual(await telegram.create_topic("a title", object()), 7)

    async def test_topic_id_prefers_correlated_update_message_id(self):
        # The UpdateMessageID whose random_id echoes our request is the
        # authoritative mapping (the same one Telethon's parser uses) — it
        # must work even when no service message appears at all.
        client = FakeTelegramClient(
            raw_response_factory=lambda req: SimpleNamespace(
                updates=[UpdateMessageID(id=321, random_id=req.random_id)]
            )
        )
        with patch.object(telegram, "tg_client", client):
            self.assertEqual(await telegram.create_topic("a title", object()), 321)


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    """The `assistant` argument routes; it never rewrites."""

    async def test_named_assistant_routes_to_its_chat(self):
        client = FakeTelegramClient()
        default_entity, plutus_entity = object(), object()
        rows = []
        with (
            routing(default_entity, plutus=plutus_entity),
            patch.object(telegram, "tg_client", client),
            patch.multiple(
                config,
                TELEGRAM_ENABLED=True,
                NEW_TOPIC_PER_CAPTURE=False,
                RING_PREFIX="mic: ",
            ),
            patch.object(telegram, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            result = await telegram.relay("ask plutus about my portfolio", "plutus")
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
            routing(default_entity, plutus=object()),
            patch.object(telegram, "tg_client", client),
            patch.multiple(
                config,
                TELEGRAM_ENABLED=True,
                NEW_TOPIC_PER_CAPTURE=False,
                RING_PREFIX="mic: ",
            ),
            patch.object(telegram, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            await telegram.relay("hello", "assistant")
        self.assertEqual(client.sent[0][0], default_entity)
        self.assertEqual(rows[0]["assistant"], "assistant")

    async def test_unknown_assistant_sends_nothing_and_names_the_valid(self):
        client = FakeTelegramClient()
        rows = []
        with (
            routing(object(), plutus=object()),
            patch.object(telegram, "tg_client", client),
            patch.object(config, "TELEGRAM_ENABLED", True),
            patch.object(telegram, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            result = await telegram.relay("hello", "ghost")
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
            routing(default_entity, plutus=plutus_entity),
            patch.object(telegram, "tg_client", client),
            patch.multiple(
                config,
                TELEGRAM_ENABLED=True,
                NEW_TOPIC_PER_CAPTURE=True,
                RING_PREFIX="mic: ",
            ),
            patch.object(telegram, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            await telegram.relay("hello", "plutus")
        self.assertIs(client.requests[0].peer, plutus_entity)
        self.assertEqual(
            client.sent,
            [(plutus_entity, "mic: hello", {"parse_mode": None, "reply_to": 55})],
        )

    async def test_dry_run_row_carries_the_assistant(self):
        rows = []
        with (
            routing(object(), plutus=object()),
            patch.object(telegram, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            result = await telegram.relay(
                f"{config.DRY_RUN_PREFIX} probe", "plutus"
            )
        self.assertEqual(result, "Dry run: received, not delivered to Telegram.")
        self.assertEqual(rows[0]["assistant"], "plutus")
        self.assertTrue(rows[0]["dry_run"])


class DurableLoggingTests(unittest.IsolatedAsyncioTestCase):
    """The capture row is the only copy of the transcript — it must be
    written no matter how delivery dies."""

    async def test_cancellation_still_logs_capture(self):
        """A real cancellation — the request dropped, the server shutting
        down — logs the only copy of the transcript and then propagates."""
        rows = []
        started = asyncio.Event()

        async def hangs(message, assistant=None):
            started.set()
            await asyncio.Event().wait()

        with (
            patch.object(telegram, "deliver", hangs),
            patch.object(telegram, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            task = asyncio.create_task(route_mcp.send_to_assistant("hello"))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["forwarded"])
        self.assertIn("CancelledError", rows[0]["error"])

    async def test_a_cancelled_send_future_is_a_failed_delivery_not_a_dropped_request(self):
        """Telethon cancels the send's future when it tears the connection
        down mid-send. Nobody cancelled the request, so the ring gets a
        delivery failure and the supervisor gets nudged — not a bare
        CancelledError out of the tool handler."""
        rows = []
        deliver = AsyncMock(side_effect=asyncio.CancelledError)
        with (
            patch.object(telegram, "deliver", deliver),
            patch.object(telegram, "log_capture", rows.append),
            patch.object(telegram, "request_recheck") as recheck,
            patch("builtins.print"),
        ):
            result = await route_mcp.send_to_assistant("hello")
        self.assertIn("delivery failed", result)
        self.assertIn("tore the connection down", result)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["forwarded"])
        self.assertIn("tore the connection down", rows[0]["error"])
        recheck.assert_called_once()

    async def test_delivery_error_still_logs_capture(self):
        rows = []
        deliver = AsyncMock(side_effect=RuntimeError("boom"))
        with (
            patch.object(telegram, "deliver", deliver),
            patch.object(telegram, "log_capture", rows.append),
            patch("builtins.print"),
        ):
            result = await route_mcp.send_to_assistant("hello")
        self.assertIn("delivery failed", result)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["forwarded"])
        self.assertIn("boom", rows[0]["error"])


class RouteDialsTests(unittest.IsolatedAsyncioTestCase):
    """Each route delivers on its own dials, and setting one
    route's dials never moves the other's.

    The globals are pinned to the opposite of whatever the route under test
    wants, so a route that quietly read a global instead of its own override
    would fail every case rather than pass by coincidence.
    """

    OTHER = {"conversation": "one_shot", "one_shot": "conversation"}

    async def sent_through(self, source, **dials):
        """Relay one capture through `source` with these dial values pinned.

        Parameters:
          source (str): "mcp" or "webhook".
          dials: config names to patch (globals and per-route overrides).

        Returns: the FakeTelegramClient, holding the topic requests and the
          message Telegram was asked to send.
        """
        client = FakeTelegramClient(topic_id=77)
        with (
            routing(object()),
            patch.object(telegram, "tg_client", client),
            patch.multiple(
                config, TELEGRAM_ENABLED=True, RING_PREFIX="mic: ", **dials
            ),
            patch.object(telegram, "log_capture", lambda row: None),
            patch("builtins.print"),
        ):
            await telegram.relay("hello", "assistant", source=source)
        return client

    def assert_saw(self, client, *, topic, context):
        """Assert the send matches one route's dials, both knobs at once."""
        if topic:
            self.assertEqual(len(client.requests), 1)
            self.assertEqual(client.sent[0][2]["reply_to"], 77)
        else:
            self.assertEqual(client.requests, [])
            self.assertIsNone(client.sent[0][2]["reply_to"])
        text = client.sent[0][1]
        if context == "conversation":
            self.assertEqual(text, "mic: hello")
        else:
            self.assertTrue(text.startswith("mic: [RING CAPTURE: ONE-SHOT]"))
            self.assertTrue(text.endswith("Transcript:\nhello"))

    async def test_each_route_delivers_on_its_own_dials(self):
        for source, other in (("webhook", "mcp"), ("mcp", "webhook")):
            for topic in (True, False):
                for context in ("conversation", "one_shot"):
                    with self.subTest(source=source, topic=topic, context=context):
                        # This route wants (topic, context); the globals and
                        # the other route are pinned to the opposite of both.
                        dials = {
                            "NEW_TOPIC_PER_CAPTURE": not topic,
                            "DELIVERY_CONTEXT": self.OTHER[context],
                            f"{source.upper()}_TOPIC_PER_CAPTURE": topic,
                            f"{source.upper()}_DELIVERY_CONTEXT": context,
                            f"{other.upper()}_TOPIC_PER_CAPTURE": not topic,
                            f"{other.upper()}_DELIVERY_CONTEXT": self.OTHER[context],
                        }
                        self.assert_saw(
                            await self.sent_through(source, **dials),
                            topic=topic,
                            context=context,
                        )
                        # Same configuration, the other door: it must still
                        # deliver on its own values.
                        self.assert_saw(
                            await self.sent_through(other, **dials),
                            topic=not topic,
                            context=self.OTHER[context],
                        )

    async def test_an_unset_override_falls_back_to_the_global(self):
        for source, other in (("webhook", "mcp"), ("mcp", "webhook")):
            for topic in (True, False):
                for context in ("conversation", "one_shot"):
                    with self.subTest(source=source, topic=topic, context=context):
                        # Neither of this route's keys is set; the other route
                        # holds the opposite of both, and must not leak.
                        dials = {
                            "NEW_TOPIC_PER_CAPTURE": topic,
                            "DELIVERY_CONTEXT": context,
                            f"{source.upper()}_TOPIC_PER_CAPTURE": None,
                            f"{source.upper()}_DELIVERY_CONTEXT": None,
                            f"{other.upper()}_TOPIC_PER_CAPTURE": not topic,
                            f"{other.upper()}_DELIVERY_CONTEXT": self.OTHER[context],
                        }
                        with patch.multiple(config, **dials):
                            self.assertEqual(
                                telegram.dials_for(source),
                                telegram.Dials(topic, context),
                            )
                        self.assert_saw(
                            await self.sent_through(source, **dials),
                            topic=topic,
                            context=context,
                        )

    async def test_one_key_overridden_leaves_the_other_on_the_global(self):
        """The two dials are independent: overriding the topic setting for a
        route must not drag its delivery context along."""
        dials = {
            "NEW_TOPIC_PER_CAPTURE": False,
            "DELIVERY_CONTEXT": "one_shot",
            "WEBHOOK_TOPIC_PER_CAPTURE": True,
            "WEBHOOK_DELIVERY_CONTEXT": None,
            "MCP_TOPIC_PER_CAPTURE": None,
            "MCP_DELIVERY_CONTEXT": None,
        }
        self.assert_saw(
            await self.sent_through("webhook", **dials), topic=True, context="one_shot"
        )
        self.assert_saw(
            await self.sent_through("mcp", **dials), topic=False, context="one_shot"
        )

    async def test_a_direct_deliver_outside_a_relay_uses_the_globals(self):
        """`deliver` is called on its own by tests and by nothing else; with
        no relay to set the dials it reads the install-wide pair."""
        client = FakeTelegramClient(topic_id=77)
        with (
            routing(object()),
            patch.object(telegram, "tg_client", client),
            patch.multiple(
                config,
                TELEGRAM_ENABLED=True,
                RING_PREFIX="mic: ",
                NEW_TOPIC_PER_CAPTURE=True,
                DELIVERY_CONTEXT="conversation",
                WEBHOOK_TOPIC_PER_CAPTURE=False,
                MCP_TOPIC_PER_CAPTURE=False,
            ),
        ):
            self.assertEqual(telegram.current_dials(), telegram.Dials(True, "conversation"))
            await telegram.deliver("hello")
        self.assert_saw(client, topic=True, context="conversation")

    def test_the_delivery_mode_label_can_speak_for_one_route(self):
        with patch.multiple(
            config,
            NEW_TOPIC_PER_CAPTURE=False,
            WEBHOOK_TOPIC_PER_CAPTURE=True,
            MCP_TOPIC_PER_CAPTURE=None,
        ):
            self.assertEqual(
                telegram.delivery_mode_label(telegram.dials_for("webhook")),
                "new topic per capture",
            )
            self.assertEqual(
                telegram.delivery_mode_label(telegram.dials_for("mcp")),
                "current Telegram conversation",
            )


class DialValidationTests(unittest.TestCase):
    """The per-route keys are read and checked at import, like the globals
    they override: a value that is neither of the two legal ones stops the
    process while someone is still looking at a terminal."""

    def _dials(self, **env_overrides):
        env = {k: v for k, v in os.environ.items() if k not in DIAL_KEYS}
        env["TELEGRAM_ENABLED"] = "false"
        env.update(env_overrides)
        return subprocess.run(
            [
                sys.executable,
                "-c",
                "from ringbearer import telegram;"
                "print(telegram.dials_for('mcp'));"
                "print(telegram.dials_for('webhook'))",
            ],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_a_bad_flag_value_exits_naming_the_key(self):
        result = self._dials(WEBHOOK_TOPIC_PER_CAPTURE="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WEBHOOK_TOPIC_PER_CAPTURE", result.stderr)
        self.assertIn("'true' or 'false'", result.stderr)

    def test_a_bad_context_value_exits_naming_the_key(self):
        result = self._dials(MCP_DELIVERY_CONTEXT="chatty")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("MCP_DELIVERY_CONTEXT", result.stderr)
        self.assertIn("'conversation' or 'one_shot'", result.stderr)

    def test_the_environment_reaches_the_right_route(self):
        result = self._dials(
            MCP_TOPIC_PER_CAPTURE="true", WEBHOOK_DELIVERY_CONTEXT="one_shot"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        mcp_dials, webhook_dials = result.stdout.strip().splitlines()
        self.assertEqual(
            mcp_dials, "Dials(topic_per_capture=True, delivery_context='conversation')"
        )
        self.assertEqual(
            webhook_dials,
            "Dials(topic_per_capture=False, delivery_context='one_shot')",
        )

    def test_unset_keys_leave_both_routes_on_the_globals(self):
        result = self._dials(NEW_TOPIC_PER_CAPTURE="true", DELIVERY_CONTEXT="one_shot")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip().splitlines(),
            ["Dials(topic_per_capture=True, delivery_context='one_shot')"] * 2,
        )


if __name__ == "__main__":
    unittest.main()
