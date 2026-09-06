"""The MCP server the ring's phone app talks to.

The tool it registers, the schema that tool exposes (which is the whole
discovery mechanism for multi-assistant installs), the standing prompt, and
the ASGI tee that makes tools/list traffic distinguishable from tools/call.

Everything that actually reaches Telegram lives in `telegram.relay`; this
module owns only the wire contract.
"""

import json

from mcp.server import MCPServer

from .. import config, telegram

mcp = MCPServer("ringbearer")

_BASE_DESCRIPTION = (
    f"Relay the user's spoken message to {config.ASSISTANT_NAME}, their personal "
    "assistant. It handles ALL requests (reminders, questions, tasks, "
    "notes, anything) and replies to the user directly in Telegram. This "
    "is the only action available: for EVERY user message, call this tool "
    "exactly once with the user's words verbatim and in full. Never "
    "paraphrase, never summarize, never answer the user yourself, and "
    "never skip the call — even for greetings, tests, or unclear speech."
)


def register_capture_tool(server, roster):
    """Register the send tool on `server`, shaped by the roster. Extra
    assistants add an optional `assistant` argument whose enum IS the
    discovery mechanism: the app's agent sees the valid names inside the
    tool schema it fetches at connect — no listing round trip, nothing to
    keep in sync by hand. A single-assistant install registers exactly the
    historical signature; the feature is invisible until ASSISTANTS is set."""
    if len(roster) > 1:
        from typing import Annotated

        from pydantic import Field

        description = _BASE_DESCRIPTION + (
            " The user has several assistants and this tool reaches them "
            "all. If the user addresses one by name (e.g. 'ask plutus "
            "...'), set `assistant` to that name; otherwise omit it for the "
            "default. Routing only — the message itself still goes "
            "verbatim, address phrase included."
        )
        # Advisory enum (json_schema_extra), NOT a Literal type: a Literal
        # makes the SDK reject an off-enum value before the handler ever
        # runs, and the transcript would vanish without a captures.jsonl
        # row. The schema still shows the agent the valid names; relay()
        # enforces them — and logs the words either way.
        names = Annotated[str, Field(json_schema_extra={"enum": list(roster)})]

        @server.tool(name=config.TOOL_NAME, description=description)
        async def send_to_assistant(
            message: str, assistant: names = config.DEFAULT_ASSISTANT
        ) -> str:
            return await telegram.relay(message, assistant)
    else:

        @server.tool(name=config.TOOL_NAME, description=_BASE_DESCRIPTION)
        async def send_to_assistant(message: str) -> str:
            return await telegram.relay(message, config.DEFAULT_ASSISTANT)

    return send_to_assistant


send_to_assistant = register_capture_tool(mcp, config.ASSISTANT_ROSTER)


@mcp.prompt()
def ring_routing() -> str:
    """Standing instruction for handling ring voice captures."""
    return (
        f"Every user message is a voice capture meant for {config.ASSISTANT_NAME}. "
        f"Call {config.TOOL_NAME} exactly once with the message verbatim, then reply "
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
