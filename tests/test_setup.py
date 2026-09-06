"""Onboarding: the one question that decides which doors this install uses.

`setup` is driven here the way a person drives it — answers typed at prompts,
one at a time — and what it produces is checked in both directions: the `.env`
it writes, and the phone cards it prints. The generated file is then read back
by a fresh interpreter, because a setup that writes something config cannot
parse is a setup that works only in a test.

Nothing here touches the checkout: STATE_DIR is a temporary directory, so the
`.env` these tests write lives and dies inside it.
"""

import io
import os
import subprocess
import sys
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from ringbearer import cli, config

REPO_ROOT = Path(__file__).resolve().parent.parent
WEBHOOK_CARD = "Pebble app settings — webhook route"
MCP_CARD = "Pebble app settings — MCP route"

# The five answers before the door question, in order: the two Telegram API
# values, the assistant's chat, its name (blank takes the default) and where
# to listen (blank port takes 8787).
COMMON = ("12345", "hash-goes-here", "@assistant_bot", "", "127.0.0.1", "")


@contextmanager
def scripted(*answers):
    """Run `setup` with these answers typed at it.

    Parameters:
      answers (str): one per prompt, in order. A short script means a prompt
        went unanswered, which surfaces as StopIteration rather than a hang.

    Yields: (state directory, everything setup printed).
    """
    replies = iter(answers)

    def typed(prompt: str = "") -> str:
        print(prompt, end="")  # keep the transcript readable, as a terminal would
        answer = next(replies)
        print(answer)
        return answer

    with TemporaryDirectory() as tmp:
        state = Path(tmp)
        printed = io.StringIO()
        # BIND_HOST/BIND_PORT in the environment would skip the listen
        # question (a container sets them); this is the interactive path.
        environment = {
            k: v for k, v in os.environ.items() if k not in ("BIND_HOST", "BIND_PORT")
        }
        with (
            patch.object(config, "STATE_DIR", state),
            patch.object(cli, "_COLOR", False),
            patch.dict(os.environ, environment, clear=True),
            patch("builtins.input", typed),
            redirect_stdout(printed),
        ):
            cli.setup()
        yield state, printed.getvalue()


def read_back(state: Path, expression: str) -> subprocess.CompletedProcess:
    """Evaluate `expression` against the config a fresh interpreter loads from
    the `.env` in `state` — the same read the server does at startup."""
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("ROUTES", "ASSISTANTS", "ASSISTANT_NAME", "ASSISTANT_CHAT")
    }
    env["RINGBEARER_STATE_DIR"] = str(state)
    return subprocess.run(
        [sys.executable, "-c", f"from ringbearer import config; print({expression})"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


class OneAssistantTests(unittest.TestCase):
    """"No" means one door, and the card for the other one never appears."""

    def test_no_writes_the_webhook_route_and_prints_only_its_card(self):
        with scripted(*COMMON, "n") as (state, printed):
            written = (state / ".env").read_text()
            self.assertIn("ROUTES=webhook\n", written)
            self.assertNotIn("ASSISTANTS=", written)
            self.assertIn(WEBHOOK_CARD, printed)
            self.assertNotIn(MCP_CARD, printed)
            self.assertNotIn("/ringbearer/mcp", printed)
            self.assertEqual((state / ".env").stat().st_mode & 0o777, 0o600)

    def test_a_blank_answer_is_a_no(self):
        with scripted(*COMMON, "") as (_state, printed):
            self.assertIn(WEBHOOK_CARD, printed)
            self.assertNotIn(MCP_CARD, printed)

    def test_the_file_it_wrote_is_the_file_config_reads(self):
        with scripted(*COMMON, "n") as (state, _printed):
            result = read_back(state, "config.ROUTES")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "('webhook',)")


class SeveralAssistantsTests(unittest.TestCase):
    """"Yes" adds the MCP route, its roster, and its card."""

    def test_yes_collects_the_roster_and_prints_both_cards(self):
        with scripted(*COMMON, "y", "plutus", "@plutus_bot", "") as (state, printed):
            written = (state / ".env").read_text()
            self.assertIn("ROUTES=webhook,mcp\n", written)
            self.assertIn("ASSISTANTS=plutus:@plutus_bot\n", written)
            self.assertIn(WEBHOOK_CARD, printed)
            self.assertIn(MCP_CARD, printed)

    def test_the_roster_repeats_until_a_blank_name(self):
        with scripted(
            *COMMON, "y", "plutus", "@plutus_bot", "qm", "-100123", ""
        ) as (state, _printed):
            self.assertIn("ASSISTANTS=plutus:@plutus_bot,qm:-100123\n", (state / ".env").read_text())

    def test_a_name_that_is_not_a_token_is_re_asked(self):
        with scripted(*COMMON, "y", "Plutus", "plu tus", "plutus", "@plutus_bot", "") as (
            state,
            printed,
        ):
            self.assertIn("lowercase token", printed)
            self.assertIn("ASSISTANTS=plutus:@plutus_bot\n", (state / ".env").read_text())

    def test_the_default_assistants_name_cannot_be_reused(self):
        # ASSISTANT_NAME was left blank above, so the default is `assistant`.
        with scripted(*COMMON, "y", "assistant", "plutus", "@plutus_bot", "") as (
            state,
            printed,
        ):
            self.assertIn("already taken", printed)
            self.assertIn("ASSISTANTS=plutus:@plutus_bot\n", (state / ".env").read_text())

    def test_yes_with_no_names_falls_back_to_the_webhook_route(self):
        """Answering yes and then naming nobody leaves one assistant, so it
        leaves one door — the answer that matters is the roster."""
        with scripted(*COMMON, "y", "") as (state, printed):
            self.assertIn("ROUTES=webhook\n", (state / ".env").read_text())
            self.assertNotIn(MCP_CARD, printed)

    def test_the_file_it_wrote_is_the_file_config_reads(self):
        with scripted(*COMMON, "y", "plutus", "@plutus_bot", "") as (state, _printed):
            result = read_back(state, "(config.ROUTES, list(config.ASSISTANT_ROSTER))")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout.strip(), "(('webhook', 'mcp'), ['assistant', 'plutus'])"
        )


class RoutesParsingTests(unittest.TestCase):
    """ROUTES is read at import like everything else, and an install that
    predates it (no line at all) keeps both doors."""

    def _routes(self, value=None):
        env = {k: v for k, v in os.environ.items() if k != "ROUTES"}
        env["TELEGRAM_ENABLED"] = "false"
        if value is not None:
            env["ROUTES"] = value
        return subprocess.run(
            [sys.executable, "-c", "from ringbearer import config; print(config.ROUTES)"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )

    def test_unset_means_both(self):
        result = self._routes()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "('webhook', 'mcp')")

    def test_one_route(self):
        self.assertEqual(self._routes("webhook").stdout.strip(), "('webhook',)")
        self.assertEqual(self._routes("mcp").stdout.strip(), "('mcp',)")

    def test_both_routes_in_either_written_order(self):
        for value in ("webhook,mcp", "mcp,webhook", " WEBHOOK , mcp "):
            with self.subTest(value=value):
                self.assertEqual(self._routes(value).stdout.strip(), "('webhook', 'mcp')")

    def test_an_unknown_route_exits_with_the_valid_names(self):
        for value in ("nonsense", "webhook,nonsense", ","):
            with self.subTest(value=value):
                result = self._routes(value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ROUTES", result.stderr)
                self.assertIn("webhook and mcp", result.stderr)


class PhoneCardTests(unittest.TestCase):
    """The cards are the whole phone-side instruction manual — the facts on
    them are load-bearing, and a trim that loses one loses a working install."""

    def card(self, *routes):
        printed = io.StringIO()
        with patch.object(cli, "_COLOR", False), redirect_stdout(printed):
            cli.print_phone_settings("100.1.2.3", "8787", "T0KEN", routes)
        return printed.getvalue()

    def test_the_webhook_card_carries_every_fact_that_matters(self):
        card = self.card("webhook")
        self.assertIn("Two halves, both required", card)
        self.assertIn('"Webhook only"', card)
        self.assertIn("Webhook settings and save the URL", card)
        self.assertIn("delivers twice", card)
        self.assertIn("http://100.1.2.3:8787/ringbearer/webhook", card)
        self.assertIn("Authorization: Bearer T0KEN", card)
        self.assertIn('"Recording only"', card)
        self.assertIn('"Both" or "Transcription only"', card)
        self.assertIn("Send test event", card)
        self.assertIn("forwards nothing", card)
        self.assertIn('"Also send to webhook" off', card)

    def test_the_mcp_card_keeps_its_settings_and_gains_the_webhook_warning(self):
        card = self.card("mcp")
        self.assertIn("http://100.1.2.3:8787/ringbearer/mcp", card)
        self.assertIn("Authorization: Bearer T0KEN", card)
        self.assertIn("WITHOUT spaces", card)
        self.assertIn("MCP Sandbox", card)
        self.assertIn("Also send to webhook: off", card)

    def test_a_docker_listener_is_not_offered_as_a_dialable_address(self):
        printed = io.StringIO()
        with patch.object(cli, "_COLOR", False), redirect_stdout(printed):
            cli.print_phone_settings("0.0.0.0", "8787", "T0KEN", ("webhook",))
        self.assertIn("<this-machine's-address>", printed.getvalue())
        self.assertNotIn("http://0.0.0.0", printed.getvalue())


if __name__ == "__main__":
    unittest.main()
