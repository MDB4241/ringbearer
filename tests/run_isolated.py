"""Run offline tests with disposable state, never a real Telegram session.

Usage: .venv/bin/python tests/run_isolated.py [unittest dotted test names]
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def main():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    with (
        tempfile.TemporaryDirectory(prefix="ringbearer-tests-") as state,
        patch.dict(os.environ, {
            "RINGBEARER_STATE_DIR": state,
            "TELEGRAM_ENABLED": "false",
            "NEW_TOPIC_PER_CAPTURE": "false",
            "TG_API_ID": "1",
            "TG_API_HASH": "test-only",
            "BRIDGE_TOKEN": "test-only",
            "ASSISTANT_NAME": "assistant",
            "ASSISTANT_CHAT": "@test_bot",
            "ASSISTANTS": "",
            "DELIVERY_CONTEXT": "conversation",
            "SESSION_NAME": "test-only",
        }),
    ):
        import telethon

        with patch.object(
            telethon, "TelegramClient",
            side_effect=AssertionError("Tests must supply a fake Telegram client"),
        ):
            loader = unittest.defaultTestLoader
            suite = (
                loader.loadTestsFromNames(sys.argv[1:]) if sys.argv[1:]
                else loader.discover(str(root / "tests"), top_level_dir=str(root))
            )
            result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
