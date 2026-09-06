"""The HTTP surface: the FastAPI app, its startup/shutdown lifecycle, the
bearer-token gate in front of the MCP mount, and /healthz.

The runtime Telegram state this module starts and reads — the client, the
recheck event, the health object — belongs to `telegram` and is touched
through that module, never copied here: lifespan rebinds two of those names
and tests rebind a third, so a local copy would go stale immediately.
"""

import asyncio
import hmac
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from . import config, telegram, transcribe
from .route.mcp import McpMethodLogger, mcp
from .route.webhook import router as webhook_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    supervisor = None
    if not config.BRIDGE_TOKEN:
        raise RuntimeError(
            "BRIDGE_TOKEN is not set — refusing to start unauthenticated. "
            "First run? python ringbearer.py setup"
        )
    # Start the model loading before anything else waits on the network: the
    # first ring capture should find a warm engine, not a cold download. It
    # loads in transcribe's worker thread, so nothing below is held up by it
    # and every capture submitted later queues behind it in the same thread.
    transcribe.start_loading()
    if config.TELEGRAM_ENABLED:
        if not (config.TG_API_ID and config.TG_API_HASH and config.ASSISTANT_CHAT):
            raise RuntimeError(
                "TELEGRAM_ENABLED but TG_API_ID/TG_API_HASH/ASSISTANT_CHAT missing "
                "— run: python ringbearer.py setup"
            )
        if not (config.STATE_DIR / f"{config.SESSION_NAME}.session").exists():
            raise RuntimeError(
                f"TELEGRAM_ENABLED but no {config.SESSION_NAME}.session found "
                "— run: python ringbearer.py login"
            )
        from telethon.errors import FloodWaitError

        # Constructing the client OPENS the session database — a Pyrogram-era
        # file at the same path dies right here with an sqlite error, so the
        # migration advice must wrap construction, not just connect().
        try:
            telegram.tg_client = telegram.make_tg_client(serving=True)
        except Exception as e:
            raise RuntimeError(
                f"Can't open {config.SESSION_NAME}.session ({type(e).__name__}: {e}).\n"
                f"If it predates the Telethon migration (Pyrogram), delete it "
                f"and run: python ringbearer.py login"
            ) from e
        # Serving-phase flood policy, set BEFORE the probes below rather than
        # after them: sleep through short waits, surface longer ones as
        # FloodWaitError. Every probe here carries a 30s deadline, so under
        # Telethon's 60s default a 31-60s flood wait would be slept internally,
        # killed by that deadline, and arrive as a bare timeout — landing in the
        # generic handler, whose advice is to delete the session. Deleting a
        # healthy login during a flood wait is the worst available move, and the
        # FloodWaitError branch below exists precisely to prevent it. deliver()
        # has the same 30s deadline and the same reason to want this.
        # login/setup keep Telethon's default via the untouched factory.
        telegram.tg_client.flood_sleep_threshold = 25
        # connect(), never start(): start() would prompt for a phone number on
        # an unauthorized session, and this path must stay launchd-safe. A bad
        # session fails fast with the fix named instead of a crash-loop
        # traceback (or worse, a silent hang on a prompt no one will answer).
        # Bounded like deliver(): a half-open connection must not leave the
        # lifespan pending forever with /healthz never binding.
        try:
            await telegram.boot_connect()
            # get_me(), never is_user_authorized(): the latter swallows EVERY
            # RPC error into False — a flood wait would read as "revoked" and
            # the advice below would tell the user to delete a healthy session
            # (while a service manager restart-loops it). get_me() returns
            # None only for a genuinely unauthorized session and lets
            # FloodWaitError reach the handler below.
            authorized = (
                await asyncio.wait_for(telegram.tg_client.get_me(), timeout=30)
            ) is not None
        except FloodWaitError as e:
            # Rate limiting is NOT a broken session — advising deletion here
            # would destroy a healthy login during a restart storm.
            raise RuntimeError(
                f"Telegram is rate-limiting this account (FloodWait: retry in "
                f"{e.seconds}s). The session is healthy — do NOT delete it; "
                "wait and restart."
            ) from e
        except Exception as e:
            raise RuntimeError(
                f"Telegram client failed to connect ({type(e).__name__}: {e}).\n"
                f"If {config.SESSION_NAME}.session was revoked, delete it and run: "
                "python ringbearer.py login"
            ) from e
        if not authorized:
            raise RuntimeError(
                f"{config.SESSION_NAME}.session exists but is not authorized (revoked, "
                "terminated, or written by a different library) — delete it "
                "and run: python ringbearer.py login"
            )
        # Resolve every roster chat once, up front. Numeric ids depend on the
        # session's entity cache and would otherwise fail on EVERY send —
        # silently, from the ring's point of view. Better: refuse to start,
        # with the assistant and the fix named. Strict on purpose: a typo'd
        # mapping should die here, while the .env edit is fresh, not weeks
        # later on a walk.
        for _name, _chat in config.ASSISTANT_ROSTER.items():
            try:
                telegram.assistant_entities[_name] = await asyncio.wait_for(
                    telegram.tg_client.get_input_entity(_chat), timeout=30
                )
            except Exception as e:
                raise RuntimeError(
                    f"Can't resolve assistant {_name!r} (chat {_chat!r}): "
                    f"{type(e).__name__}: {e}\n"
                    "A numeric id only works for chats this account has "
                    "already seen from this session; the @username form "
                    f"always works — set it in {config.STATE_DIR / '.env'}."
                ) from e
        # The get_me() above was a real round trip, so the link starts verified
        # rather than merely assumed. From here the supervisor owns it: it holds
        # the connection open for the life of the process, and it is what makes
        # /healthz able to answer honestly.
        telegram.tg_health.mark_ok()
        telegram.recheck_now = asyncio.Event()
        supervisor = asyncio.create_task(telegram.keep_supervising())
    async with mcp.session_manager.run():
        yield
    if supervisor is not None:
        supervisor.cancel()
        with suppress(asyncio.CancelledError):
            await supervisor
    if telegram.tg_client is not None:
        await telegram.tg_client.disconnect()
    transcribe.shutdown()


# Docs/OpenAPI off: nothing here is browsable, and the README's "everything
# except /healthz requires the token" claim should be literally true.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def token_ok(authorization: str | None) -> bool:
    # Compare bytes: compare_digest raises TypeError on non-ASCII str input,
    # and Starlette decodes headers as latin-1 — a malformed header should be
    # a clean 401, not a 500.
    return authorization is not None and hmac.compare_digest(
        authorization.encode("utf-8", "replace"), f"Bearer {config.BRIDGE_TOKEN}".encode()
    )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # The MCP mount bypasses route-level auth, so gate it here.
    if request.url.path.startswith(config.MCP_MOUNT):
        if not token_ok(request.headers.get("authorization")):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


# Registered before the MCP mount, and that order is load-bearing: Starlette
# matches routes in the order they were added and a Mount claims every path
# under its prefix, so /ringbearer/webhook added after the mount would be
# swallowed by the MCP app and never reached. Both live under MCP_MOUNT so the
# auth middleware's one prefix check covers both doors.
app.include_router(webhook_router)


# host="0.0.0.0" matters: with the default localhost host the SDK auto-enables
# DNS-rebinding protection whose allowlist would reject the phone's LAN or
# tailnet Host header (lowlevel/server.py:739).
app.mount(
    config.MCP_MOUNT,
    McpMethodLogger(
        mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    ),
)


@app.get("/healthz")
async def healthz(response: Response):
    """Report whether Telegram is actually reachable, not whether it is enabled.

    503 when it is not: the shipped Docker healthcheck calls urlopen, which
    raises on any non-2xx, so an existing deployment gets the new signal without
    editing its compose file. This handler never talks to Telegram — it reads
    state the supervisor maintains — so an open, unauthenticated endpoint
    cannot be polled into API traffic no matter how hard anyone hits it.
    """
    connection = telegram.tg_health.snapshot()
    degraded = config.TELEGRAM_ENABLED and not telegram.tg_health.up
    if degraded:
        response.status_code = 503
    return {
        "ok": not degraded,
        "telegram": config.TELEGRAM_ENABLED,
        "connection": connection,
        "transcribe": transcribe.snapshot(),
    }
