"""
ringbearer: Pebble Index 01 ring -> your AI assistant's Telegram DM.

One does not simply type. Ringbearer carries your spoken words from the ring
to your assistant, delivered as you.

Double-click-hold the ring and speak. The Pebble app's MCP sandbox agent calls
this bridge's send_to_assistant tool with the transcript, and the bridge
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
                            would and call the tool (dry run; --live sends;
                            --assistant <name> targets a mapped assistant)
  python ringbearer.py service  install the launchd agent — a real background
                            service (macOS; `service uninstall` removes it)

Endpoints:
  <MCP_MOUNT>/mcp   MCP server (Streamable HTTP), bearer-token gated.
  /healthz          Open health check: 200 when Telegram is reachable,
                    503 when it is not. Talks to no one; reads state.
"""

import sys

if sys.version_info < (3, 10):
    sys.exit(f"ringbearer needs Python 3.10+ (you have {sys.version.split()[0]}).")
