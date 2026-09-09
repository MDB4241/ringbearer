"""Duplicate safety at the MCP, SQLite, and process boundaries. No network."""

import asyncio
import inspect
import selectors
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from mcp.server import MCPServer

import ringbearer as rb
from tests.test_topic_delivery import FakeTelegramClient, routing


class ReceiptTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "receipts.sqlite3"
        self.client = FakeTelegramClient()
        self.rows = []
        for p in (
            patch.object(rb, "RECEIPTS_DB", self.db, create=True),
            patch.multiple(
                rb, TELEGRAM_ENABLED=True, tg_client=self.client,
                NEW_TOPIC_PER_CAPTURE=False, RING_PREFIX="mic: ",
                **routing(object(), other=object()),
            ),
            patch.object(rb, "log_capture", self.rows.append),
            patch("builtins.print"),
        ):
            p.start()
            self.addCleanup(p.stop)

    async def call(self, message="one action", assistant="assistant", key="capture-1"):
        self.assertIn("capture_id", inspect.signature(rb.relay).parameters)
        return await rb.relay(message, assistant, capture_id=key)

    async def test_schema_and_legacy_calls_in_both_roster_modes(self):
        for roster in ({"assistant": "@a"}, {"assistant": "@a", "other": "@b"}):
            with self.subTest(roster=roster):
                server = MCPServer("receipt-schema-test")
                rb.register_capture_tool(server, roster)
                (tool,) = await server.list_tools()
                schema = tool.input_schema
                self.assertEqual(schema["required"], ["message"])
                self.assertIn("capture_id", schema["properties"])
                self.assertIsNone(schema["properties"]["capture_id"]["default"])
                self.assertEqual("assistant" in schema["properties"], len(roster) > 1)
                result = await server.call_tool(rb.TOOL_NAME, {"message": "legacy capture"})
                self.assertIn("Delivered", result.content[0].text)
                if len(roster) > 1:
                    result = await server.call_tool(
                        rb.TOOL_NAME, {"message": "legacy capture", "assistant": "other"}
                    )
                    self.assertIn("Delivered", result.content[0].text)
        self.assertEqual(len(self.client.sent), 3)
        self.assertFalse(self.db.exists())

    async def test_routing_prompt_does_not_turn_ambiguity_into_success(self):
        prompt = rb.ring_routing().lower()
        self.assertIn("ambiguous", prompt)
        self.assertIn("failed", prompt)
        self.assertIn("confirmed", prompt)

    async def test_mcp_dispatch_forwards_capture_id_in_both_roster_modes(self):
        for roster in ({"assistant": "@a"}, {"assistant": "@a", "other": "@b"}):
            server = MCPServer("keyed-dispatch-test")
            rb.register_capture_tool(server, roster)
            args = {"message": "one action", "capture_id": f"mcp-{len(roster)}"}
            if len(roster) > 1:
                args["assistant"] = "other"
            first = await server.call_tool(rb.TOOL_NAME, args)
            retry = await server.call_tool(rb.TOOL_NAME, args)
            self.assertIn("Delivered", first.content[0].text)
            self.assertIn("Duplicate", retry.content[0].text)
        self.assertEqual(len(self.client.sent), 2)

    async def test_null_id_uses_legacy_path_without_receipt_store(self):
        self.assertIn("Delivered", await self.call(key=None))
        self.assertFalse(self.db.exists())

    async def test_unkeyed_identical_messages_are_not_deduplicated(self):
        await rb.relay("repeat", "assistant")
        await rb.relay("repeat", "assistant")
        self.assertEqual(len(self.client.sent), 2)
        self.assertFalse(self.db.exists())

    async def test_sequential_duplicate_is_confirmed_and_suppressed(self):
        self.assertIn("Delivered", await self.call())
        self.assertIn("Duplicate", await self.call())
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(len(self.rows), 1)

    async def test_conflicts_never_change_message_route_or_delivery_settings(self):
        await self.call()
        for args in ({"message": "different"}, {"assistant": "other"}):
            self.assertIn("different", await self.call(**args))
        for setting, value in (
            ("ASSISTANT_ROSTER", {"assistant": "@changed"}),
            ("DELIVERY_CONTEXT", "one_shot"),
            ("NEW_TOPIC_PER_CAPTURE", True),
            ("RING_PREFIX", "changed: "),
        ):
            with patch.object(rb, setting, value):
                self.assertIn("different", await self.call())
        self.assertEqual(len(self.client.sent), 1)

    async def test_invalid_ids_fail_without_normalization_or_delivery(self):
        for key in ("", " ", " capture-1", "capture-1\n", "x" * 129, 123):
            with self.subTest(key=key):
                self.assertIn("Failed", await self.call(key=key))
        self.assertEqual(self.client.sent, [])
        self.assertFalse(self.db.exists())

    async def test_dry_run_does_not_claim_an_id(self):
        self.assertIn("Dry run", await self.call(message="DRYRUN: probe"))
        self.assertFalse(self.db.exists())
        self.assertIn("Delivered", await self.call())

    async def test_missing_client_and_disabled_delivery_are_retryable(self):
        for overrides in ({"tg_client": None}, {"TELEGRAM_ENABLED": False}):
            with self.subTest(overrides=overrides):
                key = next(iter(overrides))
                with patch.multiple(rb, **overrides):
                    self.assertIn("Failed", await self.call(key=key))
                self.assertIn("Delivered", await self.call(key=key))
        self.assertEqual(len(self.client.sent), 2)

    async def test_unknown_route_does_not_consume_id(self):
        self.assertIn("Unknown assistant", await self.call(assistant="unknown"))
        self.assertIn("Delivered", await self.call())

    async def test_concurrent_duplicate_reports_in_progress_without_replay(self):
        entered, release = asyncio.Event(), asyncio.Event()
        original = self.client.send_message

        async def held_send(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original(*args, **kwargs)

        self.client.send_message = held_send
        first = asyncio.create_task(self.call())
        try:
            await asyncio.wait_for(entered.wait(), 1)
            self.assertIn("Ambiguous", await asyncio.wait_for(self.call(), 1))
        finally:
            release.set()
            first_result = await first
        self.assertIn("Delivered", first_result)
        self.assertIn("Duplicate", await self.call())
        self.assertEqual(len(self.client.sent), 1)

    async def test_exceptions_after_send_never_permit_retry(self):
        for error in (ConnectionError("ack lost"), TimeoutError(), RuntimeError("unknown")):
            with self.subTest(error=type(error).__name__):
                self.client.send_error = error
                key = type(error).__name__
                self.assertIn("Ambiguous", await self.call(key=key))
                self.assertIn("Ambiguous", await self.call(key=key))
        self.assertEqual(len(self.client.sent), 3)

    async def test_actual_timeout_keeps_pending_claim(self):
        entered = asyncio.Event()

        async def held_send(*args, **kwargs):
            self.client.sent.append("attempt")
            entered.set()
            await asyncio.Event().wait()

        self.client.send_message = held_send
        with patch.object(rb, "CAPTURE_DELIVERY_TIMEOUT", 0.02):
            self.assertIn("Ambiguous", await self.call())
        self.assertTrue(entered.is_set())
        self.assertIn("Ambiguous", await self.call())
        self.assertEqual(len(self.client.sent), 1)

    async def test_cancelled_request_suppresses_retry(self):
        entered = asyncio.Event()

        async def held_send(*args, **kwargs):
            self.client.sent.append("attempt")
            entered.set()
            await asyncio.Event().wait()

        self.client.send_message = held_send
        task = asyncio.create_task(self.call())
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIn("Ambiguous", await self.call())
        self.assertEqual(len(self.client.sent), 1)

    async def test_cancelled_send_future_returns_ambiguous(self):
        self.client.send_error = asyncio.CancelledError()
        self.assertIn("Ambiguous", await self.call())
        self.assertIn("Ambiguous", await self.call())
        self.assertEqual(len(self.client.sent), 1)

    async def test_topic_creation_ambiguity_is_not_replayed(self):
        self.client.create_error = ConnectionError("topic ack lost")
        with patch.object(rb, "NEW_TOPIC_PER_CAPTURE", True):
            self.assertIn("Ambiguous", await self.call())
            self.assertIn("Ambiguous", await self.call())
        self.assertEqual(len(self.client.requests), 1)
        self.assertEqual(self.client.sent, [])

    async def test_receipt_commit_failure_after_send_is_suppressed(self):
        with patch.object(rb, "_receipt_finish", side_effect=sqlite3.OperationalError()):
            self.assertIn("Ambiguous", await self.call())
        self.assertIn("Ambiguous", await self.call())
        self.assertEqual(len(self.client.sent), 1)

    async def test_capture_log_failure_cannot_erase_confirmed_receipt(self):
        with patch.object(rb, "log_capture", side_effect=OSError("disk full")):
            result = await self.call()
        self.assertIn("Delivered", result)
        self.assertIn("log", result.lower())
        self.assertIn("Duplicate", await self.call())
        self.assertEqual(len(self.client.sent), 1)

    async def test_receipt_store_failure_fails_closed_without_leaking_error(self):
        with patch.object(rb, "_receipt_claim", side_effect=sqlite3.OperationalError("private")):
            result = await self.call()
        self.assertIn("Failed", result)
        self.assertNotIn("private", result)
        self.assertEqual(self.client.sent, [])

    async def test_corrupt_store_is_not_replaced(self):
        self.db.write_bytes(b"not a sqlite database")
        self.assertIn("Failed", await self.call())
        self.assertEqual(self.db.read_bytes(), b"not a sqlite database")
        self.assertEqual(self.client.sent, [])

    async def test_capacity_refuses_new_ids_without_evicting_old_receipts(self):
        with patch.object(rb, "RECEIPT_LIMIT", 2):
            await self.call(key="sent")
            self.client.send_error = ConnectionError("unknown")
            await self.call(key="pending")
            self.assertIn("full", await self.call(key="third"))
            self.assertIn("Duplicate", await self.call(key="sent"))
            self.assertIn("Ambiguous", await self.call(key="pending"))
        self.assertEqual(len(self.client.sent), 2)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM receipts").fetchone()[0], 2)

    async def test_receipts_are_private_minimal_and_private_before_sqlite_open(self):
        connect = sqlite3.connect

        def private_connect(path, **kwargs):
            self.assertEqual(stat.S_IMODE(Path(path).stat().st_mode), 0o600)
            return connect(path, **kwargs)

        with patch.object(rb.sqlite3, "connect", private_connect):
            await self.call(message="private transcript", key="private-capture-id")
        self.assertEqual(stat.S_IMODE(self.db.stat().st_mode), 0o600)
        with closing(connect(self.db)) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(receipts)")]
        self.assertEqual(columns, ["key_hash", "content_hash", "status"])
        contents = self.db.read_bytes()
        for private in (b"private transcript", b"private-capture-id", b"@assistant_bot"):
            self.assertNotIn(private, contents)


class ProcessReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)

    def command(self, mode):
        return [sys.executable, "-m", "tests.receipt_worker", str(self.state), mode]

    def run_child(self, mode):
        return subprocess.run(
            self.command(mode), capture_output=True, text=True, timeout=10, check=False,
            cwd=Path(__file__).resolve().parents[1],
        )

    def assert_one_send(self):
        self.assertEqual((self.state / "fake-sends.txt").read_text(), "send\n")

    def test_confirmed_receipt_survives_a_fresh_process(self):
        first, retry = self.run_child("send"), self.run_child("send")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn("Delivered", first.stdout)
        self.assertIn("Duplicate", retry.stdout)
        self.assert_one_send()

    def test_process_death_after_send_before_receipt_finish_suppresses_retry(self):
        first, retry = self.run_child("crash"), self.run_child("send")
        self.assertEqual(first.returncode, 23, first.stderr)
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn("Ambiguous", retry.stdout)
        self.assert_one_send()

    def test_concurrent_process_cannot_send_a_claimed_id(self):
        with subprocess.Popen(
            self.command("hold"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
            cwd=Path(__file__).resolve().parents[1],
        ) as first:
            try:
                assert first.stdout is not None
                with selectors.DefaultSelector() as ready:
                    ready.register(first.stdout, selectors.EVENT_READ)
                    self.assertTrue(ready.select(10), "worker never reached fake send")
                self.assertEqual(first.stdout.readline(), "SENDING\n")
                retry = self.run_child("send")
                self.assertEqual(retry.returncode, 0, retry.stderr)
                self.assertIn("Ambiguous", retry.stdout)
                output, errors = first.communicate("release\n", timeout=10)
                self.assertEqual(first.returncode, 0, errors)
                self.assertIn("Delivered", output)
            finally:
                if first.poll() is None:
                    first.kill()
                    first.communicate()
        self.assert_one_send()
