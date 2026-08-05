#!/usr/bin/env python3
"""
ring-bridge: Pebble Index 01 ring -> your AI assistant's Telegram DM.

Double-click-hold the ring and speak. The Pebble app's MCP sandbox agent calls
this bridge's send_to_<assistant> tool with the transcript, and the bridge
posts it into your Telegram DM with the assistant -- as you, from your own
Telegram account -- so one thread carries the whole conversation and the
assistant replies exactly as it would to a typed message.

Works with any assistant that lives in a Telegram chat: Hermes, OpenClaw,
a bot you wrote yourself.

First-time setup:  python bridge.py setup    (walks you through every secret)
Telegram login:    python bridge.py login    (one-time; creates the session file)
Run the server:    .venv/bin/uvicorn bridge:app --host <reachable-ip> --port 8787

Endpoints:
  <MCP_MOUNT>/mcp   MCP server (Streamable HTTP), bearer-token gated.
  /healthz          Open liveness check.
"""

# Python 3.14+: Pyrogram's sync module calls asyncio.get_event_loop() at import
# time, which raises if no loop is set yet. Ensure one exists before importing.
import asyncio

try:
    asyncio.get_running_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

import hmac
import json
import os
import re
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server import MCPServer

load_dotenv()

BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
TELEGRAM_ENABLED = os.environ.get("TELEGRAM_ENABLED", "false").lower() == "true"
ASSISTANT_CHAT = os.environ.get("ASSISTANT_CHAT", "")  # @username or chat id of the assistant DM
ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "assistant")
RING_PREFIX = os.environ.get("RING_PREFIX", "\U0001f3a4 ")
TG_API_ID = os.environ.get("TG_API_ID", "")
TG_API_HASH = os.environ.get("TG_API_HASH", "")
SESSION_NAME = os.environ.get("SESSION_NAME", "ring_bridge")
MCP_MOUNT = os.environ.get("MCP_MOUNT", "/bridge")

HERE = Path(__file__).parent
CAPTURES = HERE / "captures.jsonl"
DRY_RUN_PREFIX = "DRYRUN:"

# The tool name the ring app's LLM sees, e.g. send_to_hermes. Keep it free of
# anything but [a-z0-9_]: the app sanitizes names for the LLM but dispatches on
# the original, so exotic characters break the round-trip (reported upstream).
ASSISTANT_SLUG = re.sub(r"[^a-z0-9_]+", "_", ASSISTANT_NAME.lower()).strip("_") or "assistant"
TOOL_NAME = f"send_to_{ASSISTANT_SLUG}"

tg_client = None


def make_tg_client():
    from pyrogram import Client

    # workdir is pinned to this file's directory: Pyrogram otherwise anchors the
    # session to Path(sys.argv[0]).parent, which is .venv/bin under uvicorn — the
    # session written by `bridge.py login` would be invisible to the server.
    return Client(
        SESSION_NAME,
        api_id=int(TG_API_ID),
        api_hash=TG_API_HASH,
        workdir=str(HERE),
    )


def log_capture(row: dict) -> None:
    with CAPTURES.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


async def deliver(message: str) -> bool:
    """Post into the assistant DM as the user. Returns True if actually sent."""
    if TELEGRAM_ENABLED and tg_client is not None:
        await tg_client.send_message(ASSISTANT_CHAT, f"{RING_PREFIX}{message}")
        return True
    return False


# --- MCP server ---------------------------------------------------------------

mcp = MCPServer("ring-bridge")


@mcp.tool(
    name=TOOL_NAME,
    description=(
        f"Relay the user's spoken message to {ASSISTANT_NAME}, their personal "
        "assistant. It handles ALL requests (reminders, questions, tasks, "
        "notes, anything) and replies to the user directly in Telegram. This "
        "is the only action available: for EVERY user message, call this tool "
        "exactly once with the user's words verbatim and in full. Never "
        "paraphrase, never summarize, never answer the user yourself, and "
        "never skip the call — even for greetings, tests, or unclear speech."
    ),
)
async def send_to_assistant(message: str) -> str:
    # Probe guard: a DRYRUN-prefixed message exercises the whole path (auth,
    # MCP dispatch, logging) without putting anything in the real assistant DM.
    # Live probes are real messages to a real assistant — never send them casually.
    if message.startswith(DRY_RUN_PREFIX):
        log_capture({
            "received_at": datetime.now().astimezone().isoformat(),
            "source": "mcp",
            "transcription": message,
            "forwarded": False,
            "dry_run": True,
        })
        print(f"[mcp] {TOOL_NAME}: DRY RUN — not delivered", flush=True)
        return "Dry run: received, not delivered to Telegram."

    started = time.monotonic()
    sent = await deliver(message)
    send_ms = round((time.monotonic() - started) * 1000)
    print(f"[mcp] {TOOL_NAME}: telegram {send_ms}ms", flush=True)
    log_capture({
        "received_at": datetime.now().astimezone().isoformat(),
        "source": "mcp",
        "transcription": message,
        "forwarded": sent,
        "telegram_ms": send_ms,
    })
    if sent:
        return f"Delivered. {ASSISTANT_NAME} will reply in Telegram."
    return "Received and logged. (Telegram delivery not yet enabled.)"


@mcp.prompt()
def ring_routing() -> str:
    """Standing instruction for handling ring voice captures."""
    return (
        f"Every user message is a voice capture meant for {ASSISTANT_NAME}. "
        f"Call {TOOL_NAME} exactly once with the message verbatim, then reply "
        "only 'Sent.'"
    )


class McpMethodLogger:
    """ASGI tee that logs each JSON-RPC method hitting the MCP mount, so
    tools/list traffic is distinguishable from actual tools/call activity."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.inner(scope, receive, send)
        chunks: list[bytes] = []

        async def teed_receive():
            msg = await receive()
            if msg["type"] == "http.request":
                chunks.append(msg.get("body", b""))
                if not msg.get("more_body"):
                    try:
                        method = json.loads(b"".join(chunks)).get("method")
                        print(f"[mcp] <- {method}", flush=True)
                    except Exception:
                        pass
            return msg

        return await self.inner(scope, teed_receive, send)


# --- HTTP app ----------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tg_client
    if not BRIDGE_TOKEN:
        raise RuntimeError(
            "BRIDGE_TOKEN is not set — refusing to start unauthenticated. "
            "First run? python bridge.py setup"
        )
    if TELEGRAM_ENABLED:
        if not (TG_API_ID and TG_API_HASH and ASSISTANT_CHAT):
            raise RuntimeError(
                "TELEGRAM_ENABLED but TG_API_ID/TG_API_HASH/ASSISTANT_CHAT missing "
                "— run: python bridge.py setup"
            )
        if not (HERE / f"{SESSION_NAME}.session").exists():
            raise RuntimeError(
                f"TELEGRAM_ENABLED but no {SESSION_NAME}.session found "
                "— run: python bridge.py login"
            )
        tg_client = make_tg_client()
        await tg_client.start()
    async with mcp.session_manager.run():
        yield
    if tg_client is not None:
        await tg_client.stop()


app = FastAPI(lifespan=lifespan)


def token_ok(authorization: str | None) -> bool:
    return authorization is not None and hmac.compare_digest(
        authorization, f"Bearer {BRIDGE_TOKEN}"
    )


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # The MCP mount bypasses route-level auth, so gate it here.
    if request.url.path.startswith(MCP_MOUNT):
        if not token_ok(request.headers.get("authorization")):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


# host="0.0.0.0" matters: with the default localhost host the SDK auto-enables
# DNS-rebinding protection whose allowlist would reject the phone's LAN or
# tailnet Host header (lowlevel/server.py:739).
app.mount(
    MCP_MOUNT,
    McpMethodLogger(
        mcp.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0")
    ),
)


@app.get("/healthz")
async def healthz():
    return {"ok": True, "telegram": TELEGRAM_ENABLED}


# --- CLI ----------------------------------------------------------------------


def login() -> None:
    """One-time interactive Pyrogram login; creates the session file."""
    if not (TG_API_ID and TG_API_HASH):
        sys.exit("Set TG_API_ID and TG_API_HASH in .env first (python bridge.py setup)")
    print("Log in with YOUR Telegram account — the bridge posts as you.")
    print("Phone number must be in full international format, e.g. +15125551234")
    print("(leading + and country code are required — a bare local number is rejected)\n")
    client = make_tg_client()
    client.start()
    me = client.get_me()
    print(f"\nLogged in as {me.first_name} (@{me.username}) — session saved as {SESSION_NAME}.session")
    print("If TELEGRAM_ENABLED=false in .env, flip it to true, then start the server.")
    client.stop()


def setup() -> None:
    """Interactive first-run walkthrough: collects every secret, writes .env."""
    import secrets

    env_path = HERE / ".env"
    if env_path.exists():
        print(f"{env_path} already exists — edit it directly, or delete it and rerun setup.")
        missing = [
            k for k in ("BRIDGE_TOKEN", "TG_API_ID", "TG_API_HASH", "ASSISTANT_CHAT")
            if not os.environ.get(k)
        ]
        if missing:
            print(f"Currently missing or empty: {', '.join(missing)}")
        return

    print("ring-bridge setup — four things, about three minutes.\n")

    token = secrets.token_urlsafe(32)
    print("1. Bridge token — generated for you:")
    print(f"     {token}")
    print("   The Pebble app must send it as the header 'Authorization: Bearer <token>'.\n")

    print("2. Telegram API credentials — create an app at https://my.telegram.org/apps")
    print("   (any app name works; you only need the two values):")
    api_id = input("     TG_API_ID: ").strip()
    api_hash = input("     TG_API_HASH: ").strip()

    print("\n3. The Telegram chat your assistant lives in — the DM transcripts should land in:")
    chat = input("     ASSISTANT_CHAT (@botusername or chat id): ").strip()

    print("\n4. Your assistant's name — becomes the MCP tool name the ring app's LLM sees")
    print("   (e.g. 'Hermes' -> send_to_hermes):")
    name = input("     ASSISTANT_NAME [assistant]: ").strip() or "assistant"

    env_path.write_text(
        f"BRIDGE_TOKEN={token}\n"
        "TELEGRAM_ENABLED=true\n"
        f"ASSISTANT_CHAT={chat}\n"
        f"ASSISTANT_NAME={name}\n"
        f"TG_API_ID={api_id}\n"
        f"TG_API_HASH={api_hash}\n"
        "SESSION_NAME=ring_bridge\n"
        "# RING_PREFIX=\U0001f3a4   # prefix on relayed messages\n"
        "# MCP_MOUNT=/bridge   # MCP endpoint becomes <MCP_MOUNT>/mcp\n"
    )
    env_path.chmod(0o600)
    print(f"\nWrote {env_path} (mode 600). Next steps:")
    print("  1. python bridge.py login")
    print("  2. .venv/bin/uvicorn bridge:app --host <ip-the-phone-can-reach> --port 8787")
    print("  3. Pebble app → Index settings → MCP servers:")
    print("       URL:    http://<that-ip>:8787/bridge/mcp   (transport: Streamable)")
    print("       Header: Authorization: Bearer <the token above>")
    print("       Server name: NO spaces (space-named servers break tool dispatch)")
    print("     then Secondary Mode → MCP Sandbox → select the server group.")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "setup":
        setup()
    elif len(sys.argv) > 1 and sys.argv[1] == "login":
        login()
    else:
        print(__doc__)
