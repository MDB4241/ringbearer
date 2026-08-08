#!/usr/bin/env python3
"""
ringbearer: Pebble Index 01 ring -> your AI assistant's Telegram DM.

One does not simply type. Ringbearer carries your spoken words from the ring
to your assistant, delivered as you.

Double-click-hold the ring and speak. The Pebble app's MCP sandbox agent calls
this bridge's send_to_<assistant> tool with the transcript, and the bridge
posts it into your Telegram DM with the assistant -- as you, from your own
Telegram account -- so one thread carries the whole conversation and the
assistant replies exactly as it would to a typed message.

Works with any assistant that lives in a Telegram chat: Hermes, OpenClaw,
a bot you wrote yourself.

Quick start — one command, one loop:

  python ringbearer.py      first run: collects every secret, logs into Telegram,
                        starts the server. Every run after: just starts the
                        server. It resumes from whatever state exists on disk.

Pieces, if you ever need one alone:

  python ringbearer.py setup    collect secrets, write .env
  python ringbearer.py login    (re)create the Telegram session
  python ringbearer.py run      start the server only — non-interactive by design,
                            so launchd/systemd can never hang on a prompt
  python ringbearer.py probe    client-side diagnostic: connect like the phone
                            would and call the tool (dry run; --live sends)
  python ringbearer.py service  install the launchd agent — a real background
                            service (macOS; `service uninstall` removes it)

Endpoints:
  <MCP_MOUNT>/mcp   MCP server (Streamable HTTP), bearer-token gated.
  /healthz          Open liveness check.
"""

import sys

if sys.version_info < (3, 10):
    sys.exit(f"ringbearer needs Python 3.10+ (you have {sys.version.split()[0]}).")

import asyncio
import hmac
import json
import os
import re
import socket
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.server import MCPServer

# Keep the room quiet: Telethon narrates connections and updates, and the MCP
# SDK logs "Terminating session" per stateless request — noise that buries the
# output that matters.
import logging

logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

HERE = Path(__file__).parent
# Private mutable state (.env, the Telegram session, captures.jsonl) lives in
# RINGBEARER_STATE_DIR when set — the seam that keeps a Docker image
# disposable while user data persists. Unset, it IS the checkout, and nothing
# changes for native installs. Process environment only, never .env: this
# value decides where .env is. Must be absolute — a relative path would move
# with the launching directory, silently splitting state between cwds; empty
# means unset, not cwd.
_state_raw = os.environ.get("RINGBEARER_STATE_DIR", "").strip()
if _state_raw and not Path(_state_raw).expanduser().is_absolute():
    sys.exit(f"RINGBEARER_STATE_DIR must be an absolute path (got: {_state_raw!r})")
STATE_DIR = (Path(_state_raw).expanduser() if _state_raw else HERE).resolve()
load_dotenv(STATE_DIR / ".env")

CAPTURES = STATE_DIR / "captures.jsonl"
DRY_RUN_PREFIX = "DRYRUN:"

# Terminal color helpers — plain when piped or NO_COLOR is set.
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _sgr(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _COLOR else s


def bold(s: str) -> str:
    return _sgr("1", s)


def dim(s: str) -> str:
    return _sgr("2", s)


def green(s: str) -> str:
    return _sgr("32", s)


def yellow(s: str) -> str:
    return _sgr("1;33", s)


def cyan(s: str) -> str:
    return _sgr("1;36", s)

BRIDGE_TOKEN = os.environ.get("BRIDGE_TOKEN", "")
TELEGRAM_ENABLED = os.environ.get("TELEGRAM_ENABLED", "false").lower() == "true"
NEW_TOPIC_PER_CAPTURE = (
    os.environ.get("NEW_TOPIC_PER_CAPTURE", "false").lower() == "true"
)
# @username, or a bare numeric chat id. A digits-only STRING gets resolved as
# a phone number, so numeric ids are coerced to int. Numeric ids are best-
# effort in Telethon — they resolve only from the session file's own entity
# cache, which starts EMPTY after a fresh login (using the chat in the
# official apps populates nothing here). The server therefore resolves the
# chat once at startup and fails fast with advice, instead of failing on
# every send. @username always works.
_chat = os.environ.get("ASSISTANT_CHAT", "")
ASSISTANT_CHAT = int(_chat) if re.fullmatch(r"-?\d+", _chat) else _chat
ASSISTANT_NAME = os.environ.get("ASSISTANT_NAME", "assistant")
RING_PREFIX = os.environ.get("RING_PREFIX", "\U0001f3a4 ")
TG_API_ID = os.environ.get("TG_API_ID", "")
if TG_API_ID and not TG_API_ID.isdigit():
    sys.exit(f"TG_API_ID in .env is not a number — edit {STATE_DIR / '.env'}")
TG_API_HASH = os.environ.get("TG_API_HASH", "")
SESSION_NAME = os.environ.get("SESSION_NAME", "ringbearer")
# Telethon appends ".session" only when the name lacks it — a name already
# carrying the suffix would desync every existence check and the post-login
# chmod (checks would look for name.session.session). Normalize once here.
if SESSION_NAME.endswith(".session"):
    SESSION_NAME = SESSION_NAME[: -len(".session")]
# A bare file name only: a separator or dot-dot would silently escape
# STATE_DIR's containment promise (Path("/data") / "/tmp/x" IS /tmp/x).
if "/" in SESSION_NAME or SESSION_NAME in ("", ".", ".."):
    sys.exit(f"SESSION_NAME must be a bare file name, not a path (got: {SESSION_NAME!r})")
MCP_MOUNT = os.environ.get("MCP_MOUNT", "/ringbearer")
BIND_HOST = os.environ.get("BIND_HOST", "")
try:
    BIND_PORT = int(os.environ.get("BIND_PORT", "8787"))
except ValueError:
    sys.exit(f"BIND_PORT in .env is not a number — edit {STATE_DIR / '.env'}")

# The tool name the ring app's LLM sees, e.g. send_to_hermes. Keep it free of
# anything but [a-z0-9_]: the app sanitizes names for the LLM but dispatches on
# the original, so exotic characters break the round-trip (reported upstream).
ASSISTANT_SLUG = re.sub(r"[^a-z0-9_]+", "_", ASSISTANT_NAME.lower()).strip("_") or "assistant"
TOOL_NAME = f"send_to_{ASSISTANT_SLUG}"

tg_client = None
assistant_entity = None  # resolved once at startup; see lifespan


def make_tg_client():
    from telethon import TelegramClient

    # The session path is explicit and absolute, so the server and
    # `ringbearer.py login` always share one file no matter which directory
    # launched the process (uvicorn, launchd, a shell — all the same).
    return TelegramClient(
        str(STATE_DIR / SESSION_NAME),
        int(TG_API_ID),
        TG_API_HASH,
    )


def log_capture(row: dict) -> None:
    CAPTURES.touch(mode=0o600, exist_ok=True)
    with CAPTURES.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def topic_title(message: str) -> str:
    normalized = " ".join(message.split())
    return normalized[:80] or "Ring capture"


def delivery_mode_label() -> str:
    if NEW_TOPIC_PER_CAPTURE:
        return "new topic per capture"
    return "current Telegram conversation"


def created_topic_root_id(result) -> int | None:
    from telethon.tl.types import MessageActionTopicCreate

    for update in getattr(result, "updates", ()):
        message = getattr(update, "message", None)
        if isinstance(getattr(message, "action", None), MessageActionTopicCreate):
            return getattr(message, "id", None)
    return None


async def deliver(message: str) -> bool:
    """Post into the assistant DM as the user. Returns True if actually sent."""
    if not TELEGRAM_ENABLED or tg_client is None:
        return False

    entity = assistant_entity if assistant_entity is not None else ASSISTANT_CHAT
    send_kwargs = {}
    if NEW_TOPIC_PER_CAPTURE:
        from telethon.tl.functions.messages import CreateForumTopicRequest

        result = await tg_client(
            CreateForumTopicRequest(peer=entity, title=topic_title(message))
        )
        topic_root_id = created_topic_root_id(result)
        if topic_root_id is None:
            raise RuntimeError("Telegram topic creation returned no topic root message")
        send_kwargs["reply_to"] = topic_root_id

    # parse_mode=None: the transcript is a promise ("verbatim and in
    # full"), so *, _, ` and [] must arrive as characters, not formatting.
    await tg_client.send_message(
        entity,
        f"{RING_PREFIX}{message}",
        parse_mode=None,
        **send_kwargs,
    )
    return True


# --- MCP server ---------------------------------------------------------------

mcp = MCPServer("ringbearer")


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
    err = None
    try:
        # Bounded: a Telegram stall must not hold the transcript hostage —
        # timeout lands in the except and the capture row still gets written.
        sent = await asyncio.wait_for(deliver(message), timeout=30)
    except Exception as e:  # the transcript is the only copy — log it no matter what
        sent, err = False, repr(e)
    send_ms = round((time.monotonic() - started) * 1000)
    print(f"[mcp] {TOOL_NAME}: telegram {send_ms}ms" + (f" ERROR {err}" if err else ""), flush=True)
    row = {
        "received_at": datetime.now().astimezone().isoformat(),
        "source": "mcp",
        "transcription": message,
        "forwarded": sent,
        "telegram_ms": send_ms,
    }
    if err:
        row["error"] = err
    log_capture(row)
    if sent:
        return f"Delivered. {ASSISTANT_NAME} will reply in Telegram."
    if err:
        return f"Logged locally, but Telegram delivery failed: {err}"
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
            "First run? python ringbearer.py setup"
        )
    if TELEGRAM_ENABLED:
        if not (TG_API_ID and TG_API_HASH and ASSISTANT_CHAT):
            raise RuntimeError(
                "TELEGRAM_ENABLED but TG_API_ID/TG_API_HASH/ASSISTANT_CHAT missing "
                "— run: python ringbearer.py setup"
            )
        if not (STATE_DIR / f"{SESSION_NAME}.session").exists():
            raise RuntimeError(
                f"TELEGRAM_ENABLED but no {SESSION_NAME}.session found "
                "— run: python ringbearer.py login"
            )
        from telethon.errors import FloodWaitError

        # Constructing the client OPENS the session database — a Pyrogram-era
        # file at the same path dies right here with an sqlite error, so the
        # migration advice must wrap construction, not just connect().
        try:
            tg_client = make_tg_client()
        except Exception as e:
            raise RuntimeError(
                f"Can't open {SESSION_NAME}.session ({type(e).__name__}: {e}).\n"
                f"If it predates the Telethon migration (Pyrogram), delete it "
                f"and run: python ringbearer.py login"
            ) from e
        # connect(), never start(): start() would prompt for a phone number on
        # an unauthorized session, and this path must stay launchd-safe. A bad
        # session fails fast with the fix named instead of a crash-loop
        # traceback (or worse, a silent hang on a prompt no one will answer).
        # Bounded like deliver(): a half-open connection must not leave the
        # lifespan pending forever with /healthz never binding.
        try:
            await asyncio.wait_for(tg_client.connect(), timeout=30)
            authorized = await asyncio.wait_for(
                tg_client.is_user_authorized(), timeout=30
            )
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
                f"If {SESSION_NAME}.session was revoked, delete it and run: "
                "python ringbearer.py login"
            ) from e
        if not authorized:
            raise RuntimeError(
                f"{SESSION_NAME}.session exists but is not authorized (revoked, "
                "terminated, or written by a different library) — delete it "
                "and run: python ringbearer.py login"
            )
        # Resolve the assistant chat once, up front. Numeric ids depend on the
        # session's entity cache and would otherwise fail on EVERY send —
        # silently, from the ring's point of view. Better: refuse to start,
        # with the fix named.
        global assistant_entity
        try:
            assistant_entity = await asyncio.wait_for(
                tg_client.get_input_entity(ASSISTANT_CHAT), timeout=30
            )
        except Exception as e:
            raise RuntimeError(
                f"Can't resolve ASSISTANT_CHAT ({ASSISTANT_CHAT!r}): "
                f"{type(e).__name__}: {e}\n"
                "A numeric id only works for chats this account has already "
                "seen from this session; the @username form always works — "
                f"set it in {STATE_DIR / '.env'}."
            ) from e
    async with mcp.session_manager.run():
        yield
    if tg_client is not None:
        await tg_client.disconnect()


# Docs/OpenAPI off: nothing here is browsable, and the README's "everything
# except /healthz requires the token" claim should be literally true.
app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def token_ok(authorization: str | None) -> bool:
    # Compare bytes: compare_digest raises TypeError on non-ASCII str input,
    # and Starlette decodes headers as latin-1 — a malformed header should be
    # a clean 401, not a 500.
    return authorization is not None and hmac.compare_digest(
        authorization.encode("utf-8", "replace"), f"Bearer {BRIDGE_TOKEN}".encode()
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
    """One-time interactive Telegram login; creates the session file."""
    if not (TG_API_ID and TG_API_HASH):
        sys.exit("Set TG_API_ID and TG_API_HASH in .env first (python ringbearer.py setup)")
    # Refuse while the server is up: two clients on one session file can
    # trigger AUTH_KEY_DUPLICATED and get the Telegram session revoked.
    s = socket.socket()
    s.settimeout(0.5)
    server_up = s.connect_ex((BIND_HOST or "127.0.0.1", BIND_PORT)) == 0
    s.close()
    if server_up:
        sys.exit(
            "The bridge appears to be running — stop it first (Ctrl-C it, or "
            "launchctl unload the agent).\nTwo clients on one session file can "
            "get your Telegram session revoked."
        )
    print("Log in with YOUR Telegram account — the bridge posts as you.")
    print("Phone number format: " + bold("+<country code><number>, digits only") + " — e.g. +12025550143")
    print("(the leading + and country code are required — that's the +1 for the US —")
    print(" and no hyphens, spaces, or parentheses; a bare local number is rejected)\n")
    async def _login():
        # Pre-create the session file owner-only: Telethon would otherwise
        # create it at the umask default, leaving the account-bearing file
        # world-readable for the whole interactive window below.
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        (STATE_DIR / f"{SESSION_NAME}.session").touch(mode=0o600, exist_ok=True)
        # Client construction and use stay inside one event loop — Telethon
        # binds the client to the loop it was created under. Construction
        # also OPENS the session DB: a Pyrogram-era file fails here.
        try:
            client = make_tg_client()
        except Exception as e:
            sys.exit(
                f"Can't open {SESSION_NAME}.session ({type(e).__name__}: {e}).\n"
                "If it predates the Telethon migration (Pyrogram), delete it "
                "and rerun: python ringbearer.py login"
            )
        await client.start()  # interactive here is the point: phone, code, 2FA
        me = await client.get_me()
        await client.disconnect()
        return me

    me = asyncio.run(_login())
    handle = f"@{me.username}" if me.username else "no public username"
    print(green(f"\nLogged in as {me.first_name} ({handle}) — session saved as {SESSION_NAME}.session"))
    # The session file IS the Telegram account — created at the umask default
    # (644); pull it to owner-only like .env and captures.jsonl.
    for f in STATE_DIR.glob(f"{SESSION_NAME}.session*"):
        f.chmod(0o600)


def setup() -> None:
    """Interactive first-run walkthrough: collects every secret, writes .env."""
    import secrets

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    env_path = STATE_DIR / ".env"
    if env_path.exists():
        print(f"{env_path} already exists — edit it directly, or delete it and rerun setup.")
        missing = [
            k for k in ("BRIDGE_TOKEN", "TG_API_ID", "TG_API_HASH", "ASSISTANT_CHAT")
            if not os.environ.get(k)
        ]
        if missing:
            print(f"Currently missing or empty: {', '.join(missing)}")
        if BRIDGE_TOKEN and BIND_HOST:
            print_phone_settings(BIND_HOST, str(BIND_PORT), BRIDGE_TOKEN)
        return

    def ask(prompt: str, *, default: str | None = None, numeric: bool = False) -> str:
        while True:
            raw = input(prompt).strip()
            if not raw and default is not None:
                return default
            if not raw:
                print(dim("     (required — this one can't be blank)"))
                continue
            if numeric and not raw.lstrip("-").isdigit():
                print(dim("     (must be a number)"))
                continue
            return raw

    print(bold("ringbearer setup") + " — five questions, about three minutes.\n")

    token = secrets.token_urlsafe(32)
    print(cyan("1. Bridge token") + " — generated for you:")
    print(f"     {yellow(token)}")
    print("   The Pebble app must send it as the header 'Authorization: Bearer <token>'.\n")

    print(cyan("2. Telegram API credentials") + " — create an app at https://my.telegram.org/apps")
    print("   (any app name works; you only need the two values):")
    api_id = ask("     TG_API_ID: ", numeric=True)
    api_hash = ask("     TG_API_HASH: ")

    print("\n" + cyan("3. The Telegram chat your assistant lives in") + " — the DM transcripts should land in:")
    chat = ask("     ASSISTANT_CHAT (@botusername or chat id): ")

    print("\n" + cyan("4. Your assistant's name") + " — becomes the MCP tool name the ring app's LLM sees")
    print("   (e.g. 'Hermes' -> send_to_hermes):")
    name = ask("     ASSISTANT_NAME [assistant]: ", default="assistant")

    # A container (or any wrapper) bakes BIND_HOST/BIND_PORT into the process
    # environment, and the environment beats .env at runtime — asking here
    # would collect an answer that could never take effect. Skip the question
    # and say where the values came from.
    if os.environ.get("BIND_HOST"):
        host = os.environ["BIND_HOST"]
        port = os.environ.get("BIND_PORT", "8787")
        print("\n" + cyan("5. Listen address") + f" — already set by the environment: {host}:{port}")
    else:
        print("\n" + cyan("5. Where should the server listen?") + " Give the IP your phone can reach")
        print("   this machine at:")
        print("   - Tailscale IP (100.x.x.x) — recommended: works from anywhere, and")
        print("     the port is never visible to your LAN or the internet.")
        print("   - LAN IP (192.168.x.x) — works, but only while your phone is on the")
        print("     same network, and anyone on that network can reach the port.")
        print("   Never expose this to the public internet.")
        while True:
            host = ask("     BIND_HOST: ")
            try:
                _s = socket.socket()
                _s.bind((host, 0))
                _s.close()
                break
            except OSError:
                print(dim("     (this machine can't bind that address — is Tailscale up?"))
                print(dim("      `ifconfig` shows what's available)"))
        port = ask("     BIND_PORT [8787]: ", default="8787", numeric=True)

    # Restricted from birth: no umask-default window with the token inside.
    env_path.touch(mode=0o600)
    env_path.write_text(
        f"BRIDGE_TOKEN={token}\n"
        "TELEGRAM_ENABLED=true\n"
        "NEW_TOPIC_PER_CAPTURE=false\n"
        f"ASSISTANT_CHAT={chat}\n"
        f"ASSISTANT_NAME={name}\n"
        f"TG_API_ID={api_id}\n"
        f"TG_API_HASH={api_hash}\n"
        f"BIND_HOST={host}\n"
        f"BIND_PORT={port}\n"
        "SESSION_NAME=ringbearer\n"
        "# RING_PREFIX=\U0001f3a4   # prefix on relayed messages\n"
        "# MCP_MOUNT=/ringbearer   # MCP endpoint becomes <MCP_MOUNT>/mcp\n"
    )
    env_path.chmod(0o600)
    print(green(f"\nWrote {env_path} (mode 600)."))
    print_phone_settings(host, port, token)


def required_missing() -> list[str]:
    """Names of required .env keys that are empty. A .env can exist and still
    be unusable (blank answers, hand-edits) — existence is not validity."""
    missing = [k for k, v in (("BRIDGE_TOKEN", BRIDGE_TOKEN), ("BIND_HOST", BIND_HOST)) if not v]
    if TELEGRAM_ENABLED:
        missing += [
            k for k, v in (
                ("TG_API_ID", TG_API_ID),
                ("TG_API_HASH", TG_API_HASH),
                ("ASSISTANT_CHAT", ASSISTANT_CHAT),
            ) if not v
        ]
    return missing


def check_bindable(host: str) -> None:
    """This machine must actually hold the address — otherwise uvicorn dies
    later with a bare errno at the moment the user expects a running server."""
    try:
        s = socket.socket()
        s.bind((host, 0))
        s.close()
    except OSError as e:
        sys.exit(
            f"Can't bind {host} ({e.strerror or e}) — this machine doesn't hold that\n"
            "address right now. Is Tailscale up? `ifconfig` lists what's available;\n"
            f"fix BIND_HOST in {STATE_DIR / '.env'}."
        )


def print_phone_settings(host: str, port: str, token: str) -> None:
    print(bold("\nPebble app settings") + " (Index settings → MCP servers)")
    print(dim("  — reprint any time with `python ringbearer.py setup`"))
    if host == "0.0.0.0":
        # The listener answers on every interface, but the phone needs a real
        # address to dial — in Docker, the address the port is published on.
        host = "<this-machine's-address>"
        print(dim("  (0.0.0.0 is the listener, not a dialable address — in Docker, use"))
        print(dim("   the host address you published the port on)"))
    print(f"  URL:     {yellow(f'http://{host}:{port}{MCP_MOUNT}/mcp')}   (transport: Streamable)")
    print(f"  Header:  {yellow(f'Authorization: Bearer {token}')}")
    print(f"  Delivery: {delivery_mode_label()}")
    print("  Name:    anything " + bold("WITHOUT spaces") + " (a space breaks tool dispatch)")
    print("  Group:   model type Default, then Secondary Mode → MCP Sandbox → pick the group.")


def incomplete_env_exit(missing: list[str]) -> None:
    sys.exit(
        f".env is incomplete — missing: {', '.join(missing)}.\n"
        f"Edit {STATE_DIR / '.env'} (see .env.example), or delete it and rerun "
        "`python ringbearer.py` to redo setup."
    )


def run() -> None:
    """Start the server. Non-interactive by design (launchd-safe): missing
    state fails fast naming the fix — never a prompt."""
    missing = required_missing()
    if missing:
        incomplete_env_exit(missing)
    if TELEGRAM_ENABLED and not (STATE_DIR / f"{SESSION_NAME}.session").exists():
        sys.exit(f"No {SESSION_NAME}.session — run: python ringbearer.py login")
    check_bindable(BIND_HOST)
    import uvicorn

    print(bold(f"ringbearer → http://{BIND_HOST}:{BIND_PORT}{MCP_MOUNT}/mcp"))
    print(f"Telegram delivery: {delivery_mode_label()}")
    if not TELEGRAM_ENABLED:
        print(
            yellow(
                "WARNING: TELEGRAM_ENABLED=false — captures are logged to "
                "captures.jsonl only, NOT delivered to Telegram."
            ),
            flush=True,
        )
    uvicorn.run(app, host=BIND_HOST, port=BIND_PORT)


def probe(live: bool = False) -> None:
    """Client-side diagnostic: connect exactly like the phone would —
    initialize, list tools, call the send tool. Dry run by default; the
    assistant DM is a live channel and --live sends a real message it will
    act on. BRIDGE_URL overrides the target (e.g. probing a remote install)."""
    import logging

    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    logging.getLogger("httpx2").setLevel(logging.WARNING)

    url = os.environ.get("BRIDGE_URL") or (
        f"http://{BIND_HOST or 'localhost'}:{BIND_PORT}{MCP_MOUNT}/mcp"
    )
    text = "bridge probe, no reply needed"
    message = text if live else f"{DRY_RUN_PREFIX} {text}"
    print(f"probing {url}" + (" (LIVE)" if live else " (dry run)"))

    async def _probe() -> None:
        headers = {"Authorization": f"Bearer {BRIDGE_TOKEN}"}
        async with httpx2.AsyncClient(headers=headers, timeout=15) as http:
            async with streamable_http_client(url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    print("tools:", [t.name for t in tools.tools])
                    result = await session.call_tool(TOOL_NAME, {"message": message})
                    print("result:", result.content[0].text)

    try:
        asyncio.run(_probe())
    except Exception as e:
        sys.exit(
            f"FAILED ({type(e).__name__}): {e}\n"
            "Is the server running, and does BRIDGE_TOKEN match?"
        )


def render_plist(label: str) -> str:
    """The launchd plist with this checkout's real paths baked in."""
    python = HERE / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    logs = HERE / "logs"
    # launchd does NOT inherit the installing shell's environment: without
    # this block a custom RINGBEARER_STATE_DIR would silently revert to the
    # checkout inside the agent — no .env found, KeepAlive crash loop.
    env_block = ""
    if STATE_DIR != HERE.resolve():
        env_block = f"""    <key>EnvironmentVariables</key>
    <dict>
        <key>RINGBEARER_STATE_DIR</key>
        <string>{STATE_DIR}</string>
    </dict>
"""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        <string>{HERE / "ringbearer.py"}</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{HERE}</string>
{env_block}    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{logs / "ringbearer.log"}</string>
    <key>StandardErrorPath</key>
    <string>{logs / "ringbearer.error.log"}</string>
    <key>LimitLoadToSessionType</key>
    <array>
        <string>Aqua</string>
        <string>Background</string>
    </array>
</dict>
</plist>
"""


def service(uninstall: bool = False) -> None:
    """Install (or remove) the launchd user agent — the graduation from a
    terminal-tied server to one that survives reboots and crashes."""
    if sys.platform != "darwin":
        sys.exit("`service` is macOS-only (launchd). On Linux, run `ringbearer.py run` under systemd.")
    import getpass
    import subprocess

    label = f"com.{getpass.getuser()}.ringbearer"
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"

    if uninstall:
        if not plist_path.exists():
            print(f"Nothing installed ({plist_path} doesn't exist).")
            return
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink()
        print(green(f"Stopped and removed {label}."))
        return

    for blocked in ("Documents", "Desktop", "Downloads"):
        for d, what in ((HERE.resolve(), "checkout"), (STATE_DIR, "state directory")):
            if Path.home() / blocked in d.parents:
                sys.exit(
                    f"The {what} lives under ~/{blocked}, which launchd agents can't read\n"
                    "(macOS TCC). Move it somewhere like ~/Projects and rerun."
                )
    missing = required_missing()
    if missing:
        incomplete_env_exit(missing)
    if TELEGRAM_ENABLED and not (STATE_DIR / f"{SESSION_NAME}.session").exists():
        sys.exit(f"No {SESSION_NAME}.session — run: python ringbearer.py login")
    check_bindable(BIND_HOST)
    s = socket.socket()
    s.settimeout(0.5)
    in_use = s.connect_ex((BIND_HOST, BIND_PORT)) == 0
    s.close()
    if in_use:
        sys.exit(
            f"Something already listens on {BIND_HOST}:{BIND_PORT} — Ctrl-C the "
            "foreground server first, then rerun."
        )

    (HERE / "logs").mkdir(exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(render_plist(label))
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)  # reinstall-safe
    r = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"launchctl load failed: {(r.stderr or r.stdout).strip()}")

    import urllib.request

    url = f"http://{BIND_HOST}:{BIND_PORT}/healthz"
    for _ in range(10):
        time.sleep(1)
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                body = resp.read().decode().strip()
            print(green(f"{label} is up — {url} → {body}"))
            break
        except OSError:
            continue
    else:
        print(yellow(f"Loaded, but {url} isn't answering yet — check {HERE / 'logs' / 'ringbearer.error.log'}"))
    print(f"Logs: {HERE / 'logs'} · stop: python ringbearer.py service uninstall")


def first_run(fresh: bool = False) -> None:
    """The whole onboarding as one loop: look at what exists on disk, collect
    what's missing, end with a running server. Safe to re-run forever."""
    if not (STATE_DIR / ".env").exists():
        if not sys.stdin.isatty():
            sys.exit(
                "No .env yet, and no terminal to ask questions in — run "
                "`python ringbearer.py` interactively once (or see .env.example)."
            )
        setup()
        # Re-exec so the fresh .env is loaded cleanly, then the loop continues
        # from the next missing piece (login). Flush first: exec replaces the
        # process image, and a piped stdout would silently lose the settings.
        # --fresh tells the next image this is still the first run.
        sys.stdout.flush()
        os.execv(sys.executable, [sys.executable, str(HERE / "ringbearer.py"), "--fresh"])
    missing = required_missing()
    if missing:
        incomplete_env_exit(missing)
    just_logged_in = False
    if TELEGRAM_ENABLED and not (STATE_DIR / f"{SESSION_NAME}.session").exists():
        if not sys.stdin.isatty():
            sys.exit(f"No {SESSION_NAME}.session — run: python ringbearer.py login")
        print("One more thing: Telegram login.\n")
        login()
        just_logged_in = True
        print()
    if fresh or just_logged_in:
        # First run only: the settings the user is about to need, reprinted at
        # the moment of need — setup's earlier printout has scrolled away by
        # now, buried under the login exchange.
        print_phone_settings(BIND_HOST, str(BIND_PORT), BRIDGE_TOKEN)
        print()
        # And make clear the foreground server is the TEST posture, plus how
        # to graduate it to a real service.
        print(bold("Starting the server in THIS terminal") + " so you can watch it work —")
        print("double-click your ring and the tool call will log below. Ctrl-C stops it.")
        # The graduation advice depends on where this is running: `service` is
        # launchd and only exists on a Mac — in a container or on Linux it
        # would just be a dead end printed at the moment of success.
        print("To run it permanently in the background instead:")
        if sys.platform == "darwin":
            print("  " + bold("python ringbearer.py service") + "   (macOS, one command)")
        else:
            print("  Docker: Ctrl-C, then " + bold("docker compose -f docker/compose.yml up -d"))
            print("  Linux (native): run `ringbearer.py run` under systemd")
        print(dim("(Details: README → Running it as a service / Docker.)\n"))
    run()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "setup":
            setup()
        elif cmd == "login":
            login()
        elif cmd == "run":
            run()
        elif cmd == "probe":
            probe(live="--live" in sys.argv)
        elif cmd == "service":
            service(uninstall=len(sys.argv) > 2 and sys.argv[2] == "uninstall")
        elif cmd == "":
            first_run()
        elif cmd == "--fresh":
            first_run(fresh=True)
        else:
            print(__doc__)
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nCancelled. Rerun `python ringbearer.py` when ready.")
    except PermissionError as e:
        sys.exit(
            f"Permission denied: {e}\n"
            f"Can this user write {STATE_DIR}? In Docker, see docker/README.md "
            "→ Troubleshooting (bind-mount ownership)."
        )
