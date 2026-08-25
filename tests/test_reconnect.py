"""The bridge has to survive the network going away.

Before this suite existed, it did not: Telethon retried five times a second
apart and then tore the session down for good, while /healthz kept answering
200 because it only ever reported that Telegram was configured. Every test here
is one of those two failures, held still.
"""

import asyncio
import unittest
from contextlib import suppress
from unittest.mock import AsyncMock, patch

from fastapi import Response
from telethon.errors.common import AuthKeyNotFound
from telethon.errors.rpcerrorlist import AuthKeyDuplicatedError, SessionRevokedError

import ringbearer


class StopLoop(Exception):
    """Ends the supervisor's deliberately unbounded loop from the test side."""


class SleepRecorder:
    """Stands in for asyncio.sleep: records what the supervisor asked to wait,
    then stops the loop once enough waits have happened. A loop that ends on its
    own never reaches the limit, which is what makes 'unbounded' testable."""

    def __init__(self, limit):
        self.delays = []
        self.limit = limit

    async def __call__(self, delay):
        self.delays.append(delay)
        if len(self.delays) >= self.limit:
            raise StopLoop()


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # The supervisor reads module-global health; give every test its own.
        ringbearer.tg_health = ringbearer.ConnectionHealth()

    async def test_transient_failure_retries_forever_on_doubling_backoff(self):
        """The original bug: five seconds of retries, then permanent silence."""
        sleeper = SleepRecorder(limit=8)
        with (
            patch.multiple(
                ringbearer,
                verify_connection=AsyncMock(side_effect=OSError("network unreachable")),
                recheck_now=None,
            ),
            patch("ringbearer.asyncio.sleep", sleeper),
            patch("builtins.print"),
        ):
            with self.assertRaises(StopLoop):
                await ringbearer.connection_supervisor()

        nominal = [1, 2, 4, 8, 16, 32, 60, 60]
        self.assertEqual(len(sleeper.delays), len(nominal))
        for waited, want in zip(sleeper.delays, nominal):
            self.assertGreaterEqual(waited, want * (1 - ringbearer.RECONNECT_JITTER))
            self.assertLessEqual(waited, want * (1 + ringbearer.RECONNECT_JITTER))
        self.assertEqual(ringbearer.tg_health.attempts, 8)
        self.assertFalse(ringbearer.tg_health.up)

    async def test_link_restores_and_clears_the_failure_state(self):
        calls = []

        async def flaky(*, rebuild=False):
            calls.append(rebuild)
            if len(calls) <= 3:
                raise OSError("no route to host")

        sleeper = SleepRecorder(limit=4)  # three backoffs, then the poll wait
        with (
            patch.multiple(ringbearer, verify_connection=flaky, recheck_now=None),
            patch("ringbearer.asyncio.sleep", sleeper),
            patch("builtins.print"),
        ):
            with self.assertRaises(StopLoop):
                await ringbearer.connection_supervisor()

        self.assertEqual(len(calls), 4)
        # First pass trusts the existing connection; every pass after a failure
        # rebuilds it rather than trusting `is_connected()`.
        self.assertEqual(calls, [False, True, True, True])
        self.assertEqual(sleeper.delays[-1], ringbearer.HEALTH_POLL_INTERVAL)
        self.assertEqual(ringbearer.tg_health.attempts, 0)
        self.assertIsNone(ringbearer.tg_health.last_error)
        with patch.object(ringbearer, "TELEGRAM_ENABLED", True):
            self.assertTrue(ringbearer.tg_health.up)

    async def test_auth_failure_stops_the_loop_instead_of_burying_it(self):
        """A revoked session is not an outage. Retrying it forever would hide
        the one message that fixes it behind a growing retry counter."""
        sleeper = SleepRecorder(limit=1)
        with (
            patch.multiple(
                ringbearer,
                verify_connection=AsyncMock(side_effect=AuthKeyNotFound()),
                recheck_now=None,
            ),
            patch("ringbearer.asyncio.sleep", sleeper),
            patch("builtins.print"),
        ):
            await ringbearer.connection_supervisor()  # returns; does not raise

        self.assertEqual(sleeper.delays, [])  # never slept, never retried
        self.assertIsNotNone(ringbearer.tg_health.fatal)
        with patch.object(ringbearer, "TELEGRAM_ENABLED", True):
            self.assertEqual(ringbearer.tg_health.snapshot()["state"], "fatal")

    async def test_every_dead_session_error_stops_the_loop(self):
        """AuthKeyDuplicatedError is a 406 under AuthKeyError, not a 401 under
        UnauthorizedError, so catching only the 401 family let the supervisor
        retry a key Telegram had already killed. It is also the likeliest of
        these to actually happen: two processes on one session file."""
        for error in (
            AuthKeyDuplicatedError(request=None),
            SessionRevokedError(request=None),
            AuthKeyNotFound(),
        ):
            with self.subTest(error=type(error).__name__):
                ringbearer.tg_health = ringbearer.ConnectionHealth()
                sleeper = SleepRecorder(limit=1)
                with (
                    patch.multiple(
                        ringbearer,
                        verify_connection=AsyncMock(side_effect=error),
                        recheck_now=None,
                    ),
                    patch("ringbearer.asyncio.sleep", sleeper),
                    patch("builtins.print"),
                ):
                    await ringbearer.connection_supervisor()
                self.assertEqual(sleeper.delays, [])
                self.assertIsNotNone(ringbearer.tg_health.fatal)

    async def test_connected_but_mute_reads_as_down(self):
        """is_connected() is a socket-level claim: it says True while the far
        end answers nothing. Health must not inherit that lie."""

        class MuteClient:
            def is_connected(self):
                return True

            async def __call__(self, request):
                await asyncio.Event().wait()  # answers, eventually, never

        sleeper = SleepRecorder(limit=1)
        with (
            patch.multiple(
                ringbearer,
                tg_client=MuteClient(),
                PING_TIMEOUT=0.05,
                TELEGRAM_ENABLED=True,
                recheck_now=None,
            ),
            patch("ringbearer.asyncio.sleep", sleeper),
            patch("builtins.print"),
        ):
            with self.assertRaises(StopLoop):
                await ringbearer.connection_supervisor()
            self.assertEqual(ringbearer.tg_health.snapshot()["state"], "down")


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    """The half the first pass of these tests missed: noticing a dead link is
    not the same as repairing one."""

    def setUp(self):
        ringbearer.tg_health = ringbearer.ConnectionHealth()

    async def test_a_mute_connection_is_torn_down_and_rebuilt(self):
        """A client can report connected while the far end answers nothing. If
        the supervisor only reconnects when `is_connected()` is False, it pings
        that corpse forever. After any failure it must rebuild instead."""

        class MuteThenFine:
            def __init__(self):
                self.connected = True
                self.disconnects = 0
                self.connects = 0
                self.pings = 0

            def is_connected(self):
                return self.connected

            async def disconnect(self):
                self.disconnects += 1
                self.connected = False

            async def connect(self):
                self.connects += 1
                self.connected = True

            async def __call__(self, request):
                self.pings += 1
                if self.disconnects == 0:  # still the original stuck socket
                    raise asyncio.TimeoutError()
                return object()

        client = MuteThenFine()
        sleeper = SleepRecorder(limit=2)  # one backoff, then the healthy poll
        with (
            patch.multiple(
                ringbearer, tg_client=client, TELEGRAM_ENABLED=True, recheck_now=None
            ),
            patch("ringbearer.asyncio.sleep", sleeper),
            patch("builtins.print"),
        ):
            with self.assertRaises(StopLoop):
                await ringbearer.connection_supervisor()

        self.assertEqual(client.disconnects, 1)  # the socket was actually closed
        self.assertEqual(client.connects, 1)
        self.assertEqual(ringbearer.tg_health.attempts, 0)
        self.assertTrue(ringbearer.tg_health.up)

    async def test_a_healthy_poll_does_not_tear_down_a_good_connection(self):
        """The rebuild is for after a failure. A steady healthy link should be
        pinged, never recycled."""

        class Fine:
            def __init__(self):
                self.disconnects = 0

            def is_connected(self):
                return True

            async def disconnect(self):
                self.disconnects += 1

            async def __call__(self, request):
                return object()

        client = Fine()
        sleeper = SleepRecorder(limit=2)
        with (
            patch.multiple(
                ringbearer, tg_client=client, TELEGRAM_ENABLED=True, recheck_now=None
            ),
            patch("ringbearer.asyncio.sleep", sleeper),
            patch("builtins.print"),
        ):
            with self.assertRaises(StopLoop):
                await ringbearer.connection_supervisor()
        self.assertEqual(client.disconnects, 0)

    async def test_the_production_wait_branch_wakes_on_a_failed_send(self):
        """Every other test here patches `recheck_now` to None, which is the
        branch that never runs in production. This one uses a real event, the
        way lifespan() wires it, and proves a failed send cuts the wait short
        instead of leaving health stale until the next poll."""
        event = asyncio.Event()
        passes = asyncio.Queue()

        async def probe(*, rebuild=False):
            await passes.put(rebuild)

        with (
            patch.multiple(ringbearer, verify_connection=probe, recheck_now=event),
            patch("builtins.print"),
        ):
            task = asyncio.create_task(ringbearer.connection_supervisor())
            try:
                await asyncio.wait_for(passes.get(), timeout=1)
                ringbearer.request_recheck()  # what relay() does on a failed send
                # Without the wake this waits HEALTH_POLL_INTERVAL and times out.
                await asyncio.wait_for(passes.get(), timeout=1)
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


class BootConnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_boot_keeps_the_tolerance_telethon_used_to_provide(self):
        attempts = []

        class Flaky:
            async def connect(self):
                attempts.append(1)
                if len(attempts) < 3:
                    raise OSError("no route to host")

        with (
            patch.object(ringbearer, "tg_client", Flaky()),
            patch("ringbearer.asyncio.sleep", AsyncMock()),
        ):
            await ringbearer.boot_connect()
        self.assertEqual(len(attempts), 3)

    async def test_boot_still_fails_loudly_when_the_window_runs_out(self):
        class Dead:
            async def connect(self):
                raise OSError("no route to host")

        with (
            patch.object(ringbearer, "tg_client", Dead()),
            patch("ringbearer.asyncio.sleep", AsyncMock()),
        ):
            with self.assertRaises(OSError):
                await ringbearer.boot_connect()


class ClientPolicyTests(unittest.TestCase):
    def test_serving_client_hands_reconnection_to_the_supervisor(self):
        captured = {}

        class FakeTelegramClient:
            def __init__(self, session, api_id, api_hash, **kwargs):
                captured.update(kwargs)

        with (
            patch("telethon.TelegramClient", FakeTelegramClient),
            patch.multiple(ringbearer, TG_API_ID="1", TG_API_HASH="hash"),
        ):
            ringbearer.make_tg_client(serving=True)
            serving = dict(captured)
            captured.clear()
            ringbearer.make_tg_client()
            interactive = dict(captured)

        self.assertFalse(serving["auto_reconnect"])
        self.assertEqual(serving["connection_retries"], 0)
        # login/setup keep Telethon's own behavior; only the server overrides it.
        self.assertEqual(interactive, {})


class HealthStateTests(unittest.TestCase):
    def setUp(self):
        self.health = ringbearer.ConnectionHealth()

    def test_a_link_never_verified_is_not_up(self):
        self.assertFalse(self.health.up)

    def test_stale_verification_ages_out_without_a_second_watchdog(self):
        """If the supervisor task dies, nothing marks a failure — so the age of
        the last round trip has to be able to turn health red on its own."""
        self.health.mark_ok()
        self.assertTrue(self.health.up)
        self.health.last_ok -= ringbearer.HEALTH_STALE_AFTER + 1
        self.assertFalse(self.health.up)


class HealthEndpointTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        ringbearer.tg_health = ringbearer.ConnectionHealth()

    async def test_healthy_link_answers_200(self):
        ringbearer.tg_health.mark_ok()
        response = Response()
        with patch.object(ringbearer, "TELEGRAM_ENABLED", True):
            body = await ringbearer.healthz(response)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["connection"]["state"], "up")

    async def test_down_link_answers_503_and_names_the_state(self):
        ringbearer.tg_health.mark_failure("OSError: network unreachable", 4.0)
        response = Response()
        with patch.object(ringbearer, "TELEGRAM_ENABLED", True):
            body = await ringbearer.healthz(response)
        self.assertEqual(response.status_code, 503)
        self.assertFalse(body["ok"])
        self.assertEqual(body["connection"]["state"], "down")
        self.assertEqual(body["connection"]["next_retry_s"], 4.0)
        self.assertIn("network unreachable", body["connection"]["error"])

    async def test_telegram_disabled_is_not_a_failure(self):
        response = Response()
        with patch.object(ringbearer, "TELEGRAM_ENABLED", False):
            body = await ringbearer.healthz(response)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(body["ok"])
        self.assertEqual(body["connection"]["state"], "disabled")

    async def test_the_enabled_flag_keeps_its_old_shape(self):
        """Existing probes read `telegram` as a boolean. The new detail lands
        beside it, never in place of it."""
        ringbearer.tg_health.mark_ok()
        with patch.object(ringbearer, "TELEGRAM_ENABLED", True):
            body = await ringbearer.healthz(Response())
        self.assertIs(body["telegram"], True)

    async def test_polling_the_open_endpoint_never_touches_telegram(self):
        """/healthz is unauthenticated and tailnet-reachable. If reading it cost
        an API call, anyone who could reach it could spend the account's rate
        limit. The supervisor owns every round trip; this handler reads state."""

        class ExplodingClient:
            def __init__(self):
                self.calls = 0

            def is_connected(self):
                self.calls += 1
                return True

            async def __call__(self, request):
                self.calls += 1
                raise AssertionError("the health path must not call Telegram")

        client = ExplodingClient()
        ringbearer.tg_health.mark_ok()
        with patch.multiple(ringbearer, TELEGRAM_ENABLED=True, tg_client=client):
            for _ in range(100):
                await ringbearer.healthz(Response())
        self.assertEqual(client.calls, 0)


if __name__ == "__main__":
    unittest.main()
