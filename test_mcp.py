#!/usr/bin/env python3
"""Probe the bridge's MCP endpoint the way the phone app would:
initialize, list tools, call the send_to_* tool. Usage:

    python test_mcp.py            # dry run — nothing reaches Telegram
    python test_mcp.py --live     # ACTUALLY messages the assistant DM
    python test_mcp.py --no-auth  # without token (expect failure)

Dry run is the default on purpose: the assistant DM is a live channel,
and every real send is a message the assistant acts on.
"""

import asyncio
import json
import os
import sys

import httpx2
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

load_dotenv()

MOUNT = os.environ.get("MCP_MOUNT", "/bridge")
URL = os.environ.get("BRIDGE_URL", f"http://localhost:8787{MOUNT}/mcp")


def probe_message() -> str:
    text = "bridge probe, no reply needed"
    return text if "--live" in sys.argv else f"DRYRUN: {text}"


async def main(with_auth: bool) -> None:
    headers = (
        {"Authorization": f"Bearer {os.environ['BRIDGE_TOKEN']}"} if with_auth else {}
    )
    async with httpx2.AsyncClient(headers=headers, timeout=15) as http:
        async with streamable_http_client(URL, http_client=http) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                print("tools:", [t.name for t in tools.tools])
                for t in tools.tools:
                    print("inputSchema:", json.dumps(t.input_schema))
                send_tool = next(
                    (t.name for t in tools.tools if t.name.startswith("send_to_")), None
                )
                if send_tool is None:
                    sys.exit("no send_to_* tool exposed — is this the right server?")
                result = await session.call_tool(send_tool, {"message": probe_message()})
                print("result:", result.content[0].text)


if __name__ == "__main__":
    with_auth = "--no-auth" not in sys.argv
    try:
        asyncio.run(main(with_auth))
    except Exception as e:
        print(f"FAILED ({type(e).__name__}): {e}")
        sys.exit(1)
