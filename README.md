# ringbearer

> *One does not simply type.*

![Python 3.11+](https://img.shields.io/badge/python-3.11+-3776AB) ![License: MIT](https://img.shields.io/badge/license-MIT-lightgrey)

Turn the **Pebble Index 01 ring** into a push-to-talk button for your own AI
assistant, using its Telegram DM as the conversation surface.

Double-click-hold the ring and speak. The Pebble app transcribes, its MCP
sandbox agent calls this bridge's one tool, and the bridge posts your words
into your existing Telegram DM with the assistant — **as you, from your own
Telegram account**. The assistant sees a normal message in a thread it already
knows, replies with full context, and the whole exchange stays auditable in
Telegram like any other conversation. Start something from the ring on a walk,
finish it on your phone later.

Works with any assistant that lives in a Telegram chat: Hermes, OpenClaw, a
bot you wrote yourself. ~280 lines of Python — FastAPI, the MCP SDK, Pyrogram.

## How it works

```mermaid
sequenceDiagram
    participant Ring as Index 01
    participant Phone as Pebble app
    participant Cloud as Core cloud agent
    participant RB as ringbearer
    participant TG as Telegram DM

    Ring->>Phone: double-click-hold + speech
    Phone->>Phone: transcribe on device
    Phone->>Cloud: transcript + tool list
    Cloud->>Phone: call send_to_*
    Phone->>RB: tools/call (Streamable HTTP, bearer token)
    RB->>TG: post transcript as you (Pyrogram)
    TG->>TG: assistant replies in-thread
```

The bridge exposes exactly **one MCP tool** — `send_to_<assistant>` — whose
description tells the app's agent to relay every message verbatim and never
answer itself. Your assistant's actual capabilities are never exposed to the
app's cloud; it just gets a pipe.

## Quick start

```bash
git clone https://github.com/MDB4241/ringbearer && cd ringbearer
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

.venv/bin/python bridge.py setup    # interactive: walks you through every secret
.venv/bin/python bridge.py login    # one-time Telegram login (as YOUR account)
.venv/bin/uvicorn bridge:app --host <ip-the-phone-can-reach> --port 8787
```

`setup` generates the bearer token, points you at
[my.telegram.org/apps](https://my.telegram.org/apps) for API credentials, asks
which chat your assistant lives in, and writes `.env` (mode 600). `login`
creates the Pyrogram session file. That's all the state there is.

Verify without touching your real DM:

```bash
.venv/bin/python test_mcp.py           # dry run — exercises auth + MCP + logging
.venv/bin/python test_mcp.py --live    # actually sends one probe message
```

## Phone settings (Pebble app)

Under Index settings → MCP servers:

- **URL:** `http://<host>:8787/bridge/mcp` — transport **Streamable**
- **Header:** `Authorization: Bearer <BRIDGE_TOKEN>`
- **Server name: no spaces.** The app sanitizes names for the LLM but
  dispatches tool calls on the original name, so a space breaks the round trip
  with "Invalid tool call" (reported upstream).
- Sandbox group **model type: Default** — the "Index Agent" type ignores
  custom MCP servers.
- Then **Secondary Mode → MCP Sandbox** → select the server group (the OK
  button stays disabled until a group is picked).

## Running it as a service (macOS)

[`ringbearer.plist.example`](ringbearer.plist.example) is a launchd user-agent
template — fill in your paths and listen IP, copy to `~/Library/LaunchAgents/`,
`launchctl load`.

Two macOS traps it already accounts for:

- The checkout must **not** live under `~/Documents`, `~/Desktop`, or
  `~/Downloads` — TCC blocks launchd agents from reading those.
- Pyrogram anchors its session file to the *launching script's* directory
  (`.venv/bin` under uvicorn); `bridge.py` pins `workdir` to its own directory
  so the session `login` writes is the one the server finds.

## Security notes

- **Bind deliberately.** Prefer a [Tailscale](https://tailscale.com) address so
  the bridge is reachable from anywhere but never exposed to the LAN or
  internet; WireGuard makes plain HTTP acceptable inside the tailnet. Never
  port-forward this.
- **The bearer token is the gate.** Everything except `/healthz` requires it;
  the server refuses to start without one. Comparison is constant-time.
- **The session file is your Telegram account.** `*.session` is gitignored;
  treat it like a password. One process per session file — a second client on
  the same session risks `AUTH_KEY_DUPLICATED` and revocation.
- **Probes are dry-run by default.** A live probe is a real message your real
  assistant will act on; `--live` is a deliberate flag for that reason.

## Design notes

This is, truthfully, webhook functionality built on MCP. The app's own webhook
mode runs a full agent turn and files every capture as a note, so the MCP
sandbox is the clean path today. There's an upstream feature request for a
direct webhook-only mode
([coredevices/mobileapp#313](https://github.com/coredevices/mobileapp/issues/313));
if it lands, this gets even simpler.

## Configuration

All via `.env` (see [`.env.example`](.env.example)):

| Variable | Meaning |
|---|---|
| `BRIDGE_TOKEN` | Bearer token the phone must send (setup generates one) |
| `TELEGRAM_ENABLED` | `true` to actually deliver; `false` = log-only mode |
| `ASSISTANT_CHAT` | `@botusername` or chat id of the assistant DM |
| `ASSISTANT_NAME` | Display name; becomes the tool name (`Hermes` → `send_to_hermes`) |
| `TG_API_ID` / `TG_API_HASH` | Telegram API credentials (my.telegram.org) |
| `SESSION_NAME` | Pyrogram session file name (default `ringbearer`) |
| `RING_PREFIX` | Prefix on relayed messages (default 🎤) |
| `MCP_MOUNT` | Mount path; endpoint is `<MCP_MOUNT>/mcp` (default `/bridge`) |

## License

[MIT](LICENSE)
