"""The phone-visible contract, pinned.

Two shapes here belong to other people's configuration, not to this code. The
MCP tool and prompt schema is what the Pebble app's agent fetches at connect —
the tool name, the description its LLM reads, the argument names it must
produce. `/healthz` is what the shipped Docker healthcheck reads. Neither may
drift silently, so both are captured as fixtures and compared field for field.
A refactor that moves code and nothing else leaves these untouched.

Capture runs in a fresh interpreter on purpose: ASSISTANTS is read at import
time, so the multi-assistant shape only exists in a process that started with
it set, and no amount of reloading inside this one is honest about that.
"""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO_ROOT = TESTS.parent
FIXTURES = TESTS / "fixtures"

# The capture, run in a subprocess. Only the import lines at the top move when
# the module layout changes; everything below them is the contract itself.
CAPTURE = """
from ringbearer import app, mcp

import asyncio
import json
import sys


async def main():
    tools = await mcp.list_tools()
    prompts = await mcp.list_prompts()
    out = {
        "tools": [json.loads(t.model_dump_json(exclude_none=True)) for t in tools],
        "prompts": [json.loads(p.model_dump_json(exclude_none=True)) for p in prompts],
    }
    from fastapi.testclient import TestClient

    # No lifespan: this asks the handler for its shape, not the server for a
    # Telegram connection.
    response = TestClient(app).get("/healthz")
    out["healthz"] = {
        "status": response.status_code,
        "keys": sorted(response.json().keys()),
    }
    json.dump(out, sys.stdout, indent=2, sort_keys=True)


asyncio.run(main())
"""


def capture(assistants=None):
    """Serialize the live tool/prompt schema and the /healthz shape.

    Parameters:
      assistants (str | None): value for the ASSISTANTS environment variable;
        None removes it, which is the single-assistant install.

    Returns: the captured dict, in the same shape as the fixtures.
    """
    env = {k: v for k, v in os.environ.items() if k != "ASSISTANTS"}
    env["TELEGRAM_ENABLED"] = "false"
    if assistants is not None:
        env["ASSISTANTS"] = assistants
    result = subprocess.run(
        [sys.executable, "-c", CAPTURE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(f"capture failed:\n{result.stdout}\n{result.stderr}")
    return json.loads(result.stdout)


class SchemaFixtureTests(unittest.TestCase):
    maxDiff = None

    def assert_matches_fixture(self, name, captured):
        expected = json.loads((FIXTURES / name).read_text())
        self.assertEqual(captured["tools"], expected["tools"])
        self.assertEqual(captured["prompts"], expected["prompts"])
        self.assertEqual(captured["healthz"], expected["healthz"])

    def test_single_assistant_schema_is_unchanged(self):
        self.assert_matches_fixture("mcp_schema_single.json", capture())

    def test_multi_assistant_schema_is_unchanged(self):
        """ASSISTANTS grows the tool an `assistant` enum — the discovery
        mechanism itself, so its exact shape is part of the contract."""
        self.assert_matches_fixture(
            "mcp_schema_multi.json", capture("second:123456")
        )


if __name__ == "__main__":
    unittest.main()
