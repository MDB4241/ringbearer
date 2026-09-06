"""The fast door: the phone posts the capture straight here.

No cloud agent, no tool call, no LLM between the ring and the assistant — the
Pebble app's "Webhook only" gesture POSTs its multipart to this route, the
bridge makes the text (or takes the phone's, if the payload carries one) and
hands it to the same delivery path the MCP door uses.

Two things this door deliberately does NOT do. It never reads the words to
choose a destination: every capture goes to `config.WEBHOOK_ASSISTANT` (A10).
And it never redirects — the app follows 301/302 on a POST and re-sends the
whole body, so `/webhook` and `/webhook/` are both registered and both answer.

The wire contract it accepts is the app's own, verified from its source: parts
`audio` (m4a, filename `<recordingId>.m4a`), `transcription`, `test`,
`recordedAt` and `client`, plus `X-Index-Trigger` and `X-Index-Test` headers.
`recordedAt` and `client` are accepted and ignored — the row's clock is this
machine's, and there is only one kind of client.
"""

import asyncio
import time
from collections import deque
from datetime import datetime

from fastapi import APIRouter, File, Form, Header, UploadFile

from .. import config, telegram, transcribe

router = APIRouter()

WEBHOOK_PATH = f"{config.MCP_MOUNT}/webhook"
DELIVER_TIMEOUT = 30  # same bound as the MCP door: a stall must not hold the row

# Recently accepted audio filenames — the phone's `<recordingId>.m4a`, which
# without request signing is the only stable id it offers. Bounded and in
# memory on purpose: the sole duplicate the app can produce is a queued retry
# across an app restart mid-flight, and a file on disk would insure against
# something rarer than the bug it adds. A payload with no audio part therefore
# has no stable id at all and is never deduped.
SEEN_LIMIT = 256
_seen: deque = deque(maxlen=SEEN_LIMIT)

NOTHING_TO_SAY = "no transcription and no audio in payload"


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() == "true"


@router.post(WEBHOOK_PATH)
@router.post(f"{WEBHOOK_PATH}/")
async def webhook(
    audio: UploadFile | None = File(default=None),
    transcription: str | None = Form(default=None),
    test: str | None = Form(default=None),
    trigger: str | None = Header(default=None, alias="X-Index-Trigger"),
    test_header: str | None = Header(default=None, alias="X-Index-Test"),
):
    """Take one ring capture off the phone and deliver it.

    Returns: a small JSON body describing what happened. Any 2xx satisfies the
    app, which never reads a successful body — the body is for curl and for a
    human reading `Recent runs` after a non-2xx. Every outcome short of an
    unhandled crash is a 200: a payload with nothing in it is a fact to log,
    not a server error, and there is no retry on the phone for a 5xx to earn.
    """
    received_at = datetime.now().astimezone().isoformat()
    data = await audio.read() if audio is not None else b""
    filename = audio.filename if audio is not None else None
    # A `transcription` part wins outright — the phone already did the work, so
    # the engine never runs (C63). Blank counts as absent: an empty part with
    # audio beside it means the phone's transcriber came back with nothing, and
    # relaying silence when the audio is right there would be the wrong call.
    text = transcription if (transcription or "").strip() else None
    is_test = _is_true(test) or _is_true(test_header)
    duplicate = bool(filename) and filename in _seen

    transcribe_ms = None
    forwarded = False
    telegram_ms = None
    dry_run = False
    reason = None
    error = None

    try:
        if is_test:
            # A12: the app's Send test event button proves the endpoint, the
            # token and the headers. It must never reach a real assistant.
            reason = "test event"
        elif duplicate:
            reason = "duplicate capture"
        elif text is None and not data:
            reason = NOTHING_TO_SAY
        else:
            if filename:
                _seen.append(filename)
            if text is None:
                try:
                    text, transcribe_ms = await transcribe.run(data, filename)
                except Exception as e:
                    error, reason = repr(e), "transcription failed"
            if error is None and not (text or "").strip():
                reason = "transcription produced no text"
            elif error is None and text.startswith(config.DRY_RUN_PREFIX):
                # Same probe guard as the MCP door: exercise auth, parsing,
                # routing and logging without writing to a live assistant DM.
                dry_run = True
            elif error is None:
                started = time.monotonic()
                try:
                    forwarded = await asyncio.wait_for(
                        telegram.deliver(text, config.WEBHOOK_ASSISTANT),
                        timeout=DELIVER_TIMEOUT,
                    )
                except asyncio.CancelledError as e:
                    if asyncio.current_task().cancelling():
                        # A dropped request or a shutting-down server. The row
                        # in the finally below is the only copy of the
                        # transcript, so it still gets written on the way out.
                        error = repr(e)
                        raise
                    # Telethon cancels the send's future when it tears the
                    # connection down mid-send: a failed delivery, not a
                    # cancelled request. Same reading as the MCP door.
                    error = "ConnectionError: Telegram tore the connection down mid-send"
                    telegram.request_recheck()
                except Exception as e:
                    error = repr(e)
                    telegram.request_recheck()
                telegram_ms = round((time.monotonic() - started) * 1000)
    finally:
        row = {
            "received_at": received_at,
            "source": "webhook",
            "trigger": trigger,
            "assistant": config.WEBHOOK_ASSISTANT,
            "transcription": text,
        }
        if audio is not None:
            # A11: how much audio arrived, never the audio itself.
            row["audio_bytes"] = len(data)
        if transcribe_ms is not None:
            row["transcribe_ms"] = transcribe_ms
            row["engine"] = transcribe.ENGINE
            row["model"] = config.TRANSCRIBE_MODEL
        row["test"] = is_test
        row["duplicate"] = duplicate
        row["forwarded"] = forwarded
        if telegram_ms is not None:
            row["telegram_ms"] = telegram_ms
        if dry_run:
            row["dry_run"] = True
        if reason:
            row["reason"] = reason
        if error:
            row["error"] = error
        telegram.log_capture(row)
        timings = "".join(
            f" {label} {ms}ms"
            for label, ms in (("transcribe", transcribe_ms), ("telegram", telegram_ms))
            if ms is not None
        )
        print(
            f"[webhook] {trigger or 'no-trigger'}:{timings or ' not delivered'}"
            + (f" ({reason})" if reason else "")
            + (f" ERROR {error}" if error else ""),
            flush=True,
        )

    body = {
        "ok": True,
        "forwarded": forwarded,
        "assistant": config.WEBHOOK_ASSISTANT,
        "test": is_test,
        "duplicate": duplicate,
    }
    if transcribe_ms is not None:
        body["transcribe_ms"] = transcribe_ms
    if dry_run:
        body["dry_run"] = True
    if reason:
        body["reason"] = reason
    if error:
        body["error"] = error
    return body
