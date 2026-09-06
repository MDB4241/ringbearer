"""The command line: setup, login, run, probe, service, and the first-run loop
that ties them together.

Also the terminal colour helpers, which nothing outside this module uses —
the server writes to a log, not to a person.
"""

import asyncio
import os
import socket
import sys
import time
from pathlib import Path

from . import __doc__ as USAGE
from . import config, telegram
from .app import app

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


def login() -> None:
    """One-time interactive Telegram login; creates the session file."""
    if not (config.TG_API_ID and config.TG_API_HASH):
        sys.exit("Set TG_API_ID and TG_API_HASH in .env first (python ringbearer.py setup)")
    # Refuse while the server is up: two clients on one session file can
    # trigger AUTH_KEY_DUPLICATED and get the Telegram session revoked.
    s = socket.socket()
    s.settimeout(0.5)
    server_up = s.connect_ex((config.BIND_HOST or "127.0.0.1", config.BIND_PORT)) == 0
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
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        (config.STATE_DIR / f"{config.SESSION_NAME}.session").touch(mode=0o600, exist_ok=True)
        # Client construction and use stay inside one event loop — Telethon
        # binds the client to the loop it was created under. Construction
        # also OPENS the session DB: a Pyrogram-era file fails here.
        try:
            client = telegram.make_tg_client()
        except Exception as e:
            sys.exit(
                f"Can't open {config.SESSION_NAME}.session ({type(e).__name__}: {e}).\n"
                "If it predates the Telethon migration (Pyrogram), delete it "
                "and rerun: python ringbearer.py login"
            )
        await client.start()  # interactive here is the point: phone, code, 2FA
        me = await client.get_me()
        await client.disconnect()
        return me

    me = asyncio.run(_login())
    handle = f"@{me.username}" if me.username else "no public username"
    print(green(f"\nLogged in as {me.first_name} ({handle}) — session saved as {config.SESSION_NAME}.session"))
    # The session file IS the Telegram account — created at the umask default
    # (644); pull it to owner-only like .env and captures.jsonl.
    for f in config.STATE_DIR.glob(f"{config.SESSION_NAME}.session*"):
        f.chmod(0o600)


def setup() -> None:
    """Interactive first-run walkthrough: collects every secret, writes .env."""
    import secrets

    config.STATE_DIR.mkdir(parents=True, exist_ok=True)
    env_path = config.STATE_DIR / ".env"
    if env_path.exists():
        print(f"{env_path} already exists — edit it directly, or delete it and rerun setup.")
        missing = [
            k for k in ("BRIDGE_TOKEN", "TG_API_ID", "TG_API_HASH", "ASSISTANT_CHAT")
            if not os.environ.get(k)
        ]
        if missing:
            print(f"Currently missing or empty: {', '.join(missing)}")
        if config.BRIDGE_TOKEN and config.BIND_HOST:
            print_phone_settings(config.BIND_HOST, str(config.BIND_PORT), config.BRIDGE_TOKEN)
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

    print(bold("ringbearer setup") + " — six questions, about three minutes.\n")

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

    print("\n" + cyan("4. Your assistant's name") + " — how the tool describes your assistant")
    print("   to the ring app's LLM, and how replies refer to it (e.g. 'Hermes'):")
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

    # The one question that decides which doors this install uses. One
    # assistant needs only the webhook: the ring posts straight here and the
    # app's cloud agent is not in the path at all. Several assistants need the
    # MCP route, because routing by spoken name is what that route is for.
    print("\n" + cyan("6. Do you have more than one assistant?"))
    print("   One — the ring posts straight to it over the webhook: no cloud")
    print("   agent in the middle, nothing to address by name.")
    print("   Several — the MCP route adds routing by spoken name ('ask plutus")
    print("   to check my portfolio'), through the app's cloud agent.")
    multiple = ask("     More than one assistant? [y/N]: ", default="n").lower()
    extras: list[tuple[str, str]] = []
    if multiple.startswith("y"):
        print("\n   Name each extra assistant and the chat it lives in.")
        print("   Names are short lowercase tokens — you say them out loud.")
        while True:
            extra = ask("     Assistant name (blank when done): ", default="")
            if not extra:
                break
            try:
                config.parse_assistants(f"{extra}:placeholder")
            except ValueError as e:
                print(dim(f"     ({e})"))
                continue
            if extra == config.slug(name) or any(extra == n for n, _ in extras):
                print(dim(f"     ({extra} is already taken)"))
                continue
            extras.append(
                (extra, ask(f"     Chat for {extra} (@botusername or chat id): "))
            )
    routes = ("webhook", "mcp") if extras else ("webhook",)

    # Restricted from birth: no umask-default window with the token inside.
    env_path.touch(mode=0o600)
    env_path.write_text(
        f"BRIDGE_TOKEN={token}\n"
        "TELEGRAM_ENABLED=true\n"
        f"ROUTES={','.join(routes)}\n"
        "NEW_TOPIC_PER_CAPTURE=false\n"
        "DELIVERY_CONTEXT=conversation\n"
        f"ASSISTANT_CHAT={chat}\n"
        f"ASSISTANT_NAME={name}\n"
        + (
            f"ASSISTANTS={','.join(f'{n}:{c}' for n, c in extras)}\n"
            if extras
            else ""
        )
        + f"TG_API_ID={api_id}\n"
        f"TG_API_HASH={api_hash}\n"
        f"BIND_HOST={host}\n"
        f"BIND_PORT={port}\n"
        "SESSION_NAME=ringbearer\n"
        "# RING_PREFIX=\U0001f3a4   # prefix on relayed messages\n"
        "# MCP_MOUNT=/ringbearer   # MCP endpoint becomes <MCP_MOUNT>/mcp\n"
        "# WEBHOOK_TOPIC_PER_CAPTURE=false     # per-route override of NEW_TOPIC_PER_CAPTURE\n"
        "# WEBHOOK_DELIVERY_CONTEXT=one_shot   # per-route override of DELIVERY_CONTEXT\n"
        "# MCP_TOPIC_PER_CAPTURE=false         # the same two knobs, for the MCP route\n"
        "# MCP_DELIVERY_CONTEXT=conversation\n"
    )
    env_path.chmod(0o600)
    print(green(f"\nWrote {env_path} (mode 600)."))
    print_phone_settings(host, port, token, routes)


def required_missing() -> list[str]:
    """Names of required .env keys that are empty. A .env can exist and still
    be unusable (blank answers, hand-edits) — existence is not validity."""
    missing = [k for k, v in (("BRIDGE_TOKEN", config.BRIDGE_TOKEN), ("BIND_HOST", config.BIND_HOST)) if not v]
    if config.TELEGRAM_ENABLED:
        missing += [
            k for k, v in (
                ("TG_API_ID", config.TG_API_ID),
                ("TG_API_HASH", config.TG_API_HASH),
                ("ASSISTANT_CHAT", config.ASSISTANT_CHAT),
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
            f"fix BIND_HOST in {config.STATE_DIR / '.env'}."
        )


def print_webhook_card(host: str, port: str, token: str) -> None:
    """The fast door's phone settings: a recording gesture, routed AND saved.

    Both halves matter and the app does not say so. "Webhook only" is a
    routing choice; the URL and headers live in a separate per-gesture webhook
    config that fires under every destination except "Nothing".
    """
    print(bold("\nPebble app settings — webhook route") + " (Index settings → the button switchboard)")
    print("  " + bold("Two halves, both required:"))
    print("   1. Route a recording gesture — Hold & Talk, or Double click & hold —")
    print('      to "Webhook only".')
    print("   2. Open that same gesture's Webhook settings and save the URL and")
    print("      header below. The route alone sends nothing, and a saved webhook")
    print("      on a gesture routed somewhere else delivers twice.")
    print(f"  URL:     {yellow(f'http://{host}:{port}{config.MCP_MOUNT}/webhook')}")
    print(f"  Header:  {yellow(f'Authorization: Bearer {token}')}")
    print('  Payload: "Recording only" (the app\'s default) sends the audio and this')
    print('           bridge makes the text. "Both" or "Transcription only" sends the')
    print("           phone's own transcript, and the bridge relays it as-is.")
    print(f"  Delivery: {telegram.delivery_mode_label(telegram.dials_for('webhook'))}")
    print("  First check: " + bold("Send test event") + " in those webhook settings — the bridge")
    print("           answers 200 and forwards nothing.")
    print('  On any gesture you leave on MCP sandbox, turn "Also send to webhook" ' + bold("off") + ".")


def print_mcp_card(host: str, port: str, token: str) -> None:
    """The named-routing door's phone settings: Index settings → MCP servers."""
    print(bold("\nPebble app settings — MCP route") + " (Index settings → MCP servers)")
    print(f"  URL:     {yellow(f'http://{host}:{port}{config.MCP_MOUNT}/mcp')}   (transport: Streamable)")
    print(f"  Header:  {yellow(f'Authorization: Bearer {token}')}")
    print(f"  Delivery: {telegram.delivery_mode_label(telegram.dials_for('mcp'))}")
    print("  Name:    anything " + bold("WITHOUT spaces") + " (a space breaks tool dispatch)")
    print("  Group:   model type Default, then Secondary Mode → MCP Sandbox → pick the group.")
    print("  Also send to webhook: " + bold("off") + " — a saved webhook fires on every")
    print('           destination except "Nothing", so one left here delivers twice.')


def print_phone_settings(host: str, port: str, token: str, routes=None) -> None:
    """Print the phone card for each configured route.

    Parameters:
      host, port, token: what the phone dials, and what it must send.
      routes (tuple[str, ...] | None): which cards to print. None means the
        configured ROUTES; `setup` passes the answer it just collected,
        because config read the environment before that .env existed.
    """
    routes = config.ROUTES if routes is None else routes
    if host == "0.0.0.0":
        # The listener answers on every interface, but the phone needs a real
        # address to dial — in Docker, the address the port is published on.
        host = "<this-machine's-address>"
        print(dim("\n  (0.0.0.0 is the listener, not a dialable address — in Docker, use"))
        print(dim("   the host address you published the port on)"))
    for route in routes:
        if route == "webhook":
            print_webhook_card(host, port, token)
        else:
            print_mcp_card(host, port, token)
    print(dim("  — reprint any time with `python ringbearer.py setup`"))


def incomplete_env_exit(missing: list[str]) -> None:
    sys.exit(
        f".env is incomplete — missing: {', '.join(missing)}.\n"
        f"Edit {config.STATE_DIR / '.env'} (see .env.example), or delete it and rerun "
        "`python ringbearer.py` to redo setup."
    )


def run() -> None:
    """Start the server. Non-interactive by design (launchd-safe): missing
    state fails fast naming the fix — never a prompt."""
    missing = required_missing()
    if missing:
        incomplete_env_exit(missing)
    if config.TELEGRAM_ENABLED and not (config.STATE_DIR / f"{config.SESSION_NAME}.session").exists():
        sys.exit(f"No {config.SESSION_NAME}.session — run: python ringbearer.py login")
    check_bindable(config.BIND_HOST)
    import uvicorn

    # The headline is the listener; the exact endpoint of each door lives on
    # that door's card below, so no URL is printed twice.
    print(bold(f"ringbearer → http://{config.BIND_HOST}:{config.BIND_PORT}"))
    print("Routes: " + ", ".join(config.ROUTES))
    if len(config.ASSISTANT_ROSTER) > 1:
        print("Assistants: " + ", ".join(
            f"{n} (default)" if n == config.DEFAULT_ASSISTANT else n
            for n in config.ASSISTANT_ROSTER
        ))
    if not config.TELEGRAM_ENABLED:
        print(
            yellow(
                "WARNING: TELEGRAM_ENABLED=false — captures are logged to "
                "captures.jsonl only, NOT delivered to Telegram."
            ),
            flush=True,
        )
    # Under launchd or Docker this banner lands in a log file that is not
    # mode 600 like .env, so the real token is shown only to a terminal.
    shown_token = config.BRIDGE_TOKEN if sys.stdout.isatty() else "<BRIDGE_TOKEN>"
    print_phone_settings(config.BIND_HOST, str(config.BIND_PORT), shown_token)
    print(flush=True)
    uvicorn.run(app, host=config.BIND_HOST, port=config.BIND_PORT)


def probe_urls() -> tuple[str, str]:
    """Where to probe: (mcp, webhook).

    BRIDGE_URL has always meant the MCP endpoint, so the webhook URL is
    derived from the same base rather than asking for a second variable — one
    install, one address, two doors on it.
    """
    base = os.environ.get("BRIDGE_URL", "").strip()
    if base:
        root = base[: -len("/mcp")] if base.endswith("/mcp") else base.rstrip("/")
    else:
        root = f"http://{config.BIND_HOST or 'localhost'}:{config.BIND_PORT}{config.MCP_MOUNT}"
    return f"{root}/mcp", f"{root}/webhook"


def probe_message(live: bool) -> str:
    """What a probe says. Dry run unless --live: the DRYRUN prefix is what
    makes the bridge log the capture and stop, instead of putting a probe in
    a real assistant's DM (A4)."""
    text = "bridge probe, no reply needed"
    return text if live else f"{config.DRY_RUN_PREFIX} {text}"


def phone_multipart(transcription: str) -> tuple[bytes, str]:
    """The app's own body: `transcription`, `recordedAt` (epoch milliseconds)
    and `client=ring`, in that order, with a UUID boundary.

    No `audio` part — the probe proves the route, the token and the relay, not
    the transcription engine.

    Returns: (body, the Content-Type value that describes it).
    """
    from uuid import uuid4

    boundary = uuid4().hex
    fields = (
        ("transcription", transcription),
        ("recordedAt", str(round(time.time() * 1000))),
        ("client", "ring"),
    )
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields
    ) + f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def probe_webhook(url: str, live: bool) -> None:
    """POST the shape the phone posts, and print what came back.

    The trigger header says `single-click-hold` and not `test-event` on
    purpose: a test event is answered before the capture ever reaches the
    relay, so it would prove the URL and the token and nothing past them —
    and the dry-run guard, the thing that makes this safe to fire at a live
    install, would never run.
    """
    import logging

    import httpx2

    logging.getLogger("httpx2").setLevel(logging.WARNING)

    body, content_type = phone_multipart(probe_message(live))
    print(f"probing webhook {url}" + (" (LIVE)" if live else " (dry run)"), flush=True)
    response = httpx2.post(
        url,
        content=body,
        headers={
            "Content-Type": content_type,
            "Authorization": f"Bearer {config.BRIDGE_TOKEN}",
            "X-Index-Trigger": "single-click-hold",
        },
        timeout=15,
    )
    print(f"result: {response.status_code} {response.text}")
    if not 200 <= response.status_code < 300:
        # Anything else is a failure worth an exit code — a 3xx included: the
        # app follows redirects on a POST and re-sends the whole body, so this
        # route answering with one would be a bug, not a hop.
        raise RuntimeError(f"{response.status_code} {response.text.strip()[:200]}")


def probe_mcp(url: str, live: bool, assistant: str | None) -> None:
    """Connect exactly like the phone would — initialize, list tools, call the
    send tool."""
    import logging

    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    logging.getLogger("httpx2").setLevel(logging.WARNING)

    print(
        f"probing mcp {url}"
        + (" (LIVE)" if live else " (dry run)")
        + (f" -> assistant {assistant}" if assistant else ""),
        flush=True,
    )

    async def _probe() -> None:
        headers = {"Authorization": f"Bearer {config.BRIDGE_TOKEN}"}
        args = {"message": probe_message(live)}
        if assistant:
            args["assistant"] = assistant
        async with httpx2.AsyncClient(headers=headers, timeout=15) as http:
            async with streamable_http_client(url, http_client=http) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    print("tools:", [t.name for t in tools.tools])
                    result = await session.call_tool(config.TOOL_NAME, args)
                    print("result:", result.content[0].text)

    asyncio.run(_probe())


def probe(live: bool = False, assistant: str | None = None) -> None:
    """Client-side diagnostic: talk to every configured route the way the
    phone talks to it — the MCP door over Streamable HTTP with a tools/call,
    the webhook door with the app's own multipart body. Dry run by default;
    the assistant DM is a live channel and --live sends a real message, on
    every route, that it will act on. --assistant <name> targets a mapped
    assistant on the MCP route (the webhook route's target is configuration,
    never the words). BRIDGE_URL overrides the target (e.g. probing a remote
    install)."""
    mcp_url, webhook_url = probe_urls()
    failed = []
    for route in config.ROUTES:
        try:
            if route == "webhook":
                probe_webhook(webhook_url, live)
            else:
                probe_mcp(mcp_url, live, assistant)
        except Exception as e:
            print(f"FAILED ({type(e).__name__}): {e}")
            failed.append(route)
    if failed:
        sys.exit(
            f"{', '.join(failed)}: probe failed. "
            "Is the server running, and does BRIDGE_TOKEN match?"
        )


def render_plist(label: str) -> str:
    """The launchd plist with this checkout's real paths baked in."""
    python = config.HERE / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    logs = config.HERE / "logs"
    # launchd does NOT inherit the installing shell's environment: without
    # this block a custom RINGBEARER_STATE_DIR would silently revert to the
    # checkout inside the agent — no .env found, KeepAlive crash loop.
    env_block = ""
    if config.STATE_DIR != config.HERE.resolve():
        env_block = f"""    <key>EnvironmentVariables</key>
    <dict>
        <key>RINGBEARER_STATE_DIR</key>
        <string>{config.STATE_DIR}</string>
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
        <string>{config.HERE / "ringbearer.py"}</string>
        <string>run</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{config.HERE}</string>
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
        for d, what in ((config.HERE.resolve(), "checkout"), (config.STATE_DIR, "state directory")):
            if Path.home() / blocked in d.parents:
                sys.exit(
                    f"The {what} lives under ~/{blocked}, which launchd agents can't read\n"
                    "(macOS TCC). Move it somewhere like ~/Projects and rerun."
                )
    missing = required_missing()
    if missing:
        incomplete_env_exit(missing)
    if config.TELEGRAM_ENABLED and not (config.STATE_DIR / f"{config.SESSION_NAME}.session").exists():
        sys.exit(f"No {config.SESSION_NAME}.session — run: python ringbearer.py login")
    check_bindable(config.BIND_HOST)
    s = socket.socket()
    s.settimeout(0.5)
    in_use = s.connect_ex((config.BIND_HOST, config.BIND_PORT)) == 0
    s.close()
    if in_use:
        sys.exit(
            f"Something already listens on {config.BIND_HOST}:{config.BIND_PORT} — Ctrl-C the "
            "foreground server first, then rerun."
        )

    (config.HERE / "logs").mkdir(exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(render_plist(label))
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)  # reinstall-safe
    r = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"launchctl load failed: {(r.stderr or r.stdout).strip()}")

    import urllib.request

    url = f"http://{config.BIND_HOST}:{config.BIND_PORT}/healthz"
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
        print(yellow(f"Loaded, but {url} isn't answering yet — check {config.HERE / 'logs' / 'ringbearer.error.log'}"))
    print(f"Logs: {config.HERE / 'logs'} · stop: python ringbearer.py service uninstall")


def first_run(fresh: bool = False) -> None:
    """The whole onboarding as one loop: look at what exists on disk, collect
    what's missing, end with a running server. Safe to re-run forever."""
    if not (config.STATE_DIR / ".env").exists():
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
        os.execv(sys.executable, [sys.executable, str(config.HERE / "ringbearer.py"), "--fresh"])
    missing = required_missing()
    if missing:
        incomplete_env_exit(missing)
    just_logged_in = False
    if config.TELEGRAM_ENABLED and not (config.STATE_DIR / f"{config.SESSION_NAME}.session").exists():
        if not sys.stdin.isatty():
            sys.exit(f"No {config.SESSION_NAME}.session — run: python ringbearer.py login")
        print("One more thing: Telegram login.\n")
        login()
        just_logged_in = True
        print()
    if fresh or just_logged_in:
        # First run only: the settings the user is about to need, reprinted at
        # the moment of need — setup's earlier printout has scrolled away by
        # now, buried under the login exchange.
        print_phone_settings(config.BIND_HOST, str(config.BIND_PORT), config.BRIDGE_TOKEN)
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


def main() -> None:
    """Dispatch argv to a verb. Unknown verbs (and --help) print the usage
    text, which is the package docstring."""
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        if cmd == "setup":
            setup()
        elif cmd == "login":
            login()
        elif cmd == "run":
            run()
        elif cmd == "probe":
            target = None
            if "--assistant" in sys.argv:
                idx = sys.argv.index("--assistant")
                if idx + 1 >= len(sys.argv):
                    sys.exit("--assistant needs a name (see ASSISTANTS in .env)")
                target = sys.argv[idx + 1]
            probe(live="--live" in sys.argv, assistant=target)
        elif cmd == "service":
            service(uninstall=len(sys.argv) > 2 and sys.argv[2] == "uninstall")
        elif cmd == "":
            first_run()
        elif cmd == "--fresh":
            first_run(fresh=True)
        else:
            print(USAGE)
    except (EOFError, KeyboardInterrupt):
        sys.exit("\nCancelled. Rerun `python ringbearer.py` when ready.")
    except PermissionError as e:
        sys.exit(
            f"Permission denied: {e}\n"
            f"Can this user write {config.STATE_DIR}? In Docker, see docker/README.md "
            "→ Troubleshooting (bind-mount ownership)."
        )
