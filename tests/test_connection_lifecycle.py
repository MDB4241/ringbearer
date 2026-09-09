"""Regressions for half-dead Telethon clients and dead supervision."""

import asyncio
import signal
import subprocess
import sys
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import Response

import ringbearer as rb


class Client:
    def __init__(self, connected=True):
        self.connected = connected
        self.events = []

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.events.append("disconnect")
        self.connected = False

    async def connect(self):
        self.events.append("connect")
        self.connected = True

    async def __call__(self, request):
        self.events.append("ping")
        if not self.connected:
            raise ConnectionError("Cannot send requests while disconnected")
        return object()

    async def send_message(self, *args, **kwargs):
        self.events.append("send")
        return SimpleNamespace(id=123)


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old, self.new = Client(), Client(False)
        self.patcher = patch.multiple(
            rb,
            tg_client=self.old,
            tg_client_lock=asyncio.Lock(),
            tg_health=rb.ConnectionHealth(),
            TELEGRAM_ENABLED=True,
            NEW_TOPIC_PER_CAPTURE=False,
            DEFAULT_ASSISTANT="assistant",
            assistant_entities={"assistant": object()},
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    async def test_rebuild_closes_old_before_connecting_new(self):
        def factory(**kwargs):
            self.assertEqual(self.old.events, ["disconnect"])
            return self.new

        with patch.object(rb, "make_tg_client", factory):
            await rb.verify_connection(rebuild=True)
        self.assertIs(rb.tg_client, self.new)
        self.assertEqual(self.new.events, ["connect", "ping"])
        self.assertEqual(
            self.new.flood_sleep_threshold, 25
        )

    async def test_disconnected_client_is_replaced_even_without_failed_probe(self):
        self.old.connected = False
        with patch.object(rb, "make_tg_client", return_value=self.new):
            await rb.verify_connection()
        self.assertEqual(self.old.events, ["disconnect"])
        self.assertEqual(self.new.events, ["connect", "ping"])

    async def test_teardown_error_forbids_replacement(self):
        async def bad_close():
            raise RuntimeError("unclosed update loop")

        self.old.disconnect = bad_close
        with (
            patch.object(rb, "make_tg_client", return_value=self.new) as factory,
            self.assertRaises(RuntimeError),
        ):
            await rb.verify_connection(rebuild=True)
        factory.assert_not_called()
        self.assertIs(rb.tg_client, self.old)

    async def test_fatal_update_loop_requires_process_boundary_not_another_disconnect(
        self,
    ):
        # Telethon records this before starting its own shielded disconnect.
        self.old._updates_error = RuntimeError("messagebox.apply_difference failed")
        with (
            patch.object(rb, "make_tg_client", return_value=self.new) as factory,
            self.assertRaisesRegex(RuntimeError, "update loop"),
        ):
            await rb.verify_connection(rebuild=True)
        factory.assert_not_called()
        self.assertEqual(self.old.events, [])

    async def test_shielded_teardown_timeout_forbids_replacement(self):
        release = asyncio.Event()
        inner = asyncio.create_task(release.wait())
        self.old.disconnect = lambda: asyncio.shield(inner)
        try:
            with (
                patch.object(rb, "CONNECT_TIMEOUT", 0.02),
                patch.object(rb, "make_tg_client", return_value=self.new) as factory,
            ):
                with self.assertRaises(RuntimeError):
                    await rb.verify_connection(rebuild=True)
                factory.assert_not_called()
                self.assertFalse(inner.done())
        finally:
            release.set()
            await inner

    async def test_replacement_waits_for_inflight_send(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def send(*args, **kwargs):
            entered.set()
            await release.wait()
            self.old.events.append("send")
            return SimpleNamespace(id=123)

        self.old.send_message = send
        with patch.object(rb, "make_tg_client", return_value=self.new):
            delivery = asyncio.create_task(rb.deliver("hello"))
            await asyncio.wait_for(entered.wait(), 1)
            replacement = asyncio.create_task(rb.verify_connection(rebuild=True))
            try:
                await asyncio.sleep(0.02)
                self.assertEqual(self.old.events, [])
            finally:
                release.set()
                await asyncio.gather(delivery, replacement, return_exceptions=True)
        self.assertEqual(self.old.events, ["send", "disconnect"])

    async def test_delivery_waits_for_replacement_and_uses_new_client(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def close():
            entered.set()
            await release.wait()
            self.old.connected = False

        self.old.disconnect = close
        with patch.object(rb, "make_tg_client", return_value=self.new):
            replacement = asyncio.create_task(rb.verify_connection(rebuild=True))
            await asyncio.wait_for(entered.wait(), 1)
            delivery = asyncio.create_task(rb.deliver("hello"))
            try:
                await asyncio.sleep(0.02)
                self.assertFalse(delivery.done())
            finally:
                release.set()
                results = await asyncio.gather(
                    replacement, delivery, return_exceptions=True
                )
        self.assertIs(results[1], True)
        self.assertEqual(self.new.events, ["connect", "ping", "send"])

    async def test_send_does_not_clear_failed_verify_evidence(self):
        rb.tg_health.supervised = True
        rb.tg_health.mark_failure("failed verify", 4)
        await rb.deliver("hello")
        self.assertEqual(rb.tg_health.attempts, 1)
        self.assertFalse(rb.tg_health.up)

    async def test_shutdown_reuses_teardown_started_by_cancelled_verify(self):
        entered, release = asyncio.Event(), asyncio.Event()
        inners = []

        async def close():
            entered.set()
            await release.wait()

        def disconnect():
            inner = asyncio.create_task(close())
            inners.append(inner)
            return asyncio.shield(inner)

        self.old.disconnect = disconnect
        verify = asyncio.create_task(rb.verify_connection(rebuild=True))
        await asyncio.wait_for(entered.wait(), 1)
        verify.cancel()
        await asyncio.gather(verify, return_exceptions=True)
        cleanup = asyncio.create_task(rb._discard_client(self.old))
        try:
            await asyncio.sleep(0.02)
            self.assertEqual(len(inners), 1)
        finally:
            release.set()
            await cleanup
            await asyncio.gather(*inners)

    async def test_real_telethon_disconnect_joins_old_background_loops(self):
        # MemorySession only. No connect/start or Telegram traffic.
        from telethon.client.telegramclient import TelegramClient
        from telethon.sessions import MemorySession

        old = TelegramClient(MemorySession(), 1, "test-only", auto_reconnect=False)
        old._updates_handle = asyncio.create_task(asyncio.Event().wait())
        old._keepalive_handle = asyncio.create_task(asyncio.Event().wait())
        await asyncio.sleep(0)

        def factory(**kwargs):
            self.assertTrue(old._updates_handle.done())
            self.assertTrue(old._keepalive_handle.done())
            return self.new

        with (
            patch.object(rb, "tg_client", old),
            patch.object(rb, "make_tg_client", factory),
        ):
            await rb.verify_connection(rebuild=True)
        self.assertEqual(self.new.events, ["connect", "ping"])

    async def test_unsupervised_health_is_immediate_despite_recent_success(self):
        rb.tg_health.mark_ok()
        response = Response()
        body = await rb.healthz(response)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(body["connection"]["supervised"])

    async def test_topic_rpc_and_send_allow_safe_replacement_between_them(self):
        from telethon.tl.types import UpdateMessageID

        entered, release = asyncio.Event(), asyncio.Event()
        lock = rb.tg_client_lock

        class TopicClient(Client):
            async def __call__(self, request):
                entered.set()
                await release.wait()

                class Result:
                    @property
                    def updates(self):
                        assert not lock.locked(), (
                            "topic parsing must not hold client lock"
                        )
                        return [UpdateMessageID(id=77, random_id=request.random_id)]

                return Result()

        old = TopicClient()
        with (
            patch.multiple(rb, tg_client=old, NEW_TOPIC_PER_CAPTURE=True),
            patch.object(rb, "make_tg_client", return_value=self.new),
            patch.object(
                self.new,
                "send_message",
                AsyncMock(return_value=SimpleNamespace(id=456)),
            ) as send,
        ):
            delivery = asyncio.create_task(rb.deliver("topic capture"))
            await asyncio.wait_for(entered.wait(), 1)
            verify = asyncio.create_task(rb.verify_connection(rebuild=True))
            await asyncio.sleep(0.01)
            self.assertEqual(old.events, [])
            release.set()
            result, _ = await asyncio.gather(delivery, verify)
        self.assertIs(result, True)
        self.assertEqual(send.call_args.kwargs["reply_to"], 77)
        self.assertEqual(old.events, ["disconnect"])

    async def test_cancelled_teardown_future_forbids_replacement(self):
        closing = asyncio.get_running_loop().create_future()
        closing.cancel()
        self.old.disconnect = lambda: closing
        with (
            patch.object(rb, "make_tg_client", return_value=self.new) as factory,
            self.assertRaisesRegex(rb.UnsafeClientError, "cancelled"),
        ):
            await rb.verify_connection(rebuild=True)
        factory.assert_not_called()
        self.assertIsNotNone(rb.tg_health.fatal)

    async def test_update_failure_during_teardown_forbids_replacement(self):
        async def close():
            self.old._updates_error = RuntimeError("update failure during teardown")

        self.old.disconnect = close
        with (
            patch.object(rb, "make_tg_client", return_value=self.new) as factory,
            self.assertRaises(rb.UnsafeClientError),
        ):
            await rb.verify_connection(rebuild=True)
        factory.assert_not_called()

    async def test_update_failure_during_successful_ping_is_not_healthy(self):
        class FailsDuringPing(Client):
            async def __call__(self, request):
                self._updates_error = RuntimeError("update loop failed during ping")
                return object()

        with (
            patch.object(rb, "tg_client", FailsDuringPing()),
            self.assertRaises(rb.UnsafeClientError),
        ):
            await rb.verify_connection()
        self.assertFalse(rb.tg_health.up)

    async def test_delivery_rejects_fatal_update_state_without_sending(self):
        self.old._updates_error = RuntimeError("update failure")
        with self.assertRaises(rb.UnsafeClientError):
            await rb.deliver("hello")
        self.assertEqual(self.old.events, [])

    async def test_replacement_never_replays_a_send(self):
        # No new delivery protocol: reconnection itself must not replay either
        # an acknowledged send or one whose acknowledgement was lost.
        for ambiguous in (False, True):
            with self.subTest(ambiguous=ambiguous):
                old, new = Client(), Client(False)
                rows = []

                async def send(*args, old=old, ambiguous=ambiguous, **kwargs):
                    old.events.append("send")
                    if ambiguous:
                        raise ConnectionError("ack lost after acceptance")
                    return SimpleNamespace(id=123)

                old.send_message = send
                with (
                    patch.multiple(
                        rb, tg_client=old, tg_health=rb.ConnectionHealth(),
                        ASSISTANT_ROSTER={"assistant": "@test_bot"},
                    ),
                    patch.object(rb, "make_tg_client", return_value=new),
                    patch.object(rb, "log_capture", rows.append),
                    patch("builtins.print"),
                ):
                    result = await rb.relay("one action", "assistant")
                    await rb.verify_connection(rebuild=True)
                self.assertEqual(old.events, ["send", "disconnect"])
                self.assertEqual(new.events, ["connect", "ping"])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["forwarded"], not ambiguous)
                self.assertIn("delivery failed" if ambiguous else "Delivered.", result)

    async def test_telethon_records_fatal_error_before_private_teardown(self):
        from telethon.client.telegramclient import TelegramClient
        from telethon.sessions import MemorySession

        client = TelegramClient(MemorySession(), 1, "test-only", auto_reconnect=False)
        failure = RuntimeError("synthetic update-loop failure")

        async def close():
            self.assertIs(client._updates_error, failure)

        with (
            patch.object(client, "is_connected", return_value=True),
            patch.object(type(client._message_box), "get_difference", side_effect=failure),
            patch.object(client, "disconnect", AsyncMock(side_effect=close)) as disconnect,
            self.assertLogs("telethon", level="ERROR"),
        ):
            await client._update_loop()
        disconnect.assert_awaited_once()
        self.assertIs(client._updates_error, failure)

    async def test_lifespan_body_exception_cleans_up_without_fatal_exit(self):
        @asynccontextmanager
        async def manager():
            yield

        with (
            patch.object(rb, "_telegram_startup", AsyncMock()),
            patch.object(rb.mcp.session_manager, "run", manager),
            patch.object(rb.os, "_exit") as exit_process,
        ):
            with self.assertRaisesRegex(ValueError, "body failed"):
                async with rb.lifespan(rb.app):
                    await asyncio.sleep(0)
                    raise ValueError("body failed")
            await asyncio.sleep(0.01)
        exit_process.assert_not_called()
        self.assertFalse(rb.tg_health.supervised)
        self.assertIn("disconnect", self.old.events)


class ProcessLifecycleTests(unittest.TestCase):
    def child(self, mode):
        try:
            return subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).with_name("lifecycle_child.py")),
                    mode,
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail(f"uvicorn stayed alive with {mode} Telegram supervision")

    def test_broken_supervision_exits_nonzero(self):
        for mode in (
            "returned",
            "crashed",
            "cancelled",
            "task_cancelled",
            "hung",
            "blocked_loop",
            "verify_resists_cancel",
            "shielded_disconnect",
            "startup_hung",
            "auth_fatal",
            "update_error",
        ):
            with self.subTest(mode=mode):
                result = self.child(mode)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn("FATAL:", result.stdout)
                if mode != "startup_hung":
                    self.assertNotIn(
                        "watchdog expired: Telegram startup", result.stdout
                    )

    def test_clean_shutdown_and_startup_failure_disarm_watchdog(self):
        for mode in (
            "normal",
            "disabled",
            "startup_error",
            "mcp_error",
            "outage",
            "sigterm",
            "shutdown_hung",
        ):
            with self.subTest(mode=mode):
                result = self.child(mode)
                expected = 3 if mode in {"startup_error", "mcp_error"} else 0
                if mode == "sigterm":
                    expected = -signal.SIGTERM
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertNotIn("FATAL:", result.stdout)
                if mode == "outage":
                    self.assertIn("OUTAGE_RETRYING", result.stdout)


if __name__ == "__main__":
    unittest.main()
