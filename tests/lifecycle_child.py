"""Disposable uvicorn process for lifecycle regression tests. Never real Telegram."""

import asyncio
import os
import signal
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


async def main(mode, state):
    # Import cost is not the behavior under test with subsecond deadlines.
    import telethon.tl.functions  # noqa: F401
    import uvicorn
    from telethon.errors.common import AuthKeyNotFound

    import ringbearer as rb

    real_exit = os._exit

    def observed_exit(code):
        print("FATAL:", rb.tg_health.fatal, flush=True)
        real_exit(code)

    rb.os._exit = observed_exit

    class Client:
        connected = False

        def __init__(self):
            self._updates_error = (
                RuntimeError("messagebox.apply_difference failed")
                if mode == "update_error"
                else None
            )

        def is_connected(self):
            return self.connected

        async def connect(self):
            if mode == "startup_hung":
                while True:
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        pass
            self.connected = True

        async def get_me(self):
            if mode == "startup_error":
                raise ValueError("fake startup error")
            return object()

        async def get_input_entity(self, chat):
            return object()

        async def __call__(self, request):
            if mode == "outage":
                raise OSError("fake network outage")
            if mode == "auth_fatal":
                raise AuthKeyNotFound()
            if mode == "verify_resists_cancel":
                while True:
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        pass
            return object()

        def disconnect(self):
            async def close():
                if mode in {"shielded_disconnect", "shutdown_hung"}:
                    await asyncio.Event().wait()
                self.connected = False

            return asyncio.shield(asyncio.create_task(close()))

    async def broken_supervisor():
        rb.tg_health.supervised = True
        await asyncio.sleep(0.02)
        if mode == "returned":
            return
        if mode == "crashed":
            raise RuntimeError("fake supervisor crash")
        if mode == "cancelled":
            raise asyncio.CancelledError()
        if mode == "task_cancelled":
            task = asyncio.current_task()
            assert task is not None
            task.cancel()
            await asyncio.sleep(0)
        if mode == "blocked_loop":
            time.sleep(10)  # noqa: ASYNC251 - deliberately block the event loop
        if mode == "shielded_disconnect":
            await rb.verify_connection(rebuild=True)
            return
        await asyncio.Event().wait()

    @asynccontextmanager
    async def broken_manager():
        raise RuntimeError("fake MCP startup failure")
        yield

    (Path(state) / "test-only.session").touch()
    with patch.multiple(
        rb,
        make_tg_client=lambda **kwargs: Client(),
        SUPERVISOR_WATCHDOG_TIMEOUT=0.15,
        VERIFY_TIMEOUT=0.15,
        CONNECT_TIMEOUT=0.03,
        PING_TIMEOUT=0.03,
        HEALTH_POLL_INTERVAL=0.02,
        RECONNECT_BACKOFF_START=0.01,
        RECONNECT_BACKOFF_CAP=0.02,
        SHUTDOWN_TIMEOUT=0.04,
    ):
        if mode in {
            "returned",
            "crashed",
            "cancelled",
            "task_cancelled",
            "hung",
            "blocked_loop",
            "shielded_disconnect",
        }:
            rb.connection_supervisor = broken_supervisor
        if mode == "mcp_error":
            rb.mcp.session_manager.run = broken_manager
        server = uvicorn.Server(
            uvicorn.Config(rb.app, host="127.0.0.1", port=0, log_level="critical")
        )
        if mode in {"normal", "disabled", "outage", "sigterm", "shutdown_hung"}:

            async def stop():
                await asyncio.sleep(0.4 if mode == "outage" else 0.08)
                if mode == "outage":
                    assert rb.tg_health.attempts > 5
                    assert rb.tg_health.supervised
                    print("OUTAGE_RETRYING", flush=True)
                if mode == "sigterm":
                    os.kill(os.getpid(), signal.SIGTERM)
                    return
                server.should_exit = True

            asyncio.create_task(stop())
        try:
            await server.serve()
        finally:
            # Startup failure must disarm the watchdog too, including when
            # uvicorn raises SystemExit(3). Wait beyond its deadline.
            await asyncio.sleep(0.3)


if __name__ == "__main__":
    mode = sys.argv[1]
    with tempfile.TemporaryDirectory(prefix="ringbearer-child-") as state:
        os.environ.update(
            {
                "RINGBEARER_STATE_DIR": state,
                "TELEGRAM_ENABLED": "false" if mode == "disabled" else "true",
                "BRIDGE_TOKEN": "test-only",
                "TG_API_ID": "1",
                "TG_API_HASH": "test-only",
                "ASSISTANT_CHAT": "@fake",
                "ASSISTANT_NAME": "assistant",
                "ASSISTANTS": "",
                "SESSION_NAME": "test-only",
                "DELIVERY_CONTEXT": "conversation",
            }
        )
        asyncio.run(main(mode, state))
