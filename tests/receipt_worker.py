"""Fresh-process receipt tests with file-backed evidence of fake sends only."""

import asyncio
import os
import sys
from pathlib import Path
from unittest.mock import patch


def main():
    state, mode = Path(sys.argv[1]), sys.argv[2]
    # Set the state directory BEFORE importing ringbearer; never load user config.
    os.environ.update({
        "RINGBEARER_STATE_DIR": str(state),
        "TELEGRAM_ENABLED": "false",
        "TG_API_ID": "1",
        "TG_API_HASH": "test-only",
        "ASSISTANT_NAME": "assistant",
        "ASSISTANT_CHAT": "@test_bot",
        "ASSISTANTS": "",
        "DELIVERY_CONTEXT": "conversation",
        "NEW_TOPIC_PER_CAPTURE": "false",
    })
    import ringbearer as rb

    class Client:
        async def send_message(self, *args, **kwargs):
            with (state / "fake-sends.txt").open("a") as stream:
                stream.write("send\n")
            sys.stdout.write("SENDING\n")
            sys.stdout.flush()
            if mode == "crash":
                os._exit(23)
            if mode == "hold":
                await asyncio.to_thread(sys.stdin.readline)

    with (
        patch.multiple(rb, TELEGRAM_ENABLED=True, tg_client=Client()),
        patch.object(rb, "log_capture"),
        patch("builtins.print"),
    ):
        result = asyncio.run(rb.relay("one action", "assistant", capture_id="process-id"))
    print(result)


if __name__ == "__main__":
    main()
